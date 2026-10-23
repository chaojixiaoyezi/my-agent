# LLM: D2只读正式JSONL，复用原版本物化；不构造Agent/JsonlMemory/索引，不读取候选、lesson或语义服务。
# 模块用途: 提供正式记忆词法查询，并为D2两类观察提供授权末核、有界分页和纯内存MAC游标。
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import secrets
import time
from pathlib import Path

from ..common.nofollow_fs import read_bytes_beneath
from ..memory_store.jsonl import _materialize_memory_events, _memory_record_from_obj
from ..memory_store.recall import MemoryRecallScope, long_term_record_matches_scope
from ..user_space.owner_resolver import OwnerHomeResult
from . import contracts
from .action_specs import action_spec
from .contracts import AuthorizedCall, DomainResult, OwnerReadContext
from .errors import HostAPIError
from .schemas import PAGE_MAX_BYTES, canonical_json, reject_identity_injection, strict_json, thaw

# 单次正式账读取的硬预算；超限明确失败，绝不扫描前缀后宣称完整。
OBSERVATION_STORE_MAX_BYTES = 16 * 1024 * 1024
# 只读游标有效秒数；服务重启会换MAC密钥，旧游标自动失效。
OBSERVATION_CURSOR_TTL_SECONDS = 300
# 单条搜索片段UTF8字节上限。
MEMORY_SNIPPET_MAX_BYTES = 2048
# 单次原文读取UTF8字节上限。
MEMORY_CHUNK_MAX_BYTES = 65536


# LLM: 不接受插件自造上下文；路径/身份必须来自A的真实resolver，缺A时明确未就绪。
# 函数用途: 复验当前调用的只读owner来源及身份一致性。
def _read_context(authorized: AuthorizedCall) -> OwnerReadContext:
    try:
        context = contracts.resolve_read_context(authorized)
    except ImportError:
        raise HostAPIError("HOST_NOT_READY", 503) from None
    if (not isinstance(context, OwnerReadContext) or not isinstance(context.owner_home, OwnerHomeResult)
            or context.source_binding is not authorized.binding
            or context.identity != authorized.effective_owner
            or context.identity != authorized.binding.effective_owner
            or context.owner_home.identity != context.identity):
        raise HostAPIError("CAPABILITY_DENIED", 403)
    return context


# LLM: 即使领域被直接调用仍按A0闭合schema校验，能力检查不替代上游真实来源核验。
# 函数用途: 验证领域动作、参数和静态能力。
def _payload(authorized: AuthorizedCall, actions: set[str]) -> dict:
    if not isinstance(authorized, AuthorizedCall) or authorized.call.action not in actions:
        raise HostAPIError("UNSUPPORTED_ACTION", 400)
    spec = action_spec(authorized.call.action)
    value = thaw(authorized.call.payload)
    reject_identity_injection(value)
    payload = spec.validate_payload(value)
    if not spec.capabilities <= authorized.binding.capabilities:
        raise HostAPIError("CAPABILITY_DENIED", 403)
    return payload


# LLM: 返回前再次经过真实A来源门，不把一次成功解析当永久授权；只限制实际序列化结果。
# 函数用途: 末核owner上下文并形成同步读取回执。
def _result(authorized, context, data, warnings=()) -> DomainResult:
    fresh = _read_context(authorized)
    if fresh.owner_home != context.owner_home or fresh.user_revision != context.user_revision:
        raise HostAPIError("CAPABILITY_DENIED", 403)
    if len(canonical_json(data).encode()) > PAGE_MAX_BYTES - 4096:
        raise HostAPIError("PAYLOAD_TOO_LARGE", 413)
    return DomainResult(200, data, warnings=tuple(warnings))


