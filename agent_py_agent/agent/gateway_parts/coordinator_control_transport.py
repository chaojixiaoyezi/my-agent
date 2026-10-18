# LLM: 冷C普通控制只消费当前真实HTTP工作片并发布原receipt；本来源不执行领域、创建Agent、授P3或接受通用写回调。
# 模块用途: 在基础业务未加载时，把健康远端owner的原控制交给既有Worker控制泵。
from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import replace

from ..common.json_io import check_io_deadline
from ..conversation.control_commands import (
    parse_conversation_control,
    persisted_control_command_text,
)
from ..conversation.input_resource_retry import input_resource_deadline
from ..user_space.owner_resolver import OwnerIdentity
from .multiprocess_coordinator import (
    coordinator_for_root,
    pin_coordinator_commit,
    retire_coordinator_commit,
)
from .multiprocess_execution_lifetime import ExecutionScopeLifetime
from .multiprocess_global_write import GlobalWriteError
from .multiprocess_http_transport import capture_http_handler_work, require_http_handler_work
from .multiprocess_route import owner_digest


# LLM: 完整输入摘要只用于同次内存变化核验，不持久化或代替原operation身份/权限公式。
# 函数用途: 固定当前HTTP正文，避免后续读取换成另一次输入。
def _body_binding(body):
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).digest()


# LLM: scope与command始终由原认证header/body及配置解析器投影；不采信body中的owner/root，不构造业务资源。
# 函数用途: 为捕获和末核使用同一个原控制解析过程。
def _request_input(handler, server, body):
    from .control_service import _scope_request_payload
    from .http_handlers import _gateway_control_scope, _request_channel
    from .request_worker import _resolve_request_owner_from_config

    text = str(body.get('command', body.get('prompt', '')) or '')
    command = parse_conversation_control(text, reject_unknown_slash=True)
    user, channel = _request_channel(handler)
    scope = _gateway_control_scope(handler, body, user_id=user, channel=channel)
    owner = _resolve_request_owner_from_config(server.control_plane.config,
        _scope_request_payload(scope), server.control_plane.home_root)
    return command, persisted_control_command_text(command, text), replace(scope, resolved_owner=owner)


