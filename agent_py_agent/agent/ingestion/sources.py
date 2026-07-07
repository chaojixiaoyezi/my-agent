"""多形态数据源:file 源(读大文件/盯增长日志)+ poll 源(定时查快照接口)+ 统一 drain 入口。

四种进数据方式的源侧支撑(与 puller.py 的 HTTP 游标源并列):
- cursor(默认):GET ?since=&limit= 增量流,drain_source(puller.py)。
- file:本地文件按【字节偏移游标】逐行读——一次性大文件读到 EOF 即算追平;文件持续
  增长(日志 tail)则下拍从偏移续读,尾部半行(写入中)留到下拍,轮转/截断如实记缺口。
  每行一个事件:JSON 对象行原样进引擎,非 JSON 行包成 {"line": 原文}(纯结构包装)。
- poll:快照型接口没有游标语义(每次查返回当前状态),按 poll_query_seconds 节拍
  GET 一次,把整份响应包成一条事件 {"polled_at", "response"} 交研判——响应没变化则
  同签名计数上涨(常态),字段变化天然成为新签名/少数派取值被抬,量小时直通全读。

铁律(与 engine 同):本层零自然语言/关键词定性,只做结构化搬运与计数;
行解析成败(json.loads)、偏移比对、节拍判断全是结构信号。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .puller import DrainBudget, DrainResult, drain_source

_LINE_TEXT_CAP = 4000
_FILE_READ_CHUNK_GUARD = 512 * 1024  # 单行长度保护:超长行截断为文本事件,不撑爆内存


def source_kind(source_url: str, source_mode: str = "") -> str:
    """源类型判定(纯字面):file:// → file;mode=poll → poll;其余 cursor。"""
    if str(source_url or "").lower().startswith("file://"):
        return "file"
    if str(source_mode or "").strip().lower() == "poll":
        return "poll"
    return "cursor"


def file_path_of(source_url: str) -> Path:
    """file:// URL → 本地绝对路径(open 层已做规范化与路径闸)。"""
    parts = urlsplit(str(source_url or ""))
    return Path(unquote(parts.path or ""))


def normalize_file_url(raw: str) -> str:
    """把 file:///abs/path 或裸绝对路径规范成 file:// URL;相对路径拒绝(游标要可复现)。"""
    text = str(raw or "").strip()
    if text.lower().startswith("file://"):
        path = file_path_of(text)
    else:
        path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValueError("file 源必须给绝对路径(file:///abs/path 或 /abs/path)")
    resolved = path.resolve(strict=False)
    return f"file://{resolved}"


def drain_watch_source(state, fetch_json, budget: DrainBudget) -> DrainResult:
    """统一 drain 入口:按源类型分流;返回 DrainResult(file 源额外带 aux_cursor=行号)。
    调用方(inline pull / harvester)对三种源同一套吸收/记账/引擎逻辑。"""
    kind = source_kind(state.source_url, getattr(state, "source_mode", ""))
    if kind == "file":
        return drain_file_source(
            file_path_of(state.source_url), state.cursor, int(getattr(state, "line_cursor", 0) or 0), budget
        )
    if kind == "poll":
        last_poll_at = float(getattr(state, "last_poll_at", 0.0) or 0.0)
        beat = max(1, int(state.tuning.poll_query_seconds))
        due = last_poll_at <= 0 or (time.time() - last_poll_at) >= beat
        return drain_poll_source(fetch_json, state.source_url, state.cursor, due=due)
    return drain_source(fetch_json, state.source_url, state.cursor, budget)


def drain_file_source(path: Path, byte_cursor: int, line_cursor: int, budget: DrainBudget) -> DrainResult:
    """file 源 drain:从字节偏移续读完整行;EOF/尾部半行 → reached_end 停在半行前。
    轮转/截断(文件比游标短)→ 回到 0 重读并记 1 次缺口(规模未知,如实入账)。
    cursor=下一次续读的字节偏移;aux_cursor=已读行号(stream_pos=行号,1-based)。"""
    result = DrainResult(cursor=byte_cursor)
    result.aux_cursor = line_cursor
    try:
        size = path.stat().st_size
    except OSError as exc:
        result.error = f"文件不可读: {exc}"[:500]
        result.error_code = "NETWORK_REQUEST_FAILED"
        return result
    if size < byte_cursor:
        result.cursor = 0
        result.aux_cursor = 0
        result.gap_events += 1  # 轮转/截断:一次未知规模的缺口,审计账可见
    try:
        _read_file_lines(path, result, budget)
    except OSError as exc:
        result.error = f"文件读取失败: {exc}"[:500]
        result.error_code = "NETWORK_REQUEST_FAILED"
    return result


