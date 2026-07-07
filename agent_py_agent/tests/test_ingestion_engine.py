"""摄取引擎单元:字段画像分类、滑窗计数、稀有度分诊、账目守恒、快照恢复。"""

from __future__ import annotations

from agent.ingestion.config import IngestTuning, tuning_from_params
from agent.ingestion.engine import StreamDigestEngine
from agent.ingestion.field_profile import FieldProfile, ProfileTable
from agent.ingestion.flatten import flatten_event
from agent.ingestion.signature import classed_pairs, signature_of
from agent.ingestion.window_counter import SlidingWindowCounter


def _tuning(**overrides) -> IngestTuning:
    # full_read_per_pull=0:本文件聚焦降维分诊层(车道/名额/压组),显式关正常量直通;
    # 直通行为的专测在 test_ingestion_full_read.py。
    base = {"window_seconds": 300, "bucket_seconds": 30, "rare_threshold": 3, "max_candidates_per_pull": 8, "full_read_per_pull": 0}
    base.update(overrides)
    return IngestTuning(**base)


def test_flatten_nested_and_arrays():
    pairs = dict(flatten_event({"a": {"b": True}, "items": [{"x": 1}, {"x": 2}], "s": "hi"}))
    assert pairs["a.b"] is True
    assert pairs["items.__len__"] == 2
    assert pairs["s"] == "hi"


def test_profile_bool_and_low_cardinality_literal():
    table = ProfileTable(8)
    assert table.observe_and_token("flag", True) == "b:T"
    assert table.observe_and_token("kind", "charge") == "s:charge"


def test_profile_high_cardinality_string_collapses():
    table = ProfileTable(4)
    for index in range(6):
        token = table.observe_and_token("trace", f"id-{index}")
    assert token == "s:*"


def test_profile_monotone_int_collapses_to_mono():
    profile = FieldProfile()
    for value in range(40):
        profile.observe(value, 8)
    assert profile.token(41) == "n:mono"


def test_profile_nonmonotone_int_becomes_magnitude_bucket():
    profile = FieldProfile()
    values = [5, 3, 9, 480, 12, 77, 260, 8, 33, 410]
    for value in values * 4:
        profile.observe(value, 4)
    assert profile.token(7) == "n:e0"
    assert profile.token(450) == "n:e2"


def test_window_counter_evicts_old_buckets():
    counter = SlidingWindowCounter(window_seconds=60, bucket_seconds=10)
    assert counter.observe("sig", now=1000.0) == 1
    assert counter.observe("sig", now=1005.0) == 2
    assert counter.window_count("sig", now=1100.0) == 0


def test_engine_rare_signature_escalates_and_common_suppresses():
    engine = StreamDigestEngine(_tuning())
    warmup = [(i, {"kind": "beat", "flag": False, "seq": i}) for i in range(200)]
    engine.process(warmup, now=2000.0)
    steady = [(200 + i, {"kind": "beat", "flag": False, "seq": 200 + i}) for i in range(200)]
    steady.insert(100, (999, {"kind": "beat", "flag": True, "seq": 300}))
    digest = engine.process(steady, now=2003.0)
    flagged = [c for c in digest.candidates if c.event.get("flag") is True]
    assert len(flagged) == 1
    assert digest.suppressed_total > 150
    assert digest.groups and digest.groups[0].call_count > 100


def test_engine_accounting_balances():
    engine = StreamDigestEngine(_tuning(max_candidates_per_pull=2, rare_threshold=1))
    events = [(i, {"v": f"unique-{i}", "n": i}) for i in range(30)]
    digest = engine.process(events, now=3000.0)
    assert digest.seen == 30
    # 每事件恰好一个归宿(候选/溢出/压组);抽检行(audit_sample)是被压事件的
    # 【复读】——事件本身仍记在压组账里,复读行单列 audit_sampled 账,不破均衡。
    primary = [c for c in digest.candidates if c.reason != "audit_sample"]
    audit = [c for c in digest.candidates if c.reason == "audit_sample"]
    assert len(primary) + len(digest.overflow) + digest.suppressed_total == 30
    assert len(audit) == engine.totals["audit_sampled"]


