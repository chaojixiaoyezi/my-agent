"""多形态数据源:file、poll、cursor 与 prepare 现场学习 adapter 的统一 drain 入口。

四种进数据方式的源侧支撑:
- cursor(默认):执行 Agent 现场学得并试通的 HTTP GET/POST 请求事实，游标与页长可绑定到
  任意 query 参数或 JSON 请求体位置，drain_source(puller.py) 只按结构推进。
- file:本地文件按【字节偏移游标】逐行读——一次性大文件读到 EOF 即算追平;文件持续
  增长(日志 tail)则下拍从偏移续读,尾部半行(写入中)留到下拍,轮转/截断如实记缺口。
  每行一个事件:JSON 对象行原样进引擎,非 JSON 行包成 {"line": 原文}(纯结构包装)。
- poll:快照型接口没有游标语义(每次查返回当前状态),按 poll_query_seconds 节拍
  执行一次现场学得的 GET/POST 请求,把整份响应包成一条事件 {"polled_at", "response"} 交研判——响应没变化则
  同签名计数上涨(常态),字段变化天然成为新签名/少数派取值被抬,量小时直通全读。
- adapter:prepare 现场写入并试通的纯来源适配器决定下一次请求和完整记录边界；进度是
  host 不解释的 JSON checkpoint，可同时保存动态时间窗、页码、offset、1-100/101-200
  区间、字符串 token 等。来源可以突发、长时间无数据或随时改变速率，host 只负责
  同源请求、原子提交、失败重放和记录去重，不预置任何业务模板。

铁律(与 engine 同):本层零自然语言/关键词定性,只做结构化搬运与计数;
行解析成败(json.loads)、偏移比对、节拍判断全是结构信号。
"""

from __future__ import annotations

import json
import os
import time
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .puller import DrainBudget, DrainResult, drain_source
from .source_http import render_source_http_request

# 与 会话运行时 的有界行帧做法一致：多读 1 字节判断单条是否真的越界。
# 合法记录完整保留；超过安全上限明确失败并停在原游标，绝不把一条切成多条或静默截短。
_MAX_FILE_RECORD_BYTES = 8 * 1024 * 1024


def source_kind(source_url: str, source_mode: str = "") -> str:
    """源类型判定(纯字面):file、poll、prepare-learned adapter 或 legacy cursor。"""
    if str(source_url or "").lower().startswith("file://"):
        return "file"
    selected = str(source_mode or "").strip().lower()
    if selected in {"poll", "adapter"}:
        return selected
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


def drain_watch_source(
    state,
    fetch_json,
    budget: DrainBudget,
    *,
    force_poll: bool = False,
) -> DrainResult:
    """统一 drain 入口:按源类型分流;返回 DrainResult(file 源额外带 aux_cursor=行号)。
    调用方(inline pull / harvester)对三种源同一套吸收/记账/引擎逻辑。"""
    kind = source_kind(state.source_url, getattr(state, "source_mode", ""))
    if kind == "file":
        fragment = load_file_fragment(state)
        if int(getattr(state, "file_fragment_bytes", 0) or 0) > 0 and fragment is None:
            result = DrainResult(cursor=state.cursor)
            result.aux_cursor = int(getattr(state, "line_cursor", 0) or 0)
            result.error = "未完成记录片段缺失或哈希不一致，未读取也未推进游标"
            result.error_code = "SOURCE_FRAGMENT_UNAVAILABLE"
            return result
        return drain_file_source(
            file_path_of(state.source_url),
            state.cursor,
            int(getattr(state, "line_cursor", 0) or 0),
            budget,
            delimiter=file_record_delimiter(state),
            expected_identity=str(getattr(state, "file_identity", "") or ""),
            expected_fragment=fragment or b"",
        )
    if kind == "poll":
        last_poll_at = float(getattr(state, "last_poll_at", 0.0) or 0.0)
        beat = max(1, int(state.tuning.poll_query_seconds))
        # LLM: The collection-boundary read bypasses only the polling cadence;
        # it does not bypass URL policy, record framing, cursor, or commit checks.
        # 函数用途: 快照源在窗口到点时强制查最后一次，避免最后一个节拍内的变化漏采。
        due = force_poll or last_poll_at <= 0 or (time.time() - last_poll_at) >= beat
        return drain_poll_source(
            fetch_json,
            state.source_url,
            state.cursor,
            due=due,
            source_envelope=(
                dict(state.source_envelope)
                if isinstance(getattr(state, "source_envelope", None), dict)
                else None
            ),
        )
    if kind == "adapter":
        return drain_adapter_source(state, fetch_json, budget)
    return drain_source(
        fetch_json,
        state.source_url,
        state.cursor,
        budget,
        source_envelope=(
            dict(state.source_envelope)
            if isinstance(getattr(state, "source_envelope", None), dict)
            else None
        ),
    )


