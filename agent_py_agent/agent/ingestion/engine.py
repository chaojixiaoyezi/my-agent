"""摄取引擎:一批原始事件 → 候选批 + 被压组账目。只用结构化信号,不做定性。

候选选择是两阶段:先把达标事件全部收进备选,call 结束按
(窗口计数升序, 全时计数升序, 序号升序) 排序取前 N——保证"最稀有的先上",
不会被同 call 先到的次稀有事件挤出;溢出的带坐标进账目,不静默丢。

达标有三条互补通道(都是纯结构化匹配/计数):
1. per-源判据 spec(若已配,learn→configure→monitor 的"配"):结果端字段取值命中
   spec 的目标集合/子串/常态之外 → 直接抬候选。判据由模型从样本学出(理解在模型),
   这里只做字面比对(执行在代码)。真实日志又花又杂(高基数噪声淹信号、结果端是
   文本消息)时,通用稀有度会瞎,只有配了判据才盯得准。
2. 签名稀有:整事件结构签名在窗口内计数 <= rare_threshold(稀有形状)。兜底保留。
3. 少数派取值:某个字面取值字段的 (路径,取值) 在窗口内计数 <= value_rare_threshold,
   且该字段窗口样本量 >= value_min_support(测试方独立复测实锤:真目标与诱饵结构
   完全相同、只差结果端一个取值——如高基数字段折叠后"成功登录"共签名,真目标混在
   常见形状里被当"不稀有"压掉,30 个真目标只 7 个进候选;取值粒度的窗口稀有度
   把"少数派取值"也抬上来,仍零自然语言判断)。兜底保留。

spec 的 ignore_fields 在压平后立即滤掉(不进签名/取值统计):高基数噪声字段不再
把"结果端那一栏"的信号淹掉,通用兜底车道在花数据上也恢复可用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import IngestTuning
from .field_profile import TOKEN_HIGH_CARD_TEXT, ProfileTable
from .flatten import flatten_event
from .signature import observed_pairs, signature_of, sketch_of, token_pairs_of
from .source_spec import SourceSpec
from .text_tokens import head_token
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
    # 达标通道:spec_target_value(per-源判据命中) / structurally_rare_signature(签名稀有)
    # / minority_field_value(少数派取值)。
    reason: str = "structurally_rare_signature"
    # 取值类通道的结构化依据:少数派通道存触发字段/取值记号/窗口计数;
    # spec 通道存命中字段/取值与匹配模式(value_token 复用为取值,spec_mode 存模式),
    # 窗口计数同样回填——"该取值窗口内出现几次"是模型逐条重判的关键证据(判据可能配错:
    # 真机实锤模型把常态取值配成 target,频次证据能让重判环把它挡掉)。
    value_path: str = ""
    value_token: str = ""
    value_window_count: int = 0
    field_window_count: int = 0
    spec_mode: str = ""
    # 少数派取值的子车道:""=字面取值,"head"=首记号(文本结果端),名额互不挤占。
    value_lane: str = ""

    @property
    def rank_count(self) -> int:
        """排序用的等效稀有度:取值类通道(spec 命中/少数派)按取值窗口计数升序——
        spec 命中不再恒最优:判据配错为常态时其命中量大、计数高,排到车道尾部,
        真正稀有的命中(计数低/证据缺失=0)先上。"""
        if self.reason in ("spec_target_value", "minority_field_value"):
            return self.value_window_count
        return self.window_count


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

    def __init__(self, tuning: IngestTuning, spec: SourceSpec | None = None) -> None:
        self.tuning = tuning
        self.spec = spec
        self.profiles = ProfileTable(tuning.low_cardinality_limit)
        self.counter = SlidingWindowCounter(tuning.window_seconds, tuning.bucket_seconds)
        # 少数派取值通道:字段样本量与 (字段,取值) 对分别计数,同一滑动窗口语义。
        self.value_counter = SlidingWindowCounter(tuning.window_seconds, tuning.bucket_seconds)
        # 首记号画像(§4 文本结果端召回):高基数文本字段按分隔符取首记号,先看首记号
        # 分布本身是否低基数(是→参与少数派频次;否→该字段此路永久关闸,防 trace/id 类爆炸)。
        # 基数上限独立于一般字段(结论词集合天然更宽,沿用 24 会被偶发杂词永久关闸)。
        self.head_profiles = ProfileTable(tuning.head_low_cardinality_limit)
        self.totals: dict[str, int] = {
            "events_seen": 0,
            "escalated": 0,
            "suppressed": 0,
            "overflow": 0,
            "escalated_minority_value": 0,
            "escalated_head_value": 0,
            "escalated_spec_target": 0,
        }
        self._census: dict[str, int] = {}
        self._first_call_done = False

    def apply_spec(self, spec: SourceSpec | None) -> None:
        """配/换 per-源判据并重置签名滑窗/census(ignore_fields 改变字段集,旧签名不再
        可比,留着会把新签名全判成"首见稀有");累计账 totals 保留,下一批重走预热遍。
        【字段画像/取值计数器/首记号画像保留】:三者都按字段路径记账,与 spec 字段集无关;
        重置会给 configure 制造第二个冷启动窗口(画像回到字面分类、支持度重新攒)——
        真机实锤:该窗口内 outside_normal 免疫与少数派闸全部"无证据不拦",漏列常态在此
        集中放行(20 条误报里 18 条来自这里)。被 ignore 的字段不再进 flat,画像不再更新、
        旧计数随窗口滑动自然过期。"""
        self.spec = spec
        self.counter = SlidingWindowCounter(self.tuning.window_seconds, self.tuning.bucket_seconds)
        self._census = {}
        self._first_call_done = False

    def process(self, events: list[tuple[int, dict]], now: float) -> CallDigest:
        """按到达顺序处理一批 (seq_hint, event);两阶段选出候选。

        冷启动首批先做"预热遍"(只喂字段画像、不取签名),让基数/单调性分类先收敛——
        否则首批积压里前几十个事件会以未收敛的字面签名霸占候选位,挤掉真正稀有的。
        """
        prewarmed = self._cold_start_prepass(events)
        self._first_call_done = True
        digest = CallDigest()
        qualifying: list[Candidate] = []
        groups: dict[str, GroupDigest] = {}
        for index, (seq_hint, event) in enumerate(events):
            digest.seen += 1
            self.totals["events_seen"] += 1
            flat = prewarmed[index] if prewarmed is not None else self._flat(event)
            pairs = (
                token_pairs_of(flat, self.profiles)
                if prewarmed is not None
                else observed_pairs(flat, self.profiles)
            )
            self._classify_one(qualifying, groups, (seq_hint, event, flat, pairs), now)
        self._select_candidates(digest, qualifying)
        digest.groups_total = len(groups)
        digest.suppressed_total = sum(group.call_count for group in groups.values())
        digest.groups = sorted(groups.values(), key=lambda g: (-g.window_count, g.signature))[
            : self.tuning.max_suppressed_groups_listed
        ]
        return digest

    def _flat(self, event: dict) -> list[tuple[str, object]]:
        """压平 + 按 spec 滤掉忽略字段(噪声不进签名/取值统计,但 spec 匹配仍看得到全字段
        ——result_field 校验时已保证不在 ignore_fields 里)。"""
        flat = flatten_event(event)
        if self.spec is None or not self.spec.ignore_fields:
            return flat
        ignored = self.spec.ignore_fields
        return [(path, value) for path, value in flat if path not in ignored]

    def _cold_start_prepass(self, events: list[tuple[int, dict]]) -> list[list[tuple[str, object]]] | None:
        if self._first_call_done or len(events) < _COLD_START_PREPASS_MIN:
            return None
        flats = [self._flat(event) for _seq, event in events]
        for flat in flats:
            for path, value in flat:
                self.profiles.observe_only(path, value)
        return flats

    def _classify_one(
        self,
        qualifying: list[Candidate],
        groups: dict[str, GroupDigest],
        item: tuple[int, dict, list[tuple[str, object]], tuple[tuple[str, str], ...]],
        now: float,
    ) -> None:
        seq_hint, event, flat, pairs = item
        signature = signature_of(pairs)
        window_count = self.counter.observe(signature, now)
        all_time = self._bump_census(signature)
        # 取值窗口计数无条件先记(spec 命中的事件也计入):取值频次账才完整,spec 候选
        # 附带的"该取值窗口内出现几次"证据才真实(§7.5 逐条重判的喂料)。
        minority = self._observe_values(flat, pairs, now)
        if self._try_spec_candidate((qualifying, groups), item, (signature, window_count, all_time), now):
            return
        # 少数派取值优先归取值车道:该证据更具体,且其车道量天生有界;若归入形状车道,
        # 会和成群的稀有形状诱饵挤同一个名额池(计数全 1 平手按序号),重蹈被挤出的算术。
        if minority is not None:
            path, token, value_count, field_count, lane = minority
            qualifying.append(
                Candidate(
                    seq_hint, event, signature, window_count, all_time == 1, all_time,
                    reason="minority_field_value",
                    value_path=path, value_token=token,
                    value_window_count=value_count, field_window_count=field_count,
                    value_lane=lane,
                )
            )
            return
        if window_count <= self.tuning.rare_threshold:
            qualifying.append(Candidate(seq_hint, event, signature, window_count, all_time == 1, all_time))
            return
        self._suppress(groups, (signature, pairs, window_count), (seq_hint, event))

    def _try_spec_candidate(
        self,
        buckets: tuple[list[Candidate], dict[str, GroupDigest]],
        item: tuple[int, dict, list[tuple[str, object]], tuple[tuple[str, str], ...]],
        sig_facts: tuple[str, int, int],
        now: float,
    ) -> bool:
        """spec 命中处理:抬候选(附取值频次证据),或洪泛免疫时按常态压组。
        返回 False = 未命中 spec,事件继续走通用车道。"""
        if self.spec is None:
            return False
        qualifying, groups = buckets
        seq_hint, event, flat, pairs = item
        hit = self.spec.match(flat)
        if hit is None:
            return False
        signature, window_count, all_time = sig_facts
        value_count, field_count = _result_field_window_counts(self, flat, now)
        if _outside_normal_common(self.tuning, hit.mode, value_count, field_count):
            self._suppress(groups, (signature, pairs, window_count), (seq_hint, event))
            return True
        qualifying.append(
            Candidate(
                seq_hint, event, signature, window_count, all_time == 1, all_time,
                reason="spec_target_value",
                value_path=hit.path, value_token=hit.value, spec_mode=hit.mode,
                value_window_count=value_count, field_window_count=field_count,
            )
        )
        return True

    def _observe_values(
        self, flat: list[tuple[str, object]], pairs: tuple[tuple[str, str], ...], now: float
    ) -> tuple[str, str, int, int, str] | None:
        return _rarest_minority_value(self, flat, pairs, now)

    def _select_candidates(self, digest: CallDigest, qualifying: list[Candidate]) -> None:
        spec_cap = (
            self.spec.max_per_pull
            if self.spec is not None and self.spec.max_per_pull > 0
            else self.tuning.spec_max_candidates_per_pull
        )
        _select_lanes(digest, qualifying, (spec_cap, self.tuning), self.totals)

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
            "head_profiles": self.head_profiles.snapshot(),
            "window": self.counter.snapshot(now),
            "value_window": self.value_counter.snapshot(now),
            "totals": dict(self.totals),
            "census": dict(ranked),
        }

    def restore(self, payload: dict[str, Any], now: float) -> None:
        self.profiles.restore(dict(payload.get("profiles") or {}))
        self.head_profiles.restore(dict(payload.get("head_profiles") or {}))
        self.counter.restore(dict(payload.get("window") or {}), now)
        self.value_counter.restore(dict(payload.get("value_window") or {}), now)
        for key, value in dict(payload.get("totals") or {}).items():
            if key in self.totals:
                self.totals[key] = int(value)
        for signature, count in dict(payload.get("census") or {}).items():
            if len(self._census) >= _CENSUS_CAP:
                break
            self._census[str(signature)] = int(count)
        # 温启动=画像已收敛,不再做冷启动预热遍(与重置前 events_seen>0 的旧语义一致)。
        self._first_call_done = self.totals["events_seen"] > 0


def _select_lanes(
    digest: CallDigest,
    qualifying: list[Candidate],
    caps: tuple[int, IngestTuning],
    totals: dict[str, int],
) -> None:
    """四车道选拔:spec 命中/稀有形状/字面少数派/首记号少数派各占各的名额,互不挤占。

    单一名额池会重蹈测试方实锤的挤出:诱饵天生是"触发端像目标"的稀有形状,
    每批达标者成群(计数全 1 平手按序号),真目标(少数派取值)混在一个池里
    排队就会被挤进 overflow——7307 个候选里只 7/30 真目标的算术根源。
    取值车道的量天生有界((路径,取值) 窗口计数 <= 阈值),独立名额不会泛滥;
    首记号子车道再独立(文本源的目标天生只从这条路上来,不与字面少数派挤);
    spec 车道是学出来的判据,不被通用车道诱饵挤掉(车道内按取值频次升序,
    判据配错为常态时高频命中沉底、稀有命中先上)。"""
    spec_cap, tuning = caps
    lanes: dict[str, list[Candidate]] = {
        "spec_target_value": [], "minority_field_value": [], "head_value": [], "shape": [],
    }
    for candidate in qualifying:
        if candidate.reason == "minority_field_value" and candidate.value_lane == "head":
            lanes["head_value"].append(candidate)
        else:
            lanes.get(candidate.reason, lanes["shape"]).append(candidate)
    kept_spec, spill_spec = _rank_lane(lanes["spec_target_value"], spec_cap)
    kept_shape, spill_shape = _rank_lane(lanes["shape"], tuning.max_candidates_per_pull)
    kept_value, spill_value = _rank_lane(lanes["minority_field_value"], tuning.value_max_candidates_per_pull)
    kept_head, spill_head = _rank_lane(lanes["head_value"], tuning.head_value_max_candidates_per_pull)
    digest.candidates = sorted(kept_spec + kept_shape + kept_value + kept_head, key=lambda c: c.seq_hint)
    digest.overflow = [
        OverflowRecord(c.seq_hint, c.signature, c.rank_count)
        for c in sorted(spill_spec + spill_shape + spill_value + spill_head, key=lambda c: c.seq_hint)
    ]
    totals["escalated"] += len(digest.candidates)
    totals["escalated_minority_value"] += len(kept_value) + len(kept_head)
    totals["escalated_head_value"] += len(kept_head)
    totals["escalated_spec_target"] += len(kept_spec)
    totals["overflow"] += len(digest.overflow)


def _rank_lane(lane: list[Candidate], keep: int) -> tuple[list[Candidate], list[Candidate]]:
    ranked = sorted(lane, key=lambda c: (c.rank_count, c.all_time_count, c.seq_hint))
    return ranked[: max(0, keep)], ranked[max(0, keep):]


def _outside_normal_common(tuning: IngestTuning, mode: str, value_count: int, field_count: int) -> bool:
    """outside_normal 洪泛免疫:常态之外命中的取值在窗口内高频出现 → 按常态压组
    (高频=常态的结构化定义;模型学常态清单漏列高频取值时,真机实锤整条车道被刷满、
    稀疏真目标反被淹没)。target 点名命中(target_value/target_contains)不受影响;
    证据缺失(计数 0)或样本量不足时不拦——没证据不定罪,与 configure 拒错同一原则。"""
    pct = tuning.outside_normal_common_value_pct
    if pct <= 0 or mode != "outside_normal" or value_count <= 0:
        return False
    if field_count < tuning.value_min_support:
        return False
    return value_count > max(tuning.value_rare_threshold, -(-field_count * pct // 100))


def _result_field_window_counts(
    engine: StreamDigestEngine, flat: list[tuple[str, object]], now: float
) -> tuple[int, int]:
    """spec 命中候选的取值频次证据:只读回查(计数已由 _observe_values 维护)——
    字面取值查 (字段,取值) 窗口计数,高基数文本查其首记号计数;查不到(如首记号
    基数闸关死)返回 0,渲染层跳过。"""
    path = engine.spec.result_field if engine.spec is not None else ""
    for fpath, value in flat:
        if fpath == path:
            return _value_window_counts(engine, path, value, now)
    return (0, 0)


def _value_window_counts(engine: StreamDigestEngine, path: str, value: object, now: float) -> tuple[int, int]:
    token = engine.profiles.token_of(path, value)
    if _is_literal_value_token(token):
        return (
            engine.value_counter.window_count(f"v\x1e{path}\x1e{token}", now),
            engine.value_counter.window_count(f"f\x1e{path}", now),
        )
    if token != TOKEN_HIGH_CARD_TEXT or not isinstance(value, str):
        return (0, 0)
    head_class = engine.head_profiles.token_of(path, head_token(value))
    if head_class == TOKEN_HIGH_CARD_TEXT:
        return (0, 0)
    return (
        engine.value_counter.window_count(f"hv\x1e{path}\x1e{head_class}", now),
        engine.value_counter.window_count(f"hf\x1e{path}", now),
    )


def _rarest_minority_value(
    engine: StreamDigestEngine, flat: list[tuple[str, object]], pairs: tuple[tuple[str, str], ...], now: float
) -> tuple[str, str, int, int, str] | None:
    """给每个取值字段记 (字段样本量, (字段,取值)) 窗口账,返回最稀的少数派取值
    (path, token, value_count, field_count, lane;lane=""字面 / "head"首记号)。

    真目标与诱饵结构相同、只差结果端一个取值时,整事件签名不稀有,但那个取值本身
    在窗口内极少——按 (路径,取值) 窗口计数 <= value_rare_threshold 抬为候选。
    字段样本量 >= value_min_support 才有"少数派"可言(冷启动/稀疏字段不硬判)。
    参与者两类,都是纯计数零语义:
    · 字面记号(布尔/低基数字面/None);单调数(n:mono)、数量级桶(n:eX)不是"取值",不参与;
    · 高基数文本(s:*)走首记号子车道(§4):结果端是"结论词 + 高基数尾巴"的文本消息时
      整值折叠失明——按分隔符取首记号做同款窗口频次,先过首记号基数闸。
    """
    if engine.tuning.value_rare_threshold <= 0:
        return None
    best: tuple[str, str, int, int, str] | None = None
    token_by_path = dict(pairs)
    for path, value in flat:
        found = _minority_value_observation(engine, (path, value, token_by_path.get(path, "")), now)
        if found is not None and (best is None or found[2] < best[2]):
            best = found
    return best


def _minority_value_observation(
    engine: StreamDigestEngine, item: tuple[str, object, str], now: float
) -> tuple[str, str, int, int, str] | None:
    path, value, token = item
    if _is_literal_value_token(token):
        return _observe_literal_value(engine, path, token, now)
    if token == TOKEN_HIGH_CARD_TEXT and isinstance(value, str):
        return _observe_head_token(engine, path, value, now)
    return None


def _observe_literal_value(
    engine: StreamDigestEngine, path: str, token: str, now: float
) -> tuple[str, str, int, int, str] | None:
    field_count = engine.value_counter.observe(f"f\x1e{path}", now)
    value_count = engine.value_counter.observe(f"v\x1e{path}\x1e{token}", now)
    if field_count < engine.tuning.value_min_support or value_count > engine.tuning.value_rare_threshold:
        return None
    return (path, token, value_count, field_count, "")


def _observe_head_token(
    engine: StreamDigestEngine, path: str, value: str, now: float
) -> tuple[str, str, int, int, str] | None:
    """首记号子车道:高基数文本按分隔符取首记号,基数闸通过才做窗口频次。

    基数闸=首记号自身的画像(ProfileTable 同款粘性高基数判定):首记号分布低基数
    (如结论词只有几种)→ 参与少数派统计,稀有首记号抬候选;首记号也高基数
    (trace/id/自由文本)→ 该字段此路永久关闸,一个候选都不产。纯切分+计数。

    稀有闸是【相对占比】(宽抬):计数 <= max(绝对阈值, 窗口字段样本量 * pct / 100)。
    只用绝对阈值时,目标共享同一结论词就会在窗口内累计超限、第 4 个起全漏——
    测试方真机 53% 召回的漏因,离线复现坐实(修后 15/15)。
    """
    head = head_token(value)
    if not head:
        return None
    head_class = engine.head_profiles.observe_and_token(path, head)
    if head_class == TOKEN_HIGH_CARD_TEXT:
        return None
    field_count = engine.value_counter.observe(f"hf\x1e{path}", now)
    value_count = engine.value_counter.observe(f"hv\x1e{path}\x1e{head_class}", now)
    if field_count < engine.tuning.value_min_support:
        return None
    limit = engine.tuning.value_rare_threshold
    if engine.tuning.head_value_rare_pct > 0:
        limit = max(limit, -(-field_count * engine.tuning.head_value_rare_pct // 100))
    if value_count > limit:
        return None
    return (path, f"s1:{head}", value_count, field_count, "head")


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
