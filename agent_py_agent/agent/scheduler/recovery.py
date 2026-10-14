# LLM: 恢复只认原 owner 的 run/claim/PID 指纹事实；已开跑死亡落 unknown，不重放动作，不触碰 RuntimeDB 的另一张账。
# 模块用途: 在原调度事务内回收明确死亡执行，并迁移旧版误留活动账本的终态记录。
from __future__ import annotations

import logging

from ..common.json_io import read_jsonl_objects_report


# LLM: 启动复用旧来源迁移与原 scheduler 恢复，不代结 RuntimeDB attempt，也不把来源缺证据猜成当前 actor。
# 函数用途: 组合根准备旧调度账本，使服务第一次读取就有准确归属和已死运行结算。
def prepare_repository(repository, conversation_store, *, now: float) -> None:
    from .creator_migration import migrate_monitor_creators

    migrate_monitor_creators(repository, conversation_store, now=now)
    repository.recover_interrupted_executions(now=now)


# LLM: 开始前失败仍先领取原 claim 再关闭精确 TaskLink，终态只走原 repository CAS；不能代替已开跑死亡的 unknown。
# 函数用途: 结算排队过期或执行准备失败，释放原任务车道，不新建或重放工作。
def fail_unstarted(service, run: dict, error_code: str, *, now: float, status: str) -> None:
    from .execution import close_unstarted_task
    from .repository import SchedulerRunFinish

    claimed = service.repository.claim_run(str(run.get("run_id") or ""), lease_seconds=30, now=now)
    if claimed is None:
        return
    close_unstarted_task(service, claimed, now=now)
    service.repository.finish_run(str(run["run_id"]), str(claimed.get("claim_id") or ""),
        SchedulerRunFinish(status=status, error_code=error_code,
            error_message="scheduled run failed before model execution", now=now))


# LLM: 预检查只省掉无候选时的写锁；最终死亡判定、claim 和终态记录都在原事务中重读。
# 函数用途: 判断是否有过期领取或旧终态需要复核，不仅凭过期就认为执行者已经死亡。
def recovery_candidates(store: dict, current: float) -> bool:
    for raw in store["runs"].values():
        if not isinstance(raw, dict):
            continue
        if raw.get("status") == "unknown":
            return True
        if raw.get("status") not in {"claimed", "running"}:
            continue
        try:
            if float(raw.get("claim_expires_at") or 0) <= current:
                return True
        except (TypeError, ValueError):
            continue
    return False


# LLM: 仅采纳同 owner/run/job/thread/claim epoch 的合法终态，防止 history 已提交而 store rename 失败后改写真实结果。
# 函数用途: 读取本次恢复候选已落下的历史回执，保留已确认的 done/failed 等终态。
def existing_terminal(repository, run: dict) -> dict | None:
    report = read_jsonl_objects_report(repository.history_path, context="scheduler.recovery.history")
    for raw in reversed(report.records):
        terminal, _error = repository._parse_run(raw, terminal=True)
        if (terminal is not None and terminal.get("claim_epoch", 0) == run.get("claim_epoch", 0)
                and all(terminal.get(key) == run.get(key) for key in ("owner", "run_id", "job_id", "thread_id"))):
            return terminal
    return None


# LLM: 终态共用 repository 的原落账入口；保存死亡原因和原 claim 引用，不伪造业务成功或额外对外回复。
# 函数用途: 将已死的 running 或旧 unknown 记录写入历史并从活动账本移出。
def settle_dead_run(repository, store: dict, run: dict, *, current: float) -> dict:
    from .repository import SchedulerRunFinish, _terminal_run_payload

    terminal = existing_terminal(repository, run)
    if terminal is None:
        ended = float(run.get("ended_at") or current)
        legacy = run["status"] == "unknown"
        code = str(run.get("error_code") or "SCHEDULER_RUN_OUTCOME_UNKNOWN") if legacy else "SCHEDULER_RUNNER_DIED"
        message = str(run.get("error_message") or "原执行结果未知；只归档记录，不自动重做。") if legacy else "执行者已死亡，已开工操作的结果未知；不会自动重做。"
        terminal = _terminal_run_payload(run, SchedulerRunFinish(status="unknown",
            response=str(run.get("response") or ""), delivery_status=str(run.get("delivery_status") or ""),
            delivery_reason=str(run.get("delivery_reason") or ""), error_code=code,
            error_message=message, now=ended), now=ended)
        terminal["recovered_claim_id"] = str(run.get("claim_id") or "")
    repository._record_terminal_unlocked(store, run, terminal, now=float(terminal.get("ended_at") or current))
    return terminal


# LLM: expired/dead 由原 _resolve_expired_claim 决定；queued 可新领，running 只结算 unknown；无候选不写锁或配额。
# 函数用途: 启动和周期复核同一份 scheduler 账，显式 job_id 只复核本人指定监控；旧 unknown 迁移只做记录收口，不产生任务执行。
def recover_runs(repository, *, current: float, job_id: str = '', expected_version: int | None = None) -> list[str]:
    from .repository import _collect_expired_dead_runs

    if not job_id and not recovery_candidates(repository._store_snapshot(), current):
        return []
    recovered = []
    with repository._mutation_scope() as (store, admission):
        if job_id:
            repository._require_mutable_job(store, job_id, expected_version)
        selected = {key: row for key, row in store['runs'].items()
            if not job_id or isinstance(row, dict) and row.get('job_id') == job_id}
        changes = _collect_expired_dead_runs(selected, repository._parse_run, current)
        for identifier, raw in selected.items():
            if isinstance(raw, dict) and raw.get("status") == "unknown":
                terminal, _error = repository._parse_run(raw, terminal=True)
                if terminal is not None:
                    changes[identifier] = terminal
        history = []
        for identifier, resolved in changes.items():
            original = dict(store["runs"][identifier])
            if resolved["status"] == "unknown":
                history.append(settle_dead_run(repository, store, original, current=current))
            else:
                store["runs"][identifier] = resolved
            recovered.append(identifier)
        if recovered:
            repository._write_store_unlocked(store, admission, history_records=history)
    if recovered:
        logging.getLogger(__name__).info("定时运行崩溃恢复完成", extra={"event": "scheduler_run_recovery",
            "owner": dict(repository.owner), "recovered_run_ids": recovered,
            "terminal_statuses": {str(row["run_id"]): row["status"] for row in history}})
    return recovered