def test_engine_snapshot_restore_prewarms_first_seen():
    engine = StreamDigestEngine(_tuning())
    events = [(i, {"kind": "beat", "seq": i}) for i in range(120)]
    engine.process(events, now=4000.0)
    snap = engine.snapshot(now=4000.0)

    fresh = StreamDigestEngine(_tuning())
    fresh.restore(snap, now=5000.0)
    digest = fresh.process([(200, {"kind": "beat", "seq": 200})], now=5000.0)
    assert all(not candidate.first_seen for candidate in digest.candidates)
    assert fresh.totals["events_seen"] == 121


def test_tuning_from_params_clamps_and_ignores_bad_values():
    tuning = tuning_from_params({"rare_threshold": "999", "page_limit": "abc", "window_seconds": 5})
    assert tuning.rare_threshold == 100
    assert tuning.page_limit == 400
    assert tuning.window_seconds == 30


def test_signature_stable_across_key_order():
    table = ProfileTable(24)
    sig_a = signature_of(classed_pairs({"a": 1, "b": "x"}, table))
    sig_b = signature_of(classed_pairs({"b": "x", "a": 1}, table))
    assert sig_a == sig_b


def test_head_token_lane_escalates_rare_text_conclusion():
    """§4 文本结果端:结论词+高基数尾巴的文本消息,稀有首记号经窗口频次抬为候选。"""
    tuning = _tuning(low_cardinality_limit=8, value_min_support=16, value_rare_threshold=3)
    engine = StreamDigestEngine(tuning)
    events = [(i, {"kind": "op", "log": f"returned ref={i:08x} t={i}"}) for i in range(80)]
    events.append((80, {"kind": "op", "log": "hijacked ref=deadbeef t=9"}))

    digest = engine.process(events, now=1000.0)

    hits = [c for c in digest.candidates if c.value_token == "s1:hijacked"]
    assert len(hits) == 1
    assert hits[0].seq_hint == 80
    assert hits[0].reason == "minority_field_value"
    assert hits[0].value_path == "log"
    # 常态首记号(returned)不因首记号车道被误抬。
    assert not [c for c in digest.candidates if c.value_token == "s1:returned"]


def test_head_token_lane_relative_ratio_spares_repeated_rare_conclusion():
    """宽抬相对占比闸(§7.3):目标共享同一结论词、窗口内累计超绝对阈值(3)时仍抬
    (相对字段样本量占比仍极低);只用绝对阈值会从第 4 条起全漏——真机 53% 召回的漏因。"""
    events = []
    seq = 0
    for i in range(400):
        events.append((seq, {"kind": "op", "log": f"returned ref={seq:08x}"}))
        seq += 1
        if i % 80 == 79:  # 每 80 条常态夹一条同词目标,共 5 条(窗口内累计 5 > 3)
            events.append((seq, {"kind": "op", "log": f"diverted ref={seq:08x}"}))
            seq += 1

    wide = StreamDigestEngine(_tuning(value_min_support=16, value_rare_threshold=3, head_value_rare_pct=2))
    hits = [c for c in wide.process(events, now=1000.0).candidates if c.value_token == "s1:diverted"]
    assert len(hits) == 5

    absolute_only = StreamDigestEngine(
        _tuning(value_min_support=16, value_rare_threshold=3, head_value_rare_pct=0)
    )
    old_hits = [
        c for c in absolute_only.process(events, now=1000.0).candidates if c.value_token == "s1:diverted"
    ]
    assert len(old_hits) == 3  # 对照:关掉相对闸即回到绝对阈值的漏


