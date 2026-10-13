# LLM: 创建人只从原 owner 计划线程的不可变 execution_origin 取证，不读取群里最新发言人、不猜管理员。
# 模块用途: 为旧群监控补准确创建人；证据缺失只标记不可管理，保留原计划、执行规则和结果。
from __future__ import annotations

from ..conversation.execution_threads import EXECUTION_ORIGIN_KEY, execution_origin_thread
from ..runtime_errors import DataCorruptionError

_SCHEMA = "scheduler_creator_migration.v1"


# LLM: 群控制权匹配原工具的准确 channel/actor 协议，local 标记或未知形状不能冒充 IM 创建人。
# 函数用途: 判断旧记录是否已有合法群创建人；不读取当前会话或推断用户身份。
def has_creator(repository, job: dict) -> bool:
    creator = job.get("creator")
    return (isinstance(creator, dict) and set(creator) == {"channel", "actor_id"}
        and creator["channel"] == repository.owner["provider"]
        and isinstance(creator["actor_id"], str) and bool(creator["actor_id"]))


# LLM: 原 ThreadStore 的准确 job.thread_id 与 owner 匹配后才读取冻结来源；当前会话绑定不能成为创建人事实。
# 函数用途: 为一项旧群监控解析原布置人的通道身份，缺证据返回稳定原因码。
def frozen_creator(repository, conversation_store, job: dict) -> tuple[dict | None, str]:
    threads = getattr(conversation_store, "threads", None)
    if threads is None:
        return None, "SCHEDULER_CREATOR_SOURCE_MISSING"
    try:
        thread = threads.load(str(job["thread_id"]))
        if thread is None or thread.owner_id != repository.owner["id"]:
            return None, "SCHEDULER_CREATOR_SOURCE_OWNER_MISMATCH"
        reference = thread.metadata.get(EXECUTION_ORIGIN_KEY)
        if not isinstance(reference, dict):
            return None, "SCHEDULER_CREATOR_FROZEN_SOURCE_MISSING"
        source = execution_origin_thread(threads.require, thread)
        if source.thread_id != str(job.get("source_thread_id") or ""):
            return None, "SCHEDULER_CREATOR_SOURCE_MISMATCH"
        if job.get("source_request_id") and reference["operation_key"] != job["source_request_id"]:
            return None, "SCHEDULER_CREATOR_SOURCE_MISMATCH"
        binding = reference["binding"]
        if not binding["actor_id"] or binding["channel"] != repository.owner["provider"]:
            return None, "SCHEDULER_CREATOR_SOURCE_MISMATCH"
        return {"channel": binding["channel"], "actor_id": binding["actor_id"]}, ""
    except (DataCorruptionError, OSError, KeyError, TypeError, AttributeError, ValueError):
        return None, "SCHEDULER_CREATOR_SOURCE_INVALID"


# LLM: 迁移只处理本 group owner 的旧缺失 creator，变更经过原 quota/锁并保留版本 CAS；幂等重试不刷时间。
# 函数用途: 冷启动补可信旧创建人或记录缺证据原因，不改变任务来源、运行规则与在途执行。
def migrate_monitor_creators(repository, conversation_store, *, now: float) -> dict[str, int]:
    result = {"migrated": 0, "unmanaged": 0}
    if repository.owner["kind"] != "group":
        return result
    jobs, _errors = repository.list_jobs()
    pending = {job["job_id"]: frozen_creator(repository, conversation_store, job)
        for job in jobs if not has_creator(repository, job)}
    if not pending:
        return result
    with repository._mutation_scope() as (store, admission):
        changed = False
        for identifier, (creator, reason) in pending.items():
            job, _error = repository._parse_job(store["jobs"].get(identifier))
            if job is None or has_creator(repository, job) or job["status"] == "deleted":
                continue
            marker = {"schema_version": _SCHEMA, "status": "migrated" if creator else "unmanaged", "reason_code": reason}
            result["migrated" if creator else "unmanaged"] += 1
            previous = job.get("creator_migration") or {}
            if {key: previous.get(key) for key in marker} == marker:
                continue
            store["jobs"][identifier] = {**job, "creator": creator,
                "creator_migration": {**marker, "at": now}, "version": int(job["version"]) + 1, "updated_at": now}
            changed = True
        if changed:
            repository._write_store_unlocked(store, admission)
    return result


# LLM: 未归属记录只暴露准确 job ID，不展示名称、提示词或他人内容，也不赋予当前查询人控制权。
# 函数用途: 为管理员诊断提供缺创建人证据的监控编号，普通管理仍沿原 creator 过滤。
def unmanaged_monitor_ids(repository) -> list[str]:
    if repository.owner["kind"] != "group":
        return []
    jobs, _errors = repository.list_jobs()
    return sorted(str(job["job_id"]) for job in jobs if not has_creator(repository, job))
