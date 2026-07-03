"""per-源判据 spec:解析校验 / 四种匹配模式 / 引擎 spec 车道 / 持久化回环。"""

from __future__ import annotations

import time
import unittest

from agent.ingestion.config import IngestTuning
from agent.ingestion.engine import StreamDigestEngine
from agent.ingestion.source_spec import canon_value, parse_source_spec


def _tuning(**overrides) -> IngestTuning:
    base = {"value_min_support": 8, "low_cardinality_limit": 8}
    base.update(overrides)
    return IngestTuning(**base)


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
        self.assertEqual(engine.profiles.snapshot(), {})  # 画像重置
        digest = engine.process(self._events(100, noise_seed=2), time.time())
        self.assertEqual(digest.seen, 100)


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
