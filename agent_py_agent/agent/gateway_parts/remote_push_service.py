# LLM: 独立push_token只签单向事件；不调用/ask控制解析，不授真人确认。nonce跨进程耐久，入队沿原Gateway回执及C末门。
# 模块用途: 验证外部推送并立即持久入队，只返回不含正文和路径的接收回执。
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import re
import time
from dataclasses import asdict, replace
from pathlib import Path

from ..common.json_io import locked_json_path, write_private_text_file_atomic_unlocked
from ..common.nofollow_fs import read_bytes_beneath
from ..conversation.learning_source import unknown_projection
from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home

# 远端主代理推送接口单次允许接收的最大请求体字节数上限（64KB）。
MAX_PUSH_BYTES = 65536
_NONCE = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")


# LLM: 错误只携带固定机器码，不包装请求、Token或底层路径；HTTP和工具共用。
# 类用途: 表示推送校验或受理失败。
class RemotePushError(ValueError):
    # LLM: status由宿主代码生成，绝不从外部正文采信。
    # 函数用途: 保存安全原因码及HTTP状态。
    def __init__(self, code: str, status: int = 400):
        self.error_code, self.status = code, status
        super().__init__(code)


# LLM: 原始HTTPMessage不能先dict丢副本；安全头即使同值重复也拒绝，无I/O。
# 函数用途: 取得唯一请求头，缺失返回空串。
def push_header(headers, name):
    values = [value for key, value in headers.items() if key.casefold() == name.casefold()]
    if len(values) > 1:
        raise RemotePushError("REMOTE_PUSH_HEADER_INVALID")
    return str(values[0]) if values else ""


# LLM: JSON重复键和NaN不可参与签名后不同解释；严格UTF8和完整object，无副作用。
# 函数用途: 解码唯一、有限值的JSON对象。
def push_json(raw):
    # LLM: 同层重复字段即拒，不能由后一个身份或正文覆盖前一个。
    # 函数用途: 检查JSON每个对象的字段唯一性。
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    # LLM: NaN/Infinity不是JSON值，不能默默改成null后签名成功入队。
    # 函数用途: 拒绝非标准常量。
    def invalid_constant(_value):
        raise ValueError("nonfinite")

    # LLM: 合法数字语法也可能溢出Python float；同非标准常量一样在入队前拒绝。
    # 函数用途: 保证嵌套JSON的小数始终有限。
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("nonfinite")
        return number

    try:
        result = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant, parse_float=finite_float)
    except (ValueError, UnicodeError, RecursionError):
        raise RemotePushError("REMOTE_PUSH_JSON_INVALID") from None
    if not isinstance(result, dict):
        raise RemotePushError("REMOTE_PUSH_JSON_INVALID")
    return result


# LLM: 固定宿主根下nofollow读取，缺文件为空；坏账/符号链接不当成空库覆盖。
# 函数用途: 读取推送配置、nonce或幂等账本，限制磁盘读取大小。
def read_push_json(root: Path, name: str):
    raw = read_bytes_beneath(root, (name,), max_bytes=16 * 1024 * 1024)
    return push_json(raw) if raw is not None else {}


_PUBLIC_ENDPOINT_FIELDS = frozenset({"endpoint_url", "enabled", "allowed_private_hosts", "allowed_callers"})


# LLM: 同文件存储下在数据源出口即剥离token_ciphertext/token_hash等凭据字段，仅向目录层返回公开白名单字段。
# 函数用途: 读取并裁剪远端推送节点的公开元数据，确保上层目录不取用、不解密、不暴露任何密文或摘要。
def read_public_push_endpoints(root: Path, name: str) -> dict[str, dict]:
    data = read_push_json(root, name)
    rows = data.get("endpoints", {}) if isinstance(data, dict) else None
    if not isinstance(rows, dict):
        raise ValueError("invalid endpoints metadata")
    public_rows: dict[str, dict] = {}
    for ref, row in rows.items():
        if not isinstance(row, dict):
            raise ValueError("invalid endpoint metadata")
        public_rows[ref] = {key: row[key] for key in _PUBLIC_ENDPOINT_FIELDS if key in row}
    return public_rows