def drain_adapter_source(state, fetch_json, budget: DrainBudget) -> DrainResult:
    """Drain one open-world HTTP source through its pinned pure adapter.

    The adapter decides only how this concrete source progresses.  Network I/O,
    secrets, the local record ordinal and commit are still owned by the host.
    A later-page failure leaves the whole cycle uncommitted so takeover resumes
    from the exact previous checkpoint.
    """

    from .source_adapter import (
        SourceAdapterError,
        accept_source_response,
        plan_source_request,
    )

    result = DrainResult(
        cursor=int(state.cursor),
        source_checkpoint=deepcopy(getattr(state, "source_checkpoint", {}) or {}),
    )
    if budget.max_events <= 0:
        return result
    envelope = state.source_envelope if isinstance(state.source_envelope, dict) else {}
    request_facts = envelope.get("request")
    adapter = envelope.get("adapter")
    audit_id = str(state.audit_root_task_id or state.prepare_root_task_id or "").strip()
    if not isinstance(request_facts, dict) or not isinstance(adapter, dict) or not audit_id:
        result.error = "动态 HTTP 来源缺少已验证的请求、适配器或 Audit 归属"
        result.error_code = "SOURCE_ADAPTER_INVALID"
        return result
    while len(result.events) < budget.max_events and time.time() < budget.deadline:
        current_checkpoint = deepcopy(result.source_checkpoint or {})
        try:
            requested_page_limit = max(
                1,
                min(budget.page_limit, budget.max_events - len(result.events)),
            )
            plan = plan_source_request(
                owner_home=state.owner_home,
                audit_id=audit_id,
                source_url=state.source_url,
                request_facts=request_facts,
                adapter=adapter,
                checkpoint=current_checkpoint,
                page_limit=requested_page_limit,
                now_unix=time.time(),
            )
        except SourceAdapterError as exc:
            result.error = str(exc)[:500]
            result.error_code = exc.code
            return result
        ok, payload, error_code = fetch_json(plan.request)
        result.pages += 1
        result.fetched_at = time.time()
        if not ok:
            result.error = str(payload)[:500]
            result.error_code = error_code or "NETWORK_REQUEST_FAILED"
            return result
        try:
            page = accept_source_response(
                owner_home=state.owner_home,
                audit_id=audit_id,
                adapter=adapter,
                checkpoint=current_checkpoint,
                context=plan.context,
                response=payload,
                max_records=requested_page_limit,
            )
        except SourceAdapterError as exc:
            result.error = str(exc)[:500]
            result.error_code = exc.code
            return result
        start = result.cursor
        page_keys = _adapter_page_record_keys(page.records, page.record_keys)
        if page.records and len(page_keys) != len(page.records):
            result.error = "来源适配器必须为每条完整记录提供来源位置键"
            result.error_code = "SOURCE_RECORD_KEYS_REQUIRED"
            return result
        result.events.extend((start + index, item) for index, item in enumerate(page.records))
        result.source_record_keys.update(
            {
                start + index: key
                for index, key in enumerate(page_keys)
            }
        )
        result.cursor += len(page.records)
        result.source_checkpoint = deepcopy(page.checkpoint)
        result.reached_end = not page.has_more
        if not page.has_more:
            break
    return result


