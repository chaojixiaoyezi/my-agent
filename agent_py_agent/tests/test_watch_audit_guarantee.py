"""/audit 保证档(逐条保证判读)的机制层契约。

钉死的契约(每条对应一个上一轮真机崩掉的口子):
1. ack-on-judge:游标/ACK 只在"逐条结论交齐(submit_verdicts)"后推进——空转判读工
   反复 pull 抽不干游标(上一轮 4% 召回的直接根因:pull 本身当 ack,空壳工没判就签收);
2. 交付即立欠账:候选行带 ack_id、在途批带 pending_acks;欠账未清,同人换人一律重投同批;
3. 引擎零丢弃:normal 内容规则命中在保证档也逐条入队(规则只记账不减负),无压组无溢出;
4. 覆盖回执:入队/已判/待判/丢弃 随时可查,丢弃恒 0(>0 = bug 亮红);
5. 判完归档:轮转切走的已判记录进 archive 留痕,不销毁;
6. 抬取不按判读积压背压(只按磁盘水位):判读慢=队列涨,不许把流留在会淘汰的源端;
7. 非保证档零回归:ack-on-next-pull 旧路逐字节不变(既有 takeover 测试盯着)。

铁律:全部结构化信号(计数/状态/令牌),不判内容——候选真假永远归模型。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent.common.audit_activation import AUDIT_ATTR
from agent.ingestion import harvester as hv
from agent.ingestion import watch_state as ws
from agent.ingestion import watch_tool as wt
from agent.ingestion.watch_state import new_state, persist_state
from agent.ingestion.watch_tool import WatchStreamTool


class _FakeSource:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def feed(self, count: int, make=None) -> None:
        base = len(self.events)
        for index in range(count):
            seq = base + index
            self.events.append(make(seq) if make else {"seq": seq, "kind": "beat", "note": f"n{seq}"})

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
    fresh = ws.WatchRegistry()
    monkeypatch.setattr(ws, "registry", fresh)
    monkeypatch.setattr(wt, "registry", fresh)
    monkeypatch.setattr(hv, "harvesters", hv._HarvesterRegistry())
    return tmp_path / "owner"


_TOOL_URL = "http://127.0.0.1:9/pull"


def _tool(
    owner_home: Path,
    source: _FakeSource,
    user_prompt: str = "",
    task_attributes: dict | None = None,
) -> WatchStreamTool:
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u-test"),
        _current_user_prompt=user_prompt,
        _current_run_params=SimpleNamespace(task_attributes=task_attributes),
    )
    tool = WatchStreamTool(agent)
    tool.allow_private_resolution = True
    tool.__dict__["_fetch_json"] = source.handle
    return tool


def _audit_state(owner_home: Path, source: _FakeSource, count: int, chunk: int = 4):
    """保证档状态:喂 count 条进真收割管线(引擎全量直通,每条一候选落 spool)。"""
    state = new_state(owner_home, "http://src.example/pull", {"full_read_per_pull": chunk, "background_harvest": 0})
    state.audit_guarantee = True
    persist_state(state)
    source.feed(count)
    assert hv._harvest_cycle(state, source.handle)
    return state


def _payload(result) -> dict:
    assert result.ok, result.output
    return json.loads(result.output)


def _ack_ids(records: list[dict]) -> list[str]:
    return [row["ack_id"] for record in records for row in (record.get("candidates") or [])]


# ── 1/2. ack-on-judge:交付立欠账,空 pull 抽不干,结论交齐才签收发新批 ──


def test_delivery_carries_ack_ids_and_pending_acks(owner_home):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 6, chunk=3)
    records, _bl = hv.read_spool_records(state, max_candidates=3, consumer="w0")
    ids = _ack_ids(records)
    assert ids and all(":" in aid for aid in ids)
    cursor = hv.read_spool_cursor(state)
    assert cursor["inflight"]["pending_acks"] == ids  # 欠账=本批全体令牌


def test_empty_pull_cannot_drain_cursor_without_verdicts(owner_home):
    """上一轮崩的根因回归:空壳判读工反复 pull,游标/ack 纹丝不动,拿到的永远是同一批+欠账。"""
    source = _FakeSource()
    state = _audit_state(owner_home, source, 6, chunk=3)
    first, _ = hv.read_spool_records(state, max_candidates=3, consumer="hollow")
    first_ids = _ack_ids(first)
    for _ in range(5):  # 空壳工五连 pull(判都没判)
        again, backlog = hv.read_spool_records(state, max_candidates=3, consumer="hollow")
        assert _ack_ids(again) == first_ids  # 重投同一批
        assert backlog.get("pending_verdicts") == len(first_ids)  # 欠账清单如实
    assert hv.acked_candidates(hv.read_spool_cursor(state)) == 0  # 一条都没被"签收"


def test_nonaudit_next_pull_still_acks_baseline(owner_home):
    """对照(零回归口径):非保证档同人第二次 pull 即确认上一批——旧路语义不变。"""
    source = _FakeSource()
    state = new_state(owner_home, "http://src.example/pull", {"full_read_per_pull": 3, "background_harvest": 0})
    persist_state(state)
    source.feed(6)
    assert hv._harvest_cycle(state, source.handle)
    hv.read_spool_records(state, max_candidates=3, consumer="w0")
    hv.read_spool_records(state, max_candidates=3, consumer="w0")
    assert hv.acked_candidates(hv.read_spool_cursor(state)) > 0


def test_verdicts_settle_batch_and_release_next(owner_home):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 8, chunk=2)
    first, _ = hv.read_spool_records(state, max_candidates=4, consumer="w0")
    ids = _ack_ids(first)
    assert len(ids) == 4
    # 部分交:acked 逐条推进、在途保留、欠账缩水
    part = hv.submit_verdicts(state, consumer="w0", verdicts=[
        {"ack_id": ids[0], "verdict": "hit", "note": "结果端明确生效"},
        {"ack_id": ids[1], "verdict": "clear"},
    ])
    assert part["ok"] and part["acked_now"] == 2 and part["pending_remaining"] == 2
    cursor = hv.read_spool_cursor(state)
    assert hv.acked_candidates(cursor) == 2
    assert set(cursor["inflight"]["pending_acks"]) == set(ids[2:])
    # 欠账未清:pull 仍是同一批(不发新批)
    again, backlog = hv.read_spool_records(state, max_candidates=4, consumer="w0")
    assert _ack_ids(again) == ids and backlog.get("pending_verdicts") == 2
    # 交齐:在途摘除,下一次 pull 发新批
    rest = hv.submit_verdicts(state, consumer="w0", verdicts=[
        {"ack_id": ids[2], "verdict": "unsure", "note": "两端对不齐,存疑"},
        {"ack_id": ids[3], "verdict": "clear"},
    ])
    assert rest["ok"] and rest["pending_remaining"] == 0
    assert "inflight" not in hv.read_spool_cursor(state)
    fresh, _ = hv.read_spool_records(state, max_candidates=4, consumer="w0")
    fresh_ids = _ack_ids(fresh)
    assert fresh_ids and not (set(fresh_ids) & set(ids))
    assert hv.acked_candidates(hv.read_spool_cursor(state)) == 4


def test_unknown_ack_ids_rejected_not_credited(owner_home):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 4, chunk=2)
    records, _ = hv.read_spool_records(state, max_candidates=2, consumer="w0")
    ids = _ack_ids(records)
    result = hv.submit_verdicts(state, consumer="w0", verdicts=[
        {"ack_id": "999:7", "verdict": "clear"},  # 手编令牌
        {"ack_id": ids[0], "verdict": "hit"},
        {"ack_id": ids[1], "verdict": "bogus-kind"},  # 非法结论
    ])
    assert result["acked_now"] == 1
    assert result["unknown_ack_ids"] == ["999:7"]
    assert result["malformed"] == 1
    assert hv.acked_candidates(hv.read_spool_cursor(state)) == 1


def test_takeover_redelivers_with_pending_acks(owner_home):
    """换人重投带欠账:继任者拿到同一批+同一份欠账,交结论照常销账(问责跨换人成立)。"""
    source = _FakeSource()
    state = _audit_state(owner_home, source, 4, chunk=2)
    first, _ = hv.read_spool_records(state, max_candidates=2, consumer="died")
    ids = _ack_ids(first)
    taken, backlog = hv.read_spool_records(state, max_candidates=2, consumer="successor")
    assert _ack_ids(taken) == ids
    assert backlog.get("pending_verdicts") == len(ids)
    done = hv.submit_verdicts(state, consumer="successor", verdicts=[
        {"ack_id": aid, "verdict": "clear"} for aid in ids
    ])
    assert done["ok"] and done["pending_remaining"] == 0


# ── 3. 引擎零丢弃:normal 规则命中也逐条入队;无压组无溢出 ──


def test_guarantee_engine_lifts_normal_rule_hits(owner_home):
    from agent.ingestion.config import IngestTuning
    from agent.ingestion.engine import StreamDigestEngine
    from agent.ingestion.source_spec import parse_source_spec

    tuning = IngestTuning(full_read_per_pull=48)
    plain = StreamDigestEngine(tuning)
    guarded = StreamDigestEngine(tuning)
    spec = parse_source_spec({"result_field": "state", "normal_values": ["ok"]})
    plain.apply_spec(spec)
    guarded.apply_spec(spec)
    # 同签名事件(无逐条变化字段):稀有兜底只抬前几条,规则减负路径才走得到
    events = [(i, {"state": "ok", "kind": "beat"}) for i in range(12)]
    baseline = plain.process(list(events), 1000.0)
    receipt = guarded.process(list(events), 1000.0, guarantee=True)
    # 非保证档:normal 规则命中走压组减负(候选少于全量);保证档:每条都成候选
    assert len(receipt.candidates) == 12
    assert receipt.suppressed_total == 0 and not receipt.overflow
    assert len(baseline.candidates) < 12  # 对照:旧路确实在减负(规则本身有效)
    # 规则命中账两边都在记(保证档只是不拿它筛,记账不打折)
    assert guarded.totals["spec_normal_rule_hits"] > 0


def test_guarantee_forces_full_read_even_with_zero_escape_valve(owner_home):
    """full_read_per_pull=0 是非保证档的逃生阀(回落有损分诊);保证档没有这条路。"""
    from agent.ingestion.config import IngestTuning
    from agent.ingestion.engine import StreamDigestEngine

    engine = StreamDigestEngine(IngestTuning(full_read_per_pull=0))
    events = [(i, {"seq": i, "kind": "beat", "note": f"n{i}"}) for i in range(20)]
    digest = engine.process(events, 1000.0, guarantee=True)
    assert len(digest.candidates) == 20
    assert digest.suppressed_total == 0 and not digest.overflow


# ── 4. 覆盖回执:入队/已判/待判/丢弃 对账;丢弃恒 0 ──


def test_audit_receipt_reconciles(owner_home):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 6, chunk=3)
    receipt = hv.audit_receipt_facts(state)
    assert receipt["enqueued"] == 6 and receipt["judged"] == 0
    assert receipt["pending"] == 6 and receipt["dropped"] == 0
    records, _ = hv.read_spool_records(state, max_candidates=3, consumer="w0")
    ids = _ack_ids(records)
    hv.submit_verdicts(state, consumer="w0", verdicts=[
        {"ack_id": ids[0], "verdict": "hit"},
        {"ack_id": ids[1], "verdict": "clear"},
        {"ack_id": ids[2], "verdict": "unsure"},
    ])
    receipt = hv.audit_receipt_facts(state)
    assert (receipt["judged"], receipt["pending"], receipt["dropped"]) == (3, 3, 0)
    assert receipt["verdicts"] == {"hit": 1, "clear": 1, "unsure": 1}


def test_verdict_ledger_appended(owner_home):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 2, chunk=2)
    records, _ = hv.read_spool_records(state, max_candidates=2, consumer="w0")
    ids = _ack_ids(records)
    hv.submit_verdicts(state, consumer="w0", verdicts=[{"ack_id": aid, "verdict": "clear"} for aid in ids])
    ledger = ws.state_dir(owner_home) / f"{state.watch_id}.verdicts.ndjson"
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [row["ack_id"] for row in rows] == ids
    assert all(row["verdict"] == "clear" and row["by"] == "w0" for row in rows)


# ── 5. 判完归档:轮转切走的行进 archive 留痕 ──


def test_rotation_archives_judged_records_in_guarantee(owner_home, monkeypatch):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 6, chunk=2)
    # 判完全部(交结论签收),归档窗口出现
    for _ in range(6):
        records, _ = hv.read_spool_records(state, max_candidates=2, consumer="w0")
        if not records:
            break
        hv.submit_verdicts(state, consumer="w0", verdicts=[
            {"ack_id": aid, "verdict": "clear"} for aid in _ack_ids(records)
        ])
    monkeypatch.setattr(hv, "_SPOOL_ROTATE_BYTES", 1)
    source.feed(2)
    assert hv._harvest_cycle(state, source.handle)
    assert state.spool_generation >= 1
    archive = ws.state_dir(owner_home) / f"{state.watch_id}.archive.ndjson"
    assert archive.exists()
    archived = [json.loads(line) for line in archive.read_text().splitlines()]
    assert archived and all(row.get("candidates") for row in archived)  # 已判记录原样留痕


# ── 6. 抬取背压:保证档不按判读积压停抬,只按磁盘水位 ──


def test_guarantee_ignores_judge_backlog_backpressure(owner_home):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 200, chunk=4)  # 未判积压远超 8×quota
    assert hv.spool_unread(state) > 0
    assert not hv._backpressured(state)  # 判读积压不背压(队列涨,不留源端)


def test_guarantee_disk_watermark_halts_harvest(owner_home):
    source = _FakeSource()
    state = new_state(owner_home, "http://src.example/pull", {
        "full_read_per_pull": 4, "background_harvest": 0, "guarantee_spool_max_mb": 1,
    })
    state.audit_guarantee = True
    persist_state(state)
    source.feed(4)
    assert hv._harvest_cycle(state, source.handle)
    # 把 spool 撑过 1MB 水位 → 下一拍整拍停抬,单独记账
    hv.spool_path(state).open("a", encoding="utf-8").write("x" * (1024 * 1024 + 1) + "\n")
    assert hv._backpressured(state)
    before = int(state.totals.get("disk_backpressure_skips", 0))
    assert hv._locked_harvest_step(state, source.handle) == "ok"
    assert int(state.totals["disk_backpressure_skips"]) == before + 1


# ── 7. 工具面:结构化 /audit 置位、棘轮、verdict 动作、inline 不回落 ──


def test_open_structured_audit_sets_guarantee_and_persists(owner_home):
    source = _FakeSource()
    tool = _tool(owner_home, source, task_attributes={AUDIT_ATTR: True})
    opened = _payload(tool.execute({"action": "open", "url": _TOOL_URL}))
    assert opened["audit_guarantee"] is True and opened["audit_note"]
    lane = ws.load_state(owner_home, opened["watch_id"])
    assert lane is not None and lane.audit_guarantee is True


def test_open_uses_stamped_task_attribute_not_prompt_text(owner_home):
    source = _FakeSource()
    tool = _tool(
        owner_home,
        source,
        user_prompt="/audit 盯这 5 个 API 几个月逐条研判",
        task_attributes={AUDIT_ATTR: True},
    )
    opened = _payload(tool.execute({"action": "open", "url": _TOOL_URL}))
    assert opened["audit_guarantee"] is True


def test_open_without_audit_stays_plain_and_ratchets(owner_home):
    source = _FakeSource()
    tool = _tool(owner_home, source)
    opened = _payload(tool.execute({"action": "open", "url": _TOOL_URL}))
    assert "audit_guarantee" not in opened  # 普通档不误开
    # 升级后,后续 open 缺参不降级(棘轮)
    tool.agent._current_run_params.task_attributes = {AUDIT_ATTR: True}
    _payload(tool.execute({"action": "open", "url": _TOOL_URL}))
    reopened = _payload(tool.execute({"action": "open", "url": _TOOL_URL}))
    assert reopened["audit_guarantee"] is True


def test_verdict_action_rejected_on_plain_watch(owner_home):
    source = _FakeSource()
    tool = _tool(owner_home, source)
    opened = _payload(tool.execute({"action": "open", "url": _TOOL_URL}))
    result = tool.execute({"action": "verdict", "watch_id": opened["watch_id"], "verdicts": [{"ack_id": "1:0", "verdict": "clear"}]})
    assert not result.ok


def test_audit_pull_uses_guarantee_contract_and_receipt(owner_home):
    source = _FakeSource()
    tool = _tool(owner_home, source, task_attributes={AUDIT_ATTR: True})
    opened = _payload(tool.execute({"action": "open", "url": _TOOL_URL}))
    source.feed(4)
    pulled = _payload(tool.execute({"action": "pull", "watch_id": opened["watch_id"], "max_wait_seconds": 3}))
    assert "保证档" in pulled["guidance"]
    assert pulled["coverage"]["audit_receipt"]["dropped"] == 0
    assert all(row.get("ack_id") for row in pulled["candidates"])
    # 状态/关闭回执同样带回执;close 时有待判要给违约警告
    status = _payload(tool.execute({"action": "status", "watch_id": opened["watch_id"]}))
    assert status["coverage"]["audit_receipt"]["enqueued"] >= 4
    closed = _payload(tool.execute({"action": "close", "watch_id": opened["watch_id"]}))
    if closed["spool_backlog_candidates_at_close"] > 0:
        assert "保证档违约警告" in closed["discarded_backlog_note"]


def test_contract_inherits_via_shared_state_across_consumers(owner_home, monkeypatch):
    """契约下传的机制=跟数据走(不靠信任 spawn 树):保证档标志随 watch 持久化,任何后来
    的消费者(子代理/孙代理/重启后的新进程)冷加载这路 watch 都拿到保证档契约——且 ack-on
    -judge 在 spool 层强制,新消费者'想跳过'结构上也推不动游标。这里模拟'另一个进程/另一个
    子代理':清进程内 registry 缓存,强制从盘上 load_state。"""
    source = _FakeSource()
    tool = _tool(owner_home, source, task_attributes={AUDIT_ATTR: True})
    opened = _payload(tool.execute({"action": "open", "url": _TOOL_URL}))
    watch_id = opened["watch_id"]
    source.feed(4)
    _payload(tool.execute({"action": "pull", "watch_id": watch_id, "max_wait_seconds": 3}))
    # 换一个"进程":新 registry(冷加载)+ 新消费者身份
    monkeypatch.setattr(ws, "registry", ws.WatchRegistry())
    monkeypatch.setattr(wt, "registry", ws.registry)
    reloaded = ws.load_state(owner_home, watch_id)
    assert reloaded is not None and reloaded.audit_guarantee is True  # 契约从盘上继承
    successor = _tool(owner_home, source)
    successor.agent._current_run_params = SimpleNamespace(run_id="grandchild-run")
    pulled = _payload(successor.execute({"action": "pull", "watch_id": watch_id, "max_wait_seconds": 3}))
    assert "保证档" in pulled["guidance"]  # 新消费者照样受同一契约
    assert pulled["candidates"] and all(row.get("ack_id") for row in pulled["candidates"])
    # 新消费者不交结论就再 pull:游标推不动,拿回同一批+欠账(问责跨换人成立)
    again = _payload(successor.execute({"action": "pull", "watch_id": watch_id, "max_wait_seconds": 3}))
    assert again.get("pending_verdicts", 0) > 0


