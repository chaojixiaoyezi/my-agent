
from __future__ import annotations

"""三种源的增量采集器 —— "不丢"的第一道保证(断点状态)。

每种源记一个断点到状态文件,重启从断点续采"不重不丢":
  FileCollector   文件源:记 byte offset(+ inode/size 防 truncate 误判),从 offset 续读。
                  日志轮转(文件被截断/换新,size 变小)时 offset 归零从头读,不漏新内容。
  FolderCollector 文件夹源:记"已处理文件名集合",新出现的文件整文件读,已处理的跳过,不漏不重。
                  对仍在 append 的文件,记每文件 offset 增量补读(子文件当文件源处理)。
  ApiCollector    API 源:HTTP 轮询 ?since=<cursor>,记返回的 next 为新 cursor,下轮带上,不丢。

采集器只负责"把新行捞出来 + 更新断点状态",不落存档不初筛(那是 daemon 编排层的事)。
返回 CollectResult(新行 + 新状态),由编排层先 append archive 再 triage 再原子写状态 —— 顺序保证
崩溃在任一步都不丢:存档先写,状态后写,最坏情况是重启后重采已存档的行(存档去重靠指纹,不丢)。
"""

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .splitter import LineSplitter, RecordSplitter

# API 轮询单次最多取多少行进内存,防一次返回过大撑爆;够大不影响吞吐。
_API_MAX_LINES_PER_POLL = 100_000
_API_TIMEOUT_SECONDS = 10.0
# 文件夹一轮最多新处理多少个文件,防突发海量新文件单轮卡死;剩下的下一轮继续(不丢)。
_FOLDER_MAX_NEW_FILES_PER_ROUND = 2000
# 文件 mtime 静默超过此秒数 → 视为"已写完",末尾无换行的尾段按完整最后一行采集(不再当残行丢掉)。
# 救的是"写完即固定、最后一行无换行"的文件(如日志切片/cp 来的片段/folder 子文件);对持续 append
# 的活跃文件,正常每行带换行→尾段为空→此逻辑不触发,残行保护照旧。阈值取得比典型写入间隔大,避免误判。
_TRAILING_FLUSH_IDLE_SECONDS = 2.0
# range 模式一拍最多连续拉多少个编号区间(防单拍拉太多卡死),够追上快速吐数据的 API。
_API_RANGE_MAX_PAGES_PER_CYCLE = 50
# 默认切割器:按行(历来行为)。源有 profile.splitter 时由 daemon 传入对应切割器。
_DEFAULT_SPLITTER = LineSplitter()


@dataclass
class CollectResult:
    """一次采集的产出:新行 + 更新后的断点状态。new_lines 顺序即落档/初筛顺序。"""

    new_lines: list[str] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def count(self) -> int:
        return len(self.new_lines)

    def cursor_repr(self) -> Any:
        """给 metrics/status 看的"当前断点"摘要(文件 offset / 已处理文件数 / api cursor)。"""
        if "offset" in self.state:
            return self.state.get("offset")
        if "cursor" in self.state:
            return self.state.get("cursor")
        if "processed" in self.state:
            return f"{len(self.state.get('processed', []))} files"
        return None


