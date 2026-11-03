"""原生管理员HTTP读写入口；沿原鉴权、全局writer门和耐久运维回执。"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from ..auth.middleware import require_admin_handler
from ..common.global_write_guard import GlobalWriteInWorkerError, require_global_writer
from ..conversation.business_access import BusinessError
from . import admin_api_modules as modules


# LLM: 不公开异常正文或宿主路径。
# 函数用途: 将领域错误映射为管理API失败响应。
def send_error(handler, code):
    status = {"INVALID_SCHEMA": 400, "RESOURCE_NOT_FOUND": 404, "CAPABILITY_DENIED": 403,
        "IDEMPOTENCY_CONFLICT": 409, "REVISION_CONFLICT": 409, "STATE_CONFLICT": 409}.get(code, 503)
    handler._send_json(status, {"ok": False, "error": {"code": code,
        "message": code, "retryable": status == 503}})


# LLM: 外层coverage和unavailable不能在投影时丢弃。
# 函数用途: 返回统一管理API成功信封。
def send_page(handler, page):
    handler._send_json(200, {"ok": True, "data": page["data"], "coverage": page["coverage"],
        "unavailable": page["unavailable"], "generated_at": modules.timestamp(time.time())})


# LLM: 查询参数闭合，重复值、空值和无时区时间不能降格成默认查询。
# 函数用途: 校验审计查询。
def audit_query(raw):
    query = parse_qs(raw, keep_blank_values=True, strict_parsing=True) if raw else {}
    if set(query) - {"cursor", "limit", "since", "actor"} or any(len(values) != 1 or not values[0] for values in query.values()):
        raise BusinessError("INVALID_SCHEMA")
    values = {key: rows[0] for key, rows in query.items()}
    if "limit" in values:
        if re.fullmatch(r"[0-9]+", values["limit"]) is None or not 1 <= int(values["limit"]) <= 100:
            raise BusinessError("INVALID_SCHEMA")
        values["limit"] = int(values["limit"])
    if len(values.get("actor", "")) > 128 or len(values.get("cursor", "")) > 4096:
        raise BusinessError("INVALID_SCHEMA")
    if "since" in values:
        try:
            if datetime.fromisoformat(values["since"].replace("Z", "+00:00")).tzinfo is None:
                raise ValueError("timezone")
        except ValueError:
            raise BusinessError("INVALID_SCHEMA") from None
    return values


# LLM: 每次访问都经过原require_admin_handler，拒绝响应保留原鉴权合同。
# 函数用途: 分派管理只读路由。
def handle_admin_api_get(handler, server):
    if require_admin_handler(handler):
        return
    try:
        url = urlsplit(handler.path)
        if url.path == "/api/admin/audit":
            page = modules.audit_page(server, handler=handler, **audit_query(url.query))
        else:
            if url.query:
                raise BusinessError("INVALID_SCHEMA")
            if url.path == "/api/admin/overview":
                page = overview(server, handler)
            elif url.path.startswith("/api/admin/modules/"):
                name = url.path.removeprefix("/api/admin/modules/")
                reader = modules.MODULE_READERS.get(name)
                if reader is None:
                    raise BusinessError("RESOURCE_NOT_FOUND")
                page = reader(server, handler=handler) if name in {"audit", "tools"} else reader(server)
            else:
                raise BusinessError("RESOURCE_NOT_FOUND")
        send_page(handler, page)
    except BusinessError as error:
        send_error(handler, error.code)
    except ValueError:
        send_error(handler, "INVALID_SCHEMA")
    except Exception:
        send_error(handler, "HOST_NOT_READY")


# LLM: 状态行固定覆盖所有模块，失源模块不省略、不补零。
# 函数用途: 聚合十模块概览。
def overview(server, handler):
    rows, unavailable, system = [], [], {}
    for name, reader in modules.MODULE_READERS.items():
        try:
            page = reader(server, handler=handler) if name in {"audit", "tools"} else reader(server)
        except Exception:
            page = {"data": {}, "coverage": "unknown", "unavailable": [{"module": name, "source": name, "reason_code": "HOST_NOT_READY"}]}
        missing = page["unavailable"]
        unavailable.extend(missing)
        counts = {} if missing else {key: len(value) for key, value in page["data"].items() if isinstance(value, list)}
        rows.append({"module": name, "status": "unavailable" if missing else "ready",
            "reason_code": "HOST_NOT_READY" if missing else None, "counts": counts})
        if name == "system":
            system = page["data"]
    return {"data": {"modules": rows, "system": system, "generated_at": modules.timestamp(time.time())},
        "coverage": "partial" if unavailable else "complete", "unavailable": unavailable}


# LLM: schema闭合校验在writer和所有副作用之前，owner/target不能作为文件路径。
# 函数用途: 校验三种管理动作请求体。
def operation_body(name, body):
    required = {"quota-reconcile": {"owner_id", "idempotency_key"},
        "channel-test": {"channel", "idempotency_key"}, "reclaim": {"kind", "target_id", "reason", "idempotency_key"}}
    if name not in required:
        raise BusinessError("RESOURCE_NOT_FOUND")
    optional = {"target"} if name == "channel-test" else set()
    if type(body) is not dict or not required[name] <= body.keys() or body.keys() - required[name] - optional:
        raise BusinessError("INVALID_SCHEMA")
    if any(type(value) is not str for value in body.values()):
        raise BusinessError("INVALID_SCHEMA")
    if not 1 <= len(body["idempotency_key"]) <= 128:
        raise BusinessError("INVALID_SCHEMA")
    if name == "quota-reconcile" and (not 1 <= len(body["owner_id"]) <= 128 or ".." in body["owner_id"]):
        raise BusinessError("INVALID_SCHEMA")
    if name == "channel-test" and (body["channel"] != "feishu" or "target" in body and not 1 <= len(body["target"]) <= 256):
        raise BusinessError("INVALID_SCHEMA")
    if name == "reclaim" and (body["kind"] not in {"turn", "subagent"} or not 1 <= len(body["target_id"]) <= 128
                              or len(body["reason"]) > 500):
        raise BusinessError("INVALID_SCHEMA")
    if name == "reclaim" and body["kind"] == "turn" and re.fullmatch(r"[A-Za-z0-9_-]+", body["target_id"]) is None:
        raise BusinessError("INVALID_SCHEMA")
    return body


# LLM: 实时鉴权只返回布尔，不二次发响应；用于耐久事务提交前和副作用前末核。
# 函数用途: 复查原HTTP管理员身份。
def still_admin(handler):
    middleware = getattr(handler, "_auth_middleware", None)
    if middleware is None:
        return True
    return middleware.require_admin(handler.headers, handler.client_address[0])[0]


# LLM: 公共send调用前先验证适配器与明确目标，不能隐式向任意联系人发消息。
# 函数用途: 解析飞书测试目标。
def channel_target(server, handler, body):
    from ..auth.middleware import extract_user_from_request
    try:
        adapter = modules.feishu_adapter(server)
    except AttributeError:
        adapter = None
    if adapter is None or not callable(getattr(adapter, "send_message", None)):
        raise BusinessError("HOST_NOT_READY")
    target = body.get("target")
    if target is None:
        user, channel = extract_user_from_request(handler)
        if channel != "feishu":
            raise BusinessError("HOST_NOT_READY")
        target = user
    return adapter, target


# LLM: POST保留原鉴权；writer门位于包括运维回执在内的全部写副作用之前。
# 函数用途: 分派受控运维写路由。
def handle_admin_api_post(handler, server):
    if require_admin_handler(handler):
        return
    try:
        url = urlsplit(handler.path)
        if url.query or not url.path.startswith("/api/admin/ops/"):
            raise BusinessError("RESOURCE_NOT_FOUND")
        body = handler._read_json()
        if body is None:
            return
        name = url.path.removeprefix("/api/admin/ops/")
        operation_body(name, body)
        require_global_writer("admin_ops")
        result = run_operation(handler, server, name, body)
        send_page(handler, {"data": result, "coverage": "complete", "unavailable": []})
    except GlobalWriteInWorkerError:
        send_error(handler, "HOST_NOT_READY")
    except BusinessError as error:
        send_error(handler, error.code)
    except Exception:
        send_error(handler, "OUTCOME_UNKNOWN")


# LLM: 先耐久记录可能进入副作用，再执行一次；崩溃/并发重投不能重做外发或回收。
# 函数用途: 在main admin_ops维护幂等运维操作。
def run_operation(handler, server, name, body):
    from ..auth.middleware import extract_user_from_request
    from ..business_spaces.v13_store import V13Store
    subject, _channel = extract_user_from_request(handler)
    home = modules.main_home_paths(server)
    store = V13Store(home.owner_home_dir, owner_id="local/main", subject_id=subject, action="admin." + name,
                     authorize=lambda: still_admin(handler))
    store._scope = "main"
    params = {key: value for key, value in body.items() if key != "idempotency_key"}
    digest = hashlib.sha256(json.dumps(params, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    entered = []
    # LLM: mutate在存储锁内只创建记录，不在配额锁内执行外部副作用。
    # 函数用途: 原子预留本次操作身份并隔离同键并发。
    def reserve(records):
        op_id = "admin-" + uuid4().hex
        at = modules.timestamp(time.time())
        records[op_id] = {"op_id": op_id, "owner_id": body.get("owner_id", "local/main"),
            "op": name.replace("-", "_"), "subject_id": subject, "params_digest": digest,
            "outcome": {"state": "unknown"}, "revision": 1, "created_at": at, "updated_at": at}
        entered.append(op_id)
        return {"op_id": op_id}
    receipt = store.transaction("admin_ops", reserve, idempotency_key=body["idempotency_key"], params_digest=digest)
    op_id = receipt["op_id"]
    current = store.get("admin_ops", op_id)
    if not entered:
        if current["outcome"] == {"state": "unknown"}:
            raise BusinessError("OUTCOME_UNKNOWN")
        if "_error" in current["outcome"]:
            raise BusinessError(current["outcome"]["_error"])
        return current["outcome"]
    try:
        if not still_admin(handler):
            raise BusinessError("CAPABILITY_DENIED")
        require_global_writer("admin_ops")
        result = execute_operation(handler, server, name, body, op_id)
    except BusinessError as error:
        result = {"_error": error.code}
    except Exception:
        result = {"_error": "OUTCOME_UNKNOWN"}
    # LLM: 结果落在同一操作记录；失败结果也保存，不允许同键重复副作用。
    # 函数用途: 保存真实运维结果及终态回执。
    def finish(records):
        row = records[op_id]
        row.update(outcome=result, revision=row["revision"] + 1, updated_at=modules.timestamp(time.time()))
        return result
    store.transaction("admin_ops", finish, idempotency_key=op_id, params_digest=digest)
    if "_error" in result:
        raise BusinessError(result["_error"])
    return result


# LLM: 配额沿原有界reconcile默认预算，外发只调用已绑定适配器公共send_message。
# 函数用途: 执行经过鉴权和耐久预留的运维动作。
def execute_operation(handler, server, name, body, op_id):
    if name == "quota-reconcile":
        from ..user_space.owner_quota import (
            owner_quota_reconcile_pending,
            reconcile_owner_quota_slice,
        )
        homes = dict(modules.owner_homes(server))
        home = homes.get(body["owner_id"])
        if home is None or not home.is_dir():
            raise BusinessError("RESOURCE_NOT_FOUND")
        stats = reconcile_owner_quota_slice(home)
        pending = owner_quota_reconcile_pending(home)
        return {"op_id": op_id, "owner_id": body["owner_id"], "reconciled": not pending,
                "reconcile_pending_after": pending, "stats": stats}
    if name == "channel-test":
        from ..adapter.protocol import OutgoingMessage
        adapter, target = channel_target(server, handler, body)
        message = OutgoingMessage(channel="feishu", user_id=target, content="【管理员通道测试】这是一条由管理员明确触发的连通性测试消息。",
            metadata={"delivery_idempotency_key": op_id})
        try:
            delivered = adapter.send_message(target, message)
        except Exception:
            delivered = False
        return {"op_id": op_id, "channel": "feishu", "delivered": delivered is True,
                "message_ref": None, "error_code": None if delivered is True else "CHANNEL_DELIVERY_FAILED"}
    return reclaim(handler, server, body, op_id)


# LLM: 资格来自读口同一当前attempt快照，取消API自带身份CAS，不扫描或导入subagents。
# 函数用途: 回收被证实执行器已退出的规范子代理。
def reclaim(handler, server, body, op_id):
    from ..runtime_db.run_cancellation import (
        RuntimeCancellationConflict,
        RuntimeCancellationTarget,
        cancel_runtime_run,
    )
    result = {"op_id": op_id, "kind": body["kind"], "target_id": body["target_id"],
              "outcome": "not_stuck", "effect_outcome": "not_started"}
    if body["kind"] == "turn":
        return reclaim_turn(handler, server, body, result)
    rows = modules.runtime_run_rows(server)
    selected = [row for row in rows if row["run_id"] == body["target_id"]]
    if not selected:
        result["outcome"] = "not_found"
        return result
    row = selected[0]
    if not row["reason"]:
        return result
    repo = modules.runtime_repository(modules.main_home_paths(server).owner_home_dir, read_only=False)
    target = RuntimeCancellationTarget(task_id=row["task_id"], run_id=row["run_id"],
        agent_run_id=row["agent_run_id"], attempt_id=row["current_attempt_id"])
    try:
        cancelled = cancel_runtime_run(repo, target, reason=body["reason"], source="admin_api")
        result.update(outcome="reclaimed" if cancelled.get("authority_closed") else "unknown",
                      effect_outcome="committed" if cancelled.get("authority_closed") else "unknown")
    except RuntimeCancellationConflict:
        result.update(outcome="unknown", effect_outcome="unknown")
    return result


# LLM: 原生claim与transport attempt共同证明运行中；沿原scope和精确目标CAS，不把processing文件当执行权。
# 函数用途: 通过原前台请求停止链回收当前运行回合。
def reclaim_turn(handler, server, body, result):
    from dataclasses import replace

    from ..conversation.agent_control import AgentControlError
    from .control_service import (
        _GatewayRequestRecord,
        _request_matches_scope,
        _stop_live_window_request,
    )
    from .execution_target import read_gateway_execution_target
    from .http_handlers import _agent_control_context
    from .io import read_json_file_report
    target_id = body["target_id"]
    if re.fullmatch(r"[A-Za-z0-9_-]+", target_id) is None:
        raise BusinessError("INVALID_SCHEMA")
    path = server.paths.processing / (target_id + ".json")
    if not path.is_file():
        result["outcome"] = "not_found"
        return result
    report = read_json_file_report(path)
    if report.load_error is not None or not report.payload:
        result.update(outcome="unknown", effect_outcome="unknown")
        return result
    record = report.payload
    if (record.get("id", record.get("request_id")) != target_id or record.get("status") != "processing"
            or record.get("turn_phase") != "open"):
        return result
    try:
        conversation = record.get("conversation") or {}
        agent, scope = _agent_control_context(handler, server, {
            "conversation_id": conversation.get("channel_conversation_id", "")})
        if not _request_matches_scope(record, scope):
            return result
        store = agent.conversation_store
        thread = store.threads.resolve(channel=scope.channel, channel_conversation_id=scope.conversation_id,
                                       channel_user_id=scope.user_id)
        target = read_gateway_execution_target(server.paths, store, thread.thread_id) if thread else None
        if target is None or target.kind != "request" or target.turn_id != target_id:
            return result
        scope = replace(scope, metadata={**scope.metadata, "expected_execution": target.to_dict(),
                                        "message_id": result["op_id"]})
        stopped = _stop_live_window_request(agent, server.paths, _GatewayRequestRecord(path, record), scope)
        if stopped.ok:
            result.update(outcome="reclaimed", effect_outcome="committed")
        elif stopped.delivery_status == "unknown":
            result.update(outcome="unknown", effect_outcome="unknown")
    except AgentControlError:
        result.update(outcome="unknown", effect_outcome="unknown")
    return result
