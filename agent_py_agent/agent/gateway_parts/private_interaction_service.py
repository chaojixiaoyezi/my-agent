# LLM: Count0原锁/admin callbacks保持懒调用；C显式同源config只读wrapper共用唯一前缀，typed来源故障不是无效用户卡。
# 模块用途: 为原Agent与冷C校验同一卡片，保G1先于读body；只有授权后表单typed错误返回可改正分类。
# LLM: 原503仅附有界安全异常元数据；不读异常正文/args/请求字段，不改响应、写门或部分提交事实。
# LLM: 跨 owner 卡片入口必须先核对宿主凭据、SDK actor、签名和个人解锁，再初始化引用 owner；引用不是批准。
# 模块用途: 接受原 IM 适配器验证后的私人卡回调，将精确决定交给原工具审批记录。
from __future__ import annotations

import json
import re
import sqlite3
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from ..conversation.im_tool_approval import resolve_im_tool_approval
from ..session_lock.service import UnlockService
from ..session_lock.store import SessionLockStore
from ..user_space.interaction_refs import verify_interaction_reference
from ..user_space.owner_resolver import OwnerIdentity
from ..user_space.task_credential_forms import CredentialFormValidationError
from .multiprocess_global_write import GlobalWriteError
from .request_worker import _resolve_request_agent_for_owner, admin_channel_identity_for_request

_PLUGIN_INPUT_RECORD_GUARD = ContextVar("plugin_input_record_guard", default=None)


# LLM: 只在同步原提交期间设置，ContextVar隔离并发调用；finally恢复，不改变原服务公开参数合同。
# 函数用途: 给三类原卡锁内提交临时绑定同一次插件票据末核。
@contextmanager
def plugin_input_record_check(guard):
    token = _PLUGIN_INPUT_RECORD_GUARD.set(guard)
    try:
        yield
    finally:
        _PLUGIN_INPUT_RECORD_GUARD.reset(token)


# LLM: 仅原卡持锁叶子调用；普通IM路径没有绑定时完全无动作，不创建记录或签权。
# 函数用途: 在原卡身份核验后执行可选插件CAS末核。
def check_plugin_input_record(row):
    guard = _PLUGIN_INPUT_RECORD_GUARD.get()
    if guard is not None:
        guard(row)


# LLM: 只读取原个人密码库的结构化状态，不能从卡片颜色、按钮或模型正文判已解锁；告警出站不经此门。
# 函数用途: 检查私人控制是否已首设密码并处于解锁期，不创建聊天、刷新活跃或自动解锁。
# LLM: 原Agent入口保持原Store初始化；冷C显式只读同库，不能取消个人密码门。
# 函数用途: 返回原私人控制的首设或闲置锁原因。
def private_interaction_lock_error(base_agent, actor_id: str) -> str:
    return private_interaction_lock_error_from_config(base_agent.home_paths.root,base_agent.config,actor_id)


# LLM: MAC/actor/expiry先于owner加载；签名群凭据及模型selection取消按当前成员核验，其余模型动作保留私人锁/原actor。
# 函数用途: 对一条已认证 SDK 控制事件核对身份、密码、执行和卡片，再写原批准记录。
# LLM: fields 仅在凭据/模型配置类型接受；模型控制走轻量原 owner store，不创建运行任务，原锁先于写账。
# 函数用途: 私人凭据 fields 只在 task_credential 接受，审批不收秘密；原锁和实际卡核验先于写账。 修改需同步原控制、恢复或权限测试。
# LLM: 默认原同步hook保持；仅真实HTTP显式approval_paths桥到同control receipt，模型/凭据原分支和G1/验签前缀不变。
# 函数用途: 处理原业务Agent的私卡回调。
def resolve_private_interaction(base_agent, body: dict, *, channel: str, actor_id: str, approval_paths=None) -> dict:
    payload, locked = validated_private_interaction(base_agent.home_paths.root, body,
        channel=channel, actor_id=actor_id,
        lock_error=lambda: private_interaction_lock_error(base_agent,actor_id),
        admin_identity=lambda: admin_channel_identity_for_request(base_agent,
            {"channel": channel,"user_id": actor_id,"metadata":{"channel":channel,"channel_chat_type":"p2p"}}))
    if locked is not None:
        return locked
    owner = OwnerIdentity(**payload['owner'])
    reference = body['reference']
    if payload["kind"] == "model_configuration":
        from ..capability.runtime_config_reload import capability_config_for_agent
        from .model_control_cards import model_control_host, submit_model_control
        capability = capability_config_for_agent(base_agent)
        if body["decision"]!="cancel" and capability is not None and not getattr(capability, "im_model_cards_enabled", True):
            raise ValueError("模型卡未启用")
        return submit_model_control(base_agent, model_control_host(base_agent, owner), owner, payload, body, actor_id=actor_id)
    agent = _resolve_request_agent_for_owner(base_agent, owner)
    if payload["kind"] == "feishu_oauth":
        from ..user_space.feishu_oauth import cancel_oauth_card
        return cancel_oauth_card(agent,payload,body,actor_id=actor_id)
    if payload["kind"] == "task_credential":
        from ..user_space.task_credentials import submit_credential_card
        return submit_credential_card(agent, payload, body)
    if "fields" in body:
        raise ValueError("审批不接收表单")
    if approval_paths is not None and body['decision'] in {'approved', 'denied'}:
        from ..conversation.tool_approval_scope import (
            ToolApprovalScope,
            tool_approval_scope_is_current,
        )
        from .private_approval_control import execute_count0_private_approval
        expected = ToolApprovalScope(**payload['scope'], agent_name='', active=False)
        if not tool_approval_scope_is_current(agent, expected):
            return execute_count0_private_approval(agent, approval_paths, payload, body, channel=channel, actor_id=actor_id)
    return resolve_im_tool_approval(agent, payload, reference=reference, message_id=str(body["message_id"]),
        chat_id=str(body["chat_id"]), decision=str(body["decision"]))


