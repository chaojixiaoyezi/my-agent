"""摄取召回底座(B2/B3/B4):抽检车道 + 反馈学习 + 自适应倾斜的状态与算法。

真机实锤(3h、891 真目标):判这环 0 误报,但"筛"只把 26% 喂给模型、一个源 0/177 全瞎
——很多真目标【语义上真、结构上和常态一样】,结构预筛天生盲。三件套(全部纯结构化,
守铁律:判定归模型、这里只计数/匹配/搬运):

- B2 抽检车道:对被压组的常态流按【轮换】抽代表样本(每签名组被抽次数升序,少抽的
  先抽)喂给模型判,撞见结构筛盲区。有界:每 call 上限 + 每分钟允额(独立滑窗)。
- B3 反馈学习:模型经 record_finding 确认一条候选后(带 watch_id+stream_pos),把该
  事件的【结构特征键】(字面取值/首记号,与引擎取值计数器同键)记入学习库;此后同键
  事件走反馈车道直接抬升——锚定在已确认真目标上,不猜。特征洪泛有界(每特征每窗口
  抬升上限),持续抬而无新确认的特征自动退休(新确认自动复活)。
- B4 自适应倾斜:抽检允额按本源的结构化盲区证据(抽检确认过 / 反馈车道在干活 /
  筛长期零抬升)在 base 与 tilt 两档间切换——筛得差的源多抽。

对账通路:引擎发出候选/示例时把 (stream_pos → 特征键) 记入有界环;record_finding
只回传 watch_id+stream_pos(收件箱 ndjson,append-only);引擎属主消费收件箱,环上
命中即注册特征。跨进程成立(收件箱在盘上),环随引擎快照持久化。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .field_profile import TOKEN_HIGH_CARD_TEXT, is_literal_value_token
from .text_tokens import head_token
from .window_counter import SlidingWindowCounter

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注,运行期不导入(防循环)。
    from .engine import StreamDigestEngine
    from .watch_state import WatchState

_RING_CAP = 2048
_LEARNED_CAP = 128
_AUDITED_CAP = 4096
_FEATURE_KEYS_PER_EVENT = 8
_RETIRE_STALE_WINDOWS = 2
_AUDIT_WINDOW_SECONDS = 60
_AUDIT_BUCKET_SECONDS = 5
_AUDIT_BOOTSTRAP_MIN_EVENTS_FACTOR = 4
_INBOX_LINE_CAP = 4096


class FeedbackState:
    """引擎侧的三件套状态:学习库(learned)+ 对账环(ring)+ 抽检轮换账(audited)
    + 抽检允额滑窗。全部随引擎快照持久化。"""

    def __init__(self) -> None:
        # 特征键 → {"c":确认数, "l":抬升数, "fa":首确认, "la":最近确认, "r":退休}
        self.learned: dict[str, dict[str, Any]] = {}
        # stream_pos → {"k":[特征键], "a":1=抽检车道发出}(插入序=FIFO 驱逐序)
        self.ring: dict[int, dict[str, Any]] = {}
        # 签名 → 已被抽检次数(轮换:少抽的先抽;超容量驱逐低计数=只丢"本来就该再抽"的)
        self.audited: dict[str, int] = {}
        self.audit_window = SlidingWindowCounter(_AUDIT_WINDOW_SECONDS, _AUDIT_BUCKET_SECONDS)

    # ---------------------------------------------------------------- 学习库

    def register_confirmed(self, keys: list[str], now: float) -> None:
        """一条已确认真目标的特征键入库:确认数 +1、退休复活;满则驱逐最弱特征。"""
        for key in keys:
            entry = self._ensure_entry(key, now)
            entry["c"] = int(entry["c"]) + 1
            entry["la"] = now
            entry["r"] = 0

    def _ensure_entry(self, key: str, now: float) -> dict[str, Any]:
        entry = self.learned.get(key)
        if entry is not None:
            return entry
        if len(self.learned) >= _LEARNED_CAP:
            self._evict_weakest()
        entry = {"c": 0, "l": 0, "fa": now, "la": now, "r": 0}
        self.learned[key] = entry
        return entry

    def _evict_weakest(self) -> None:
        ranked = sorted(
            self.learned.items(),
            key=lambda kv: (0 if kv[1].get("r") else 1, int(kv[1].get("c") or 0), float(kv[1].get("la") or 0.0)),
        )
        for key, _entry in ranked[: max(1, len(ranked) - _LEARNED_CAP + 1)]:
            self.learned.pop(key, None)

    def record_lift(self, key: str, now: float, *, retire_min_lifted: int, window_seconds: int) -> None:
        """反馈车道抬升一次的记账 + 自动退休:持续抬而【无新确认】(确认数停在注册那一次
        且已过 stale 窗)→ 退休停抬;之后再有确认自动复活(register_confirmed 清退休位)。"""
        entry = self.learned.get(key)
        if entry is None:
            return
        entry["l"] = int(entry["l"]) + 1
        stale = now - float(entry.get("la") or 0.0) > _RETIRE_STALE_WINDOWS * max(1, window_seconds)
        if retire_min_lifted > 0 and int(entry["l"]) >= retire_min_lifted and int(entry["c"]) <= 1 and stale:
            entry["r"] = 1

    def active_keys(self) -> set[str]:
        return {key for key, entry in self.learned.items() if not entry.get("r")}

    # ---------------------------------------------------------------- 对账环

    def remember_position(self, pos: int, keys: list[str], *, audit: bool) -> None:
        if pos in self.ring:
            self.ring.pop(pos)
        elif len(self.ring) >= _RING_CAP:
            oldest = next(iter(self.ring))
            self.ring.pop(oldest)
        self.ring[pos] = {"k": list(keys), "a": 1 if audit else 0}

    # ---------------------------------------------------------------- 抽检轮换

    def bump_audited(self, signature: str) -> None:
        count = self.audited.get(signature)
        if count is None and len(self.audited) >= _AUDITED_CAP:
            self._evict_low_audited()
        self.audited[signature] = int(count or 0) + 1

    def _evict_low_audited(self) -> None:
        ranked = sorted(self.audited.items(), key=lambda kv: kv[1])
        for signature, _count in ranked[: max(1, len(ranked) // 8)]:
            self.audited.pop(signature, None)

    # ---------------------------------------------------------------- 快照

    def snapshot(self, now: float) -> dict[str, Any]:
        return {
            "learned": {key: dict(entry) for key, entry in self.learned.items()},
            "ring": [[pos, dict(entry)] for pos, entry in self.ring.items()],
            "audited": dict(self.audited),
            "audit_window": self.audit_window.snapshot(now),
        }

    def restore(self, payload: dict[str, Any], now: float) -> None:
        for key, entry in dict(payload.get("learned") or {}).items():
            if isinstance(entry, dict) and len(self.learned) < _LEARNED_CAP:
                self.learned[str(key)] = _learned_entry(entry)
        self._restore_ring(list(payload.get("ring") or []))
        for signature, count in dict(payload.get("audited") or {}).items():
            if len(self.audited) >= _AUDITED_CAP:
                break
            self.audited[str(signature)] = int(count)
        self.audit_window.restore(dict(payload.get("audit_window") or {}), now)

    def _restore_ring(self, items: list) -> None:
        for item in items:
            if len(self.ring) >= _RING_CAP:
                return
            if not (isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[1], dict)):
                continue
            self.ring[int(item[0])] = {
                "k": [str(k) for k in (item[1].get("k") or [])],
                "a": 1 if item[1].get("a") else 0,
            }


def _learned_entry(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "c": int(raw.get("c") or 0),
        "l": int(raw.get("l") or 0),
        "fa": float(raw.get("fa") or 0.0),
        "la": float(raw.get("la") or 0.0),
        "r": 1 if raw.get("r") else 0,
    }


# -------------------------------------------------------------------- 特征键

_KEY_SEP = "\x1e"


def literal_feature_key(path: str, token: str) -> str:
    """字面取值特征键——与引擎 value_counter 的 (字段,取值) 计数键同构,可直接查频次。"""
    return f"v{_KEY_SEP}{path}{_KEY_SEP}{token}"


def head_feature_key(path: str, head_class: str) -> str:
    """首记号特征键——与引擎 value_counter 的首记号计数键同构。"""
    return f"hv{_KEY_SEP}{path}{_KEY_SEP}{head_class}"


_FEATURE_MAJORITY_MIN_SUPPORT = 8


def event_feature_keys(engine: StreamDigestEngine, flat: list[tuple[str, object]], now: float) -> list[str]:
    """一条事件的结构特征键(只读,不动任何画像/计数):字面取值 + 首记号,按窗口频次
    升序取前 N——越稀的越可能是区分性特征。【多数派特征不入围】:取值占字段窗口样本量
    过半的特征(如全流共有的 kind=login)与常态无区分度,登记它会让反馈车道连常态一起抬
    (目标按监控前提是稀疏的,过半即常态)。零语义:记号相等 + 计数比较。"""
    scored: list[tuple[int, str]] = []
    for path, value in flat:
        keyed = _feature_of(engine, path, value)
        if keyed is None:
            continue
        value_count = engine.value_counter.window_count(keyed, now)
        field_key = f"f{_KEY_SEP}{path}" if keyed.startswith("v") else f"hf{_KEY_SEP}{path}"
        field_count = engine.value_counter.window_count(field_key, now)
        if field_count >= _FEATURE_MAJORITY_MIN_SUPPORT and value_count * 2 > field_count:
            continue
        scored.append((value_count, keyed))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [key for _count, key in scored[:_FEATURE_KEYS_PER_EVENT]]


def _feature_of(engine: StreamDigestEngine, path: str, value: object) -> str | None:
    token = engine.profiles.token_of(path, value)
    if is_literal_value_token(token):
        return literal_feature_key(path, token)
    if token != TOKEN_HIGH_CARD_TEXT or not isinstance(value, str):
        return None
    head = head_token(value)
    if not head:
        return None
    head_class = engine.head_profiles.token_of(path, head)
    if head_class == TOKEN_HIGH_CARD_TEXT:
        return None
    return head_feature_key(path, head_class)


def match_learned_feature(
    engine: StreamDigestEngine, flat: list[tuple[str, object]], now: float
) -> tuple[str, int, int] | None:
    """反馈车道匹配:事件特征键 ∩ 学习库活跃键,取窗口频次最稀的一个。
    返回 (特征键, 取值窗口计数, 字段窗口计数);无匹配返回 None。"""
    active = engine.feedback.active_keys()
    if not active:
        return None
    best: tuple[str, int, int] | None = None
    for path, value in flat:
        keyed = _feature_of(engine, path, value)
        if keyed is None or keyed not in active:
            continue
        value_count = engine.value_counter.window_count(keyed, now)
        field_key = f"f{_KEY_SEP}{path}" if keyed.startswith("v") else f"hf{_KEY_SEP}{path}"
        field_count = engine.value_counter.window_count(field_key, now)
        if best is None or value_count < best[1]:
            best = (keyed, value_count, field_count)
    return best


def feature_key_parts(key: str) -> tuple[str, str, str]:
    """特征键 → (kind, path, token);坏键返回空串三元组(渲染层跳过)。"""
    parts = key.split(_KEY_SEP)
    if len(parts) != 3:
        return ("", "", "")
    return (parts[0], parts[1], parts[2])


# -------------------------------------------------------------------- 抽检预算(B2+B4)


def audit_budget(engine: StreamDigestEngine, now: float) -> int:
    """本次 process 的抽检名额 = min(每 call 上限, 每分钟允额余量);允额按盲区证据倾斜。

    盲区证据(全部结构化计数):①抽检样本被模型确认过(audit_confirmed>0);②反馈
    车道在实际抬升(escalated_feedback>0——需要反馈救的源就是筛不到位的源);③筛长期
    零抬升(events_seen 足够多而 escalated 仍为 0,如 0% 全瞎源的冷启动形态)。"""
    tuning = engine.tuning
    per_call = int(tuning.audit_sample_per_pull or 0)
    if per_call <= 0:
        return 0
    totals = engine.totals
    blind = (
        int(totals.get("audit_confirmed") or 0) > 0
        or int(totals.get("escalated_feedback") or 0) > 0
        or (
            int(totals.get("events_seen") or 0)
            >= _AUDIT_BOOTSTRAP_MIN_EVENTS_FACTOR * max(1, int(tuning.value_min_support))
            and int(totals.get("escalated") or 0) == 0
        )
    )
    if blind:
        per_call = int(tuning.audit_tilt_per_pull or per_call)
        allowance = int(tuning.audit_tilt_per_minute or 0)
    else:
        allowance = int(tuning.audit_sample_per_minute or 0)
    used = engine.feedback.audit_window.window_count("audit", now)
    return max(0, min(per_call, allowance - used))


def pick_audit_groups(engine: StreamDigestEngine, groups: list, budget: int) -> list:
    """轮换选组:被抽次数升序(没抽过的先抽)→ 窗口计数降序(常态大流量里更可能藏着
    稀疏目标)→ 签名字典序(确定性)。"""
    ranked = sorted(
        groups,
        key=lambda g: (engine.feedback.audited.get(g.signature, 0), -int(g.window_count), g.signature),
    )
    return ranked[: max(0, budget)]


# -------------------------------------------------------------------- 确认收件箱(对账通路)


def inbox_path(owner_home: Path, watch_id: str) -> Path:
    return Path(owner_home) / "watch_state" / f"{watch_id}.feedback.ndjson"


def append_confirmation(owner_home: Path, watch_id: str, stream_pos: int) -> bool:
    """record_finding 侧:把一条确认(watch_id+stream_pos)追加进收件箱。best-effort:
    watch 不存在/写失败都返回 False,绝不影响结论账主通道。"""
    state_file = Path(owner_home) / "watch_state" / f"{watch_id}.json"
    if not state_file.is_file():
        return False
    path = inbox_path(owner_home, watch_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"pos": int(stream_pos), "t": round(time.time(), 3)}) + "\n")
    except (OSError, ValueError):
        return False
    return True


def consume_feedback_inbox(state: WatchState, now: float) -> int:
    """引擎属主消费收件箱(调用方持 state.lock):从上次偏移顺读完整行,环上命中即
    注册特征;抽检车道发出的确认计入倾斜证据。返回本次消费的确认条数。"""
    path = inbox_path(state.owner_home, state.watch_id)
    lines, state.feedback_offset = _read_new_lines(path, int(state.feedback_offset or 0))
    consumed = 0
    for line in lines:
        pos = _confirmation_pos(line)
        if pos is None:
            continue
        consumed += 1
        _absorb_confirmation(state.engine, pos, now)
    return consumed


def _read_new_lines(path: Path, offset: int) -> tuple[list[str], int]:
    try:
        if offset > path.stat().st_size:
            offset = 0  # 收件箱被外部重建(变小):偏移越界会永久读空,回卷从头补读
        with path.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read()
    except OSError:
        return [], offset
    if not chunk:
        return [], offset
    end = chunk.rfind(b"\n")
    if end < 0:
        return [], offset  # 尾部半行(写入中):下次再收
    complete = chunk[: end + 1]
    lines = [line for line in complete.decode("utf-8", "replace").splitlines() if line.strip()]
    return lines, offset + len(complete)


def _confirmation_pos(line: str) -> int | None:
    if len(line) > _INBOX_LINE_CAP:
        return None
    try:
        record = json.loads(line)
        return int(record.get("pos"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _absorb_confirmation(engine: StreamDigestEngine, pos: int, now: float) -> None:
    engine.totals["feedback_confirmed_seen"] = int(engine.totals.get("feedback_confirmed_seen") or 0) + 1
    entry = engine.feedback.ring.get(pos)
    if entry is None:
        engine.totals["feedback_ring_miss"] = int(engine.totals.get("feedback_ring_miss") or 0) + 1
        return
    if entry.get("a"):
        engine.totals["audit_confirmed"] = int(engine.totals.get("audit_confirmed") or 0) + 1
    engine.feedback.register_confirmed([str(key) for key in (entry.get("k") or [])], now)


__all__ = [
    "FeedbackState",
    "append_confirmation",
    "audit_budget",
    "consume_feedback_inbox",
    "event_feature_keys",
    "feature_key_parts",
    "head_feature_key",
    "inbox_path",
    "literal_feature_key",
    "match_learned_feature",
    "pick_audit_groups",
]
