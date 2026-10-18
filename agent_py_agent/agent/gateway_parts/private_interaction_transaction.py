# LLM: 私卡MAC决定或显式G1模型元数据入口只在本次HTTP真实C来源逐次短提交；不造control receipt、claim、Agent或持久许可。
# LLM: owner配置纯解析归owner_resources唯一叶，原资源构造/卡片来源和末writer不变。
# 模块用途: 为冷C接原凭据/OAuth/审批取消与规范决定运输、模型元数据及已有模型卡决定/分段投递，保原owner库、来源寿命和部分提交重放。
from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import asdict, replace
from functools import partial

from ..common.global_write_guard import require_global_writer
from ..conversation.input_dependencies import input_dependency_owner_resources
from ..user_space.owner_resolver import (
    owner_identity_from_config,
    resolve_owner_home,
)
from .control_plane import GatewayControlPlane
from .multiprocess_coordinator import (
    coordinator_for_root,
    pin_coordinator_commit,
    retire_coordinator_commit,
)
from .multiprocess_execution_lifetime import ExecutionScopeLifetime
from .multiprocess_global_write import GlobalWriteError
from .multiprocess_http_transport import require_http_transport
from .multiprocess_route import owner_digest
from .owner_conversation_store import owner_conversation_store_from_config


# LLM: 摘要仅本次内存比较完整结构，含fields也不输出/持久/作为用户权限；不允许调用期间更换参数。
# 函数用途: 固定当前原卡回调的同一次输入。
def _body_binding(body):
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).digest()


