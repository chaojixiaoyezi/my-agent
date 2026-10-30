# LLM: 孤儿监督/选择/runner属于原agent_core业务；只消费真实root注入的Agent与独立原请求，平台门仍核原P3/P5/T，不新建run/attempt。
# 模块用途: 同步消费canonical child工作，等待原runner与设施实际收口，不复制终态后的执行范围。
from __future__ import annotations

from ....gateway_parts.multiprocess_background_execution import background_short_operation
from ....gateway_parts.multiprocess_subagent_execution import (
    _require_original_child_ref,
    capture_child_transport,
)


# LLM: supervisor沿原死亡证明/RuntimeDB欠账恢复，MP auto-start仅留canonical任务；只选ref这一run并同步原worker，网络在T外。
# 函数用途: 认领一个原孤儿工作片，原请求持续processing直到实际runner结果或typed未知收口。
def execute_orphan_source(agent, ref):
    from ...runner.dispatch import _runner_retry_reason, runner_failure_retry_limit
    from ...runner.timeout_policy import get_task_timeout
    from ...runner.worker import RunSubagentWorkerParams, _run_subagent_worker
    from .capability_auto_sweep import (
        _is_stalled_dispatchable_orphan,
        _runtime_authority_recovery_block,
        supervise_stalled_orphans,
    )
    from .conversation_lifecycle_gate import (
        conversation_lifecycle_decisions,
    )

    task = agent.subagents.load(ref["source_id"])
    transport = capture_child_transport(agent, task)
    background_short_operation("orphan_original_source", lambda: _require_original_child_ref(transport, agent))
    summary = supervise_stalled_orphans(agent)
    task = agent.subagents.load(ref["source_id"])
    decision = conversation_lifecycle_decisions(agent, [task]).get(task.id)
    result = {"source_kind": "orphan", "source_id": ref["source_id"], "supervision": summary, "outcome": "handled"}
    block = _runtime_authority_recovery_block(agent.subagents, task)
    if block is not None:
        return {**result, "outcome": "pending", "recovery_block": block}
    if not _is_stalled_dispatchable_orphan(task, decision):
        from dataclasses import asdict

        from ....subagents.canonical_source import orphan_has_work

        return {**result, "outcome": "pending" if orphan_has_work(asdict(task)) else "handled"}
    runner = _run_subagent_worker(RunSubagentWorkerParams(agent.config, agent.root, task.id, "", False, 0, True,
        _runner_retry_reason(task, runner_failure_retry_limit(agent.config), runtime_policy=agent.runtime_guard_policy),
        timeout_seconds=get_task_timeout(task, 0, agent.config), local_store=agent.local_store,
        backend_override=getattr(agent, "_subagent_worker_backend_override", None), request_transport=transport))
    return {**result, "run_id": runner.run_id, "status": runner.status, "ok": runner.ok}