def _adapter_page_record_keys(
    records: list[dict],
    declared: tuple[str, ...],
) -> list[str]:
    """Namespace source positions; never infer identity from record content."""

    if not records:
        return []
    if declared:
        return [f"source-position:{key}" for key in declared]
    return []


def apply_cursor_page_feedback(state, drain: DrainResult) -> None:
    """Persist a smaller HTTP cursor page after a typed oversized-page retry.

    This changes only the number of complete records requested per HTTP page.
    It never changes event contents, judgment batching, or file/poll sources.
    """
    reductions = int(getattr(drain, "page_limit_reductions", 0) or 0)
    effective = int(getattr(drain, "effective_page_limit", 0) or 0)
    current = int(getattr(state.tuning, "page_limit", 0) or 0)
    if reductions <= 0 or effective <= 0 or effective >= current:
        return
    state.tuning = replace(state.tuning, page_limit=effective)
    state.totals["page_limit_reductions"] = (
        int(state.totals.get("page_limit_reductions", 0) or 0) + reductions
    )


def drain_file_source(
    path: Path,
    byte_cursor: int,
    line_cursor: int,
    budget: DrainBudget,
    *,
    delimiter: bytes = b"\n",
    expected_identity: str = "",
    expected_fragment: bytes = b"",
) -> DrainResult:
    """从字节游标读取完整记录；默认一行一条，也支持精确多行分隔符。

    末尾未完成记录留在原游标，并通过 ``incomplete_fragment`` 交给调用方先持久化。
    有未完成片段时若文件身份或片段前缀变化，明确失败，绝不把新旧文件拼成一条。
    """
    result = DrainResult(cursor=byte_cursor)
    result.aux_cursor = line_cursor
    try:
        stat = path.stat()
    except OSError as exc:
        result.error = f"文件不可读: {exc}"[:500]
        result.error_code = "NETWORK_REQUEST_FAILED"
        return result
    identity = f"{stat.st_dev}:{stat.st_ino}"
    result.source_identity = identity
    if expected_identity and identity != expected_identity:
        if expected_fragment:
            result.error = "文件在未完成记录期间被替换，旧片段仍保留，未推进游标"
            result.error_code = "SOURCE_FRAGMENT_SOURCE_CHANGED"
            return result
        result.cursor = 0
        result.aux_cursor = 0
        if byte_cursor > 0:
            result.gap_events += 1
    elif stat.st_size < byte_cursor:
        if expected_fragment:
            result.error = "文件在未完成记录期间被截断，旧片段仍保留，未推进游标"
            result.error_code = "SOURCE_FRAGMENT_SOURCE_CHANGED"
            return result
        result.cursor = 0
        result.aux_cursor = 0
        result.gap_events += 1
    if not delimiter:
        result.error = "文件记录分隔符不能为空"
        result.error_code = "SOURCE_ENVELOPE_INVALID"
        return result
    try:
        _read_file_records(
            path,
            result,
            budget,
            delimiter=delimiter,
            expected_fragment=expected_fragment,
        )
    except OSError as exc:
        result.error = f"文件读取失败: {exc}"[:500]
        result.error_code = "NETWORK_REQUEST_FAILED"
    return result


def _read_file_records(
    path: Path,
    result: DrainResult,
    budget: DrainBudget,
    *,
    delimiter: bytes,
    expected_fragment: bytes,
) -> None:
    with path.open("rb") as handle:
        handle.seek(result.cursor)
        result.reached_end = _drain_open_handle(
            handle,
            result,
            budget,
            delimiter=delimiter,
            expected_fragment=expected_fragment,
        )