# LLM: 所有相对组件从宿主DTO确定，根到叶均no-follow且有界，不写锁、缓存或目录。
# 函数用途: 安全读取一个当前owner下的正式账文件。
def _owner_bytes(context: OwnerReadContext, path: Path) -> bytes:
    home = context.owner_home
    try:
        path.relative_to(home.home_dir)
        return read_bytes_beneath(home.root, path.relative_to(home.root).parts,
                                  max_bytes=OBSERVATION_STORE_MAX_BYTES, require_dir_fd=True) or b""
    except (OSError, ValueError, UnicodeError):
        raise HostAPIError("STORE_UNAVAILABLE", 503) from None


# LLM: 游标不携带明文owner、过滤器或路径，MAC绑定固定授权/动作/参数/范围；无落盘或快照权威副本。
# 类用途: 为D2当前服务实例签发有期限的不可篡改分页位置。
class _ObservationCursors:
    # LLM: 密钥仅在本服务内存，重启后旧游标失效；不得把本类当授权发行器。
    # 函数用途: 准备私有分页密钥。
    def __init__(self):
        self._key = secrets.token_bytes(32)

    # LLM: 包含安装/激活/owner/action/filter/grant和资源范围，跨目标或改查询不能续读。
    # 函数用途: 计算本次调用的游标绑定摘要。
    def binding(self, authorized, payload) -> str:
        binding, owner = authorized.binding, authorized.effective_owner
        row = [binding.installation_ref, binding.package_sha256, binding.activation_id, binding.grant_revision,
               [owner.provider, owner.owner_kind, owner.owner_id], binding.user_revision, binding.policy_digest,
               authorized.call.action, {k: v for k, v in payload.items() if k != "cursor"}, authorized.resource_scope]
        return hashlib.sha256(canonical_json(row).encode()).hexdigest()

    # LLM: snapshot是原正式数据摘要或entry修订摘要，不存入业务目录；位置不能由外部明文指定。
    # 函数用途: 签名一个有期限的下一页位置。
    def encode(self, binding, snapshot, position) -> str:
        raw = canonical_json({"b": binding, "s": snapshot, "p": position,
                              "e": int(time.time()) + OBSERVATION_CURSOR_TTL_SECONDS}).encode()
        return base64.urlsafe_b64encode(raw + hmac.digest(self._key, raw, "sha256")).decode()

    # LLM: 仅接收本实例签名的同绑定同快照游标；D2派单要求改版返回409而非设计稿的通用410。
    # 函数用途: 验签并还原有界位置。
    def position(self, cursor, binding, snapshot) -> int:
        if cursor is None:
            return 0
        try:
            decoded = base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True)
            raw, signature = decoded[:-32], decoded[-32:]
            if not hmac.compare_digest(signature, hmac.digest(self._key, raw, "sha256")):
                raise ValueError("signature")
            row = json.loads(raw)
            if (set(row) != {"b", "s", "p", "e"} or row["b"] != binding or row["s"] != snapshot
                    or type(row["p"]) is not int or row["p"] < 0 or row["e"] <= time.time()):
                raise ValueError("binding")
            return row["p"]
        except (ValueError, TypeError, UnicodeError, KeyError):
            raise HostAPIError("CURSOR_EXPIRED", 409) from None


# LLM: 顺序由领域固定；按JSON字节预算减少本页条数必须给next_cursor，不静默丢项。
# 函数用途: 构造可重放且不会混合不同快照的列表页。
def _page(rows, payload, cursor_state, cursors, errors=()):
    binding, snapshot = cursor_state
    position = cursors.position(payload.get("cursor"), binding, snapshot)
    if position > len(rows):
        raise HostAPIError("CURSOR_EXPIRED", 409)
    items, used = [], 4096
    for row in rows[position:position + payload["limit"]]:
        size = len(canonical_json(row).encode()) + 1
        if used + size > PAGE_MAX_BYTES - 8192:
            break
        items.append(row)
        used += size
    end = position + len(items)
    if not items and end < len(rows):
        raise HostAPIError("PAYLOAD_TOO_LARGE", 413)
    return {"items": items, "next_cursor": cursors.encode(binding, snapshot, end) if end < len(rows) else None,
            "snapshot_id": snapshot, "complete": end == len(rows) and not errors,
            "load_errors": [{"code": code, "ref": None} for code in errors]}


