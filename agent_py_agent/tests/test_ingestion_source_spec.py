"""per-源判据 spec:解析校验 / 四种匹配模式 / 引擎 spec 车道 / 持久化回环。"""

from __future__ import annotations

import time
import unittest

from agent.ingestion.config import IngestTuning
from agent.ingestion.engine import StreamDigestEngine
from agent.ingestion.source_spec import NORMAL_RULE_MODES, canon_value, parse_source_spec


def _tuning(**overrides) -> IngestTuning:
    # 聚焦 spec 车道分诊行为,显式关正常量直通(直通专测在 test_ingestion_full_read.py)。
    base = {"value_min_support": 8, "low_cardinality_limit": 8, "full_read_per_pull": 0}
    base.update(overrides)
    return IngestTuning(**base)


def _catch_resp(i: int) -> str:
    """monitoring_catch 难判流结果端:真事=得逞回显、正常业务=无得逞记号、防御成功=含常态记号。"""
    if i % 50 == 0:
        return "200 OK body: root:x:0:0:root:/root:/bin/bash"  # 真事(得逞回显)
    if i % 3 == 0:
        return f'200 OK body:{{"ok":true,"n":{i}}}'  # 正常业务(既无常态也无得逞记号)
    return "401 bad password"  # 防御成功(含常态记号)


class TestSpecParse(unittest.TestCase):
    def test_parse_full_spec_round_trips(self):
        spec = parse_source_spec(
            {
                "result_field": "a.b",
                "target_values": ["bad", False, 3],
                "normal_values": ["ok"],
                "normal_value_contains": ["fine"],
                "ignore_fields": ["noise1", "noise2"],
                "max_per_pull": 5,
            }
        )
        payload = spec.to_payload()
        self.assertEqual(payload["result_field"], "a.b")
        # 非字符串标量按 canon 规范化(布尔/数字),两边可比
        self.assertEqual(sorted(payload["target_values"]), ["3", "bad", "false"])
        self.assertEqual(payload["max_per_pull"], 5)
        again = parse_source_spec(payload)
        self.assertEqual(again, spec)

    def test_rejects_bad_shapes(self):
        bad_cases = [
            "not a dict",
            {},  # 空 spec
            {"result_field": "a.b"},  # 有字段没判据
            {"target_values": ["x"]},  # 有判据没字段
            {"result_field": "a", "target_values": "x"},  # 非数组
            {"result_field": "a", "target_values": [""]},  # 空值
            {"result_field": "a", "target_values": [{"deep": 1}]},  # 非标量
            {"result_field": "a", "target_values": ["x"], "ignore_fields": ["a"]},  # 字段被忽略
        ]
        for raw in bad_cases:
            with self.assertRaises(ValueError, msg=repr(raw)):
                parse_source_spec(raw)

    def test_ignore_only_spec_allowed(self):
        spec = parse_source_spec({"ignore_fields": ["n1"]})
        self.assertIsNone(spec.match([("n1", "x"), ("other", "y")]))