# LLM: 跨owner控制仍须实际宿主凭据；仅授权后parser的实际typed错误可返回form，原身份/期限/任务拒绝不降级，须同步adapter固定出口。
# 函数用途: 校验适配器来源后处理控制卡；原授权后的typed表单错误允许改正原卡，身份/版本和宿主故障仍沿原分类，不暴露输入。
# LLM: 原503捕获仅附安全诊断，诊断失败不影响原回执；不能据此推断未写入或重放决定。
def handle_private_interaction(handler, server) -> None:
    middleware = getattr(handler, "_auth_middleware", None)
    if middleware is None or not middleware.has_gateway_credential(dict(handler.headers)):
        handler._send_json(403, {"ok": False, "error_code": "LOCAL_CREDENTIAL_REQUIRED"})
        return
    try:
        if server is None:
            raise ValueError("服务不可用")
        channel = str(handler.headers.get("X-Channel") or "")
        actor_id = str(handler.headers.get("X-User-Id") or "")
        if not channel or not actor_id:
            raise ValueError("缺少已验证发送者")
        body = handler._read_json()
        control = getattr(server, 'control_plane', None)
        if control is not None and control.worker_count > 0:
            from .private_interaction_transaction import execute_coordinator_private_interaction
            result = execute_coordinator_private_interaction(handler,server,body,channel=channel,actor_id=actor_id)
        else:
            if server.agent is None:
                raise ValueError("服务不可用")
            result = resolve_private_interaction(server.agent,body,channel=channel,actor_id=actor_id,approval_paths=server.paths)
    except CredentialFormValidationError as error:
        handler._send_json(400, {'ok':False, 'error_code':error.code, 'error_stage':error.stage,
                                 'error_field':error.field, 'message':error.message})
        return
    except (OSError, RuntimeError, sqlite3.Error, GlobalWriteError) as error:
        _report_private_interaction_failure(error)
        handler._send_json(503, {"ok": False, "error_code": "IM_INTERACTION_HOST_UNAVAILABLE",
                                 "error_stage": "execution", "message": "宿主暂时无法处理卡片，请稍后重试。"})
        return
    except (TypeError, ValueError, KeyError):
        handler._send_json(403, {"ok": False, "error_code": "IM_INTERACTION_INVALID",
                                 "error_stage": "interaction", "message": "这张确认卡已失效，或不属于当前私人会话。"})
        return
    handler._send_json(503 if result.get("error_code")=="COORDINATOR_BUSINESS_NOT_READY" else 200, result)


