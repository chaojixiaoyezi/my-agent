# LLM: worker 真正退出当前工作片后，使用原 RuntimeDB settle_agent_attempt 释放 exact 执行权；不结束逻辑任务、不改审批或能力申请。
# 模块用途: 防止 BLOCKED/PENDING 的已退出子代理留下 running attempt，使权限批准后还要等孤儿超时才能续跑。
from __future__ import annotations

import logging
import sqlite3

_LOGGER = logging.getLogger(__name__)


# LLM: 仅宿主 worker 在 session lease 退出后调用；未知/未结工具副作用、换代和缺身份都不能放开新执行，复用原 DB CAS。
# 函数用途: 结束一个等待外部输入的执行片，不将整个 AgentRun 标成成功或失败。
def settle_resumable_worker_slice(worker, run_id: str, result, *, attempt_id: str, dry_run: bool) -> None:
    if dry_run or not attempt_id or str(result.status) not in {'PENDING', 'BLOCKED'}:
        return
    repo = getattr(worker.subagents, 'runtime_db', None)
    if repo is None:
        return
    try:
        task = worker.subagents.load(run_id)
        session = (task.attributes or {}).get('runner_session') or {}
        if session.get('attempt_id') != attempt_id or session.get('status') not in {'completed', 'failed'}:
            return
        authority = repo.agent_run_for_run_id(run_id)
        if authority is None or str(authority['current_attempt_id'] or '') != attempt_id:
            return
        if repo.unsettled_attempt_operations(attempt_id):
            return
        repo.settle_agent_attempt(agent_run_id=str(authority['agent_run_id']), attempt_id=attempt_id,
            payload={'runtime_status': 'unfinished', 'runtime_source': 'subagent_worker_slice_exit',
                     'runner_status': str(result.status)})
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        # 原周期恢复仍可补账；错误不能变成完成或越过未结副作用。
        _LOGGER.warning('runner slice closeout failed run_id=%s attempt_id=%s', run_id, attempt_id, exc_info=True)
