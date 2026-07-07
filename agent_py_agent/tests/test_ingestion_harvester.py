"""后台连续摄取(harvester)钉子(§8-1 加难实锤:900 条/秒 + 源端滚动缓冲 ~6 分钟淘汰,
模型研判期间无人拉流 → 淘汰即永久丢,命中全集中在早期序号)。

钉死六层契约:
1. 解耦:模型不 pull 的时间里,收割线程持续拉流喂引擎,候选批落 spool——源端
   缓冲淘汰不再造成丢失(游标始终追平流末尾)。
2. 消费契约:pull 从 spool 取候选批,载荷字段与 inline 模式同构(candidates/
   suppressed_groups/coverage/watch),模型判读无感;积压如实入账(spool_backlog_*)。
3. 判定零污染:候选行与 inline 同源渲染(candidate_rows),本层不加任何定性。
4. 世代轮转:积压清零且文件超限时换代,读者按 generation 对齐,不重不漏。
5. 生命周期:close 停线程;窗口走完(+余量)自停;源报错不放弃(退避续拉,
   缺口在 gap 账目如实体现)。
6. 跨进程:租约防双收;重启后从盘上游标续,读游标 sidecar 各自独立。
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
from agent.ingestion.watch_tool import WatchStreamTool


class _FakeSource:
    """内存版游标源:支持"滚动缓冲淘汰"(只保留最近 retain 条),复刻测试台 RING 语义。"""

    def __init__(self, retain: int = 0) -> None:
        self.events: list[dict] = []
        self.retain = retain
        self.fail = False

    def feed(self, count: int, make=None) -> None:
        base = len(self.events) + getattr(self, "_dropped", 0)
        for index in range(count):
            seq = base + index
            event = (make or (lambda s: {"seq": s, "kind": "beat", "flag": False}))(seq)
            self.events.append(event)
        if self.retain and len(self.events) > self.retain:
            dropped = len(self.events) - self.retain
            self._dropped = getattr(self, "_dropped", 0) + dropped
            self.events = self.events[dropped:]

    def handle(self, url: str) -> tuple[bool, object, str]:
        if self.fail:
            return False, "boom", "NETWORK_REQUEST_FAILED"
        from urllib.parse import parse_qs, urlsplit

        query = parse_qs(urlsplit(url).query)
        since = int(query.get("since", ["0"])[0])
        limit = int(query.get("limit", ["50"])[0])
        items = [dict(event) for event in self.events if event["seq"] >= since][:limit]
        total = getattr(self, "_dropped", 0) + len(self.events)
        next_cursor = (items[-1]["seq"] + 1) if items else max(since, total)
        return True, {"items": items, "next_cursor": next_cursor}, ""


@pytest.fixture()
def owner_home(tmp_path, monkeypatch):
    # watch_tool 按值导入 registry:两处必须换成同一个新实例,工具与测试才看同一 state 对象。
    fresh = ws.WatchRegistry()
    monkeypatch.setattr(ws, "registry", fresh)
    monkeypatch.setattr(wt, "registry", fresh)
    monkeypatch.setattr(hv, "harvesters", hv._HarvesterRegistry())
    return tmp_path / "owner"


def _tool(owner_home: Path, source: _FakeSource) -> WatchStreamTool:
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u-test"))
    tool = WatchStreamTool(agent)
    tool.allow_private_resolution = True
    tool._fetch_json = source.handle
    return tool


def _payload(result) -> dict:
    assert result.ok, result.output
    return json.loads(result.output)


def _open(tool, **extra) -> dict:
    params = {"action": "open", "url": "http://127.0.0.1:9/pull", "watch_window_seconds": 1200}
    params.update(extra)
    return _payload(tool.execute(params))


def _wait_until(predicate, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _state(owner_home: Path, watch_id: str):
    return ws.registry.get_or_load(owner_home, watch_id)


def test_harvester_drains_while_model_is_thinking(owner_home):
    source = _FakeSource()
    source.feed(200)
    source.events.append({"seq": 200, "kind": "beat", "flag": True})
    tool = _tool(owner_home, source)
    opened = _open(tool)
    state = _state(owner_home, opened["watch_id"])
    # 模型没有调 pull——收割者自己把流喝干、候选落 spool。
    assert _wait_until(lambda: state.cursor >= 201 and state.spool_seq >= 1)
    assert hv.spool_path(state).exists()

    pulled = _payload(tool.execute({"action": "pull", "watch_id": opened["watch_id"], "max_wait_seconds": 5}))
    flagged = [c for c in pulled["candidates"] if c["event"].get("flag") is True]
    assert flagged, pulled["candidates"]
    assert pulled["coverage"]["cursor"] >= 201
    assert pulled["harvester"]["running"] is True
    assert "spool_backlog_candidates" in pulled["coverage"]


def test_rolling_buffer_eviction_does_not_lose_events(owner_home):
    # 源只保留最近 60 条(滚动淘汰);持续喂入期间收割者跟拉,事件不因淘汰而丢。
    source = _FakeSource(retain=60)
    tool = _tool(owner_home, source)
    opened = _open(tool)
    state = _state(owner_home, opened["watch_id"])
    for _round in range(6):
        source.feed(50)
        assert _wait_until(lambda: state.last_reached_end and state.cursor >= len(source.events) + getattr(source, "_dropped", 0), timeout=6.0)
    assert state.cursor >= 300
    assert state.totals["gap_events"] == 0  # 全程跟拉:一条都没被淘汰掉
    assert state.engine.totals["events_seen"] >= 300


def test_pull_consumes_backlog_in_order_and_accounts_rest(owner_home):
    source = _FakeSource()
    tool = _tool(owner_home, source)
    opened = _open(tool, max_candidates_per_pull=2)
    state = _state(owner_home, opened["watch_id"])
    # 三批各含一个稀有事件 → 至少 3 条候选进 spool。
    for burst in range(3):
        source.feed(1, make=lambda s, b=burst: {"seq": s, "kind": f"rare-{b}", "flag": True})
        source.feed(30)
        assert _wait_until(lambda: state.cursor >= len(source.events), timeout=6.0)
    assert _wait_until(lambda: state.totals.get("spool_candidates", 0) >= 3)

    first = _payload(tool.execute({"action": "pull", "watch_id": opened["watch_id"], "max_wait_seconds": 3}))
    assert first["candidates"], "积压候选必须立即可取"
    remaining = first["coverage"]["spool_backlog_candidates"]
    second = _payload(tool.execute({"action": "pull", "watch_id": opened["watch_id"], "max_wait_seconds": 3}))
    if remaining > 0:
        assert second["candidates"], "第二次 pull 应继续消化积压"
        assert second["coverage"]["spool_backlog_candidates"] <= remaining
    # 顺序性:spool 序号单调 → 候选流不乱序。
    positions = [c["stream_pos"] for c in first["candidates"] + second["candidates"]]
    assert positions == sorted(positions)


def test_close_stops_harvester_thread(owner_home):
    source = _FakeSource()
    source.feed(20)
    tool = _tool(owner_home, source)
    opened = _open(tool)
    state = _state(owner_home, opened["watch_id"])
    assert _wait_until(lambda: hv.harvesters.get_live(state.watch_id) is not None)
    handle = hv.harvesters.get_live(state.watch_id)
    _payload(tool.execute({"action": "close", "watch_id": opened["watch_id"]}))
    assert _wait_until(lambda: not handle.thread.is_alive(), timeout=10.0)


def test_window_complete_stops_harvester(owner_home, monkeypatch):
    monkeypatch.setattr(hv, "_WINDOW_GRACE_SECONDS", 0.0)
    source = _FakeSource()
    source.feed(10)
    tool = _tool(owner_home, source)
    opened = _open(tool, watch_window_seconds=30)
    state = _state(owner_home, opened["watch_id"])
    assert _wait_until(lambda: hv.harvesters.get_live(state.watch_id) is not None)
    handle = hv.harvesters.get_live(state.watch_id)
    state.opened_at = time.time() - 120  # 把窗口拨到已走完
    assert _wait_until(lambda: not handle.thread.is_alive(), timeout=10.0)


def test_source_error_keeps_trying_and_reports_honestly(owner_home):
    source = _FakeSource()
    source.feed(40)
    tool = _tool(owner_home, source)
    opened = _open(tool)
    state = _state(owner_home, opened["watch_id"])
    assert _wait_until(lambda: state.cursor >= 40)
    source.fail = True
    assert _wait_until(lambda: state.last_error != "", timeout=6.0)
    pulled = _payload(tool.execute({"action": "pull", "watch_id": opened["watch_id"], "max_wait_seconds": 2}))
    assert pulled["last_source_error"], "源故障必须在载荷里如实可见"
    assert pulled["coverage"]["cursor"] >= 40
    # 收割者不放弃:源恢复后续拉、游标继续推进。
    source.fail = False
    source.feed(20)
    assert _wait_until(lambda: state.cursor >= 60, timeout=10.0)


def test_spool_rotation_keeps_reader_continuity(owner_home, monkeypatch):
    monkeypatch.setattr(hv, "_SPOOL_ROTATE_BYTES", 1)  # 每次消费追平后都触发轮转
    source = _FakeSource()
    tool = _tool(owner_home, source)
    opened = _open(tool)
    state = _state(owner_home, opened["watch_id"])
    seen: list[int] = []
    for burst in range(3):
        source.feed(1, make=lambda s, b=burst: {"seq": s, "kind": f"one-{b}", "flag": True})
        source.feed(20)
        assert _wait_until(lambda: state.cursor >= len(source.events), timeout=6.0)
        assert _wait_until(lambda: state.totals.get("spool_candidates", 0) > len(seen))
        pulled = _payload(tool.execute({"action": "pull", "watch_id": opened["watch_id"], "max_wait_seconds": 3}))
        seen.extend(c["stream_pos"] for c in pulled["candidates"])
    assert len(seen) == len(set(seen)), "轮转后不得重复投递"
    assert state.spool_generation >= 1, "应发生过至少一次换代"


def test_restart_resumes_harvest_from_disk_cursor(owner_home):
    source = _FakeSource()
    source.feed(100)
    tool = _tool(owner_home, source)
    opened = _open(tool)
    state = _state(owner_home, opened["watch_id"])
    assert _wait_until(lambda: state.cursor >= 100)
    hv.stop_harvester(state.watch_id)
    # 模拟进程重启:注册表清空,新工具从盘上快照复活,收割续游标(不重读旧事件)。
    reborn = ws.WatchRegistry()
    ws.registry = reborn
    wt.registry = reborn
    source.feed(50)
    tool2 = _tool(owner_home, source)
    pulled = _payload(tool2.execute({"action": "pull", "watch_id": opened["watch_id"], "max_wait_seconds": 5}))
    state2 = _state(owner_home, opened["watch_id"])
    assert _wait_until(lambda: state2.cursor >= 150, timeout=8.0)
    assert state2.engine.totals["events_seen"] >= 150
    assert pulled["watch"]["watch_window_seconds"] == 1200


def test_cold_start_backlog_chunking_surfaces_late_rare_events(owner_home):
    """真机实锤(fleet2 回归):冷启动一次 drain 12421 条整批 process,91 条达标稀有
    挤 8 个候选位,位于积压中段的真命中落 overflow(审计有账、模型看不见)。
    分片喂引擎后,候选位随积压量线性扩,积压中段/尾段的稀有事件必须能进 spool。"""
    source = _FakeSource()
    # 5000 条积压:头部一批同质噪声 + 中段(2500)与尾段(4800)各埋一个稀有事件。
    source.feed(2500)
    source.feed(1, make=lambda s: {"seq": s, "kind": "rare-mid", "flag": True})
    source.feed(2299)
    source.feed(1, make=lambda s: {"seq": s, "kind": "rare-tail", "flag": True})
    source.feed(199)
    tool = _tool(owner_home, source)
    opened = _open(tool)  # 默认 harvest_chunk_events=500
    state = _state(owner_home, opened["watch_id"])
    # 等「全部积压喂完引擎」(events_seen):drain 一到位 cursor 就是 5000、spool>=2 中途即满足,
    # 机器忙时片循环还没消化到尾段就读 spool——基线即有的竞态挂法,这里等真正的完成信号。
    assert _wait_until(lambda: state.engine.totals.get("events_seen", 0) >= 5000, timeout=8.0)
    assert _wait_until(lambda: state.totals.get("spool_candidates", 0) >= 2)
    spooled = hv.spool_path(state).read_text(encoding="utf-8")
    assert '"rare-mid"' in spooled, "积压中段的稀有事件必须进候选批"
    assert '"rare-tail"' in spooled, "积压尾段的稀有事件必须进候选批"


def test_source_envelope_probed_at_open_and_surfaces_in_pull(owner_home):
    """判据锚定(fleet2 实锤:B 路自立判据 20+ 误报):源信封的标量元数据(schema_note 等)
    在 open 探针抓取、原样透传进 open/pull 载荷;跨进程重启后从快照恢复。代码只搬运不解读。"""

    class _NotedSource(_FakeSource):
        def handle(self, url):
            ok, payload, code = super().handle(url)
            if ok and isinstance(payload, dict):
                payload["api"] = "pay_stream"
                payload["schema_note"] = "结果端判据: state=captured 才是真命中"
            return ok, payload, code

    source = _NotedSource()
    source.feed(30)
    tool = _tool(owner_home, source)
    opened = _open(tool)
    assert opened["source_envelope"]["schema_note"].startswith("结果端判据")
    assert opened["source_envelope"]["api"] == "pay_stream"
    assert "items" not in opened["source_envelope"]  # 列表类不进信封,只透传标量
    pulled = _payload(tool.execute({"action": "pull", "watch_id": opened["watch_id"], "max_wait_seconds": 3}))
    assert pulled["source_envelope"]["schema_note"].startswith("结果端判据")
    # 重启复活:信封随快照持久。
    reborn = ws.WatchRegistry()
    ws.registry = reborn
    wt.registry = reborn
    state2 = _state(owner_home, opened["watch_id"])
    assert state2.source_envelope.get("api") == "pay_stream"


def test_remote_lease_prevents_double_harvest(owner_home, monkeypatch):
    source = _FakeSource()
    source.feed(30)
    tool = _tool(owner_home, source)
    opened = _open(tool, background_harvest=0)  # 不起本地线程,只造状态
    state = _state(owner_home, opened["watch_id"])
    # 伪造"别的进程"的新鲜租约。
    monkeypatch.setattr(hv, "_lease_owner", lambda: "pid:me")
    lease_path = ws.state_dir(owner_home) / f"{state.watch_id}.harvester.json"
    lease_path.write_text(
        json.dumps({"owner": "pid:other", "watch_id": state.watch_id, "heartbeat_at": time.time()}),
        encoding="utf-8",
    )
    ensured = hv.ensure_harvester(state, source.handle)
    assert ensured == {"mode": "remote", "lease": json.loads(lease_path.read_text(encoding="utf-8"))}
    assert hv.harvesters.get_live(state.watch_id) is None, "远端在收:本进程不得再起线程"
    # 租约过期 → 接管。
    lease_path.write_text(
        json.dumps({"owner": "pid:other", "watch_id": state.watch_id, "heartbeat_at": time.time() - 60}),
        encoding="utf-8",
    )
    ensured = hv.ensure_harvester(state, source.handle)
    assert ensured == {"mode": "local"}
    assert _wait_until(lambda: hv.harvesters.get_live(state.watch_id) is not None)


def test_spool_pull_carries_frequent_hit_alert(owner_home):
    """spool 路契约:高频命中类调查告警(带示例)随批落 spool,消费批合并渲染进 pull
    payload(与 inline 同契约)——后台收割期间命中照抬有界、告警不丢、零按频丢弃。"""
    source = _FakeSource()
    tool = _tool(owner_home, source)
    opened = _open(tool)
    state = _state(owner_home, opened["watch_id"])
    # 盲配反(源还没喂过,冷窗口放行),再灌 ok≈90% 的流让收割者自己喝。
    configured = tool.execute(
        {
            "action": "configure",
            "watch_id": opened["watch_id"],
            "spec": {"result_field": "status", "target_values": ["ok"]},
        }
    )
    assert configured.ok
    source.feed(300, make=lambda s: {"seq": s, "status": "fail" if s % 10 == 0 else "ok"})
    assert _wait_until(lambda: state.cursor >= 300 and state.spool_seq >= 1)

    pulled = _payload(tool.execute({"action": "pull", "watch_id": opened["watch_id"], "max_wait_seconds": 5}))

    alerts = pulled.get("frequent_hit_investigation") or []
    assert alerts and alerts[0]["path"] == "status" and alerts[0]["value"] == "ok"
    assert alerts[0]["exemplar_event"], "示例事件须随告警过 spool JSON 往返"
    assert "内容" in pulled.get("frequent_hit_note", "")
    assert pulled["engine_totals"]["spec_frequent_hits"] > 100
    lifted = [row for row in pulled["candidates"] if row["triage"]["reason"] == "spec_target_value"]
    assert lifted, "spool 路命中同样照抬,不许按频率丢弃"
