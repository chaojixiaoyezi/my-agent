# LLM: Subagent recovery strategy turns task-local refs into safe parent dispatch decisions.
# 模块用途: 根据 latest_continue_packet/checkpoint/summary 判断子代理失败后该续跑、接管、领导权恢复还是熔断。

from __future__ import annotations

"""Refs-first recovery strategy for failed or stalled subagent runs."""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import SubAgentTask
from .task_attribute_reader import task_int, task_list, task_role, task_status, task_text

_PACKET_SCHEMA_VERSION = "subagent_continue_packet.v1"
_RECOVERABLE_STATUSES = {"BLOCKED", "FAILED", "TIMEOUT", "CHANNEL_ERROR", "ERROR"}
_DEAD_STATUSES = {"TIMEOUT", "CHANNEL_ERROR"}
_COORDINATOR_ROLES = {"coordinator", "lead", "team_lead", "child_coordinator"}
_CLOSED_STATUSES = {"DONE", "COMPLETED", "ABANDONED", "TAKEN_OVER"}


# LLM: SubagentRecoveryStrategyRequest keeps recovery policy knobs explicit and testable.
# 类用途: 传入一个任务快照和少量恢复阈值，避免服务直接读取全局配置或散传参数。
@dataclass(frozen=True)
class SubagentRecoveryStrategyRequest:
    task: SubAgentTask
    now: float = 0.0
    packet_max_age_seconds: float = 0.0
    no_progress_attempt_limit: int = 4


# LLM: SubagentRecoveryStrategy is the stable refs-only result consumed by dispatch and tests.
# 类用途: 汇总恢复动作、packet 状态、降级 refs 和给 runner 的简短续跑指令。
@dataclass(frozen=True)
class SubagentRecoveryStrategy:
    run_id: str
    status: str
    role: str
    recommended_action: str
    packet_status: str
    packet_ref: str = ""
    uses_continue_packet: bool = False
    memory_scope: str = "task_local"
    fallback_refs: list[str] = field(default_factory=list)
    takeover_refs: list[str] = field(default_factory=list)
    child_run_ids: list[str] = field(default_factory=list)
    leadership_recovery: bool = False
    no_progress_fuse: bool = False
    blocked_by: list[str] = field(default_factory=list)
    runner_instruction: str = ""

    # LLM: to_dict exposes a compact JSON shape without leaking full packet bodies.
    # 函数用途: 给 orchestration payload/CLI 返回机器可读摘要，保持 refs-only。
    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "role": self.role,
            "recommended_action": self.recommended_action,
            "packet_status": self.packet_status,
            "packet_ref": self.packet_ref,
            "uses_continue_packet": self.uses_continue_packet,
            "memory_scope": self.memory_scope,
            "fallback_refs": list(self.fallback_refs),
            "takeover_refs": list(self.takeover_refs),
            "child_run_ids": list(self.child_run_ids),
            "leadership_recovery": self.leadership_recovery,
            "no_progress_fuse": self.no_progress_fuse,
            "blocked_by": list(self.blocked_by),
            "runner_instruction": self.runner_instruction,
        }


# LLM: _PacketState keeps packet validation details private to this service.
# 类用途: 保存 packet 是否可用、为什么不可用，以及读取到的小 payload。
@dataclass(frozen=True)
class _PacketState:
    status: str
    ref: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    blocked_by: list[str] = field(default_factory=list)


# LLM: build_subagent_recovery_strategy is the single decision entry for parent recovery dispatch.
# 函数用途: 根据任务状态和恢复文件，返回续跑原 run、创建接管 run、leader recovery 或熔断建议。
def build_subagent_recovery_strategy(request: SubagentRecoveryStrategyRequest) -> SubagentRecoveryStrategy:
    task = request.task
    packet = _read_packet_state(request)
    fallback_refs = _fallback_refs(task)
    no_progress_fuse = _no_progress_fuse(task, request.no_progress_attempt_limit)
    action = _recommended_action(task, packet, fallback_refs, no_progress_fuse)
    return SubagentRecoveryStrategy(
        run_id=task_text(task, "id"),
        status=task_status(task),
        role=task_role(task),
        recommended_action=action,
        packet_status=packet.status,
        packet_ref=packet.ref,
        uses_continue_packet=packet.status == "ready" and _action_uses_packet(action),
        fallback_refs=fallback_refs,
        takeover_refs=_takeover_refs(task),
        child_run_ids=task_list(task, "child_ids"),
        leadership_recovery=action == "recover_coordinator_leadership",
        no_progress_fuse=no_progress_fuse,
        blocked_by=packet.blocked_by,
        runner_instruction=_runner_instruction(task, packet, fallback_refs, action),
    )


