# LLM: Takeover run service creates one replacement runner without losing source task refs.
# 模块用途: 原子化创建接管 run、记录旧 run 被接管，并防止同一失败任务无限扩容。

from __future__ import annotations

"""Create idempotent takeover runs for dead subagent tasks."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import SubAgentTask


# LLM: TakeoverRunRequest bundles source run and creation overrides for future tool/CLI use.
# 类用途: 描述要接管哪个 run、为什么接管，以及可选的新 agent 名/角色/计划。
@dataclass(frozen=True)
class TakeoverRunRequest:
    source_run_id: str
    reason: str
    agent_name: str = ""
    role: str = ""
    plan: list[str] = field(default_factory=list)


# LLM: TakeoverRunResult is a compact audit result for tests, CLI, and parent dispatch payloads.
# 类用途: 返回旧 run、新接管 run、是否新建、接管 refs 和消息，不展开 artifacts 正文。
@dataclass(frozen=True)
class TakeoverRunResult:
    source_run_id: str
    takeover_run_id: str
    created: bool
    applied: bool
    message: str
    takeover_refs: dict[str, str] = field(default_factory=dict)

    # LLM: to_dict preserves a JSON-friendly response shape for future orchestration tools.
    # 函数用途: 把接管结果转成稳定字典，避免调用方依赖 dataclass 内部结构。
    def to_dict(self) -> dict[str, object]:
        return {
            "source_run_id": self.source_run_id,
            "takeover_run_id": self.takeover_run_id,
            "created": self.created,
            "applied": self.applied,
            "message": self.message,
            "takeover_refs": dict(self.takeover_refs),
        }


# LLM: SubAgentTakeoverRunService owns replacement creation and source takeover marking.
# 类用途: 集中处理接管 run 创建、refs 继承、幂等检查和旧 run 状态记录。
class SubAgentTakeoverRunService:
    """Create one takeover run for a source task, or return the existing one."""

    # LLM: __init__ stores the manager facade used for persistence and hierarchy links.
    # 函数用途: 初始化接管服务依赖；本身不读写任务。
    def __init__(self, manager: Any) -> None:
        self.manager = manager

    # LLM: create creates a same-scope takeover run and then records takeover on the source.
    # 函数用途: 幂等地创建接管 run；如果旧 run 已有 takeover_by，则复用已有 run。
    def create(self, request: TakeoverRunRequest) -> TakeoverRunResult:
        source = self.manager.load(request.source_run_id)
        existing = _existing_takeover(self.manager, source)
        if existing:
            return _existing_result(source, existing)
        takeover = _create_takeover_task(self.manager, source, request)
        self.manager.record_takeover(source.id, take_over_by=takeover.id, reason=request.reason, locked_files=[])
        return TakeoverRunResult(
            source_run_id=source.id,
            takeover_run_id=takeover.id,
            created=True,
            applied=True,
            message=f"created takeover run {takeover.id} for {source.id}",
            takeover_refs=_source_refs(source),
        )


# LLM: _existing_takeover prevents repeated recovery from spawning endless replacements.
# 函数用途: 如果 source.takeover_by 指向仍存在的 run，直接复用它。
def _existing_takeover(manager: Any, source: SubAgentTask) -> SubAgentTask | None:
    takeover_id = str(source.takeover_by or "").strip()
    if not takeover_id:
        return None
    try:
        return manager.load(takeover_id)
    except FileNotFoundError:
        return None


# LLM: _existing_result reports idempotent reuse with the original source refs.
# 函数用途: 生成“已存在接管 run”的稳定返回，不改动任何任务状态。
def _existing_result(source: SubAgentTask, existing: SubAgentTask) -> TakeoverRunResult:
    return TakeoverRunResult(
        source_run_id=source.id,
        takeover_run_id=existing.id,
        created=False,
        applied=False,
        message=f"source {source.id} already taken over by {existing.id}",
        takeover_refs=_source_refs(source),
    )


# LLM: _create_takeover_task creates a sibling replacement with source refs in attributes.
# 函数用途: 新建接管 run，并把旧任务目录/artifacts/checkpoint/packet 作为可写或可读引用带过去。
def _create_takeover_task(manager: Any, source: SubAgentTask, request: TakeoverRunRequest) -> SubAgentTask:
    takeover = manager.create_run(
        goal=f"接管 run {source.id}: {source.goal}",
        thought="旧 runner 已超时或断通道；从 task-local refs 接续，不重新理解任务。",
        plan=request.plan or _default_takeover_plan(),
        agent_name=request.agent_name or _takeover_agent_name(source),
        role=request.role or source.role or "worker",
        parent_id=source.parent_id,
        root_id=source.root_id or source.id,
        depth=source.depth,
        allowed_skills=list(source.allowed_skills),
        allowed_tools=list(source.allowed_tools),
        acceptance_checks=list(source.acceptance_checks),
        extra_write_roots=_takeover_write_roots(source),
        workflow_mode="off",
    )
    takeover.attributes["takeover_source_run_id"] = source.id
    takeover.attributes["takeover_source_refs"] = _source_refs(source)
    takeover.current_step = "读取 takeover_source_refs.latest_continue_packet 或 checkpoint 后接续原任务。"
    takeover.latest_summary = source.latest_summary
    takeover.artifact_refs = _unique_strings([*source.artifact_refs, source.agent_run_artifacts_dir])
    takeover.evidence_refs = list(source.evidence_refs)
    manager.save(takeover)
    return takeover


# LLM: _default_takeover_plan keeps takeover behavior concrete but role-agnostic.
# 函数用途: 新接管 run 默认先读 refs，再继续实现/验证，最后写结果和证据。
def _default_takeover_plan() -> list[str]:
    return [
        "读取 takeover_source_refs.latest_continue_packet；不可用则读 checkpoint/summary",
        "继续原 run 未完成的 current_step/next_action",
        "复用原任务目录和 artifacts refs 写入结果与证据",
        "完成后写 final_report/output 并等待父级验收",
    ]


# LLM: _takeover_agent_name makes replacement identity visible without depending on user naming.
# 函数用途: 生成可读的接管 agent_name，方便看板和日志区分旧 run 与新 run。
def _takeover_agent_name(source: SubAgentTask) -> str:
    base = str(source.agent_name or source.id or "worker").strip()
    return f"{base}-takeover"


# LLM: _source_refs records the exact task-local recovery files and shared artifact roots.
# 函数用途: 给新 run 和测试提供同一任务目录、artifacts、checkpoint、packet 的固定引用。
def _source_refs(source: SubAgentTask) -> dict[str, str]:
    refs = {
        "task_dir": source.task_dir,
        "agent_run_workspace_dir": source.agent_run_workspace_dir,
        "agent_run_artifacts_dir": source.agent_run_artifacts_dir,
        "task_workspace_artifacts_dir": source.task_workspace_artifacts_dir,
        "latest_continue_packet": _latest_continue_packet_ref(source),
        "checkpoint": source.agent_run_checkpoint_json,
        "summary": source.agent_run_summary_md,
        "output_json": source.output_json,
        "runner_result": source.runner_result_json,
        "takeover_readiness": source.takeover_readiness_json,
    }
    return {key: str(value) for key, value in refs.items() if str(value or "").strip()}


# LLM: _latest_continue_packet_ref keeps the packet path derivation identical to recovery strategy.
# 函数用途: 从 source.agent_run_compactions_dir 推导 latest_continue_packet.json。
def _latest_continue_packet_ref(source: SubAgentTask) -> str:
    if not source.agent_run_compactions_dir:
        return ""
    return str(Path(source.agent_run_compactions_dir) / "latest_continue_packet.json")


# LLM: _takeover_write_roots grants the replacement access to source-owned task/artifact directories.
# 函数用途: 新 run 可在同一任务产物区继续写，但不扩大到主代理或其他分支目录。
def _takeover_write_roots(source: SubAgentTask) -> list[str]:
    return _existing_dirs(
        [
            source.task_dir,
            source.agent_run_artifacts_dir,
            source.task_workspace_artifacts_dir,
            source.task_workspace_shared_dir,
        ]
    )


# LLM: _existing_dirs filters write roots down to real directories.
# 函数用途: 避免把空字符串或文件路径当成接管写入根。
def _existing_dirs(values: list[str]) -> list[str]:
    dirs: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and Path(text).is_dir() and text not in dirs:
            dirs.append(text)
    return dirs


# LLM: _unique_strings deduplicates refs while preserving source order.
# 函数用途: 清理 artifact/evidence refs，避免重复路径增加上下文噪音。
def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(str(item) for item in values if str(item or "").strip()))


__all__ = [
    "SubAgentTakeoverRunService",
    "TakeoverRunRequest",
    "TakeoverRunResult",
]