# LLM: owner字符串须与唯一规范路径编号逐字一致，清洗或跨目录输入不能变成合法身份。
# 函数用途: 解析local/main、local/用户和providers/通道/users或groups/编号。
def push_owner(value):
    if not isinstance(value, str):
        raise RemotePushError("REMOTE_PUSH_TARGET_INVALID")
    parts = value.split("/")
    if value == "local/main":
        owner = OwnerIdentity.local_main()
    elif len(parts) == 2 and parts[0] == "local":
        owner = OwnerIdentity.local_user(parts[1])
    elif len(parts) == 4 and parts[0] == "providers" and parts[2] in {"users", "groups"}:
        factory = OwnerIdentity.provider_user if parts[2] == "users" else OwnerIdentity.provider_group
        owner = factory(parts[1], parts[3])
    else:
        raise RemotePushError("REMOTE_PUSH_TARGET_INVALID")
    if resolve_owner_home(Path("/"), owner).owner_id != value:
        raise RemotePushError("REMOTE_PUSH_TARGET_INVALID")
    return owner


# LLM: 字段闭合，外部不能设置路径、角色、工具授权、回调或owner冻结字段；内容只作不可信事件。
# 函数用途: 规范化一次推送的正文、会话策略和业务幂等键。
def validate_push_payload(body):
    allowed = {"target_owner", "instruction", "title", "idempotency_key", "event_id", "source_kind",
               "thread_mode", "thread_reuse_key", "context_payload", "context_snapshot"}
    if set(body) - allowed:
        raise RemotePushError("REMOTE_PUSH_SCHEMA_INVALID")
    result = dict(body)
    key = result.get("idempotency_key") or result.get("event_id")
    if result.get("idempotency_key") and result.get("event_id") and result["idempotency_key"] != result["event_id"]:
        raise RemotePushError("REMOTE_PUSH_SCHEMA_INVALID")
    result["idempotency_key"] = key
    for name, limit in (("target_owner", 256), ("instruction", 32768), ("idempotency_key", 256)):
        value = result.get(name)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise RemotePushError("REMOTE_PUSH_SCHEMA_INVALID")
    push_owner(result["target_owner"])
    mode = result.setdefault("thread_mode", "new_thread")
    if mode not in {"new_thread", "reuse_key"}:
        raise RemotePushError("REMOTE_PUSH_SCHEMA_INVALID")
    for name, limit in (("title", 256), ("thread_reuse_key", 256), ("source_kind", 128)):
        if name in result and (not isinstance(result[name], str) or len(result[name]) > limit):
            raise RemotePushError("REMOTE_PUSH_SCHEMA_INVALID")
    if mode == "reuse_key" and not result.get("thread_reuse_key"):
        raise RemotePushError("REMOTE_PUSH_SCHEMA_INVALID")
    if "context_payload" in result and not isinstance(result["context_payload"], dict):
        raise RemotePushError("REMOTE_PUSH_SCHEMA_INVALID")
    snapshot = result.get("context_snapshot", [])
    if (not isinstance(snapshot, list) or len(snapshot) > 256 or any(not isinstance(row, dict)
            or set(row) != {"role", "content"} or row["role"] not in {"user", "assistant", "tool"}
            or not isinstance(row["content"], str) for row in snapshot)):
        raise RemotePushError("REMOTE_PUSH_SCHEMA_INVALID")
    return result


# LLM: 仅真实peer位于受信代理网段才消费XFF，沿右到左剥离受信链，不认攻击者最左字段。
# 函数用途: 按可选CIDR白名单检查推送来源，格式损坏失败关闭。
def check_push_ip(peer, headers, allowed, proxies):
    try:
        address = ipaddress.ip_address(peer)
        trusted = [ipaddress.ip_network(item, strict=False) for item in proxies]
        if any(address in network for network in trusted):
            forwarded = push_header(headers, "X-Forwarded-For")
            chain = [ipaddress.ip_address(item.strip()) for item in forwarded.split(",")] if forwarded else []
            while chain and any(address in network for network in trusted):
                address = chain.pop()
        networks = [ipaddress.ip_network(item, strict=False) for item in allowed]
        if networks and not any(address in network for network in networks):
            raise ValueError("denied")
    except (ValueError, TypeError):
        raise RemotePushError("REMOTE_PUSH_SOURCE_IP_DENIED", 403) from None


