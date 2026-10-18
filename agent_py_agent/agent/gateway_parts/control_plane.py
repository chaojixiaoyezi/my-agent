# LLM: C只保存受信配置、canonical数据根、原constructor root及实际持锁租约；不是Agent，root只定位同源配置、不授owner业务写权。
# 模块用途: 让原HTTP/队列在owner隔离时继续接收，并只读投影当前P2等待决定。
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from pathlib import Path

from .multiprocess_coordinator import current_coordinator
from .multiprocess_route import OwnerRouteMismatchError, owner_digest


# LLM: 持锁组合根注入原constructor roots完整tuple/显式capability路径；不从ownerhome、请求、state/心跳反推来源，不创建owner目录。
# 类用途: 保存独立于业务Agent的真实C控制面依赖。
@dataclass(frozen=True)
class GatewayControlPlane:
    config: object
    home_root: Path
    paths: object
    worker_count: int
    c_run_id: str
    agent_factory: object = None
    agent_root: Path | None = None
    capability_config_path: Path | None = None
    workspace_roots: tuple[Path, ...] = ()

    # LLM: 原lease门内保存同一控制面和paths出处；已有paths不能被替代C重绑，否则旧HTTP会跟随新代次借权，重启须用新路径DTO。
    # 函数用途: 即使原C撤权摘登记，已进入的HTTP上下文仍保留原角色和代次边界。
    def __post_init__(self):
        lease = current_coordinator(self.paths.root, self.worker_count)
        if lease is not None and lease.c_run_id == self.c_run_id:
            with lease._lifecycle_gate:
                if (current_coordinator(self.paths.root, self.worker_count) is not lease
                        or lease.control_plane is not None
                        or (getattr(self.paths, '_coordinator_role_lease', lease) is not lease)):
                    raise OwnerRouteMismatchError('coordinator_input_source_changed')
                object.__setattr__(self, '_coordinator_role_lease', lease)
                self.paths._coordinator_role_lease = lease
                lease.control_plane = self

    # LLM: 当前登记/PID/FD、捕获的原lease同一性及冻结代次同时满足；缺原来源或替换C一律拒绝。
    # 函数用途: 只取得本对象创建时那一个仍持锁的原P2控制器，run_id重用不能复活旧调用。
    def controller(self):
        lease = current_coordinator(self.paths.root, self.worker_count)
        original = getattr(self, '_coordinator_role_lease', None)
        if lease is None or lease is not original or lease.c_run_id != self.c_run_id:
            return None
        return lease.controller

    # LLM: 原T→controller→lease门内冻结本次观察版本并执行原writer；未冻owner来自同次安全读，已有宿主owner只沿原权威保持，观察版本不重签其权限。
    #   HTTP builder不接受客户端顶层owner；metadata自报只能和实际owner冲突校验，旧guidance仅原宿主冻结字段恢复。host准备不能改actor/media/metadata/owner。
    #   回调只允许同步小型宿主准备，不等待线程/网络/模型；写前末检原来源，客户端保留版本和缺owner旧重放一律拒绝。
    # 函数用途: 在完整prepared摘要前保存原身份与本次绑定观察版本；已有v2回执不再运行此准备，撤权或失败不调用提交。
    def prepare_input(self, payload, *, prepare_request=None, commit=None):
        from ..runtime_errors import DataCorruptionError
        from ..user_space.admin_channel_identity import read_admin_channel_identity_snapshot
        from ..user_space.owner_resolver import resolve_owner_home
        from .io import (
            GATEWAY_REQUEST_FINGERPRINT_SCHEMA,
            gateway_request_fingerprint,
            validated_gateway_request_fingerprint,
        )
        from .multiprocess_takeover_runtime import CoordinatorTakeover
        from .request_owner_binding import REQUEST_OWNER_KEY, frozen_request_owner
        from .request_worker import _resolve_request_owner_from_config

        lease = getattr(self, '_coordinator_role_lease', None)
        controller = self.controller()
        if lease is None or type(controller) is not CoordinatorTakeover:
            raise OwnerRouteMismatchError('coordinator_input_source_required')
        with controller._mutex, lease._lifecycle_gate:
            if (current_coordinator(self.paths.root, self.worker_count) is not lease
                    or lease.control_plane is not self or type(controller) is not CoordinatorTakeover
                    or not controller.active()):
                raise OwnerRouteMismatchError('coordinator_input_source_changed')
            result = copy.deepcopy(payload)
            metadata = result.get('metadata')
            if (type(metadata) is not dict or 'gateway_admin_binding_version' in metadata or 'route' in result):
                raise OwnerRouteMismatchError('reserved_route_input')
            if result.get('kind') == 'steer_closeout_replay' and frozen_request_owner(result) is None:
                raise OwnerRouteMismatchError('missing_replay_owner')
            validated_gateway_request_fingerprint(result, result['id'])
            snapshot = read_admin_channel_identity_snapshot(self.home_root)
            owner = _resolve_request_owner_from_config(self.config, result, self.home_root, binding_snapshot=snapshot)
            encoded_owner = asdict(owner)
            expected = metadata.get(REQUEST_OWNER_KEY)
            if expected is not None and expected != encoded_owner:
                raise DataCorruptionError('ingress and Gateway owner conflict')
            # 同一宿主owner的既有metadata断言进入原完整摘要；它只核对顶层身份，不从断言派生或重签权限。
            metadata[REQUEST_OWNER_KEY] = dict(encoded_owner)
            metadata['gateway_admin_binding_version'] = snapshot.version
            result[REQUEST_OWNER_KEY] = encoded_owner
            result['owner_id'] = resolve_owner_home(self.home_root, owner).owner_id
            result['request_fingerprint'] = gateway_request_fingerprint(result, result['id'])
            result['request_fingerprint_schema'] = GATEWAY_REQUEST_FINGERPRINT_SCHEMA
            request_id, fingerprint = result['id'], result['request_fingerprint']
            if prepare_request is not None:
                result = prepare_request(result)
                if (type(result) is not dict or result.get('id') != request_id
                        or result.get('request_fingerprint') != fingerprint
                        or result.get('request_fingerprint_schema') != GATEWAY_REQUEST_FINGERPRINT_SCHEMA
                        or gateway_request_fingerprint(result, request_id) != fingerprint
                        or frozen_request_owner(result) != owner):
                    raise OwnerRouteMismatchError('prepared_input_changed')
            if (current_coordinator(self.paths.root, self.worker_count) is not lease
                    or lease.control_plane is not self or self.controller() is not controller
                    or not controller.active()):
                raise OwnerRouteMismatchError('coordinator_input_source_changed')
            return result if commit is None else commit(result)

    # LLM: 已发布C/W路由都核形状及绑定，再沿纯owner解析投影P2；缺路由才是未盖章，不产生终态。
    # 函数用途: 给已过ACL的请求返回路由拒绝或当前隔离等待，原文件保留供显式恢复。
    def wait_projection(self, payload):
        from ..user_space.owner_resolver import OwnerIdentity
        from .multiprocess_route import validate_published_route
        from .request_worker import _resolve_request_owner_from_config

        if "route" in payload:
            checked = validate_published_route(payload, self.worker_count, self.c_run_id)
            if not checked["ok"]:
                return {"terminal": False, "error_code": checked["error_code"],
                        "reason": checked["reason"], "admission_wait_reason": checked["reason"]}
            owner = OwnerIdentity(*checked["owner"])
        else:
            owner = _resolve_request_owner_from_config(self.config, payload, self.home_root)
        control = self.controller()
        rejection = (control.admission_rejection(owner_digest(owner)) if control is not None
                     else {"error_code": "OWNER_PARTITION_QUARANTINED", "reason": "coordinator_context_unavailable"})
        if rejection is None:
            return {}
        return {"terminal": False, "error_code": rejection["error_code"],
                "reason": rejection["reason"], "admission_wait_reason": rejection["reason"]}

    # LLM: HTTP建立后才调用；原P2复核源/隔离，并在租约生命周期短门内调用真实业务factory，隔离不调用。
    # 函数用途: 仅在本代允许时物化基础业务Agent；没有许可就保持C-only。
    def start_business(self, factory):
        from ..user_space.owner_resolver import owner_identity_from_config

        control = self.controller()
        if control is None:
            return None
        made = []
        control.launch_owner_writer(owner_digest(owner_identity_from_config(self.config)),
                                    lambda: made.append(factory()))
        return made[0] if made else None

    # LLM: 全局控制凭据不借owner业务许可；在捕获的原C生命周期短门最后核实际锁/代次，关闭或替代C不能补建凭据。
    # 函数用途: 由原持锁C准备HTTP本机鉴权凭据；不构造Agent，坏凭据仍沿原G2b拒绝。
    def prepare_local_credential(self):
        from .local_client_token import LocalClientCredentialError, ensure_local_client_credential

        original = getattr(self, "_coordinator_role_lease", None)
        if original is None:
            raise LocalClientCredentialError("COORDINATOR_CONTEXT_REQUIRED")
        with original._lifecycle_gate:
            current = current_coordinator(self.paths.root, self.worker_count)
            if current is not original or current.c_run_id != self.c_run_id:
                raise LocalClientCredentialError("COORDINATOR_CONTEXT_REQUIRED")
            return ensure_local_client_credential(self.home_root)