# LLM: raw来自UTF8校验通过的正式文本；ignore只丢尾部不完整代码点，不改正文中任何字符。
# 函数用途: 从正文的已知字符边界取得指定字节上限的片段。
def _utf8_slice(raw: bytes, offset: int, maximum: int) -> str:
    if offset > len(raw) or (offset < len(raw) and raw[offset] & 0xC0 == 0x80):
        raise HostAPIError("CURSOR_EXPIRED", 409)
    return raw[offset:offset + maximum].decode("utf-8", errors="ignore")


# LLM: 原materializer对版本和时间有类型假定，坏事件须显式报错，不能让坏remove恢复旧正文。
# 函数用途: 在复用正式记忆物化前检查事件字段。
def _memory_event(raw):
    if not isinstance(raw, dict):
        raise ValueError("record")
    record = _memory_record_from_obj(raw)
    if record is None or not all(isinstance(value, str) for value in
                                  (record.entry_id, record.content, record.kind, record.action, record.source)):
        raise ValueError("text")
    if type(record.version) is not int or record.version < 1:
        raise ValueError("version")
    if record.attributes is not None and not isinstance(record.attributes, dict):
        raise ValueError("attributes")
    attrs = record.attributes or {}
    if not record.entry_id or len(record.entry_id.encode("utf-8")) > 128:
        raise ValueError("entry_id")
    if any(value is not None and not isinstance(value, str) for value in (raw.get("status"), attrs.get("status"))):
        raise ValueError("status")
    if any("confirmed" in fields and type(fields["confirmed"]) is not bool for fields in (raw, attrs)):
        raise ValueError("confirmed")
    for value in (record.expires_at, record.created_at, record.updated_at, record.last_accessed_at):
        if type(value) not in {int, float} or not math.isfinite(value):
            raise ValueError("timestamp")
    record.content.encode("utf-8")
    # 不读取候选文件；误混入正式账的显式未确认标记也不能变成公开事实。
    if raw.get("confirmed") is False or raw.get("status") in {"pending", "candidate", "unconfirmed", "rejected"}:
        record.attributes = {**(record.attributes or {}), "confirmed": False}
    return record


# LLM: 不调用会mkdir或touch的记忆仓库；坏账不得静默当成功空结果，也不泄露解析异常原文。
# 函数用途: 有界读取正式事件并按原删除、版本及过期规则物化。
def _formal_records(context):
    raw = _owner_bytes(context, context.owner_home.long_term_jsonl)
    events, errors = [], []
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        try:
            events.append(_memory_event(strict_json(line, OBSERVATION_STORE_MAX_BYTES)))
        except (HostAPIError, ValueError, TypeError, OverflowError):
            errors = ["MEMORY_STORE_CORRUPT"]
    if errors:
        return [], hashlib.sha256(raw).hexdigest(), errors
    return _materialize_memory_events(events), hashlib.sha256(raw).hexdigest(), errors


# LLM: scope沿原个人/全局/已存在项目规则，动态范围只能来自已授权resource_scope，不能由query文字猜。
# 函数用途: 过滤本次允许公开的正式fact/event/project。
def _visible_memories(records, authorized):
    scope = MemoryRecallScope.from_runtime(task_attributes={
        "memory_scopes": thaw(authorized.resource_scope.get("memory_scopes", ()))})
    projects = [("project", row.attributes["scope_key"]) for row in records
                if isinstance(row.attributes, dict) and row.attributes.get("scope_type") == "project"
                and isinstance(row.attributes.get("scope_key"), str) and row.attributes["scope_key"]]
    scope = MemoryRecallScope(tuple(dict.fromkeys((*scope.keys, *projects))))
    return sorted((row for row in records if row.kind in {"fact", "event", "project"}
                   and _published(row) and long_term_record_matches_scope(row, scope)), key=lambda row: row.entry_id)


