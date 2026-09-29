# LLM: 唤醒毒丸的持久层：每条唤醒一份尝试账（wake_queue/attempts/<id>.json，失败计数的唯一权威）、结案记录
#   （wake_queue/quarantine/<id>.json）和人工重放留档（quarantine/replayed/<id>/<n>.json，重放次数的唯一权威）。
#   唤醒信封内容是冻结的，
#   计数不能写进信封；结案记录 = 原信封字段原样 + status=failed_permanently + 顶层 quarantine 键，
#   发布层只比较 WakeSignal 字段，顶层附加键不参与对账。判定全部委托 wake_poison 纯函数，这里只做加锁读写。
#   结案顺序比照 WakeStore.mark_handled：先写结案记录、再删 pending、最后结掉关联观察，任何时刻至少一份在位。
#   本模块只新增，不改 mark_handled 的签名和行为。改动时联查 store_wake_publication、retention_scan 与
#   test_wake_attempt_store。
# 模块用途: 记录一条后台唤醒每次被领取的结果，在反复同因失败时把它结案、不再领取，并支持管理员人工重放。
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..common.json_io import write_json_file_atomic_unlocked
from ..gateway_parts.daemon_metadata import build_process_identity, process_identity_is_live
from ..gateway_parts.io import locked_file_transition, write_json_file_atomic
from ..runtime_errors import DataCorruptionError, compact_error_message, runtime_error_report
from .models import WakeSignal
from .store_io import read_json_object_report, unlink_quietly
from .store_layout import ConversationStorage
from .wake_poison import (
    WAKE_ATTEMPT_ABANDONED,
    WAKE_STATUS_FAILED_PERMANENTLY,
    WAKE_VERDICT_FAILURE,
    QuarantineDecision,
    WakeAttemptVerdict,
    WakePoisonState,
    WakeStallAlert,
    mark_stall_alerted,
    next_poison_state,
    quarantine_decision,
    stall_alert,
    verdict_for_batch,
)

WAKE_ATTEMPTS_SCHEMA = "wake-attempts.v1"
WAKE_QUARANTINE_SCHEMA = "wake-quarantine.v1"
WAKE_REPLAY_NOT_FOUND = "WAKE_REPLAY_NOT_FOUND"
WAKE_REPLAY_DOMAIN_TERMINAL = "WAKE_REPLAY_DOMAIN_TERMINAL"
WAKE_REPLAY_PENDING_CONFLICT = "WAKE_REPLAY_PENDING_CONFLICT"
# 尝试账里保留的错误消息只作诊断，截断到这个长度；判定从不读它。
_WAKE_ERROR_SAMPLE_CHARS = 200


# LLM: claim_id 与 batch_size 写进 in_flight；batch_size 是本次一起执行的唤醒条数，进程中途死亡时据此把 abandoned
#   改写成批次失败（批大小大于 1 时不计数，只让下一次逐条执行）。
# 类用途: 描述一次即将开始的尝试。
@dataclass(frozen=True)
class WakeAttemptStart:
    claim_id: str
    batch_size: int = 1


# LLM: decision 非空表示调用方应结案而不是继续执行；abandoned 表示 begin 发现上次尝试的进程已死并已补记；
#   stall_alert 非空表示连续不计数已满提醒窗口，本次已在尝试账里记下提醒，调用方负责发运维事件与宿主提示。
# 类用途: 一次记账后的结果：最新状态、是否该结案、是否识别出中途死亡的尝试、是否该发长时间不计数提醒。
@dataclass(frozen=True)
class WakeAttemptOutcome:
    state: WakePoisonState
    decision: QuarantineDecision | None
    abandoned: bool = False
    stall_alert: WakeStallAlert | None = None


# LLM: 失败时 ok=False 且 error_code 为 WAKE_REPLAY_*，不改任何文件；成功时 signal 是写回 pending 的原信封。
# 类用途: 人工重放的结构化结果。
@dataclass(frozen=True)
class WakeReplayResult:
    ok: bool
    error_code: str = ""
    signal: WakeSignal | None = None


