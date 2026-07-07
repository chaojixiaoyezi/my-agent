"""多形态数据源钉子:file 源(读大文件/日志 tail)+ poll 源(定时查快照接口)
+ judgment_note 轻量记忆(教一次别重教)。

契约:
- file:字节偏移游标逐行续读;JSON 对象行原样、其余行 {"line": 原文};尾部半行不消费;
  追加续读不重读;轮转/截断回 0 重读并记缺口;路径过危险根闸;stream_pos=行号(1-based)。
- poll:到节拍才查,整份响应包成一条事件;节拍未到空手追平;样本=当下一份响应。
- judgment_note:configure 存,快照持久化,重启 load 回来,open/sample/pull/status 载荷带回。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent.ingestion import harvester as hv
from agent.ingestion import watch_state as ws
from agent.ingestion import watch_tool as wt
from agent.ingestion.puller import DrainBudget
from agent.ingestion.sources import (
    drain_file_source,
    drain_poll_source,
    normalize_file_url,
    sample_file_lines,
    source_kind,
)
from agent.ingestion.watch_tool import WatchStreamTool


def _budget(max_events: int = 1000) -> DrainBudget:
    return DrainBudget(max_events=max_events, page_limit=400, deadline=time.time() + 10)


# ---------------------------------------------------------------------------
# file 源:纯 drain 层
# ---------------------------------------------------------------------------


def test_file_drain_json_and_text_lines(tmp_path: Path):
    log = tmp_path / "app.log"
    log.write_text(
        '{"kind":"login","status":"ok"}\n'
        "plain text warning line\n"
        "\n"
        '{"kind":"login","status":"denied"}\n',
        encoding="utf-8",
    )
    drain = drain_file_source(log, 0, 0, _budget())
    assert drain.error == ""
    assert drain.reached_end is True
    # 空行占行号不产事件:行号 1,2,4。
    assert [seq for seq, _ in drain.events] == [1, 2, 4]
    assert drain.events[0][1] == {"kind": "login", "status": "ok"}
    assert drain.events[1][1] == {"line": "plain text warning line"}
    assert drain.aux_cursor == 4
    assert drain.cursor == log.stat().st_size


def test_file_drain_tail_partial_line_and_append(tmp_path: Path):
    log = tmp_path / "grow.log"
    log.write_text('{"a":1}\n{"b":', encoding="utf-8")  # 尾部半行(写入中)
    first = drain_file_source(log, 0, 0, _budget())
    assert [seq for seq, _ in first.events] == [1]
    half_offset = first.cursor  # 停在半行前
    with log.open("a", encoding="utf-8") as handle:
        handle.write('2}\n{"c":3}\n')
    second = drain_file_source(log, first.cursor, first.aux_cursor, _budget())
    assert [(seq, event) for seq, event in second.events] == [(2, {"b": 2}), (3, {"c": 3})]
    assert second.cursor > half_offset and second.reached_end is True


def test_file_drain_rotation_resets_and_accounts_gap(tmp_path: Path):
    log = tmp_path / "rotate.log"
    log.write_text('{"a":1}\n{"a":2}\n{"a":3}\n', encoding="utf-8")
    first = drain_file_source(log, 0, 0, _budget())
    assert first.aux_cursor == 3
    log.write_text('{"fresh":1}\n', encoding="utf-8")  # 轮转:文件变短
    second = drain_file_source(log, first.cursor, first.aux_cursor, _budget())
    assert second.gap_events == 1  # 一次未知规模缺口,如实入账
    assert [(seq, event) for seq, event in second.events] == [(1, {"fresh": 1})]


def test_file_drain_missing_file_reports_error(tmp_path: Path):
    drain = drain_file_source(tmp_path / "nope.log", 0, 0, _budget())
    assert drain.error and not drain.events


def test_file_sample_reads_head_without_cursor(tmp_path: Path):
    log = tmp_path / "big.log"
    log.write_text("".join(f'{{"n":{i}}}\n' for i in range(50)), encoding="utf-8")
    events, error = sample_file_lines(log, 10)
    assert error == "" and len(events) == 10
    assert events[0] == {"n": 0}


def test_normalize_file_url_and_kind(tmp_path: Path):
    url = normalize_file_url(str(tmp_path / "x.log"))
    assert url.startswith("file:///")
    assert source_kind(url) == "file"
    assert source_kind("http://h/pull") == "cursor"
    assert source_kind("http://h/health", "poll") == "poll"
    with pytest.raises(ValueError):
        normalize_file_url("relative/path.log")


# ---------------------------------------------------------------------------
# poll 源:纯 drain 层
# ---------------------------------------------------------------------------


def test_poll_drain_wraps_snapshot_and_respects_beat():
    calls = {"n": 0}

    def fetch(url: str):
        calls["n"] += 1
        return True, {"status": "ok", "queue_depth": calls["n"]}, ""

    first = drain_poll_source(fetch, "http://h/health", 0, due=True)
    assert calls["n"] == 1 and first.cursor == 1 and first.fetched_at > 0
    seq, event = first.events[0]
    assert seq == 1 and event["response"] == {"status": "ok", "queue_depth": 1} and "polled_at" in event
    # 节拍未到(due=False,由 drain_watch_source 按 last_poll_at 算):不查、空手追平。
    second = drain_poll_source(fetch, "http://h/health", 1, due=False)
    assert calls["n"] == 1 and second.events == [] and second.reached_end is True and second.fetched_at == 0.0


def test_poll_drain_propagates_fetch_error():
    drain = drain_poll_source(lambda url: (False, "boom", "NETWORK_REQUEST_FAILED"), "http://h/x", 3, due=True)
    assert drain.error == "boom" and drain.cursor == 3 and drain.events == []


# ---------------------------------------------------------------------------
# 工具层端到端(open/pull/sample/configure + 持久化回环)
# ---------------------------------------------------------------------------


@pytest.fixture()
def owner_home(tmp_path, monkeypatch):
    fresh = ws.WatchRegistry()
    monkeypatch.setattr(ws, "registry", fresh)
    monkeypatch.setattr(wt, "registry", fresh)
    monkeypatch.setattr(hv, "harvesters", hv._HarvesterRegistry())
    return tmp_path / "owner"


def _tool(owner_home: Path, fetch=None) -> WatchStreamTool:
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u-test"))
    tool = WatchStreamTool(agent)
    tool.allow_private_resolution = True
    if fetch is not None:
        tool._fetch_json = fetch
    return tool


def _payload(result) -> dict:
    assert result.ok, result.output
    return json.loads(result.output)


def test_file_watch_end_to_end_pull_and_resume(tmp_path: Path, owner_home: Path):
    log = tmp_path / "events.log"
    log.write_text("".join(f'{{"kind":"beat","status":"ok","n":{i}}}\n' for i in range(6)), encoding="utf-8")
    tool = _tool(owner_home)
    opened = _payload(tool.execute({"action": "open", "url": str(log), "background_harvest": 0}))
    assert opened["ok"]
    pulled = _payload(tool.execute({"action": "pull", "watch_id": opened["watch_id"], "max_wait_seconds": 0}))
    assert [row["stream_pos"] for row in pulled["candidates"]] == [1, 2, 3, 4, 5, 6]
    assert pulled["coverage"]["reached_stream_end"] is True
    # 追加两行 → 再 pull 从行号续,不重读。
    with log.open("a", encoding="utf-8") as handle:
        handle.write('{"kind":"beat","status":"ok","n":6}\n{"kind":"beat","status":"weird","n":7}\n')
    again = _payload(tool.execute({"action": "pull", "watch_id": opened["watch_id"], "max_wait_seconds": 0}))
    assert [row["stream_pos"] for row in again["candidates"]] == [7, 8]


def test_file_watch_blocks_dangerous_path(owner_home: Path):
    tool = _tool(owner_home)
    result = tool.execute({"action": "open", "url": "file:///etc/hosts"})
    assert not result.ok
    assert "拒" in result.output


def test_poll_watch_end_to_end(owner_home: Path):
    snapshots = iter([
        {"status": "ok", "queue_depth": 1},
        {"status": "ok", "queue_depth": 2},
    ])

    def fetch(url: str):
        return True, next(snapshots), ""

    tool = _tool(owner_home, fetch)
    opened = _payload(tool.execute({
        "action": "open", "url": "http://127.0.0.1:9/health", "mode": "poll",
        "poll_query_seconds": 5, "background_harvest": 0,
    }))
    assert opened["ok"]
    # open 探针消费了第一份快照(信封);pull 拿到第二份包装成事件。
    pulled = _payload(tool.execute({"action": "pull", "watch_id": opened["watch_id"], "max_wait_seconds": 0}))
    assert len(pulled["candidates"]) == 1
    event = pulled["candidates"][0]["event"]
    assert event["response"]["queue_depth"] == 2
    state = ws.registry.get_or_load(owner_home, opened["watch_id"])
    assert state.source_mode == "poll" and state.last_poll_at > 0


def test_judgment_note_roundtrip_and_reload(owner_home: Path):
    """轻量记忆:教一次 → 持久化 → 重启(重新 load)→ open/pull/status 都带回,不用重教。"""
    def fetch(url: str):
        return True, {"items": [], "next_cursor": 0}, ""

    tool = _tool(owner_home, fetch)
    opened = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "background_harvest": 0}))
    note = "用户教:latency>500ms 且 retry>3 才要紧;state=degraded 是常态别报"
    configured = _payload(tool.execute({"action": "configure", "watch_id": opened["watch_id"], "judgment_note": note}))
    assert configured["judgment_note"] == note
    # 只给 note 不给 spec 也算合法 configure(有的源只需要判读须知)。
    assert configured["spec"] is None
    # 模拟重启:清注册表,从盘上快照复活。
    ws.registry.drop(opened["watch_id"])
    reloaded = ws.load_state(owner_home, opened["watch_id"])
    assert reloaded is not None and reloaded.judgment_note == note
    ws.registry.put(reloaded)
    reopened = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "background_harvest": 0}))
    assert reopened["resumed_existing_watch"] is True
    assert reopened["judgment_note"] == note
    assert "不用" in reopened["guidance"]  # open 引导:已有须知,不用重教
    status = _payload(tool.execute({"action": "status", "watch_id": opened["watch_id"]}))
    assert status["judgment_note"] == note
    pulled = _payload(tool.execute({"action": "pull", "watch_id": opened["watch_id"], "max_wait_seconds": 0}))
    assert pulled["judgment_note"] == note


def test_reopen_after_close_and_restart_clears_closed(owner_home: Path):
    """真机实锤防回归:close 过的源,重启(注册表清空、从盘复活)后再显式 open,
    closed 必须翻回 False 且 persist 后不被盘上旧 True 单调合并吃回——否则收割线程
    按 closed 自停,重开的盯守空转(游标不动、永远零候选)。"""
    def fetch(url: str):
        return True, {"items": [], "next_cursor": 0}, ""

    tool = _tool(owner_home, fetch)
    opened = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "background_harvest": 0}))
    _payload(tool.execute({"action": "close", "watch_id": opened["watch_id"]}))
    assert ws.load_state(owner_home, opened["watch_id"]).closed is True
    ws.registry.drop(opened["watch_id"])  # 模拟重启:进程内注册表清空
    reopened = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "background_harvest": 0}))
    assert reopened["resumed_existing_watch"] is True
    state = ws.registry.get_or_load(owner_home, opened["watch_id"])
    assert state.closed is False
    ws.persist_state(state)  # 收割拍/后续 persist 也不得把它翻回 True
    assert ws.load_state(owner_home, opened["watch_id"]).closed is False


def test_reopen_with_window_starts_fresh_window(owner_home: Path):
    """真机实锤防回归:close 过(或旧窗已走完)的源带窗口重开=新一场盯守,窗口起点
    必须重置——否则沿用旧 opened_at,窗口生下来就 complete,收割自停+模型直接收工。"""
    def fetch(url: str):
        return True, {"items": [], "next_cursor": 0}, ""

    tool = _tool(owner_home, fetch)
    opened = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "watch_window_seconds": 600, "background_harvest": 0}))
    state = ws.registry.get_or_load(owner_home, opened["watch_id"])
    state.opened_at -= 5000  # 旧场早已走完
    _payload(tool.execute({"action": "close", "watch_id": opened["watch_id"]}))
    reopened = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "watch_window_seconds": 720, "background_harvest": 0}))
    assert reopened["watch"]["window_complete"] is False
    assert reopened["watch"]["remaining_seconds"] > 600


def test_midwindow_takeover_open_keeps_original_window(owner_home: Path):
    """补岗接管中途 open(带同样的窗口参数)不得重置窗口起点:续的是原窗,不是加时。"""
    def fetch(url: str):
        return True, {"items": [], "next_cursor": 0}, ""

    tool = _tool(owner_home, fetch)
    opened = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "watch_window_seconds": 600, "background_harvest": 0}))
    state = ws.registry.get_or_load(owner_home, opened["watch_id"])
    state.opened_at -= 300  # 窗口过半,未走完
    again = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "watch_window_seconds": 600, "background_harvest": 0}))
    assert again["resumed_existing_watch"] is True
    assert 250 <= again["watch"]["remaining_seconds"] <= 320  # 原窗剩余,没有被重置回 600


def test_configure_requires_spec_or_note(owner_home: Path):
    def fetch(url: str):
        return True, {"items": [], "next_cursor": 0}, ""

    tool = _tool(owner_home, fetch)
    opened = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "background_harvest": 0}))
    result = tool.execute({"action": "configure", "watch_id": opened["watch_id"]})
    assert not result.ok