def test_head_token_candidates_use_independent_lane_quota():
    """首记号候选独立名额(§7.3):同批字面少数派挤爆 value 车道名额时,
    文本结果端的首记号目标不与之同池、照样上。"""
    tuning = _tuning(value_min_support=16, value_rare_threshold=3, head_value_rare_pct=2)
    engine = StreamDigestEngine(tuning)
    events = [(i, {"status": "ok", "log": f"returned ref={i:08x}"}) for i in range(80)]
    for j in range(10):  # 10 个各只出现一次的字面少数派(> 字面车道名额 8)
        events.append((80 + j, {"status": f"v{j}", "log": f"returned ref={80 + j:08x}"}))
    events.append((90, {"status": "ok", "log": "diverted ref=deadbeef"}))

    digest = engine.process(events, now=1000.0)

    assert [c for c in digest.candidates if c.value_token == "s1:diverted"]
    assert engine.totals["escalated_head_value"] == 1
    literal_kept = [c for c in digest.candidates if c.reason == "minority_field_value" and c.value_lane != "head"]
    assert len(literal_kept) == 8  # 字面车道仍按自己的名额截断


def test_spec_candidates_carry_window_frequency_and_rank_rare_first():
    """spec 命中附取值窗口频次证据(§7.5 重判喂料),车道内按频次升序:高频命中沉底、
    稀有命中优先保留,不再恒最优平手按序号——这是"高频命中照抬但不淹稀有"的名额算术
    (未到调查线 <frequent_hit_investigate_pct 时连告警都不发,只留沉底证据)。"""
    from agent.ingestion.source_spec import parse_source_spec
    from agent.ingestion.watch_payloads import candidate_rows

    tuning = _tuning(value_min_support=16, value_rare_threshold=3, head_value_rare_pct=2)
    engine = StreamDigestEngine(tuning)
    engine.apply_spec(
        parse_source_spec({"result_field": "log", "target_values": ["returned", "diverted"], "max_per_pull": 2})
    )
    # returned ≈13% 混流(高频但 <25% 常态级):每 8 条 1 条 returned,其余 accepted。
    warmup = [
        (i, {"kind": "op", "log": f"{'returned' if i % 8 == 7 else 'accepted'} ref={i:08x}"})
        for i in range(60)
    ]
    engine.process(warmup, now=1000.0)

    batch2 = [(100 + i, {"kind": "op", "log": f"returned ref={100 + i:08x}"}) for i in range(2)]
    batch2.append((103, {"kind": "op", "log": "diverted ref=deadbeef"}))
    digest = engine.process(batch2, now=1010.0)

    kept_spec = [c for c in digest.candidates if c.reason == "spec_target_value"]
    assert len(kept_spec) == 2  # max_per_pull 截断(3 命中只留 2)
    diverted = [c for c in kept_spec if "diverted" in c.value_token]
    assert diverted and diverted[0].value_window_count == 1  # 稀有命中保住且证据=首记号窗口计数
    returned = [c for c in kept_spec if "returned" in c.value_token]
    assert returned and returned[0].value_window_count >= 4  # 高频命中带出"这取值窗口内更常见"的证据
    assert not digest.frequent_hits  # <25% 调查线:不发告警,沉底证据路保持
    rows = candidate_rows(digest)
    spec_rows = [r for r in rows if r["triage"]["reason"] == "spec_target_value"]
    assert all("value_window_count" in r["triage"]["spec_match"] for r in spec_rows)


