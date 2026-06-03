
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...common.value_parsing import TOOL_TEXT_LIST_OPTIONS, string_list
from ...path_access_policy import PathAccessPolicy
from ..runner.ref_fields import params_output_refs
from .create_target_roots import (
    agent_workspace_roots,
)

WRITE_SUBAGENT_TOOLS = {"write_file", "apply_patch"}


@dataclass(frozen=True)
class ExternalWriteTargetRequest:
    agent: object
    allowed_tools: list[str]
    params: dict[str, object]


def external_write_target_error(request: ExternalWriteTargetRequest) -> str:
    if not WRITE_SUBAGENT_TOOLS.intersection({str(item or "") for item in request.allowed_tools or []}):
        return ""
    targets = _structured_write_targets(request.params)
    if not targets:
        return ""
    roots = _workspace_roots(request.agent)
    workspace_root = roots[0] if roots else Path.cwd().resolve(strict=False)
    path_policy = PathAccessPolicy.from_config(getattr(request.agent, "config", None))
    for target in targets:
        path = _target_path(target, workspace_root)
        if path is None:
            continue
        decision = path_policy.check(path)
        if decision.allowed:
            continue
        if decision.code != "PATH_DANGEROUS_ROOT_BLOCKED" and (suggestion := _workspace_typo_message(path, roots)):
            return suggestion
        return (
            "子代理写入目标位于危险目录，当前 path_access_mode=normal 不允许访问: "
            f"target={path}; dangerous_root={decision.dangerous_root}"
        )
    return ""


def _structured_write_targets(params: dict[str, object]) -> list[str]:
    targets: list[str] = []
    for value in _target_sources(params):
        _append_target_strings(targets, string_list(value, TOOL_TEXT_LIST_OPTIONS))
    return targets


def _target_sources(params: dict[str, object]) -> list[object]:
    return [
        params.get("extra_write_roots"),
        params.get("write_roots"),
        params.get("target_roots"),
        params_output_refs(params),
    ]


def _append_target_strings(targets: list[str], values: list[str]) -> None:
    for item in values:
        if item and "://" not in item and item not in targets:
            targets.append(item)


def _workspace_roots(agent) -> list[Path]:
    raw = getattr(getattr(agent, "subagents", None), "workspace_root", None)
    if not isinstance(raw, str | Path):
        return []
    root = Path(raw).expanduser().resolve(strict=False)
    return agent_workspace_roots(agent, root)


def _target_path(target: str, workspace_root: Path) -> Path | None:
    text = str(target or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = workspace_root / path
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _workspace_typo_message(target: Path, roots: list[Path]) -> str:
    for root in roots:
        suffix = _shared_workspace_suffix(target, root)
        if not suffix:
            continue
        suggested = root.joinpath(*suffix)
        return (
            "suspected_path_typo=true;"
            f"target={target};"
            f"suggested_target={suggested};"
            "请使用 suggested_target 重新调用 schedule_child_subagents 或 create_subagents，"
            "不要写 capability_request。"
        )
    return ""


def _shared_workspace_suffix(target: Path, root: Path) -> list[str]:
    root_parts = root.parts
    target_parts = target.parts
    if not root_parts:
        return []
    workspace_name = root_parts[-1]
    matches = [index for index, part in enumerate(target_parts) if part == workspace_name]
    for index in matches:
        if target_parts[index:] and target_parts[index] == workspace_name:
            return list(target_parts[index + 1 :])
    return []
