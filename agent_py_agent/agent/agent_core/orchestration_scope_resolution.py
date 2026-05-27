# LLM: Scope resolution records why a tool used explicit ids or the active runner context.
# 模块用途: 给 tree/dispatch 等编排工具输出结构化身份裁决；线程上下文只能做权限上界，不能静默覆盖显式身份。

from __future__ import annotations

from dataclasses import dataclass, field

from .parameters import _string_list
from .runner_context import current_subagent_run_id

_OVERRIDE_WARNING = "explicit_scope_overridden_by_current_runner"


@dataclass(frozen=True)
class ScopeResolution:
    source: str
    effective: dict[str, object] = field(default_factory=dict)
    explicit: dict[str, object] = field(default_factory=dict)
    ignored_explicit: dict[str, object] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "effective": dict(self.effective),
            "explicit": dict(self.explicit),
            "ignored_explicit": dict(self.ignored_explicit),
            "warnings": list(self.warnings),
        }


# LLM: tree_scope_resolution makes current-runner overrides visible without widening permissions.
# 函数用途: inspect_agent_tree 在 runner 内只能看当前子树；如模型传了外部 root/run，返回 warning 而不是静默吞掉。
def tree_scope_resolution(
    agent: object,
    params: dict[str, object],
    *,
    effective_run_id: str = "",
    effective_root_id: str = "",
    effective_scope: str = "",
) -> ScopeResolution:
    explicit = _explicit_scope(params, include_parent=False)
    current = current_subagent_run_id(agent)
    effective = _clean_mapping(
        {
            "run_id": effective_run_id,
            "root_id": effective_root_id,
            "scope": effective_scope,
        }
    )
    ignored = explicit if current and explicit else {}
    warnings = [_OVERRIDE_WARNING] if ignored else []
    return ScopeResolution(
        source="current_runner_context" if current else _source_for_explicit(explicit),
        effective=effective,
        explicit=explicit,
        ignored_explicit=ignored,
        warnings=warnings,
    )


# LLM: dispatch_scope_resolution explains runner-context dispatch ownership in the tool payload.
# 函数用途: dispatch_subagents 仍按当前 runner 约束 parent，但把被覆盖的 parent/root 显式写出来。
def dispatch_scope_resolution(
    agent: object,
    params: dict[str, object],
    *,
    effective_parent_run_id: str = "",
    effective_root_id: str = "",
    effective_run_ids: list[str] | None = None,
) -> ScopeResolution:
    explicit = _explicit_scope(params, include_parent=True)
    current = current_subagent_run_id(agent)
    effective = _clean_mapping(
        {
            "parent_run_id": effective_parent_run_id,
            "root_id": effective_root_id,
            "run_ids": [item for item in (effective_run_ids or []) if item],
        }
    )
    ignored = _ignored_by_current_runner(explicit, current)
    warnings = [_OVERRIDE_WARNING] if ignored else []
    return ScopeResolution(
        source="current_runner_context" if current else _source_for_explicit(explicit),
        effective=effective,
        explicit=explicit,
        ignored_explicit=ignored,
        warnings=warnings,
    )


def scope_resolution_payload(resolution: ScopeResolution) -> dict[str, object]:
    payload = {"scope_resolution": resolution.to_dict()}
    if resolution.warnings:
        payload["scope_warnings"] = list(resolution.warnings)
    return payload


# LLM: identity_scope_resolution is for model-facing tools that act as one agent.
# 函数用途: 消息、能力申请和协作证据等工具在 runner 内以当前 run 为准；显式冲突只返回 warning，不静默冒充。
def identity_scope_resolution(
    agent: object,
    params: dict[str, object],
    *,
    explicit_keys: tuple[str, ...],
    effective_agent_id: str = "",
) -> ScopeResolution:
    explicit = _explicit_identity(params, explicit_keys)
    current = current_subagent_run_id(agent)
    effective = _clean_mapping(
        {
            "agent_id": effective_agent_id or current or _first_explicit_value(explicit),
        }
    )
    ignored = _ignored_identity_by_current_runner(explicit, current)
    warnings = [_OVERRIDE_WARNING] if ignored else []
    return ScopeResolution(
        source="current_runner_context" if current else _source_for_explicit(explicit),
        effective=effective,
        explicit=explicit,
        ignored_explicit=ignored,
        warnings=warnings,
    )


def _explicit_scope(params: dict[str, object], *, include_parent: bool) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key in ("run_id", "root_id", "scope"):
        value = str(params.get(key) or "").strip()
        if value:
            payload[key] = value
    if include_parent:
        parent = str(params.get("parent_run_id") or "").strip()
        if parent:
            payload["parent_run_id"] = parent
        run_ids = _explicit_run_ids(params)
        if run_ids:
            payload["run_ids"] = run_ids
    return payload


def _explicit_run_ids(params: dict[str, object]) -> list[str]:
    ids: list[str] = []
    for key in (
        "run_ids",
        "include_run_ids",
        "dispatch_run_ids",
        "subagent_run_ids",
        "subagent_ids",
        "target_run_ids",
        "target_subagent_ids",
        "agent_ids",
        "child_run_ids",
    ):
        for run_id in _string_list(params.get(key)):
            if run_id not in ids:
                ids.append(run_id)
    return ids


def _ignored_by_current_runner(explicit: dict[str, object], current: str) -> dict[str, object]:
    if not current:
        return {}
    ignored: dict[str, object] = {}
    parent = str(explicit.get("parent_run_id") or "").strip()
    if parent and parent != current:
        ignored["parent_run_id"] = parent
    root_id = str(explicit.get("root_id") or "").strip()
    if root_id:
        ignored["root_id"] = root_id
    return ignored


def _explicit_identity(params: dict[str, object], keys: tuple[str, ...]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key in keys:
        value = str(params.get(key) or "").strip()
        if value:
            payload[key] = value
    return payload


def _ignored_identity_by_current_runner(explicit: dict[str, object], current: str) -> dict[str, object]:
    if not current:
        return {}
    return {
        key: value
        for key, value in explicit.items()
        if str(value or "").strip() and str(value or "").strip() != current
    }


def _first_explicit_value(explicit: dict[str, object]) -> str:
    for value in explicit.values():
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _source_for_explicit(explicit: dict[str, object]) -> str:
    return "explicit_params" if explicit else "default_scope"


def _clean_mapping(values: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values.items():
        if isinstance(value, list):
            cleaned = [str(item) for item in value if str(item or "").strip()]
            if cleaned:
                result[key] = cleaned
            continue
        text = str(value or "").strip()
        if text:
            result[key] = text
    return result


__all__ = [
    "ScopeResolution",
    "dispatch_scope_resolution",
    "identity_scope_resolution",
    "scope_resolution_payload",
    "tree_scope_resolution",
]