def test_outside_normal_frequent_class_not_discarded_and_alerts():
    """根本设计修复(g8 复验实锤:按百分比免疫把 18% 密度真事整车道吞掉):常态清单
    漏列/真事高发导致"常态之外"某取值类高频时,命中【绝不按频率丢弃】——照进 spec 车道
    (名额内稀有先上、高频沉底、溢出入 overflow 账),同时发调查告警(计数事实+示例事件)
    请模型按内容定去留;被内容规则认出的常态按条目记账。"""
    from agent.ingestion.source_spec import parse_source_spec

    tuning = _tuning(value_min_support=16, value_rare_threshold=3, head_value_rare_pct=2)
    engine = StreamDigestEngine(tuning)
    # 常态只学了 accepted,retried 以 50% 高频出现(远超 25% 调查线),再夹 1 条稀有 diverted。
    engine.apply_spec(parse_source_spec({"result_field": "log", "normal_values": ["accepted", "queued"]}))
    events = []
    for i in range(300):
        word = "retried" if i % 2 else "accepted"
        events.append((i, {"kind": "op", "log": f"{word} ref={i:08x}"}))
    events.append((300, {"kind": "op", "log": "diverted ref=deadbeef"}))

    digest = engine.process(events, now=1000.0)

    spec_hits = [c for c in digest.candidates if c.reason == "spec_target_value"]
    assert [c for c in spec_hits if "diverted" in c.value_token]  # 稀有常态之外优先保住
    assert [c for c in spec_hits if "retried" in c.value_token]  # 高频常态之外照抬,不整批扔
    # 零"按频率压组":被压组只有 accepted(内容规则命中回落通用车道的常态),retried 全在
    # 候选+overflow 账里(151 个 spec 命中 = 车道名额 8 + 溢出 143,坐标可审计)。
    assert digest.suppressed_total < 150
    assert len(spec_hits) + len(digest.overflow) >= 151 - 8
    alerts = list(digest.frequent_hits.values())
    assert len(alerts) == 1 and alerts[0]["mode"] == "outside_normal"
    assert alerts[0]["value_class"].startswith("head:")  # 文本结果端按首记号类折叠,一类一行
    assert alerts[0]["exemplar_event"] and alerts[0]["hits_this_call"] > 100
    assert engine.totals["spec_frequent_hits"] > 100
    # 内容规则命中账:accepted 被"认得它了"而减负,按规则条目计数、零静默。
    assert engine.rule_hits.get("normal_value\x1eaccepted", 0) == 150
    assert digest.normal_rule_hits == 150


def test_configure_keeps_value_counters_no_second_cold_start():
    """真机实锤:apply_spec 若重置取值计数器,configure 后有第二个支持度冷启动窗口
    (20 条误报里 18 条来自这里)。取值计数器/首记号画像按字段路径记账、与 spec 字段集
    无关——configure 保留它们,调查告警的频次证据立即在岗(新语义:高频命中照抬不丢,
    告警即时可发=统计没有被重置的直接证据)。"""
    from agent.ingestion.source_spec import parse_source_spec

    tuning = _tuning(value_min_support=64, value_rare_threshold=3, head_value_rare_pct=2)
    engine = StreamDigestEngine(tuning)
    warmup = [(i, {"kind": "op", "log": f"retried ref={i:08x}"}) for i in range(200)]
    engine.process(warmup, now=1000.0)  # 支持度/频次已积累
    engine.apply_spec(parse_source_spec({"result_field": "log", "normal_values": ["accepted"]}))

    batch = [(200 + i, {"kind": "op", "log": f"retried ref={200 + i:08x}"}) for i in range(30)]
    digest = engine.process(batch, now=1001.0)

    # configure 后第一批:频次证据在岗(窗口计数没归零)→ 30 条高频命中全部照抬
    # (车道 8 + overflow 22,零压组零丢弃),且调查告警第一批就带足计数事实。
    hits = [c for c in digest.candidates if c.reason == "spec_target_value"]
    assert len(hits) == 8 and len(digest.overflow) == 22
    assert digest.suppressed_total == 0
    alerts = list(digest.frequent_hits.values())
    assert alerts and alerts[0]["field_window_count"] >= 200  # 证据没有"重新攒 64 条"的洞
    assert engine.totals["spec_frequent_hits"] == 30