def collect_file(
    locator: str,
    state: dict[str, Any],
    *,
    now: float | None = None,
    splitter: RecordSplitter | None = None,
) -> CollectResult:
    """文件源增量采集。state: {offset:int, size:int, inode:int}。

    不丢/不重要点:
      - 从 offset 续读到文件当前末尾,offset 推进到新末尾。
      - 轮转检测:当前 size < 记录的 size(文件被截断/换了同名新文件)→ offset 归零从头读,
        避免漏掉轮转后的新内容,也避免 seek 到超出文件尾读不到。
      - 残行保护 + 静默兜底:末尾没 \n 的尾段默认当"还没写完的残行"不采(避免把半行当一行);
        但当文件 mtime 已静默超过 idle_flush_seconds(写完即固定、最后一行就是没换行)→ 该尾段
        是完整的最后一行,必须采,否则像 folder 子文件那样永久丢最后一行。now 可注入便于测试。
      - 文件还不存在:返回空结果保持原状态(daemon 可能起在源之前),不报错不丢。
    """
    path = Path(locator)
    prev_offset = int(state.get("offset", 0) or 0)
    prev_size = int(state.get("size", 0) or 0)
    try:
        stat = path.stat()
    except FileNotFoundError:
        return CollectResult(new_lines=[], state=dict(state))
    except OSError as exc:
        return CollectResult(new_lines=[], state=dict(state), error=f"stat_failed: {exc}")

    size = stat.st_size
    inode = getattr(stat, "st_ino", 0)
    mtime = float(getattr(stat, "st_mtime", 0.0) or 0.0)
    start = prev_offset
    # 轮转/截断:文件变小了,从头读(否则 seek 越界 + 漏新内容)。
    if size < prev_size or (prev_offset > size):
        start = 0

    new_lines: list[str] = []
    new_offset = start
    try:
        with path.open("rb") as handle:
            handle.seek(start)
            raw = handle.read()
            new_offset = handle.tell()
    except OSError as exc:
        return CollectResult(new_lines=[], state=dict(state), error=f"read_failed: {exc}")

    text = raw.decode("utf-8", errors="replace")
    if text:
        now_ts = time.time() if now is None else now
        # 文件一段时间没动 = 写完了:末尾无闭合边界的尾段是完整记录(不是残行),要采。
        file_idle = (now_ts - mtime) >= _TRAILING_FLUSH_IDLE_SECONDS
        active_splitter = splitter if splitter is not None else _DEFAULT_SPLITTER
        new_lines, consumed = active_splitter.split(text, flush_trailing=file_idle)
        new_offset = start + consumed

    return CollectResult(
        new_lines=new_lines,
        state={"offset": new_offset, "size": size, "inode": inode},
    )


def collect_folder(
    locator: str,
    state: dict[str, Any],
    *,
    now: float | None = None,
    splitter: RecordSplitter | None = None,
) -> CollectResult:
    """文件夹源增量采集。state: {processed: {filename: offset}}。

    不丢/不重要点:
      - 列目录下所有 *.log(及无扩展名文件),对每个文件记一个 offset(当文件源增量读)。
      - 新文件:processed 里没有的,从 offset 0 整文件读,记进 processed。
      - 已处理文件仍在 append:从记录的 offset 续读增量,不重复给老行。
      - 子文件写完即固定、最后一行无换行:经 collect_file 的静默兜底采到(否则永久丢最后一行)。
        每轮都重扫已知文件,所以刚写完时残行留到下轮静默后补采,不丢。
      - 已消失的文件:保留其 processed 记录(避免它再出现时被当新文件重读);不报错。
    """
    folder = Path(locator)
    processed = dict(state.get("processed", {}) or {})
    if not folder.is_dir():
        return CollectResult(new_lines=[], state={"processed": processed})

    try:
        entries = sorted(p for p in folder.iterdir() if p.is_file())
    except OSError as exc:
        return CollectResult(new_lines=[], state={"processed": processed}, error=f"listdir_failed: {exc}")

    new_lines: list[str] = []
    for entry in _entries_within_new_file_budget(entries, processed):
        name = entry.name
        prev_offset = int(processed.get(name, 0) or 0)
        sub = collect_file(str(entry), {"offset": prev_offset, "size": prev_offset}, now=now, splitter=splitter)
        new_lines.extend(sub.new_lines)
        processed[name] = int(sub.state.get("offset", prev_offset) or prev_offset)

    return CollectResult(new_lines=new_lines, state={"processed": processed})


def _entries_within_new_file_budget(entries: list[Path], processed: dict[str, Any]) -> list[Path]:
    """选出本轮要处理的文件:所有已知文件 + 至多 _FOLDER_MAX_NEW_FILES_PER_ROUND 个新文件。

    新文件配额用尽后,后续新文件留到下一轮(不丢,只是延后),避免突发海量新文件单轮卡死。
    """
    known = [entry for entry in entries if entry.name in processed]
    fresh = [entry for entry in entries if entry.name not in processed]
    return known + fresh[:_FOLDER_MAX_NEW_FILES_PER_ROUND]