class TestSpecMatch(unittest.TestCase):
    def test_match_modes_and_order(self):
        spec = parse_source_spec(
            {
                "result_field": "r",
                "target_values": ["boom"],
                "target_value_contains": ["bad-"],
                "normal_values": ["ok"],
                "normal_value_contains": ["fine"],
            }
        )
        self.assertEqual(spec.match([("r", "boom")]).mode, "target_value")
        self.assertEqual(spec.match([("r", "bad-123abc")]).mode, "target_contains")
        self.assertIsNone(spec.match([("r", "ok")]))  # 常态精确
        self.assertIsNone(spec.match([("r", "fine ref=9啊")]))  # 常态记号
        self.assertEqual(spec.match([("r", "weird")]).mode, "outside_normal")
        self.assertIsNone(spec.match([("other", "boom")]))  # 只看 result_field

    def test_multivalue_target_after_normal_not_shadowed(self):
        # 数组叶子:同一 result_field 压平出多个取值,真目标排在常态取值之后时,不能被
        # 常态短路漏掉(classify 抬升类优先、扫到即返回;常态仅全程无抬升命中时才兜底记账)。
        spec = parse_source_spec(
            {
                "result_field": "items[].r",
                "target_value_contains": ["boom"],
                "normal_value_contains": ["w"],
            }
        )
        normal_then_target = [("items[].r", "w9999 ok"), ("items[].r", "boom breach detected")]
        hit = spec.match(normal_then_target)
        self.assertIsNotNone(hit, "常态在前、真目标在后:目标必须仍被抬升,不被短路漏报")
        self.assertEqual(hit.mode, "target_contains")
        self.assertEqual(spec.classify(normal_then_target).mode, "target_contains")
        # 全是常态取值 → classify 报常态记账(供引擎回落 _count_rule_hit),match 映射为不抬。
        self.assertIn(spec.classify([("items[].r", "w1"), ("items[].r", "w2")]).mode, NORMAL_RULE_MODES)
        self.assertIsNone(spec.match([("items[].r", "w1"), ("items[].r", "w2")]))

    def test_canon_bridges_scalars(self):
        spec = parse_source_spec({"result_field": "flag", "target_values": ["true"]})
        self.assertIsNotNone(spec.match([("flag", True)]))
        self.assertIsNone(spec.match([("flag", False)]))
        self.assertEqual(canon_value(None), "null")
        self.assertEqual(canon_value(3.0), "3")

    def test_bare_normal_values_match_text_field_first_token(self):
        # §4 真机洪泛修复:模型给裸词常态集,结果端是"结论词+高基数尾巴"整句文本。
        # 首记号兜底:常态句(首记号∈集合)判常态、目标句(首记号∉集合)判候选;整串精确仍优先。
        spec = parse_source_spec({"result_field": "log", "normal_values": ["accepted", "returned"]})
        self.assertIsNone(spec.match([("log", "accepted ref=deadbeef t=7")]), "常态句首记号命中→不抬")
        self.assertIsNone(spec.match([("log", "returned ref=99 t=8")]))
        hit = spec.match([("log", "diverted ref=abc123 t=9")])
        self.assertIsNotNone(hit, "目标句首记号非常态→抬候选")
        self.assertEqual(hit.mode, "outside_normal")
        # 裸词/枚举字段行为不变(canon 本身即裸词)。
        self.assertIsNone(spec.match([("log", "accepted")]))
        self.assertIsNotNone(spec.match([("log", "diverted")]))

    def test_bare_target_values_match_text_field_first_token(self):
        spec = parse_source_spec({"result_field": "log", "target_values": ["diverted"]})
        hit = spec.match([("log", "diverted ref=abc t=1")])
        self.assertIsNotNone(hit)
        self.assertEqual(hit.mode, "target_value")
        self.assertIsNone(spec.match([("log", "accepted ref=abc t=2")]))

    def test_target_contains_wins_over_neutral_normal_token(self):
        # monitoring_catch 同源:结果端文本同时带【目标得逞记号】和【中性遥测前缀】时,
        # target 命中优先于 normal——带 telemetry 的真事(命令回显 uid=0)不被 normal 误压。
        # 真机实锤:模型把中性词 telemetry 配进 normal,19 个带 telemetry 的真 RCE 被整类压掉;
        # 只要目标得逞记号也配进 target_value_contains,target 优先即可救回。
        spec = parse_source_spec(
            {
                "result_field": "response",
                "target_value_contains": ["uid=0", "root:x:0:0"],
                "normal_value_contains": ["telemetry", "bad password"],
            }
        )
        hit = spec.match([("response", "200 OK [telemetry] child proc -> uid=0(root) (gadget fired)")])
        self.assertIsNotNone(hit, "带 telemetry 的目标得逞记号必须先命中 target,不被 normal 压")
        self.assertEqual(hit.mode, "target_contains")
        # 纯中性/防御记号(无得逞记号)→ 常态规则命中,不抬
        self.assertIsNone(spec.match([("response", "401 [telemetry] bad password")]))