# LLM: 只捕获原HTTP/control/runtime/lease/pin和寿命；资源/owner不是许可，卡片与当前认证仍由每个commit末核。
# 类用途: 在原C同代和目标owner门内执行原私卡或模型元数据的同步RMW；审批取消不授当前执行权。
class _PrivateInteractionTransaction:
    # LLM: 必须真实原HTTP handler/server，审批取消另捕获实际HTTP work；构造不写owner，pin仅保责任，不能传给W或替代用户决定。
    # 函数用途: 保存这一次实际卡片入口的原对象与完整输入。
    def __init__(self, handler, server, body, payload, channel, actor_id):
        from ..user_space.owner_resolver import OwnerIdentity

        self.payload, self._model_scope, self._model_scope_origin = payload, None, None
        self._approval_work = self._approval_work_origin = None
        if payload['kind'] == 'tool_approval':
            from .multiprocess_http_transport import capture_http_request_work
            try:
                self._approval_work = self._approval_work_origin = capture_http_request_work(handler)
            except ValueError as error:
                raise GlobalWriteError('approval_http_work_unavailable', 'private_interaction') from error
        self._capture(handler, server, body, OwnerIdentity(**payload["owner"]), channel, actor_id,
                      hashlib.sha256(body["reference"].encode()).hexdigest())

    # LLM: 首次model无私卡MAC，owner只由原G1 scope与唯一config解析器取得；私卡factory与原校验路径不改变。
    # 函数用途: 为本次真实模型菜单创建同一HTTP领域来源，不构造假引用或业务Agent。
    @classmethod
    def for_model_metadata(cls, handler, server, body):
        from .model_metadata_transaction import model_request_scope
        scope, owner = model_request_scope(handler, server, body)
        source = cls.__new__(cls)
        source.payload = None
        from .multiprocess_http_transport import capture_http_request_work
        try:
            source._model_work = capture_http_request_work(handler)
        except ValueError as error:
            raise GlobalWriteError('model_http_work_unavailable', 'model_metadata') from error
        source._model_scope = source._model_scope_origin = scope
        source._capture(handler, server, body, owner, scope.channel, scope.user_id,
                        _body_binding(body).hex())
        return source

    # LLM: 仅共享原HTTP/control/Root/controller/lease实际捕获，不从领域字段签写权；两个factory仍分别核MAC或原model scope。
    # 函数用途: 保存同一次原设施引用及pin，避免复制第二套来源/生命周期规则。
    def _capture(self, handler, server, body, owner, channel, actor_id, pin_identity):
        from .http_service import GatewayHTTPHandler, GatewayHTTPServer

        if not isinstance(handler, GatewayHTTPHandler) or type(server) is not GatewayHTTPServer:
            raise GlobalWriteError("private_http_source_required", "private_interaction")
        control, runtime, paths = server.control_plane, server.drain_runtime, server.paths
        if type(control) is not GatewayControlPlane or handler.server is not server.server:
            raise GlobalWriteError("private_http_source_required", "private_interaction")
        _require_private_transport(runtime, control=control, paths=paths)
        lease = coordinator_for_root(paths.root, context=control)
        controller = control.controller()
        if lease is None or controller is None or controller is not runtime.controller:
            raise GlobalWriteError("private_coordinator_required", "private_interaction")
        self.owner = owner
        self.handler, self.server, self.control, self.runtime, self.paths = (
            handler,
            server,
            control,
            runtime,
            paths,
        )
        self.lease, self.controller = lease, controller
        self.body, self.channel, self.actor = body, channel, actor_id
        self.middleware = handler._auth_middleware
        self.pid, self.thread, self.origin = os.getpid(), threading.get_ident(), self
        self.binding = _body_binding(body)
        self.lifetime = ExecutionScopeLifetime()
        self.lifetime.bind_original(self)
        self._lifetime = self.lifetime
        self._references = (
            handler,
            server,
            server.server,
            control,
            runtime,
            paths,
            lease,
            controller,
            self.middleware,
            self.owner,
        )
        self._resources = None
        self.pin = pin_coordinator_commit(
            lease,
            (
                "private_interaction",
                owner_digest(self.owner),
                pin_identity,
            ),
        )
        self._pin = self.pin
        self._closed = False

    # LLM: 同一次资源对象与canonical路径冻结；只读取资源，不初始化Agent或重选owner。
    # 函数用途: 将实际领域服务绑定到本次原提交器。
    def bind_resources(self, inputs, home, config, repo):
        if self._resources is not None:
            raise GlobalWriteError("private_resources_changed", "private_interaction")
        self._resources = (
            inputs,
            home,
            config,
            repo,
            inputs.store,
            inputs.quota,
            inputs.load_child,
            inputs.upsert_wake,
            inputs.commit,
            inputs.blocking,
        )
        self._paths = (inputs.store.storage.root, inputs.quota.owner_root, repo.db_path)

    # LLM: model配置宿主只含已裁决config/home/原Store；保存原对象和路径供同一HTTP末门复核，不授予模型/工具执行身份。
    # 函数用途: 绑定首次菜单实际使用的最小资源，不初始化外来Agent、任务或输入账。
    def bind_model_resources(self, host):
        if self._resources is not None or self._model_scope is None:
            raise GlobalWriteError("private_resources_changed", "private_interaction")
        self._resources = (host, host.home_paths, host.config, host.conversation_store)
        self._paths = (host.home_paths.owner_home_dir, host.conversation_store.storage.root)

    # LLM: 不从新C/新callback补许可；重核原handler credential/SDK字段，私卡再核MAC/锁/admin，首次模型核原scope/work，审批取消核原work；配置仅作资源事实。
    # 函数用途: 在每次卡行、目录、线程或输入/refs/Wake/deadline写前验证同一个活来源。
    def require_current(self):
        from .private_interaction_service import validated_private_interaction_from_config

        if (
            type(self) is not _PrivateInteractionTransaction
            or self.origin is not self
            or self.pid != os.getpid()
            or self.thread != threading.get_ident()
            or type(self.lifetime) is not ExecutionScopeLifetime
            or self.lifetime is not self._lifetime
        ):
            raise GlobalWriteError("private_source_changed", "private_interaction")
        try:
            self.lifetime.require_original(self)
        except ValueError as error:
            raise GlobalWriteError("private_source_closed", "private_interaction") from error
        current = (
            self.handler,
            self.server,
            self.server.server,
            self.server.control_plane,
            self.server.drain_runtime,
            self.server.paths,
            coordinator_for_root(self.paths.root, context=self.control),
            self.control.controller(),
            getattr(self.handler, "_auth_middleware", None),
            self.owner,
        )
        if (
            any(a is not b for a, b in zip(current, self._references))
            or self.pin is not self._pin
            or self.pin not in self.lease._commit_pins
            or self.handler.server is not self.server.server
            or _body_binding(self.body) != self.binding
            or self._resources is None
        ):
            raise GlobalWriteError("private_source_changed", "private_interaction")
        _require_private_transport(self.runtime, control=self.control, paths=self.paths)
        if (
            not self.middleware.has_gateway_credential(dict(self.handler.headers))
            or self.handler.headers.get("X-Channel") != self.channel
            or self.handler.headers.get("X-User-Id") != self.actor
        ):
            raise ValueError("原卡宿主或操作者已变化")
        if self._model_scope is not None:
            from .model_metadata_transaction import require_model_metadata_current
            require_model_metadata_current(self)
            return
        if self.payload['kind'] == 'tool_approval':
            from .multiprocess_http_transport import require_http_request_work
            try:
                require_http_request_work(self._approval_work, handler=self.handler)
            except ValueError as error:
                raise GlobalWriteError('approval_http_work_unavailable', 'private_interaction') from error
            work = self._approval_work
            if (work is not self._approval_work_origin or work.server is not self.server.server
                    or work.runtime is not self.runtime or work.control is not self.control or work.paths is not self.paths
                    or work.controller is not self.controller):
                raise GlobalWriteError('approval_http_work_changed', 'private_interaction')
        payload, locked = validated_private_interaction_from_config(
            self.control.home_root,
            self.control.config,
            self.body,
            channel=self.channel,
            actor_id=self.actor,
        )
        if locked is not None or payload != self.payload:
            raise ValueError("原卡身份或个人锁已变化")
        inputs, home, config, repo, store, quota, load_child, upsert, commit, blocking = (
            self._resources
        )
        if (
            inputs.store is not store
            or inputs.quota is not quota
            or inputs.load_child is not load_child
            or inputs.upsert_wake is not upsert
            or inputs.commit is not commit
            or inputs.blocking is not blocking
            or inputs.owner != self.owner
            or owner_identity_from_config(config) != self.owner
            or (store.storage.root, quota.owner_root, repo.db_path) != self._paths
            or home.owner_home_dir
            != resolve_owner_home(self.control.home_root, self.owner).home_dir
        ):
            raise GlobalWriteError("private_resources_changed", "private_interaction")

    # LLM: controller/lease锁内执行小RMW；配额原严格扫描不冒称硬实时，网络/模型/Delivery不进此回调。
    # 函数用途: 逐次消费原C目标owner末门，关闭后保已存事实并拒绝晚写。
    def perform(self, operation):
        self.require_current()
        results = []

        # LLM: 只有原controller在真实生命周期门内同步调用，不保存窗口供迟到或复制上下文使用。
        # 函数用途: 末核后完成这一段原事务。
        def commit():
            self.require_current()
            results.append(operation())

        decision = self.controller.launch_owner_writer(owner_digest(self.owner), commit)
        if decision.get("status") != "started" or len(results) != 1:
            raise GlobalWriteError("private_coordinator_unavailable", "private_interaction")
        return results[0]

    # LLM: 关闭共享lifetime先拒所有副本，再精确退原pin；不清卡/input/P3，不从失败回滚已提交业务。
    # 函数用途: 结束本次同步领域来源的责任。
    def close(self):
        if (
            self.origin is not self
            or self.pid != os.getpid()
            or self.thread != threading.get_ident()
        ):
            raise GlobalWriteError("private_source_changed", "private_interaction")
        if self._closed:
            return
        self.lifetime.close()
        retire_coordinator_commit(self.lease, self.pin)
        self.body = None
        self._closed = True