# LLM: 新read_only模式不建目录/迁移，仍用原has_password/last_activity/status判据；坏库保持错误不当成已解锁。
# 函数用途: 在冷C只读原个人锁，Count0默认仍原初始化。
def private_interaction_lock_error_from_config(home_root, config, actor_id, *, read_only=False):
    if not config.feishu_session_lock_enabled:
        return ''
    path = Path(home_root)/'session_lock'/'private_chat_locks.db'
    if not path.is_file():
        return 'IM_PASSWORD_SETUP_REQUIRED'
    service = UnlockService(SessionLockStore(path,read_only=read_only),idle_limit_seconds=config.feishu_personal_idle_lock_seconds)
    status = service.status(actor_id)
    return 'IM_PASSWORD_SETUP_REQUIRED' if status.requires_password_setup else 'IM_UNLOCK_REQUIRED' if status.locked else ''


# LLM: 原MAC/SDK actor/群submitter/chat/个人锁/admin顺序保持；callbacks只由两个原host资源wrapper提供，不从JSON签权。
# 函数用途: 为原Agent和真实C解析同一原卡身份，返回原锁提示或验签结果。
def validated_private_interaction(home_root, body, *, channel, actor_id, lock_error, admin_identity):
    allowed = {"reference", "decision", "message_id", "chat_id", "fields", "submitter"}
    if not set(body) <= allowed or not {"reference", "decision", "message_id", "chat_id"} <= set(body):
        raise ValueError("交互字段无效")
    if "submitter" not in body and set(body) not in ({"reference", "decision", "message_id", "chat_id"}, {"reference", "decision", "message_id", "chat_id", "fields"}):
        raise ValueError("交互字段无效")
    reference = body["reference"]
    if not isinstance(reference, str):
        raise ValueError("交互引用无效")
    payload = verify_interaction_reference(Path(home_root), reference, channel=channel, actor_id=actor_id, submitter=body.get("submitter"), decision=str(body["decision"]))
    if payload.get("kind") not in {"tool_approval", "task_credential", "model_configuration", "feishu_oauth"}:
        raise ValueError("交互类型无效")
    from ..user_space.interaction_refs import interaction_presentation
    presentation = interaction_presentation(payload,str(body["decision"])) or {}
    group_input = presentation.get("kind") == "group"
    if group_input and body["chat_id"] != presentation["chat_id"]:
        raise ValueError("群卡来源不匹配")
    error = "" if group_input or body["decision"]=="cancel" else lock_error()
    if error:
        return payload, {"ok": False, "error_code": error, "message": "请先在机器人私聊设置或解锁个人密码，再确认原卡片。"}
    owner = OwnerIdentity(**payload["owner"])
    if owner == OwnerIdentity.local_main() and admin_identity() is None:
        raise ValueError("管理员绑定已变化")
    return payload, None


# LLM: 显式config来自原C组合根，回调只沿原只读锁及唯一admin读口；与Count0共用同一验签/字段/执行逻辑，不物化Agent。
# 函数用途: 在冷C的每次末核读取当前个人锁及管理员绑定事实。
def validated_private_interaction_from_config(home_root, config, body, *, channel, actor_id):
    from .request_worker import _admin_channel_identity_from_config
    return validated_private_interaction(home_root,body,channel=channel,actor_id=actor_id,
        lock_error=lambda: private_interaction_lock_error_from_config(home_root,config,actor_id,read_only=True),
        admin_identity=lambda: _admin_channel_identity_from_config(config,home_root,
            {"channel":channel,"user_id":actor_id,"metadata":{"channel":channel,"channel_chat_type":"p2p"}}))


# LLM: 仅复用原native/source诊断的固定stderr、有界标识和失败省略模式；不调用其会读args的分类器，不读message/locals/filename/HTTP数据。
# 函数用途: 在原503分支记录类型、固定机器码、整数errno及尾12函数行号；任何诊断故障均不影响原响应或已有提交。
def _report_private_interaction_failure(error):
    try:
        error_type = type.__getattribute__(type(error), '__name__')
        if type(error_type) is not str or re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,95}', error_type) is None:
            error_type = None
        code = None
        # 只接受宿主已知机器码；未知类的任意同格式字符串也不能进入日志。
        for base in type.__getattribute__(type(error), '__mro__'):
            values = type.__getattribute__(base, '__dict__')
            if 'error_code' in values:
                code = values['error_code']
                break
        if (type(code) is not str or re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', code) is None
                or code not in {'GLOBAL_WRITE_REJECTED', 'EXECUTION_CONTEXT_REQUIRED', 'ACTIVE_TURN_OUTCOME_UNCERTAIN'}):
            code = None
        number = OSError.errno.__get__(error) if isinstance(error, OSError) else None
        trace = BaseException.__traceback__.__get__(error)
        origins = []
        while trace is not None:
            name, line = trace.tb_frame.f_code.co_name, trace.tb_lineno
            if (type(name) is str and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,95}', name) is not None
                    and type(line) is int and 0 < line < 2**31):
                origins.append({'function': name, 'line': line})
                origins = origins[-12:]
            trace = trace.tb_next
        payload = {'schema_version': 'private_interaction_diagnostic.v1', 'error_type': error_type,
            'error_code': code, 'errno': number if type(number) is int else None, 'origin': origins}
        sys.stderr.write('[gateway-private-interaction-diagnostic] '
            + json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + '\n')
        sys.stderr.flush()
    except BaseException:
        pass


