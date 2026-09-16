# LLM: 范围裁决是宿主结构化诊断，不接受自然语言授权；调用方必须提供当前执行身份。
# 模块用途: 统一说明树查询、派工和消息工具实际使用的身份以及被忽略的冲突参数。

from __future__ import annotations

from dataclasses import dataclass, field

from ...common.value_parsing import TOOL_TEXT_LIST_OPTIONS, string_list
from ..runner.context import current_subagent_run_id

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


# LLM: 裁决只说明宿主选择了哪个 run/root/thread；显式请求不得覆盖正在运行的子代理身份。
# 函数用途: 将查询范围与被忽略参数公开为结构化诊断，不从提示正文推断权限。
def tree_scope_resolution(
    agent: object,
    params: dict[str, object],
    *,
    effective_run_id: str = "",
    effective_root_id: str = "",
    effective_scope: str = "",
    effective_thread_id: str = "",
) -> ScopeResolution:
    explicit = _explicit_scope(params, include_parent=False)
    current = current_subagent_run_id(agent)
    effective = _clean_mapping(
        {
            "run_id": effective_run_id,
            "root_id": effective_root_id,
            "scope": effective_scope,
            "thread_id": effective_thread_id,
        }
    )
    ignored = explicit if current and explicit else {}
    warnings = [_OVERRIDE_WARNING] if ignored else []
    return ScopeResolution(
        source="current_runner_context" if current else "current_conversation_context" if effective_thread_id else _source_for_explicit(explicit),
        effective=effective,
        explicit=explicit,
        ignored_explicit=ignored,
        warnings=warnings,
    )


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
    for run_id in string_list(params.get("run_ids"), TOOL_TEXT_LIST_OPTIONS):
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
            _set_clean_list(result, key, value)
            continue
        text = str(value or "").strip()
        if text:
            result[key] = text
    return result


def _set_clean_list(target: dict[str, object], key: str, value: list[object]) -> None:
    cleaned = [str(item) for item in value if str(item or "").strip()]
    if cleaned:
        target[key] = cleaned


__all__ = [
    "ScopeResolution",
    "dispatch_scope_resolution",
    "identity_scope_resolution",
    "scope_resolution_payload",
    "tree_scope_resolution",
]
