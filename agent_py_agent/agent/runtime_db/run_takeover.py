# LLM: 子代理被接替后的运行账收口。未关闭来源（BLOCKED、暂停等）转 TAKEN_OVER 之后再也不会续跑，但它在 runtime.db 的
#   agent_run 还停在 created（settle_agent_attempt 只结束执行片、有意保留可续跑）；这里用 settle_agent_run 的精确 CAS 把它收成
#   cancelled——与 runner_result_admission._runner_runtime_terminal_status 的 TAKEN_OVER→cancelled 同一口径。
#   只在当前 attempt 已静止（repository._QUIESCENT_ATTEMPT_STATUSES：终态集去掉 unknown）时写；仍在运行、排队或结果未知的
#   attempt 不动（停下活的执行轮归取消入口，UNKNOWN 必须等显式恢复）。已终态、没有权威行都是无操作。
#   调用方是接替落账之后的宿主（services/base.record_takeover），失败不影响已落盘的接替关系。
#   改动同步 test_subagent_takeover_runtime_closeout.py 与 docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md 的 G03 观察。
# 模块用途: 让被接替的子代理在运行账里也有终态，而不是永远停在 created。
from __future__ import annotations

import logging

from .operations import AGENT_RUN_TERMINAL_STATUSES
from .repository import _QUIESCENT_ATTEMPT_STATUSES

_LOGGER = logging.getLogger(__name__)
# 运行账事件里标明这次收口来自接替（结构化来源，不从正文推断）。
TAKEOVER_RUNTIME_SOURCE = "subagent_takeover"


# LLM: 只读权威行后用 expected_attempt_status 把“当前 attempt 仍是刚读到的静止状态”放进同一写事务核对，读写之间 attempt 变了
#   就返回 settle 的拒绝原因而不是覆盖。payload 只带结构化字段。副作用：成功时写 agent_runs.status=cancelled 与 agent_run.completed 事件。
# 函数用途: 把一个已被接替、执行轮已结束的子代理运行收口为 cancelled。
def settle_taken_over_run(repository: object, run_id: str, *, takeover_by: str) -> dict[str, object]:
    authority = repository.agent_run_for_run_id(run_id)
    if authority is None:
        return {"settled": False, "reason": "no_runtime_authority"}
    run_status = str(authority["status"] or "")
    if run_status in AGENT_RUN_TERMINAL_STATUSES:
        return {"settled": False, "reason": "already_terminal", "run_status": run_status}
    attempt_id = str(authority["current_attempt_id"] or "")
    attempt = repository.get_attempt(attempt_id) if attempt_id else None
    attempt_status = str(attempt["status"] or "") if attempt is not None else ""
    if attempt_status not in _QUIESCENT_ATTEMPT_STATUSES:
        return {"settled": False, "reason": "source_attempt_active", "attempt_status": attempt_status}
    result = repository.settle_agent_run(
        agent_run_id=str(authority["agent_run_id"] or ""),
        status="cancelled",
        attempt_id=attempt_id,
        expected_attempt_status=attempt_status,
        payload={"status": "cancelled", "runtime_status": "cancelled", "runtime_reason": "taken_over",
                 "runtime_source": TAKEOVER_RUNTIME_SOURCE, "run_id": run_id, "takeover_by": takeover_by},
    )
    return {"settled": bool(result.get("settled")), "reason": str(result.get("reason") or "settled"),
            "attempt_id": attempt_id}


# LLM: 接替已落盘后的补账：没有权威库（本地非托管）直接跳过；任何异常只记日志并返回 write_error，绝不让已成功的接替变失败。
# 函数用途: 接替落账之后顺手把来源运行收口，失败只留诊断。
def settle_taken_over_run_best_effort(repository: object | None, run_id: str, *, takeover_by: str) -> dict[str, object]:
    if repository is None:
        return {"settled": False, "reason": "no_runtime_db"}
    try:
        return settle_taken_over_run(repository, run_id, takeover_by=takeover_by)
    except Exception as exc:  # noqa: BLE001 - 补账失败不能反过来否定已落盘的接替
        _LOGGER.warning("taken-over run %s runtime closeout failed: %s", run_id, type(exc).__name__)
        return {"settled": False, "reason": "write_error", "error_type": type(exc).__name__}


__all__ = ["TAKEOVER_RUNTIME_SOURCE", "settle_taken_over_run", "settle_taken_over_run_best_effort"]