def test_realistic_density_targets_escalate_at_any_density():
    """g8 复验实锤回归(验收①):目标密度 17% 的 outside_normal 命中照抬(旧 2% 免疫把
    整车道目标当"常态"吞进被压组:seen=8470/escalated=6、召回平 ~7%);密度 75%(哪天
    真事占比过了 25% 线)也照抬——按百分比丢弃的判据已移除,不存在"撞线即吞"。
    高密度只多一条调查告警(带示例),由模型按内容定去留;被压组里零 spec 命中。"""
    from agent.ingestion.source_spec import parse_source_spec

    tuning = _tuning(value_min_support=16, value_rare_threshold=3)
    engine = StreamDigestEngine(tuning)
    engine.apply_spec(parse_source_spec({"result_field": "flag", "normal_values": ["false"]}))
    # ~17% 目标密度、散布均匀(与 g8 sim 的 hash 散布同形态):每 6 条 1 条 flag=true。
    events = [(i, {"kind": "beat", "flag": (i % 6) == 0}) for i in range(300)]

    digest = engine.process(events, now=1000.0)

    hits = [c for c in digest.candidates if c.reason == "spec_target_value"]
    assert hits, "~17% 密度的常态之外目标必须进候选"
    assert not digest.frequent_hits  # 25% 调查线以下:连告警都不发
    assert engine.totals["spec_frequent_hits"] == 0

    # 75% 主导性高频(漏列常态或真事刷屏,内容才知道):命中仍全量进候选/overflow 账,
    # 一条不吞;只发调查告警请模型按内容定性。
    flooded = StreamDigestEngine(_tuning(value_min_support=16, value_rare_threshold=3))
    flooded.apply_spec(parse_source_spec({"result_field": "flag", "normal_values": ["false"]}))
    flood_events = [(i, {"kind": "beat", "flag": (i % 4) != 3}) for i in range(300)]
    flood_digest = flooded.process(flood_events, now=1000.0)
    flood_hits = [c for c in flood_digest.candidates if c.reason == "spec_target_value"]
    assert len(flood_hits) == 8, "75% 密度的命中也照抬满车道名额,不许整批扔"
    assert len(flood_digest.overflow) == 225 - 8  # 溢出带坐标进账,零静默
    assert flood_digest.suppressed_total < 75  # 被压组只有 flag=false 的常态,零 spec 命中
    alerts = list(flood_digest.frequent_hits.values())
    assert alerts and alerts[0]["mode"] == "outside_normal" and alerts[0]["exemplar_event"]


def test_named_high_frequency_targets_escalate_and_alert_knob_can_disable():
    """点名 target 高频照抬(cap 内)且带调查告警;frequent_hit_investigate_pct=0 只关
    告警,抬升行为不变(不存在任何'关掉就回到按频丢弃'的路径)。"""
    from agent.ingestion.source_spec import parse_source_spec

    events = [(i, {"kind": "op", "log": f"retried ref={i:08x}"}) for i in range(120)]
    named = StreamDigestEngine(_tuning(value_min_support=16, value_rare_threshold=3))
    named.apply_spec(parse_source_spec({"result_field": "log", "target_value_contains": ["retried"]}))
    digest = named.process(events, now=1000.0)
    assert named.totals["escalated_spec_target"] > 0  # 点名高频照抬(cap 内)
    named_alerts = list(digest.frequent_hits.values())
    assert named_alerts and named_alerts[0]["mode"] == "target_contains"

    muted = StreamDigestEngine(
        _tuning(value_min_support=16, value_rare_threshold=3, frequent_hit_investigate_pct=0)
    )
    muted.apply_spec(parse_source_spec({"result_field": "log", "normal_values": ["accepted"]}))
    muted_digest = muted.process(events, now=1000.0)
    outside = [c for c in muted_digest.candidates if c.spec_mode == "outside_normal"]
    assert outside  # 高频常态之外仍抬
    assert not muted_digest.frequent_hits and muted.totals["spec_frequent_hits"] == 0


def test_head_token_lane_stays_silent_for_high_cardinality_heads():
    """trace/id 类字段:首记号分布本身高基数 → 基数闸关死,一个候选都不从此路产。
    基数上限须 < 支持度(默认 48<64 同序):纯高基数字段 distinct 与样本量同速涨,
    闸先于抬生效;测试用小参数显式保持该次序。"""
    tuning = _tuning(
        low_cardinality_limit=8, head_low_cardinality_limit=8, value_min_support=16, value_rare_threshold=3
    )
    engine = StreamDigestEngine(tuning)
    events = [(i, {"kind": "op", "trace": f"{i:040x}"}) for i in range(120)]

    digest = engine.process(events, now=1000.0)

    assert not [c for c in digest.candidates if c.value_token.startswith("s1:")]


