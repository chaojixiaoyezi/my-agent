# LLM: D1只消费宿主注册的原存储/授权/游标/回执端口，不构造执行身份。
# 模块用途: 投影任务、读取待办并精确请求任务取消。
from __future__ import annotations

import hashlib
from contextlib import ExitStack
from datetime import datetime, timezone

from ..conversation.store_io import safe_file_stem
from ..user_space.owner_resolver import _owner_id
from .action_specs import action_spec
from .contracts import AuthorizedCall, DomainResult, decode_call
from .errors import HostAPIError
from .schemas import PAGE_MAX_BYTES, canonical_json, thaw


# LLM: services由组合根提供revalidate/resolve/page/once；revalidate核真实issuer、存活grant及enabled，
# resolve只读取得{owner,store,allows}，page复用A的绑定快照MAC游标，once复用原持久control receipt。
# once必须先写原in-progress回执再执行；未知提交保unknown、同摘要返回原结果、不同摘要409，禁止自动重做。
# 函数用途: 在任何领域I/O前复核真实来源、闭合参数和精确capability。
def checked(services, authorized: AuthorizedCall, prefix: str) -> dict:
    services.revalidate(authorized)
    call = authorized.call
    if not call.action.startswith(prefix):
        raise HostAPIError("UNSUPPORTED_ACTION")
    envelope = {"schema": call.schema, "action": call.action, "payload": thaw(call.payload)}
    if call.idempotency_key is not None:
        envelope["idempotency_key"] = call.idempotency_key
    if call.as_user_id is not None:
        envelope["as_user"] = {"user_id": call.as_user_id}
    decoded = decode_call(canonical_json(envelope).encode())
    if not action_spec(call.action).capabilities <= authorized.binding.capabilities:
        raise HostAPIError("CAPABILITY_DENIED", 403)
    if authorized.effective_owner != authorized.binding.effective_owner:
        raise HostAPIError("TARGET_DENIED", 403)
    return thaw(decoded.payload)


# LLM: context只能由宿主resolver提供，不接受插件路径或初始化冷owner。
# 函数用途: 取得与已验证owner完全相符的原存储。
def context_for(services, authorized):
    context = services.resolve(authorized)
    if context.owner != authorized.effective_owner:
        raise HostAPIError("TARGET_DENIED", 403)
    return context


# LLM: 原路径validator不能清洗请求为另一资源；读取前先拒绝非原样opaque编号。
# 函数用途: 验证资源编号不会改变原文件定位。
def resource_id(value: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."} or safe_file_stem(value) != value:
        raise HostAPIError("RESOURCE_NOT_FOUND", 404)
    return value


# LLM: thread/task分开授权；allows必须重核canonical归属及installation创建事实，ID本身不授范围。
# 函数用途: 要求一个精确资源属于本次授权。
def require_scope(context, authorized, kind: str, value: str) -> None:
    resource_id(value)
    if context.allows(authorized, kind, value) is not True:
        raise HostAPIError("RESOURCE_NOT_FOUND", 404)


# LLM: 不使用require_thread创建或纠正记录，损坏不伪装空结果。
# 函数用途: 校验授权线程的原身份及完整provider/群/个人owner命名空间。
def require_thread(context, authorized, thread_id: str):
    require_scope(context, authorized, "thread", thread_id)
    thread, error = context.store.threads.load_report(thread_id)
    if error is not None:
        raise HostAPIError("STORE_UNAVAILABLE", 503)
    if thread is None or thread.thread_id != thread_id or thread.owner_id != _owner_id(context.owner):
        raise HostAPIError("RESOURCE_NOT_FOUND", 404)
    return thread


# LLM: 即便拥有线程也必须单独拥有任务；TaskLink唯一权威，不从telemetry读状态。
# 函数用途: 严格读取任务并复核所属线程。
def require_task(context, authorized, task_id: str):
    require_scope(context, authorized, "task", task_id)
    task, error = context.store.tasks.load_report(task_id)
    if error is not None:
        raise HostAPIError("STORE_UNAVAILABLE", 503)
    if task is None or task.task_id != task_id:
        raise HostAPIError("RESOURCE_NOT_FOUND", 404)
    require_thread(context, authorized, task.thread_id)
    return task


# LLM: 无原revision时使用canonical全状态指纹，CAS时在原转换锁内重新计算，不建第二份状态账。
# 函数用途: 生成有界且可重复的任务版本。
def task_revision(task) -> int:
    digest = hashlib.sha256(canonical_json(task.to_dict()).encode()).digest()
    return (int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)) or 1


# LLM: 缺少真实时间返回null，不从更新时间或当前时间捏造完成时间。
# 函数用途: 把已知epoch投影为UTC。
def utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value else None