# LLM: _read_packet_state validates the latest packet without trusting artifact bodies.
# 函数用途: 读取 latest_continue_packet.json，并判断 missing/corrupt/stale/schema/owner 等状态。
def _read_packet_state(request: SubagentRecoveryStrategyRequest) -> _PacketState:
    ref = _packet_ref(request.task)
    if not ref:
        return _PacketState(status="missing")
    path = Path(ref)
    if not path.exists():
        return _PacketState(status="missing", ref=str(path))
    if _packet_is_stale(path, request):
        return _PacketState(status="stale", ref=str(path), blocked_by=["packet_stale"])
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return _PacketState(status="corrupt", ref=str(path), blocked_by=["packet_corrupt"])
    return _validated_packet_state(request.task, path, payload)


# LLM: _validated_packet_state enforces task-local ownership before dispatch trusts a packet.
# 函数用途: 校验 packet schema、run_id、owner、memory_scope 和写主 memory 边界。
def _validated_packet_state(task: SubAgentTask, path: Path, payload: object) -> _PacketState:
    if not isinstance(payload, dict):
        return _PacketState(status="corrupt", ref=str(path), blocked_by=["packet_not_object"])
    blockers = _packet_blockers(task, payload)
    if blockers:
        return _PacketState(status="invalid", ref=str(path), payload=payload, blocked_by=blockers)
    if not bool(payload.get("ready_to_continue", False)):
        return _PacketState(status="closed", ref=str(path), payload=payload)
    return _PacketState(status="ready", ref=str(path), payload=payload)


# LLM: _packet_blockers lists every trust failure as a stable machine-readable string.
# 函数用途: 判断 packet 是否属于当前 run、是否 task-local、是否不会污染主代理 memory。
def _packet_blockers(task: SubAgentTask, payload: dict[str, Any]) -> list[str]:
    owner = payload.get("owner") if isinstance(payload.get("owner"), dict) else {}
    blockers: list[str] = []
    if payload.get("schema_version") != _PACKET_SCHEMA_VERSION:
        blockers.append("schema_mismatch")
    task_id = task_text(task, "id")
    if str(payload.get("run_id") or "") != task_id:
        blockers.append("run_id_mismatch")
    if str(owner.get("owner_id") or "") != task_id:
        blockers.append("owner_mismatch")
    if payload.get("memory_scope") != "task_local":
        blockers.append("memory_scope_not_task_local")
    if bool(payload.get("writes_main_memory", True)):
        blockers.append("writes_main_memory")
    return blockers


# LLM: _recommended_action keeps recovery decisions conservative and non-recursive.
# 函数用途: 用固定优先级选择熔断、leader recovery、接管、续跑或人工检查。
def _recommended_action(
    task: SubAgentTask,
    packet: _PacketState,
    fallback_refs: list[str],
    no_progress_fuse: bool,
) -> str:
    if no_progress_fuse:
        return "stop_no_progress_and_escalate"
    if _needs_leadership_recovery(task):
        return "recover_coordinator_leadership"
    if _needs_takeover(task):
        return _takeover_action(packet)
    if packet.status == "ready" and _is_recoverable(task):
        return "rerun_original_from_continue_packet"
    if fallback_refs and _is_recoverable(task):
        return "rerun_original_from_checkpoint"
    if _is_closed(task):
        return "closed_no_action"
    return "manual_review_missing_recovery_refs"


