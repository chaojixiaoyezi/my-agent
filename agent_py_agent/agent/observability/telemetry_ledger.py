# LLM: 权威 JSONL 只追加，使用流式分页而非丢历史的轮转；owner_home 必须来自宿主。
# 模块用途: 保存脱敏工具调用，提供查询、时间过滤与工具耗时统计。
from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field
from itertools import islice
from pathlib import Path
from typing import Any

from agent_py_agent.agent.common.directory_lock import locked_private_directory
from agent_py_agent.agent.common.nofollow_fs import (
    append_text_beneath,
    ensure_private_dir,
    open_readonly_file_beneath,
)

from ..common.log_redaction import redact_sensitive_value
from ..workflows.models import redact_bearer_values

SCHEMA = "tool_call_telemetry.v1"
PARTS = ("data", "telemetry")


# LLM: 结构化秘密整值替换，字符串先用公共规则脱敏再截断；未知对象不调用 repr。
# 函数用途: 生成有限且可序列化的参数摘要。
def _sanitize(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {str(k)[:200]: "[REDACTED]" if _sensitive(str(k), v) else _sanitize(v, depth + 1)
                for k, v in islice(value.items(), 50)}
    if isinstance(value, (list, tuple)):
        return [_sanitize(v, depth + 1) for v in value[:50]]
    if isinstance(value, str):
        return redact_bearer_values(value)[:200]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return "[UNSUPPORTED]"


# LLM: 仅明确命名的整数 Token 计数豁免，真实 token 即使数字也遮蔽；保留 apiKey 兼容。
# 函数用途: 判断字段名是否需要整值脱敏。
def _sensitive(key, value):
    normalized = key.lower().replace("_", "").replace("-", "")
    if type(value) is int and normalized in {"maxtokens", "inputtokens", "outputtokens", "prompttokens", "completiontokens", "cachedtokens", "totaltokens", "tokencount"}:
        return False
    return any(k in normalized for k in ("apikey", "token", "password", "secret", "authorization", "cookie"))


# LLM: 非有限数和负数在持久化之前拒绝，不静默纠正遥测事实。
# 函数用途: 校验非负数值。
def _number(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise ValueError("expected finite nonnegative number")
    return value


# LLM: 创建子目录全程锚定规范 owner；锁在所有实例和进程中使用同一个名字。
# 函数用途: 返回写账临界区，不在构造或查询阶段创建目录。
def _lock(home):
    ensure_private_dir(home / "data" / "telemetry", canonical_root=home)
    return locked_private_directory(home, relative_parts=PARTS, lock_name=".telemetry.lock")


# LLM: 禁止 NaN 持久化，字符串统一 UTF-8；不会自动脱敏非参数业务字段。
# 函数用途: 生成单行 JSON 文本。
def _encode(row):
    return json.dumps(row, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n"


# LLM: 无文件返回空迭代，坏行显式抛错而非悄悄漏计；读 FD 锚定 no-follow 路径。
# 函数用途: 逐行读取账本，内存不随文件大小增长。
def _entries(home):
    try:
        descriptor = open_readonly_file_beneath(home, (*PARTS, "tool_calls.jsonl"))
    except FileNotFoundError:
        return
    with os.fdopen(descriptor, encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if not isinstance(row, dict) or row.get("schema") != SCHEMA:
                raise ValueError("invalid telemetry record")
            yield row


# LLM: 筛选键是调用方合同，拼错拒绝；时间按 started_at 闭区间。
# 函数用途: 判断一条记录是否符合条件。
def _matches(row, filters):
    for key, expected in filters.items():
        if expected is None or expected == "":
            continue
        if key in {"since", "until", "min_duration_ms"}:
            actual = row["duration_ms" if key == "min_duration_ms" else "started_at"]
            if (actual > _number(expected)) if key == "until" else (actual < _number(expected)):
                return False
        elif row.get(key) != expected:
            return False
    return True


# LLM: cursor 是过滤后的零基偏移，查询保持追加顺序；非法分页值显式拒绝。
# 函数用途: 从迭代器读取一个有界结果页。
def _page(rows, limit, cursor):
    if type(limit) is not int or type(cursor) is not int or min(limit, cursor) < 0:
        raise ValueError("invalid pagination")
    return list(islice(rows, cursor, cursor + limit))


# LLM: nearest-rank 百分位用于完整保留的调用；非 ok 全部算失败，成功率为 0~1。
# 函数用途: 计算单工具统计。
def _stats(rows):
    durations = sorted(r["duration_ms"] for r in rows)
    total = len(rows)
    ok = sum(r["status"] == "ok" for r in rows)
    errors = {}
    for row in rows:
        if row["error_code"]:
            errors[row["error_code"]] = errors.get(row["error_code"], 0) + 1
    return dict(calls=total, succeeded=ok, failed=total - ok, success_rate=ok / total,
                avg_duration_ms=sum(durations) / total, p50_duration_ms=durations[math.ceil(total * .5) - 1],
                p95_duration_ms=durations[math.ceil(total * .95) - 1], max_duration_ms=durations[-1],
                last_called_at=max(r["started_at"] for r in rows), error_codes=errors)


# LLM: 字段保留工具调用的结构化关联身份；schema 固定，duration_ms 可由起止补算。
# 类用途: 单次工具调用记录。
@dataclass
class ToolCallRecord:
    call_id: str
    tool_name: str
    owner_id: str = ""
    task_id: str = ""
    run_id: str = ""
    parent_run_id: str = ""
    root_run_id: str = ""
    workflow_run_id: str = ""
    step_id: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    duration_ms: float = 0.0
    status: str = "ok"
    error_code: str = ""
    args_summary: dict = field(default_factory=dict)
    output_bytes: int = 0
    artifact_ref: str = ""

    # LLM: 返回独立快照，调用方修改不会回写账本。
    # 函数用途: 输出带版本的字典。
    def to_dict(self):
        return {"schema": SCHEMA, **asdict(self)}


# LLM: 路径由可信调用方提供；类本身不接线执行器，不授予访问其他 owner 的权限。
# 类用途: 工具调用明细账本。
class ToolTelemetryLedger:
    # LLM: 构造不产生任何文件，空 owner_id 表示单 home 范围，不跨目录发现。
    # 函数用途: 绑定存储位置和可选 owner 身份。
    def __init__(self, owner_home, *, owner_id=""):
        self._home = Path(owner_home).absolute()
        self._owner_id = owner_id

    # LLM: 不修改输入对象；owner 冲突、非法状态和数值写前拒绝，写入使用进程锁。
    # 函数用途: 规范化并耐久追加调用记录。
    def record_tool_call(self, record):
        payload = record.to_dict() if isinstance(record, ToolCallRecord) else dict(record)
        if payload.pop("schema", SCHEMA) != SCHEMA:
            raise ValueError("unsupported schema")
        item = ToolCallRecord(**payload)
        if not isinstance(item.call_id, str) or not item.call_id or not isinstance(item.tool_name, str) or not item.tool_name:
            raise ValueError("call_id and tool_name required")
        if self._owner_id and item.owner_id not in ("", self._owner_id):
            raise ValueError("owner mismatch")
        item.owner_id = item.owner_id or self._owner_id
        if item.status not in {"ok", "error", "denied", "timeout"}:
            raise ValueError("invalid status")
        for value in (item.started_at, item.ended_at, item.duration_ms, item.output_bytes):
            _number(value)
        if item.ended_at < item.started_at or type(item.output_bytes) is not int:
            raise ValueError("invalid timing or byte count")
        item.duration_ms = round(item.duration_ms or (item.ended_at - item.started_at) * 1000, 3)
        if not isinstance(item.args_summary, dict):
            raise ValueError("args_summary must be a dict")
        item.args_summary = _sanitize(item.args_summary)
        line = _encode(item.to_dict())
        with _lock(self._home):
            append_text_beneath(self._home, (*PARTS, "tool_calls.jsonl"), line)
        return item

    # LLM: 支持任务规定的关键字过滤；通过 kwargs 保持单函数参数数量不超过六个。
    # 函数用途: 返回过滤后按追加顺序排列的一页字典。
    def query_tool_calls(self, *, limit=100, cursor=0, **filters):
        allowed = {"task_id", "run_id", "workflow_run_id", "tool_name", "status", "min_duration_ms", "since", "until"}
        if filters.keys() - allowed:
            raise ValueError("unknown filter")
        rows = (r for r in _entries(self._home) if (not self._owner_id or r["owner_id"] == self._owner_id) and _matches(r, filters))
        return _page(rows, limit, cursor)

    # LLM: 重复 call_id 按最后一条返回；完整历史仍保留用于审计。
    # 函数用途: 获取单次调用明细。
    def get_tool_call(self, call_id):
        result = None
        for row in _entries(self._home):
            if row["call_id"] == call_id and (not self._owner_id or row["owner_id"] == self._owner_id):
                result = row
        return result

    # LLM: 聚合保留全部历史，精确分位需要内存保存耗时；不设隐式保留窗口。
    # 函数用途: 按工具名汇总调用统计。
    def aggregate_tool_stats(self, *, since=None, until=None):
        groups = {}
        for row in _entries(self._home):
            if (not self._owner_id or row["owner_id"] == self._owner_id) and _matches(row, dict(since=since, until=until)):
                groups.setdefault(row["tool_name"], []).append(row)
        return {"schema": "tool_telemetry_stats.v1", "total_calls": sum(map(len, groups.values())),
                "tools": {name: _stats(rows) for name, rows in sorted(groups.items())}}