# LLM: secret只存在本次宿主内存；磁盘仅SHA256验证器，不持久化可恢复明文密钥，不赋予Gateway或Plugin身份。
# 函数用途: 校验启用/过期/owner/action、时间窗、来源IP和原始正文的HMAC。
def authenticate_push(root, raw, headers, peer, current):
    config = read_push_json(root, "tokens.json")
    secret = push_header(headers, "X-Agent-Push-Token")
    digest = hashlib.sha256(secret.encode()).hexdigest()
    rows = config.get("tokens", [])
    if not isinstance(rows, list) or not secret or len(secret) > 512:
        raise RemotePushError("REMOTE_PUSH_TOKEN_INVALID", 401)
    matches = [row for row in rows if isinstance(row, dict) and isinstance(row.get("token_sha256"), str)
               and hmac.compare_digest(row["token_sha256"], digest)]
    if len(matches) != 1:
        raise RemotePushError("REMOTE_PUSH_TOKEN_INVALID", 401)
    row = matches[0]
    expiry = row.get("expires_at")
    if (row.get("enabled") is not True or not isinstance(row.get("token_id"), str) or not row["token_id"]
            or expiry is not None and (type(expiry) not in {int, float} or not math.isfinite(expiry) or expiry <= current)):
        raise RemotePushError("REMOTE_PUSH_TOKEN_INVALID", 401)
    stamp, nonce = push_header(headers, "X-Agent-Timestamp"), push_header(headers, "X-Agent-Nonce")
    if not re.fullmatch(r"[0-9]{1,12}", stamp) or abs(int(stamp) - current) > 300:
        raise RemotePushError("REMOTE_PUSH_TIMESTAMP_EXPIRED", 401)
    if _NONCE.fullmatch(nonce) is None:
        raise RemotePushError("REMOTE_PUSH_SIGNATURE_INVALID", 401)
    message = f"{stamp}.{nonce}.{hashlib.sha256(raw).hexdigest()}".encode()
    signature = hmac.digest(secret.encode(), message, "sha256").hex()
    supplied = push_header(headers, "X-Agent-Signature")
    if re.fullmatch(r"[a-f0-9]{64}", supplied) is None or not hmac.compare_digest(signature, supplied):
        raise RemotePushError("REMOTE_PUSH_SIGNATURE_INVALID", 401)
    body = validate_push_payload(push_json(raw))
    owners, actions = row.get("allowed_target_owners"), row.get("allowed_actions")
    if (not isinstance(owners, list) or body["target_owner"] not in owners
            or actions != ["push_event"]):
        raise RemotePushError("REMOTE_PUSH_TOKEN_SCOPE_DENIED", 403)
    check_push_ip(peer, headers, row.get("allowed_source_ips", []), config.get("trusted_proxies", []))
    return row, body, nonce


# LLM: nonce键为token身份+nonce摘要；跨进程同文件锁下消费600秒，不以进程内集合防重放。
# 函数用途: 原子拒绝重复签名并清理已到期nonce。
def consume_push_nonce(root, token_id, nonce, current):
    path = root / "nonces.json"
    key = hashlib.sha256(json.dumps([token_id, nonce]).encode()).hexdigest()
    with locked_json_path(path):
        data = read_push_json(root, "nonces.json")
        data = {name: expiry for name, expiry in data.items() if type(expiry) in {int, float} and expiry >= current}
        if key in data:
            raise RemotePushError("REMOTE_PUSH_NONCE_REPLAYED", 409)
        if len(data) >= 100000:
            raise RemotePushError("REMOTE_PUSH_CAPACITY_EXCEEDED", 503)
        data[key] = current + 600
        persist_push_json(path, data)


# LLM: 真实Agent或显式原C只提供canonical根，不从body、队列位置、线程或Path.home猜宿主路径。
# 函数用途: 取得当前监听器的推送账本位置，缺宿主时拒绝受理。
def remote_push_root(server):
    agent, control = getattr(server, "agent", None), getattr(server, "control_plane", None)
    root = getattr(getattr(agent, "home_paths", None), "root", None) if agent is not None else getattr(control, "home_root", None)
    if root is None:
        raise RemotePushError("REMOTE_PUSH_UNAVAILABLE", 503)
    return Path(root) / "remote_push"


# LLM: handler读有限原始bytes后签名；所有错误回执不含正文、签名、底层路径或堆栈。
# 函数用途: 接收唯一远端推送路由并返回202耐久队列回执。
def handle_remote_push(handler, server):
    try:
        size = push_header(handler.headers, "Content-Length")
        if push_header(handler.headers, "Transfer-Encoding") or not size.isdigit() or not 0 < int(size) <= MAX_PUSH_BYTES:
            raise RemotePushError("REMOTE_PUSH_BODY_TOO_LARGE", 413)
        raw = handler.rfile.read(int(size))
        if len(raw) != int(size):
            raise RemotePushError("REMOTE_PUSH_JSON_INVALID")
        result = accept_remote_push(server, raw, handler.headers, handler.client_address[0])
        handler._send_json(202, result)
    except RemotePushError as exc:
        handler._send_json(exc.status, {"ok": False, "error_code": exc.error_code})
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        handler._send_json(503, {"ok": False, "error_code": "REMOTE_PUSH_UNAVAILABLE"})


