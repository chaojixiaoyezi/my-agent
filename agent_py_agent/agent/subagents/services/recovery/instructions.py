from __future__ import annotations

from typing import Any

from ..task_attribute_reader import task_text
from .modes import LEADERSHIP_RECOVERY, NO_PROGRESS_LIMIT_REACHED, is_takeover_mode


def runner_instruction(task: Any, packet: Any, recovery_refs: list[str], recovery_mode: str) -> str:
    if recovery_mode == NO_PROGRESS_LIMIT_REACHED:
        return "连续恢复没有进展：不要继续自动重试，也不要继续扩容；请汇总 refs 后等待父级/用户决策。"
    if recovery_mode == LEADERSHIP_RECOVERY:
        return (
            "coordinator/lead 已失联或失败：请调用 subagents-leadership-recovery-plan 选择新 leader，"
            "再分批接管其 child_run_ids，不要重复重启失联 coordinator。"
        )
    if is_takeover_mode(recovery_mode):
        return _takeover_instruction(task, packet, recovery_refs)
    if getattr(packet, "status", "") == "ready":
        return _packet_instruction(task, packet)
    if recovery_refs:
        return _recovery_refs_instruction(task, packet, recovery_refs)
    return "缺少可用恢复 refs：请先生成 checkpoint/summary/continue packet，再继续。"


def _packet_instruction(task: Any, packet: Any) -> str:
    return (
        f"恢复 run {task.id}：先读取 task-local latest_continue_packet.json：{packet.ref}，"
        "再按 packet.recommended_read_paths 读取最少必要 refs。不要重新从用户目标开始规划，"
        "不要读取主代理 SOUL/USER/memory，只接着 current_step/next_action 执行。"
    )


def _recovery_refs_instruction(task: Any, packet: Any, recovery_refs: list[str]) -> str:
    refs = ", ".join(recovery_refs[:4])
    prefix = _packet_load_prefix(packet)
    return (
        f"恢复 run {task_text(task, 'id')}：{prefix}latest_continue_packet 不可用，改读 checkpoint/summary recovery_refs：{refs}。"
        "只根据这些 task-local refs 接续，不要重读主代理长期记忆。"
    )


def _packet_load_prefix(packet: Any) -> str:
    error = getattr(packet, "load_error", None)
    if not isinstance(error, dict) or not error:
        return ""
    category = str(error.get("category") or "").strip()
    context = str(error.get("context") or "").strip()
    path = str(error.get("path") or getattr(packet, "ref", "") or "").strip()
    parts = ["latest_continue_packet 读取失败"]
    if category:
        parts.append(f"category={category}")
    if context:
        parts.append(f"context={context}")
    if path:
        parts.append(f"path={path}")
    return "；".join(parts) + "。"


def _takeover_instruction(task: Any, packet: Any, recovery_refs: list[str]) -> str:
    source = packet.ref if getattr(packet, "status", "") == "ready" else ", ".join(recovery_refs[:3])
    return (
        f"原 run {task_text(task, 'id')} 看起来已挂死：创建 takeover run 接管同一个任务目录 {task_text(task, 'task_dir')} "
        f"和同一批 artifacts refs。恢复入口：{source}。不要重写健康分支。"
    )


__all__ = ["runner_instruction"]