# LLM: 正式authority是唯一来源，显式未确认/秘密标记仍须拒绝，不从缺少标记猜一份新审批。
# 函数用途: 排除不允许经插件公开的记录。
def _published(record) -> bool:
    attrs = record.attributes or {}
    return (attrs.get("confirmed") is not False and not attrs.get("secret")
            and attrs.get("status") not in {"pending", "candidate", "unconfirmed", "rejected"})


# LLM: 来源只投影opaque ID，不输出绝对路径、URL、候选正文或审核元信息。
# 函数用途: 构造一个UTF8有界的Memory条目。
def _memory_view(record) -> dict:
    raw = record.content.encode("utf-8")
    text = _utf8_slice(raw, 0, MEMORY_SNIPPET_MAX_BYTES)
    source = record.source
    source_ref = source if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", source) else None
    return {"entry_id": record.entry_id, "kind": record.kind, "revision": record.version,
            "content": text, "content_complete": len(text.encode()) == len(raw), "source_ref": source_ref}


# LLM: call遵循A0的AuthorizedCall->DomainResult；A组合根负责真实来源注册，本服务从不以DTO自证授权。
# 类用途: 读取当前effective owner的正式记忆，提供词法搜索及版本固定的完整正文分页。
class MemoryReadService:
    # LLM: 游标仅是分页状态，不创建持久文件或索引。
    # 函数用途: 初始化当前服务实例的分页签名器。
    def __init__(self):
        self._cursors = _ObservationCursors()

    # LLM: 双重来源校验；游标绑定原账和可见投影，时间到期改变集合也不得静默移动位置。
    # 函数用途: 分发两个记忆只读动作。
    def call(self, authorized: AuthorizedCall) -> DomainResult:
        payload = _payload(authorized, {"memory.search", "memory.get"})
        context = _read_context(authorized)
        records, snapshot, errors = _formal_records(context)
        records = _visible_memories(records, authorized)
        binding = self._cursors.binding(authorized, payload)
        if authorized.call.action == "memory.search":
            rows = [_memory_view(record) for record in records if payload["query"].casefold() in record.content.casefold()]
            snapshot = hashlib.sha256(snapshot.encode() + canonical_json(rows).encode()).hexdigest()
            data = _page(rows, payload, (binding, snapshot), self._cursors, errors)
            data["query_evidence"] = {"query": payload["query"], "scope": "effective_owner", "engine": "lexical",
                                      "complete": data["complete"]}
        else:
            if errors:
                raise HostAPIError("STORE_UNAVAILABLE", 503)
            data = self._get(records, payload, binding)
        return _result(authorized, context, data, errors)

    # LLM: 单条快照含版本和正文摘要；正文同时遵守UTF8和JSON转义预算，位置按实际字节推进。
    # 函数用途: 读取最多64KiB、保持UTF8字符边界的记忆正文。
    def _get(self, records, payload, binding):
        record = next((row for row in records if row.entry_id == payload["entry_id"]), None)
        if record is None:
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)
        raw = record.content.encode("utf-8")
        snapshot = hashlib.sha256(str(record.version).encode() + b":" + raw).hexdigest()
        position = self._cursors.position(payload.get("cursor"), binding, snapshot)
        text = _utf8_slice(raw, position, MEMORY_CHUNK_MAX_BYTES)
        while len(canonical_json(text).encode("utf-8")) > PAGE_MAX_BYTES - 8192:
            text = _utf8_slice(raw, position, len(text.encode("utf-8")) // 2)
        end = position + len(text.encode("utf-8"))
        return {"entry_id": record.entry_id, "kind": record.kind, "revision": record.version, "text": text,
                "next_cursor": self._cursors.encode(binding, snapshot, end) if end < len(raw) else None,
                "complete": end == len(raw)}
