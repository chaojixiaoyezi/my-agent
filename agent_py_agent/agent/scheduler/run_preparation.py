# LLM: 来源规范与迁移证明只写原run context并守queued/claim CAS；预检与终态checkpoint仍沿原事务，不建恢复旁账。
# 模块用途: 原调度仓库原子冻结执行来源、合法旧来源迁移、预检与最终交付回执。
from __future__ import annotations

from copy import deepcopy


# LLM: 依赖原SchedulerRepository事务与epoch；旧来源只能在未开工原run内显式规范，不自行领取或复活终态。
# 类用途: 为调度仓库提供幂等来源冻结和严格旧来源恢复，重启沿同一run及审计证明。
class SchedulerRunPreparation:
    # LLM: 老监控只补稳定展示编号，不改 source/rule/run/时间；启动迁移显式记版本，查询不会偷偷写账。
    # 函数用途: 在原 owner 配额事务中一次迁移旧监控编号，保持原监控身份和历史。
    def migrate_monitor_aliases(self) -> None:
        from .monitor_state import next_alias
        with self._mutation_scope() as (store, admission):
            jobs, _errors = self._valid_jobs(store)
            changed = False
            for job in jobs:
                if job.get('alias'):
                    continue
                alias = self.due_index.allocate_job_alias() if self.due_index is not None else next_alias(store)
                store['jobs'][str(job['job_id'])] = {**job, 'alias': alias, 'alias_migration': 'scheduler_alias.v1'}
                changed = True
            if changed:
                self._write_store_unlocked(store, admission)

    # LLM: 结果由持有准确 claim 的 NO 工具产生；存入原 run，只有 done 收口才成为依赖确认结果。
    # 函数用途: 保存 JSON 工具结果；原 owner 配额检查继续约束输出大小。
    def record_rule_result(self, run_id: str, claim_id: str, value: object) -> None:
        import json

        json.dumps(value, allow_nan=False)
        with self._mutation_scope() as (store, admission):
            run = self._require_claim(store, run_id, claim_id)
            store["runs"][run_id] = {**run, "structured_result": deepcopy(value)}
            self._write_store_unlocked(store, admission)

    # LLM: 只读原快照并调用同一 epoch/claim 校验；不是续租，执行写入仍须各自 CAS。
    # 函数用途: 准入读完任务记录后确认持有者未变，避免把旧领取报告为当前执行权。
    def check_run_claim(self, run_id: str, claim_id: str) -> None:
        self._require_claim(self._store_snapshot(), run_id, claim_id)

    # LLM: 旧origin双引用须属原quota owner_root，queued且claim_epoch=0/未start/未冻异源才同次迁移；不覆盖job/source_task或复活终态。
    # 函数用途: 原store一次保存来源快照及合法旧work的old/new证明，竞争领取或终态拒绝，重放沿同一已冻结结果。
    def prepare_run_context(self, run_id: str, context: dict, *, origin_migration: dict | None = None) -> dict:
        from .repository import SchedulerConflictError

        with self._mutation_scope() as (store, admission):
            run, _error = self._parse_run(store["runs"].get(run_id))
            if run is None:
                raise SchedulerConflictError("定时执行不存在")
            if run.get("execution_context") is not None:
                if origin_migration is not None and (
                        run.get('origin_thread_id') != origin_migration['to_thread_id']
                        or run['execution_context'].get('origin_migration') != origin_migration):
                    raise SchedulerConflictError('定时来源已经冻结，不能替换')
                return deepcopy(run)
            if run["status"] != "queued" or run.get("started_at"):
                raise SchedulerConflictError("定时执行已开始，不能补造来源")
            updated = {**run, "execution_context": deepcopy(context)}
            if origin_migration is not None:
                job = self._require_job(store, str(run['job_id']))
                if (job.get('status') != 'active' or run.get('claim_id') or run.get('claimed_at') or run.get('claim_epoch', 0) != 0
                        or origin_migration.get('schema_version') != 'scheduler_origin_migration.v1'
                        or run.get('origin_thread_id') != origin_migration['from_thread_id']
                        or run.get('source_thread_id') != origin_migration['from_thread_id']
                        or job.get('source_thread_id') != origin_migration['from_thread_id']
                        or job.get('thread_id') != origin_migration['plan_thread_id']
                        or job.get('source_task_id') != run.get('source_task_id')
                        or run.get('source_task_id') != origin_migration['source_task_id']
                        or job.get('creator') != run.get('creator')
                        or job.get('owner') != run.get('owner') or run.get('owner') != origin_migration['owner']
                        or origin_migration['owner_home'] != str(self.quota_enforcer.owner_root)
                        or origin_migration['from_thread_id'] == origin_migration['to_thread_id']
                        or origin_migration['source_reference']['source_thread_id'] != origin_migration['to_thread_id']
                        or origin_migration['plan_reference']['source_thread_id'] != origin_migration['to_thread_id']
                        or origin_migration['source_reference']['binding'] != origin_migration['plan_reference']['binding']
                        or run.get('creator') != {'channel': origin_migration['plan_reference']['binding']['channel'],
                                                'actor_id': origin_migration['plan_reference']['binding']['actor_id']}
                        or context.get('origin_thread_id') != origin_migration['to_thread_id']
                        or context.get('origin_migration') != origin_migration):
                    raise SchedulerConflictError('旧定时来源迁移证明与原运行不一致')
                updated['origin_thread_id'] = origin_migration['to_thread_id']
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
