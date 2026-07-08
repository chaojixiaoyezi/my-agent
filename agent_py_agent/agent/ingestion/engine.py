"""摄取引擎:一批原始事件 → 候选批 + 被压组账目。只用结构化信号,不做定性。

正常量直通(降维分诊之前的第一问):本批量不超过 min(full_read_per_pull, 判读余量)
→ 整批全量抬给模型逐条认真读(常见形状不压组、名额不裁剪;唯 normal_* 内容过滤规则
命中仍记账回落=教过的常态减负)。量涨出预算/判读积压吃光余量 → 自动回落下述降维
分诊,判完积压又自动恢复——正常量逐条读与洪水降级是同一根反压信号上的连续谱,
纯计数切换,零速率估算。

降维分诊的候选选择是两阶段:先把达标事件全部收进备选,call 结束按
(窗口计数升序, 全时计数升序, 序号升序) 排序取前 N——保证"最稀有的先上",
不会被同 call 先到的次稀有事件挤出;溢出的带坐标进账目,不静默丢。

达标有三条互补通道(都是纯结构化匹配/计数):
1. per-源判据 spec(若已配,learn→configure→monitor 的"配"):结果端字段取值命中
   spec 的目标集合/子串/常态之外 → 直接抬候选。判据由模型从样本学出(理解在模型),
   这里只做字面比对(执行在代码)。真实日志又花又杂(高基数噪声淹信号、结果端是
   文本消息)时,通用稀有度会瞎,只有配了判据才盯得准。
   【铁则:去留永远来自内容判断(spec 是模型按内容学出的规则),不来自计数阈值】——
   spec 命中绝不因"取值出现得频繁"被丢弃(旧版的按百分比免疫压组已整体移除:真事
   变频繁照样被当"常态"整批吞,2%→25% 只挪线不换判据)。频率只当触发器:高频命中类
   随批发结构化调查告警(计数事实+示例事件),模型按内容定性后要么 configure 建常态
   规则(命中逐条记账,可审计),要么照报真事。命中常态规则(normal_*)的事件不进
   spec 车道(这就是"研判过、认得它了"的减负),但回落通用兜底车道,稀有仍可抬。
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
from .field_profile import TOKEN_HIGH_CARD_TEXT, ProfileTable, is_literal_value_token
from .flatten import flatten_event
from .signature import observed_pairs, signature_of, sketch_of, token_pairs_of
from .source_spec import NORMAL_RULE_MODES, SourceSpec
from .text_tokens import head_token
from .watch_feedback import (
    FeedbackState,
    audit_budget,
    event_feature_keys,
    feature_key_parts,
    match_learned_feature,
    pick_audit_groups,
)
from .window_counter import SlidingWindowCounter

_CENSUS_CAP = 50000
_SNAPSHOT_CENSUS_CAP = 20000
_COLD_START_PREPASS_MIN = 64
# 规则命中账的键数上限(规则条目数本身受 spec 解析上限约束,这里只防坏快照)。
_RULE_HITS_CAP = 256
# 累计账键:前 7 个是原有主漏斗账;后 5 个是召回三件套账(抽检发出/抽检确认/反馈车道
# 抬升/收件箱确认见闻/对账环未命中);audit_throttled 是判读吞吐反压钳掉的抽检名额
# (真机洪泛净负的观测口:>0 说明反压在干活)。restore 只回填已知键。
_TOTAL_KEYS = (
    "events_seen", "escalated", "suppressed", "overflow",
    "escalated_minority_value", "escalated_head_value", "escalated_spec_target",
    "escalated_feedback", "audit_sampled", "audit_confirmed",
    "feedback_confirmed_seen", "feedback_ring_miss", "audit_throttled",
    # 正常量直通账:full_stream_read 通道抬升的事件数(本批量 <= 直通预算时整批全量上,
    # 常见形状不压组——"正常量逐条认真读"的观测口)。
    "escalated_full_read",
    # 调查触发观测口:>0 说明有 spec 命中类正高频出现(命中照抬不丢,只是提醒模型按
    # 内容研判这一类:判据配反/常态漏列 → configure 建规则;真事高发 → 照报)。
    "spec_frequent_hits",
    # 内容过滤规则(spec 的 normal_*)命中账:被规则认出的常态事件数(不进 spec 车道
    # =减负来源;per-规则明细在 engine.rule_hits,零静默丢弃的核算口)。
    "spec_normal_rule_hits",
)


@dataclass
class Candidate:
    seq_hint: int
    event: dict
    signature: str
    window_count: int
    first_seen: bool
    all_time_count: int = 1
    # 达标通道:spec_target_value(per-源判据命中) / structurally_rare_signature(签名稀有)
    # / minority_field_value(少数派取值) / confirmed_target_similar(反馈学习:与已确认
    # 真目标同特征) / audit_sample(抽检车道:常态流分层抽样,非判据命中) /
    # full_stream_read(正常量直通:本批量在判读预算内,全量逐条上,非筛选命中)。
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
    # 抽检车道:该被压组本 call 的事件数(模型判读的代表性证据)。
    audit_group_count: int = 0

    @property
    def rank_count(self) -> int:
        """排序用的等效稀有度:取值类通道(spec 命中/少数派/反馈)按取值窗口计数升序——
        spec 命中不再恒最优:判据配错为常态时其命中量大、计数高,排到车道尾部,
        真正稀有的命中(计数低/证据缺失=0)先上。"""
        if self.reason in ("spec_target_value", "minority_field_value", "confirmed_target_similar"):
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
    # 高频命中类的调查告警:(字段\x1e取值类) → 计数事实+示例事件(频率只触发调查不决定
    # 去留——命中仍照常抬升;payload 渲染成 frequent_hit_investigation,模型按内容定性:
    # 判据配反/常态漏列 → 重 configure 建规则;真事高发 → 照报)。
    frequent_hits: dict[str, dict] = field(default_factory=dict)
    # 本批被内容过滤规则(spec 的 normal_*)认出的常态事件数(减负核算口,零静默)。
    normal_rule_hits: int = 0


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
        self.totals: dict[str, int] = dict.fromkeys(_TOTAL_KEYS, 0)
        self._census: dict[str, int] = {}
        self._first_call_done = False
        # 摄取召回三件套状态(B2 抽检/B3 反馈学习/B4 倾斜),随 snapshot 持久化。
        self.feedback = FeedbackState()
        # 内容过滤规则命中账:(normal 模式\x1e规则条目) → 命中数;随 snapshot 持久化。
        self.rule_hits: dict[str, int] = {}

    def apply_spec(self, spec: SourceSpec | None) -> None:
        """配/换 per-源判据。只有 ignore_fields 变化才重置签名滑窗/census(字段集变了
        旧签名不再可比,重置后下一批重走预热遍);只改取值判据(target_*/normal_*,即
        内容过滤规则的增删)→ 签名窗/census/预热态全保留:按告警建规则是常规动作,每次
        都付冷启动窗口会反过来惩罚建规则(真机实锤:重置期少数派闸"无证据不拦"集中放行,
        20 条误报里 18 条来自这里)。字段画像/取值计数器/首记号画像永远保留(按字段路径
        记账,与 spec 字段集无关);per-规则命中账随 spec 版本重置;累计账 totals 保留。"""
        old_ignore = self.spec.ignore_fields if self.spec is not None else frozenset()
        new_ignore = spec.ignore_fields if spec is not None else frozenset()
        self.spec = spec
        self.rule_hits = {}
        if old_ignore == new_ignore:
            return
        self.counter = SlidingWindowCounter(self.tuning.window_seconds, self.tuning.bucket_seconds)
        self._census = {}
        self._first_call_done = False

    def value_window_counts(self, path: str, value: str, now: float) -> tuple[int, int]:
        """configure 侧回查(§11.2 误配防线):某取值在结果端字段的窗口频次证据
        (value_count, field_count)。字段在窗口内零样本 → (0,0):无证据不拦,且先按
        字段样本量短路、不触碰画像(不建空 profile),纯回查零副作用。"""
        if not path:
            return (0, 0)
        if self.value_counter.window_count(f"f\x1e{path}", now) <= 0 and self.value_counter.window_count(f"hf\x1e{path}", now) <= 0:
            return (0, 0)
        return _value_window_counts(self, path, value, now)

    def process(
        self,
        events: list[tuple[int, dict]],
        now: float,
        *,
        judge_headroom: int | None = None,
        cold_start: bool = False,
        guarantee: bool = False,
    ) -> CallDigest:
        """按到达顺序处理一批 (seq_hint, event);两阶段选出候选。

        冷启动首批先做"预热遍"(只喂字段画像、不取签名),让基数/单调性分类先收敛——
        否则首批积压里前几十个事件会以未收敛的字面签名霸占候选位,挤掉真正稀有的。

        judge_headroom(判读吞吐反压,真机实锤:抽检 4000+/用户把主代理判力淹了,
        逐条报出 156→18):调用方(harvester)给出"消费者此刻还判得动几条"的结构化
        余量——抽检车道只花这个余量,绝不越过;None=无反压信号(inline 同步消费/
        测试),行为与旧版一致。真信号车道(spec/少数派/首记号/反馈)永不受限。

        cold_start(内容型直通开关,治"判据学出前放走存量"):watch 层在本源还没
        configure 出 spec(source_spec is None)时传 True——此时任何"结构规则"都还没
        学出来,不能靠它筛存量,整批 full_read 无条件生效(宁滥勿漏,每条都递到模型)。
        spec.passthrough 同理(模型学出"结构分不开、成败只在响应措辞")→ full_read 无条件。
        两者都仍受 full_read_per_pull>0 总闸约束(=0 是逃生阀,回落旧的降维分诊)。

        guarantee(/audit 保证档,契约=每条都判一条不漏):full_read 无条件生效且
        【不受 full_read_per_pull=0 逃生阀约束】(保证档没有"回落有损分诊"这条路);
        normal_* 内容过滤规则命中的事件也不再压组减负——规则只记账(减负账照亮),
        事件本体照样成候选逐条递给模型。学到的判据在保证档只为判得准,绝不筛掉任何一条。
        """
        prewarmed = self._cold_start_prepass(events)
        self._first_call_done = True
        digest = CallDigest()
        qualifying: list[Candidate] = []
        groups: dict[str, GroupDigest] = {}
        content_mode = guarantee or cold_start or (self.spec is not None and self.spec.passthrough)
        full_read = guarantee or self._full_read_active(events, judge_headroom, content_mode)
        for index, (seq_hint, event) in enumerate(events):
            digest.seen += 1
            self.totals["events_seen"] += 1
            flat = prewarmed[index] if prewarmed is not None else self._flat(event)
            pairs = (
                token_pairs_of(flat, self.profiles)
                if prewarmed is not None
                else observed_pairs(flat, self.profiles)
            )
            self._classify_one(
                (qualifying, groups, digest), (seq_hint, event, flat, pairs), now,
                full_read=full_read, guarantee=guarantee,
            )
        self._select_candidates(digest, qualifying, full_read=full_read)
        digest.groups_total = len(groups)
        digest.suppressed_total = sum(group.call_count for group in groups.values())
        digest.groups = sorted(groups.values(), key=lambda g: (-g.window_count, g.signature))[
            : self.tuning.max_suppressed_groups_listed
        ]
        _append_audit_samples(self, digest, groups, (now, judge_headroom))
        _remember_positions(self, digest, now)
        return digest

    def _flat(self, event: dict) -> list[tuple[str, object]]:
        """压平 + 按 spec 滤掉忽略字段(噪声不进签名/取值统计,但 spec 匹配仍看得到全字段
        ——result_field 校验时已保证不在 ignore_fields 里)。"""
        flat = flatten_event(event)
        if self.spec is None or not self.spec.ignore_fields:
            return flat
        ignored = self.spec.ignore_fields
        return [(path, value) for path, value in flat if path not in ignored]

    def _full_read_active(
        self, events: list[tuple[int, dict]], judge_headroom: int | None, content_mode: bool = False
    ) -> bool:
        return _full_read_active(self.tuning, events, judge_headroom, content_mode)

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
        buckets: tuple[list[Candidate], dict[str, GroupDigest], CallDigest],
        item: tuple[int, dict, list[tuple[str, object]], tuple[tuple[str, str], ...]],
        now: float,
        *,
        full_read: bool = False,
        guarantee: bool = False,
    ) -> None:
        qualifying, groups, _digest = buckets
        seq_hint, event, flat, pairs = item
        signature = signature_of(pairs)
        window_count = self.counter.observe(signature, now)
        all_time = self._bump_census(signature)
        # 取值窗口计数无条件先记(spec 命中的事件也计入):取值频次账才完整,spec 候选
        # 附带的"该取值窗口内出现几次"证据才真实(§7.5 逐条重判的喂料)。
        minority = self._observe_values(flat, pairs, now)
        spec_outcome = self._try_spec_candidate(buckets, item, (signature, window_count, all_time), now)
        if spec_outcome == "escalated":
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
        # 反馈车道(B3):与模型已确认真目标同特征的事件直接抬升——真目标频次涨过少数派
        # 阈值后(共享结论词第 4 条起)通用车道会盲,这里按已确认特征续抬,不受稀有闸限。
        # 直通模式跳过:事件横竖全量上,别白花反馈特征的窗口配额(fb 计数键)。
        if not full_read and _try_feedback_candidate(self, qualifying, (seq_hint, event, flat, (signature, window_count, all_time)), now):
            return
        if window_count <= self.tuning.rare_threshold:
            qualifying.append(Candidate(seq_hint, event, signature, window_count, all_time == 1, all_time))
            return
        # 正常量直通:常见形状也整批全量上(不压组),模型逐条认真读——唯 normal_* 内容
        # 过滤规则命中的事件仍走压组减负(那是"研判过、认得它了"的常态,命中已逐条记账)。
        # 保证档(guarantee)连这条减负也关:规则命中只记账,事件照样成候选——学到的
        # 判据绝不变成有损筛,每条都到模型眼前(/audit 契约:一条不漏)。
        if full_read and (guarantee or spec_outcome != "normal_fallthrough"):
            qualifying.append(
                Candidate(
                    seq_hint, event, signature, window_count, all_time == 1, all_time,
                    reason="full_stream_read",
                )
            )
            return
        _suppress(self, groups, (signature, pairs, window_count), (seq_hint, event))

    def _try_spec_candidate(
        self,
        buckets: tuple[list[Candidate], dict[str, GroupDigest], CallDigest],
        item: tuple[int, dict, list[tuple[str, object]], tuple[tuple[str, str], ...]],
        sig_facts: tuple[str, int, int],
        now: float,
    ) -> str:
        """spec 匹配:target/常态之外 → 一律抬候选(高频类只加调查告警,绝不因频率丢弃;
        车道内频次升序=稀有先上高频沉底,负载有界);常态规则命中 → 记账后回落通用车道。
        返回三态:"escalated"=已抬候选;"normal_fallthrough"=常态规则命中记账后回落
        (直通模式也不无脑抬,规则减负仍有效,稀有兜底照走);"no_match"=未配/未命中。"""
        if self.spec is None:
            return "no_match"
        qualifying, _groups, digest = buckets
        seq_hint, event, flat, pairs = item
        hit = self.spec.classify(flat)
        if hit is None:
            return "no_match"
        if hit.mode in NORMAL_RULE_MODES:
            _count_rule_hit(self, hit, digest)
            return "normal_fallthrough"
        signature, window_count, all_time = sig_facts
        value_count, field_count, value_class = _result_field_window_facts(self, flat, now)
        if _frequent_hit_class(self.tuning, value_count, field_count):
            self.totals["spec_frequent_hits"] += 1
            _record_frequent_hit(digest.frequent_hits, (hit, value_class), (value_count, field_count), (seq_hint, event))
        qualifying.append(
            Candidate(
                seq_hint, event, signature, window_count, all_time == 1, all_time,
                reason="spec_target_value",
                value_path=hit.path, value_token=hit.value, spec_mode=hit.mode,
                value_window_count=value_count, field_window_count=field_count,
            )
        )
        return "escalated"

    def _observe_values(
        self, flat: list[tuple[str, object]], pairs: tuple[tuple[str, str], ...], now: float
    ) -> tuple[str, str, int, int, str] | None:
        return _rarest_minority_value(self, flat, pairs, now)

    def _select_candidates(
        self, digest: CallDigest, qualifying: list[Candidate], *, full_read: bool = False
    ) -> None:
        if full_read:
            _select_full_read(digest, qualifying, self.totals)
            return
        spec_cap = (
            self.spec.max_per_pull
            if self.spec is not None and self.spec.max_per_pull > 0
            else self.tuning.spec_max_candidates_per_pull
        )
        _select_lanes(digest, qualifying, (spec_cap, self.tuning), self.totals)

    def _bump_census(self, signature: str) -> int:
        return _bump_census(self._census, signature)

    def snapshot(self, now: float) -> dict[str, Any]:
        ranked = sorted(self._census.items(), key=lambda kv: (-kv[1], kv[0]))[:_SNAPSHOT_CENSUS_CAP]
        return {
            "profiles": self.profiles.snapshot(),
            "head_profiles": self.head_profiles.snapshot(),
            "window": self.counter.snapshot(now),
            "value_window": self.value_counter.snapshot(now),
            "totals": dict(self.totals),
            "census": dict(ranked),
            "feedback": self.feedback.snapshot(now),
            "rule_hits": dict(self.rule_hits),
        }

    def restore(self, payload: dict[str, Any], now: float) -> None:
        _restore_engine(self, payload, now)


def _restore_engine(engine: StreamDigestEngine, payload: dict[str, Any], now: float) -> None:
    engine.profiles.restore(dict(payload.get("profiles") or {}))
    engine.head_profiles.restore(dict(payload.get("head_profiles") or {}))
    engine.counter.restore(dict(payload.get("window") or {}), now)
    engine.value_counter.restore(dict(payload.get("value_window") or {}), now)
    for key, value in dict(payload.get("totals") or {}).items():
        if key in engine.totals:
            engine.totals[key] = int(value)
    for signature, count in dict(payload.get("census") or {}).items():
        if len(engine._census) >= _CENSUS_CAP:
            break
        engine._census[str(signature)] = int(count)
    engine.feedback.restore(dict(payload.get("feedback") or {}), now)
    raw_rules = list(dict(payload.get("rule_hits") or {}).items())[:_RULE_HITS_CAP]
    engine.rule_hits = {str(key): int(count) for key, count in raw_rules}
    # 温启动=画像已收敛,不再做冷启动预热遍(与重置前 events_seen>0 的旧语义一致)。
    engine._first_call_done = engine.totals["events_seen"] > 0


def _bump_census(census: dict[str, int], signature: str) -> int:
    current = census.get(signature)
    if current is not None:
        census[signature] = current + 1
        return current + 1
    if len(census) >= _CENSUS_CAP:
        return 1
    census[signature] = 1
    return 1


def _full_read_active(
    tuning: IngestTuning,
    events: list[tuple[int, dict]],
    judge_headroom: int | None,
    content_mode: bool = False,
) -> bool:
    """正常量直通判定(纯计数):本批量 <= min(直通上限, 判读余量) 才整批直通。
    判读积压把余量吃光 → 自动回落分诊(洪水/判不过来时的降级),积压清了自动恢复;
    单批超上限(冷启动追赶/洪峰)→ 该批走分诊。0=直通关闭。

    content_mode(冷启动未学 / spec.passthrough 内容型):批量/余量双闸【不适用】——
    结构分不开或判据未学时,靠批量/稀有度筛就是"用结构规则替模型拍板放走要紧事"
    (根因2/3)。这条路无条件全量直通(每条都递到模型),积压只由 spool backlog 天然
    降速、绝不结构丢弃。仍受 full_read_per_pull>0 总闸约束(=0 逃生阀回落降维分诊)。"""
    cap = int(tuning.full_read_per_pull or 0)
    if cap <= 0 or not events:
        return False
    if content_mode:
        return True
    if judge_headroom is not None:
        cap = min(cap, judge_headroom)
    return len(events) <= cap


def _select_full_read(digest: CallDigest, qualifying: list[Candidate], totals: dict[str, int]) -> None:
    """直通批全收零裁剪(总量已由直通预算封顶):"逐条认真读"不许再被车道名额挤出。
    各车道账目照记(直通批里 spec/少数派证据行仍归各自账)。"""
    digest.candidates = sorted(qualifying, key=lambda c: c.seq_hint)
    digest.overflow = []
    totals["escalated"] += len(digest.candidates)
    totals["escalated_full_read"] += sum(1 for c in digest.candidates if c.reason == "full_stream_read")
    totals["escalated_spec_target"] += sum(1 for c in digest.candidates if c.reason == "spec_target_value")
    minority_rows = [c for c in digest.candidates if c.reason == "minority_field_value"]
    totals["escalated_minority_value"] += len(minority_rows)
    totals["escalated_head_value"] += sum(1 for c in minority_rows if c.value_lane == "head")


def _try_feedback_candidate(
    engine: StreamDigestEngine, qualifying: list[Candidate], item: tuple, now: float
) -> bool:
    """反馈车道(B3):事件特征命中学习库(模型确认过的真目标特征)→ 抬候选。
    洪泛有界:每特征每窗口抬升上限(fb 计数键),超限落回通用车道/压组;
    每次实抬记账进特征(持续抬而无新确认 → 自动退休)。
    item=(seq_hint, event, flat, (signature, window_count, all_time))。"""
    if engine.tuning.feedback_max_candidates_per_pull <= 0:
        return False
    seq_hint, event, flat, sig_facts = item
    matched = match_learned_feature(engine, flat, now)
    if matched is None:
        return False
    key, value_count, field_count = matched
    if engine.tuning.feedback_feature_window_cap > 0:
        # 先查后记:只有【实抬】才占窗口配额。被拒尝试若也计数,到达率一旦 ≥ 过期率,
        # 计数永不回落 → 该特征永久闸死(离线台实锤:24 之后全部真目标被拒)。
        if engine.value_counter.window_count(f"fb\x1e{key}", now) >= engine.tuning.feedback_feature_window_cap:
            return False
        engine.value_counter.observe(f"fb\x1e{key}", now)
    engine.feedback.record_lift(
        key, now,
        retire_min_lifted=engine.tuning.feedback_retire_min_lifted,
        window_seconds=engine.tuning.window_seconds,
    )
    kind, path, token = feature_key_parts(key)
    signature, window_count, all_time = sig_facts
    qualifying.append(
        Candidate(
            seq_hint, event, signature, window_count, all_time == 1, all_time,
            reason="confirmed_target_similar",
            value_path=path, value_token=token,
            value_window_count=value_count, field_window_count=field_count,
            value_lane="head" if kind == "hv" else "",
        )
    )
    return True


def _append_audit_samples(
    engine: StreamDigestEngine,
    digest: CallDigest,
    groups: dict[str, GroupDigest],
    budget_ctx: tuple[float, int | None],
) -> None:
    """抽检车道(B2+B4):从本 call 被压组里按轮换抽代表事件抬为候选(reason=
    audit_sample),模型判力空余时撞结构筛盲区;预算=每 call 上限 ∧ 每分钟允额
    (盲区证据触发倾斜)∧ 判读余量(反压:本 call 已选的真车道候选先占余量,抽检
    只用剩下的——"抬的绝不超过判得完的",钳掉的名额进 audit_throttled 账)。
    budget_ctx=(now, judge_headroom);示例事件是完整原始事件,判定仍全归模型。"""
    now, judge_headroom = budget_ctx
    budget = audit_budget(engine, now)
    if budget <= 0 or not groups:
        return
    if judge_headroom is not None:
        allowed = max(0, judge_headroom - len(digest.candidates))
        if allowed < budget:
            # 涓流保底(不足4):backlog 长期压住 headroom 时抽检会被钳到 0、反馈飞轮的
            # 盲区发现整窗断粮(真机 90 分钟 backlog 恒 >8 → headroom 恒 0 → 抽检 0 条)。
            # 保留每分钟 audit_floor_per_minute 条的最小额度——与抽检分钟允额同一滑窗
            # 共账,负载仍有界(默认 1/min ≈ 真机判读量的 5%,不淹判力)。
            allowed = max(allowed, min(budget, _audit_floor_allowance(engine, now)))
            # 只记真损失:本来抽得出的组数 - 反压后还抽得出的组数。
            engine.totals["audit_throttled"] += min(budget, len(groups)) - min(allowed, len(groups))
            budget = allowed
    if budget <= 0:
        return
    for group in pick_audit_groups(engine, list(groups.values()), budget):
        engine.feedback.audit_window.observe("audit", now)
        engine.feedback.bump_audited(group.signature)
        digest.candidates.append(
            Candidate(
                group.exemplar_seq, group.exemplar, group.signature, group.window_count,
                False, engine._census.get(group.signature, 1),
                reason="audit_sample", audit_group_count=group.call_count,
            )
        )
        engine.totals["audit_sampled"] += 1
    digest.candidates.sort(key=lambda c: c.seq_hint)


def _audit_floor_allowance(engine: StreamDigestEngine, now: float) -> int:
    """反压钳位下的抽检涓流余量:每分钟 audit_floor_per_minute 减去本分钟已抬的抽检数
    (与 audit_budget 的分钟允额同一滑窗,floor 不会额外放大总额)。0=保底关闭。"""
    floor = int(getattr(engine.tuning, "audit_floor_per_minute", 0) or 0)
    if floor <= 0:
        return 0
    used = engine.feedback.audit_window.window_count("audit", now)
    return max(0, floor - used)


def _remember_positions(engine: StreamDigestEngine, digest: CallDigest, now: float) -> None:
    """对账环登记(B3 的接缝):模型看得见的每条事件(候选行 + 被压组示例)都记
    (stream_pos → 特征键);record_finding 回传 watch_id+stream_pos 即可把确认对回
    结构特征。被压组示例标记抽检来源(确认它=筛漏了它=倾斜证据)。"""
    for candidate in digest.candidates:
        keys = event_feature_keys(engine, engine._flat(candidate.event), now)
        engine.feedback.remember_position(
            candidate.seq_hint, keys, audit=candidate.reason == "audit_sample"
        )
    for group in digest.groups:
        keys = event_feature_keys(engine, engine._flat(group.exemplar), now)
        engine.feedback.remember_position(group.exemplar_seq, keys, audit=True)


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
        "spec_target_value": [], "minority_field_value": [], "head_value": [],
        "confirmed_target_similar": [], "shape": [],
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
    kept_fb, spill_fb = _rank_lane(lanes["confirmed_target_similar"], tuning.feedback_max_candidates_per_pull)
    digest.candidates = sorted(kept_spec + kept_shape + kept_value + kept_head + kept_fb, key=lambda c: c.seq_hint)
    digest.overflow = [
        OverflowRecord(c.seq_hint, c.signature, c.rank_count)
        for c in sorted(spill_spec + spill_shape + spill_value + spill_head + spill_fb, key=lambda c: c.seq_hint)
    ]
    totals["escalated"] += len(digest.candidates)
    totals["escalated_minority_value"] += len(kept_value) + len(kept_head)
    totals["escalated_head_value"] += len(kept_head)
    totals["escalated_spec_target"] += len(kept_spec)
    totals["escalated_feedback"] += len(kept_fb)
    totals["overflow"] += len(digest.overflow)


def _rank_lane(lane: list[Candidate], keep: int) -> tuple[list[Candidate], list[Candidate]]:
    ranked = sorted(lane, key=lambda c: (c.rank_count, c.all_time_count, c.seq_hint))
    return ranked[: max(0, keep)], ranked[max(0, keep):]


def _suppress(
    engine: StreamDigestEngine,
    groups: dict[str, GroupDigest],
    keyed: tuple[str, tuple[tuple[str, str], ...], int],
    item: tuple[int, dict],
) -> None:
    """常见【形状】按签名压组(每组留示例+计数,抽检车道会回捞):这是体量压缩的正路
    ——与按取值频率丢 spec 命中无关(那条路已移除)。"""
    signature, pairs, window_count = keyed
    seq_hint, event = item
    engine.totals["suppressed"] += 1
    group = groups.get(signature)
    if group is None:
        groups[signature] = GroupDigest(signature, sketch_of(pairs), window_count, 1, seq_hint, event)
        return
    group.call_count += 1
    group.window_count = window_count


def _count_rule_hit(engine: StreamDigestEngine, hit, digest: CallDigest) -> None:
    """内容过滤规则命中记账(哪条规则、拦了多少,零静默):不进 spec 车道=减负,
    事件仍走通用兜底车道(稀有仍可抬,规则不是硬闸)。"""
    key = f"{hit.mode}\x1e{hit.value}"
    engine.rule_hits[key] = engine.rule_hits.get(key, 0) + 1
    engine.totals["spec_normal_rule_hits"] += 1
    digest.normal_rule_hits += 1


def _frequent_hit_class(tuning: IngestTuning, value_count: int, field_count: int) -> bool:
    """调查触发线(纯计数,只触发、不决定去留):spec 命中的取值类在窗口内高频出现 →
    这一类值得按内容看一眼(判据配反?常态漏列?还是真事高发?)。命中本身照常抬升,
    绝不因频率丢弃——"频率超阈值即丢"的旧判据(2%→25% 的免疫压组)已整体移除。
    证据缺失(计数 0)或样本量不足(< value_min_support)不触发:无证据不惊动。"""
    pct = tuning.frequent_hit_investigate_pct
    if pct <= 0 or value_count <= 0 or field_count < tuning.value_min_support:
        return False
    return value_count > max(tuning.value_rare_threshold, -(-field_count * pct // 100))


def _record_frequent_hit(
    alerts: dict[str, dict],
    hit_class: tuple[Any, str],
    counts: tuple[int, int],
    item: tuple[int, dict],
) -> None:
    """把高频命中类的计数事实+示例事件记进本批调查告警(同 (字段,取值类) 折叠一条)。
    键用【取值类记号】不用原始取值:文本结果端每条尾巴都不同,按原值折叠会一事一行
    刷爆告警;类记号(字面记号/首记号类)与窗口计数器同粒度,一类恰一行。
    示例事件存原件,渲染/落盘层截断——调查要看请求端+结果端的内容,光有计数不够。"""
    hit, value_class = hit_class
    value_count, field_count = counts
    seq_hint, event = item
    key = f"{hit.path}\x1e{value_class}"
    record = alerts.get(key)
    if record is None:
        alerts[key] = {
            "path": hit.path,
            "value": hit.value,
            "value_class": value_class,
            "mode": hit.mode,
            "value_window_count": value_count,
            "field_window_count": field_count,
            "hits_this_call": 1,
            "exemplar_stream_pos": seq_hint,
            "exemplar_event": event,
        }
        return
    record["value_window_count"] = value_count
    record["field_window_count"] = field_count
    record["mode"] = hit.mode
    record["hits_this_call"] = int(record.get("hits_this_call") or 0) + 1


def _result_field_window_facts(
    engine: StreamDigestEngine, flat: list[tuple[str, object]], now: float
) -> tuple[int, int, str]:
    """spec 命中候选的取值频次证据 (value_count, field_count, 取值类记号):只读回查
    (计数已由 _observe_values 维护)——字面取值查 (字段,取值) 窗口计数,高基数文本查
    其首记号计数;查不到(如首记号基数闸关死)返回 (0,0,""),渲染层跳过、调查线不触发。"""
    path = engine.spec.result_field if engine.spec is not None else ""
    for fpath, value in flat:
        if fpath == path:
            return _value_window_facts(engine, path, value, now)
    return (0, 0, "")


def _value_window_counts(engine: StreamDigestEngine, path: str, value: object, now: float) -> tuple[int, int]:
    counts = _value_window_facts(engine, path, value, now)
    return (counts[0], counts[1])


def _value_window_facts(engine: StreamDigestEngine, path: str, value: object, now: float) -> tuple[int, int, str]:
    token = engine.profiles.token_of(path, value)
    if _is_literal_value_token(token):
        return (
            engine.value_counter.window_count(f"v\x1e{path}\x1e{token}", now),
            engine.value_counter.window_count(f"f\x1e{path}", now),
            token,
        )
    if token != TOKEN_HIGH_CARD_TEXT or not isinstance(value, str):
        return (0, 0, "")
    head_class = engine.head_profiles.token_of(path, head_token(value))
    if head_class == TOKEN_HIGH_CARD_TEXT:
        return (0, 0, "")
    return (
        engine.value_counter.window_count(f"hv\x1e{path}\x1e{head_class}", now),
        engine.value_counter.window_count(f"hf\x1e{path}", now),
        f"head:{head_class}",
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
    """字面取值记号判定(实现挪进 field_profile.is_literal_value_token,反馈层共用)。"""
    return is_literal_value_token(token)


__all__ = ["CallDigest", "Candidate", "GroupDigest", "OverflowRecord", "StreamDigestEngine"]
