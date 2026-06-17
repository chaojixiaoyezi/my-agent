
from __future__ import annotations

"""存档联合查询 —— 在某源存档里按 pattern/时间确定性 grep,返回真实匹配行(防幻觉证据)。

研判时主代理交叉验证用:log_source_query 在 archive/<source>.log 里跑确定性正则匹配 + 可选
时间窗口过滤,返回真实命中的原始行(带行号)。这是确定性结果,不是模型臆测 —— 主代理的研判
结论必须建立在这些真实返回上(证据链)。
"""

import re
from dataclasses import dataclass
from itertools import islice
from typing import Any

from .store import LogOpsStore, classify_source, source_id_for

# 单次查询最多扫多少匹配行进结果,防一次拉爆;命中超限给 truncated 标记。
_MAX_MATCHES = 500
# 单次查询最多扫多少行(防超大存档全扫卡死);扫满给 scan_truncated 标记。
_MAX_SCAN_LINES = 5_000_000

_ISO_TS_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?)")


def resolve_source_id(store: LogOpsStore, source: str) -> str:
    """把用户给的 source(可能是 source_id,也可能是原始 locator)解析成 source_id。

    先看是不是已知 source_id;否则按 locator 算 source_id(与采集时同一算法,对得上)。
    """
    source = str(source or "").strip()
    if not source:
        return ""
    for spec in store.source_specs():
        if source in (spec.source_id, spec.locator):
            return spec.source_id
    # 不在 config 里:按 locator 现算(允许查还没配进监控但已有存档的源)。
    kind = classify_source(source)
    return source_id_for(kind, source)


@dataclass(frozen=True)
class QueryRequest:
    """log_source_query 的查询参数包(用 dataclass 收敛参数,避免长参数列表)。"""

    source: str
    pattern: str
    time_range: tuple[str, str] | None = None
    limit: int = 100
    case_sensitive: bool = False


@dataclass(frozen=True)
class _LineFilter:
    """单行过滤器:正则/子串匹配 + 可选时间窗口。matcher 闭包 + start/end ISO 串。"""

    matcher: Any
    start_ts: str
    end_ts: str

    def accepts(self, line: str) -> bool:
        if not line.strip():
            return False
        if (self.start_ts or self.end_ts) and not _in_time_range(line, self.start_ts, self.end_ts):
            return False
        return bool(self.matcher(line))


@dataclass(frozen=True)
class _ScanResult:
    matches: list[dict[str, Any]]
    scanned: int
    scan_truncated: bool


def query_archive(store: LogOpsStore, request: QueryRequest) -> dict[str, Any]:
    """在某源存档里确定性匹配。返回 {ok, source_id, matched, scanned, matches:[{line_no,line,timestamp}], ...}。

    pattern 当正则(非法正则回退成字面子串匹配,避免模型给的怪 pattern 直接报错)。
    time_range=(start_iso, end_iso) 按行首 ISO 时间字符串字典序过滤(ISO8601 字典序即时间序)。
    """
    source_id = resolve_source_id(store, request.source)
    if not source_id:
        return {"ok": False, "error": "missing_source", "message": "source 不能为空。"}
    archive_path = store.archive_path(source_id)
    if not archive_path.exists():
        return _empty_query_result(source_id)

    capped = max(1, min(int(request.limit or 100), _MAX_MATCHES))
    start_ts, end_ts = (request.time_range or ("", ""))
    line_filter = _LineFilter(_build_matcher(request.pattern, request.case_sensitive), start_ts, end_ts)
    try:
        scan = _scan_archive(archive_path, line_filter, capped)
    except OSError as exc:
        return {"ok": False, "error": "read_failed", "source_id": source_id, "message": str(exc)}

    return {
        "ok": True,
        "source_id": source_id,
        "pattern": request.pattern,
        "matched": len(scan.matches),
        "scanned": scan.scanned,
        "truncated": len(scan.matches) >= capped,
        "scan_truncated": scan.scan_truncated,
        "time_range": {"start": start_ts, "end": end_ts} if (start_ts or end_ts) else None,
        "matches": scan.matches,
    }


def _empty_query_result(source_id: str) -> dict[str, Any]:
    return {
        "ok": True,
        "source_id": source_id,
        "matched": 0,
        "scanned": 0,
        "matches": [],
        "message": "该源还没有存档(可能 daemon 未起或尚未采到该源数据)。",
    }


