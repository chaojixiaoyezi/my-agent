"""摄取引擎单元:字段画像分类、滑窗计数、稀有度分诊、账目守恒、快照恢复。"""

from __future__ import annotations

from agent.ingestion.config import IngestTuning, tuning_from_params
from agent.ingestion.engine import StreamDigestEngine
from agent.ingestion.field_profile import FieldProfile, ProfileTable
from agent.ingestion.flatten import flatten_event
from agent.ingestion.signature import classed_pairs, signature_of
from agent.ingestion.window_counter import SlidingWindowCounter


def _tuning(**overrides) -> IngestTuning:
    base = {"window_seconds": 300, "bucket_seconds": 30, "rare_threshold": 3, "max_candidates_per_pull": 8}
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
    """spec 命中附取值窗口频次证据(§7.5 重判喂料),车道内按频次升序:判据配错偏常态
    (高频但未到常态级 <spec_target_common_value_pct)时高频命中沉底、稀有命中优先保留,
    不再恒最优平手按序号。到常态级(≥25%)的配反命中由 P2 免疫直接压组断源,
    见 test_named_target_common_value_suppressed_with_alert。"""
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
    assert not digest.spec_target_common  # <25% 常态级:免疫不介入,沉底证据路保持
    rows = candidate_rows(digest)
    spec_rows = [r for r in rows if r["triage"]["reason"] == "spec_target_value"]
    assert all("value_window_count" in r["triage"]["spec_match"] for r in spec_rows)


def test_outside_normal_common_value_suppressed_not_flooded():
    """洪泛免疫(真机实锤):常态清单漏列一个高频取值时,"常态之外"不再把该取值刷满
    spec 车道——高频命中按常态压组(可抽查不静默),稀有的常态之外取值照抬。"""
    from agent.ingestion.source_spec import parse_source_spec

    tuning = _tuning(value_min_support=16, value_rare_threshold=3, head_value_rare_pct=2)
    engine = StreamDigestEngine(tuning)
    # 常态五种漏列 retried(高频 8%):旧行为 retried 全部 outside_normal 洪泛
    engine.apply_spec(parse_source_spec({"result_field": "log", "normal_values": ["accepted", "queued"]}))
    events = []
    for i in range(300):
        word = "retried" if i % 4 == 3 else "accepted"
        events.append((i, {"kind": "op", "log": f"{word} ref={i:08x}"}))
    events.append((300, {"kind": "op", "log": "diverted ref=deadbeef"}))

    digest = engine.process(events, now=1000.0)

    spec_hits = [c for c in digest.candidates if c.reason == "spec_target_value"]
    retried_hits = [c for c in spec_hits if "retried" in c.value_token]
    # 免疫生效前的支持度积累期(field_count<64)会放进少量 retried,高频后全部压组
    assert len(retried_hits) < 12
    assert [c for c in spec_hits if "diverted" in c.value_token]  # 稀有常态之外照抬
    assert digest.suppressed_total > 50  # 高频 retried 进被压组账目,不静默丢


def test_configure_keeps_value_counters_no_second_cold_start():
    """真机实锤:apply_spec 若重置取值计数器,configure 后有第二个支持度冷启动窗口,
    漏列常态的 outside_normal 在此集中放行(20 条误报里 18 条)。取值计数器/首记号画像
    按字段路径记账、与 spec 字段集无关——configure 保留它们,免疫立即在岗。"""
    from agent.ingestion.source_spec import parse_source_spec

    tuning = _tuning(value_min_support=64, value_rare_threshold=3, head_value_rare_pct=2)
    engine = StreamDigestEngine(tuning)
    warmup = [(i, {"kind": "op", "log": f"retried ref={i:08x}"}) for i in range(200)]
    engine.process(warmup, now=1000.0)  # 支持度/频次已积累
    engine.apply_spec(parse_source_spec({"result_field": "log", "normal_values": ["accepted"]}))

    batch = [(200 + i, {"kind": "op", "log": f"retried ref={200 + i:08x}"}) for i in range(30)]
    digest = engine.process(batch, now=1001.0)

    # configure 后第一批:漏列的高频常态立即被免疫压组,不再有"重新攒 64 条"的放行窗
    assert not [c for c in digest.candidates if c.reason == "spec_target_value"]
    assert digest.suppressed_total == 30


