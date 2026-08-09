
"""Canonical state payloads and locator/projection records for subagent runs.

The detailed state lives in task-local ``work/agents/<run_id>/canonical_state``.
Task/run JSON files and owner projections are locators or compact views, so
loaders jump back to the canonical payload when it exists.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ...common.json_io import write_json_file_atomic
from ...model_visible_refs import has_placeholder_path_segment
from ..models import SubAgentTask

CANONICAL_STATE_FILENAME = "canonical_state.json"
STATE_LOCATOR_SCHEMA_VERSION = "subagent-state-locator.v1"


@dataclass(frozen=True)
class AgentRunState:
    run_id: str
    canonical_path: Path | None
    payload: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(self.payload, ensure_ascii=False, indent=2)


def build_agent_run_state(task: SubAgentTask) -> AgentRunState:
    path = canonical_state_path_for_task(task)
    return AgentRunState(run_id=task.id, canonical_path=path, payload=asdict(task))


def build_agent_state_locator(task: SubAgentTask, state: AgentRunState) -> dict[str, Any]:
    return {
        "schema_version": STATE_LOCATOR_SCHEMA_VERSION,
        "kind": "subagent_state_locator",
        "run_id": task.id,
        "agent_id": task.id,
        "task_id": task.root_id or task.id,
        "root_id": task.root_id or task.id,
        "parent_id": task.parent_id,
        "canonical_state_ref": str(state.canonical_path or ""),
        "task_workspace_dir": task.task_workspace_dir,
        "agent_run_workspace_dir": task.agent_run_workspace_dir,
        "updated_at": task.updated_at,
        "status_mirror": task.status,
    }


def build_owner_agent_projection(task: SubAgentTask, state: AgentRunState) -> dict[str, Any]:
    return {
        "schema_version": "owner-agent-projection.v1",
        "agent_id": task.id,
        "run_id": task.id,
        "task_id": task.root_id or task.id,
        "root_id": task.root_id or task.id,
        "parent_id": task.parent_id,
        "owner_id": task.owner,
        "status": task.status,
        "progress": task.progress,
        "current_tool": task.current_tool,
        "last_progress_at": task.last_progress_at,
        "last_progress_summary": task.last_progress_summary,
        "artifact_refs": list(getattr(task, "artifact_refs", []) or []),
        "evidence_refs": list(getattr(task, "evidence_refs", []) or []),
        "declared_output_refs": _declared_output_refs(task),
        "output_json_ref": str(getattr(task, "output_json", "") or ""),
        "runner_result_ref": str(getattr(task, "runner_result_json", "") or ""),
        "result_file_ref": str(getattr(task, "runner_result_file", "") or ""),
        "final_report_ref": str(getattr(task, "agent_run_final_report_md", "") or ""),
        "latest_tool_progress_ref": _latest_tool_progress_ref(task),
        "canonical_state_ref": str(state.canonical_path or ""),
        "task_workspace_dir": task.task_workspace_dir,
        "agent_run_workspace_dir": task.agent_run_workspace_dir,
        "updated_at": task.updated_at,
    }


def canonical_state_path_for_task(task: SubAgentTask) -> Path | None:
    run_workspace = str(getattr(task, "agent_run_workspace_dir", "") or "").strip()
    if not run_workspace:
        return None
    return Path(run_workspace) / CANONICAL_STATE_FILENAME


def canonical_state_path_from_payload(
    payload: dict[str, Any],
    workspace: Path | None = None,
) -> Path | None:
    # 3.txt B.6：canonical_ref 由框架重算——只信 agent_run_workspace_dir（框架
    # 每次 save 由 ensure_subagent_task_workspace 重算的物化事实），不信任
    # task.json/投影自报的 canonical_state_ref 字符串：单独篡改 ref 可把状态
    # 读取/回写重定向到任意文件。域内约束以同 payload 的 task_workspace_dir
    # （同为框架物化字段）为锚：awd 恒是 task 工作区根的 work/agents/<run_id>
    # 子目录（任务工作区可经 attributes task_root 权威声明放到 manager
    # workspace 外，audit 场景），两字段互相印证，单独篡改任一字段都会被
    # 另一字段抓住。完整伪造（同时改所有字段）超出文件级防线，归 R1
    # WorkspaceBinding。
    run_workspace = str(payload.get("agent_run_workspace_dir") or "").strip()
    if not run_workspace:
        return None
    anchor = workspace or _payload_task_workspace_root(payload)
    path = _bounded_workspace_path(Path(run_workspace), anchor)
    if path is None:
        return None
    return path / CANONICAL_STATE_FILENAME


def _payload_task_workspace_root(payload: dict[str, Any]) -> Path | None:
    text = str(payload.get("task_workspace_dir") or "").strip()
    if not text:
        return None
    path = Path(text)
    if not path.is_absolute() or ".." in path.parts or len(path.parts) <= 1:
        return None
    return path


def _bounded_workspace_path(path: Path, workspace: Path | None) -> Path | None:
    """把自报工作区路径收敛到合法域；非法返回 None。

    workspace 提供（调用方显式锚点，或 payload 内 task_workspace_dir）→ 解析
    后强制位于 workspace 之内；缺失（只读投影调用方）→ 宽松模式：绝对路径 +
    无 .. 段 + 非根目录。完整伪造（同时篡改所有字段）超出文件级防线，归 R1
    binding。
    """
    if not path.is_absolute() or ".." in path.parts:
        return None
    if workspace is None:
        return path if len(path.parts) > 1 else None
    try:
        candidate = path.resolve(strict=False)
        anchor = Path(workspace).expanduser().resolve(strict=False)
        candidate.relative_to(anchor)
        return candidate
    except (OSError, ValueError):
        return None


def read_agent_state_payload(path: Path, workspace: Path | None = None) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("subagent task state must be a JSON object")
    canonical_path = canonical_state_path_from_payload(data, workspace=workspace)
    if canonical_path and canonical_path.exists() and _different_path(canonical_path, path):
        canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
        if isinstance(canonical, dict):
            return canonical
    if is_agent_state_locator(data):
        raise FileNotFoundError(f"canonical subagent state missing for locator: {canonical_path}")
    return data


def is_agent_state_locator(payload: dict[str, Any]) -> bool:
    return str(payload.get("schema_version") or "") in {
        STATE_LOCATOR_SCHEMA_VERSION,
        "owner-agent-projection.v1",
    }


def write_agent_run_state(state: AgentRunState) -> None:
    if state.canonical_path is None:
        return
    state.canonical_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_file_atomic(state.canonical_path, state.payload)


def _declared_output_refs(task: SubAgentTask) -> list[str]:
    attrs = getattr(task, "attributes", {}) or {}
    refs: list[str] = []
    if isinstance(attrs, dict):
        for key in ("output_refs", "output_files", "artifact_refs"):
            refs.extend(_string_list(attrs.get(key)))
    refs.extend(str(item) for item in getattr(task, "artifact_refs", []) or [])
    return list(dict.fromkeys(item for item in refs if item and not has_placeholder_path_segment(item)))


def _latest_tool_progress_ref(task: SubAgentTask) -> str:
    workspace = str(getattr(task, "agent_run_workspace_dir", "") or "").strip()
    return str(Path(workspace) / "progress" / "latest_tool_progress.json") if workspace else ""


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        return [text for item in value if (text := str(item or "").strip())]
    return []


def _different_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() != right.resolve()
    except OSError:
        return str(left) != str(right)