class TestEngineSpecLane(unittest.TestCase):
    def _events(self, count, result="ok", noise_seed=0):
        # 高基数噪声字段 junk 专门淹签名;result 字段是判定端
        return [
            (i, {"seq": i, "junk": f"j{noise_seed}-{i}", "result": result})
            for i in range(count)
        ]

    def test_spec_lane_escalates_target_hidden_from_generic_lanes(self):
        engine = StreamDigestEngine(_tuning())
        now = time.time()
        engine.process(self._events(200), now)
        engine.apply_spec(
            parse_source_spec({"result_field": "result", "normal_values": ["ok"], "ignore_fields": ["junk"]})
        )
        events = self._events(150, noise_seed=1)
        events[80] = (1080, {"seq": 1080, "junk": "j-x", "result": "bad"})
        digest = engine.process(events, now + 5)
        spec_hits = [c for c in digest.candidates if c.reason == "spec_target_value"]
        self.assertEqual([c.seq_hint for c in spec_hits], [1080])
        self.assertEqual(spec_hits[0].value_token, "bad")
        self.assertEqual(spec_hits[0].spec_mode, "outside_normal")
        self.assertEqual(engine.totals["escalated_spec_target"], 1)

    def test_normal_contains_catches_unseen_target_in_messagey_field(self):
        engine = StreamDigestEngine(_tuning())
        engine.apply_spec(
            parse_source_spec({"result_field": "note", "normal_value_contains": ["pass", "rework"]})
        )
        now = time.time()
        events = [(i, {"seq": i, "note": f"pass ref={i:08d}"}) for i in range(120)]
        events[60] = (60, {"seq": 60, "note": "defect ref=00abc999"})
        digest = engine.process(events, now)
        spec_hits = [c for c in digest.candidates if c.reason == "spec_target_value"]
        self.assertEqual([c.seq_hint for c in spec_hits], [60])

    def test_spec_lane_has_own_quota_and_overflow(self):
        engine = StreamDigestEngine(_tuning(spec_max_candidates_per_pull=2))
        engine.apply_spec(parse_source_spec({"result_field": "r", "target_values": ["hit"]}))
        events = [(i, {"seq": i, "r": "hit" if i < 5 else "ok"}) for i in range(50)]
        digest = engine.process(events, time.time())
        spec_hits = [c for c in digest.candidates if c.reason == "spec_target_value"]
        self.assertEqual(len(spec_hits), 2)
        overflow_pos = {record.seq_hint for record in digest.overflow}
        self.assertTrue({2, 3, 4} & overflow_pos)  # 溢出带坐标,不静默丢

    def test_spec_max_per_pull_override_wins(self):
        engine = StreamDigestEngine(_tuning(spec_max_candidates_per_pull=2))
        engine.apply_spec(
            parse_source_spec({"result_field": "r", "target_values": ["hit"], "max_per_pull": 4})
        )
        events = [(i, {"seq": i, "r": "hit" if i < 6 else "ok"}) for i in range(40)]
        digest = engine.process(events, time.time())
        self.assertEqual(len([c for c in digest.candidates if c.reason == "spec_target_value"]), 4)

    def test_apply_spec_resets_shape_stats_and_reruns_prepass(self):
        engine = StreamDigestEngine(_tuning())
        engine.process(self._events(200), time.time())
        seen_before = engine.totals["events_seen"]
        engine.apply_spec(parse_source_spec({"ignore_fields": ["junk"]}))
        self.assertEqual(engine.totals["events_seen"], seen_before)  # 累计账保留
        # 签名滑窗/census 重置(旧签名与新字段集不可比);字段画像【保留】——按路径记账、
        # 与 spec 字段集无关,重置会给 configure 制造第二个冷启动放行窗(真机 18 条误报)。
        self.assertEqual(engine.counter.census(time.time()), {})
        self.assertGreater(len(engine.profiles.snapshot()), 0)
        digest = engine.process(self._events(100, noise_seed=2), time.time())
        self.assertEqual(digest.seen, 100)

    def test_high_cardinality_text_target_beats_normal_flood(self):
        # monitoring_catch 根因/修法(机制层锁定):高基数文本结果端(响应正文每条不同),
        # 学常态(normal_value_contains 盯"常态之外")→ 正常业务响应不含常态记号也被 outside_normal
        # 洪泛抬升,把判读淹掉、真目标沉底;学得逞记号(target_value_contains)→ 只精准抬命中得逞
        # 记号的(≈真事量级)。同一条流,学常态的 spec 抬升量必远多于学得逞记号的精准抬。
        events = [(i, {"seq": i, "response": _catch_resp(i)}) for i in range(300)]

        def spec_hits(spec_dict):
            eng = StreamDigestEngine(_tuning(spec_max_candidates_per_pull=200))
            eng.apply_spec(parse_source_spec(spec_dict))
            eng.process(events, time.time())
            return eng.totals["escalated_spec_target"]

        normal_hits = spec_hits({"result_field": "response", "normal_value_contains": ["bad password"]})
        target_hits = spec_hits(
            {"result_field": "response", "target_value_contains": ["root:x:0:0", "uid=0"]}
        )
        self.assertGreaterEqual(target_hits, 6, "6 个真事全被得逞记号精准抬")
        self.assertGreater(
            normal_hits, 5 * target_hits,
            "学常态在高基数文本上必然 outside_normal 洪泛(正常业务也抬),远多于学得逞记号的精准抬",
        )


class TestEngineIgnoreFields(unittest.TestCase):
    def test_ignored_noise_restores_shape_lane_signal(self):
        """同结构事件只在忽略噪声后共签名:形状车道恢复"常见形状被压、稀有形状上浮"。"""
        tuning = _tuning()
        noisy = StreamDigestEngine(tuning)
        filtered = StreamDigestEngine(tuning)
        filtered.apply_spec(parse_source_spec({"ignore_fields": ["junk"]}))
        now = time.time()
        events = [(i, {"seq": i, "junk": f"u-{i}", "kind": "same"}) for i in range(300)]
        digest_noisy = noisy.process(events, now)
        digest_filtered = filtered.process(events, now)
        # 滤噪后:300 条同构事件折成 1 个组,被压计数显著高于带噪声时
        self.assertLessEqual(len(digest_filtered.groups), len(digest_noisy.groups))
        self.assertGreater(digest_filtered.suppressed_total, 250)


if __name__ == "__main__":
    unittest.main()