def collect_api(locator: str, state: dict[str, Any]) -> CollectResult:
    """API 源轮询采集。state: {cursor:int|str}。

    协议(对齐模拟器):GET <locator>?since=<cursor> → JSON {"next": <new_cursor>, "lines": [...]}。
    不丢要点:把返回的 next 记成新 cursor,下轮带上;next 没变 → 没新数据,cursor 不动。
    locator 已带 query 时用 & 续接,否则用 ?。网络错误返回原状态 + error,不丢(下轮重试同 cursor)。
    range 模式(state.mode=="range"):改按编号区间 ?start=N&end=M 分页拉,见 _collect_api_range。
    """
    if str(state.get("mode") or "") == "range":
        return _collect_api_range(locator, state)
    cursor = state.get("cursor", 0)
    if cursor is None:
        cursor = 0
    url = _build_poll_url(locator, cursor)
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=_API_TIMEOUT_SECONDS) as response:  # noqa: S310 - 受控轮询 URL
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        return CollectResult(new_lines=[], state={"cursor": cursor}, error=f"api_request_failed: {exc}")
    except OSError as exc:
        return CollectResult(new_lines=[], state={"cursor": cursor}, error=f"api_request_failed: {exc}")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        return CollectResult(new_lines=[], state={"cursor": cursor}, error=f"api_bad_json: {exc}")
    if not isinstance(payload, dict):
        return CollectResult(new_lines=[], state={"cursor": cursor}, error="api_bad_shape")

    raw_lines = payload.get("lines", []) or []
    new_lines = [str(item) for item in raw_lines if str(item).strip()][:_API_MAX_LINES_PER_POLL]
    next_cursor = payload.get("next", cursor)
    if next_cursor is None:
        next_cursor = cursor
    return CollectResult(new_lines=new_lines, state={"cursor": next_cursor})


def _collect_api_range(locator: str, state: dict[str, Any]) -> CollectResult:
    """按编号区间分页拉:每拍连续取 (cursor, cursor+batch] 直到追上服务器最新编号。

    不丢=编号连续无缺口。API 协议:GET ?start=N&end=M → {"lines":[...], "latest": 当前最大编号}。
    一拍拉多个区间(每个区间一次 HTTP,符合"按编号区间查"),封顶 _API_RANGE_MAX_PAGES_PER_CYCLE
    防快速吐数据时单拍卡死;没追上的下一拍继续(不丢)。
    """
    cursor = int(state.get("cursor", 0) or 0)
    batch = max(1, int(state.get("batch", 100) or 100))
    collected: list[str] = []
    for _ in range(_API_RANGE_MAX_PAGES_PER_CYCLE):
        lines, latest, error = _fetch_api_range(locator, cursor + 1, cursor + batch)
        if error:
            return CollectResult(
                new_lines=collected,
                state={"cursor": cursor, "mode": "range", "batch": batch},
                error=error,
            )
        collected.extend(lines)
        if latest <= cursor:
            break  # 服务器没有更新的编号了
        cursor = min(cursor + batch, latest)
        if cursor >= latest:
            break  # 追上最新编号
    return CollectResult(
        new_lines=collected[:_API_MAX_LINES_PER_POLL],
        state={"cursor": cursor, "mode": "range", "batch": batch},
    )


def _fetch_api_range(locator: str, start: int, end: int) -> tuple[list[str], int, str]:
    """拉单个编号区间 [start,end]。返回 (记录行, 服务器最大编号 latest, 错误串)。"""
    sep = "&" if ("?" in locator) else "?"
    url = f"{locator}{sep}start={start}&end={end}"
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=_API_TIMEOUT_SECONDS) as response:  # noqa: S310
            body = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as exc:
        return [], 0, f"api_request_failed: {exc}"
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        return [], 0, f"api_bad_json: {exc}"
    if not isinstance(payload, dict):
        return [], 0, "api_bad_shape"
    raw = payload.get("lines", []) or []
    lines = [str(item) for item in raw if str(item).strip()]
    latest = int(payload.get("latest", 0) or 0)
    return lines, latest, ""


def collect_source(
    kind: str,
    locator: str,
    state: dict[str, Any],
    *,
    splitter: RecordSplitter | None = None,
) -> CollectResult:
    """按源类型分发到对应采集器。未知类型返回空结果 + error(不崩 daemon)。

    splitter 透传:文件/文件夹按 splitter 切逻辑记录(默认按行);api 返回的已是切好的行,不过切割器。
    daemon 生产路径用真实时间;要注入 now 测静默兜底就直接测 collect_file/collect_folder。
    """
    if kind == "file":
        return collect_file(locator, state, splitter=splitter)
    if kind == "folder":
        return collect_folder(locator, state, splitter=splitter)
    if kind == "api":
        return collect_api(locator, state)
    return CollectResult(new_lines=[], state=dict(state), error=f"unknown_source_kind: {kind}")


def _build_poll_url(locator: str, cursor: Any) -> str:
    sep = "&" if ("?" in locator) else "?"
    return f"{locator}{sep}since={cursor}"


__all__ = [
    "CollectResult",
    "collect_api",
    "collect_file",
    "collect_folder",
    "collect_source",
]