# LLM: _runner_instruction converts the action into a compact parent-facing handoff.
# 函数用途: 给 dispatch_subagents 的单 run 恢复场景提供简短指令，避免模型重新解释任务。
def _runner_instruction(
    task: SubAgentTask,
    packet: _PacketState,
    fallback_refs: list[str],
    action: str,
) -> str:
    if action == "stop_no_progress_and_escalate":
        return "连续恢复没有进展：不要继续自动重试，也不要继续扩容；请汇总 refs 后等待父级/用户决策。"
    if action == "recover_coordinator_leadership":
        return (
            "coordinator/lead 已失联或失败：请调用 subagents-leadership-recovery-plan 选择新 leader，"
            "再分批接管其 child_run_ids，不要重复重启失联 coordinator。"
        )
    if action.startswith("create_takeover_run"):
        return _takeover_instruction(task, packet, fallback_refs)
    if packet.status == "ready":
        return _packet_instruction(task, packet)
    if fallback_refs:
        return _fallback_instruction(task, fallback_refs)
    return "缺少可用恢复 refs：请先生成 checkpoint/summary/continue packet，再继续。"


# LLM: _packet_instruction tells the runner exactly which compact packet to read first.
# 函数用途: 正常恢复时强调先读 latest_continue_packet，避免重新从用户目标开始规划。
def _packet_instruction(task: SubAgentTask, packet: _PacketState) -> str:
    return (
        f"恢复 run {task.id}：先读取 task-local latest_continue_packet.json：{packet.ref}，"
        "再按 packet.recommended_read_paths 读取最少必要 refs。不要重新从用户目标开始规划，"
        "不要读取主代理 SOUL/USER/memory，只接着 current_step/next_action 执行。"
    )


# LLM: _fallback_instruction keeps damaged-packet recovery useful without silently restarting.
# 函数用途: packet 不可用时，明确按 checkpoint/summary 降级接续。
def _fallback_instruction(task: SubAgentTask, fallback_refs: list[str]) -> str:
    refs = ", ".join(fallback_refs[:4])
    return (
        f"恢复 run {task_text(task, 'id')}：latest_continue_packet 不可用，改读 checkpoint/summary fallback refs：{refs}。"
        "只根据这些 task-local refs 接续，不要重读主代理长期记忆。"
    )


# LLM: _takeover_instruction keeps takeover bound to the same task directory and artifacts.
# 函数用途: 原 runner 挂死时，提醒新 run 接管同一任务目录和 artifacts，而不是新开无关任务。
def _takeover_instruction(task: SubAgentTask, packet: _PacketState, fallback_refs: list[str]) -> str:
    source = packet.ref if packet.status == "ready" else ", ".join(fallback_refs[:3])
    return (
        f"原 run {task_text(task, 'id')} 看起来已挂死：创建 takeover run 接管同一个任务目录 {task_text(task, 'task_dir')} "
        f"和同一批 artifacts refs。恢复入口：{source}。不要重写健康分支。"
    )


# LLM: _packet_ref derives the canonical latest packet path from the run workspace.
# 函数用途: 只从 agent_run_compactions_dir 推导 latest_continue_packet.json，保持路径规则唯一。
def _packet_ref(task: SubAgentTask) -> str:
    compactions_dir = task_text(task, "agent_run_compactions_dir")
    if not compactions_dir:
        return ""
    return str(Path(compactions_dir) / "latest_continue_packet.json")


# LLM: _packet_is_stale compares file mtime only when the caller enables a max age.
# 函数用途: 让测试和未来自动恢复可以判断过期 packet，默认不误伤手动恢复。
def _packet_is_stale(path: Path, request: SubagentRecoveryStrategyRequest) -> bool:
    if request.packet_max_age_seconds <= 0:
        return False
    now = request.now or time.time()
    try:
        return path.stat().st_mtime < now - request.packet_max_age_seconds
    except OSError:
        return False


# LLM: _fallback_refs orders small task-local recovery files before heavier reports.
# 函数用途: packet 不可用时提供 checkpoint/summary/task/failure handoff 等最小读取顺序。
def _fallback_refs(task: SubAgentTask) -> list[str]:
    values = [
        task_text(task, "agent_run_checkpoint_json"),
        task_text(task, "agent_run_summary_md"),
        task_text(task, "agent_run_task_md"),
        task_text(task, "failure_handoff_json"),
        task_text(task, "takeover_readiness_json"),
        task_text(task, "output_json"),
        task_text(task, "runner_result_json"),
    ]
    return _existing_refs(values)