def _drain_open_handle(
    handle,
    result: DrainResult,
    budget: DrainBudget,
    *,
    delimiter: bytes = b"\n",
    expected_fragment: bytes = b"",
) -> bool:
    """逐条消费直到追平或预算耗尽；记录边界不参与业务解释。"""
    first = True
    while len(result.events) < budget.max_events and time.time() < budget.deadline:
        expected = expected_fragment if first else b""
        first = False
        if not _consume_one_record(handle, result, delimiter, expected):
            return not bool(result.error)
    return False


def _consume_one_record(
    handle,
    result: DrainResult,
    delimiter: bytes = b"\n",
    expected_fragment: bytes = b"",
) -> bool:
    """读一条完整物理记录；未完成时回到记录起点并交出完整片段。"""
    start = handle.tell()
    raw, complete, too_large = _read_delimited_record(handle, delimiter)
    result.fragment_checked = True
    if too_large:
        handle.seek(start)
        result.error = (
            f"单条文件记录超过 {_MAX_FILE_RECORD_BYTES} 字节；"
            "未截断、未推进游标，请调整来源成条方式或安全上限"
        )
        result.error_code = "SOURCE_RECORD_TOO_LARGE"
        return False
    if expected_fragment and not raw.startswith(expected_fragment):
        handle.seek(start)
        result.error = "文件尾部片段与持久片段不一致，未拼接、未推进游标"
        result.error_code = "SOURCE_FRAGMENT_MISMATCH"
        return False
    if not raw:
        if expected_fragment:
            result.error = "持久片段在来源中已不可见，未推进游标"
            result.error_code = "SOURCE_FRAGMENT_MISMATCH"
        return False
    if not complete:
        handle.seek(start)
        result.incomplete_fragment = raw
        result.fragment_start = start
        return False
    result.cursor = handle.tell()
    result.aux_cursor += 1
    body = raw[: -len(delimiter)]
    if delimiter == b"\n" and body.endswith(b"\r"):
        body = body[:-1]
    text = body.decode("utf-8", "replace")
    if text.strip():
        result.events.append((result.aux_cursor, _line_event(text)))
    return True


def _read_delimited_record(handle, delimiter: bytes) -> tuple[bytes, bool, bool]:
    """Return (raw_with_delimiter_or_fragment, complete, too_large)."""
    start = handle.tell()
    buffer = bytearray()
    while True:
        chunk = handle.read(64 * 1024)
        if chunk:
            buffer.extend(chunk)
            boundary = buffer.find(delimiter)
            if boundary >= 0:
                if boundary > _MAX_FILE_RECORD_BYTES:
                    handle.seek(start)
                    return b"", False, True
                end = boundary + len(delimiter)
                handle.seek(start + end)
                return bytes(buffer[:end]), True, False
            if len(buffer) > _MAX_FILE_RECORD_BYTES + len(delimiter):
                handle.seek(start)
                return b"", False, True
            continue
        handle.seek(start)
        if len(buffer) > _MAX_FILE_RECORD_BYTES:
            return b"", False, True
        return bytes(buffer), False, False


def _line_event(text: str) -> dict:
    """一行 → 一个事件:JSON 对象原样;其余(纯文本/非对象 JSON)包成 {"line": 原文}。
    纯结构包装,不解读内容；模型视图另有独立预算，本地原始记录不在这里截断。"""
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    return {"line": text}


def sample_file_lines(
    path: Path,
    count: int,
    *,
    delimiter: bytes = b"\n",
) -> tuple[list[dict], str]:
    """file 源从文件头按同一记录边界只读取样，不动正式游标。"""
    budget = DrainBudget(max_events=count, page_limit=count, deadline=time.time() + 15)
    drain = drain_file_source(path, 0, 0, budget, delimiter=delimiter)
    return [event for _seq, event in drain.events], drain.error