# LLM: 仅已持对应locked_json_path者调用；私有原子写及fsync由同一公共叶完成，不重复取不可重入锁。
# 函数用途: 耐久保存推送账本，文件出生0600。
def persist_push_json(path, value):
    write_private_text_file_atomic_unlocked(path, json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False))


# LLM: 不物化Agent；当前owner用实际store，其它owner沿原runtime路径解析器，不能继承主用户已解析绝对路径。
# 函数用途: 为已过Token ACL的目标打开唯一会话库；不存在的owner不在此创建账号。
def target_push_store(server, owner):
    from ..conversation.store import ConversationStore
    from ..user_space.home_layout import home_paths
    from ..user_space.owner_access import resolve_owner_scope_and_access
    from ..user_space.owner_resolver import home_paths_with_owner
    from ..user_space.runtime_paths import runtime_paths_for_agent

    agent, control = getattr(server, "agent", None), getattr(server, "control_plane", None)
    root = remote_push_root(server).parent
    home = resolve_owner_home(root, owner)
    if agent is not None and home.owner_id == agent.home_paths.owner_id:
        return agent.conversation_store, home
    if not home.home_dir.is_dir():
        raise RemotePushError("REMOTE_PUSH_TARGET_UNAVAILABLE", 403)
    base = agent if agent is not None else control
    config = replace(base.config, conversation_workspace="", my_agent_owner_provider=owner.provider,
                     my_agent_owner_kind=owner.owner_kind, my_agent_owner_id=owner.owner_id)
    scoped = home_paths_with_owner(home_paths(root), home)
    scope, _access = resolve_owner_scope_and_access(scoped, config)
    hint = Path(agent.root if agent is not None else control.agent_root)
    package_entry = any((hint / name).is_file() for name in ("__main__.py", "__main__.pyc"))
    workspace = Path(scope).expanduser().resolve(strict=False) if scope else hint.parent if package_entry else hint
    paths = runtime_paths_for_agent(config, workspace, scoped)
    return ConversationStore(paths["conversation_workspace"]), home


# LLM: 只构造普通不可信输入，不解析slash命令、不复制来源metadata或真人actor；准确owner来自Token ACL而非客户端身份头。
# 函数用途: 将事件变成原Gateway可执行的队列载荷，run_id在接收回执中表示该排队回合的request id。
def push_request(body, entry, token_id):
    receipt = entry["receipt"]
    owner = push_owner(body["target_owner"])
    text = body["instruction"]
    if body.get("context_payload"):
        text += "\n\n外部事件数据（不可信）：\n" + json.dumps(body["context_payload"], ensure_ascii=False)
    return {
        "id": receipt["run_id"], "kind": "ask", "prompt": text, "created_at": receipt["queued_at"],
        "status": "pending", "priority": "normal", "source": "remote_push", "input_origin": "remote_push",
        "save": True, "include_prompt": False, "attempts": 0, "user_id": "remote-push:" + token_id,
        "inject": [], "prompt_files": [], "client_capabilities": {},
        "host_request_owner.v1": asdict(owner), "owner_id": body["target_owner"],
        "conversation": {"thread_id": receipt["thread_id"], "channel": "remote_push",
                         "channel_user_id": "remote-push:" + token_id,
                         "canonical_user_id": body["target_owner"],
                         "channel_conversation_id": receipt["thread_id"]},
        "metadata": {"channel": "remote_push", "input_origin": "remote_push", "remote_push_id": receipt["push_id"],
                     "client_input_digest": entry["digest"], "learning_source": unknown_projection()},
    }