def test_outside_normal_immunity_spares_named_targets_and_can_be_disabled():
    """点名 target 的高频取值不受免疫影响;pct=0 关闭免疫回到旧行为。"""
    from agent.ingestion.source_spec import parse_source_spec

    events = [(i, {"kind": "op", "log": f"retried ref={i:08x}"}) for i in range(120)]
    named = StreamDigestEngine(_tuning(value_min_support=16, value_rare_threshold=3))
    named.apply_spec(parse_source_spec({"result_field": "log", "target_value_contains": ["retried"]}))
    digest = named.process(events, now=1000.0)
    assert named.totals["escalated_spec_target"] > 0  # 点名高频照抬(cap 内)

    legacy = StreamDigestEngine(
        _tuning(value_min_support=16, value_rare_threshold=3, outside_normal_common_value_pct=0)
    )
    legacy.apply_spec(parse_source_spec({"result_field": "log", "normal_values": ["accepted"]}))
    legacy_digest = legacy.process(events, now=1000.0)
    outside = [c for c in legacy_digest.candidates if c.spec_mode == "outside_normal"]
    assert outside  # 关掉免疫=旧行为,高频常态之外仍抬


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


# --- P2 点名 target 配反免疫:target 取值≈常态时压组防洪泛(u-2hb 26 误报形态) ----------


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


def test_named_target_common_value_suppressed_with_alert():
    """配反的 target(常态高频值)在支持度热身后被压组:不再逐条抬升(断洪泛源),
    digest 带结构化告警、totals 记账——2h 26 误报的持续洪泛形态从源头掐断。"""
    tuning = _tuning(value_min_support=64, value_rare_threshold=3)
    engine = StreamDigestEngine(tuning)
    engine.process(_status_events(0, 200, fail_every=12), now=1000.0)  # 频次热身:ok≈92%
    engine.apply_spec(_inverted_spec())

    digest = engine.process(_status_events(200, 30), now=1001.0)

    assert not [c for c in digest.candidates if c.reason == "spec_target_value"]
    assert digest.suppressed_total == 30
    alerts = list(digest.spec_target_common.values())
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["path"] == "status" and alert["value"] == "ok" and alert["mode"] == "target_value"
    assert alert["suppressed_this_call"] == 30
    assert alert["field_window_count"] >= 64
    assert engine.totals["spec_target_suppressed"] == 30


def test_named_target_sparse_density_not_suppressed():
    """护栏:真·稀疏目标(离线台密度 5-8% 量级)绝不误杀——8% 的 target 照常逐条抬升,
    零告警零压组记账。"""
    tuning = _tuning(value_min_support=64, value_rare_threshold=3)
    engine = StreamDigestEngine(tuning)
    engine.process(_status_events(0, 200, fail_every=12), now=1000.0)
    from agent.ingestion.source_spec import parse_source_spec

    engine.apply_spec(parse_source_spec({"result_field": "status", "target_values": ["fail"]}))

    digest = engine.process(_status_events(200, 36, fail_every=12), now=1001.0)

    fail_hits = [c for c in digest.candidates if c.reason == "spec_target_value"]
    assert len(fail_hits) == 3  # 36 条里 3 条 fail,全部抬升
    assert not digest.spec_target_common
    assert engine.totals["spec_target_suppressed"] == 0


def test_named_target_common_disabled_by_zero_pct():
    """旋钮 0=关:行为回到旧版(配反 target 照抬),供出问题时一键回退。"""
    tuning = _tuning(value_min_support=64, value_rare_threshold=3, spec_target_common_value_pct=0)
    engine = StreamDigestEngine(tuning)
    engine.process(_status_events(0, 200), now=1000.0)
    engine.apply_spec(_inverted_spec())

    digest = engine.process(_status_events(200, 30), now=1001.0)

    assert [c for c in digest.candidates if c.reason == "spec_target_value"]
    assert not digest.spec_target_common


def test_named_target_contains_mode_also_immunized():
    """target_value_contains 命中的【具体取值】≈常态时同样免疫(按取值频次判,不按规则)。"""
    from agent.ingestion.source_spec import parse_source_spec

    tuning = _tuning(value_min_support=64, value_rare_threshold=3)
    engine = StreamDigestEngine(tuning)
    engine.process(_status_events(0, 200), now=1000.0)
    engine.apply_spec(parse_source_spec({"result_field": "status", "target_value_contains": ["o"]}))

    digest = engine.process(_status_events(200, 30), now=1001.0)

    assert not [c for c in digest.candidates if c.reason == "spec_target_value"]
    alerts = list(digest.spec_target_common.values())
    assert alerts and alerts[0]["mode"] == "target_contains"


def test_named_target_cold_window_not_suppressed():
    """冷启动(字段窗口样本量 < value_min_support)= 无证据不定罪:照常抬升,不压不警。"""
    tuning = _tuning(value_min_support=64, value_rare_threshold=3)
    engine = StreamDigestEngine(tuning)
    engine.apply_spec(_inverted_spec())

    digest = engine.process(_status_events(0, 30), now=1000.0)

    assert [c for c in digest.candidates if c.reason == "spec_target_value"]
    assert not digest.spec_target_common