def test_head_token_profiles_survive_snapshot_restore():
    tuning = _tuning(low_cardinality_limit=8, value_min_support=16, value_rare_threshold=3)
    engine = StreamDigestEngine(tuning)
    engine.process([(i, {"trace": f"{i:040x}", "log": f"returned ref={i:08x}"}) for i in range(80)], now=1000.0)
    snap = engine.snapshot(now=1000.0)

    fresh = StreamDigestEngine(tuning)
    fresh.restore(snap, now=1001.0)

    assert fresh.head_profiles.profiles["trace"].overflowed is True
    assert fresh.head_profiles.profiles["log"].overflowed is False
    digest = fresh.process([(200, {"trace": "f" * 40, "log": "hijacked ref=deadbeef"})], now=1002.0)
    assert [c.value_token for c in digest.candidates if c.value_token.startswith("s1:")] == ["s1:hijacked"]


# --- 高频命中类的调查告警(P2 u-2hb 配反形态的新处置:照抬有界 + 告警交模型研判) --------


def _inverted_spec():
    from agent.ingestion.source_spec import parse_source_spec

    # 判据配反:把常态高频取值 ok 配成了 target(u-2hb 形态)。
    return parse_source_spec({"result_field": "status", "target_values": ["ok"]})


def _status_events(start: int, count: int, *, fail_every: int = 0) -> list:
    events = []
    for i in range(count):
        word = "fail" if fail_every and i % fail_every == fail_every - 1 else "ok"
        events.append((start + i, {"kind": "op", "status": word}))
    return events


def test_inverted_target_flood_escalates_bounded_with_alert():
    """配反的 target(常态高频值):命中照抬但被车道名额+溢出账兜住(判读不被淹、零丢弃
    零压组),同批带调查告警(计数事实+示例)——模型按内容识别配反后重 configure 建规则,
    而不是代码按频率替模型扔(2h 26 误报的根治=告警驱动的重配,不是盲扔)。"""
    tuning = _tuning(value_min_support=64, value_rare_threshold=3)
    engine = StreamDigestEngine(tuning)
    engine.process(_status_events(0, 200, fail_every=12), now=1000.0)  # 频次热身:ok≈92%
    engine.apply_spec(_inverted_spec())

    digest = engine.process(_status_events(200, 30), now=1001.0)

    hits = [c for c in digest.candidates if c.reason == "spec_target_value"]
    assert len(hits) == 8 and len(digest.overflow) == 22  # 名额有界,其余入溢出账
    assert digest.suppressed_total == 0  # 零按频压组
    alerts = list(digest.frequent_hits.values())
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["path"] == "status" and alert["value"] == "ok" and alert["mode"] == "target_value"
    assert alert["hits_this_call"] == 30
    assert alert["field_window_count"] >= 64
    assert alert["exemplar_event"].get("status") == "ok"  # 按内容研判的示例喂料
    assert engine.totals["spec_frequent_hits"] == 30


def test_named_target_sparse_density_no_alert():
    """护栏:真·稀疏目标(离线台密度 5-8% 量级)照常逐条全量抬升,零告警零记账。"""
    tuning = _tuning(value_min_support=64, value_rare_threshold=3)
    engine = StreamDigestEngine(tuning)
    engine.process(_status_events(0, 200, fail_every=12), now=1000.0)
    from agent.ingestion.source_spec import parse_source_spec

    engine.apply_spec(parse_source_spec({"result_field": "status", "target_values": ["fail"]}))

    digest = engine.process(_status_events(200, 36, fail_every=12), now=1001.0)

    fail_hits = [c for c in digest.candidates if c.reason == "spec_target_value"]
    assert len(fail_hits) == 3  # 36 条里 3 条 fail,全部抬升
    assert not digest.frequent_hits
    assert engine.totals["spec_frequent_hits"] == 0