# LLM: 仅由宿主以既有owner资源及原已验证卡载荷调用；不认证插件、不接原始表单、不续租或解密。
# 函数用途: 只读原凭据、模型或OAuth卡，供插件安全待办投影使用。
def read_plugin_pending_input(home, inputs, payload, *, presentation_ref, body=None):
    import hashlib
    import time
    from dataclasses import asdict

    from ..common.json_io import read_json_object_report
    from ..plugin_host.errors import HostAPIError
    from ..plugin_host.schemas import canonical_json
    from ..user_space.feishu_oauth import oauth_path_for_home
    from ..user_space.task_credentials import credential_path
    from .model_control_cards import _record_path

    if payload.get("owner") != asdict(inputs.owner):
        raise HostAPIError("TARGET_DENIED", 403)
    kind = payload.get("kind")
    routes = {"task_credential": (credential_path, "credential_ref", "pending"),
              "model_configuration": (_record_path, "control_id", "active"),
              "feishu_oauth": (oauth_path_for_home, "actor_id", "pending")}
    if kind not in routes:
        raise HostAPIError("UNSUPPORTED_INTERACTION", 400)
    locate, key, status = routes[kind]
    try:
        report = read_json_object_report(locate(home, payload[key]), context="plugin.pending_input")
        if report.load_error:
            raise HostAPIError("STORE_UNAVAILABLE", 503)
        row = report.payload
        if not row:
            return None
        if body is not None:
            _check_plugin_input_card_identity(row, payload, body)
        identity = ("owner", "kind", "channel", "actor_id", "expires_at", key)
        if any(row.get(name) != payload.get(name) for name in identity):
            raise HostAPIError("INTERACTION_CONFLICT", 409)
        if row.get("status") != status or row["expires_at"] <= time.time():
            return None
        binding = _plugin_input_binding(inputs, row)
        if binding is None:
            return None
        thread_id, run_id, request_id, identity = binding
        identifier = "input-" + hashlib.sha256(canonical_json([row["owner"], kind, identity]).encode()).hexdigest()[:40]
        digest = hashlib.sha256(canonical_json(row).encode()).digest()
        return {"interaction_id": identifier, "request_id": request_id, "thread_id": thread_id, "run_id": run_id,
                "kind": kind, "revision": (int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)) or 1,
                "expires_at": row["expires_at"], "presentation_ref": presentation_ref}
    except HostAPIError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise HostAPIError("STORE_UNAVAILABLE", 503) from None


# LLM: 输入任务身份取原依赖，不从旧私卡猜当前root；模型卡无任务依赖，只读取其原线程。
# 函数用途: 核对待输入的原owner/任务/ref并提取非秘密身份。
def _plugin_input_binding(inputs, row):
    from ..plugin_host.errors import HostAPIError
    from ..user_space.owner_resolver import _owner_id

    if row["kind"] == "model_configuration":
        thread, error = inputs.store.threads.load_report(row["thread_id"])
        if error:
            raise HostAPIError("STORE_UNAVAILABLE", 503)
        if thread is None or thread.owner_id != _owner_id(inputs.owner):
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)
        return row["thread_id"], None, row["control_id"], row["control_id"]
    dependency = inputs.read(row["input_id"])
    credential = row["kind"] == "task_credential"
    provider = "credential" if credential else "feishu_oauth"
    reference = row["credential_ref"] if credential else row["input_id"]
    if dependency["provider"] != provider or dependency["reference"] != reference:
        raise HostAPIError("INTERACTION_CONFLICT", 409)
    if dependency["status"] != "pending" or not inputs.current(dependency):
        return None
    scope = dependency["scope"]
    if credential and any(scope.get(key) != value for key, value in row["scope"].items()):
        raise HostAPIError("INTERACTION_CONFLICT", 409)
    return scope["thread_id"], scope["run_id"], dependency["conversation_request_id"], row["input_id"]