def test_audit_pull_never_falls_back_to_inline(owner_home, monkeypatch):
    """收割线程起不来且 spool 无积压时:保证档 pull 空批如实返回(spool 路载荷),绝不
    回落 inline drain(那条路事件不入 durable 队列、没有逐条签收对账);非保证档同景
    照旧回落 inline(对照,零回归)。"""
    source = _FakeSource()
    tool = _tool(owner_home, source, task_attributes={AUDIT_ATTR: True})
    # background_harvest=0 调参也不放行 inline:保证档强制 spool 路(顺带钉这个语义)
    monkeypatch.setattr(hv, "ensure_harvester", lambda _state, _fetch: None)
    opened = _payload(tool.execute({"action": "open", "url": _TOOL_URL, "background_harvest": 0}))
    source.feed(3)
    pulled = _payload(tool.execute({"action": "pull", "watch_id": opened["watch_id"], "max_wait_seconds": 0}))
    assert pulled["candidates"] == []  # 空批(慢=延迟)而不是 inline 抬回来的无账候选
    assert "harvester" in pulled  # spool 路载荷(inline 路没有 harvester 块)
    # 对照:非保证档同样条件回落 inline,从源端把事件抬上来(旧路语义不变)
    tool.agent._current_run_params.task_attributes = {}
    plain = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/plain"}))
    hv.stop_harvester(plain["watch_id"])
    pulled_plain = _payload(tool.execute({"action": "pull", "watch_id": plain["watch_id"], "max_wait_seconds": 0}))
    assert pulled_plain["candidates"] and "harvester" not in pulled_plain