# LLM: 原owner/config路径解析后由输入领域工厂组装quota/child/既存repo，保secret快照/foreign overrides清空及原commit末核；不造Agent。
# 函数用途: 为原C卡片事务准备真实的最小owner资源，不造Agent-shaped替身。
def _resources(transaction):
    home, config, paths = _owner_resource_configuration(transaction)
    owner = transaction.owner
    store = owner_conversation_store_from_config(config, home, initialize=False)
    inputs, repo = input_dependency_owner_resources(store, owner, owner_home=home.owner_home_dir,
        subagent_workspace=paths["subagent_workspace"], commit=transaction.perform, blocking=False)
    transaction.bind_resources(inputs, home, config, repo)
    return home, config, inputs


# LLM: 原私卡来源只把同control/owner交给唯一纯解析叶；不改变资源构造时机、字段身份或writer，须同验_resources及model调用方。
# 函数用途: 保持原私卡内部调用入口，委托规范owner配置与路径计算，不创建任何资源。
def _owner_resource_configuration(transaction):
    from .owner_resources import resolve_owner_resource_configuration

    return resolve_owner_resource_configuration(transaction.control, transaction.owner)


# LLM: 原G1/SDK/MAC/锁先行；已有模型卡add/edit显式原registry+只读健康与同source分段Delivery，首次菜单/批准不借此开放，审批cancel只退休原等待，决定显式送原W或实际本机车道。
# 函数用途: 让冷C完成原卡片输入事务，保Count0/其它卡片等待与秘密边界。
def execute_coordinator_private_interaction(handler, server, body, *, channel, actor_id):
    from ..capability.runtime_config_reload import capability_config_for_paths
    from ..user_space.feishu_oauth import cancel_oauth_card_for_owner
    from ..user_space.interaction_actor_audit import (
        interaction_audit_host,
        record_interaction_actor,
    )
    from ..user_space.task_credentials import submit_credential_card_for_owner
    from .private_interaction_service import validated_private_interaction_from_config

    require_global_writer("private_interaction")
    control = server.control_plane
    _require_private_transport(server.drain_runtime, control=control, paths=server.paths)
    from ..user_space.interaction_refs import verify_interaction_reference

    payload = verify_interaction_reference(
        control.home_root,
        body["reference"],
        channel=channel,
        actor_id=actor_id,
        submitter=body.get("submitter"),
        decision=str(body["decision"]),
    )
    allowed = (
        payload["kind"] == "task_credential"
        and body["decision"] in {"submit", "cancel"}
        or payload["kind"] == "feishu_oauth"
        and body["decision"] == "cancel"
        or payload["kind"] == "tool_approval" and body["decision"] in {"cancel", "approved", "denied"}
        or payload["kind"] == "model_configuration"
        and payload.get("mode") in {"selection", "form"}
        and (body["decision"] == "cancel"
             or payload.get("mode") == "selection" and body["decision"] in {"select", "set_default", "add", "edit"}
             or payload.get("mode") == "form" and body["decision"] == "save")
    )
    if not allowed:
        return {
            "ok": False,
            "status": "waiting",
            "error_code": "COORDINATOR_BUSINESS_NOT_READY",
            "terminal": False,
        }
    payload, locked = validated_private_interaction_from_config(
        control.home_root, control.config, body, channel=channel, actor_id=actor_id
    )
    if locked is not None:
        return locked
    transaction = _PrivateInteractionTransaction(handler, server, body, payload, channel, actor_id)
    primary = None
    try:
        home, config, inputs = _resources(transaction)
        audit_host = interaction_audit_host(control.home_root, asdict(transaction.owner))
        audit_host.config.audit_enabled = bool(config.audit_enabled)

        # LLM: 此审计宿主只供原点击审计，不提供业务/current/权限；秘密表单不进入审计。
        # 函数用途: 保留原点击者与UTC时间记录。
        def audit(actor):
            return record_interaction_actor(
                audit_host, payload, decision="cancel", actor=actor, blocking=False
            )

        if payload["kind"] == "tool_approval":
            return _execute_coordinator_tool_approval(transaction, inputs, partial(audit, actor_id))
        if payload["kind"] == "feishu_oauth":
            return cancel_oauth_card_for_owner(
                home.owner_home_dir,
                transaction.owner,
                inputs,
                payload,
                body,
                actor_id=actor_id,
                audit=audit,
            )
        if control.agent_root is None:
            raise ValueError("private_capability_origin_required")
        capability = capability_config_for_paths(
            root=control.agent_root, user_path=control.capability_config_path
        )
        if payload["kind"] == "model_configuration":
            return _execute_coordinator_model_card(transaction, home, config, inputs, capability)
        return submit_credential_card_for_owner(
            home.owner_home_dir,
            capability,
            inputs,
            payload,
            body,
            audit=audit,
        )
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            transaction.close()
        except BaseException:
            if primary is None:
                raise
            primary.add_note("private_interaction_cleanup_unconfirmed")