def test_frequent_alert_knob_zero_disables_alert_only():
    """旋钮 0=只关告警:抬升行为与有告警时完全一致(没有任何路径回到"按频丢弃")。"""
    tuning = _tuning(value_min_support=64, value_rare_threshold=3, frequent_hit_investigate_pct=0)
    engine = StreamDigestEngine(tuning)
    engine.process(_status_events(0, 200), now=1000.0)
    engine.apply_spec(_inverted_spec())

    digest = engine.process(_status_events(200, 30), now=1001.0)

    assert [c for c in digest.candidates if c.reason == "spec_target_value"]
    assert digest.suppressed_total == 0
    assert not digest.frequent_hits


def test_cold_window_no_alert_still_escalates():
    """冷启动(字段窗口样本量 < value_min_support)= 无证据不惊动:照常抬升,零告警。"""
    tuning = _tuning(value_min_support=64, value_rare_threshold=3)
    engine = StreamDigestEngine(tuning)
    engine.apply_spec(_inverted_spec())

    digest = engine.process(_status_events(0, 30), now=1000.0)

    assert [c for c in digest.candidates if c.reason == "spec_target_value"]
    assert not digest.frequent_hits


def test_normal_rule_hits_accounted_and_survive_snapshot():
    """内容过滤规则账(建了哪条、拦了多少,零静默):normal_values/normal_value_contains
    命中按【规则条目】计数;digest 报本批数、totals 报累计;快照往返保留;换 spec 版本
    账目重置(账跟着规则清单走)。规则命中的事件回落通用车道,不硬丢。"""
    from agent.ingestion.source_spec import parse_source_spec

    tuning = _tuning(value_min_support=16, value_rare_threshold=3)
    engine = StreamDigestEngine(tuning)
    engine.apply_spec(
        parse_source_spec({"result_field": "status", "normal_values": ["ok"], "normal_value_contains": ["done"]})
    )
    events = [(i, {"kind": "op", "status": "ok" if i % 2 else f"done x{i}"}) for i in range(40)]
    digest = engine.process(events, now=1000.0)

    assert digest.normal_rule_hits == 40
    assert engine.rule_hits["normal_value\x1eok"] == 20
    assert engine.rule_hits["normal_contains\x1edone"] == 20
    assert engine.totals["spec_normal_rule_hits"] == 40

    snap = engine.snapshot(now=1000.0)
    fresh = StreamDigestEngine(tuning)
    fresh.restore(snap, now=1001.0)
    assert fresh.rule_hits == engine.rule_hits

    engine.apply_spec(parse_source_spec({"result_field": "status", "normal_values": ["ok", "done"]}))
    assert engine.rule_hits == {}  # 规则清单换版,per-规则账重开(累计 totals 保留)
    assert engine.totals["spec_normal_rule_hits"] == 40


def test_apply_spec_resets_window_only_on_ignore_change():
    """建规则不付冷启动:只改取值判据(normal_*/target_*)保留签名窗/census/预热态;
    ignore_fields 变化才重置(字段集变了旧签名不可比,按旧语义重走预热遍)。"""
    from agent.ingestion.source_spec import parse_source_spec

    tuning = _tuning(value_min_support=16, value_rare_threshold=3)
    engine = StreamDigestEngine(tuning)
    engine.process([(i, {"kind": "op", "status": "ok", "junk": f"u-{i:06d}"}) for i in range(100)], now=1000.0)
    assert engine._first_call_done and engine._census

    engine.apply_spec(parse_source_spec({"result_field": "status", "normal_values": ["ok"]}))
    assert engine._first_call_done is True and engine._census  # 规则级变更:统计原地保留

    engine.apply_spec(
        parse_source_spec({"result_field": "status", "normal_values": ["ok"], "ignore_fields": ["junk"]})
    )
    assert engine._first_call_done is False and not engine._census  # 字段集变更:重置重预热
