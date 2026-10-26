# LLM: 白名单DTO是唯一公开形状；内部资产字典及原Scheduler运行正文永不外传。
# 模块用途: 裁剪资产概况、分页与聚合故障覆盖信息。
from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from hashlib import sha256

from ..common.nofollow_fs import read_bytes_beneath
from ..plugin_host.errors import HostAPIError
from ..plugin_host.owner_context import reject_constant, unique_object
from .access_policy import AuditGroupError
from .owner_catalog import source_error
from .readonly_store import ReadonlyAssetStore
from .safe_projection import parameters_schema, safe_text


# LLM: 数字计量只接受非负int，弱类型源视为损坏而非隐式变零。
# 函数用途: 验证资产已有运行次数。
def run_count(value):
    if type(value) is not int or value < 0:
        raise ValueError("invalid run count")
    return value


# LLM: schema和描述文本分别清洗，不复制metadata或任意源字段。
# 函数用途: 生成严格AssetSummary白名单DTO。
def asset_summary(owner, kind, raw):
    runtime = raw.get("runtime_summary", {})
    schema = raw.get("parameters_schema", raw.get("metadata", {}).get("parameters_schema", {}))
    enabled = raw.get("enabled", True)
    if type(enabled) is not bool or type(runtime) is not dict:
        raise ValueError("invalid asset summary")
    revision = raw.get("revision", 0)
    if not (type(revision) is int and revision >= 0 or type(revision) is str
            and re.fullmatch(r"(?:[0-9a-f]{64}|[0-9]+:[0-9]+)", revision)):
        raise ValueError("invalid revision")
    return {"owner_id": owner, "kind": kind, "id": safe_text(raw["id"], 128),
            "name": safe_text(raw.get("name", raw["id"])),
            "summary": safe_text(raw.get("summary", raw.get("description", ""))),
            "parameters_schema": parameters_schema(schema), "enabled": enabled,
            "revision": revision, "updated_at": safe_text(raw.get("updated_at", "")),
            "run_count": run_count(runtime.get("run_count", 0)), "last_run_at": timestamp(runtime.get("last_run_at")),
            "last_status": safe_text(runtime.get("last_status", ""), 128),
            "invocable": enabled and kind in {"script", "skill", "workflow", "monitor"}}


# LLM: 时间只消费结构化epoch或ISO字符串，不使用当前时间伪造运行证据。
# 函数用途: 统一公开时间格式。
def timestamp(value):
    if value in (None, "", 0):
        return ""
    if type(value) in (int, float):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    return safe_text(value, 80)


# LLM: 对每个来源/种类和单资产分别记录错误，已读到的合法条目仍保留，不初始化源Store。
# 函数用途: 合并指定owner与种类的只读资产摘要。
def collect_assets(root, owners, kinds):
    rows, errors = [], []
    for owner in owners:
        reader = ReadonlyAssetStore(root, owner)
        for kind in kinds:
            try:
                entries = reader.entries(kind)
            except (OSError, ValueError, KeyError, TypeError, HostAPIError):
                errors.append(source_error(owner, kind))
                continue
            for identifier, parts in entries:
                try:
                    raw = reader.get(kind, identifier, skill_parts=parts)
                    rows.append(asset_summary(owner, kind, raw))
                except (OSError, ValueError, KeyError, TypeError, HostAPIError, RecursionError):
                    errors.append(source_error(owner, kind))
    return sorted(rows, key=lambda row: (row["owner_id"], row["kind"], row["id"])), errors


# LLM: 告警只投影原Scheduler已确认的失败/unknown终态；response/error_message/structured_result不回显。
# 函数用途: 将异常运行转换为有界的异常摘要，不把提示词当告警。
def alert_summary(owner, row):
    if row.get("status") not in {"failed", "unknown"}:
        return None
    return {"owner_id": owner, "id": safe_text(row["run_id"], 128), "asset_id": safe_text(row.get("job_id", ""), 128),
            "kind": "monitor", "severity": "error", "status": "open", "summary": safe_text(row.get("error_code", ""), 256),
            "created_at": timestamp(row.get("finished_at", row.get("created_at"))), "source": "scheduler_runs"}


# LLM: 原store与history合并按run_id去重；坏来源保留显式错误，不借构造Repository产生锁或目录。
# 函数用途: 读取监控异常流水，限定文件大小及记录数。
def collect_alerts(root, owners):
    rows, errors = {}, []
    for owner in owners:
        reader = ReadonlyAssetStore(root, owner)
        try:
            store = reader.scheduler()
            raw = read_bytes_beneath(reader.root, (*reader.parts, "data", "scheduler", "history.jsonl"),
                                     max_bytes=4 * 1024 * 1024, require_dir_fd=True)
            history = [json.loads(line, object_pairs_hook=unique_object, parse_constant=reject_constant)
                       for line in (raw or b"").decode("utf-8").split("\n") if line.strip()]
            runs = [*history, *store.get("runs", {}).values()]
            if len(runs) > 10000:
                raise ValueError("alert limit")
            for run in runs:
                summary = alert_summary(owner, run)
                if summary is not None:
                    rows[(owner, summary["id"])] = summary
        except (OSError, ValueError, TypeError, KeyError, HostAPIError, RecursionError):
            errors.append(source_error(owner, "scheduler_runs"))
    return sorted(rows.values(), key=lambda row: (row["created_at"], row["owner_id"], row["id"]), reverse=True), errors


# LLM: cursor只承载查询指纹与位置，不放路径、源码或任意JSON；换筛选条件必须重新分页。
# 函数用途: 返回带覆盖、时间戳和来源错误的有界结果页。
def page(rows, cursor, limit, errors, *, query):
    if type(limit) is not int or not 1 <= limit <= 200 or type(cursor) is not str or len(cursor) > 256:
        raise AuditGroupError("INVALID_PAGINATION", status_code=400)
    fingerprint = sha256(json.dumps(query, sort_keys=True).encode()).hexdigest()[:16]
    try:
        prefix, index = base64.urlsafe_b64decode(cursor.encode()).decode().split(":") if cursor else (fingerprint, "0")
        offset = int(index)
        if prefix != fingerprint or offset < 0:
            raise ValueError()
    except (ValueError, UnicodeError):
        raise AuditGroupError("INVALID_CURSOR", status_code=400) from None
    end = offset + limit
    next_cursor = base64.urlsafe_b64encode(f"{fingerprint}:{end}".encode()).decode() if end < len(rows) else ""
    return {"items": rows[offset:end], "next_cursor": next_cursor,
            "coverage": "partial" if errors else "complete", "source_errors": errors,
            "as_of": datetime.now(timezone.utc).isoformat()}