# LLM: 取消只退休原等待；批准/拒绝只交原control receipt/实际业务scope，不借用凭据input row或模型host。
# 函数用途: 显式分派同一签名审批卡的两类原领域操作。
def _execute_coordinator_tool_approval(transaction, inputs, audit):
    if transaction.body['decision'] != 'cancel':
        from .private_approval_control import execute_coordinator_private_approval
        return execute_coordinator_private_approval(transaction)
    from ..conversation.im_tool_approval import cancel_im_tool_approval
    return cancel_im_tool_approval(inputs.store, transaction.payload, transaction.body, audit=audit,
                                  commit_transition=transaction.perform, blocking=False)


# LLM: 来源只取已捕获的原私卡transaction；本leaf构造原registry并实际消费原RMW/分段dispatch，不签新许可或创建Agent。
# 函数用途: 执行原模型卡决定，首次菜单和其它控制种类不能进入这里。
def _execute_coordinator_model_card(transaction, home, config, inputs, capability):
    payload, body, actor_id = transaction.payload, transaction.body, transaction.actor
    from .model_control_cards import model_control_host_from_resources, submit_model_control
    if body["decision"] != "cancel" and capability is not None and not capability.im_model_cards_enabled:
        raise ValueError("模型卡未启用")
    host = model_control_host_from_resources(config, home, inputs.store)
    if payload.get("mode") == "selection" and body["decision"] in {"add", "edit"}:
        from ..delivery import DeliveryService, build_default_channel_registry
        from .channel_health import adapter_runtime_health_for_paths
        from .model_control_cards import _bind_model_submission_actor
        from .model_control_dispatch import ModelCardDelivery, dispatch_model_form

        registry = build_default_channel_registry(config, runtime_health_provider=partial(
            adapter_runtime_health_for_paths, transaction.control.paths, cleanup_stale=False))
        delivery = ModelCardDelivery(DeliveryService(registry), transaction.perform, transaction.require_current)
        transaction.perform(lambda: _bind_model_submission_actor(host,host,payload,actor_id,False))
        return dispatch_model_form(host,host,transaction.owner,payload,body,delivery=delivery)
    return submit_model_control(host, host, transaction.owner, payload, body,
                                actor_id=actor_id, commit_transition=transaction.perform, blocking=False)


# LLM: 只适配原HTTP来源的结构性ValueError，不吞权限/业务错误，更不把普通同码异常当授权；实际typedRuntimeError原样上抛。
# 函数用途: 将失效C设施标成宿主暂不可用，不能误报成用户卡片无效。
def _require_private_transport(runtime, *, control, paths):
    try:
        require_http_transport(runtime, control=control, paths=paths)
    except ValueError as error:
        raise GlobalWriteError("private_http_source_unavailable", "private_interaction") from error
