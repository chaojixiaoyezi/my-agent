"""摄取引擎:一批原始事件 → 候选批 + 被压组账目。只用结构化信号,不做定性。

候选选择是两阶段:先把达标事件全部收进备选,call 结束按
(窗口计数升序, 全时计数升序, 序号升序) 排序取前 N——保证"最稀有的先上",
不会被同 call 先到的次稀有事件挤出;溢出的带坐标进账目,不静默丢。

达标有两条互补通道(都是纯结构化计数):
1. 签名稀有:整事件结构签名在窗口内计数 <= rare_threshold(稀有形状)。
2. 少数派取值:某个字面取值字段的 (路径,取值) 在窗口内计数 <= value_rare_threshold,
   且该字段窗口样本量 >= value_min_support(测试方独立复测实锤:真目标与诱饵结构
   完全相同、只差结果端一个取值——如高基数字段折叠后"成功登录"共签名,真目标混在
   常见形状里被当"不稀有"压掉,30 个真目标只 7 个进候选;取值粒度的窗口稀有度
   把"少数派取值"也抬上来,仍零自然语言判断)。
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
    # 达标通道:structurally_rare_signature(签名稀有) / minority_field_value(少数派取值)。
    reason: str = "structurally_rare_signature"
    # 少数派取值通道的结构化依据(reason=minority_field_value 时有值):
    # 触发字段路径、取值记号、该 (路径,取值) 的窗口计数、该字段窗口样本量。
    value_path: str = ""
    value_token: str = ""
    value_window_count: int = 0
    field_window_count: int = 0

    @property
    def rank_count(self) -> int:
        """排序用的等效稀有度:取值通道按取值窗口计数排(签名计数可能很大)。"""
        return self.value_window_count if self.reason == "minority_field_value" else self.window_count


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
        # 少数派取值通道:字段样本量与 (字段,取值) 对分别计数,同一滑动窗口语义。
        self.value_counter = SlidingWindowCounter(tuning.window_seconds, tuning.bucket_seconds)
        self.totals: dict[str, int] = {
            "events_seen": 0,
            "escalated": 0,
            "suppressed": 0,
            "overflow": 0,
            "escalated_minority_value": 0,
        }
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
        minority = self._observe_values(pairs, now)
        # 少数派取值优先归取值车道:该证据更具体,且其车道量天生有界;若归入形状车道,
        # 会和成群的稀有形状诱饵挤同一个名额池(计数全 1 平手按序号),重蹈被挤出的算术。
        if minority is not None:
            path, token, value_count, field_count = minority
            qualifying.append(
                Candidate(
                    seq_hint, event, signature, window_count, all_time == 1, all_time,
                    reason="minority_field_value",
                    value_path=path, value_token=token,
                    value_window_count=value_count, field_window_count=field_count,
                )
            )
            return
        if window_count <= self.tuning.rare_threshold:
            qualifying.append(Candidate(seq_hint, event, signature, window_count, all_time == 1, all_time))
            return
        self._suppress(groups, (signature, pairs, window_count), (seq_hint, event))

    def _observe_values(
        self, pairs: tuple[tuple[str, str], ...], now: float
    ) -> tuple[str, str, int, int] | None:
        """给每个字面取值字段记 (字段样本量, (字段,取值)) 窗口账,返回最稀的少数派取值。

        真目标与诱饵结构相同、只差结果端一个取值时,整事件签名不稀有,但那个取值本身
        在窗口内极少——按 (路径,取值) 窗口计数 <= value_rare_threshold 抬为候选。
        字段样本量 >= value_min_support 才有"少数派"可言(冷启动/稀疏字段不硬判)。
        只看字面记号(布尔/低基数字面/None):高基数折叠(s:*)、单调数(n:mono)、
        数量级桶(n:eX)不是"取值",不参与。纯计数,零语义。
        """
        threshold = self.tuning.value_rare_threshold
        if threshold <= 0:
            return None
        best: tuple[str, str, int, int] | None = None
        for path, token in pairs:
            if not _is_literal_value_token(token):
                continue
            field_count = self.value_counter.observe(f"f\x1e{path}", now)
            value_count = self.value_counter.observe(f"v\x1e{path}\x1e{token}", now)
            if field_count < self.tuning.value_min_support or value_count > threshold:
                continue
            if best is None or value_count < best[2]:
                best = (path, token, value_count, field_count)
        return best

    def _select_candidates(self, digest: CallDigest, qualifying: list[Candidate]) -> None:
        """两车道选拔:稀有形状与少数派取值各占各的名额,互不挤占。

        单一名额池会重蹈测试方实锤的挤出:诱饵天生是"触发端像目标"的稀有形状,
        每批达标者成群(计数全 1 平手按序号),真目标(少数派取值)混在一个池里
        排队就会被挤进 overflow——7307 个候选里只 7/30 真目标的算术根源。
        取值车道的量天生有界((路径,取值) 窗口计数 <= 阈值),独立名额不会泛滥。"""
        shape_lane = [c for c in qualifying if c.reason != "minority_field_value"]
        value_lane = [c for c in qualifying if c.reason == "minority_field_value"]
        kept_shape, spill_shape = self._rank_lane(shape_lane, self.tuning.max_candidates_per_pull)
        kept_value, spill_value = self._rank_lane(value_lane, self.tuning.value_max_candidates_per_pull)
        digest.candidates = sorted(kept_shape + kept_value, key=lambda c: c.seq_hint)
        digest.overflow = [
            OverflowRecord(c.seq_hint, c.signature, c.rank_count)
            for c in sorted(spill_shape + spill_value, key=lambda c: c.seq_hint)
        ]
        self.totals["escalated"] += len(digest.candidates)
        self.totals["escalated_minority_value"] += len(kept_value)
        self.totals["overflow"] += len(digest.overflow)

    @staticmethod
    def _rank_lane(lane: list[Candidate], keep: int) -> tuple[list[Candidate], list[Candidate]]:
        ranked = sorted(lane, key=lambda c: (c.rank_count, c.all_time_count, c.seq_hint))
        return ranked[: max(0, keep)], ranked[max(0, keep):]

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
            "value_window": self.value_counter.snapshot(now),
            "totals": dict(self.totals),
            "census": dict(ranked),
        }

    def restore(self, payload: dict[str, Any], now: float) -> None:
        self.profiles.restore(dict(payload.get("profiles") or {}))
        self.counter.restore(dict(payload.get("window") or {}), now)
        self.value_counter.restore(dict(payload.get("value_window") or {}), now)
        for key, value in dict(payload.get("totals") or {}).items():
            if key in self.totals:
                self.totals[key] = int(value)
        for signature, count in dict(payload.get("census") or {}).items():
            if len(self._census) >= _CENSUS_CAP:
                break
            self._census[str(signature)] = int(count)


def _is_literal_value_token(token: str) -> bool:
    """字面取值记号才参与少数派统计:b:T/b:F、非折叠字面(s:xxx/n:123)、null。
    折叠/归并记号(s:*、n:mono、n:eX 数量级桶、t:类型)不代表具体取值。"""
    if token in ("s:*", "n:mono"):
        return False
    if token.startswith("t:"):
        return False
    if token.startswith("n:e") and token[3:].lstrip("-").isdigit():
        return False
    return True


__all__ = ["CallDigest", "Candidate", "GroupDigest", "OverflowRecord", "StreamDigestEngine"]
