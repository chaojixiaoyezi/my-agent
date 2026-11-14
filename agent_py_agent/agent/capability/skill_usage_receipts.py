"""基于 SQLite RuntimeRepository 的技能精确使用回执与负向记次存储（`usage.receipts`）。

本模块将学习技能的版本绑定使用计数（`UsageReceipt` / `UsageAggregate`）与
任务级去重负向记次（`NegativeReceipt`）持久化在 `RuntimeRepository.metadata`
表中，确保多轮通道信号不跨版本污染，且在同一任务内的多通道负向触发至多记 1 次。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

# 中文说明：SQLite metadata 命名空间与序列化版本号，固定按契约 sm-*:v1 前缀隔离。
SEQUENCE_METADATA_KEY = "sm-seq:v1"
USE_RECEIPT_PREFIX = "sm-use:v1:"
USE_RECEIPT_PREFIX_END = "sm-use:v1;"
USE_AGGREGATE_PREFIX = "sm-use-total:v1:"
USE_AGGREGATE_PREFIX_END = "sm-use-total:v1;"
HURT_RECEIPT_PREFIX = "sm-hurt:v1:"
HURT_RECEIPT_PREFIX_END = "sm-hurt:v1;"

# 中文说明：单次 3 击淘汰/回滚允许的最大负向记次阈值（条）。
MAX_STRIKE_HURT_COUNT = 3
# 中文说明：有界维护扫除单批默认扫描的 SQLite 键数量上限（条）。
DEFAULT_RECEIPT_CLEANUP_BATCH_ENTRIES = 128
# 中文说明：单个合法任务/会话或 SHA-256 标识字符串解析的字符数上限（字符）。
MAX_RECEIPT_IDENTIFIER_CHARS = 512

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REAL_USE_BODY_EVENTS = frozenset({"manual_get", "host_injected"})


def _clean_text(value: Any, *, max_chars: int = MAX_RECEIPT_IDENTIFIER_CHARS) -> str:
    text = str(value or "").strip()
    if len(text) > max_chars:
        return text[:max_chars]
    return text


def _normalize_sha256(value: Any) -> str:
    digest = _clean_text(value, max_chars=128).lower()
    if _SHA256_RE.fullmatch(digest):
        return digest
    return ""


def _normalize_epoch(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _normalize_timestamp(value: Any) -> float:
    if isinstance(value, bool):
        return time.time()
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return time.time()
    if ts <= 0.0 or ts != ts:
        return time.time()
    return ts


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_task_key(
    *,
    task_key: str = "",
    task_id: str = "",
    request_id: str = "",
) -> str:
    """解析精确使用回执使用的规范化 task_key。"""
    explicit = _clean_text(task_key)
    if explicit:
        return explicit
    cleaned_task = _clean_text(task_id)
    if cleaned_task:
        return f"task:{cleaned_task}"
    cleaned_req = _clean_text(request_id)
    if cleaned_req:
        return f"request:{cleaned_req}"
    return ""


def canonical_negative_key(
    *,
    root_task_id: str = "",
    task_id: str = "",
    request_id: str = "",
    receipt_id: str = "",
) -> str:
    """按 V2-2 优先级解析任务级去重负向键（root_task_id -> task_id -> request_id -> receipt_id）。"""
    root = _clean_text(root_task_id)
    if root:
        return root if root.startswith(("task:", "request:", "receipt:")) else f"task:{root}"
    task = _clean_text(task_id)
    if task:
        return task if task.startswith(("task:", "request:", "receipt:")) else f"task:{task}"
    req = _clean_text(request_id)
    if req:
        return req if req.startswith("request:") else f"request:{req}"
    receipt = _clean_text(receipt_id)
    if receipt:
        return receipt if receipt.startswith("receipt:") else f"receipt:{receipt}"
    return ""


def candidate_delivery_task_keys(
    *,
    task_key: str = "",
    root_task_id: str = "",
    task_id: str = "",
    request_id: str = "",
) -> list[str]:
    """生成用于通道 C 检查正文送达回执的候选 task_key 列表（按优先级去重）。"""
    ordered: list[str] = []
    seen: set[str] = set()

    def _add(item: str) -> None:
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)

    explicit = _clean_text(task_key)
    root = _clean_text(root_task_id)
    task = _clean_text(task_id)
    req = _clean_text(request_id)
    _add(explicit)
    if task:
        _add(f"task:{task}")
        _add(task)
    if root:
        _add(f"task:{root}")
        _add(root)
    if req:
        _add(f"request:{req}")
        _add(req)
    return ordered


def receipt_key(
    incarnation_id: str,
    activation_id: str,
    content_sha256: str,
    task_key_value: str,
) -> str:
    """构造单任务单版本的 UsageReceipt 主键。"""
    return (
        f"{USE_RECEIPT_PREFIX}{incarnation_id}:{activation_id}:"
        f"{content_sha256}:{_sha256_text(task_key_value)}"
    )


def aggregate_key(
    incarnation_id: str,
    activation_id: str,
    content_sha256: str,
) -> str:
    """构造单版本汇总 UsageAggregate 主键。"""
    return f"{USE_AGGREGATE_PREFIX}{incarnation_id}:{activation_id}:{content_sha256}"


def negative_receipt_key(
    incarnation_id: str,
    activation_id: str,
    canonical_neg_key_or_digest: str,
    *,
    already_hashed: bool = False,
) -> str:
    """构造任务级去重 NegativeReceipt 主键。"""
    digest = (
        canonical_neg_key_or_digest
        if already_hashed
        else _sha256_text(canonical_neg_key_or_digest)
    )
    return f"{HURT_RECEIPT_PREFIX}{incarnation_id}:{activation_id}:{digest}"


def _read_metadata_json(conn: Any, key: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    raw = row["value"] if isinstance(row, Mapping) or hasattr(row, "keys") else row[0]
    try:
        loaded = json.loads(str(raw))
    except (TypeError, ValueError):
        return None
    return dict(loaded) if isinstance(loaded, dict) else None


def _write_metadata_json(conn: Any, key: str, document: Mapping[str, Any]) -> None:
    encoded = json.dumps(dict(document), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    conn.execute(
        "INSERT INTO metadata(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, encoded),
    )


def _next_sequence(conn: Any) -> int:
    row = conn.execute(
        "SELECT value FROM metadata WHERE key = ?",
        (SEQUENCE_METADATA_KEY,),
    ).fetchone()
    current = 0
    if row is not None:
        raw = row["value"] if isinstance(row, Mapping) or hasattr(row, "keys") else row[0]
        try:
            current = max(0, int(str(raw).strip()))
        except (TypeError, ValueError):
            current = 0
    nxt = current + 1
    conn.execute(
        "INSERT INTO metadata(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SEQUENCE_METADATA_KEY, str(nxt)),
    )
    return nxt


def _default_aggregate(
    incarnation_id: str,
    activation_id: str,
    content_sha256: str,
    *,
    at: float,
) -> dict[str, Any]:
    return {
        "incarnation_id": incarnation_id,
        "activation_id": activation_id,
        "content_sha256": content_sha256,
        "independent_uses": 0,
        "strike_epoch": 0,
        "uses_since_hurt": 0,
        "last_real_used_at": 0.0,
        "sequence": 0,
        "coverage": "exact",
        "hurt_count": 0,
        "epoch_started_at": at,
    }


def _normalized_aggregate(
    raw: Mapping[str, Any] | None,
    *,
    incarnation_id: str,
    activation_id: str,
    content_sha256: str,
    fallback_at: float,
) -> dict[str, Any]:
    base = _default_aggregate(incarnation_id, activation_id, content_sha256, at=fallback_at)
    if not isinstance(raw, Mapping):
        return base
    hurt_val = int(raw.get("hurt_count") or 0)
    hurt_val = max(0, min(MAX_STRIKE_HURT_COUNT, hurt_val))
    cov = str(raw.get("coverage") or "exact")
    if cov not in ("exact", "lower_bound"):
        cov = "exact"
    return {
        "incarnation_id": str(raw.get("incarnation_id") or incarnation_id),
        "activation_id": str(raw.get("activation_id") or activation_id),
        "content_sha256": _normalize_sha256(raw.get("content_sha256")) or content_sha256,
        "independent_uses": max(0, int(raw.get("independent_uses") or 0)),
        "strike_epoch": max(0, int(raw.get("strike_epoch") or 0)),
        "uses_since_hurt": max(0, int(raw.get("uses_since_hurt") or 0)),
        "last_real_used_at": max(0.0, float(raw.get("last_real_used_at") or 0.0)),
        "sequence": max(0, int(raw.get("sequence") or 0)),
        "coverage": cov,
        "hurt_count": hurt_val,
        "epoch_started_at": max(0.0, float(raw.get("epoch_started_at") or fallback_at)),
    }


def _normalized_receipt(raw: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    return {
        "last_counted_epoch": max(0, int(raw.get("last_counted_epoch") or 0)),
        "first_seen_at": max(0.0, float(raw.get("first_seen_at") or 0.0)),
        "last_real_used_at": max(0.0, float(raw.get("last_real_used_at") or 0.0)),
        "last_sequence": max(1, int(raw.get("last_sequence") or 1)),
    }


def record_exact_use(
    store: Any,
    event: Mapping[str, Any],
    *,
    runtime: RuntimeRepository,
) -> dict[str, Any]:
    """记录单次技能正文实际投递的精确版本绑定使用回执与汇总计数。

    仅在事件属于 learned 技能且完成真实正文交付（`manual_get` 或 `host_injected`
    且 `body_chars > 0`）、绑定完整并具备规范化 `task_key` 时入账。
    """
    del store  # 参数保留用于与合同签名对齐，数据权威在 runtime.metadata
    event_name = _clean_text(event.get("event"), max_chars=64)
    kind = _clean_text(event.get("kind"), max_chars=64)
    if kind == "capability_package":
        return {"status": "ignored", "reason": "capability_package"}
    if event_name not in _REAL_USE_BODY_EVENTS:
        return {"status": "ignored", "reason": "non_delivery_event"}
    try:
        body_chars = int(event.get("body_chars") or 0)
    except (TypeError, ValueError):
        body_chars = 0
    if body_chars <= 0:
        return {"status": "ignored", "reason": "zero_body_chars"}

    incarnation_id = _clean_text(event.get("incarnation_id"))
    activation_id = _clean_text(event.get("activation_id"))
    content_sha256 = _normalize_sha256(event.get("content_sha256"))
    resolved_task_key = canonical_task_key(
        task_key=_clean_text(event.get("task_key")),
        task_id=_clean_text(event.get("task_id")),
        request_id=_clean_text(event.get("request_id")),
    )
    if not incarnation_id or not activation_id or not content_sha256:
        return {"status": "ignored", "reason": "unbound_version"}
    if not resolved_task_key:
        return {"status": "ignored", "reason": "missing_task_key"}

    at = _normalize_timestamp(event.get("at"))
    event_epoch = _normalize_epoch(event.get("strike_epoch"))
    use_key = receipt_key(incarnation_id, activation_id, content_sha256, resolved_task_key)
    agg_key = aggregate_key(incarnation_id, activation_id, content_sha256)

    with runtime.transaction() as conn:
        agg = _normalized_aggregate(
            _read_metadata_json(conn, agg_key),
            incarnation_id=incarnation_id,
            activation_id=activation_id,
            content_sha256=content_sha256,
            fallback_at=at,
        )
        current_epoch = int(agg["strike_epoch"])
        effective_epoch = current_epoch if event_epoch is None else event_epoch
        existing_receipt = _normalized_receipt(_read_metadata_json(conn, use_key))

        if existing_receipt is None:
            seq = _next_sequence(conn)
            agg["independent_uses"] += 1
            agg["last_real_used_at"] = max(float(agg["last_real_used_at"]), at)
            agg["sequence"] = seq

            can_credit_post_epoch = (
                effective_epoch >= current_epoch
                and int(agg["hurt_count"]) < MAX_STRIKE_HURT_COUNT
            )
            if can_credit_post_epoch:
                agg["uses_since_hurt"] += 1
                counted_epoch = current_epoch
                status = "counted"
            else:
                counted_epoch = min(effective_epoch, current_epoch)
                status = "recorded_late"

            receipt_doc = {
                "last_counted_epoch": max(0, counted_epoch),
                "first_seen_at": at,
                "last_real_used_at": at,
                "last_sequence": seq,
            }
            _write_metadata_json(conn, use_key, receipt_doc)
            _write_metadata_json(conn, agg_key, agg)
            return {
                "status": status,
                "receipt_key": use_key,
                "aggregate_key": agg_key,
                "receipt": receipt_doc,
                "aggregate": dict(agg),
                "independent_uses_incremented": True,
                "uses_since_hurt_incremented": can_credit_post_epoch,
            }

        # 同任务重复触发：绝不再次增加 independent_uses；仅当进入更新轮次且尚未在该轮记过时才增加 uses_since_hurt
        last_counted_epoch = int(existing_receipt["last_counted_epoch"])
        can_credit_new_epoch = (
            effective_epoch >= current_epoch
            and last_counted_epoch < current_epoch
            and int(agg["hurt_count"]) < MAX_STRIKE_HURT_COUNT
        )
        should_refresh_ts = at > float(existing_receipt["last_real_used_at"])
        if not can_credit_new_epoch and not should_refresh_ts:
            return {
                "status": "duplicate",
                "receipt_key": use_key,
                "aggregate_key": agg_key,
                "receipt": dict(existing_receipt),
                "aggregate": dict(agg),
                "independent_uses_incremented": False,
                "uses_since_hurt_incremented": False,
            }

        seq = _next_sequence(conn)
        if can_credit_new_epoch:
            agg["uses_since_hurt"] += 1
            existing_receipt["last_counted_epoch"] = current_epoch
        existing_receipt["last_real_used_at"] = max(float(existing_receipt["last_real_used_at"]), at)
        existing_receipt["last_sequence"] = seq
        agg["last_real_used_at"] = max(float(agg["last_real_used_at"]), at)
        agg["sequence"] = max(int(agg["sequence"]), seq)
        _write_metadata_json(conn, use_key, existing_receipt)
        _write_metadata_json(conn, agg_key, agg)
        return {
            "status": "counted_new_epoch" if can_credit_new_epoch else "duplicate_refreshed",
            "receipt_key": use_key,
            "aggregate_key": agg_key,
            "receipt": dict(existing_receipt),
            "aggregate": dict(agg),
            "independent_uses_incremented": False,
            "uses_since_hurt_incremented": can_credit_new_epoch,
        }


def record_exact_negative(
    store: Any,
    *,
    incarnation_id: str,
    activation_id: str,
    content_sha256: str,
    root_task_id: str = "",
    task_id: str = "",
    request_id: str = "",
    receipt_id: str = "",
    at: float | None = None,
    runtime: RuntimeRepository,
) -> dict[str, Any]:
    """按 V2-2 任务级去重键原子记录单版本负向记次（3/4/5 阶梯累计到第 3 次）。"""
    del store
    inc = _clean_text(incarnation_id)
    act = _clean_text(activation_id)
    sha = _normalize_sha256(content_sha256)
    neg_key_text = canonical_negative_key(
        root_task_id=root_task_id,
        task_id=task_id,
        request_id=request_id,
        receipt_id=receipt_id,
    )
    if not inc or not act or not sha:
        return {
            "status": "rejected",
            "code": "INVALID_VERSION_BINDING",
            "reason": "Missing incarnation_id, activation_id, or valid content_sha256 for negative receipt.",
        }
    if not neg_key_text:
        return {
            "status": "rejected",
            "code": "MISSING_NEGATIVE_TASK_KEY",
            "reason": "Missing root_task_id, task_id, request_id, or receipt_id for V2-2 task dedup.",
        }

    neg_digest = _sha256_text(neg_key_text)
    neg_key = negative_receipt_key(inc, act, neg_digest, already_hashed=True)
    agg_key = aggregate_key(inc, act, sha)
    ts = _normalize_timestamp(at)

    with runtime.transaction() as conn:
        agg = _normalized_aggregate(
            _read_metadata_json(conn, agg_key),
            incarnation_id=inc,
            activation_id=act,
            content_sha256=sha,
            fallback_at=ts,
        )
        existing_neg = _read_metadata_json(conn, neg_key)
        if existing_neg is not None:
            return {
                "status": "duplicate",
                "receipt_key": neg_key,
                "receipt_digest": neg_digest,
                "canonical_negative_key": neg_key_text,
                "receipt": dict(existing_neg),
                "aggregate": dict(agg),
                "hurt_count": int(agg["hurt_count"]),
                "strike_epoch": int(agg["strike_epoch"]),
                "epoch_started_at": float(agg["epoch_started_at"]),
                "sequence": int(agg["sequence"]),
            }

        current_hurt = int(agg["hurt_count"])
        if current_hurt >= MAX_STRIKE_HURT_COUNT:
            return {
                "status": "already_retired",
                "receipt_key": neg_key,
                "receipt_digest": neg_digest,
                "canonical_negative_key": neg_key_text,
                "aggregate": dict(agg),
                "hurt_count": current_hurt,
                "strike_epoch": int(agg["strike_epoch"]),
                "epoch_started_at": float(agg["epoch_started_at"]),
                "sequence": int(agg["sequence"]),
            }

        new_hurt = current_hurt + 1
        new_epoch = int(agg["strike_epoch"]) + 1
        accepted_epoch = max(1, min(MAX_STRIKE_HURT_COUNT, new_hurt))
        seq = _next_sequence(conn)

        agg["hurt_count"] = new_hurt
        agg["strike_epoch"] = new_epoch
        agg["uses_since_hurt"] = 0
        agg["epoch_started_at"] = ts
        agg["sequence"] = max(int(agg["sequence"]), seq)

        neg_doc = {
            "incarnation_id": inc,
            "activation_id": act,
            "receipt_digest": neg_digest,
            "accepted_epoch": accepted_epoch,
            "at": ts,
            "sequence": seq,
        }
        _write_metadata_json(conn, neg_key, neg_doc)
        _write_metadata_json(conn, agg_key, agg)
        return {
            "status": "accepted",
            "receipt_key": neg_key,
            "receipt_digest": neg_digest,
            "canonical_negative_key": neg_key_text,
            "receipt": neg_doc,
            "aggregate": dict(agg),
            "hurt_count": new_hurt,
            "strike_epoch": new_epoch,
            "epoch_started_at": ts,
            "sequence": seq,
        }


def has_body_delivery_receipt(
    runtime: RuntimeRepository | None,
    *,
    incarnation_id: str,
    activation_id: str,
    content_sha256: str,
    task_key: str = "",
    root_task_id: str = "",
    task_id: str = "",
    request_id: str = "",
) -> bool:
    """通道 C 专用：仅通过主键点查确认当前任务是否存在正文实际送达的使用回执。"""
    if runtime is None:
        return False
    inc = _clean_text(incarnation_id)
    act = _clean_text(activation_id)
    sha = _normalize_sha256(content_sha256)
    if not inc or not act or not sha:
        return False
    candidates = candidate_delivery_task_keys(
        task_key=task_key,
        root_task_id=root_task_id,
        task_id=task_id,
        request_id=request_id,
    )
    if not candidates:
        return False
    keys = [receipt_key(inc, act, sha, item) for item in candidates]
    placeholders = ",".join("?" for _ in keys)
    with runtime.transaction() as conn:
        row = conn.execute(
            f"SELECT 1 FROM metadata WHERE key IN ({placeholders}) LIMIT 1",
            tuple(keys),
        ).fetchone()
    return row is not None


def read_usage_aggregate(
    runtime: RuntimeRepository | None,
    *,
    incarnation_id: str,
    activation_id: str,
    content_sha256: str,
) -> dict[str, Any] | None:
    """按主键读取单个版本组合的 UsageAggregate，不存在时返回 None。"""
    if runtime is None:
        return None
    inc = _clean_text(incarnation_id)
    act = _clean_text(activation_id)
    sha = _normalize_sha256(content_sha256)
    if not inc or not act or not sha:
        return None
    key = aggregate_key(inc, act, sha)
    with runtime.transaction() as conn:
        raw = _read_metadata_json(conn, key)
    if raw is None:
        return None
    return _normalized_aggregate(
        raw,
        incarnation_id=inc,
        activation_id=act,
        content_sha256=sha,
        fallback_at=0.0,
    )


def _pair_from_receipt_key(key: str, prefix: str, expected_tail_parts: int) -> tuple[str, str] | None:
    if not key.startswith(prefix):
        return None
    body = key[len(prefix) :]
    parts = body.rsplit(":", expected_tail_parts)
    if len(parts) < expected_tail_parts + 1:
        return None
    head = parts[0]
    head_parts = head.split(":", 1)
    if len(head_parts) != 2:
        return None
    inc, act = head_parts[0].strip(), head_parts[1].strip()
    if not inc or not act:
        return None
    return inc, act


def cleanup_retired_aggregates(
    runtime: RuntimeRepository | None,
    *,
    keep_pairs: Iterable[tuple[str, str]],
    after_entry: str = "",
    max_entries: int = DEFAULT_RECEIPT_CLEANUP_BATCH_ENTRIES,
    deadline: float | None = None,
) -> dict[str, Any]:
    """按游标分片清理已退役版本的明细回执，并保证最后才删除汇总锚点 `sm-use-total:v1:`。

    扫除顺序严格遵循：
    1) `sm-use:v1:`（使用明细） -> 2) `sm-hurt:v1:`（负向明细） -> 3) `sm-use-total:v1:`（汇总锚点）。
    """
    if runtime is None:
        return {
            "scanned": 0,
            "deleted": 0,
            "has_more": False,
            "after_entry": "",
            "complete": True,
        }

    protected = {(str(inc).strip(), str(act).strip()) for inc, act in keep_pairs if inc and act}
    stages: list[tuple[str, str, int]] = [
        (USE_RECEIPT_PREFIX, USE_RECEIPT_PREFIX_END, 2),
        (HURT_RECEIPT_PREFIX, HURT_RECEIPT_PREFIX_END, 1),
        (USE_AGGREGATE_PREFIX, USE_AGGREGATE_PREFIX_END, 1),
    ]

    start_stage = 0
    cursor_key = _clean_text(after_entry, max_chars=1024)
    if cursor_key:
        for idx, (prefix, _end, _tail) in enumerate(stages):
            if cursor_key.startswith(prefix):
                start_stage = idx
                break

    scanned_total = 0
    deleted_total = 0
    remaining = max(1, int(max_entries or DEFAULT_RECEIPT_CLEANUP_BATCH_ENTRIES))

    for stage_idx in range(start_stage, len(stages)):
        prefix, prefix_end, tail_parts = stages[stage_idx]
        exclusive = bool(stage_idx == start_stage and cursor_key.startswith(prefix) and len(cursor_key) > len(prefix))
        lower = cursor_key if exclusive else prefix
        while remaining > 0:
            if deadline is not None and time.monotonic() >= deadline:
                return {
                    "scanned": scanned_total,
                    "deleted": deleted_total,
                    "has_more": True,
                    "after_entry": lower if exclusive else prefix,
                    "complete": False,
                }
            batch_limit = min(remaining, DEFAULT_RECEIPT_CLEANUP_BATCH_ENTRIES)
            sql_op = ">" if exclusive else ">="
            with runtime.transaction() as conn:
                rows = conn.execute(
                    f"SELECT key, value FROM metadata WHERE key {sql_op} ? AND key < ? ORDER BY key LIMIT ?",
                    (lower, prefix_end, batch_limit),
                ).fetchall()
                if not rows:
                    break
                to_delete: list[str] = []
                last_key = ""
                for row in rows:
                    last_key = str(row["key"] if hasattr(row, "keys") else row[0])
                    scanned_total += 1
                    remaining -= 1
                    pair = _pair_from_receipt_key(last_key, prefix, tail_parts)
                    if pair is None and prefix == USE_AGGREGATE_PREFIX:
                        try:
                            val = json.loads(str(row["value"] if hasattr(row, "keys") else row[1]))
                            if isinstance(val, dict):
                                inc_v = str(val.get("incarnation_id") or "").strip()
                                act_v = str(val.get("activation_id") or "").strip()
                                if inc_v and act_v:
                                    pair = (inc_v, act_v)
                        except (TypeError, ValueError):
                            pair = None
                    if pair is not None and pair not in protected:
                        to_delete.append(last_key)
                if to_delete:
                    placeholders = ",".join("?" for _ in to_delete)
                    conn.execute(
                        f"DELETE FROM metadata WHERE key IN ({placeholders})",
                        tuple(to_delete),
                    )
                    deleted_total += len(to_delete)
                lower = last_key
                exclusive = True
                if len(rows) < batch_limit:
                    break
                if remaining <= 0 or (deadline is not None and time.monotonic() >= deadline):
                    return {
                        "scanned": scanned_total,
                        "deleted": deleted_total,
                        "has_more": True,
                        "after_entry": last_key,
                        "complete": False,
                    }
        cursor_key = ""

    return {
        "scanned": scanned_total,
        "deleted": deleted_total,
        "has_more": False,
        "after_entry": "",
        "complete": True,
    }
