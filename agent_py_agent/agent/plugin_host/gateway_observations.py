# LLM: D1/D2共享真实A授权和当前owner原账；固定路由不接受插件注册，不读取其他owner或初始化Agent。
# 模块用途: 将任务/Goal/交互与正式记忆/注册制品组装为生产观察端口。
from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from ..gateway_parts import control_operation_service as controls
from ..gateway_parts.control_service import GatewayControlScope
from ..gateway_parts.io import write_json_file_atomic
from .action_specs import action_specs
from .artifacts import ArtifactReadService, ArtifactTaskScope
from .authorization import revalidate_call
from .contracts import DomainResult, resolve_read_context
from .conversations import BoundCursor, digest
from .errors import HostAPIError
from .goals import GoalAdapter
from .interactions import InteractionAdapter, ResponseTickets
from .memory import MemoryReadService, _owner_bytes
from .schemas import canonical_json, strict_json, thaw
from .tasks import TaskAdapter


# LLM: 路径只从原TaskLink和owner取得；canonical归属与安装范围要同时满足，不从名称猜任务权限。
# 类用途: 给D1适配器提供真实授权、原store、分页与原control receipt。
class GatewayObservationServices:
    # LLM: 构造只保留同一组合根端口，不IO。
    # 函数用途: 绑定当前宿主、会话域和原MAC。
    def __init__(self, resources, conversation, cursors):
        self.resources, self.conversation, self.cursors = resources, conversation, cursors

    # LLM: 所有领域入口及返回前均经同一真实issuer。
    # 函数用途: 复核当前调用仍有资格。
    def revalidate(self, authorized):
        revalidate_call(authorized)

    # LLM: 冷读当前owner，不缓存会被停用的执行主体或通过路径猜owner。
    # 函数用途: 返回D1要求的存储和准确范围核验口。
    def resolve(self, authorized):
        context = resolve_read_context(authorized)
        return SimpleNamespace(owner=context.identity, owner_id=context.owner_home.owner_id, store=self.resources.store_for(context),
                               allows=self.allows, read=context)

    # LLM: thread/task分别授权；created-by-installation必须有原Goal或原请求来源证明。
    # 函数用途: 检查精确资源的当前owner及安装范围。
    def allows(self, authorized, kind, identifier):
        context = resolve_read_context(authorized)
        store = self.resources.store_for(context)
        if kind == "thread":
            try:
                self.conversation.require_thread(store, identifier, authorized, context)
                return True
            except HostAPIError as error:
                if error.status == 404:
                    return False
                raise
        task, error = store.tasks.load_report(identifier)
        if error:
            raise HostAPIError("STORE_UNAVAILABLE", 503)
        if task is None or not self.allows(authorized, "thread", task.thread_id):
            return False
        scope = authorized.binding.grant["tasks"]
        if identifier in scope["explicit_ids"]:
            return True
        if scope["created_by_installation"]:
            goals, error = store.goals.list_report(task.thread_id)
            if error:
                raise HostAPIError("STORE_UNAVAILABLE", 503)
            if any(goal.task_id == identifier and goal.metadata.get("plugin_origin", {}).get("installation_ref")
                   == authorized.binding.installation_ref for goal in goals):
                return True
            from .requests import read_request_facts
            try:
                read_request_facts(self.resources.paths_for(context), identifier, expected_owner=context.identity,
                                   installation_ref=authorized.binding.installation_ref)
                return True
            except HostAPIError as error:
                if error.code in {"RESOURCE_NOT_FOUND", "INVALID_SCHEMA"}:
                    return False
                raise
        return False

    # LLM: MAC绑定完整安装/owner/action/filter及原集合快照；原数据变化明确过期，不拼接新旧页。
    # 函数用途: 生成D1闭合分页视图。
    def page(self, authorized, rows):
        context = resolve_read_context(authorized)
        payload = thaw(authorized.call.payload)
        cursor = BoundCursor(self.cursors, authorized, context, {k: v for k, v in payload.items() if k != "cursor"})
        saved = cursor.decode(payload.get("cursor"))
        anchor = {"sha256": digest(rows)}
        if saved and saved["anchor"] != anchor:
            raise HostAPIError("CURSOR_EXPIRED", 409)
        offset = saved["offset"] if saved else 0
        selected = rows[offset:offset + payload.get("limit", 50)]
        end = offset + len(selected)
        import time
        token = cursor.encode(anchor, end, saved["expires"] if saved else time.time() + 900) if end < len(rows) else None
        return {"items": selected, "next_cursor": token, "snapshot_id": anchor["sha256"], "complete": token is None, "load_errors": []}

    # LLM: 复用原control receipt及转换锁；先存executing再调用副作用，unknown绝不自动重做。
    # 函数用途: 为D1写操作提供唯一持久幂等回执。
    def once(self, authorized, effect):
        context = resolve_read_context(authorized)
        paths = self.resources.paths_for(context)
        message_id = digest([authorized.binding.installation_ref, context.owner_home.owner_id,
                             authorized.call.action, authorized.call.idempotency_key])
        scope = GatewayControlScope(user_id=context.owner_home.owner_id, channel="plugin",
            conversation_id=authorized.binding.installation_ref, resolved_owner=context.identity,
            metadata={"message_id": message_id})
        text = canonical_json(authorized.call.payload)
        operation_id, fingerprint = controls.gateway_control_operation_identity(scope, text)
        with controls.gateway_control_operation_transition(paths, operation_id):
            self.revalidate(authorized)
            prior = controls.read_gateway_control_operation(paths, operation_id)
            if prior:
                if prior.input_digest != fingerprint:
                    raise HostAPIError("IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD", 409)
                if prior.state == "completed":
                    result = prior.result.get("details") or prior.result["plugin_domain"]
                    return DomainResult(result["status"], result["data"])
                raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown")
            import time
            operation = controls._new_control_operation_receipt(scope, "plugin_domain", text, operation_id, fingerprint)
            path = controls.gateway_control_operation_path(paths, operation_id)
            write_json_file_atomic(path, replace(operation, state="executing", updated_at=time.time()).to_dict())
            try:
                result = effect()
            except HostAPIError as exc:
                if exc.outcome == "not_started":
                    path.unlink(missing_ok=True)
                raise
            write_json_file_atomic(path, replace(operation, state="completed", updated_at=time.time(), result={
                "kind": "plugin_domain", "ok": True, "message": "",
                "details": {"status": result.http_status, "data": thaw(result.data)}}).to_dict())
            return result

    # LLM: 只从已授权原task_path读取注册账，路径为空/跨owner不能当制品根；不扫描整个owner。
    # 函数用途: 给D2提供经过原TaskLink和线程授权的准确业务根。
    def task_scopes(self, authorized, context):
        store = self.resources.store_for(context)
        threads, errors = store.threads.list_report(limit=0)
        if errors:
            raise HostAPIError("STORE_UNAVAILABLE", 503)
        scopes = []
        for thread in threads:
            if not self.allows(authorized, "thread", thread.thread_id):
                continue
            tasks, errors = store.tasks.list_report(thread.thread_id)
            if errors:
                raise HostAPIError("STORE_UNAVAILABLE", 503)
            for task in tasks:
                if not task.task_path or not self.allows(authorized, "task", task.task_id):
                    continue
                root = Path(task.task_path)
                if not root.is_absolute() or not root.is_relative_to(context.owner_home.home_dir):
                    raise HostAPIError("RESOURCE_NOT_FOUND", 404)
                scopes.append(ArtifactTaskScope(context.identity, task.task_id, root, (root,)))
        return scopes


