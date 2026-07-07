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


def _configure_inert_spec(state) -> None:
    """给 state 配一份不匹配事件的最小 spec(只 ignore 一个不存在的字段):source_spec 非空
    → cold_start=False,但 classify 恒 None、不改任何候选行为。用于隔离测"已配稳态源的
    headroom 闸"这一条路(与 no-spec 冷启动无条件直通、passthrough 无条件直通区分开)。"""
    spec = parse_source_spec({"ignore_fields": ["__inert__"]})
    state.source_spec = spec.to_payload()
    state.engine.apply_spec(spec)


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


def test_cold_start_full_reads_backlog_ignoring_headroom():
    """根因2:冷启动(还没 configure 出 spec)读存量时,判据没学出来,不能靠结构规则/
    批量/headroom 闸筛掉存量——整批无条件 full_read(宁滥勿漏,每条都递到模型)。
    大批(300>预算 48)且 headroom=0(积压顶死)本会回落粗筛,cold_start 下仍全量直通。"""
    engine = StreamDigestEngine(_tuning(full_read_per_pull=48))
    engine.process(_commons(0, 40), now=1000.0)  # 先热身让形状变常见
    digest = engine.process(_commons(40, 300), now=1010.0, judge_headroom=0, cold_start=True)
    assert len(digest.candidates) == 300  # 一条不筛,全量到模型
    assert digest.suppressed_total == 0 and digest.overflow == []
    assert engine.totals["escalated_full_read"] >= 300


def test_passthrough_spec_full_reads_ignoring_headroom():
    """根因1+3:模型学出"结构分不开、成败只在响应正文",configure passthrough=true →
    引擎对该源无条件 full_read,不受批量/headroom 闸,也不做稀有度/形状裁剪。"""
    spec = parse_source_spec({"passthrough": True})
    engine = StreamDigestEngine(_tuning(full_read_per_pull=48), spec)
    engine.process(_commons(0, 40), now=1000.0)
    digest = engine.process(_commons(40, 200), now=1010.0, judge_headroom=0)
    assert len(digest.candidates) == 200
    assert all(c.reason == "full_stream_read" for c in digest.candidates)
    assert engine.totals["escalated_full_read"] >= 200


def test_passthrough_surfaces_target_that_shares_shape_with_decoy():
    """根因1 核心:真目标与迷惑项 request/status 结构完全相同(都 200 成功),正文都是
    "同一个开头结论词 + 唯一高基数尾巴"(head token 相同、整值各不同),区别只藏在正文中段
    的语义(applied/persisted vs blocked/not-applied)。此时:
    · 整值高基数 → 字面少数派失明;· head token 相同 → 首记号少数派也分不开;· 签名相同 → 不 rare。
    非 passthrough 时真目标被压组吞(模型看不见=复现 14/15 漏报);passthrough 下每条都 surface,
    由模型读正文语义定真假(结构不替模型做去留)。

    正文构造成"同一开头词 accepted + 每条唯一的 conn id(前段就各不同 → 字段高基数 → s:* 折叠)
    + 中段语义 verdict":整值高基数(字面失明)、首记号 accepted 恒同(首记号少数派也失明)、
    签名恒同(不 rare),verdict 的语义差(applied vs blocked)引擎任何结构记号都摸不到。"""
    def _body(seq, verdict):
        return f"accepted conn={seq:08x}f1 handling inbound at edge; verdict {verdict} by policy engine"

    def _decoy(seq):
        return (seq, {"src_ip": "10.0.0.9", "req": "POST /apply",
                      "resp": {"status": 200, "body": _body(seq, "blocked_downstream_not_applied")}})

    def _target(seq):
        return (seq, {"src_ip": "10.0.0.9", "req": "POST /apply",
                      "resp": {"status": 200, "body": _body(seq, "applied_and_now_persisted")}})

    # audit 抽检车道关掉:隔离结构筛的去留本身(抽检会随机回捞被压组示例,是另一条正交的
    # 召回路,不该混进"结构规则是否吞掉真目标"的判定)。
    tune = _tuning(full_read_per_pull=0, audit_sample_per_pull=0, audit_floor_per_minute=0)
    bare = StreamDigestEngine(tune)
    bare.process([_decoy(i) for i in range(40)], now=1000.0)
    bare_digest = bare.process([_decoy(40 + i) for i in range(5)] + [_target(45)], now=1010.0)
    assert 45 not in {c.seq_hint for c in bare_digest.candidates}  # 真目标被结构规则吞掉(复现漏报)

    # passthrough:同一批,真目标 seq=45 与所有迷惑项都进候选,交给模型读正文判。
    spec = parse_source_spec({"passthrough": True})
    pt = StreamDigestEngine(_tuning(full_read_per_pull=48), spec)
    pt.process([_decoy(i) for i in range(40)], now=1000.0)
    pt_digest = pt.process([_decoy(40 + i) for i in range(5)] + [_target(45)], now=1010.0)
    pt_positions = {c.seq_hint for c in pt_digest.candidates}
    assert 45 in pt_positions and len(pt_positions) == 6  # 真目标 + 5 迷惑项全部 surface


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
    # 已配(非 passthrough)spec:headroom 闸只对"学过结构判据的稳态源"生效——积压时回落
    # 到学出来的结构规则是安全的。no-spec(冷启动)与 passthrough 走无条件直通(根因2/3),
    # 不受本闸,由各自的钉子测试覆盖。这里只给 ignore_fields(不匹配事件、不改候选行为),
    # 目的仅是让 source_spec 非空 → cold_start=False,复现 headroom 闸的连续谱。
    _configure_inert_spec(state)
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
