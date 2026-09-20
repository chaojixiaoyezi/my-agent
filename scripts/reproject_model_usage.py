#!/usr/bin/env python3
# LLM: 本脚本只读历史用量并生成展示投影；双来源计数保持零值，修改时同步用量重投影回归。
# 模块用途: 核对旧账本计数，不修改 canonical 记录、不调用模型或网络。
"""模块用途: 把历史 model_usage 账本按"协议归一"口径重算成只读展示投影。

这个脚本只回答一个问题：**旧账本里的 accounted_input_tokens 在今天的新口径下应该是多少**。
历史账本的 accounted_input_tokens 出自旧启发式（input_tokens → prompt_tokens →
cache_creation_input_tokens 取第一个非空值），Anthropic 兼容会话的缓存命中/缓存写入没有计入，
所以历史总量偏小；一旦某轮供应商返回 input_tokens=0，旧口径甚至会把整轮输入记成 0。
脚本因此同时输出两份事实：账本原值（legacy_*，原样带出、绝不覆盖）与重算投影（reprojected_*），
并按证据强度标 exact / partial / incomplete。

安全边界（改动时必须保持）：
- 只读：只以文本方式打开账本，不写任何文件、不改仓库其它文件、不联网、不调用模型。
- 只搬运计数与协议标签，不打印会话正文；账本本身也只有计数。
- legacy_* 一律是账本原值，用来对照，不参与任何覆盖写。

LLM: 改本脚本时必须守住三条契约——
(1) 不引入任何写文件/网络/子进程副作用；
(2) confidence 只能依据账本里真实存在的结构化字段（backends / models / 分栏字段 / 账本投影标记），
    不许用文件名、会话名或模型名猜协议；
(3) 重算值必须走产品同一条 usage.normalize_usage 协议语义（本脚本只是把账本分栏装回供应商形状），
    不能在这里另写一份"取第一个非空字段"的启发式。
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _ensure_repo_root_on_path() -> Path:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    return repo_root


_REPO_ROOT = _ensure_repo_root_on_path()

from agent_py_agent.agent.agent_core.model.usage import normalize_usage

SCHEMA = "model_usage_reprojection.v1"
LEDGER_SCHEMA = "model_call_summary.v1"

CONFIDENCE_EXACT = "exact"
CONFIDENCE_PARTIAL = "partial"
CONFIDENCE_INCOMPLETE = "incomplete"
# 文件级置信度取最弱的一行：incomplete 会传染，因为它就是"这份文件里存在不可归属的账"。
_CONFIDENCE_RANK = {
    CONFIDENCE_EXACT: 0,
    CONFIDENCE_PARTIAL: 1,
    CONFIDENCE_INCOMPLETE: 2,
}

PROTOCOL_UNKNOWN = "unknown"
PROTOCOL_MIXED = "mixed"
ANTHROPIC = "anthropic_compatible"
OPENAI_COMPATIBLE = "openai_compatible"
OPENAI_RESPONSES = "openai_responses"

# LLM: 只有协议族标签能决定"缓存是否已含在输入总数里"；文件名、模型名不能。标签按语义推导
# 而不是封闭枚举，遇到没见过的标签单独留证并把这一行降级（"无法归属"），宁可标不完整，不许猜。
_ANTHROPIC_MARKER = "anthropic"
_OPENAI_MARKER = "openai"
_RESPONSES_MARKER = "responses"
# echo/base 是仓库内声明的非供应商后端（不产生真实计费用量），单独识别为"无协议含义"。
_NON_PROTOCOL_BACKENDS = frozenset({"base", "echo"})


# LLM: 账本里没有供应商原始 usage，只有已经聚合好的分栏计数，所以重算只能"按协议反推形状"：
# 把同一组计数分别装成 Anthropic / OpenAI 兼容 / OpenAI Responses 三种形状，交给产品同一条
# normalize_usage 求值。这样工具与新链路口径永远一致，不会各自维护一份协议知识。
# 类用途: 给 normalize_usage 提供最小只读载体（usage 映射 + 空正文），不参与序列化。
class _SyntheticResponse:
    # LLM: normalize_usage 走属性访问（response.usage），不接受裸 dict，因此必须包一层对象。
    # 函数用途: 把账本分栏计数包成"像一次供应商响应"的对象，仅供归一函数读取。
    def __init__(self, usage: dict[str, int]) -> None:
        self.usage = usage
        self.text = ""


# LLM: 一行账本的原始事实；所有字段都可能缺失（老 schema / 手工修补），缺失一律保留 None，
# 由投影层决定是 partial 还是 incomplete，绝不在解析层擅自补 0。
# 类用途: 保存一条 model_call_summary 记录里与用量重算有关的结构化字段。
@dataclass(frozen=True)
class LedgerRow:
    index: int
    event_id: str
    recorded_schema: str
    backends: tuple[str, ...]
    models: tuple[str, ...]
    accounted_input_tokens: int | None
    output_tokens: int | None
    cache_read_input_tokens: int | None
    cache_write_input_tokens: int | None
    ledger_total_tokens: int | None
    provider_input_tokens: int | None
    provider_cache_read_input_tokens: int | None
    provider_cache_write_input_tokens: int | None
    estimated_input_tokens: int | None
    provider_call_count: int | None
    estimated_call_count: int | None
    has_usage_breakdown: bool
    incremental_snapshot: bool


# LLM: 一行重算结果同时带原值、重算值与判定理由；high 只在协议无法归属（incomplete）时出现，
# 用来表达"下限..上限"的诚实区间，而不是给一个假装精确的单一数字。
# 类用途: 保存单行账本的重算投影，供文件级求和与 JSON 输出。
@dataclass(frozen=True)
class RowProjection:
    index: int
    event_id: str
    protocol: str
    confidence: str
    basis: str
    backends: tuple[str, ...]
    models: tuple[str, ...]
    legacy_input_tokens: int
    legacy_total_tokens: int
    cache_read_input_tokens: int
    cache_write_input_tokens: int
    output_tokens: int
    reprojected_input_tokens: int
    reprojected_input_tokens_high: int | None
    reprojected_total_tokens: int
    reprojected_total_tokens_high: int | None
    notes: tuple[str, ...]

    # LLM: JSON 是机器可读面，字段名即契约；row_index/event_id 是唯一的行身份，不含正文。
    # 函数用途: 把一行重算结果转成可序列化字典。
    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "row_index": self.index,
            "event_id": self.event_id,
            "protocol": self.protocol,
            "confidence": self.confidence,
            "reprojection_basis": self.basis,
            "backends": list(self.backends),
            "models": list(self.models),
            "legacy_input_tokens": self.legacy_input_tokens,
            "legacy_total_tokens": self.legacy_total_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_write_input_tokens": self.cache_write_input_tokens,
            "output_tokens": self.output_tokens,
            "reprojected_input_tokens": self.reprojected_input_tokens,
            "reprojected_total_tokens": self.reprojected_total_tokens,
            "notes": list(self.notes),
        }
        if self.reprojected_input_tokens_high is not None:
            payload["reprojected_input_tokens_high"] = self.reprojected_input_tokens_high
            payload["reprojected_total_tokens_high"] = self.reprojected_total_tokens_high
        return payload


# LLM: 文件级投影是"若干行求和 + 最弱置信度 + 结构化理由"，不做任何跨行协议猜测；
# incomplete 的文件必须在展示层显著标注，否则读者会把它当精确值。
# 类用途: 保存一个账本文件的重算投影及其求和结果。
@dataclass(frozen=True)
class FileProjection:
    path: str
    protocol: str
    confidence: str
    legacy_input_tokens: int
    legacy_total_tokens: int
    reprojected_input_tokens: int
    reprojected_total_tokens: int
    reprojected_input_tokens_high: int | None
    reprojected_total_tokens_high: int | None
    delta_tokens: int
    notes: tuple[str, ...]
    rows: tuple[RowProjection, ...] = ()
    skipped_records: int = 0
    unparsable_lines: int = 0

    # LLM: 文件级输出字段名与任务约定一致；rows 永远是明细证据，summary 字段永远可直接比较。
    # 函数用途: 把一个文件的重算投影转成可序列化字典（含逐行明细）。
    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": self.path,
            "protocol": self.protocol,
            "confidence": self.confidence,
            "legacy_input_tokens": self.legacy_input_tokens,
            "legacy_total_tokens": self.legacy_total_tokens,
            "reprojected_input_tokens": self.reprojected_input_tokens,
            "reprojected_total_tokens": self.reprojected_total_tokens,
            "delta_tokens": self.delta_tokens,
            "row_count": len(self.rows),
            "skipped_records": self.skipped_records,
            "unparsable_lines": self.unparsable_lines,
            "notes": list(self.notes),
            "rows": [row.to_dict() for row in self.rows],
        }
        if self.reprojected_input_tokens_high is not None:
            payload["reprojected_input_tokens_high"] = self.reprojected_input_tokens_high
            payload["reprojected_total_tokens_high"] = self.reprojected_total_tokens_high
        return payload


# 函数用途: 把账本里的一个 backend 标签归类到协议族；认不出的标签返回 unknown，不猜协议。
def protocol_for_backend(label: object) -> str:
    name = str(label or "").strip().lower()
    if not name or name in _NON_PROTOCOL_BACKENDS:
        return ""
    if _ANTHROPIC_MARKER in name:
        return ANTHROPIC
    if _OPENAI_MARKER in name:
        return OPENAI_RESPONSES if _RESPONSES_MARKER in name else OPENAI_COMPATIBLE
    return PROTOCOL_UNKNOWN


# LLM: 一行的 backends 是"这一行增量里出现过的后端集合"，不是每次调用的归属表；因此集合里只剩
# 一个协议族时，这一行的所有调用都属于该协议（可以精确重算）；出现两个以上协议族时，
# 分栏计数无法按协议拆开，只能给出区间并标 incomplete。
# 函数用途: 由一行账本的 backend 集合判定协议，并返回未知标签供说明使用。
def classify_backends(backends: tuple[str, ...]) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    protocols: set[str] = set()
    unknown_labels: list[str] = []
    for label in backends:
        protocol = protocol_for_backend(label)
        if protocol == PROTOCOL_UNKNOWN:
            # 认不出的标签既不等于"没有协议信息"，也不能当已知协议用；单独留证并降级。
            unknown_labels.append(str(label).strip())
        elif protocol:
            protocols.add(protocol)
    ordered = tuple(sorted(protocols))
    labels = tuple(unknown_labels)
    if not ordered:
        # 空集合或 echo/base 这类非供应商后端：没有协议证据 → unknown。
        return PROTOCOL_UNKNOWN, ordered, labels
    if len(ordered) == 1 and not labels:
        return ordered[0], ordered, labels
    # 多个已知协议，或"已知协议 + 认不出的标签"：这一行的分栏无法整体归属到单一协议。
    return PROTOCOL_MIXED, ordered, labels


# 函数用途: 按协议把账本分栏装回供应商形状，用主链路的 normalize_usage 算出"本次总输入"。
def protocol_input_tokens(
    protocol: str,
    accounted: int,
    cache_read: int,
    cache_write: int,
    output: int,
) -> int:
    if protocol == ANTHROPIC:
        usage = {
            "input_tokens": accounted,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write,
            "output_tokens": output,
        }
    elif protocol == OPENAI_RESPONSES:
        usage = {
            "input_tokens": accounted,
            "input_tokens_details": {"cached_tokens": cache_read},
            "output_tokens": output,
        }
    elif protocol == OPENAI_COMPATIBLE:
        usage = {
            "prompt_tokens": accounted,
            "prompt_tokens_details": {"cached_tokens": cache_read},
            "output_tokens": output,
        }
    else:
        raise ValueError(f"unsupported protocol for reprojection: {protocol}")
    return normalize_usage(_SyntheticResponse(usage)).input_tokens


# 函数用途: 宽松解析账本里的整数字段；缺失/非数字一律返回 None，不补 0。
def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# 函数用途: 从 model_calls 里取出 provider/estimated 分栏字典，缺失时返回空字典。
def _breakdown_bucket(calls: dict[str, Any], name: str) -> dict[str, Any]:
    breakdown = calls.get("usage_breakdown")
    if not isinstance(breakdown, dict):
        return {}
    bucket = breakdown.get(name)
    return bucket if isinstance(bucket, dict) else {}


# 函数用途: 只接受列表/元组里的非空字符串，避免畸形字段被逐字符拆成后端标签。
def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in (str(entry).strip() for entry in value) if item)


# 函数用途: 把一条账本记录解析成 LedgerRow；无法识别的记录返回 None（由文件层计入 skipped）。
def row_from_record(index: int, record: dict[str, Any]) -> LedgerRow | None:
    calls = record.get("model_calls")
    if not isinstance(calls, dict) or not calls:
        return None
    provider = _breakdown_bucket(calls, "provider")
    estimated = _breakdown_bucket(calls, "estimated")
    projection = calls.get("ledger_projection")
    projection = projection if isinstance(projection, dict) else {}
    return LedgerRow(
        index=index,
        event_id=str(record.get("event_id") or ""),
        recorded_schema=str(calls.get("schema") or ""),
        backends=_string_tuple(calls.get("backends")),
        models=_string_tuple(calls.get("models")),
        accounted_input_tokens=_optional_int(calls.get("accounted_input_tokens")),
        output_tokens=_optional_int(calls.get("output_tokens")),
        cache_read_input_tokens=_optional_int(calls.get("cached_input_tokens")),
        cache_write_input_tokens=_optional_int(calls.get("cache_creation_input_tokens")),
        ledger_total_tokens=_optional_int(calls.get("total_tokens")),
        provider_input_tokens=_optional_int(provider.get("input_tokens")),
        provider_cache_read_input_tokens=_optional_int(provider.get("cache_read_input_tokens")),
        provider_cache_write_input_tokens=_optional_int(provider.get("cache_write_input_tokens")),
        estimated_input_tokens=_optional_int(estimated.get("input_tokens")),
        provider_call_count=_optional_int(calls.get("provider_usage_call_count")),
        estimated_call_count=_optional_int(calls.get("estimated_usage_call_count")),
        has_usage_breakdown=isinstance(calls.get("usage_breakdown"), dict),
        incremental_snapshot=str(projection.get("kind") or "") == "cumulative_snapshot_delta",
    )


# LLM: 用量重投影只在顶层与 provider 分栏两个来源间选择；零是有效计数，不能按真假值切换来源。
# 函数用途: 优先读取顶层计数，缺失时读取分栏；只返回值，不修改原账本。
def _first_known(primary: int | None, provider: int | None) -> int | None:
    return primary if primary is not None else provider


# LLM: 顶层 accounted 含本地估算调用，provider 分栏只含供应商真值；重算目标是"这份账的累计口径"，
# 所以优先顶层，顶层缺失时才用 provider + estimated 相加兜底。
# 函数用途: 取出一行账本用于重算的四个计数（输入/输出/缓存读/缓存写）。
def row_counters(row: LedgerRow) -> tuple[int, int, int, int, list[str]]:
    missing: list[str] = []
    accounted = row.accounted_input_tokens
    if accounted is None:
        if row.provider_input_tokens is None and row.estimated_input_tokens is None:
            missing.append("accounted_input_tokens")
        else:
            accounted = (row.provider_input_tokens or 0) + (row.estimated_input_tokens or 0)
    cache_read = _first_known(row.cache_read_input_tokens, row.provider_cache_read_input_tokens)
    if cache_read is None:
        missing.append("cached_input_tokens")
    cache_write = _first_known(row.cache_write_input_tokens, row.provider_cache_write_input_tokens)
    if cache_write is None:
        missing.append("cache_creation_input_tokens")
    output = row.output_tokens
    if output is None:
        missing.append("output_tokens")
    if not row.has_usage_breakdown:
        missing.append("usage_breakdown_partition")
    return accounted or 0, output or 0, cache_read or 0, cache_write or 0, missing


# 函数用途: 根据协议与分栏事实给出这一行的置信度，并补齐结构化判定理由。
def row_confidence(
    row: LedgerRow,
    protocol: str,
    counters: tuple[int, int, int, int],
    missing: list[str],
    notes: list[str],
) -> str:
    accounted, _output, cache_read, cache_write = counters
    estimated_calls = row.estimated_call_count or 0
    confidence = CONFIDENCE_EXACT
    if missing:
        notes.append("missing_columns=" + ",".join(missing))
        confidence = CONFIDENCE_PARTIAL
    if estimated_calls > 0:
        # 本地估算不是供应商真值：可以展示，但不能当精确值。
        notes.append(f"estimated_usage_calls={estimated_calls}")
        confidence = CONFIDENCE_PARTIAL
    if protocol in (PROTOCOL_UNKNOWN, PROTOCOL_MIXED):
        # 协议定不下来 → 不知道缓存读是否已含在总数里 → 不可归属，只能给区间。
        notes.append("protocol_not_attributable")
        return CONFIDENCE_INCOMPLETE
    if protocol == ANTHROPIC and cache_write > 0 and accounted == cache_write:
        # 旧启发式在两个字段都缺失时会退回 cache_creation_input_tokens；数值恰好相等时无法区分
        # "未缓存输入"与"缓存写入"，再加一次就可能重复计数，因此降级为 partial。
        notes.append("ambiguous_accounted_may_equal_cache_write")
        confidence = CONFIDENCE_PARTIAL
    if protocol in (OPENAI_COMPATIBLE, OPENAI_RESPONSES) and cache_read > accounted:
        # 总数已含缓存明细却小于明细本身：账本自相矛盾，只能标 partial。
        notes.append("cache_read_exceeds_inclusive_input")
        confidence = CONFIDENCE_PARTIAL
    return confidence


# LLM: 单行重算的唯一入口。顺序是"先看有没有用量 → 再看协议能不能定 → 最后看分栏全不全"，
# 因为零用量行在任何协议下都精确为 0，不该被协议未知拖成 incomplete。
# 函数用途: 把一行账本投影成"账本原值 + 重算值 + 置信度 + 理由"。
def project_row(row: LedgerRow) -> RowProjection:
    accounted, output, cache_read, cache_write, missing = row_counters(row)
    counters = (accounted, output, cache_read, cache_write)
    protocol, protocols, unknown_labels = classify_backends(row.backends)
    notes: list[str] = []
    if len(protocols) > 1:
        notes.append("mixed_protocol_row=" + ",".join(protocols))
    if unknown_labels:
        notes.append("unknown_backend_labels=" + ",".join(unknown_labels))
    if row.recorded_schema and row.recorded_schema != LEDGER_SCHEMA:
        notes.append(f"unexpected_schema={row.recorded_schema}")
    if row.incremental_snapshot:
        notes.append("incremental_snapshot_delta_row")

    legacy_total = row.ledger_total_tokens
    if legacy_total is None:
        legacy_total = accounted + output
        notes.append("ledger_total_tokens_missing")

    # 零用量行在任何协议下都精确为 0；但"字段缺失导致的 0"不是真零，必须继续走分栏判定。
    if accounted == 0 and cache_read == 0 and cache_write == 0 and output == 0 and not missing:
        return RowProjection(
            index=row.index,
            event_id=row.event_id,
            protocol=protocol,
            confidence=CONFIDENCE_EXACT,
            basis="zero_usage_row",
            backends=row.backends,
            models=row.models,
            legacy_input_tokens=accounted,
            legacy_total_tokens=legacy_total,
            cache_read_input_tokens=cache_read,
            cache_write_input_tokens=cache_write,
            output_tokens=output,
            reprojected_input_tokens=0,
            reprojected_input_tokens_high=None,
            reprojected_total_tokens=0,
            reprojected_total_tokens_high=None,
            notes=tuple(notes + ["zero_usage_row"]),
        )

    confidence = row_confidence(row, protocol, counters, missing, notes)
    inclusive_input = protocol_input_tokens(OPENAI_COMPATIBLE, accounted, cache_read, cache_write, output)
    exclusive_input = protocol_input_tokens(ANTHROPIC, accounted, cache_read, cache_write, output)
    high: int | None
    if confidence == CONFIDENCE_INCOMPLETE:
        # 协议不可归属：下限 = "缓存已含在总数里"，上限 = "缓存与总数互斥"，两者都给出来。
        reprojected = min(inclusive_input, exclusive_input)
        high = max(inclusive_input, exclusive_input)
        basis = "unattributable_protocol_bounds"
        if high == reprojected:
            notes.append("bounds_coincide_no_cache_columns")
    elif protocol == ANTHROPIC:
        reprojected = exclusive_input
        high = None
        basis = "anthropic_exclusive_cache_added"
        if accounted == 0 and cache_read > 0:
            notes.append("legacy_dropped_cache_read")
    else:
        reprojected = inclusive_input
        high = None
        basis = f"{protocol}_inclusive_cache_kept"
    return RowProjection(
        index=row.index,
        event_id=row.event_id,
        protocol=protocol,
        confidence=confidence,
        basis=basis,
        backends=row.backends,
        models=row.models,
        legacy_input_tokens=accounted,
        legacy_total_tokens=legacy_total,
        cache_read_input_tokens=cache_read,
        cache_write_input_tokens=cache_write,
        output_tokens=output,
        reprojected_input_tokens=reprojected,
        reprojected_input_tokens_high=high,
        reprojected_total_tokens=reprojected + output,
        reprojected_total_tokens_high=None if high is None else high + output,
        notes=tuple(notes),
    )


# 函数用途: 汇总若干行的计数与理由，供文件级投影使用（不做跨行协议猜测）。
def _aggregate_rows(rows: tuple[RowProjection, ...]) -> dict[str, Any]:
    protocol_counts: dict[str, int] = {}
    basis_counts: dict[str, int] = {}
    note_counts: dict[str, int] = {}
    models: set[str] = set()
    confidence = CONFIDENCE_EXACT
    legacy_input = legacy_total = reprojected = reprojected_total = 0
    high_input = high_total = 0
    has_high = False
    for row in rows:
        protocol_counts[row.protocol] = protocol_counts.get(row.protocol, 0) + 1
        basis_counts[row.basis] = basis_counts.get(row.basis, 0) + 1
        models.update(row.models)
        for note in row.notes:
            key = note.split("=", 1)[0]
            note_counts[key] = note_counts.get(key, 0) + 1
        if _CONFIDENCE_RANK[row.confidence] > _CONFIDENCE_RANK[confidence]:
            confidence = row.confidence
        legacy_input += row.legacy_input_tokens
        legacy_total += row.legacy_total_tokens
        reprojected += row.reprojected_input_tokens
        reprojected_total += row.reprojected_total_tokens
        if row.reprojected_input_tokens_high is not None:
            has_high = True
            # 上界必须覆盖"没有区间的行"（它们只有一个精确值），否则文件上界会小于主值。
            high_input += row.reprojected_input_tokens_high
            high_total += row.reprojected_total_tokens_high or row.reprojected_total_tokens
        else:
            high_input += row.reprojected_input_tokens
            high_total += row.reprojected_total_tokens
    return {
        "protocol_counts": protocol_counts,
        "basis_counts": basis_counts,
        "note_counts": note_counts,
        "models": models,
        "confidence": confidence,
        "legacy_input_tokens": legacy_input,
        "legacy_total_tokens": legacy_total,
        "reprojected_input_tokens": reprojected,
        "reprojected_total_tokens": reprojected_total,
        "reprojected_input_tokens_high": high_input if has_high else None,
        "reprojected_total_tokens_high": high_total if has_high else None,
    }


# 函数用途: 把聚合结果翻译成人类可读的结构化理由串（只有标签与计数，不含正文）。
def _file_notes(aggregate: dict[str, Any], row_count: int, skipped: int, unparsable: int) -> tuple[str, ...]:
    notes = [f"rows={row_count}"]
    if skipped:
        notes.append(f"skipped_records={skipped}")
    if unparsable:
        notes.append(f"unparsable_lines={unparsable}")
    protocols = aggregate["protocol_counts"]
    if protocols:
        notes.append("protocols=" + ",".join(f"{key}:{protocols[key]}" for key in sorted(protocols)))
    # 跨行协议/模型混用只做"可见性"标注：协议决定缓存口径，模型名不决定，所以模型混用不改置信度，
    # 但必须让读者看见，避免把"多模型会话"误当单一模型样本。
    if len([key for key in protocols if key]) > 1:
        notes.append(f"mixed_protocols_across_rows={len([key for key in protocols if key])}")
    models = aggregate["models"]
    if len(models) > 1:
        notes.append(f"mixed_models_across_rows={len(models)}")
    bases = aggregate["basis_counts"]
    notes.append("bases=" + ",".join(f"{key}:{bases[key]}" for key in sorted(bases)))
    for key in sorted(aggregate["note_counts"]):
        notes.append(f"{key}={aggregate['note_counts'][key]}")
    return tuple(notes)


# LLM: 文件级协议只做"行协议去重"：一个协议 → 该协议；多个 → mixed；没有 → unknown。
# 不做"多数票"，因为多数票会让一份混合协议的账本看起来像单一协议。
# 函数用途: 由行协议集合判定文件级协议标签。
def file_protocol(protocol_counts: dict[str, int]) -> str:
    keys = [key for key in protocol_counts if key]
    if len(keys) == 1:
        return keys[0]
    return PROTOCOL_MIXED if keys else PROTOCOL_UNKNOWN


# 函数用途: 读取一个账本文件并生成文件级重算投影（只读打开，绝不写入）。
def project_file(path: Path) -> FileProjection:
    rows: list[RowProjection] = []
    skipped = 0
    unparsable = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for index, line in enumerate(handle):
            text = line.strip()
            if not text:
                continue
            try:
                record = json.loads(text)
            except ValueError:
                unparsable += 1
                continue
            if not isinstance(record, dict):
                unparsable += 1
                continue
            parsed = row_from_record(index, record)
            if parsed is None:
                skipped += 1
                continue
            rows.append(project_row(parsed))
    aggregate = _aggregate_rows(tuple(rows))
    confidence = aggregate["confidence"] if rows else CONFIDENCE_PARTIAL
    notes = list(_file_notes(aggregate, len(rows), skipped, unparsable))
    # 账没读全就不能标准确值：坏行/无法识别的记录会让求和缺一块，一律至少降级为 partial。
    if (unparsable or skipped) and _CONFIDENCE_RANK[confidence] < _CONFIDENCE_RANK[CONFIDENCE_PARTIAL]:
        confidence = CONFIDENCE_PARTIAL
        notes.append("ledger_rows_incomplete")
    # 交付要求：混合协议的账本不得整体当精确值。跨行协议不同时，每一行仍各自可精确重算（行内只有
    # 一个协议族），但"整份文件 = 单一协议样本"这个前提不成立，所以文件级降级为 incomplete 并说明。
    if len([key for key in aggregate["protocol_counts"] if key]) > 1:
        confidence = CONFIDENCE_INCOMPLETE
        if aggregate["confidence"] == CONFIDENCE_EXACT:
            notes.append("mixed_protocols_rows_individually_exact")
    if not rows:
        notes.append("no_usage_rows_present")
    return FileProjection(
        path=str(path),
        protocol=file_protocol(aggregate["protocol_counts"]),
        confidence=confidence,
        legacy_input_tokens=aggregate["legacy_input_tokens"],
        legacy_total_tokens=aggregate["legacy_total_tokens"],
        reprojected_input_tokens=aggregate["reprojected_input_tokens"],
        reprojected_total_tokens=aggregate["reprojected_total_tokens"],
        reprojected_input_tokens_high=aggregate["reprojected_input_tokens_high"],
        reprojected_total_tokens_high=aggregate["reprojected_total_tokens_high"],
        delta_tokens=aggregate["reprojected_total_tokens"] - aggregate["legacy_total_tokens"],
        notes=tuple(notes),
        rows=tuple(rows),
        skipped_records=skipped,
        unparsable_lines=unparsable,
    )


# LLM: 账本目录的 canonical 布局是 <workspace>/conversations/model_usage/*.jsonl；owner home 则是
# <home>/workspace/runtime/workspaces/<ws>/conversations/model_usage/*.jsonl。下面的 (相对根, 扫描模式)
# 只是"固定深度快路径"：命中就用，全部落空时仍可用 --deep 整树递归（布局不封闭）。
# 之所以不默认递归：真实 owner home 下实测有约 50 万个目录，整树遍历要几十秒且纯属浪费。
_LEDGER_SWEEPS = (
    ("", "*/conversations/model_usage/*.jsonl"),
    ("", "conversations/model_usage/*.jsonl"),
    ("workspaces", "*/conversations/model_usage/*.jsonl"),
    ("runtime/workspaces", "*/conversations/model_usage/*.jsonl"),
    ("workspace/runtime/workspaces", "*/conversations/model_usage/*.jsonl"),
    ("owners/*/*/workspace/runtime/workspaces", "*/conversations/model_usage/*.jsonl"),
    ("owners", "*/*/workspace/runtime/workspaces/*/conversations/model_usage/*.jsonl"),
)


# 函数用途: 在一个目录下找出账本文件；默认走固定深度快路径，--deep 时整树递归。
def scan_ledger_dir(path: Path, *, deep: bool) -> list[Path]:
    if path.name == "model_usage":
        return sorted(item for item in path.glob("*.jsonl") if item.is_file())
    matched: dict[str, Path] = {}
    for root, pattern in _LEDGER_SWEEPS:
        base = path if not root else path / root
        if not base.is_dir():
            continue
        for item in base.glob(pattern):
            if item.is_file():
                matched.setdefault(str(item.resolve()), item)
    if deep and not matched:
        for item in path.glob("**/model_usage/*.jsonl"):
            if item.is_file():
                matched.setdefault(str(item.resolve()), item)
    return [matched[key] for key in sorted(matched)]


# LLM: 只读发现账本：显式文件直接用；目录先按 canonical 布局探测，--deep 才整树递归。
# 函数用途: 把命令行目标展开成待投影的账本文件列表与错误列表。
def discover_ledger_paths(targets: list[str], *, deep: bool = False) -> tuple[list[Path], list[str]]:
    found: list[Path] = []
    errors: list[str] = []
    for target in targets:
        path = Path(target).expanduser()
        if path.is_file():
            found.append(path)
            continue
        if not path.is_dir():
            errors.append(f"输入不存在: {path}")
            continue
        matched = scan_ledger_dir(path, deep=deep)
        if not matched:
            hint = "" if deep else "（如为非常规布局，可加 --deep 整树递归）"
            errors.append(f"目录下没有找到 model_usage/*.jsonl: {path}{hint}")
            continue
        found.extend(matched)
    unique: dict[str, Path] = {}
    for path in found:
        unique.setdefault(str(path.resolve()), path)
    return [unique[key] for key in sorted(unique)], errors


# 函数用途: 逐个投影账本文件；单个文件读不了只记错误，不中断其它文件。
def project_paths(paths: list[Path]) -> tuple[list[FileProjection], list[str]]:
    projections: list[FileProjection] = []
    errors: list[str] = []
    for path in paths:
        try:
            projections.append(project_file(path))
        except OSError as exc:
            errors.append(f"读取失败 {path}: {type(exc).__name__}")
    return projections, errors


# 函数用途: 汇总所有文件的分类计数与总量，供人类可读尾部与 JSON summary 使用。
def summarize(projections: list[FileProjection]) -> dict[str, Any]:
    counts = {CONFIDENCE_EXACT: 0, CONFIDENCE_PARTIAL: 0, CONFIDENCE_INCOMPLETE: 0}
    legacy_total = reprojected_total = 0
    for item in projections:
        counts[item.confidence] = counts.get(item.confidence, 0) + 1
        legacy_total += item.legacy_total_tokens
        reprojected_total += item.reprojected_total_tokens
    return {
        "files": len(projections),
        "rows": sum(len(item.rows) for item in projections),
        "exact": counts[CONFIDENCE_EXACT],
        "partial": counts[CONFIDENCE_PARTIAL],
        "incomplete": counts[CONFIDENCE_INCOMPLETE],
        "legacy_total_tokens": legacy_total,
        "reprojected_total_tokens": reprojected_total,
        "delta_tokens": reprojected_total - legacy_total,
    }


# 函数用途: 组装完整 JSON 载荷（输入目标 + 汇总 + 每个文件投影）。
def build_payload(
    targets: list[str],
    projections: list[FileProjection],
    errors: list[str],
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "targets": list(targets),
        "summary": summarize(projections),
        "files": [item.to_dict() for item in projections],
        "errors": list(errors),
    }


# 函数用途: 人类可读输出用的千分位数字，便于肉眼核对量级。
def _num(value: int | None) -> str:
    if value is None:
        return "-"
    return f"{value:,}"


# LLM: 终端里中文标签占两列，直接用 ljust 会算错宽度导致两栏错位；这里按东亚宽字符计宽。
# 函数用途: 计算字符串在终端里的显示宽度（中文/全角算 2 列）。
def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)


# 函数用途: 按显示宽度补齐的空格，输出稳定的两栏（标签 / 值）文本。
def _two_column(prefix: str, label: str, value: str, width: int) -> str:
    padding = max(0, width - _display_width(label))
    return f"{prefix}{label}{' ' * padding}: {value}"


def _confidence_mark(confidence: str) -> str:
    if confidence == CONFIDENCE_INCOMPLETE:
        return "⚠ 旧口径/不完整，不可当精确值"
    if confidence == CONFIDENCE_PARTIAL:
        return "△ 旧口径（分栏不全或含本地估算），低于精确值"
    return "● 单一协议且分栏齐全，可作精确值"


# 函数用途: 渲染一个文件的两栏文本块；--rows 时追加逐行明细。
def render_file(projection: FileProjection, *, show_rows: bool) -> list[str]:
    lines = [f"账本: {projection.path}"]
    fields: list[tuple[str, str]] = [
        ("记录行数", _num(len(projection.rows))),
        ("协议", projection.protocol),
        ("置信度", projection.confidence),
        ("旧口径输入(账本原值)", _num(projection.legacy_input_tokens)),
        ("旧口径总量(账本原值)", _num(projection.legacy_total_tokens)),
        ("重算输入(普通+读+写)", _num(projection.reprojected_input_tokens)),
        ("重算总量", _num(projection.reprojected_total_tokens)),
        ("重算差额", f"{projection.delta_tokens:+,}"),
        ("质量标记", _confidence_mark(projection.confidence)),
    ]
    if projection.reprojected_input_tokens_high is not None:
        fields.insert(
            7,
            (
                "重算区间(下限..上限)",
                f"{_num(projection.reprojected_input_tokens)}.."
                f"{_num(projection.reprojected_input_tokens_high)}",
            ),
        )
    fields.append(("说明", "; ".join(projection.notes)))
    width = max(_display_width(label) for label, _ in fields)
    lines.extend(_two_column("  ", label, value, width) for label, value in fields)
    if show_rows:
        row_fields = [
            "行",
            "协议",
            "置信度",
            "旧输入",
            "旧总量",
            "缓存读",
            "缓存写",
            "输出",
            "重算输入",
            "重算总量",
            "依据",
            "说明",
        ]
        lines.append("  " + " | ".join(row_fields))
        for row in projection.rows:
            lines.append(
                "  "
                + " | ".join(
                    [
                        str(row.index),
                        row.protocol,
                        row.confidence,
                        _num(row.legacy_input_tokens),
                        _num(row.legacy_total_tokens),
                        _num(row.cache_read_input_tokens),
                        _num(row.cache_write_input_tokens),
                        _num(row.output_tokens),
                        _num(row.reprojected_input_tokens),
                        _num(row.reprojected_total_tokens),
                        row.basis,
                        "; ".join(row.notes),
                    ]
                )
            )
    return lines


# 函数用途: 渲染人类可读全文（逐文件两栏 + 总体分类计数 + 不完整清单）。
def render_text(
    payload: dict[str, Any],
    projections: list[FileProjection],
    *,
    show_rows: bool,
) -> str:
    lines: list[str] = []
    for projection in projections:
        lines.extend(render_file(projection, show_rows=show_rows))
        lines.append("")
    summary = payload["summary"]
    lines.append("=== 汇总（只读重算投影，账本未被修改）===")
    summary_fields = [
        ("文件数", _num(summary["files"])),
        ("记录行数", _num(summary["rows"])),
        ("分类计数", f"exact={summary['exact']}, partial={summary['partial']}, incomplete={summary['incomplete']}"),
        ("旧口径总量", _num(summary["legacy_total_tokens"])),
        ("重算总量", _num(summary["reprojected_total_tokens"])),
        ("重算差额", f"{summary['delta_tokens']:+,}"),
    ]
    width = max(_display_width(label) for label, _ in summary_fields)
    lines.extend(_two_column("  ", label, value, width) for label, value in summary_fields)
    incomplete = [item for item in projections if item.confidence == CONFIDENCE_INCOMPLETE]
    if incomplete:
        lines.append(f"  不完整文件（旧口径/不完整，不可当精确值）: {len(incomplete)}")
        for item in incomplete:
            reasons = "; ".join(item.notes[:3])
            lines.append(f"    - {item.path} :: {reasons}")
    missing = [item for item in projections if item.confidence == CONFIDENCE_PARTIAL]
    if missing:
        lines.append(f"  分栏不全文件（旧口径，低于精确值）: {len(missing)}")
        for item in missing:
            reasons = "; ".join(item.notes[:3])
            lines.append(f"    - {item.path} :: {reasons}")
    for error in payload["errors"]:
        lines.append(f"  错误: {error}")
    return "\n".join(lines)


# 函数用途: 命令行参数定义。
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只读重算历史 model_usage 账本的用量投影（不写账本、不改仓库、不调用模型）。",
    )
    parser.add_argument(
        "targets",
        nargs="+",
        help="账本 jsonl 路径、model_usage 目录、workspace 或 owner home 目录。",
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON。")
    parser.add_argument(
        "--rows",
        action="store_true",
        help="人类可读模式下追加逐行明细（JSON 模式始终包含明细）。",
    )
    parser.add_argument(
        "--fail-on-incomplete",
        action="store_true",
        help="存在 incomplete 文件时以退出码 2 结束，便于脚本化门禁。",
    )
    parser.add_argument(
        "--deep",
        action="store_true",
        help="目录输入时整树递归查找 model_usage/*.jsonl（默认只走固定深度快路径）。",
    )
    return parser


# 函数用途: 入口；只读投影后按 --json / 人类可读输出，返回进程退出码。
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths, discovery_errors = discover_ledger_paths(args.targets, deep=args.deep)
    if not paths:
        for error in discovery_errors:
            print(f"错误: {error}", file=sys.stderr)
        return 1
    projections, read_errors = project_paths(paths)
    errors = discovery_errors + read_errors
    payload = build_payload(args.targets, projections, errors)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_text(payload, projections, show_rows=args.rows))
    if errors:
        return 1
    if args.fail_on_incomplete and payload["summary"]["incomplete"] > 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
