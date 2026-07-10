"""watch_stream 工具:open/pull/status/close/list、跨进程复活、出站闸、审计面隔离。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent.ingestion import watch_state as ws
from agent.ingestion.watch_tool import WatchStreamTool


class _FakeSource:
    """内存版游标源:GET ?since&limit 语义与正式测试台一致。"""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def feed(self, count: int, make=None) -> None:
        base = len(self.events)
        for index in range(count):
            seq = base + index
            event = (make or (lambda s: {"seq": s, "kind": "beat", "flag": False}))(seq)
            self.events.append(event)

    def handle(self, url: str) -> tuple[bool, object, str]:
        from urllib.parse import parse_qs, urlsplit

        query = parse_qs(urlsplit(url).query)
        since = int(query.get("since", ["0"])[0])
        limit = int(query.get("limit", ["50"])[0])
        items = [dict(event) for event in self.events if event["seq"] >= since][:limit]
        next_cursor = (items[-1]["seq"] + 1) if items else max(since, len(self.events))
        return True, {"items": items, "next_cursor": next_cursor}, ""


@pytest.fixture()
def owner_home(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "registry", ws.WatchRegistry())
    return tmp_path / "owner"


def _tool(owner_home: Path, source: _FakeSource) -> WatchStreamTool:
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u-test"))
    tool = WatchStreamTool(agent)
    tool.allow_private_resolution = True
    tool._fetch_json = source.handle  # 注入内存源(网络闸另测)
    return tool


def _payload(result) -> dict:
    assert result.ok, result.output
    return json.loads(result.output)


def test_open_pull_status_close_roundtrip(owner_home):
    source = _FakeSource()
    source.feed(600)
    source.events.append({"seq": 600, "kind": "beat", "flag": True})
    tool = _tool(owner_home, source)

    opened = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "watch_window_seconds": 1200, "background_harvest": 0}))
    watch_id = opened["watch_id"]
    assert opened["resumed_existing_watch"] is False

    # 学判据后再盯(生产正流程 learn→monitor):配了结构规则(flag=false 是常态)才做压缩降维。
    # 未配 spec 的冷启动=宁滥勿漏无条件全读(根因2,另见 test_ingestion_full_read),这里给规则
    # 以验证"已学出结构判据的稳态源"仍正常压组、稀有目标照抬。
    _payload(tool.execute({"action": "configure", "watch_id": watch_id, "spec": {"result_field": "flag", "normal_values": ["false"]}}))

    pulled = _payload(tool.execute({"action": "pull", "watch_id": watch_id}))
    flagged = [c for c in pulled["candidates"] if '"flag": true' in json.dumps(c["event"]).lower() or c["event"].get("flag") is True]
    assert flagged, pulled["candidates"]
    assert pulled["coverage"]["reached_stream_end"] is True
    assert pulled["coverage"]["cursor"] == 601
    assert pulled["watch"]["watch_window_seconds"] == 1200
    assert pulled["suppressed_events_this_call"] > 500

    status = _payload(tool.execute({"action": "status", "watch_id": watch_id}))
    assert status["coverage"]["cursor"] == 601

    closed = _payload(tool.execute({"action": "close", "watch_id": watch_id}))
    assert closed["watch"]["closed"] is True
    assert closed["spool_backlog_candidates_at_close"] == 0  # inline 模式无 spool 积压
    assert "discarded_backlog_note" not in closed

    listed = _payload(tool.execute({"action": "list"}))
    assert listed["count"] == 1 and listed["watches"][0]["closed"] is True


def _tool_with_run(owner_home: Path, source: _FakeSource, run_id: str) -> WatchStreamTool:
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u-test"),
        _current_run_params=SimpleNamespace(run_id=run_id),
    )
    tool = WatchStreamTool(agent)
    tool.allow_private_resolution = True
    tool._fetch_json = source.handle
    return tool


def test_fanout_hint_fires_when_one_run_opens_multiple_sources(owner_home):
    """根因4 结构探针:同一个 run(一个子代理)开第 2 路 watch 时,open 回执给出"一源一子代理"
    扇出提示。纯按 opened_by_run 计数触发,不判内容。"""
    source = _FakeSource()
    source.feed(5)
    tool = _tool_with_run(owner_home, source, run_id="run-solo-multi")

    first = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "background_harvest": 0}))
    assert "fanout_hint" not in first  # 第一路不提示

    second = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:10/pull", "background_harvest": 0}))
    assert "fanout_hint" in second and second.get("watches_this_run") == 2
    assert "一源一子代理" in second["fanout_hint"] or "一个数据源" in second["fanout_hint"]

    third = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:11/pull", "background_harvest": 0}))
    assert third.get("watches_this_run") == 3


def test_no_fanout_hint_for_one_source_per_run(owner_home):
    """正解形态:一源一子代理(每个 run 只开一路 watch)→ 不触发扇出提示。"""
    source = _FakeSource()
    source.feed(5)
    tool_a = _tool_with_run(owner_home, source, run_id="run-a")
    tool_b = _tool_with_run(owner_home, source, run_id="run-b")

    a = _payload(tool_a.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "background_harvest": 0}))
    b = _payload(tool_b.execute({"action": "open", "url": "http://127.0.0.1:10/pull", "background_harvest": 0}))
    assert "fanout_hint" not in a and "fanout_hint" not in b  # 不同 run 各盯一路,不误报


def test_no_fanout_hint_without_run_context(owner_home):
    """无编排上下文(run_id 空,如裸测试/直连)→ 不触发(opened_by_run 空不计数)。"""
    source = _FakeSource()
    source.feed(5)
    tool = _tool(owner_home, source)  # 无 _current_run_params → run_id=""
    tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "background_harvest": 0})
    second = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:10/pull", "background_harvest": 0}))
    assert "fanout_hint" not in second


def test_close_surfaces_unjudged_spool_backlog(owner_home):
    """g8 不足4·不静默弃判:close 时 spool 还有已抬未判候选 → 关闭回执如实亮出数目与提示
    (纯计数,不拦关闭;要盯完先 pull 清账再 close)。"""
    source = _FakeSource()
    tool = _tool(owner_home, source)
    state = ws.new_state(owner_home, "http://127.0.0.1:9/pull", {"background_harvest": 0})
    state.totals["spool_candidates"] = 97
    ws.persist_state(state)
    (ws.state_dir(owner_home) / f"{state.watch_id}.read.json").write_text(
        json.dumps({"read_seq": 0, "candidates_consumed": 40, "updated_at": 0}), encoding="utf-8"
    )

    closed = _payload(tool.execute({"action": "close", "watch_id": state.watch_id}))

    assert closed["spool_backlog_candidates_at_close"] == 57
    assert "57" in closed["discarded_backlog_note"]


def test_pull_resumes_from_disk_after_process_restart(owner_home):
    source = _FakeSource()
    source.feed(300)
    tool = _tool(owner_home, source)
    opened = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "background_harvest": 0}))
    _payload(tool.execute({"action": "pull", "watch_id": opened["watch_id"]}))

    # 模拟进程重启:清空进程内注册表,新工具实例从盘上快照复活并续游标。
    ws.registry = ws.WatchRegistry()
    source.feed(50)
    tool2 = _tool(owner_home, source)
    pulled = _payload(tool2.execute({"action": "pull", "watch_id": opened["watch_id"]}))
    assert pulled["coverage"]["cursor"] == 350
    assert pulled["coverage"]["seen_this_call"] == 50
    assert pulled["engine_totals"]["events_seen"] == 350


def test_open_reuses_watch_for_same_url(owner_home):
    source = _FakeSource()
    source.feed(10)
    tool = _tool(owner_home, source)
    first = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "background_harvest": 0}))
    second = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "background_harvest": 0}))
    assert first["watch_id"] == second["watch_id"]
    assert second["resumed_existing_watch"] is True


def test_private_host_blocked_without_grant(owner_home):
    tool = WatchStreamTool(SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u")))
    tool.allow_private_resolution = False
    result = tool.execute({"action": "open", "url": "http://192.168.77.10:8901/pull", "background_harvest": 0})
    assert not result.ok
    assert result.error_code in {"NETWORK_PRIVATE_HOST_BLOCKED", "NETWORK_PRIVATE_IP_BLOCKED"}


def test_private_host_allowed_with_injected_grant(owner_home):
    source = _FakeSource()
    source.feed(5)
    tool = _tool(owner_home, source)
    tool.allow_private_resolution = None
    tool.allowed_private_hosts = ("192.168.77.10",)
    opened = _payload(tool.execute({"action": "open", "url": "http://192.168.77.10:8901/pull", "background_harvest": 0}))
    assert opened["ok"] is True


def test_audit_file_is_ndjson_outside_report_surface(owner_home):
    source = _FakeSource()
    source.feed(120)
    tool = _tool(owner_home, source)
    opened = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "background_harvest": 0}))
    _payload(tool.execute({"action": "pull", "watch_id": opened["watch_id"]}))
    audit_files = list((owner_home / "watch_state").glob("*.audit.ndjson"))
    assert audit_files, "审计账必须落盘"
    # 对账脚本的扫描面(.jsonl/.json/.md/.txt/.log)绝不能包含审计账后缀,否则原始事件 ID 混进上报面。
    assert all(path.suffix == ".ndjson" for path in audit_files)
    record = json.loads(audit_files[0].read_text(encoding="utf-8").splitlines()[0])
    assert "cursor_to" in record and "groups" in record


def test_pull_unknown_watch_id_lists_known(owner_home):
    source = _FakeSource()
    tool = _tool(owner_home, source)
    result = tool.execute({"action": "pull", "watch_id": "ws-nope"})
    assert not result.ok
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"


def test_source_error_persists_cursor_and_reports(owner_home):
    source = _FakeSource()
    source.feed(40)
    tool = _tool(owner_home, source)
    opened = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "background_harvest": 0}))
    _payload(tool.execute({"action": "pull", "watch_id": opened["watch_id"]}))

    tool._fetch_json = lambda url: (False, "boom", "NETWORK_REQUEST_FAILED")
    result = tool.execute({"action": "pull", "watch_id": opened["watch_id"]})
    assert not result.ok
    body = json.loads(result.output)
    assert body["coverage"]["cursor"] == 40
    assert result.error_code == "NETWORK_REQUEST_FAILED"
