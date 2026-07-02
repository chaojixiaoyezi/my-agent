"""钉子测试:用真正式测试台(multi_source_simulator)的 5 种异构 schema 离线回放摄取引擎。

验的就是 T1 的验收本体:
  1) 每条真命中(结果端才可判)都被结构化稀有度抬成候选——引擎不看语义也不漏真目标;
  2) 压缩比:候选远小于事件总量(LLM 只需按批研判);
  3) 换词不变性:把所有字符串值做双射改名后,分诊决策(按事件位置)完全一致——
     证明本层没有任何自然语言/关键词定性(铁律钉子)。
"""

from __future__ import annotations

import importlib.util
import random
from pathlib import Path

import pytest

from agent.ingestion.config import IngestTuning
from agent.ingestion.engine import StreamDigestEngine

_SIMULATOR = Path(__file__).resolve().parents[2] / "scripts" / "watch_harness" / "multi_source_simulator.py"


@pytest.fixture(scope="module")
def simulator_module():
    spec = importlib.util.spec_from_file_location("multi_source_simulator", _SIMULATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _event_kind(position: int, hits: set[int], rng: random.Random) -> str:
    if position in hits:
        return "hit"
    return "confuser" if rng.random() < 0.015 else "noise"


def _generate_stream(sim, source_index: int, plan: tuple[int, set[int]], tmp_path: Path):
    """直接驱动模拟器的 SourceState 生成一路事件流(混 1.5% 迷惑 + 稀疏命中)。plan=(总数, 命中位置)。

    给模拟器打"假墙钟"(每事件 +10ms = 100 条/秒):紧循环生成会让时间戳字段几乎不动、
    以不真实的低瞬时基数骗过画像;真实流速下该字段毫秒级翻新、很快按高基数收敛。
    """
    total, hit_positions = plan
    spec = sim._build_schemas()[source_index]
    source = sim.SourceState(source_index, spec, seed=777 + source_index)
    key_path = str(tmp_path / f"key-{source_index}.jsonl")
    rng = random.Random(42 + source_index)
    clock = {"now": 1_780_000_000.0 + source_index}
    original_time = sim.time.time
    sim.time.time = lambda: clock["now"]
    try:
        for position in range(total):
            clock["now"] += 0.01
            source.append(_event_kind(position, hit_positions, rng), key_path)
    finally:
        sim.time.time = original_time
    return list(source.ring), spec


def _replay(engine: StreamDigestEngine, events: list[dict], chunk: int, base_now: float):
    """按 pull 批次回放(每批推进虚拟时钟),汇总所有候选与账目。"""
    candidates, overflow, suppressed = [], [], 0
    for offset in range(0, len(events), chunk):
        batch = [(offset + i, event) for i, event in enumerate(events[offset : offset + chunk])]
        digest = engine.process(batch, now=base_now + (offset / chunk) * 3.0)
        candidates.extend(digest.candidates)
        overflow.extend(digest.overflow)
        suppressed += digest.suppressed_total
    return candidates, overflow, suppressed


@pytest.mark.parametrize("source_index", [0, 1, 2, 3, 4])
def test_every_true_hit_surfaces_as_candidate(simulator_module, tmp_path, source_index):
    # 命中间距按正式测试台口径(22 hits/1260s 轮转 5 源 → 每源 ~4-5 分钟一条)。
    total = 36000
    hit_positions = {2000, 12000, 22000, 33000}
    events, spec = _generate_stream(simulator_module, source_index, (total, hit_positions), tmp_path)
    engine = StreamDigestEngine(IngestTuning())
    candidates, overflow, suppressed = _replay(engine, events, chunk=300, base_now=100000.0)

    hit_ids = {event[spec.id_field] for i, event in enumerate(events) if i in hit_positions}
    surfaced_ids = {candidate.event.get(spec.id_field) for candidate in candidates}
    assert hit_ids <= surfaced_ids, f"真命中没全部浮出: 缺 {hit_ids - surfaced_ids}"

    # 压缩比:给模型看的候选必须远小于事件总量,LLM 才喝得动;溢出有账不静默。
    assert len(candidates) < total * 0.03, f"压缩比不足: {len(candidates)}/{total}"
    assert suppressed > total * 0.9


def test_value_renaming_invariance_proves_no_keyword_logic(simulator_module, tmp_path):
    """铁律钉子:把全部字符串值双射改名(语义全毁),分诊决策按位置必须一致。"""
    total = 3000
    hit_positions = {800, 2100}
    events, _spec = _generate_stream(simulator_module, 0, (total, hit_positions), tmp_path)

    def rename(value):
        if isinstance(value, str):
            return "".join(chr(0x2500 + (ord(ch) % 64)) for ch in value)
        if isinstance(value, dict):
            return {key: rename(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rename(item) for item in value]
        return value

    renamed = [{key: rename(item) for key, item in event.items()} for event in events]

    plain_engine = StreamDigestEngine(IngestTuning())
    renamed_engine = StreamDigestEngine(IngestTuning())
    plain_candidates, plain_overflow, _ = _replay(plain_engine, events, chunk=250, base_now=200000.0)
    renamed_candidates, renamed_overflow, _ = _replay(renamed_engine, renamed, chunk=250, base_now=200000.0)

    plain_positions = sorted(c.seq_hint for c in plain_candidates) + sorted(o.seq_hint for o in plain_overflow)
    renamed_positions = sorted(c.seq_hint for c in renamed_candidates) + sorted(o.seq_hint for o in renamed_overflow)
    assert plain_positions == renamed_positions
