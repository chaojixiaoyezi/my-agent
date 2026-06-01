# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

from __future__ import annotations

"""minimal agent-run workspaces inside a runtime-memory task workspace.

Human version:
Each subagent run now gets a small filesystem workspace under
`tasks/<task_id>/work/agents/<run_id>/`. The legacy work-order directory remains the
write-compatible source for existing code; this workspace is the new recovery
and takeover surface that later phases can grow independently.
"""

import json
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 AgentRunWorkspacePaths 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 AgentRunWorkspacePaths 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class AgentRunWorkspacePaths:
    """Concrete files for one task-local agent run workspace."""

    root: Path
    agent_yaml: Path
    state_json: Path
    task_md: Path
    timeline_jsonl: Path
    checkpoint_json: Path
    summary_md: Path
    final_report_md: Path
    findings_jsonl: Path
    inbox_dir: Path
    outbox_dir: Path
    artifacts_dir: Path
    compactions_dir: Path
    # LLM: compact chain files are additive recovery refs; original run files stay intact.
    compaction_ledger_jsonl: Path
    latest_compaction_summary_md: Path
    latest_compaction_metadata_json: Path
    legacy_run_ref_json: Path


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 EnsureAgentRunWorkspaceRequest 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 EnsureAgentRunWorkspaceRequest 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class EnsureAgentRunWorkspaceRequest:
    """Bundle inputs for syncing one agent-run workspace."""

    # LLM: new run workspace knobs should join this bundle instead of widening sync signatures.
    root: Path
    task: Any
    task_id: str
    now: float


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 ensure_agent_run_workspace 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 校验 ensure agent run workspace 的输入、状态或路径，提前暴露无效数据和越界条件。
def ensure_agent_run_workspace(
    request: EnsureAgentRunWorkspaceRequest | Path | None = None,
    task: Any | None = None,
    *,
    root: Path | None = None,
    task_id: str | None = None,
    now: float | None = None,
) -> AgentRunWorkspacePaths:
    """Create/update the Phase 1 agent-run workspace skeleton for a subagent task."""

    inputs = _coerce_ensure_request(request, task, root=root, task_id=task_id, now=now)
    paths = agent_run_workspace_paths(inputs.root)
    _ensure_directories(paths)
    _write_agent_yaml_if_missing(paths.agent_yaml, inputs.task, inputs.task_id, inputs.now)
    _write_json(paths.state_json, _state_payload(inputs.task, inputs.task_id, inputs.now))
    _write_markdown(paths.task_md, _task_markdown(inputs.task, inputs.task_id))
    _write_json(paths.checkpoint_json, _checkpoint_payload(inputs.task, inputs.task_id, inputs.now))
    _write_markdown(paths.summary_md, _summary_markdown(inputs.task, inputs.task_id))
    _write_final_report(paths.final_report_md, inputs.task, inputs.task_id)
    _write_findings(paths.findings_jsonl, inputs.task)
    _write_json(
        paths.legacy_run_ref_json,
        _legacy_run_ref_payload(inputs.task, inputs.task_id, inputs.now),
    )
    _append_timeline(paths.timeline_jsonl, _timeline_event(inputs.task, inputs.task_id, inputs.now))
    return paths


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _coerce_ensure_request 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 coerce ensure request 涉及的字段，让后续匹配和存储使用同一形态。
def _coerce_ensure_request(
    request: EnsureAgentRunWorkspaceRequest | Path | None,
    task: Any | None,
    *,
    root: Path | None,
    task_id: str | None,
    now: float | None,
) -> EnsureAgentRunWorkspaceRequest:
    if isinstance(request, EnsureAgentRunWorkspaceRequest):
        return request
    resolved_root = root if root is not None else request
    if resolved_root is None or task is None or task_id is None or now is None:
        raise TypeError("ensure_agent_run_workspace requires root, task, task_id, and now")
    return EnsureAgentRunWorkspaceRequest(
        root=Path(resolved_root),
        task=task,
        task_id=task_id,
        now=now,
    )


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 agent_run_workspace_paths 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 agent run workspace paths 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def agent_run_workspace_paths(root: Path) -> AgentRunWorkspacePaths:
    """Return all Phase 1 files for an agent-run workspace root."""

    return AgentRunWorkspacePaths(
        root=root,
        agent_yaml=root / "agent.yaml",
        state_json=root / "state.json",
        task_md=root / "task.md",
        timeline_jsonl=root / "timeline.jsonl",
        checkpoint_json=root / "checkpoint.json",
        summary_md=root / "summary.md",
        final_report_md=root / "final_report.md",
        findings_jsonl=root / "findings.jsonl",
        inbox_dir=root / "inbox",
        outbox_dir=root / "outbox",
        artifacts_dir=root / "artifacts",
        compactions_dir=root / "compactions",
        compaction_ledger_jsonl=root / "compactions" / "compaction_ledger.jsonl",
        latest_compaction_summary_md=root / "compactions" / "latest_summary.md",
        latest_compaction_metadata_json=root / "compactions" / "latest_metadata.json",
        legacy_run_ref_json=root / "legacy_run_ref.json",
    )


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _ensure_directories 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 校验 ensure directories 的输入、状态或路径，提前暴露无效数据和越界条件。
def _ensure_directories(paths: AgentRunWorkspacePaths) -> None:
    for directory in [
        paths.root,
        paths.inbox_dir,
        paths.outbox_dir,
        paths.artifacts_dir,
        paths.artifacts_dir / "tool_outputs",
        paths.artifacts_dir / "reports",
        paths.compactions_dir,
    ]:
        directory.mkdir(parents=True, exist_ok=True)


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _state_payload 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 state payload 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def _state_payload(task: Any, task_id: str, now: float) -> dict[str, object]:
    return {
        "version": 1,
        "task_id": task_id,
        "run_id": str(getattr(task, "id", "")),
        "parent_run_id": str(getattr(task, "parent_id", "")),
        "root_task_id": task_id,
        "depth": int(getattr(task, "depth", 0) or 0),
        "status": str(getattr(task, "status", "")),
        "verification_status": str(getattr(task, "verification_status", "")),
        "progress": float(getattr(task, "progress", 0.0) or 0.0),
        "current_step": str(getattr(task, "current_step", "")),
        "latest_summary": str(getattr(task, "latest_summary", "")),
        "blockers": list(getattr(task, "blockers", []) or []),
        "artifact_refs": list(getattr(task, "artifact_refs", []) or []),
        "evidence_refs": list(getattr(task, "evidence_refs", []) or []),
        "updated_at": now,
    }


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _checkpoint_payload 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 checkpoint payload 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def _checkpoint_payload(task: Any, task_id: str, now: float) -> dict[str, object]:
    return {
        "version": 1,
        "task_id": task_id,
        "run_id": str(getattr(task, "id", "")),
        "status": str(getattr(task, "status", "")),
        "progress": float(getattr(task, "progress", 0.0) or 0.0),
        "current_step": str(getattr(task, "current_step", "")),
        "latest_summary": str(getattr(task, "latest_summary", "")),
        "blockers": list(getattr(task, "blockers", []) or []),
        "artifact_refs": list(getattr(task, "artifact_refs", []) or []),
        "evidence_refs": list(getattr(task, "evidence_refs", []) or []),
        "legacy_checkpoint_ref": str(getattr(task, "checkpoint_json", "")),
        "legacy_status_report_ref": str(getattr(task, "status_report_json", "")),
        "updated_at": now,
    }


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _legacy_run_ref_payload 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 legacy run ref payload 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def _legacy_run_ref_payload(task: Any, task_id: str, now: float) -> dict[str, object]:
    task_dir = str(getattr(task, "task_dir", ""))
    return {
        "version": 2,
        "mode": "legacy_subagent_work_order_adapter",
        "task_id": task_id,
        "run_id": str(getattr(task, "id", "")),
        "parent_run_id": str(getattr(task, "parent_id", "")),
        "depth": int(getattr(task, "depth", 0) or 0),
        "status": str(getattr(task, "status", "")),
        "legacy_task_dir": task_dir,
        "legacy_task_json": str(Path(task_dir) / "task.json") if task_dir else "",
        "legacy_run_json": str(Path(task_dir) / "run.json") if task_dir else "",
        "agent_run_workspace_status": "phase_1_skeleton",
        "updated_at": now,
    }


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _timeline_event 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 timeline event 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _timeline_event(task: Any, task_id: str, now: float) -> dict[str, object]:
    return {
        "ts": now,
        "event": "agent_run_workspace_synced",
        "task_id": task_id,
        "run_id": str(getattr(task, "id", "")),
        "status": str(getattr(task, "status", "")),
        "summary": str(getattr(task, "latest_summary", "")),
    }


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _write_agent_yaml_if_missing 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 write agent yaml if missing 相关记录，集中处理目标路径、格式化和状态更新。
def _write_agent_yaml_if_missing(path: Path, task: Any, task_id: str, now: float) -> None:
    if path.exists():
        return
    content = (
        "version: 1\n"
        f'run_id: "{_yaml_quote(str(getattr(task, "id", "")))}"\n'
        f'root_task_id: "{_yaml_quote(task_id)}"\n'
        f'parent_run_id: "{_yaml_quote(str(getattr(task, "parent_id", "")))}"\n'
        f'agent_name: "{_yaml_quote(str(getattr(task, "agent_name", "")))}"\n'
        f'role: "{_yaml_quote(str(getattr(task, "role", "")))}"\n'
        f'owner: "{_yaml_quote(str(getattr(task, "owner", "")))}"\n'
        f"depth: {int(getattr(task, 'depth', 0) or 0)}\n"
        f"created_at: {float(getattr(task, 'created_at', 0.0) or now)}\n"
        "visibility: task-local\n"
        "memory_scope: run_workspace\n"
    )
    path.write_text(content, encoding="utf-8")


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _task_markdown 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 task markdown 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _task_markdown(task: Any, task_id: str) -> str:
    plan_lines = "\n".join(f"- {item}" for item in list(getattr(task, "plan", []) or [])) or "- 暂无"
    acceptance_checks = list(getattr(task, "acceptance_checks", []) or [])
    acceptance_lines = "\n".join(f"- [ ] {item}" for item in acceptance_checks) or "- [ ] 未设置"
    return (
        "# Task\n\n"
        f"- task_id: {task_id}\n"
        f"- run_id: {getattr(task, 'id', '')}\n"
        f"- legacy_task_dir: {getattr(task, 'task_dir', '')}\n\n"
        "## Goal\n\n"
        f"{getattr(task, 'goal', '') or '待填写'}\n\n"
        "## Plan\n\n"
        f"{plan_lines}\n\n"
        "## Acceptance\n\n"
        f"{acceptance_lines}\n"
    )


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _summary_markdown 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 summary markdown 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _summary_markdown(task: Any, task_id: str) -> str:
    latest = str(getattr(task, "latest_summary", "")) or "暂无"
    return (
        "# Summary\n\n"
        f"- task_id: {task_id}\n"
        f"- run_id: {getattr(task, 'id', '')}\n"
        f"- status: {getattr(task, 'status', '')}\n"
        f"- current_step: {getattr(task, 'current_step', '') or getattr(task, 'status', '')}\n\n"
        "## Latest\n\n"
        f"{latest}\n"
    )


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _write_final_report 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 write final report 相关记录，集中处理目标路径、格式化和状态更新。
def _write_final_report(path: Path, task: Any, task_id: str) -> None:
    result = str(getattr(task, "result", ""))
    status = str(getattr(task, "status", ""))
    if not result and path.exists():
        return
    body = result or "待完成后填写"
    _write_markdown(
        path,
        "# Final Report\n\n"
        f"- task_id: {task_id}\n"
        f"- run_id: {getattr(task, 'id', '')}\n"
        f"- status: {status}\n\n"
        "## Result\n\n"
        f"{body}\n",
    )


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _write_findings 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 write findings 相关记录，集中处理目标路径、格式化和状态更新。
def _write_findings(path: Path, task: Any) -> None:
    payloads = [_finding_payload(item) for item in list(getattr(task, "findings", []) or [])]
    lines = [json.dumps(payload, ensure_ascii=False, sort_keys=True) for payload in payloads if payload is not None]
    path.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _finding_payload 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 finding payload 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def _finding_payload(item: object) -> dict[str, object] | None:
    if is_dataclass(item):
        return asdict(item)
    return item if isinstance(item, dict) else None


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _write_json 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 write json 相关记录，集中处理目标路径、格式化和状态更新。
def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _write_markdown 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 write markdown 相关记录，集中处理目标路径、格式化和状态更新。
def _write_markdown(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _append_timeline 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 append timeline 相关记录，集中处理目标路径、格式化和状态更新。
def _append_timeline(path: Path, payload: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _yaml_quote 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 yaml quote 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _yaml_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


__all__ = [
    "AgentRunWorkspacePaths",
    "EnsureAgentRunWorkspaceRequest",
    "agent_run_workspace_paths",
    "ensure_agent_run_workspace",
]