# LLM: 闭合投影不包含路径、工具参数、owner或正文；TaskLink没有终结时间时明确null。
# 函数用途: 将原任务状态转成公开Task。
def project_task(task) -> dict:
    return {"task_id": task.task_id, "thread_id": task.thread_id,
            "parent_task_id": getattr(task, "parent_task_id", None), "status": task.status[:64],
            "kind": task.work_kind[:32], "name": task.work_name[:120], "revision": task_revision(task),
            "started_at": utc(task.created_at), "ended_at": utc(getattr(task, "ended_at", None))}


# LLM: 回包前再核鲜活权限；写已提交时保留committed事实，不能以撤权伪装未执行。
# 函数用途: 返回不可变领域成功结果。
def finish(services, authorized, status: int, data, *, outcome="not_started") -> DomainResult:
    try:
        services.revalidate(authorized)
    except HostAPIError as error:
        raise HostAPIError(error.code, error.status, outcome, error.message) from None
    if len(canonical_json(data).encode()) > PAGE_MAX_BYTES:
        raise HostAPIError("PAYLOAD_TOO_LARGE", 413, outcome)
    return DomainResult(status, data)


# LLM: 只接受五种既定待办状态，不把未知值降级pending或把待办全done视为任务完成。
# 函数用途: 裁剪待办内容到公开字段。
def project_progress(rows) -> list[dict]:
    result = []
    for row in rows:
        if row.get("status") not in {"pending", "in_progress", "done", "skipped", "blocked"}:
            raise HostAPIError("STORE_UNAVAILABLE", 503)
        result.append({"id": resource_id(row["id"]), "title": str(row.get("title", ""))[:256],
                       "status": row["status"]})
    return result


# LLM: 原状态写入必须留在原任务转换锁内。
# 类用途: 提供任务动作适配入口。
class TaskAdapter:
    # LLM: operations提供progress(a,task)、descendants(a,task)精确ID列表、cancel(a,tasks,reason)。
    # cancel在已持原锁下只请求准确任务/attempt取消，不能递归夺锁、清Goal、扩大任务集合或虚报资源停止。
    # 函数用途: 保存原领域服务和精确取消端口。
    def __init__(self, services, operations):
        self.services, self.operations = services, operations

    # LLM: 路由固定四个动作，写入经原回执后再取任务锁；重放也必须先重核授权。
    # 函数用途: 执行任务读取、进度查询或取消请求。
    def call(self, authorized):
        payload = checked(self.services, authorized, "tasks.")
        context = context_for(self.services, authorized)
        if authorized.call.action == "tasks.cancel":
            result = self.services.once(authorized, lambda: self._cancel(context, authorized, payload))
            return finish(self.services, authorized, result.http_status, result.data, outcome="committed")
        if authorized.call.action == "tasks.list":
            require_thread(context, authorized, payload["thread_id"])
            tasks, errors = context.store.tasks.list_report(payload["thread_id"])
            if errors:
                raise HostAPIError("STORE_UNAVAILABLE", 503)
            rows = [project_task(task) for task in tasks if context.allows(authorized, "task", task.task_id)]
            data = self.services.page(authorized, rows)
        else:
            task = require_task(context, authorized, payload["task_id"])
            data = project_task(task)
            if authorized.call.action == "tasks.progress":
                page = self.services.page(authorized, project_progress(self.operations.progress(authorized, task)))
                data = {"task": data, "items": page["items"], "has_more": page["next_cursor"] is not None,
                        "next_cursor": page["next_cursor"]}
        return finish(self.services, authorized, 200, data)

    # LLM: 根和后代统一排序取原锁；锁内重核CAS/集合/授权，集合变化拒绝而不临时扩锁，无论如何不触碰Goal。
    # 函数用途: 串行受理准确任务集合的取消。
    def _cancel(self, context, authorized, payload):
        task = require_task(context, authorized, payload["task_id"])
        descendants = self.operations.descendants(authorized, task) if payload["include_children"] else []
        selected = set(descendants) | {task.task_id}
        for task_id in selected:
            require_task(context, authorized, task_id)
        with ExitStack() as locks:
            for task_id in sorted(selected):
                locks.enter_context(context.store.tasks.transition_guard(task_id))
            self.services.revalidate(authorized)
            task = require_task(context, authorized, payload["task_id"])
            if task_revision(task) != payload["expected_revision"]:
                raise HostAPIError("REVISION_CONFLICT", 409)
            descendants = self.operations.descendants(authorized, task) if payload["include_children"] else []
            if set(descendants) | {task.task_id} != selected:
                raise HostAPIError("REVISION_CONFLICT", 409)
            tasks = [task] + [require_task(context, authorized, child_id)
                              for child_id in sorted(selected - {task.task_id})]
            self.services.revalidate(authorized)
            result = self.operations.cancel(authorized, tuple(tasks), payload["reason"])
            if (type(result.get("cancel_requested")) is not bool
                    or (result.get("resources_stopped") is not None and type(result["resources_stopped"]) is not bool)):
                raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown")
            return DomainResult(202, {"task_id": task.task_id, "cancel_requested": result["cancel_requested"],
                                      "resources_stopped": result.get("resources_stopped")})