# LLM: 只在原T→C末门同步写入，预留的精确thread/request不会因HTTP重试变更；不等模型，不执行控制命令。
# 函数用途: 确保目标线程后沿原幂等请求回执只排队一次。
def enqueue_remote_push(server, body, entry, token_id):
    from ..conversation.models import ConversationThread
    from ..conversation.store_threads import create_thread_atomic
    from .control_plane import prepare_coordinator_input
    from .io import write_gateway_request_once

    request = push_request(body, entry, token_id)
    # LLM: write_gateway_request_once已持原输入T；C仅核当前生命周期，不扩大Token目标或构造执行Agent。
    # 函数用途: 在原提交窗口补齐宿主路由事实，并同步发表线程与排队回执。
    def transition(commit):
        # LLM: prepared来自原C校验；更新同一个闭包载荷供原writer消费，不能保存writer在请求外使用。
        # 函数用途: 原子提交原队列身份，不在回执里伪造已执行的模型run。
        def publish(prepared):
            prepared = dict(prepared)
            request.clear()
            request.update(prepared)
            store, home = target_push_store(server, push_owner(body["target_owner"]))
            identifier = entry["receipt"]["thread_id"]
            thread, error = store.threads.load_report(identifier)
            if error:
                raise RemotePushError("REMOTE_PUSH_UNAVAILABLE", 503)
            if thread is None:
                create_thread_atomic(store.threads, ConversationThread(
                    thread_id=identifier, owner_id=home.owner_id, canonical_user_id=home.owner_id,
                    owner_home=str(home.home_dir), title=body.get("title", "外部事件"),
                    learning_source=unknown_projection(), metadata={"input_origin": "remote_push"},
                ))
            elif thread.owner_id != home.owner_id or thread.metadata.get("input_origin") != "remote_push":
                raise RemotePushError("REMOTE_PUSH_THREAD_CONFLICT", 409)
            if body.get("context_snapshot"):
                from ..conversation.thread_fork_service import fork_untrusted_context

                fork_untrusted_context(store, identifier, body["context_snapshot"], entry["receipt"]["push_id"])
            return commit()

        return prepare_coordinator_input(server.paths, request, commit=publish)

    write_gateway_request_once(server.paths, request, commit_transition=transition)


# LLM: state锁串行幂等与rate；线程hash另含mode前缀，new_thread与reuse_key不能同标签碰撞。prepared恢复仍复用原ID。
# 函数用途: 验证并耐久接纳一次推送，重复业务键返回原接收结果。
def accept_remote_push(server, raw, headers, peer):
    root, current = remote_push_root(server), time.time()
    row, body, nonce = authenticate_push(root, raw, headers, peer, current)
    consume_push_nonce(root, row["token_id"], nonce, current)
    path = root / "receipts.json"
    with locked_json_path(path, blocking=False):
        state = read_push_json(root, "receipts.json")
        entries, threads = state.setdefault("entries", {}), state.setdefault("threads", {})
        key = hashlib.sha256(json.dumps([row["token_id"], body["target_owner"], body["idempotency_key"]]).encode()).hexdigest()
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        entry = entries.get(key)
        if entry is not None and entry["digest"] != digest:
            raise RemotePushError("REMOTE_PUSH_IDEMPOTENCY_CONFLICT", 409)
        if entry is not None and entry["accepted"]:
            return {**entry["receipt"], "duplicate": True}
        if entry is None:
            rate = row.get("rate_limit_per_minute", 30)
            if type(rate) is not int or rate < 1:
                raise RemotePushError("REMOTE_PUSH_TOKEN_INVALID", 401)
            rates = state.setdefault("rates", {})
            rates = state["rates"] = {name: [stamp for stamp in stamps if stamp > current - 60]
                                      for name, stamps in rates.items() if stamps and stamps[-1] > current - 60}
            stamps = rates.setdefault(row["token_id"], [])
            if len(stamps) >= rate:
                raise RemotePushError("REMOTE_PUSH_RATE_LIMITED", 429)
            if len(entries) >= 10000:
                raise RemotePushError("REMOTE_PUSH_CAPACITY_EXCEEDED", 503)
            label = body["idempotency_key"] if body["thread_mode"] == "new_thread" else body["thread_reuse_key"]
            thread_key = hashlib.sha256(json.dumps(
                [row["token_id"], body["target_owner"], body["thread_mode"] + ":" + label]).encode()).hexdigest()
            thread_id = threads.setdefault(thread_key, "thread_remote_" + thread_key)
            entry = entries[key] = {"digest": digest, "accepted": False, "receipt": {
                "ok": True, "status": "accepted", "push_id": "push_" + key,
                "target_owner": body["target_owner"], "thread_id": thread_id,
                "run_id": "req_remote_" + key, "queued_at": current,
            }}
            stamps.append(current)
            persist_push_json(path, state)
        enqueue_remote_push(server, body, entry, row["token_id"])
        entry["accepted"] = True
        persist_push_json(path, state)
        return dict(entry["receipt"])
