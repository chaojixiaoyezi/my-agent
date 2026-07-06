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
from .field_profile import TOKEN_HIGH_CARD_TEXT, ProfileTable, is_literal_value_token
from .flatten import flatten_event
from .signature import observed_pairs, signature_of, sketch_of, token_pairs_of
from .source_spec import SourceSpec
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
# 累计账键:前 7 个是原有主漏斗账;后 5 个是召回三件套账(抽检发出/抽检确认/反馈车道
# 抬升/收件箱确认见闻/对账环未命中);audit_throttled 是判读吞吐反压钳掉的抽检名额
# (真机洪泛净负的观测口:>0 说明反压在干活)。restore 只回填已知键。
_TOTAL_KEYS = (
    "events_seen", "escalated", "suppressed", "overflow",
    "escalated_minority_value", "escalated_head_value", "escalated_spec_target",
    "escalated_feedback", "audit_sampled", "audit_confirmed",
    "feedback_confirmed_seen", "feedback_ring_miss", "audit_throttled",
    # 点名 target 配反免疫钳掉的命中(P2 观测口:>0 说明 target 取值当前≈常态在被压组)。
    "spec_target_suppressed",
    # outside_normal 免疫钳掉的命中(g8 观测口:>0 说明"常态之外"某取值高频≈常态被压组
    # ——要么常态清单漏列了高频取值,要么目标密度高于免疫线,判读侧据告警重 configure)。
    "spec_outside_normal_suppressed",
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
    # 真目标同特征) / audit_sample(抽检车道:常态流分层抽样,非判据命中)。
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
    # 点名 target 配反免疫的结构化告警:(字段\x1e取值) → 计数事实(P2,payload 渲染成
    # spec_target_common_suppressed 让模型看到"判据配反证据"并重 sample+configure)。
    spec_target_common: dict[str, dict] = field(default_factory=dict)


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

    def value_window_counts(self, path: str, value: str, now: float) -> tuple[int, int]:
        """configure 侧回查:某取值在【指定结果端字段】的窗口频次证据 (value_count, field_count)。

        §11.2 误配防线用:apply_spec 保留 value_counter/画像,所以历轮 pull 攒下的窗口频次在
        (重)configure 时可回查——模型把某常态高频取值配成 target 时,这里能看见它在窗口里
        高频出现(≈常态)。字段在窗口内【零样本】(首次 configure 前没 pull 过 / 该字段没进过
        流)→ (0,0),无证据不拦、且不触碰画像(不建空 profile)。纯回查零副作用。
        """
        if not path:
            return (0, 0)
        # 先按窗口字段样本量短路:零样本 = 无证据,直接 (0,0),既不误拦也不改引擎状态。
        if self.value_counter.window_count(f"f\x1e{path}", now) <= 0 and self.value_counter.window_count(f"hf\x1e{path}", now) <= 0:
            return (0, 0)
        return _value_window_counts(self, path, value, now)

    def process(
        self, events: list[tuple[int, dict]], now: float, *, judge_headroom: int | None = None
    ) -> CallDigest:
        """按到达顺序处理一批 (seq_hint, event);两阶段选出候选。

        冷启动首批先做"预热遍"(只喂字段画像、不取签名),让基数/单调性分类先收敛——
        否则首批积压里前几十个事件会以未收敛的字面签名霸占候选位,挤掉真正稀有的。

        judge_headroom(判读吞吐反压,真机实锤:抽检 4000+/用户把主代理判力淹了,
        逐条报出 156→18):调用方(harvester)给出"消费者此刻还判得动几条"的结构化
        余量——抽检车道只花这个余量,绝不越过;None=无反压信号(inline 同步消费/
        测试),行为与旧版一致。真信号车道(spec/少数派/首记号/反馈)永不受限。
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
            self._classify_one((qualifying, groups, digest.spec_target_common), (seq_hint, event, flat, pairs), now)
        self._select_candidates(digest, qualifying)
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
        buckets: tuple[list[Candidate], dict[str, GroupDigest], dict[str, dict]],
        item: tuple[int, dict, list[tuple[str, object]], tuple[tuple[str, str], ...]],
        now: float,
    ) -> None:
        qualifying, groups, _spec_alerts = buckets
        seq_hint, event, flat, pairs = item
        signature = signature_of(pairs)
        window_count = self.counter.observe(signature, now)
        all_time = self._bump_census(signature)
        # 取值窗口计数无条件先记(spec 命中的事件也计入):取值频次账才完整,spec 候选
        # 附带的"该取值窗口内出现几次"证据才真实(§7.5 逐条重判的喂料)。
        minority = self._observe_values(flat, pairs, now)
        if self._try_spec_candidate(buckets, item, (signature, window_count, all_time), now):
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
        if _try_feedback_candidate(self, qualifying, (seq_hint, event, flat, (signature, window_count, all_time)), now):
            return
        if window_count <= self.tuning.rare_threshold:
            qualifying.append(Candidate(seq_hint, event, signature, window_count, all_time == 1, all_time))
            return
        self._suppress(groups, (signature, pairs, window_count), (seq_hint, event))

    def _try_spec_candidate(
        self,
        buckets: tuple[list[Candidate], dict[str, GroupDigest], dict[str, dict]],
        item: tuple[int, dict, list[tuple[str, object]], tuple[tuple[str, str], ...]],
        sig_facts: tuple[str, int, int],
        now: float,
    ) -> bool:
        """spec 命中处理:抬候选(附取值频次证据),或洪泛免疫时按常态压组。
        返回 False = 未命中 spec,事件继续走通用车道。"""
        if self.spec is None:
            return False
        qualifying, groups, spec_alerts = buckets
        seq_hint, event, flat, pairs = item
        hit = self.spec.match(flat)
        if hit is None:
            return False
        signature, window_count, all_time = sig_facts
        value_count, field_count = _result_field_window_counts(self, flat, now)
        if _suppressed_spec_hit(self, (groups, spec_alerts), (hit, value_count, field_count), ((signature, pairs, window_count), (seq_hint, event))):
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
            "feedback": self.feedback.snapshot(now),
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
        self.feedback.restore(dict(payload.get("feedback") or {}), now)
        # 温启动=画像已收敛,不再做冷启动预热遍(与重置前 events_seen>0 的旧语义一致)。
        self._first_call_done = self.totals["events_seen"] > 0


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


def _suppressed_spec_hit(
    engine: StreamDigestEngine,
    sinks: tuple[dict[str, GroupDigest], dict[str, dict]],
    hit_facts: tuple[Any, int, int],
    keyed_item: tuple[tuple[str, tuple[tuple[str, str], ...], int], tuple[int, dict]],
) -> bool:
    """spec 命中的洪泛压制裁决+执行:outside_normal 高频免疫(既有)与点名 target 配反免疫
    (P2,压组之外还要记告警/totals)都在此收口;不压制返回 False,由调用方照抬候选。
    两种免疫压制都记结构化告警(g8 复验教训:outside_normal 免疫静默吞掉整车道目标时,
    模型/监控完全看不见——告警把"该取值高频≈常态被压组"的计数事实亮给判读侧)。"""
    groups, spec_alerts = sinks
    hit, value_count, field_count = hit_facts
    keyed, item = keyed_item
    outside_common = _outside_normal_common(engine.tuning, hit.mode, value_count, field_count)
    if not outside_common and not _named_target_common(engine.tuning, hit.mode, value_count, field_count):
        return False
    if outside_common:
        engine.totals["spec_outside_normal_suppressed"] += 1
    else:
        engine.totals["spec_target_suppressed"] += 1
    _record_spec_target_common(spec_alerts, hit, value_count, field_count)
    engine._suppress(groups, keyed, item)
    return True


def _named_target_common(tuning: IngestTuning, mode: str, value_count: int, field_count: int) -> bool:
    """点名 target 配反免疫(P2):target_value/target_contains 命中的具体取值在窗口内占字段
    样本量比例 >= spec_target_common_value_pct → 判"该取值当前≈常态"(高频=常态,与 configure
    拒错/outside_normal 免疫同一结构化定义),按常态压组防洪泛。
    阈值(默认 25%)故意远高于 configure 闸(2%):真·稀疏目标(离线台密度 5-8%)绝够不着;
    真目标事故尖峰突破 25% 才暂压、占比回落立即自愈(纯窗口计数,无持久状态)。
    证据缺失(计数 0)或样本量不足(< value_min_support)不拦——没证据不定罪。"""
    pct = tuning.spec_target_common_value_pct
    if pct <= 0 or mode not in ("target_value", "target_contains") or value_count <= 0:
        return False
    if field_count < tuning.value_min_support:
        return False
    return value_count > max(tuning.value_rare_threshold, -(-field_count * pct // 100))


def _record_spec_target_common(alerts: dict[str, dict], hit, value_count: int, field_count: int) -> None:
    """把配反免疫的计数事实记进本批告警(同 (字段,取值) 折叠成一条,附本批压组次数)。"""
    key = f"{hit.path}\x1e{hit.value}"
    record = alerts.get(key)
    if record is None:
        alerts[key] = {
            "path": hit.path,
            "value": hit.value,
            "mode": hit.mode,
            "value_window_count": value_count,
            "field_window_count": field_count,
            "suppressed_this_call": 1,
        }
        return
    record["value_window_count"] = value_count
    record["field_window_count"] = field_count
    record["suppressed_this_call"] = int(record.get("suppressed_this_call") or 0) + 1


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
    """字面取值记号判定(实现挪进 field_profile.is_literal_value_token,反馈层共用)。"""
    return is_literal_value_token(token)


__all__ = ["CallDigest", "Candidate", "GroupDigest", "OverflowRecord", "StreamDigestEngine"]