# LLM: _takeover_refs lists shared directories a takeover run must keep using.
# 函数用途: 告诉新接管者继续使用同一个 task_dir/artifacts/shared workspace，而不是另起炉灶。
def _takeover_refs(task: SubAgentTask) -> list[str]:
    return _existing_refs(
        [
            task_text(task, "task_dir"),
            task_text(task, "agent_run_workspace_dir"),
            task_text(task, "agent_run_artifacts_dir"),
            task_text(task, "task_workspace_artifacts_dir"),
            task_text(task, "task_workspace_shared_dir"),
        ]
    )


# LLM: _existing_refs filters empty/missing paths while preserving order.
# 函数用途: 统一清理恢复 refs，避免工具响应里塞不存在的路径。
def _existing_refs(values: list[str]) -> list[str]:
    refs: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and Path(text).exists() and text not in refs:
            refs.append(text)
    return refs


# LLM: _needs_leadership_recovery detects failed coordinators with live child refs.
# 函数用途: coordinator/lead 带着下级失败时，优先让新 leader 接管孩子，而不是无限重试旧 leader。
def _needs_leadership_recovery(task: SubAgentTask) -> bool:
    role = task_role(task).lower()
    return bool(task_list(task, "child_ids")) and (role in _COORDINATOR_ROLES or "coordinator" in role) and _is_dead(task)


# LLM: _needs_takeover detects dead worker-style runs that should be replaced.
# 函数用途: 超时、断通道或 runner_timeout 时建议接管 run，而不是重复唤醒挂死 run。
def _needs_takeover(task: SubAgentTask) -> bool:
    return _is_dead(task) and not _needs_leadership_recovery(task)


# LLM: _is_dead classifies statuses and failure types that imply the old process is gone.
# 函数用途: 将 TIMEOUT/CHANNEL_ERROR/runner_timeout 等统一成需要接管的状态。
def _is_dead(task: SubAgentTask) -> bool:
    status = task_status(task)
    failure_type = task_text(task, "failure_type").lower()
    return status in _DEAD_STATUSES or failure_type in {"runner_timeout", "channel_error", "runner_channel_failed"}


# LLM: _is_recoverable keeps retry logic scoped to known incomplete states.
# 函数用途: 判断任务是否还应该继续推进，避免已关闭任务被重新 dispatch。
def _is_recoverable(task: SubAgentTask) -> bool:
    return task_status(task) in _RECOVERABLE_STATUSES


# LLM: _is_closed recognizes terminal states that need no automatic recovery.
# 函数用途: 已完成/放弃/被接管的任务不再建议恢复。
def _is_closed(task: SubAgentTask) -> bool:
    return task_status(task) in _CLOSED_STATUSES


# LLM: _no_progress_fuse converts repeated attempts into an explicit stop signal.
# 函数用途: runner_attempts 超过阈值时熔断，防止无限重试和无限扩容。
def _no_progress_fuse(task: SubAgentTask, attempt_limit: int) -> bool:
    if attempt_limit <= 0:
        return False
    return task_int(task, "runner_attempts") >= attempt_limit and _is_recoverable(task)


# LLM: _takeover_action preserves whether the packet or fallback refs are the source of truth.
# 函数用途: 生成接管动作名，让测试和日志看出接管使用哪个恢复入口。
def _takeover_action(packet: _PacketState) -> str:
    if packet.status == "ready":
        return "create_takeover_run_from_continue_packet"
    return "create_takeover_run_from_checkpoint"


# LLM: _action_uses_packet centralizes action names that actually rely on packet contents.
# 函数用途: 避免 leader recovery 或熔断场景误报 uses_continue_packet。
def _action_uses_packet(action: str) -> bool:
    return action in {"rerun_original_from_continue_packet", "create_takeover_run_from_continue_packet"}


# LLM: _string_list normalizes child ids without mutating task fields.
# 函数用途: 清理 child_run_ids，保持结果可 JSON 化。
def _string_list(values: list[str]) -> list[str]:
    return [str(item) for item in values if str(item or "").strip()]


__all__ = [
    "SubagentRecoveryStrategy",
    "SubagentRecoveryStrategyRequest",
    "build_subagent_recovery_strategy",
]