def _read_file_lines(path: Path, result: DrainResult, budget: DrainBudget) -> None:
    with path.open("rb") as handle:
        handle.seek(result.cursor)
        result.reached_end = _drain_open_handle(handle, result, budget)


def _drain_open_handle(handle, result: DrainResult, budget: DrainBudget) -> bool:
    """逐行消费直到追平或预算耗尽;True=追平(EOF/尾部半行),False=预算耗尽后面还有。"""
    while len(result.events) < budget.max_events and time.time() < budget.deadline:
        if not _consume_one_line(handle, result):
            return True
    return False


def _consume_one_line(handle, result: DrainResult) -> bool:
    """读一条完整行进 result;EOF/尾部半行(写入中)返回 False——不消费、偏移停在
    行首,下拍读到完整行再算。空行占行号、不产事件。"""
    line = handle.readline(_FILE_READ_CHUNK_GUARD)
    if not line:
        return False
    if not line.endswith(b"\n") and len(line) < _FILE_READ_CHUNK_GUARD:
        return False
    result.cursor = handle.tell()
    result.aux_cursor += 1
    text = line.decode("utf-8", "replace").rstrip("\r\n")
    if text.strip():
        result.events.append((result.aux_cursor, _line_event(text)))
    return True


def _line_event(text: str) -> dict:
    """一行 → 一个事件:JSON 对象原样;其余(纯文本/非对象 JSON)包成 {"line": 原文}。
    纯结构包装,不解读内容;超长截断防撑爆(引擎压平/渲染另有各自上限)。"""
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    return {"line": text[:_LINE_TEXT_CAP]}


def sample_file_lines(path: Path, count: int) -> tuple[list[dict], str]:
    """file 源取样:从文件头读 count 个事件(学判据用,不动盯守游标)。返回 (events, error)。"""
    budget = DrainBudget(max_events=count, page_limit=count, deadline=time.time() + 15)
    drain = drain_file_source(path, 0, 0, budget)
    return [event for _seq, event in drain.events], drain.error


def drain_poll_source(fetch_json, source_url: str, cursor: int, *, due: bool) -> DrainResult:
    """poll 源 drain:due=False(节拍未到,由调用方按 last_poll_at×poll_query_seconds 算)
    → 空手追平(调用方照常长轮询等待);due=True → GET 一次,整份响应包成一条事件,
    游标自增。fetched_at>0 表示本次真的查了(调用方据此更新 last_poll_at)。"""
    result = DrainResult(cursor=cursor)
    result.reached_end = True
    if not due:
        return result
    now = time.time()
    ok, payload, error_code = fetch_json(source_url)
    result.pages = 1
    result.fetched_at = now
    if not ok:
        result.error = str(payload)[:500]
        result.error_code = error_code or "NETWORK_REQUEST_FAILED"
        return result
    result.cursor = cursor + 1
    result.events.append((result.cursor, {"polled_at": round(now, 3), "response": payload}))
    return result


def probe_poll_envelope(fetch_json, source_url: str, value_cap: int, key_cap: int) -> dict:
    """poll 源的 open 探针:原样 GET 一次,搬顶层标量(不拼 since/limit——快照接口
    没有游标参数语义)。失败返回空,不阻塞 open。"""
    ok, payload, _code = fetch_json(source_url)
    if not ok or not isinstance(payload, dict):
        return {}
    envelope: dict = {}
    for key, value in payload.items():
        if len(envelope) >= key_cap:
            break
        if isinstance(value, (str, int, float, bool)):
            envelope[str(key)] = value if not isinstance(value, str) else value[:value_cap]
    return envelope


__all__ = [
    "drain_file_source",
    "drain_poll_source",
    "drain_watch_source",
    "file_path_of",
    "normalize_file_url",
    "probe_poll_envelope",
    "sample_file_lines",
    "source_kind",
]
