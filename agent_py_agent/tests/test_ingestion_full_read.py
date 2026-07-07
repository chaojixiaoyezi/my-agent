"""正常量直通(full_stream_read)钉子——需求:1000-100000/天的正常量够模型逐条认真读。

契约:
1. 本批量 <= min(full_read_per_pull, 判读余量) → 整批全量抬(常见形状不压组、名额不裁剪),
   模型每条都看得到;账目 escalated_full_read 可核算。
2. 量超预算(洪水/冷启动追赶)→ 该批自动回落降维分诊(压组/名额/抽检全套旧行为)。
3. 判读积压吃光余量 → 直通自动关闭;消费推进读游标 → 余量回升 → 直通自动恢复。
   正常量全读与洪水降级是同一根反压信号上的连续谱,纯计数切换。
4. normal_* 内容过滤规则命中的事件在直通模式仍记账回落(教过的常态=减负照旧生效)。
5. spec/少数派车道的 triage 证据在直通批里保留(reason 不被 full_stream_read 覆盖)。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent.ingestion import harvester as hv
from agent.ingestion import watch_state as ws
from agent.ingestion import watch_tool as wt
from agent.ingestion.config import IngestTuning
from agent.ingestion.engine import StreamDigestEngine
from agent.ingestion.harvester import judge_headroom, judge_quota, read_spool_records
from agent.ingestion.source_spec import parse_source_spec
from agent.ingestion.watch_tool import WatchStreamTool


def _tuning(**overrides) -> IngestTuning:
    base = {"window_seconds": 300, "bucket_seconds": 30, "rare_threshold": 3, "full_read_per_pull": 48}
    base.update(overrides)
    return IngestTuning(**base)


def _commons(start: int, count: int) -> list[tuple[int, dict]]:
    """同形状常见事件(旧行为:窗口计数超过 rare_threshold 后整组被压)。"""
    return [(i, {"kind": "login", "status": "ok", "user": f"u{i % 3}"}) for i in range(start, start + count)]


def test_small_batch_full_passthrough_no_suppression():
    engine = StreamDigestEngine(_tuning())
    # 先热身一批让形状变"常见"(窗口计数 > rare_threshold),再来一小批。
    engine.process(_commons(0, 40), now=1000.0)
    digest = engine.process(_commons(40, 20), now=1010.0)
    assert digest.seen == 20
    assert len(digest.candidates) == 20  # 全量上,一条不压
    assert digest.suppressed_total == 0 and digest.groups_total == 0
    assert digest.overflow == []
    reasons = {c.reason for c in digest.candidates}
    assert "full_stream_read" in reasons
    assert engine.totals["escalated_full_read"] > 0
    # 流序保持(全量直通按 seq 排)。
    positions = [c.seq_hint for c in digest.candidates]
    assert positions == sorted(positions)


def test_large_batch_falls_back_to_triage():
    engine = StreamDigestEngine(_tuning(full_read_per_pull=48))
    digest = engine.process(_commons(0, 300), now=1000.0)
    assert digest.seen == 300
    # 超预算:回落降维分诊——常见形状被压组,绝不 300 条全抬。
    assert len(digest.candidates) < 300
    assert digest.suppressed_total > 0
    assert engine.totals["escalated_full_read"] == 0


def test_headroom_gates_passthrough_and_recovers():
    engine = StreamDigestEngine(_tuning())
    engine.process(_commons(0, 40), now=1000.0)
    # 判读余量只剩 5,本批 10 条 > 5 → 不直通(回落分诊,常见形状被压)。
    throttled = engine.process(_commons(40, 10), now=1010.0, judge_headroom=5)
    assert any(True for _ in [throttled]) and throttled.suppressed_total > 0
    # 余量恢复(消费者判完积压)→ 同样的小批恢复直通。
    recovered = engine.process(_commons(50, 10), now=1020.0, judge_headroom=48)
    assert len(recovered.candidates) == 10
    assert recovered.suppressed_total == 0


def test_zero_cap_disables_passthrough():
    engine = StreamDigestEngine(_tuning(full_read_per_pull=0))
    engine.process(_commons(0, 40), now=1000.0)
    digest = engine.process(_commons(40, 10), now=1010.0)
    assert digest.suppressed_total > 0  # 旧行为:常见形状照压
    assert engine.totals["escalated_full_read"] == 0


def test_normal_rules_still_reduce_load_in_full_read():
    """直通模式下,normal_* 内容过滤规则命中仍记账回落(教过的常态减负不失效);
    spec target 命中保留 spec_target_value 证据(不被 full_stream_read 盖掉)。"""
    tuning = _tuning(value_min_support=8, low_cardinality_limit=8)
    spec = parse_source_spec(
        {"result_field": "status", "normal_values": ["ok"], "target_values": ["breached"]}
    )
    engine = StreamDigestEngine(tuning, spec)
    engine.process(_commons(0, 40), now=1000.0)  # status=ok 全部命中 normal 规则
    batch = _commons(40, 9) + [(49, {"kind": "login", "status": "breached", "user": "u1"})]
    digest = engine.process(batch, now=1010.0)
    by_reason = {}
    for candidate in digest.candidates:
        by_reason.setdefault(candidate.reason, []).append(candidate)
    # target 命中走 spec 车道(证据在),不混进 full_stream_read。
    assert [c.seq_hint for c in by_reason.get("spec_target_value", [])] == [49]
    # normal 命中(status=ok 的 9 条)记账回落:不作为 full_stream_read 抬,常见形状照压。
    assert "full_stream_read" not in by_reason
    assert digest.normal_rule_hits == 9
    assert digest.suppressed_total == 9
    assert engine.totals["spec_normal_rule_hits"] >= 49


def test_full_read_batch_keeps_minority_evidence():
    """直通批里少数派取值的 triage 证据保留(reason=minority_field_value 优先于直通)。"""
    tuning = _tuning(value_min_support=16, value_rare_threshold=3, low_cardinality_limit=8)
    engine = StreamDigestEngine(tuning)
    warm = [(i, {"kind": "login", "flag": False}) for i in range(64)]
    engine.process(warm, now=1000.0)
    batch = [(100 + i, {"kind": "login", "flag": False}) for i in range(9)]
    batch.append((120, {"kind": "login", "flag": True}))  # 少数派取值
    digest = engine.process(batch, now=1010.0)
    assert len(digest.candidates) == 10  # 全量直通
    minority = [c for c in digest.candidates if c.reason == "minority_field_value"]
    assert [c.seq_hint for c in minority] == [120]


# ---------------------------------------------------------------------------
# 端到端(harvester spool 路):直通批整批落 spool、消费口粮一口取走、余量闭环。
# ---------------------------------------------------------------------------


class _FakeSource:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def feed(self, count: int) -> None:
        base = len(self.events)
        for index in range(count):
            self.events.append({"seq": base + index, "kind": "beat", "status": "ok"})

    def handle(self, url: str) -> tuple[bool, object, str]:
        from urllib.parse import parse_qs, urlsplit

        query = parse_qs(urlsplit(url).query)
        since = int(query.get("since", ["0"])[0])
        limit = int(query.get("limit", ["50"])[0])
        picked = [event for event in self.events if event["seq"] >= since][:limit]
        next_cursor = (picked[-1]["seq"] + 1) if picked else since
        # 事件体不含 seq(形状恒定=常见形状):直通标记的观测不被"每条签名都稀有"掩盖。
        items = [{k: v for k, v in event.items() if k != "seq"} for event in picked]
        return True, {"items": items, "next_cursor": next_cursor}, ""


@pytest.fixture()
def owner_home(tmp_path, monkeypatch):
    fresh = ws.WatchRegistry()
    monkeypatch.setattr(ws, "registry", fresh)
    monkeypatch.setattr(wt, "registry", fresh)
    monkeypatch.setattr(hv, "harvesters", hv._HarvesterRegistry())
    return tmp_path / "owner"


def test_spool_roundtrip_full_read_quota_and_recovery(owner_home: Path):
    source = _FakeSource()
    state = ws.new_state(owner_home, "http://src.example/pull", {"full_read_per_pull": 16, "background_harvest": 0})
    ws.persist_state(state)
    assert judge_quota(state.tuning) == 16
    # 拍1:12 条(<=16)→ 直通全落 spool。
    source.feed(12)
    assert hv._harvest_cycle(state, source.handle)
    assert state.totals["spool_candidates"] == 12
    # 无人消费:余量=16-12=4;拍2 来 6 条 > 余量 → 回落分诊(常见形状被压,spool 只多稀有/抽检额度)。
    source.feed(6)
    assert hv._harvest_cycle(state, source.handle)
    assert state.totals["spool_candidates"] < 18
    # 消费:一口气取走全部积压(消费口粮=judge_quota),流序单调。
    records, backlog = read_spool_records(state, max_candidates=judge_quota(state.tuning))
    positions = [row["stream_pos"] for record in records for row in record.get("candidates", [])]
    assert positions == sorted(positions)
    assert backlog["candidates_unread"] == 0
    # 余量恢复 → 直通恢复。
    assert judge_headroom(state) == 16
    source.feed(10)
    assert hv._harvest_cycle(state, source.handle)
    tail_backlog = hv._backlog_info(state, hv.read_spool_cursor(state))
    assert tail_backlog["candidates_unread"] == 10  # 10 条全量直通,一条不少


def test_pull_payload_marks_full_read(owner_home: Path):
    source = _FakeSource()
    source.feed(8)
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u-test"))
    tool = WatchStreamTool(agent)
    tool.allow_private_resolution = True
    tool._fetch_json = source.handle
    opened = json.loads(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull", "background_harvest": 0}).output)
    pulled = json.loads(tool.execute({"action": "pull", "watch_id": opened["watch_id"], "max_wait_seconds": 0}).output)
    assert pulled["candidates"], pulled
    assert any(row.get("triage", {}).get("reason") == "full_stream_read" for row in pulled["candidates"])
    assert any(row.get("triage", {}).get("full_read") is True for row in pulled["candidates"])
    assert "full_stream_read" in pulled["guidance"]