# LLM: 精确类型/origin/PID/thread/lifetime和原work共同约束一次固定producer；没有perform(callback)或可持久化writer。
# 类用途: 保存原HTTP、C租约与固定控制输入，最后短门只允许发布这一条原回执。
class _CoordinatorControlTransport:
    # LLM: 实际handler/upper/lower/control同源才捕获；pin只记录本次责任，不授权owner或创建Agent。
    # 函数用途: 固定一次冷控制的原对象与预算，不写业务状态。
    def __init__(self, handler, server, body):
        from .control_operation_service import gateway_control_operation_identity
        from .control_plane import GatewayControlPlane
        from .http_service import GatewayHTTPHandler, GatewayHTTPServer
        from .owner_management_control import is_owner_management_control

        if (not isinstance(handler, GatewayHTTPHandler) or type(server) is not GatewayHTTPServer
                or type(server.control_plane) is not GatewayControlPlane or server.control_plane.worker_count <= 0
                or server.agent is not None):
            raise GlobalWriteError('control_http_source_required', 'control_transport')
        self.handler, self.server, self.body = handler, server, body
        try:
            self.work = capture_http_handler_work(handler, server)
        except ValueError as error:
            raise GlobalWriteError('control_http_source_unavailable', 'control_transport') from error
        self.control, self.runtime, self.paths = server.control_plane, server.drain_runtime, server.paths
        self.lease = coordinator_for_root(self.paths.root, context=self.control)
        self.controller = self.control.controller()
        self.command, self.command_text, self.scope = _request_input(handler, server, body)
        if (self.lease is None or self.controller is None or self.command is None or not self.command.valid
                or self.command.kind == 'model' or is_owner_management_control(self.command.kind)
                or self.scope.resolved_owner == OwnerIdentity.local_main()):
            raise GlobalWriteError('control_business_not_ready', 'control_transport')
        self.owner = self.scope.resolved_owner
        self.operation_id, self.input_digest = gateway_control_operation_identity(self.scope, self.command_text)
        self.middleware = handler._auth_middleware
        self.binding, self.pid, self.thread, self.origin = _body_binding(body), os.getpid(), threading.get_ident(), self
        self.lifetime = ExecutionScopeLifetime()
        self.lifetime.bind_original(self)
        self._lifetime = self.lifetime
        self._refs = (self.work, self.control, self.runtime, self.paths, self.lease, self.controller,
                      self.middleware, self.control.config, self.owner)
        self._home, self._generation = self.control.home_root, self.lease.c_run_id
        self.deadline = input_resource_deadline()
        self._closed = False
        self.pin = self._pin = pin_coordinator_commit(self.lease,
            ('control_transport', owner_digest(self.owner), self.operation_id))

    # LLM: 同一次scope/work/credential及C引用必须在最后门内重核；预算不签写权，关闭或复制source不能借新C续用。
    # 函数用途: 在读取原事实和发布前确认本次固定来源仍有效。
    def require_current(self):
        if (type(self) is not _CoordinatorControlTransport or self.origin is not self
                or self.pid != os.getpid() or self.thread != threading.get_ident()
                or self.lifetime is not self._lifetime or type(self.lifetime) is not ExecutionScopeLifetime):
            raise GlobalWriteError('control_http_source_changed', 'control_transport')
        try:
            self.lifetime.require_original(self)
            require_http_handler_work(self.work, self.handler, self.server)
        except ValueError as error:
            raise GlobalWriteError('control_http_source_unavailable', 'control_transport') from error
        current = (self.work, self.server.control_plane, self.server.drain_runtime, self.server.paths,
                   coordinator_for_root(self.paths.root, context=self.control), self.control.controller(),
                   getattr(self.handler, '_auth_middleware', None), self.control.config, self.scope.resolved_owner)
        if (any(a is not b for a, b in zip(current, self._refs)) or self.server.agent is not None
                or self.pin is not self._pin or self.pin not in self.lease._commit_pins
                or self.control.home_root != self._home or self.lease.c_run_id != self._generation
                or _body_binding(self.body) != self.binding
                or not self.middleware.has_gateway_credential(dict(self.handler.headers))
                or _request_input(self.handler, self.server, self.body) != (self.command, self.command_text, self.scope)):
            raise GlobalWriteError('control_http_source_changed', 'control_transport')
        check_io_deadline(self.deadline)

    # LLM: 只调用固定receipt/route CAS叶子，不提供任意路径或可执行callback；原controller目标owner门与Root门只包最后本地提交。
    # 函数用途: 将这一条原回执安全交给Worker，返回已存原事实。
    def publish(self, receipt, expected_raw):
        from .multiprocess_control_forward import prepare_coordinator_remote_control
        self.require_current()
        return prepare_coordinator_remote_control(self, receipt, expected_raw)

    # LLM: 先关闭原共享lifetime，再精确退pin；不清回执/P3、不回滚已存事实，不让清理错误盖掉首个typed异常。
    # 函数用途: 结束本次HTTP运输责任。
    def close(self):
        if self.origin is not self or self.pid != os.getpid() or self.thread != threading.get_ident():
            raise GlobalWriteError('control_http_source_changed', 'control_transport')
        if not self._closed:
            self.lifetime.close()
            retire_coordinator_commit(self.lease, self.pin)
            self._closed = True


# LLM: 仅实际冷HTTP分支使用；所有层保原typed对象，finally只收本次来源，不补发或执行领域。
# 函数用途: 为本次普通控制准备原幂等回执，完成后关闭来源。
def execute_coordinator_control(handler, server, body):
    from .control_operation_service import prepare_coordinator_control_operation
    if _request_input(handler, server, body)[2].resolved_owner == OwnerIdentity.local_main():
        return None
    source = _CoordinatorControlTransport(handler, server, body)
    primary = None
    try:
        return prepare_coordinator_control_operation(source)
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            source.close()
        except BaseException:
            if primary is None:
                raise
            primary.add_note('control_transport_cleanup_unconfirmed')
