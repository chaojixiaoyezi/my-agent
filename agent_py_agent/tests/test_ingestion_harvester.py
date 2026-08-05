"""后台连续摄取(harvester)钉子(§8-1 加难实锤:900 条/秒 + 源端滚动缓冲 ~6 分钟淘汰,
模型研判期间无人拉流 → 淘汰即永久丢,命中全集中在早期序号)。

钉死六层契约:
1. 解耦:模型不 pull 的时间里,收割线程持续拉流喂引擎,候选批落 spool——源端
   缓冲淘汰不再造成丢失(游标始终追平流末尾)。
2. 消费契约:pull 从 spool 取候选批,载荷字段与 inline 模式同构(candidates/
   suppressed_groups/coverage/watch),模型判读无感;积压如实入账(spool_backlog_*)。
3. 判定零污染:候选行与 inline 同源渲染(candidate_rows),本层不加任何定性。
4. 世代轮转:积压清零且文件超限时换代,读者按 generation 对齐,不重不漏。
5. 生命周期:close 停线程;窗口走完立即停止拉新数据;源报错不放弃(退避续拉,
   缺口在 gap 账目如实体现)。
6. 跨进程:租约防双收;重启后从盘上游标续,读游标 sidecar 各自独立。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
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

    def handle(self, request) -> tuple[bool, object, str]:
        if self.fail:
            return False, "boom", "NETWORK_REQUEST_FAILED"
        from urllib.parse import parse_qs, urlsplit

        url = request.url
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
    params = {"action": "open", "url": "http://127.0.0.1:9/pull?since=<next>&limit=<limit>", "watch_window_seconds": 1200}
    params.update(extra)
    return _payload(tool.execute(params))


def _open_with_host_tuning(
    tool,
    monkeypatch,
    *,
    open_params: dict[str, object] | None = None,
    **overrides,
) -> dict:
    """Inject internal ingestion tuning without reviving removed tool fields."""
    original_new_state = wt.new_state

    def _new_state(*args, **kwargs):
        state = original_new_state(*args, **kwargs)
        tuning = replace(state.tuning, **overrides)
        state.tuning = tuning
        state.engine.tuning = tuning
        return state

    with monkeypatch.context() as scoped:
        scoped.setattr(wt, "new_state", _new_state)
        return _open(tool, **(open_params or {}))


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
    # 模型没有调 pull——收割者自己把流喝干、候选落 spool(201<背压上限 384,不触发背压)。
    assert _wait_until(lambda: state.cursor >= 201 and state.spool_seq >= 1)
    assert hv.spool_path(state).exists()

    # content_mode 记录=一批可精读量(48),逐 pull 消费(反 rubber-stamp 配速):
    # 循环 pull 直到末尾那条 flag 事件被取出——"收割者已抬到、pull 能拿到"的契约不变,
    # 只是不再一 pull 把整条流(201 条)整车倒给模型。
    seen_flag = False
    last_pull: dict = {}
    for _ in range(12):
        last_pull = _payload(tool.execute({"action": "pull", "watch_id": opened["watch_id"], "max_wait_seconds": 5}))
        if any(c["event"].get("flag") is True for c in last_pull["candidates"]):
            seen_flag = True
            break
    assert seen_flag, "末尾 flag 事件必须能被逐批 pull 取到"
    assert last_pull["coverage"]["cursor"] >= 201
    assert last_pull["harvester"]["running"] is True
    assert "spool_backlog_candidates" in last_pull["coverage"]


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


def test_harvester_publishes_cursor_only_after_batch_commit(owner_home, monkeypatch):
    """cursor 只能表示 engine/spool/audit 已完成，不能提前暴露网络读取水位。"""
    source = _FakeSource()
    tool = _tool(owner_home, source)
    opened = _open(tool)
    state = _state(owner_home, opened["watch_id"])
    assert _wait_until(lambda: state.last_reached_end)

    entered = threading.Event()
    release = threading.Event()
    original_process = state.engine.process

    def blocking_process(*args, **kwargs):
        entered.set()
        assert release.wait(5.0), "test must release the blocked engine"
        return original_process(*args, **kwargs)

    monkeypatch.setattr(state.engine, "process", blocking_process)
    source.feed(10)
    assert entered.wait(5.0), "harvester should start processing the new batch"
    try:
        assert state.cursor == 0
        assert state.engine.totals["events_seen"] == 0
    finally:
        release.set()
    assert _wait_until(
        lambda: state.cursor >= 10 and state.engine.totals["events_seen"] >= 10,
        timeout=6.0,
    )


def test_pull_consumes_backlog_in_order_and_accounts_rest(owner_home):
    source = _FakeSource()
    tool = _tool(owner_home, source)
    # 关直通聚焦分诊消费顺序:直通把小批全量抬升,跨拍与车道排序叠加后本测试的
    # "全体候选流序单调"构造不再成立(直通消费顺序专测在 test_ingestion_full_read.py)。
    opened = _open(tool, max_candidates_per_pull=2, full_read_per_pull=0)
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
    source = _FakeSource()
    source.feed(10)
    tool = _tool(owner_home, source)
    opened = _open(tool, watch_window_seconds=30)
    state = _state(owner_home, opened["watch_id"])
    assert _wait_until(lambda: hv.harvesters.get_live(state.watch_id) is not None)
    handle = hv.harvesters.get_live(state.watch_id)
    state.opened_at = time.time() - 120  # 把窗口拨到已走完
    assert _wait_until(lambda: not handle.thread.is_alive(), timeout=10.0)


def test_window_deadline_commits_one_final_cursor_catch_up(owner_home):
    source = _FakeSource()
    source.feed(10)
    tool = _tool(owner_home, source)
    opened = _open(tool, watch_window_seconds=30)
    state = _state(owner_home, opened["watch_id"])
    handle = hv.harvesters.get_live(state.watch_id)
    assert handle is not None
    handle.stop_event.set()
    assert _wait_until(lambda: not handle.thread.is_alive(), timeout=10.0)

    cursor_before = state.cursor
    source.feed(20)
    state.opened_at = time.time() - 30

    assert hv._harvest_once(state, source.handle) == "stop"
    assert state.cursor == cursor_before + 20
    assert state.window_finalized_at > 0

    source.feed(5)
    assert hv._harvest_once(state, source.handle) == "stop"
    assert state.cursor == cursor_before + 20

    restored = ws.load_state(owner_home, state.watch_id)
    assert restored is not None
    assert restored.window_finalized_at == state.window_finalized_at


def test_window_deadline_forces_final_poll_even_before_normal_cadence(owner_home):
    source = _FakeSource()
    source.feed(1)
    state = ws.new_state(
        owner_home,
        "http://127.0.0.1:9/poll",
        {"watch_window_seconds": 30, "poll_query_seconds": 300},
    )
    state.source_mode = "poll"
    state.source_envelope = {
        "mode": "poll",
        "record_boundary": "whole_response",
        "request": {"method": "GET"},
        "valid": True,
    }
    ws.persist_state(state)

    assert hv._harvest_once(state, source.handle) == "ok"
    first_poll_at = state.last_poll_at
    first_cursor = state.cursor
    state.opened_at = time.time() - 30

    assert hv._harvest_once(state, source.handle) == "stop"
    assert state.window_finalized_at >= first_poll_at
    assert state.last_poll_at > first_poll_at
    assert state.cursor == first_cursor + 1


def test_harvester_wait_is_capped_by_pending_window_deadline(owner_home):
    state = ws.new_state(
        owner_home,
        "http://127.0.0.1:9/pull?since=<next>&limit=<limit>",
        {"watch_window_seconds": 30, "poll_interval_seconds": 60},
    )
    state.opened_at = time.time() - 29.5
    wait = hv._next_harvest_wait(state, backoff=10)
    assert 0 <= wait <= 0.6

    state.window_finalized_at = time.time()
    assert hv._next_harvest_wait(state, backoff=10) == (
        state.tuning.poll_interval_seconds + 10
    )


def test_explicit_new_window_clears_previous_boundary_commit(owner_home, monkeypatch):
    source = _FakeSource()
    tool = _tool(owner_home, source)
    opened = _open_with_host_tuning(
        tool,
        monkeypatch,
        open_params={"watch_window_seconds": 30},
        background_harvest=0,
    )
    state = _state(owner_home, opened["watch_id"])
    state.opened_at = time.time() - 60
    state.window_finalized_at = time.time() - 30
    ws.persist_state(state)

    reopened = _open_with_host_tuning(
        tool,
        monkeypatch,
        open_params={"watch_window_seconds": 45},
        background_harvest=0,
    )
    restored = ws.load_state(owner_home, reopened["watch_id"])

    assert restored is not None
    assert restored.window_finalized_at == 0
    assert time.time() - restored.opened_at < 2


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


def test_cold_start_backlog_chunking_surfaces_late_rare_events(owner_home, monkeypatch):
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
    # 关背压聚焦"分片喂→中段/尾段稀有不落 overflow"这一条契约(背压另有专测):否则收割者
    # 抬到上限就停等消费,5000 存量不会一次喂完,与本测试的"整批喂完"前提相互干扰。
    opened = _open_with_host_tuning(
        tool,
        monkeypatch,
        spool_backpressure_factor=0,
    )
    state = _state(owner_home, opened["watch_id"])
    # 等「全部积压喂完引擎」(events_seen):drain 一到位 cursor 就是 5000、spool>=2 中途即满足,
    # 机器忙时片循环还没消化到尾段就读 spool——基线即有的竞态挂法,这里等真正的完成信号。
    assert _wait_until(lambda: state.engine.totals.get("events_seen", 0) >= 5000, timeout=8.0)
    assert _wait_until(lambda: state.totals.get("spool_candidates", 0) >= 2)
    spooled = hv.spool_path(state).read_text(encoding="utf-8")
    assert '"rare-mid"' in spooled, "积压中段的稀有事件必须进候选批"
    assert '"rare-tail"' in spooled, "积压尾段的稀有事件必须进候选批"


def test_source_envelope_probed_at_open_and_surfaces_in_pull(owner_home):
    """源信封只保存采集结构，业务标量不进入持久数据源配置。"""

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
    assert opened["source_envelope"]["mode"] == "cursor"
    assert opened["source_envelope"]["record_list_key"] == "items"
    assert opened["source_envelope"]["cursor_field"] == "next_cursor"
    assert "schema_note" not in opened["source_envelope"]
    assert "api" not in opened["source_envelope"]
    pulled = _payload(tool.execute({"action": "pull", "watch_id": opened["watch_id"], "max_wait_seconds": 3}))
    assert pulled["source_envelope"]["record_boundary"] == "array_item"
    # 重启复活:信封随快照持久。
    reborn = ws.WatchRegistry()
    ws.registry = reborn
    wt.registry = reborn
    state2 = _state(owner_home, opened["watch_id"])
    assert state2.source_envelope.get("record_list_key") == "items"


def test_remote_lease_prevents_double_harvest(owner_home, monkeypatch):
    source = _FakeSource()
    source.feed(30)
    tool = _tool(owner_home, source)
    opened = _open_with_host_tuning(
        tool,
        monkeypatch,
        background_harvest=0,
    )  # 不起本地线程,只造状态
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


def test_atomic_claim_closes_thread_start_registry_window(owner_home, monkeypatch):
    """A second caller cannot start another reader before the first handle is registered."""
    source = _FakeSource()
    tool = _tool(owner_home, source)
    opened = _open_with_host_tuning(tool, monkeypatch, background_harvest=0)
    state = _state(owner_home, opened["watch_id"])
    put_entered = threading.Event()
    release_put = threading.Event()
    worker_started = threading.Event()
    worker_ids: list[str] = []

    class _BlockingRegistry(hv._HarvesterRegistry):
        def put(self, handle):
            put_entered.set()
            assert release_put.wait(5.0)
            super().put(handle)

    registry = _BlockingRegistry()
    monkeypatch.setattr(hv, "harvesters", registry)

    def parked_loop(
        _state,
        _fetch_json,
        stop_event,
        lease_id,
        _on_records_ready=None,
        _on_window_finalized=None,
    ):
        worker_ids.append(lease_id)
        worker_started.set()
        stop_event.wait(5.0)
        hv._clear_lease(_state, lease_id)

    monkeypatch.setattr(hv, "_harvest_loop", parked_loop)
    first_result: list[dict | None] = []
    first = threading.Thread(
        target=lambda: first_result.append(hv.ensure_harvester(state, source.handle)),
        daemon=True,
    )
    first.start()
    assert worker_started.wait(5.0)
    assert put_entered.wait(5.0)

    second = hv.ensure_harvester(state, source.handle)
    assert second is not None and second["mode"] == "remote"
    assert len(worker_ids) == 1

    release_put.set()
    first.join(timeout=5.0)
    assert first_result == [{"mode": "local"}]
    registry.stop(state.watch_id)


def test_harvester_notifies_once_per_nonempty_backlog_transition(owner_home, monkeypatch):
    source = _FakeSource()
    source.feed(3)
    tool = _tool(owner_home, source)
    opened = _open_with_host_tuning(tool, monkeypatch, background_harvest=0)
    state = _state(owner_home, opened["watch_id"])
    notifications: list[int] = []

    ensured = hv.ensure_harvester(
        state,
        source.handle,
        on_records_ready=lambda ready_state: (
            notifications.append(
                int(hv.spool_backlog_facts(ready_state)["candidates_unjudged"])
            )
            or True
        ),
    )

    assert ensured == {"mode": "local"}
    assert _wait_until(lambda: bool(notifications))
    time.sleep(0.1)
    assert len(notifications) == 1
    assert notifications[0] > 0
    hv.stop_harvester(state.watch_id)


def test_ready_notification_rearms_when_ack_catches_prior_watermark(owner_home, monkeypatch):
    """A new tranche wakes even if collector never sampled the brief empty instant."""
    source = _FakeSource()
    tool = _tool(owner_home, source)
    opened = _open_with_host_tuning(tool, monkeypatch, background_harvest=0)
    state = _state(owner_home, opened["watch_id"])
    notifications: list[int] = []

    state.totals["spool_candidates"] = 5
    notified_through = hv._records_ready_notification_watermark(
        state,
        lambda ready_state: (
            notifications.append(
                int(hv.spool_backlog_facts(ready_state)["candidates_unjudged"])
            )
            or True
        ),
        notified_through=0,
    )
    assert notified_through == 5
    assert notifications == [5]

    # New rows arrive while the prior notified tranche still has work.  Do not
    # create one wake per source record.
    state.totals["spool_candidates"] = 7
    assert hv._write_spool_cursor(
        state,
        {"candidates_consumed": 4, "candidates_acked": 4},
    )
    notified_through = hv._records_ready_notification_watermark(
        state,
        lambda _state: notifications.append(-1) or True,
        notified_through=notified_through,
    )
    assert notified_through == 5
    assert notifications == [5]

    # The consumer acknowledges exactly the prior watermark.  The queue never
    # appeared empty to this helper, but rows 6-7 are now a distinct tranche
    # and must immediately wake the worker.
    assert hv._write_spool_cursor(
        state,
        {"candidates_consumed": 5, "candidates_acked": 5},
    )
    notified_through = hv._records_ready_notification_watermark(
        state,
        lambda ready_state: (
            notifications.append(
                int(hv.spool_backlog_facts(ready_state)["candidates_unjudged"])
            )
            or True
        ),
        notified_through=notified_through,
    )
    assert notified_through == 7
    assert notifications == [5, 2]


def test_harvester_notifies_once_after_final_boundary_commit(owner_home, monkeypatch):
    source = _FakeSource()
    tool = _tool(owner_home, source)
    opened = _open_with_host_tuning(
        tool,
        monkeypatch,
        open_params={"watch_window_seconds": 30},
        background_harvest=0,
    )
    state = _state(owner_home, opened["watch_id"])
    state.opened_at = time.time() - 60
    ws.persist_state(state)
    notifications: list[float] = []

    ensured = hv.ensure_harvester(
        state,
        source.handle,
        on_window_finalized=lambda finalized_state: (
            notifications.append(float(finalized_state.window_finalized_at)) or True
        ),
    )

    assert ensured == {"mode": "local"}
    assert _wait_until(lambda: bool(notifications))
    time.sleep(0.1)
    restored = ws.load_state(owner_home, state.watch_id)
    assert restored is not None
    assert len(notifications) == 1
    assert notifications[0] > 0
    assert restored.window_finalized_at == notifications[0]


def test_late_harvester_cannot_renew_or_clear_takeover_lease(owner_home, monkeypatch):
    source = _FakeSource()
    tool = _tool(owner_home, source)
    opened = _open_with_host_tuning(tool, monkeypatch, background_harvest=0)
    state = _state(owner_home, opened["watch_id"])
    claimed, old_lease = hv._claim_harvester_lease(state)
    assert claimed
    lease_path = ws.state_dir(owner_home) / f"{state.watch_id}.harvester.json"
    stale = {**old_lease, "heartbeat_at": time.time() - 60}
    lease_path.write_text(json.dumps(stale), encoding="utf-8")

    claimed, new_lease = hv._claim_harvester_lease(state)
    assert claimed
    assert new_lease["lease_id"] != old_lease["lease_id"]
    assert not hv._write_lease(state, str(old_lease["lease_id"]))
    hv._clear_lease(state, str(old_lease["lease_id"]))
    current = json.loads(lease_path.read_text(encoding="utf-8"))
    assert current["lease_id"] == new_lease["lease_id"]
    assert hv._write_lease(state, str(new_lease["lease_id"]))
    hv._clear_lease(state, str(new_lease["lease_id"]))


def test_fresh_unreadable_lease_fails_closed(owner_home, monkeypatch):
    source = _FakeSource()
    tool = _tool(owner_home, source)
    opened = _open_with_host_tuning(tool, monkeypatch, background_harvest=0)
    state = _state(owner_home, opened["watch_id"])
    lease_path = ws.state_dir(owner_home) / f"{state.watch_id}.harvester.json"
    lease_path.write_text("{not-json", encoding="utf-8")

    ensured = hv.ensure_harvester(state, source.handle)

    assert ensured is not None and ensured["mode"] == "remote"
    assert ensured["lease"]["error_code"] == "HARVESTER_LEASE_UNREADABLE"
    assert hv.harvesters.get_live(state.watch_id) is None


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
    note = pulled.get("frequent_hit_note", "")
    assert "频率" in note and "不决定业务结论" in note
    assert pulled["engine_totals"]["spec_frequent_hits"] > 100
    lifted = [row for row in pulled["candidates"] if row["triage"]["reason"] == "spec_target_value"]
    assert lifted, "spool 路命中同样照抬,不许按频率丢弃"
