# LLM: 执行准备与预检事实只写原 scheduler run；没有第二个检查点账本。动作 done 时原终态事务才推进 job checkpoint。
# 模块用途: 原调度仓库的执行上下文、预检观察及等待任务最终交付回执 CAS。
from __future__ import annotations

from copy import deepcopy


# LLM: 依赖 SchedulerRepository 的原事务与 claim epoch 校验；不自行领取、执行或恢复模型任务。
# 类用途: 为调度仓库增加幂等准备能力，保证重启不会改变已预留工作身份。
class SchedulerRunPreparation:
    # LLM: 只读原快照并调用同一 epoch/claim 校验；不是续租，执行写入仍须各自 CAS。
    # 函数用途: 准入读完任务记录后确认持有者未变，避免把旧领取报告为当前执行权。
    def check_run_claim(self, run_id: str, claim_id: str) -> None:
        self._require_claim(self._store_snapshot(), run_id, claim_id)

    # LLM: 仅 queued 且尚未领取时允许第一次冻结；已冻结原样返回，拒绝在运行后补造执行来源。
    # 函数用途: 将来源会话绑定快照与原 run 同次写盘，供原 wake、模型和投递路径读取。
    def prepare_run_context(self, run_id: str, context: dict) -> dict:
        from .repository import SchedulerConflictError

        with self._mutation_scope() as (store, admission):
            run, _error = self._parse_run(store["runs"].get(run_id))
            if run is None:
                raise SchedulerConflictError("定时执行不存在")
            if run.get("execution_context") is not None:
                return deepcopy(run)
            if run["status"] != "queued" or run.get("started_at"):
                raise SchedulerConflictError("定时执行已开始，不能补造来源")
            updated = {**run, "execution_context": deepcopy(context)}
            store["runs"][run_id] = updated
            self._write_store_unlocked(store, admission)
        return deepcopy(updated)

    # LLM: 当前 claim 独占预检结果，重放沿原结果而不重跑工具；checkpoint 在工作成功前保持不动。
    # 函数用途: 在原调度事务中保存预检的 changed/no_change/failed 结果和摘要。
    def record_precheck(self, run_id: str, claim_id: str, observation: dict) -> dict:
        if observation.get("status") not in {"changed", "no_change", "failed"}:
            raise ValueError("预检状态无效")
        with self._mutation_scope() as (store, admission):
            run = self._require_claim(store, run_id, claim_id)
            if run.get("precheck_observation") is not None:
                return deepcopy(run["precheck_observation"])
            store["runs"][run_id] = {**run, "precheck_observation": deepcopy(observation)}
            self._write_store_unlocked(store, admission)
        return deepcopy(observation)

    # LLM: 仅保存同 task/thread、仍 waiting 且 wake_handled 的后台交付结果；原 wake 尚未确认时先写现有结果字段，
    #   再由调用方确认 wake，避免周期对账读到 completed 就用第一片回复结束任务。无 job/checkpoint 写入副作用。
    # 函数用途: 把子任务续接后的最终投递回执写回原定时 run，供正常收口和进程重启读取。
    def record_waiting_delivery(self, report, *, now: float) -> bool:
        if not report.wake_handled:
            return False
        with self._mutation_scope() as (store, admission):
            run, _error = self._parse_run(store["runs"].get(report.task_id))
            if run is None or run["status"] != "waiting" or run["thread_id"] != report.thread_id:
                return False
            store["runs"][report.task_id] = {
                **run, "response": report.response[:4000], "delivery_status": report.delivery_status[:80],
                "delivery_reason": report.delivery_reason[:160], "updated_at": now,
            }
            self._write_store_unlocked(store, admission)
        return True
