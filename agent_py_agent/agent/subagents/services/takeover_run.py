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
        chain_limit = _takeover_chain_limit(self.manager)
        if chain_limit > 0 and _takeover_chain_depth(source) >= chain_limit:
            return _chain_exhausted_result(self.manager, source, chain_limit)
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
        return _existing_takeover_by_source_ref(manager, source.id)
    try:
        return manager.load(takeover_id)
    except FileNotFoundError:
        return _existing_takeover_by_source_ref(manager, source.id)


# LLM: source-ref scanning makes takeover creation idempotent even if a stale source snapshot lost takeover_by.
# 函数用途: 根据 takeover task attributes 反查已有接管 run，防止旧 runner/旧父级快照导致重复创建接管分支。
def _existing_takeover_by_source_ref(manager: Any, source_run_id: str) -> SubAgentTask | None:
    try:
        tasks = manager.list_runs()
    except Exception:
        return None
    source_id = str(source_run_id or "").strip()
    candidates: list[SubAgentTask] = []
    for task in tasks:
        attrs = getattr(task, "attributes", {}) or {}
        if str(attrs.get("takeover_source_run_id") or "").strip() != source_id:
            continue
        if str(getattr(task, "status", "") or "").upper() in {"ABANDONED", "TAKEN_OVER"}:
            continue
        candidates.append(task)
    candidates.sort(key=lambda item: item.created_at or item.updated_at or 0.0)
    return candidates[0] if candidates else None


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


# LLM: _chain_exhausted_result turns repeated takeover timeouts into a visible blocker instead of more children.
# 函数用途: 同一任务连续接管超过上限时停止扩容，标记当前 run 为 BLOCKED 并保留 refs 给父级决策。
def _chain_exhausted_result(manager: Any, source: SubAgentTask, chain_limit: int) -> TakeoverRunResult:
    source.status = "BLOCKED"
    source.failure_type = "takeover_chain_exhausted"
    source.blockers = _unique_strings(
        [
            *source.blockers,
            f"takeover chain reached max depth {chain_limit}; escalate or adjust timeout/scope before retry",
        ]
    )
    source.current_step = "takeover chain exhausted; waiting for parent decision"
    source.result = source.result or "连续 takeover 仍无进展，已停止继续创建接管 run。"
    manager.save(source)
    return TakeoverRunResult(
        source_run_id=source.id,
        takeover_run_id="",
        created=False,
        applied=False,
        message=f"takeover chain exhausted for {source.id}; max_depth={chain_limit}",
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
    takeover.attributes["takeover_lineage_root_run_id"] = _takeover_lineage_root(source)
    takeover.attributes["takeover_chain_depth"] = _takeover_chain_depth(source) + 1
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
    if source.agent_run_latest_session_continue_packet_json:
        return source.agent_run_latest_session_continue_packet_json
    if not source.agent_run_compactions_dir:
        return ""
    return str(Path(source.agent_run_compactions_dir) / "session" / "latest_continue_packet.json")


# LLM: _takeover_write_roots grants the replacement access to source-owned task/artifact directories.
# 函数用途: 新 run 可在同一任务产物区继续写，但不扩大到主代理或其他分支目录。
def _takeover_write_roots(source: SubAgentTask) -> list[str]:
    return _unique_strings(
        [
            *_explicit_source_write_roots(source),
            *_existing_dirs(
                [
                    source.task_dir,
                    source.agent_run_artifacts_dir,
                    source.task_workspace_artifacts_dir,
                    source.task_workspace_shared_dir,
                ]
            ),
        ]
    )


# LLM: explicit source write roots may be future product directories that write_file will create.
# 函数用途: 保留父级派工时明确给原 run 的写入目录，即使目录还不存在；避免 takeover 再次申请同一写权限。
def _explicit_source_write_roots(source: SubAgentTask) -> list[str]:
    return _unique_strings([str(item) for item in source.allowed_write_roots if str(item or "").strip()])


# LLM: _takeover_chain_limit keeps the safety fuse explicit; zero means unrestricted.
# 函数用途: 读取 manager 上的接管链深度上限；0 表示不限制，显式正数才触发接管链熔断。
def _takeover_chain_limit(manager: Any) -> int:
    try:
        return max(0, int(getattr(manager, "takeover_chain_max_depth", 0) or 0))
    except (TypeError, ValueError):
        return 0


# LLM: _takeover_chain_depth is stored on replacement runs and defaults to 0 for original runs.
# 函数用途: 获取当前 run 已经位于第几层 takeover 链，用于防止无限接管。
def _takeover_chain_depth(source: SubAgentTask) -> int:
    try:
        return max(0, int((source.attributes or {}).get("takeover_chain_depth", 0) or 0))
    except (TypeError, ValueError):
        return 0


# LLM: _takeover_lineage_root keeps later takeover runs tied to the original failed run.
# 函数用途: 返回接管链最初的 run_id，便于日志和后续 no-progress 诊断。
def _takeover_lineage_root(source: SubAgentTask) -> str:
    attrs = source.attributes or {}
    root = str(attrs.get("takeover_lineage_root_run_id") or "").strip()
    return root or source.id


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
