"""数据源原生桥接与 W9 共用的 owner、事务、分页和只读查询边界。"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import ExitStack, closing
from dataclasses import asdict
from datetime import datetime, timezone
from urllib.parse import unquote

from ..assets.datasource_store import DatasourceStore, open_sqlite
from ..plugin_host.errors import HostAPIError


# LLM: 时间只取本次调用注入的时钟，不创建全局状态。
# 函数用途: 把当前业务时间转成带时区的字符串。
def w9_now(call):
    return datetime.fromtimestamp(call.clock(), timezone.utc).isoformat()


# LLM: 每个调用沿 W0 的 scope 检查获取原生存储，不恢复展示身份。
# 函数用途: 取得当前 owner 的 v13 存储。
def _w9_store(call):
    from .v13_store import v13_store_for
    return v13_store_for(call, "owner")


# LLM: 读取后核 owner，不把其它 owner 的存在情况公开。
# 函数用途: 读取本 owner 的一条记录。
def w9_get(call, collection, record_id):
    record = _w9_store(call).get(collection, record_id)
    if record is not None:
        if record.get("owner_id", call.owner_id) != call.owner_id:
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)
        call.context.check(call.action, {"owner_id": call.owner_id, "record_id": record_id})
    return record


# LLM: 原存储只读列表不制造新记录，逐行保留 owner 授权。
# 函数用途: 列出当前 owner 的集合内容。
def w9_list(call, collection):
    records = _w9_store(call).list(collection)
    return [row for row in records if row.get("owner_id", call.owner_id) == call.owner_id]


# LLM: 幂等指纹只存摘要；真实副作用必须在锁内 mutate 中执行，秘密收据由 receipt_view 裁剪。
# 函数用途: 在 W0 原事务内写入并保留现场实时授权。
def w9_write(call, collection, mutate, receipt_view=None):
    digest = hashlib.sha256(json.dumps({"action": call.action, "params": call.params,
        "expected_revision": call.expected_revision}, sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode()).hexdigest()

    # LLM: 真正执行 mutate 前复核活跃上下文；重放由原生存储负责。
    # 函数用途: 为锁内写操作加上本次业务的实时权限检查。
    def checked(records):
        call.context.check(call.action, {"owner_id": call.owner_id})
        return mutate(records)

    return _w9_store(call).transaction(collection, checked,
        idempotency_key=call.idempotency_key, params_digest=digest, receipt_view=receipt_view)


# LLM: 分页交给原有 HMAC 游标实现，绑定实际查询及内容，不自行管理密钥。
# 函数用途: 对公开投影分页并报告覆盖范围。
def w9_page(call, collection, items):
    revision = hashlib.sha256(json.dumps([str(call.owner_home), collection, items],
        sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return call.services.spaces.page(call.action, call.params, items, {"revision": revision},
        actor=call.services.actor(call.context))


# LLM: 空间来源由 W0 决定，调用者显式给定空间仍须原实时门验证。
# 函数用途: 获取并核验本次调用的空间。
def w9_require_space(call, space_id=None):
    from ..gateway_parts.business_api_v13 import v13_call_frozen, v13_resolve_space
    if space_id is None:
        page = v13_call_frozen(call, "spaces.list", {"state": "active", "limit": 2})
        items = page["items"]
        if len(items) != 1 or page.get("next_cursor"):
            raise HostAPIError("INVALID_SCHEMA", 400)
        space_id = items[0]["space_id"]
    return v13_resolve_space(call, space_id)["space_id"]


# LLM: workspace 只取受信宿主的当前工作区，绝不从 JSON 路径恢复。
# 函数用途: 获取原生数据源工作区覆盖根。
def _workspace(call):
    return getattr(call.context.host, "workspace", None)


# LLM: 每次绑定当前 owner home，不重用其它身份的全局数据源句柄。
# 函数用途: 构造原生数据源存储。
def w9_datasource_store(call):
    return DatasourceStore(call.owner_home)


# LLM: 墓碑与不存在统一 404；规格仍使用原生 nofollow 路径及 owner Vault。
# 函数用途: 获取未删除数据源的原始规格字典。
def w9_datasource(call, datasource_id):
    if w9_get(call, "datasource_tombstones", datasource_id) is not None:
        raise HostAPIError("RESOURCE_NOT_FOUND", 404)
    call.context.check(call.action, {"owner_id": call.owner_id, "datasource_id": datasource_id})
    try:
        return asdict(w9_datasource_store(call).get_datasource_spec(datasource_id,
            workspace_root=_workspace(call)))
    except KeyError:
        raise HostAPIError("RESOURCE_NOT_FOUND", 404) from None


# LLM: 仅返回合同声明字段；不返回 DSN、内部路径、credential 值或扩展 options。
# 函数用途: 把原生安全摘要投影成前端数据源视图。
def _view(call, summary, health=None, deleted=False):
    health = health or w9_get(call, "datasource_health", summary["datasource_id"]) or {}
    return {"datasource_id": summary["datasource_id"], "name": summary["title"],
        "kind": summary["kind"], "scope": summary["scope"], "queryable": summary["kind"] == "sqlite",
        "credential_ref": summary.get("credential_ref") or None, "has_secret": summary["has_secret"],
        "credential_keys": list(summary["credential_keys"]), "allowed_tables": list(summary["allowed_tables"]),
        "poll_interval_sec": health.get("poll_interval_sec"), "revision": summary["revision"], "deleted": deleted}


# LLM: 显式空间只用于权限，不从请求改变 owner；已删除规格默认隐藏。
# 函数用途: 列出可见且脱敏的数据源。
def list_datasources(call):
    if call.params.get("space_id"):
        w9_require_space(call, call.params["space_id"])
    rows = []
    tombstones = {row["datasource_id"] for row in w9_list(call, "datasource_tombstones")}
    for summary in w9_datasource_store(call).list_datasources(workspace_root=_workspace(call)):
        identifier = summary["datasource_id"]
        deleted = identifier in tombstones
        if deleted and not call.params.get("include_deleted", False):
            continue
        call.context.check(call.action, {"owner_id": call.owner_id, "datasource_id": identifier})
        row = _view(call, summary, deleted=deleted)
        if "kind" in call.params and row["kind"] != call.params["kind"]:
            continue
        if "queryable" in call.params and row["queryable"] != call.params["queryable"]:
            continue
        rows.append(row)
    return w9_page(call, "datasource_health", rows)


# LLM: 健康记录不保存凭据或 DSN，版本来自原生规格并在事务锁内更新。
# 函数用途: 创建或更新一个数据源的健康元数据。
def _health(call, identifier, revision, previous=None):
    previous = previous or {}
    return {"datasource_id": identifier, "owner_id": call.owner_id,
        "poll_interval_sec": call.params.get("poll_interval_sec", previous.get("poll_interval_sec")),
        "last_test": previous.get("last_test"), "revision": revision,
        "created_at": previous.get("created_at", w9_now(call)), "updated_at": w9_now(call)}


# LLM: 公开键清单边界先于 Vault/规格副作用，拒绝而不是截断秘密元数据。
# 函数用途: 提前检查凭据清单的数量和键名长度。
def _admit_credentials(values):
    if values is not None and (len(values) > 50 or any(not 1 <= len(key) <= 128 for key in values)):
        raise HostAPIError("INVALID_SCHEMA", 400)


# LLM: URI 映射到原 database_path，原 Store 继续执行 owner/nofollow 校验。
# 函数用途: 解析 SQLite DSN，避免静默查询错误的默认库。
def _sqlite_path(value):
    if not value:
        return ""
    if not value.startswith("sqlite:///") or "?" in value or "#" in value:
        raise HostAPIError("INVALID_SCHEMA", 400)
    path = unquote(value[len("sqlite:///"):])
    if not path or "\x00" in path:
        raise HostAPIError("INVALID_SCHEMA", 400)
    return path


# LLM: 原生创建及 Vault 写在幂等事务回调内执行，原 CAS=0 防止覆盖旧规格。
# 函数用途: 创建一个数据源并将凭据送入原生加密存储。
def create_datasource(call):
    if call.params.get("space_id"):
        w9_require_space(call, call.params["space_id"])
    p = call.params
    identifier = p["datasource_id"]

    # LLM: 存在检查和 native CAS 均在本次幂等写回调内，不因重放再次写 Vault。
    # 函数用途: 保存规格并登记初始健康行。
    def mutate(records):
        _admit_credentials(p.get("credentials"))
        store = w9_datasource_store(call)
        try:
            store.get_datasource_spec(identifier, workspace_root=_workspace(call))
        except KeyError:
            pass
        else:
            raise HostAPIError("STATE_CONFLICT", 409)
        spec = {"datasource_id": identifier, "title": p["name"], "kind": p["kind"],
            "dsn_template": p.get("dsn_template", ""), "allowed_tables": p.get("allowed_tables", []),
            "read_only": True}
        if p["kind"] == "sqlite" and p.get("dsn_template"):
            spec["database_path"] = _sqlite_path(p["dsn_template"])
        try:
            summary = store.save_datasource(spec, credential_values=p.get("credentials"),
                scope=p.get("scope", "owner"), workspace_root=_workspace(call), expected_revision=0)
        except ValueError as error:
            if str(error) == "DATASOURCE_REVISION_CONFLICT":
                raise HostAPIError("STATE_CONFLICT", 409) from None
            raise
        row = _health(call, identifier, summary["revision"])
        records[identifier] = row
        return {"datasource": _view(call, summary, row)}

    return w9_write(call, "datasource_health", mutate)


# LLM: 原生规格版本是 CAS 权威；只修改合同允许字段，凭据更新仍走原 Vault。
# 函数用途: 按版本更新数据源。
def update_datasource(call):
    p = call.params
    _admit_credentials(p.get("credentials"))
    identifier = p["datasource_id"]

    # LLM: 获取当前规格后以原生锁内 CAS 发表，锁外版本只作请求前置检查。
    # 函数用途: 更新规格和公开健康元数据。
    def mutate(records):
        spec = w9_datasource(call, identifier)
        if call.expected_revision != spec["revision"]:
            raise HostAPIError("REVISION_CONFLICT", 409)
        for source, target in (("name", "title"), ("dsn_template", "dsn_template"), ("allowed_tables", "allowed_tables")):
            if source in p:
                spec[target] = p[source]
        if spec["kind"] == "sqlite" and "dsn_template" in p:
            spec["database_path"] = _sqlite_path(p["dsn_template"])
        try:
            summary = w9_datasource_store(call).save_datasource(spec,
                credential_values=p.get("credentials"), scope=spec["scope"], workspace_root=_workspace(call),
                expected_revision=call.expected_revision)
        except ValueError as error:
            if str(error) == "DATASOURCE_REVISION_CONFLICT":
                raise HostAPIError("REVISION_CONFLICT", 409) from None
            raise
        row = _health(call, identifier, summary["revision"], records.get(identifier))
        records[identifier] = row
        return {"datasource": _view(call, summary, row)}

    return w9_write(call, "datasource_health", mutate)


# LLM: 只扫描当前 owner 原生脚本元数据；不执行脚本或读取源码。
# 函数用途: 查出仍引用给定数据源的脚本。
def _scripts(call):
    from ..assets.script_store import ScriptStore
    return ScriptStore(call.owner_home, workspace_root=_workspace(call)).list_scripts()


# LLM: 引用可为原生字符串或结构化 datasource_id，不解析自然语言描述。
# 函数用途: 判断脚本是否声明读取该数据源。
def _script_uses(script, identifier):
    sources = script.get("data_sources", [])
    if isinstance(sources, dict):
        sources = list(sources.values())
    return any(source == identifier or (isinstance(source, dict)
        and (source.get("datasource_id") == identifier or source.get("source_id") == identifier)) for source in sources)


# LLM: 绑定校验只读原 token 账，不返回摘要以外的 verifier。
# 函数用途: 检查是否仍有有效摄入令牌引用数据源。
def _token_bound(call, identifier):
    from ..gateway_parts.remote_push_service import read_push_json
    root = call.context.host.home_paths.root / "remote_push"
    return any(row.get("datasource_id") == identifier
        and row.get("allowed_target_owners") == [call.owner_id] and row.get("enabled") is True
        and (row.get("expires_at") is None or row["expires_at"] > call.clock())
        for row in read_push_json(root, "tokens.json").get("tokens", []))


# LLM: 原生无硬删除接口；只登记墓碑，保留原规格及 Vault，受引用约束和 CAS 控制。
# 函数用途: 软删除一个未被脚本或令牌绑定的数据源。
def delete_datasource(call):
    identifier = call.params["datasource_id"]

    # LLM: 在墓碑事务内重核当前规格和引用，幂等重放不重复删除。
    # 函数用途: 按当前版本写入数据源墓碑。
    def mutate(records):
        spec = w9_datasource(call, identifier)
        if spec["revision"] != call.expected_revision:
            raise HostAPIError("REVISION_CONFLICT", 409)
        if _token_bound(call, identifier) or any(_script_uses(row, identifier) for row in _scripts(call)):
            raise HostAPIError("STATE_CONFLICT", 409)
        records[identifier] = {"datasource_id": identifier, "owner_id": call.owner_id,
            "deleted_by": call.subject_id, "revision": 1, "created_at": w9_now(call), "updated_at": w9_now(call)}
        return {"datasource_id": identifier, "deleted": True, "mode": "tombstone"}

    return w9_write(call, "datasource_tombstones", mutate)


# LLM: 原生探测失败属于已完成观察，返回 ok=false；结果只保存稳定码、不保存异常正文。
# 函数用途: 测试数据源并登记最近一次探测结果。
def test_datasource(call):
    identifier = call.params["datasource_id"]

    # LLM: 原生探测放在幂等回调内，重放不会重复发出连接。
    # 函数用途: 保存探测状态并构造前端观察结果。
    def mutate(records):
        spec = w9_datasource(call, identifier)
        result = w9_datasource_store(call).test_datasource(identifier, workspace_root=_workspace(call))
        tested = w9_now(call)
        last = {"ok": result["ok"], "error_code": result.get("error_code") or None,
            "latency_ms": result["latency_ms"], "tested_at": tested}
        previous = records.get(identifier, {})
        row = _health(call, identifier, max(spec["revision"], previous.get("revision", 0)) + 1, previous)
        row["last_test"] = last
        records[identifier] = row
        details = result.get("details", {})
        return {**last, "datasource_id": identifier, "kind": spec["kind"],
            "observation": details.get("observation") or None, "connected": details.get("connected")}

    return w9_write(call, "datasource_health", mutate)


# LLM: 只解码规范时间戳或已存 epoch 数值；错误不伪造成当前时刻。
# 函数用途: 将已验证来源的时间换成秒。
def _epoch(value):
    if type(value) in (int, float):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except ValueError:
        return None


# LLM: 逐事件重核完整性；合法多owner旧token仅标未验证，不恢复W9空间，不阻断其它来源。
# 函数用途: 获取当前owner有可信来源关联的耐久推送，缺历史输入显式标为未验证。
def w9_push_entries(call):
    from ..gateway_parts.input_delivery_service import read_gateway_input_receipt
    from ..gateway_parts.remote_push_service import read_push_json
    root = call.context.host.home_paths.root / "remote_push"
    entries = read_push_json(root, "receipts.json").get("entries", {})
    tokens = {row["token_id"]: row for row in read_push_json(root, "tokens.json").get("tokens", [])}
    paths = getattr(call.context.host, "business_v13_gateway_paths", None)
    results = []
    for entry in entries.values():
        receipt = entry.get("receipt", {})
        if entry.get("accepted") is not True or receipt.get("target_owner") != call.owner_id:
            continue
        result = {**entry, "source_verified": False}
        request_id = receipt.get("run_id", "")
        if paths is None or not re.fullmatch(r"req_remote_[0-9a-f]{64}", request_id):
            results.append(result)
            continue
        source = read_gateway_input_receipt(paths, request_id, blocking=False)
        if source is None:
            results.append(result)
            continue
        request = source.prepared_request
        metadata, conversation = request.get("metadata", {}), request.get("conversation", {})
        user = request.get("user_id", "")
        token_id = user.removeprefix("remote-push:") if isinstance(user, str) else ""
        token = tokens.get(token_id)
        if (source.request_id != request_id or request.get("id") != request_id
                or receipt.get("push_id") != "push_" + request_id.removeprefix("req_remote_")
                or request.get("owner_id") != call.owner_id
                or request.get("source") != "remote_push" or request.get("input_origin") != "remote_push"
                or metadata.get("remote_push_id") != receipt.get("push_id")
                or metadata.get("client_input_digest") != entry.get("digest")
                or conversation.get("thread_id") != receipt.get("thread_id")
                or conversation.get("channel") != "remote_push"
                or conversation.get("channel_user_id") != user
                or not user.startswith("remote-push:") or token is None
                or not isinstance(token.get("allowed_target_owners"), list)
                or call.owner_id not in token["allowed_target_owners"]):
            raise HostAPIError("HOST_NOT_READY", 503)
        if token["allowed_target_owners"] != [call.owner_id]:
            results.append(result)
            continue
        result.update(token_id=token_id, space_id=token.get("space_id"),
            prompt=request.get("prompt", ""), source_verified=True)
        results.append(result)
    return results


# LLM: 事件只来自同 owner 已受理 push 或成功脚本运行；无事实时保留 unknown。
# 函数用途: 计算连接器群组的延迟和健康状态。
def fleet_health(call):
    if call.params.get("space_id"):
        w9_require_space(call, call.params["space_id"])
    tombstones = {row["datasource_id"] for row in w9_list(call, "datasource_tombstones")}
    summaries = w9_datasource_store(call).list_datasources(workspace_root=_workspace(call))
    identifier = call.params.get("datasource_id")
    if identifier:
        w9_datasource(call, identifier)
        summaries = [row for row in summaries if row["datasource_id"] == identifier]
    from ..gateway_parts.remote_push_service import read_push_json
    token_rows = read_push_json(call.context.host.home_paths.root / "remote_push", "tokens.json").get("tokens", [])
    bindings = {row["token_id"]: row.get("datasource_id") for row in token_rows
        if row.get("allowed_target_owners") == [call.owner_id]}
    events = {}
    pushes = w9_push_entries(call)
    for entry in pushes:
        key = bindings.get(entry.get("token_id"))
        stamp = _epoch(entry.get("receipt", {}).get("queued_at"))
        if key and stamp is not None:
            events[key] = max(events.get(key, stamp), stamp)
    scripts = _scripts(call)
    items = []
    for summary in summaries:
        key = summary["datasource_id"]
        if key in tombstones:
            continue
        call.context.check(call.action, {"owner_id": call.owner_id, "datasource_id": key})
        for script in scripts:
            runtime = script.get("runtime_summary", {}) or {}
            if runtime.get("last_status") == "success" and _script_uses(script, key):
                stamp = _epoch(runtime.get("last_run_at"))
                if stamp is not None:
                    events[key] = max(events.get(key, stamp), stamp)
        row = w9_get(call, "datasource_health", key) or {}
        last = row.get("last_test")
        stamp = events.get(key)
        poll = row.get("poll_interval_sec")
        lag = max(0.0, call.clock() - stamp) if stamp is not None else None
        status = "unknown"
        if last is not None and last["ok"] is False:
            status = "down"
        elif lag is not None and poll is not None:
            status = "healthy" if lag <= 2 * poll else "lagging"
        items.append({"datasource_id": key, "name": summary["title"], "kind": summary["kind"],
            "status": status, "last_event_at": datetime.fromtimestamp(stamp, timezone.utc).isoformat() if stamp is not None else None,
            "poll_interval_sec": poll, "lag_sec": lag, "last_test": last})
    return {"items": items[:500], "generated_at": w9_now(call),
        "coverage": "partial" if len(items) > 500 or any(not row.get("source_verified") for row in pushes) else "complete"}


# LLM: 词法检查保留引号和注释语义，拒绝多语句和写语句；SQLite authorizer 仍是执行时权限权威。
# 函数用途: 检查查询必须是单条 SELECT 或 WITH SELECT。
def w9_check_query(statement):
    if not isinstance(statement, str) or not statement.strip():
        raise HostAPIError("QUERY_REJECTED", 400)
    tokens = re.findall(r"--[^\n]*(?:\n|$)|/\*[\s\S]*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]|[A-Za-z_][A-Za-z_0-9]*|;|[^\s]", statement)
    significant = [token for token in tokens if not token.startswith(("--", "/*"))]
    if significant and significant[-1] == ";":
        significant.pop()
    words = [token.upper() for token in significant if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", token)]
    if (not significant or ";" in significant or not words or words[0] not in ("SELECT", "WITH")
            or "SELECT" not in words or any(word in {
                "INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER", "ATTACH", "DETACH",
                "PRAGMA", "VACUUM", "REINDEX", "ANALYZE", "BEGIN", "COMMIT", "ROLLBACK"
            } for word in words)):
        raise HostAPIError("QUERY_REJECTED", 400)


# LLM: 默认拒绝非 SELECT/READ/FUNCTION 指令；表范围取原生 allowed_tables，函数禁文件及扩展装载。
# 函数用途: 为 SQLite 连接构造不可写的授权器。
def _authorizer(allowed_tables):
    allowed = {name.casefold() for name in allowed_tables}

    # LLM: 表名与操作码来自 SQLite，不解析错误字符串或用户提示词决定权限。
    # 函数用途: 拒绝越表、扩展装载和所有写操作。
    def authorize(operation, first, second, database, origin):
        if operation in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
            return sqlite3.SQLITE_OK
        if (operation == sqlite3.SQLITE_READ and database in ("main", None)
                and not first.casefold().startswith("sqlite_") and (not allowed or first.casefold() in allowed)):
            return sqlite3.SQLITE_OK
        if operation == sqlite3.SQLITE_FUNCTION and (second or "").casefold() not in {
                "load_extension", "readfile", "writefile", "fts3_tokenizer"}:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    return authorize


# LLM: 行只含 JSON 标量；SQLite 的 BLOB 用十六进制展示，不把 Python bytes 带出合同。
# 函数用途: 把 SQLite 行转换为 JSON 安全形式。
def _query_row(row):
    return [value.hex() if isinstance(value, bytes) else value for value in row]


# LLM: 全部连接先只读准备成功再取结果，authorizer 覆盖真实表访问；断开自动收口，不执行多语句。
# 函数用途: 在本 owner 的受限数据源上运行联邦只读查询。
def w9_query(call, statement, datasource_ids=None, limit=100):
    w9_check_query(statement)
    if call.params.get("space_id"):
        w9_require_space(call, call.params["space_id"])
    if datasource_ids is None:
        tombstones = {row["datasource_id"] for row in w9_list(call, "datasource_tombstones")}
        datasource_ids = [row["datasource_id"] for row in w9_datasource_store(call).list_datasources(
            workspace_root=_workspace(call)) if row["datasource_id"] not in tombstones]
    identifiers = list(dict.fromkeys(datasource_ids))
    if len(identifiers) > 20 or not 1 <= limit <= 1000:
        raise HostAPIError("LIMIT_EXCEEDED", 400)
    results = []
    with ExitStack() as stack:
        prepared = []
        for identifier in identifiers:
            from ..assets.datasource_store import DatasourceConnectionSpec
            spec = DatasourceConnectionSpec(**w9_datasource(call, identifier))
            started = time.monotonic()
            if spec.kind != "sqlite":
                results.append({"datasource_id": identifier, "ok": False, "columns": [], "rows": [],
                    "truncated": False, "error_code": "DATASOURCE_DRIVER_UNSUPPORTED", "latency_ms": 0.0})
                continue
            try:
                conn = stack.enter_context(closing(open_sqlite(spec, read_only=True)))
            except (OSError, ValueError, sqlite3.Error):
                results.append({"datasource_id": identifier, "ok": False, "columns": [], "rows": [],
                    "truncated": False, "error_code": "DATASOURCE_CONNECT_FAILED", "latency_ms": (time.monotonic() - started) * 1000})
                continue
            conn.set_authorizer(_authorizer(spec.allowed_tables))
            deadline = time.monotonic() + 2
            conn.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)
            try:
                conn.execute("EXPLAIN " + statement).fetchall()
            except sqlite3.Error:
                raise HostAPIError("QUERY_REJECTED", 400) from None
            prepared.append((identifier, conn, started))
        for identifier, conn, started in prepared:
            try:
                cursor = conn.execute(statement)
                columns = [field[0] for field in cursor.description]
                if len(columns) > 500:
                    raise HostAPIError("LIMIT_EXCEEDED", 400)
                rows = cursor.fetchmany(limit + 1)
                results.append({"datasource_id": identifier, "ok": True, "columns": columns,
                    "rows": [_query_row(row) for row in rows[:limit]], "truncated": len(rows) > limit,
                    "error_code": None, "latency_ms": (time.monotonic() - started) * 1000})
            except sqlite3.Error:
                raise HostAPIError("QUERY_REJECTED", 400) from None
    order = {key: index for index, key in enumerate(identifiers)}
    results.sort(key=lambda row: order[row["datasource_id"]])
    return {"results": results, "coverage": "complete" if all(row["ok"] for row in results) else "partial"}


# LLM: 查询路由没有写副作用，owner/空间、墓碑、SQL 和表范围在共用引擎统一核验。
# 函数用途: 执行前端联邦搜索请求。
def query(call):
    return w9_query(call, call.params["statement"], call.params.get("datasource_ids"), call.params.get("limit", 100))


# LLM: 多集合共用 W0 日志提交点；只派发一次 mutate，回执与审计保持同笔提交。
# 函数用途: 在同一原事务内更新多个 W9 集合。
def w9_write_many(call, collections, mutate, receipt_view=None):
    digest = hashlib.sha256(json.dumps({"action": call.action, "params": call.params,
        "expected_revision": call.expected_revision}, sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode()).hexdigest()

    # LLM: 多集合提交前核本次活跃上下文，不从记录推断权限。
    # 函数用途: 给多集合变更加实时授权。
    def checked(records):
        call.context.check(call.action, {"owner_id": call.owner_id})
        return mutate(records)

    return _w9_store(call).transaction_many(collections, checked,
        idempotency_key=call.idempotency_key, params_digest=digest, receipt_view=receipt_view)