def file_record_delimiter(state) -> bytes:
    envelope = (
        state.source_envelope
        if isinstance(getattr(state, "source_envelope", None), dict)
        else {}
    )
    boundary = envelope.get("record_boundary")
    if not isinstance(boundary, dict) or boundary.get("mode") == "line":
        return b"\n"
    delimiter = boundary.get("delimiter")
    if not isinstance(delimiter, str) or not delimiter:
        return b""
    return delimiter.encode("utf-8")


def file_fragment_path(state) -> Path:
    from .watch_state import state_dir

    return state_dir(state.owner_home) / f"{state.watch_id}.fragment.bin"


def load_file_fragment(state) -> bytes | None:
    expected = int(getattr(state, "file_fragment_bytes", 0) or 0)
    if expected <= 0:
        return b""
    try:
        payload = file_fragment_path(state).read_bytes()
    except OSError:
        return None
    if len(payload) != expected:
        return None
    digest = str(getattr(state, "file_fragment_sha256", "") or "")
    if digest and sha256(payload).hexdigest() != digest:
        return None
    return payload


def persist_file_fragment(state, drain: DrainResult) -> bool:
    """Persist or clear the incomplete physical record before cursor commit."""
    if source_kind(state.source_url, getattr(state, "source_mode", "")) != "file":
        return True
    path = file_fragment_path(state)
    fragment = drain.incomplete_fragment
    try:
        if fragment is not None:
            from .watch_state import _unique_tmp

            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = _unique_tmp(path)
            try:
                with tmp.open("wb") as handle:
                    handle.write(fragment)
                    handle.flush()
                    os.fsync(handle.fileno())
                tmp.replace(path)
            finally:
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
            state.file_fragment_start = int(drain.fragment_start)
            state.file_fragment_bytes = len(fragment)
            state.file_fragment_sha256 = sha256(fragment).hexdigest()
        elif drain.fragment_checked:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            state.file_fragment_start = 0
            state.file_fragment_bytes = 0
            state.file_fragment_sha256 = ""
        if drain.source_identity:
            state.file_identity = drain.source_identity
    except OSError:
        return False
    return True


def drain_poll_source(
    fetch_json,
    source_url: str,
    cursor: int,
    *,
    due: bool,
    source_envelope: dict[str, object] | None = None,
) -> DrainResult:
    """poll 源 drain:due=False(节拍未到,由调用方按 last_poll_at×poll_query_seconds 算)
    → 空手追平(调用方照常长轮询等待);due=True → 执行一次已钉住请求,整份响应包成一条事件,
    游标自增。fetched_at>0 表示本次真的查了(调用方据此更新 last_poll_at)。"""
    result = DrainResult(cursor=cursor)
    result.reached_end = True
    if not due:
        return result
    now = time.time()
    request_facts = (source_envelope or {}).get("request")
    if not isinstance(request_facts, dict):
        result.error = "HTTP 来源缺少已验证的请求配置"
        result.error_code = "SOURCE_REQUEST_INVALID"
        return result
    try:
        request = render_source_http_request(
            source_url,
            request_facts,
            cursor=None,
            page_size=None,
        )
    except ValueError as exc:
        result.error = str(exc)[:500]
        result.error_code = (
            "SOURCE_SECRET_UNAVAILABLE"
            if "SecretRef" in str(exc)
            else "SOURCE_REQUEST_INVALID"
        )
        return result
    ok, payload, error_code = fetch_json(request)
    result.pages = 1
    result.fetched_at = now
    if not ok:
        result.error = str(payload)[:500]
        result.error_code = error_code or "NETWORK_REQUEST_FAILED"
        return result
    result.cursor = cursor + 1
    result.events.append((result.cursor, {"polled_at": round(now, 3), "response": payload}))
    return result


__all__ = [
    "apply_cursor_page_feedback",
    "drain_adapter_source",
    "drain_file_source",
    "drain_poll_source",
    "drain_watch_source",
    "file_path_of",
    "normalize_file_url",
    "file_fragment_path",
    "file_record_delimiter",
    "load_file_fragment",
    "persist_file_fragment",
    "sample_file_lines",
    "source_kind",
]