# LLM: 插件票据仍需原MAC/SDK身份/个人锁及管理员绑定，不能直接把插件actor当真人；不提交表单。
# 函数用途: 用原私人卡校验器核对当前真人身份和原签名载荷。
def validate_plugin_input(context, body, identity):
    from dataclasses import asdict

    from ..plugin_host.errors import HostAPIError

    try:
        payload, locked = validated_private_interaction_from_config(context.base.home_paths.root,
            context.base.config, body, channel=identity["channel"], actor_id=identity["actor_id"])
        if locked is not None:
            raise HostAPIError(locked["error_code"], 403)
        if payload.get("owner") != asdict(context.inputs.owner) or payload["kind"] == "tool_approval":
            raise HostAPIError("TARGET_DENIED", 403)
        allowed = {"task_credential": {"submit", "cancel"},
                   "model_configuration": {"select", "set_default", "save", "cancel"}, "feishu_oauth": {"cancel"}}
        if body["decision"] not in allowed[payload["kind"]]:
            raise HostAPIError("UNSUPPORTED_INTERACTION", 400)
        return payload
    except HostAPIError:
        raise
    except (ValueError, TypeError, KeyError):
        raise HostAPIError("HUMAN_TICKET_INVALID", 403) from None


# LLM: 签票前同样核原展示卡身份；这只是只读防转发检查，提交仍必须重新进入原卡锁内CAS。
# 函数用途: 复用原凭据/模型身份规则并核OAuth原message/chat，不把签名有效等同原卡展示有效。
def _check_plugin_input_card_identity(row, payload, body):
    from ..plugin_host.errors import HostAPIError

    try:
        if payload["kind"] == "task_credential":
            from ..user_space.task_credential_replay import assert_credential_callback_identity
            assert_credential_callback_identity(row, payload, body)
        elif payload["kind"] == "model_configuration":
            from .model_control_cards import _require_model_control_record
            _require_model_control_record(row, payload, body)
        else:
            control = row.get("im_control") or {}
            if row.get("state") != body["reference"] or any(control.get(k) != body.get(k) for k in ("message_id", "chat_id")):
                raise ValueError("原卡展示不匹配")
    except (ValueError, KeyError, TypeError):
        raise HostAPIError("HUMAN_TICKET_INVALID", 403) from None


# LLM: 仅原资源叶子写入；callback由原卡锁调用，禁止在锁外预读冒充CAS，不模拟OAuth官方callback。
# 函数用途: 将已验证的真人决定送到原凭据、模型或OAuth取消事务。
def execute_plugin_input(context, payload, body, identity, record_guard):
    # 局部按需加载宿主错误码与交互操作者审计辅助函数。
    from ..plugin_host.errors import HostAPIError
    from ..user_space.interaction_actor_audit import (
        interaction_audit_host,
        record_interaction_actor,
    )

    latest = validate_plugin_input(context, body, identity)
    if latest != payload:
        raise HostAPIError("INTERACTION_CONFLICT", 409)
    home, inputs = context.home_paths.owner_home_dir, context.inputs
    actor = identity["actor_id"]

    # LLM: 原点击审计只能沿当前owner记录，返回不是业务成功证明。
    # 函数用途: 复用原取消审计，不把表单或签名写进日志。
    def audit(value):
        host = interaction_audit_host(context.base.home_paths.root, payload["owner"])
        return record_interaction_actor(host, payload, decision="cancel", actor=value, blocking=inputs.blocking)

    with plugin_input_record_check(record_guard):
        kind = payload["kind"]
        if kind == "task_credential":
            from ..user_space.task_credentials import submit_credential_card_for_owner
            return submit_credential_card_for_owner(home, context.capability, inputs, payload, body, audit=audit)
        if kind == "feishu_oauth":
            from ..user_space.feishu_oauth import cancel_oauth_card_for_owner
            return cancel_oauth_card_for_owner(home, inputs.owner, inputs, payload, body, actor_id=actor, audit=audit)
        if body["decision"] != "cancel" and not getattr(context.capability, "im_model_cards_enabled", False):
            raise HostAPIError("CAPABILITY_DENIED", 403)
        from .model_control_cards import model_control_host_from_resources, submit_model_control
        host = model_control_host_from_resources(context.base.config, context.home_paths, inputs.store)
        return submit_model_control(context.base, host, inputs.owner, payload, body, actor_id=actor,
                                    commit_transition=inputs.commit, blocking=inputs.blocking)