# LLM: 只持有 storage、定位 pending 文件的回调和结掉观察的回调；不访问整个 Store，不做判定、不发事件。
#   尝试账坏了抛 DataCorruptionError，不静默清零（清零会让毒丸重新获得无限次机会）。
# 类用途: 唤醒尝试账、结案与重放的唯一读写入口，挂在 WakeStore.attempts 上。
class WakeAttemptStore:
    # 函数用途: 绑定存储目录与两个窄回调，不读写文件。
    def __init__(
        self,
        storage: ConversationStorage,
        *,
        find_pending_path: Callable[[str], Path | None],
        mark_observations_handled: Callable[..., None],
    ) -> None:
        self.storage = storage
        self._find_pending_path = find_pending_path
        self._mark_observations_handled = mark_observations_handled

    # LLM: 纯读，不取锁；没有账返回空状态和 None，坏账返回空状态加结构化错误，调用方不能把错误当成"没失败过"。
    # 函数用途: 读取一条唤醒当前的判定状态，供调度判断下次最早可再试的时间。
    def state_report(self, wake_signal_id: str) -> tuple[WakePoisonState, dict[str, Any] | None]:
        path = self.storage.wake_attempt_path(wake_signal_id)
        payload, error = read_json_object_report(path, context="conversation.wake_attempts.read")
        if error is not None:
            return WakePoisonState(), error
        try:
            return _ledger_state(payload), None
        except ValueError as exc:
            return WakePoisonState(), _corruption_report(exc, path)

    # LLM: 在执行前调用；上一条 in_flight 属于已确认死亡的进程时先补记一次 attempt:abandoned（按那次尝试的批大小
    #   经 verdict_for_batch 改写，批次中死亡只算批次失败），再写入本次 in_flight。进程是否存活只看结构化身份
    #   （process_identity_is_live 返回 False 才算死），无法判断时不补记。
    # 函数用途: 标记一次尝试开始，并识别上一次尝试是否在执行中途连同进程一起消失；会写尝试账。
    def begin(self, signal: WakeSignal, start: WakeAttemptStart, *, now: float) -> WakeAttemptOutcome:
        path = self.storage.wake_attempt_path(signal.wake_signal_id)
        with locked_file_transition(path):
            ledger = self._read_ledger(path, signal)
            state = _ledger_state(ledger)
            previous = ledger.get("in_flight")
            abandoned = _in_flight_abandoned(previous)
            if abandoned:
                state = next_poison_state(state, verdict_for_batch(WAKE_ATTEMPT_ABANDONED, _batch_size(previous)), now=now)
            ledger.update(state=state.to_dict(), in_flight={
                "claim_id": str(start.claim_id or ""), "batch_size": start.batch_size,
                "owner_process": build_process_identity(), "started_at": now})
            write_json_file_atomic_unlocked(path, ledger)
        return WakeAttemptOutcome(state, quarantine_decision(state), abandoned=abandoned)

    # LLM: 按一次尝试的判定推进状态并清掉 in_flight；成功直接删除尝试账。只有计数失败才更新 last_error。
    #   verdict 必须已按本次批大小经 verdict_for_batch 改写。连续不计数满提醒窗口时在同一把锁里记下提醒
    #   （mark_stall_alerted），随结果返回，调用方负责发事件；这样同一窗口只提醒一次。
    # 函数用途: 记下一次尝试的结果，返回最新状态、是否该结案和是否该发提醒；会写或删尝试账。
    def record(
        self, signal: WakeSignal, verdict: WakeAttemptVerdict, *, now: float, error: BaseException | None = None,
    ) -> WakeAttemptOutcome:
        path = self.storage.wake_attempt_path(signal.wake_signal_id)
        with locked_file_transition(path):
            ledger = self._read_ledger(path, signal)
            state = next_poison_state(_ledger_state(ledger), verdict, now=now)
            if state == WakePoisonState():
                unlink_quietly(path)
                return WakeAttemptOutcome(state, None)
            alert = stall_alert(state, now=now)
            if alert is not None:
                state = mark_stall_alerted(state, now=now)
            ledger.update(state=state.to_dict(), in_flight=None)
            if verdict.kind == WAKE_VERDICT_FAILURE and error is not None:
                ledger["last_error"] = _error_facts(error)
            write_json_file_atomic_unlocked(path, ledger)
        return WakeAttemptOutcome(state, quarantine_decision(state), stall_alert=alert)

    # LLM: 只结案仍在 pending 的唤醒（找不到返回 None）；原信封字段原样保留，只改 status/handled_at 并附顶层
    #   quarantine 键。写结案记录 → 删 pending → 删尝试账 → 结掉关联观察，观察不结会被观察车道当成兜底再跑一遍。
    # 函数用途: 把一条反复同因失败的唤醒结案为 failed_permanently，不再被领取；会写、删文件并更新观察回执。
    def quarantine(self, wake_signal_id: str, decision: QuarantineDecision, *, now: float) -> WakeSignal | None:
        target = self.storage.wake_quarantine_path(wake_signal_id)
        attempts = self.storage.wake_attempt_path(wake_signal_id)
        with locked_file_transition(target):
            pending = self._find_pending_path(wake_signal_id)
            if pending is None:
                return None
            payload, error = read_json_object_report(pending, context="conversation.wake_quarantine.read")
            if error is not None:
                raise DataCorruptionError(f"pending wake {wake_signal_id} is unreadable and cannot be settled")
            ledger, _ledger_error = read_json_object_report(attempts, context="conversation.wake_quarantine.attempts")
            replays = _archive_count(self._replay_archive_dir(wake_signal_id))
            record = {**payload, "status": WAKE_STATUS_FAILED_PERMANENTLY, "handled_at": now,
                      "quarantine": _quarantine_facts(decision, ledger, now, replay_count=replays)}
            write_json_file_atomic_unlocked(target, record)
            unlink_quietly(pending)
            unlink_quietly(attempts)
        settled = WakeSignal.from_dict(record)
        if settled.observation_id:
            self._mark_observations_handled([settled.observation_id], now=now)
        return settled

    # LLM: 只列结案目录顶层（不含 replayed 留档）；逐条坏账进 load_errors，不静默跳过。只返回结构化字段，不含摘要或正文。
    # 函数用途: 列出已结案的唤醒，供运维面和 /wakes 命令展示。
    def quarantined(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        rows: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        directory = self.storage.wake_quarantine_dir
        for path in sorted(directory.glob("*.json")) if directory.is_dir() else ():
            payload, error = read_json_object_report(path, context="conversation.wake_quarantine.list")
            if error is not None:
                errors.append(error)
                continue
            rows.append(_quarantine_row(payload))
        return rows, errors

    # LLM: 在结案记录的锁内完成：核对仍有结案记录、pending 没有同 ID 文件、领域未终态（由调用方传入判定），
    #   然后按原 ID 和冻结内容写回 pending，结案记录移入 replayed/<id>/<n>.json 留档，尝试账清零。
    #   领域判定为 None 表示调用方不做领域检查。任何拒绝都不改文件。
    # 函数用途: 管理员人工重放一条已结案的唤醒；会写 pending、留档和尝试账，并删除结案记录。
    def replay(
        self, wake_signal_id: str, *, now: float, domain_terminal: Callable[[WakeSignal], bool] | None = None,
    ) -> WakeReplayResult:
        source = self.storage.wake_quarantine_path(wake_signal_id)
        with locked_file_transition(source):
            payload, error = read_json_object_report(source, context="conversation.wake_replay.read")
            if error is not None:
                raise DataCorruptionError(f"quarantined wake {wake_signal_id} is unreadable")
            if not payload:
                return WakeReplayResult(False, WAKE_REPLAY_NOT_FOUND)
            facts = payload.pop("quarantine", None)
            facts = facts if isinstance(facts, dict) else {}
            restored = {**payload, "status": "pending", "handled_at": 0.0}
            signal = WakeSignal.from_dict(restored)
            if self._find_pending_path(wake_signal_id) is not None:
                return WakeReplayResult(False, WAKE_REPLAY_PENDING_CONFLICT, signal)
            if domain_terminal is not None and domain_terminal(signal):
                return WakeReplayResult(False, WAKE_REPLAY_DOMAIN_TERMINAL, signal)
            archive = self._replay_archive_dir(wake_signal_id)
            write_json_file_atomic(self.storage.wake_signal_path(signal), restored)
            write_json_file_atomic(archive / f"{_archive_count(archive) + 1}.json",
                                   {**payload, "quarantine": {**facts, "replayed_at": now}})
            unlink_quietly(source)
            unlink_quietly(self.storage.wake_attempt_path(wake_signal_id))
        return WakeReplayResult(True, "", signal)

    # LLM: 结案 ID 已由 wake_quarantine_path 做过不透明 ID 校验；每条唤醒一个子目录，避免 ID 前缀相互匹配。
    # 函数用途: 定位一条唤醒的重放留档目录。
    def _replay_archive_dir(self, wake_signal_id: str) -> Path:
        return self.storage.wake_replayed_dir / self.storage.wake_quarantine_path(wake_signal_id).stem

    # LLM: 在调用方持有的锁内读取；没有账返回新账，坏账抛 DataCorruptionError。
    # 函数用途: 读出一条唤醒的尝试账字典。
    def _read_ledger(self, path: Path, signal: WakeSignal) -> dict[str, Any]:
        payload, error = read_json_object_report(path, context="conversation.wake_attempts.read")
        if error is not None:
            raise DataCorruptionError(f"wake attempts ledger for {signal.wake_signal_id} is unreadable")
        if not payload:
            return _new_ledger(signal)
        try:
            _ledger_state(payload)
        except ValueError as exc:
            raise DataCorruptionError(f"wake attempts ledger for {signal.wake_signal_id} is invalid: {exc}") from exc
        return payload


# 函数用途: 生成一份新的尝试账。
def _new_ledger(signal: WakeSignal) -> dict[str, Any]:
    return {"schema": WAKE_ATTEMPTS_SCHEMA, "wake_signal_id": signal.wake_signal_id, "thread_id": signal.thread_id,
            "state": WakePoisonState().to_dict(), "in_flight": None, "last_error": None}


# LLM: 形状不对只抛 ValueError，由调用方转成数据损坏或结构化读取错误；state 缺失视为空状态。
# 函数用途: 从尝试账字典取出判定状态。
def _ledger_state(ledger: dict[str, Any]) -> WakePoisonState:
    if ledger.get("schema", WAKE_ATTEMPTS_SCHEMA) != WAKE_ATTEMPTS_SCHEMA:
        raise ValueError("wake attempts ledger schema is unknown")
    return WakePoisonState.from_dict(ledger.get("state") or {})


# 函数用途: 数一条唤醒已有的重放留档个数。
def _archive_count(archive: Path) -> int:
    return len(list(archive.glob("*.json"))) if archive.is_dir() else 0


# LLM: in_flight 里的批大小只接受正整数；旧账或坏值按 1（单条）处理，不因缺字段放宽成批次。
# 函数用途: 读出上一次尝试的批大小。
def _batch_size(in_flight: object) -> int:
    value = in_flight.get("batch_size") if isinstance(in_flight, dict) else None
    return value if type(value) is int and value >= 1 else 1


# LLM: 只有结构化进程身份被确认已死（False）才算中途死亡；None（无法判断）与存活都不算。
# 函数用途: 判断上一条未收尾的尝试是否已随进程消失。
def _in_flight_abandoned(in_flight: object) -> bool:
    if not isinstance(in_flight, dict):
        return False
    return process_identity_is_live(in_flight.get("owner_process")) is False


# LLM: 类型与 category 是结构化事实；消息只截断保存作诊断，任何判定都不读它。
# 函数用途: 整理最近一次计数失败的错误事实。
def _error_facts(error: BaseException) -> dict[str, str]:
    report = runtime_error_report(error)
    return {"type": type(error).__name__, "category": str(report.get("category") or ""),
            "error_code": str(getattr(error, "error_code", "") or ""),
            "message": compact_error_message(error)[:_WAKE_ERROR_SAMPLE_CHARS]}


# 函数用途: 组装结案记录里的判定事实，带上结案时尝试账的状态、最近错误和此前的重放次数。
def _quarantine_facts(
    decision: QuarantineDecision, ledger: dict[str, Any], now: float, *, replay_count: int,
) -> dict[str, Any]:
    return {"schema": WAKE_QUARANTINE_SCHEMA, "quarantined_at": now, "decision": decision.to_dict(),
            "attempts": ledger.get("state") or {}, "last_error": ledger.get("last_error"),
            "replay_count": replay_count}


# 函数用途: 读取非负整数形式的重放次数，其它形状按 0。
def _replay_count(facts: dict[str, Any]) -> int:
    value = facts.get("replay_count")
    return value if type(value) is int and value >= 0 else 0


# LLM: 只投影结构化字段（ID、会话、reason、原因码、次数、时间），不带 summary、metadata 或错误消息。
# 函数用途: 把一条结案记录转成列表中的一行。
def _quarantine_row(payload: dict[str, Any]) -> dict[str, Any]:
    facts = payload.get("quarantine") if isinstance(payload.get("quarantine"), dict) else {}
    decision = facts.get("decision") if isinstance(facts.get("decision"), dict) else {}
    return {"wake_signal_id": str(payload.get("wake_signal_id") or ""), "thread_id": str(payload.get("thread_id") or ""),
            "reason": str(payload.get("reason") or ""), "reason_code": str(decision.get("reason_code") or ""),
            "same_cause_count": decision.get("same_cause_count"), "total_count": decision.get("total_count"),
            "mixed_causes": decision.get("mixed_causes") is True, "quarantined_at": facts.get("quarantined_at"),
            "replay_count": _replay_count(facts)}


# 函数用途: 把尝试账形状错误整理成与读取错误一致的结构化报告。
def _corruption_report(exc: BaseException, path: Path) -> dict[str, Any]:
    report = runtime_error_report(DataCorruptionError(str(exc)), context="conversation.wake_attempts.read")
    report["path"] = str(path)
    return report


__all__ = [
    "WAKE_ATTEMPTS_SCHEMA",
    "WAKE_QUARANTINE_SCHEMA",
    "WAKE_REPLAY_DOMAIN_TERMINAL",
    "WAKE_REPLAY_NOT_FOUND",
    "WAKE_REPLAY_PENDING_CONFLICT",
    "WakeAttemptOutcome",
    "WakeAttemptStart",
    "WakeAttemptStore",
    "WakeReplayResult",
]