# LLM: 中央目录没有assets/telemetry动作，不额外开放动态前缀；这里只组装现有22个D动作。
# 类用途: 固定分发D1与D2的生产观察入口。
class GatewayObservations:
    actions = frozenset(name for name in action_specs() if name.startswith(("tasks.", "goals.", "interactions.", "memory.", "workspace.artifacts.")))

    # LLM: 所有backend只绑定可信宿主；短期人工票据必须另经真实人入口签发。
    # 函数用途: 组装五类既有适配器。
    def __init__(self, resources, conversation, cursors):
        self.services = GatewayObservationServices(resources, conversation, cursors)
        from .gateway_operations import GatewayOperations
        operations = GatewayOperations(self.services)
        self.tickets = ResponseTickets(self.services, operations.verify_human)
        self.routes = {"tasks": TaskAdapter(self.services, operations), "goals": GoalAdapter(self.services, operations),
            "interactions": InteractionAdapter(self.services, operations, self.tickets), "memory": MemoryReadService(),
            "workspace": ArtifactReadService(self.services.task_scopes)}

    # LLM: 固定五组对象只接真实AuthorizedCall，未知动作由中央目录及本白名单共同拒绝。
    # 函数用途: 路由当前观察或有限控制调用。
    def call(self, authorized):
        if authorized.call.action not in self.actions:
            raise HostAPIError("UNSUPPORTED_ACTION")
        return self.routes[authorized.call.action.split(".")[0]].call(authorized)