def _scan_archive(archive_path: Any, line_filter: _LineFilter, capped: int) -> _ScanResult:
    """扫存档文件,返回命中行 + 扫描计数。命中达 capped 或扫满扫描上限即停。"""
    counter = _ScanCounter()
    with archive_path.open("r", encoding="utf-8", errors="replace") as handle:
        numbered = enumerate(counter.tap(islice(handle, _MAX_SCAN_LINES)), start=1)
        hits = (_scan_one_line(line_no, raw, line_filter) for line_no, raw in numbered)
        matches = list(islice((hit for hit in hits if hit is not None), capped))
    scan_truncated = counter.count >= _MAX_SCAN_LINES and len(matches) < capped
    return _ScanResult(matches, counter.count, scan_truncated)


class _ScanCounter:
    """包一层迭代器统计真实扫过多少行(islice 限流后仍能拿到扫描计数,用于截断判定)。"""

    def __init__(self) -> None:
        self.count = 0

    def tap(self, iterable: Any) -> Any:
        for item in iterable:
            self.count += 1
            yield item


def _scan_one_line(line_no: int, raw: str, line_filter: _LineFilter) -> dict[str, Any] | None:
    """单行匹配:命中返回 match dict,不命中返回 None。"""
    line = raw.rstrip("\n")
    if not line_filter.accepts(line):
        return None
    return {"line_no": line_no, "line": line, "timestamp": _line_timestamp(line)}


def _line_matches(line: str, matcher, start_ts: str, end_ts: str) -> bool:
    if not line.strip():
        return False
    if (start_ts or end_ts) and not _in_time_range(line, start_ts, end_ts):
        return False
    return matcher(line)


def _build_matcher(pattern: str, case_sensitive: bool):
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        compiled = re.compile(pattern, flags)
        return lambda line: bool(compiled.search(line))
    except re.error:
        needle = pattern if case_sensitive else pattern.lower()
        if case_sensitive:
            return lambda line: needle in line
        return lambda line: needle in line.lower()


def _line_timestamp(line: str) -> str:
    match = _ISO_TS_RE.match(line)
    return match.group(1) if match else ""


def _in_time_range(line: str, start_ts: str, end_ts: str) -> bool:
    ts = _line_timestamp(line)
    if not ts:
        return False
    if start_ts and ts < start_ts:
        return False
    if end_ts and ts > end_ts:
        return False
    return True


def query_multi(store: LogOpsStore, sources: list[str], request: QueryRequest) -> dict[str, Any]:
    """跨多源查 request.pattern(request.source 被 sources 覆盖),汇总命中供联合分析拼事件链。

    sources=源标识列表;含 '*' 查所有已登记源。对每源跑 query_archive,按源分组返回有命中的源,
    便于"同一 IP/IOC 在哪几个源出现过、各几次"这类跨源关联研判。
    """
    target_ids = _resolve_target_ids(store, sources)
    if not target_ids:
        return {"ok": False, "error": "no_sources", "message": "没有可查的源(给源列表或 '*')。"}
    per_source: dict[str, Any] = {}
    total = 0
    for sid in target_ids:
        req = QueryRequest(
            source=sid,
            pattern=request.pattern,
            time_range=request.time_range,
            limit=request.limit,
            case_sensitive=request.case_sensitive,
        )
        res = query_archive(store, req)
        matched = int(res.get("matched", 0) or 0)
        if matched > 0:
            per_source[sid] = {
                "matched": matched,
                "matches": res.get("matches", []),
                "truncated": res.get("truncated", False),
            }
            total += matched
    return {
        "ok": True,
        "pattern": request.pattern,
        "sources_queried": len(target_ids),
        "sources_with_hits": len(per_source),
        "total_matched": total,
        "per_source": per_source,
    }


def _resolve_target_ids(store: LogOpsStore, sources: list[str]) -> list[str]:
    """把 sources(列表,可能含 '*' 或 source_id/locator)解析成 source_id 列表(去重保序)。"""
    if any(str(item).strip() == "*" for item in sources):
        return [spec.source_id for spec in store.source_specs()]
    out: list[str] = []
    seen: set[str] = set()
    for item in sources:
        sid = resolve_source_id(store, str(item))
        if sid and sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


__all__ = ["QueryRequest", "query_archive", "query_multi", "resolve_source_id"]