# LLM: 缺控制对象保持默认单进程原行为；内部模式的事实只能来自启动组合根显式注入的对象。
# 函数用途: 给HTTP处理器读取当前隔离等待，不借用业务Agent判断。
def server_wait_projection(server, payload):
    control = getattr(server, "control_plane", None)
    return control.wait_projection(payload) if control is not None else {}


# LLM: 只在fresh分支消费原C registry/lease/control；Count0原样准备/提交不新增读盘，已有receipt重放不得调用本入口。
# 函数用途: 给所有首次输入共用真实身份与原提交末门；副作用仅来自同步传入的原writer，不创建Agent或第二状态。
def prepare_coordinator_input(paths, payload, *, prepare_request=None, commit=None):
    from . import multiprocess_coordinator as coordinator

    if not coordinator._HELD_COORDINATORS and not vars(paths).get('_coordinator_role_lease'):
        prepared = payload if prepare_request is None else prepare_request(payload)
        return prepared if commit is None else commit(prepared)
    lease = coordinator.coordinator_for_root(paths.root, context=paths)
    if lease is None:
        if coordinator.coordinator_role_present(paths.root, context=paths):
            raise OwnerRouteMismatchError('coordinator_input_source_changed')
        prepared = payload if prepare_request is None else prepare_request(payload)
        return prepared if commit is None else commit(prepared)
    control = lease.control_plane
    if type(control) is not GatewayControlPlane or control.paths.root != paths.root:
        raise OwnerRouteMismatchError('coordinator_input_source_required')
    return control.prepare_input(payload, prepare_request=prepare_request, commit=commit)
