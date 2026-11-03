"""FIM 适配器查询与 canary marker 的单次秘密、生命周期及触发证据。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import re
import secrets
from dataclasses import replace
from datetime import datetime, timezone

from ..gateway_parts.business_api_v13 import v13_provider
from ..plugin_host.errors import HostAPIError
from .v13_w9_datasources import (
    w9_get,
    w9_list,
    w9_now,
    w9_page,
    w9_push_entries,
    w9_require_space,
    w9_write,
    w9_write_many,
)

_FIM_CURSOR_KEY = secrets.token_bytes(32)


# LLM: 外部来源游标额外绑定 owner、主体、筛选及当前适配器，不把原游标当权限。
# 函数用途: 对 FIM 来源游标签名或核验。
def _fim_cursor(call, provider, *, encode=None):
    binding = [str(call.owner_home), call.owner_id, call.subject_id, call.action,
        {k: v for k, v in call.params.items() if k != "cursor"}, id(provider)]
    scope = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    if encode is not None:
        payload = json.dumps([scope, encode], separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(hmac.digest(_FIM_CURSOR_KEY, payload, "sha256") + payload).decode()
    cursor = call.params.get("cursor")
    if cursor is None:
        return None
    try:
        raw = base64.b64decode(cursor, altchars=b"-_", validate=True)
        if not hmac.compare_digest(raw[:32], hmac.digest(_FIM_CURSOR_KEY, raw[32:], "sha256")):
            raise ValueError("cursor")
        stored_scope, value = json.loads(raw[32:])
        if stored_scope != scope or not isinstance(value, str) or not value:
            raise ValueError("cursor")
        return value
    except (ValueError, TypeError, UnicodeError):
        raise HostAPIError("CURSOR_EXPIRED", 503) from None


# LLM: FIM 没有原生引擎，缺适配器时保持 HOST_NOT_READY，不伪造空记录。
# 函数用途: 读取真实 FIM 提供者的一页事件。
def fim_events(call):
    if call.params.get("space_id"):
        w9_require_space(call, call.params["space_id"])
    provider = v13_provider(call, "osquery_fim")
    p = call.params
    result = provider.events(host=p.get("host"), path_prefix=p.get("path_prefix"),
        since=p.get("since"), cursor=_fim_cursor(call, provider), limit=p.get("limit", 50))
    return {"items": result["items"], "next_cursor": _fim_cursor(call, provider,
        encode=result["next_cursor"]) if result.get("next_cursor") is not None else None,
        "coverage": "partial" if result.get("next_cursor") else "complete"}


# LLM: window 由 schema 校验，数值是固定时间单位换算，不从展示文案决定业务。
# 函数用途: 查询指定时间窗的 FIM 聚合事实。
def fim_summary(call):
    if call.params.get("space_id"):
        w9_require_space(call, call.params["space_id"])
    window = call.params["window"]
    seconds = int(window[:-1]) * {"m": 60, "h": 3600, "d": 86400}[window[-1]]
    result = v13_provider(call, "osquery_fim").summary(window_seconds=seconds)
    return {"window": window, "events_total": result["events_total"], "by_action": result["by_action"],
        "top_hosts": result["top_hosts"], "top_paths": result["top_paths"], "coverage": result.get("coverage", "complete")}


# LLM: 所有公开读取去除 owner 和 verifier，只保留合同字段。
# 函数用途: 构造标记的公开视图。
def _view(row):
    return {key: value for key, value in row.items() if key not in ("owner_id", "value_sha256")}


# LLM: 标记 ID 不能借其它 owner 的记录恢复权限，空间逐次由原服务检查。
# 函数用途: 获取并校验标记。
def _marker(call, token_id):
    row = w9_get(call, "canary_markers", token_id)
    if row is None:
        raise HostAPIError("RESOURCE_NOT_FOUND", 404)
    w9_require_space(call, row["space_id"])
    return row


# LLM: 只替换 secret-once 字段，不改变其它回执；原始值不进 v13 receipts。
# 函数用途: 保存可安全重放的标记创建回执。
def _receipt(result):
    return {**result, "secret": "<redacted>"}


# LLM: 文档正文是显式诱饵成品，不写入记录或审计；上传复用原配额、权限与原子 artifact 入口。
# 函数用途: 将文档型标记发布为原生业务附件。
def _document(call, space_id, secret):
    from ..gateway_parts.business_artifacts_http import upload_artifact
    result = upload_artifact(io.BytesIO(("canary_marker=" + secret + "\n").encode()),
        {"space_id": space_id, "idempotency_key": call.idempotency_key},
        "canary-marker.txt", call.context, call.services)
    return {key: result[key] for key in ("artifact_id", "version_ref", "name", "mime_type", "size_bytes", "content_sha256")}


# LLM: 随机值只在首次响应公开；唯一持久 verifier 是 SHA256，禁止将秘密放进标签或触发详情。
# 函数用途: 创建凭据、API key 或文档型标记。
def create_marker(call):
    space_id = w9_require_space(call, call.params.get("space_id"))
    p = call.params

    # LLM: 随机值及附件发表都在原幂等事务回调内，重放不再生成或上传。
    # 函数用途: 保存标记指纹并构造首次响应。
    def mutate(records):
        secret = "canary_" + p["kind"] + "_" + secrets.token_urlsafe(32)
        digest = hashlib.sha256(secret.encode()).hexdigest()
        identifier = "marker-" + secrets.token_hex(16)
        now = w9_now(call)
        expiry = datetime.fromtimestamp(call.clock() + p.get("expires_in_days", 30) * 86400, timezone.utc).isoformat()
        row = {"token_id": identifier, "space_id": space_id, "kind": p["kind"], "label": p["label"],
            "placement_hint": p.get("placement_hint"), "state": "active", "fingerprint": digest[:16],
            "artifact": _document(call, space_id, secret) if p["kind"] == "document" else None,
            "expires_at": expiry, "trigger_count": 0, "last_triggered_at": None,
            "revision": 1, "created_at": now, "updated_at": now, "owner_id": call.owner_id, "value_sha256": digest}
        records[identifier] = row
        return {"marker": _view(row), "secret": secret}

    return w9_write(call, "canary_markers", mutate, receipt_view=_receipt)


# LLM: 无论 active/revoked 都按最新记录做 CAS，吊销不抹除既有证据。
# 函数用途: 紧急吊销一个标记。
def revoke_marker(call):
    identifier = call.params["token_id"]
    _marker(call, identifier)

    # LLM: 事务内再次检查 owner/空间与最新版本，防止锁外旧快照覆盖。
    # 函数用途: 将当前标记更新成 revoked。
    def mutate(records):
        row = records.get(identifier)
        if row is None or row["owner_id"] != call.owner_id:
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)
        w9_require_space(call, row["space_id"])
        if row["revision"] != call.expected_revision:
            raise HostAPIError("REVISION_CONFLICT", 409)
        row.update(state="revoked", revision=row["revision"] + 1, updated_at=w9_now(call))
        return {"marker": _view(row)}

    return w9_write(call, "canary_markers", mutate)


# LLM: 标记及触发使用同一多集合事务，不出现删除一半的可见状态。
# 函数用途: 按版本删除一个标记及关联证据。
def delete_marker(call):
    identifier = call.params["token_id"]

    # LLM: 所有校验在同一原生锁中，未知 owner 与不存在统一 404。
    # 函数用途: 原子删除标记和它的触发记录。
    def mutate(collections):
        records, triggers = collections["canary_markers"], collections["canary_triggers"]
        row = records.get(identifier)
        if row is None or row["owner_id"] != call.owner_id:
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)
        w9_require_space(call, row["space_id"])
        if row["revision"] != call.expected_revision:
            raise HostAPIError("REVISION_CONFLICT", 409)
        for key in list(triggers):
            if triggers[key]["token_id"] == identifier and triggers[key]["owner_id"] == call.owner_id:
                del triggers[key]
        del records[identifier]
        return {"token_id": identifier, "deleted": True}

    return w9_write_many(call, ("canary_markers", "canary_triggers"), mutate)


# LLM: 只比较候选hash，不恢复秘密；遍历原存储/推送配额内完整正文，不能静默截断后报complete。
# 函数用途: 判断一段证据是否含该标记。
def _matches(value, digest):
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    candidates = re.finditer(r"canary_(?:credential|api_key|document)_[A-Za-z0-9_-]{20,100}", text)
    return any(hmac.compare_digest(hashlib.sha256(item.group().encode()).hexdigest(), digest) for item in candidates)


# LLM: 只有本 owner、本空间的规范 alert 能成为证据，描述只能用于 hash 匹配、不能决定权限。
# 函数用途: 读取有界的标记触发来源。
def _sources(call, marker):
    sources = []
    alerts = sorted(w9_list(call, "soc_alerts"), key=lambda row: row.get("created_at", ""), reverse=True)
    if len(alerts) > 5000:
        raise HostAPIError("LIMIT_EXCEEDED", 400)
    for row in alerts:
        if row.get("space_id") == marker["space_id"] and _matches([row.get("description", ""), row.get("entities", [])], marker["value_sha256"]):
            sources.append(("alert", row["alert_id"], row.get("created_at") or w9_now(call)))
    entries = sorted(w9_push_entries(call), key=lambda row: row.get("receipt", {}).get("queued_at", 0), reverse=True)
    if len(entries) > 5000:
        raise HostAPIError("LIMIT_EXCEEDED", 400)
    for entry in entries:
        if entry.get("space_id") != marker["space_id"] or not entry.get("source_verified"):
            continue
        if _matches(entry.get("prompt", ""), marker["value_sha256"]):
            receipt = entry["receipt"]
            stamp = datetime.fromtimestamp(receipt["queued_at"], timezone.utc).isoformat()
            sources.append(("inbox_push", receipt["push_id"], stamp))
    return sources, any(not row.get("source_verified") for row in entries)


# LLM: 来源最多各5000条，先同一事务登记全部命中再签游标，避免下一页因自身补写而失效。
# 函数用途: 获取触发追踪并登记新发现的有界证据。
def list_triggers(call):
    identifier = call.params["token_id"]
    marker = _marker(call, identifier)
    sources, incomplete = _sources(call, marker)
    known = {(row["source"], row["source_ref"]) for row in w9_list(call, "canary_triggers") if row["token_id"] == identifier}
    pending = [item for item in sources if (item[0], item[1]) not in known]
    batch = pending
    if batch:
        key = hashlib.sha256(json.dumps([identifier, batch], sort_keys=True).encode()).hexdigest()
        scan_call = replace(call, idempotency_key="marker-scan-" + key,
            params={"token_id": identifier, "scan_digest": key})

        # LLM: 同一源只登记一次；被吊销标记仍保留触发，并显式标记 revoked。
        # 函数用途: 原子登记新触发并更新标记统计。
        def mutate(collections):
            rows, triggers = collections["canary_markers"], collections["canary_triggers"]
            current = rows.get(identifier)
            if current is None or current["owner_id"] != call.owner_id:
                raise HostAPIError("RESOURCE_NOT_FOUND", 404)
            w9_require_space(call, current["space_id"])
            added = 0
            for source, ref, observed in batch:
                trigger_id = "trigger-" + hashlib.sha256(json.dumps([identifier, source, ref]).encode()).hexdigest()[:32]
                if trigger_id in triggers:
                    continue
                now = w9_now(call)
                triggers[trigger_id] = {"trigger_id": trigger_id, "owner_id": call.owner_id,
                    "token_id": identifier, "observed_at": observed, "source": source, "source_ref": ref,
                    "details": {"matched_by": "sha256", "revoked": current["state"] == "revoked"},
                    "revision": 1, "created_at": now, "updated_at": now}
                added += 1
            if added:
                current.update(trigger_count=current["trigger_count"] + added,
                    last_triggered_at=max([item[2] for item in batch] + ([current["last_triggered_at"]] if current["last_triggered_at"] else [])),
                    revision=current["revision"] + 1, updated_at=w9_now(call))
            return {"added": added}

        w9_write_many(scan_call, ("canary_markers", "canary_triggers"), mutate)
    rows = [{key: value for key, value in row.items() if key not in ("owner_id", "revision", "created_at", "updated_at")}
        for row in w9_list(call, "canary_triggers") if row["token_id"] == identifier]
    rows.sort(key=lambda row: (row["observed_at"], row["trigger_id"]), reverse=True)
    result = w9_page(call, "canary_triggers", rows)
    if incomplete or len(pending) > len(batch):
        result["coverage"] = "partial"
    return result
