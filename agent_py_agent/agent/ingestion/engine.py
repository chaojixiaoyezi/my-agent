"""摄取引擎:一批原始事件 → 候选批 + 被压组账目。只用结构化信号(签名稀有度),不做定性。

候选选择是两阶段:先把窗口计数 <= 阈值的全部收进备选,call 结束按
(窗口计数升序, 全时计数升序, 序号升序) 排序取前 N——保证"最稀有的先上",
不会被同 call 先到的次稀有事件挤出;溢出的带坐标进账目,不静默丢。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import IngestTuning
from .field_profile import ProfileTable
from .flatten import flatten_event
from .signature import classed_pairs, signature_of, sketch_of, token_pairs_of
from .window_counter import SlidingWindowCounter

_CENSUS_CAP = 50000
_SNAPSHOT_CENSUS_CAP = 20000
_COLD_START_PREPASS_MIN = 64


@dataclass
class Candidate:
    seq_hint: int
    event: dict
    signature: str
    window_count: int
    first_seen: bool
    all_time_count: int = 1


@dataclass
class OverflowRecord:
    seq_hint: int
    signature: str
    window_count: int


@dataclass
class GroupDigest:
    signature: str
    sketch: dict[str, str]
    window_count: int
    call_count: int
    exemplar_seq: int
    exemplar: dict


@dataclass
class CallDigest:
    seen: int = 0
    candidates: list[Candidate] = field(default_factory=list)
    overflow: list[OverflowRecord] = field(default_factory=list)
    groups: list[GroupDigest] = field(default_factory=list)
    groups_total: int = 0
    suppressed_total: int = 0


class StreamDigestEngine:
    """跨 pull 持久的降维引擎:字段画像 + 签名滑窗计数 + 稀有度分诊。"""

    def __init__(self, tuning: IngestTuning) -> None:
        self.tuning = tuning
        self.profiles = ProfileTable(tuning.low_cardinality_limit)
        self.counter = SlidingWindowCounter(tuning.window_seconds, tuning.bucket_seconds)
        self.totals: dict[str, int] = {"events_seen": 0, "escalated": 0, "suppressed": 0, "overflow": 0}
        self._census: dict[str, int] = {}

    def process(self, events: list[tuple[int, dict]], now: float) -> CallDigest:
        """按到达顺序处理一批 (seq_hint, event);两阶段选出候选。

        冷启动首批先做"预热遍"(只喂字段画像、不取签名),让基数/单调性分类先收敛——
        否则首批积压里前几十个事件会以未收敛的字面签名霸占候选位,挤掉真正稀有的。
        """
        prewarmed = self._cold_start_prepass(events)
        digest = CallDigest()
        qualifying: list[Candidate] = []
        groups: dict[str, GroupDigest] = {}
        for index, (seq_hint, event) in enumerate(events):
            digest.seen += 1
            self.totals["events_seen"] += 1
            pairs = prewarmed[index] if prewarmed is not None else classed_pairs(event, self.profiles)
            self._classify_one(qualifying, groups, (seq_hint, event, pairs), now)
        self._select_candidates(digest, qualifying)
        digest.groups_total = len(groups)
        digest.suppressed_total = sum(group.call_count for group in groups.values())
        digest.groups = sorted(groups.values(), key=lambda g: (-g.window_count, g.signature))[
            : self.tuning.max_suppressed_groups_listed
        ]
        return digest

    def _cold_start_prepass(self, events: list[tuple[int, dict]]) -> list[tuple[tuple[str, str], ...]] | None:
        if self.totals["events_seen"] > 0 or len(events) < _COLD_START_PREPASS_MIN:
            return None
        flats = [flatten_event(event) for _seq, event in events]
        for flat in flats:
            for path, value in flat:
                self.profiles.observe_only(path, value)
        return [token_pairs_of(flat, self.profiles) for flat in flats]

    def _classify_one(
        self,
        qualifying: list[Candidate],
        groups: dict[str, GroupDigest],
        item: tuple[int, dict, tuple[tuple[str, str], ...]],
        now: float,
    ) -> None:
        seq_hint, event, pairs = item
        signature = signature_of(pairs)
        window_count = self.counter.observe(signature, now)
        all_time = self._bump_census(signature)
        if window_count <= self.tuning.rare_threshold:
            qualifying.append(Candidate(seq_hint, event, signature, window_count, all_time == 1, all_time))
            return
        self._suppress(groups, (signature, pairs, window_count), (seq_hint, event))

    def _select_candidates(self, digest: CallDigest, qualifying: list[Candidate]) -> None:
        ranked = sorted(qualifying, key=lambda c: (c.window_count, c.all_time_count, c.seq_hint))
        keep = self.tuning.max_candidates_per_pull
        digest.candidates = sorted(ranked[:keep], key=lambda c: c.seq_hint)
        digest.overflow = [
            OverflowRecord(c.seq_hint, c.signature, c.window_count)
            for c in sorted(ranked[keep:], key=lambda c: c.seq_hint)
        ]
        self.totals["escalated"] += len(digest.candidates)
        self.totals["overflow"] += len(digest.overflow)

    def _suppress(
        self,
        groups: dict[str, GroupDigest],
        keyed: tuple[str, tuple[tuple[str, str], ...], int],
        item: tuple[int, dict],
    ) -> None:
        signature, pairs, window_count = keyed
        seq_hint, event = item
        self.totals["suppressed"] += 1
        group = groups.get(signature)
        if group is None:
            groups[signature] = GroupDigest(signature, sketch_of(pairs), window_count, 1, seq_hint, event)
            return
        group.call_count += 1
        group.window_count = window_count

    def _bump_census(self, signature: str) -> int:
        current = self._census.get(signature)
        if current is not None:
            self._census[signature] = current + 1
            return current + 1
        if len(self._census) >= _CENSUS_CAP:
            return 1
        self._census[signature] = 1
        return 1

    def snapshot(self, now: float) -> dict[str, Any]:
        ranked = sorted(self._census.items(), key=lambda kv: (-kv[1], kv[0]))[:_SNAPSHOT_CENSUS_CAP]
        return {
            "profiles": self.profiles.snapshot(),
            "window": self.counter.snapshot(now),
            "totals": dict(self.totals),
            "census": dict(ranked),
        }

    def restore(self, payload: dict[str, Any], now: float) -> None:
        self.profiles.restore(dict(payload.get("profiles") or {}))
        self.counter.restore(dict(payload.get("window") or {}), now)
        for key, value in dict(payload.get("totals") or {}).items():
            if key in self.totals:
                self.totals[key] = int(value)
        for signature, count in dict(payload.get("census") or {}).items():
            if len(self._census) >= _CENSUS_CAP:
                break
            self._census[str(signature)] = int(count)


__all__ = ["CallDigest", "Candidate", "GroupDigest", "OverflowRecord", "StreamDigestEngine"]
