# LLM: 唤醒毒丸的持久层：每条唤醒一份尝试账（wake_queue/attempts/<id>.json，失败计数的唯一权威）、结案记录
#   （wake_queue/quarantine/<id>.json）和人工重放留档（quarantine/replayed/<id>/<n>.json，重放次数的唯一权威）。
#   第 3 步新增：读不出的 pending 信封原字节留档在 quarantine/unreadable/<id>.json，读不出的尝试账原字节留档在
#   quarantine/ledger/<id>.json；in_flight 可带 stopping_at（优雅停机标记）。唤醒信封内容是冻结的，
#   计数不能写进信封；结案记录 = 原信封字段原样 + status=failed_permanently + 顶层 quarantine 键，
#   发布层只比较 WakeSignal 字段，顶层附加键不参与对账。判定全部委托 wake_poison 纯函数，这里只做加锁读写。
#   结案顺序比照 WakeStore.mark_handled：先写结案记录、再删 pending、最后结掉关联观察，任何时刻至少一份在位。
#   本模块只新增，不改 mark_handled 的签名和行为。改动时联查 store_wake_publication、retention_scan 与
#   test_wake_attempt_store。
# 模块用途: 记录一条后台唤醒每次被领取的结果，在反复同因失败时把它结案、不再领取，并支持管理员人工重放。
from __future__ import annotations

import os
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
    WAKE_ATTEMPT_GATEWAY_STOPPED,
    WAKE_STATUS_FAILED_PERMANENTLY,
    WAKE_VERDICT_FAILURE,
    QuarantineDecision,
    WakeAttemptVerdict,
    WakePoisonState,
    WakeStallAlert,
    ensure_writable_state,
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
WAKE_REPLAY_SOURCE_UNREADABLE = "WAKE_REPLAY_SOURCE_UNREADABLE"
# 结案记录已超过保留期移进 quarantine/archive/（store_wake_quarantine_archive）；归档的不再重放。
WAKE_REPLAY_ARCHIVED = "WAKE_REPLAY_ARCHIVED"
# 尝试账里保留的错误消息只作诊断，截断到这个长度；判定从不读它。
_WAKE_ERROR_SAMPLE_CHARS = 200


# LLM: claim_id、batch_size、turn_id 写进 in_flight；batch_size 是本次一起执行的唤醒条数，进程中途死亡时据此把 abandoned
#   改写成批次失败（批大小大于 1 时不计数，只让下一次逐条执行）。turn_id 是这一片的精确回合号（与 _run_params 注入时同源：
#   定时 run、派活、会话消息唤醒；没有就是空串），进程中途死亡时 C6 按它收尾那一片认领的补充消息——进程内的在途登记随旧进程
#   消失，所以必须持久化。读旧账时缺这个字段按空串处理。
# 类用途: 描述一次即将开始的尝试。
@dataclass(frozen=True)
class WakeAttemptStart:
    claim_id: str
    batch_size: int = 1
    turn_id: str = ""


# LLM: decision 非空表示调用方应结案而不是继续执行；abandoned 表示 begin/preflight 发现上次尝试的进程已死并已补记
#   （带停机标记的记 attempt:gateway_stopped、不计数，没标记的记 attempt:abandoned、计数）；abandoned_turn_id 是那次
#   在途记录里持久化的回合号（旧账或没有回合号时为空串），C6 据此收尾死掉那一片认领过的补充消息；
#   stall_alert 非空表示连续不计数已满提醒窗口，本次已在尝试账里记下提醒，调用方负责发运维事件与宿主提示。
# 类用途: 一次记账后的结果：最新状态、是否该结案、是否识别出中途死亡的尝试及其回合号、是否该发长时间不计数提醒。
@dataclass(frozen=True)
class WakeAttemptOutcome:
    state: WakePoisonState
    decision: QuarantineDecision | None
    abandoned: bool = False
    stall_alert: WakeStallAlert | None = None
    abandoned_turn_id: str = ""


# LLM: 失败时 ok=False 且 error_code 为 WAKE_REPLAY_*，不改任何文件；成功时 signal 是写回 pending 的原信封。
# 类用途: 人工重放的结构化结果。
@dataclass(frozen=True)
class WakeReplayResult:
    ok: bool
    error_code: str = ""
    signal: WakeSignal | None = None


# LLM: settled 非空 = 正常结案后的信号；source_unreadable=True = pending 信封读不出，原字节已移到 unreadable/、
#   尝试账已删、关联观察已结，没有结案记录；两者都空 = 唤醒已不在 pending。ledger_preserved_at 非空表示尝试账
#   读不出、原字节留在 quarantine/ledger/。调用方（接线层）据此发 wake_quarantined 日志与宿主提示，不再读文件。
# 类用途: 一次结案的结构化结果。
@dataclass(frozen=True)
class WakeQuarantineResult:
    settled: WakeSignal | None
    source_unreadable: bool = False
    ledger_preserved_at: str = ""


# LLM: 只持有 storage、定位 pending 文件的回调和两个结掉观察的回调（按观察 ID / 按 wake_signal_id）；不访问整个 Store，
#   不做判定、不发事件。尝试账坏了抛 DataCorruptionError，不静默清零（清零会让毒丸重新获得无限次机会）；
#   接线层拿到 DataCorruptionError 后按 ledger_corrupt_decision 结案，坏账由 quarantine 原样留档。
# 类用途: 唤醒尝试账、结案与重放的唯一读写入口，挂在 WakeStore.attempts 上。
class WakeAttemptStore:
    # 函数用途: 绑定存储目录与两个窄回调，不读写文件；按 wake_signal_id 结观察的回调另经 bind_wake_observation_settler 绑定。
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
        self._mark_observations_handled_for_wake: Callable[..., list[str]] | None = None

    # LLM: ConversationStore 装配时调用一次，传观察层的 mark_handled_for_wake；没绑定时读不出信封的结案只移文件、不结观察。
    #   不改其它回调，不读写文件。
    # 函数用途: 绑定"按 wake_signal_id 结掉关联观察"的回调。
    def bind_wake_observation_settler(self, callback: Callable[..., list[str]]) -> None:
        self._mark_observations_handled_for_wake = callback

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

    # LLM: 只看文件在不在，不读内容、不取锁；跳过阶段每拍都要对每条 pending 唤醒判一次，只有账存在才值得加锁 preflight。
    # 函数用途: 判断一条唤醒有没有尝试账。
    def has_ledger(self, wake_signal_id: str) -> bool:
        return self.storage.wake_attempt_path(wake_signal_id).is_file()

    # LLM: 在锁内读账，只处理"上一次 in_flight 属于已确认死亡的进程"这一种情况：in_flight 带 stopping_at 记不计数的
    #   attempt:gateway_stopped，没有就记 attempt:abandoned（按那次的批大小经 verdict_for_batch 改写），清掉 in_flight
    #   并写盘；没有账、没有 in_flight、进程还活着或无法判断时不写盘，只返回当前状态与结案判定。账坏抛 DataCorruptionError，
    #   由接线层按 ledger_corrupt 结案。begin 仍照旧识别中途死亡，用来兜住 preflight 与 begin 之间的竞态。
    # 函数用途: 领取前先把上一次随进程消失的尝试补记清楚；可能写尝试账。
    def preflight(self, signal: WakeSignal, *, now: float) -> WakeAttemptOutcome:
        path = self.storage.wake_attempt_path(signal.wake_signal_id)
        with locked_file_transition(path):
            payload, error = read_json_object_report(path, context="conversation.wake_attempts.read")
            if error is not None:
                raise DataCorruptionError(f"wake attempts ledger for {signal.wake_signal_id} is unreadable")
            if not payload:
                return WakeAttemptOutcome(WakePoisonState(), None)
            ledger = self._checked_ledger(payload, signal)
            state, previous = _ledger_state(ledger), ledger.get("in_flight")
            if not _in_flight_abandoned(previous):
                return WakeAttemptOutcome(state, quarantine_decision(state))
            state = next_poison_state(state, verdict_for_batch(_abandoned_verdict(previous), _batch_size(previous)), now=now)
            alert = stall_alert(state, now=now)
            state = mark_stall_alerted(state, now=now) if alert is not None else state
            ledger.update(state=ensure_writable_state(state).to_dict(), in_flight=None)
            write_json_file_atomic_unlocked(path, ledger)
        return _attempt_outcome(state, previous, abandoned=True, alert=alert)

    # LLM: 在执行前调用；上一条 in_flight 属于已确认死亡的进程时先补记一次（同 preflight 的 stopping_at 规则，按那次尝试的
    #   批大小经 verdict_for_batch 改写，批次中死亡只算批次失败），再写入本次 in_flight。进程是否存活只看结构化身份
    #   （process_identity_is_live 返回 False 才算死），无法判断时不补记。写账前经 ensure_writable_state 校验。
    # 函数用途: 标记一次尝试开始，并识别上一次尝试是否在执行中途连同进程一起消失；会写尝试账。
    def begin(self, signal: WakeSignal, start: WakeAttemptStart, *, now: float) -> WakeAttemptOutcome:
        path = self.storage.wake_attempt_path(signal.wake_signal_id)
        with locked_file_transition(path):
            ledger = self._read_ledger(path, signal)
            state = _ledger_state(ledger)
            previous = ledger.get("in_flight")
            abandoned = _in_flight_abandoned(previous)
            if abandoned:
                state = next_poison_state(
                    state, verdict_for_batch(_abandoned_verdict(previous), _batch_size(previous)), now=now)
            ledger.update(state=ensure_writable_state(state).to_dict(), in_flight={
                "claim_id": str(start.claim_id or ""), "batch_size": start.batch_size,
                "turn_id": str(start.turn_id or ""), "owner_process": build_process_identity(), "started_at": now})
            write_json_file_atomic_unlocked(path, ledger)
        return _attempt_outcome(state, previous, abandoned=abandoned)

    # LLM: 优雅停机用：只有 in_flight 的 claim_id 和 owner_process 都是本进程这次尝试的，才在锁内写 stopping_at；
    #   账不存在、读不出、没有 in_flight 或对不上都不写，返回 False（停机路径不能因坏账失败，坏账留给下次 preflight）。
    #   不替在途尝试写结果：worker 若在退出前跑完会照常 record 覆盖 in_flight，两边不会并发写同一份结果。
    # 函数用途: 给本进程在途的一次尝试打上"正在停机"标记；可能写尝试账。
    def mark_stopping(self, wake_signal_id: str, claim_id: str, *, now: float) -> bool:
        path = self.storage.wake_attempt_path(wake_signal_id)
        with locked_file_transition(path):
            payload, error = read_json_object_report(path, context="conversation.wake_attempts.read")
            in_flight = payload.get("in_flight") if error is None else None
            if not _in_flight_owned_here(in_flight, claim_id):
                return False
            payload["in_flight"] = {**in_flight, "stopping_at": now}
            write_json_file_atomic_unlocked(path, payload)
        return True

    # LLM: 唤醒已离开 pending（被确认、按取消结案、同批顺带确认）时删账，不读内容：坏账也删，唤醒已经结了没有毒丸风险。
    #   在同一把账锁里删：停机线程的 mark_stopping 可能同时在写这份账，不持锁的话它的原子写会在删之后把账写回来。
    # 函数用途: 删掉一条唤醒的尝试账；会删文件。
    def discard(self, wake_signal_id: str) -> None:
        path = self.storage.wake_attempt_path(wake_signal_id)
        with locked_file_transition(path):
            unlink_quietly(path)

    # LLM: 按一次尝试的判定推进状态并清掉 in_flight；成功直接删除尝试账。只有计数失败才更新 last_error。
    #   verdict 必须已按本次批大小经 verdict_for_batch 改写。连续不计数满提醒窗口时在同一把锁里记下提醒
    #   （mark_stall_alerted），随结果返回，调用方负责发事件；这样同一窗口只提醒一次。写账前经 ensure_writable_state
    #   校验，状态不自洽时抛 ValueError、原账不动。
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
            ledger.update(state=ensure_writable_state(state).to_dict(), in_flight=None)
            if verdict.kind == WAKE_VERDICT_FAILURE and error is not None:
                ledger["last_error"] = _error_facts(error)
            write_json_file_atomic_unlocked(path, ledger)
        return WakeAttemptOutcome(state, quarantine_decision(state), stall_alert=alert)

    # LLM: 只结案仍在 pending 的唤醒（找不到返回空结果）；原信封字段原样保留，只改 status/handled_at 并附顶层
    #   quarantine 键。顺序固定：写结案记录（坏账时已带预先算出的 ledger_preserved_at）→ 删 pending → 移坏账 → 删尝试账 →
    #   结掉关联观察。结案记录必须最先落盘，pending 紧接着删：若先移账、再删 pending，两步之间崩溃会留下 pending 而账已不在，
    #   这条唤醒会从零重新计数（9a 复审，C6 收窄窗口）；现在任何一步之后崩溃，最多留下一份没有 pending 对应的孤儿坏账。
    #   pending 信封读不出时走 _quarantine_unreadable（原字节移到 unreadable/，不在坏内容上补字段）。
    # 函数用途: 把一条反复同因失败的唤醒结案为 failed_permanently，不再被领取；会写、删、移文件并更新观察回执。
    def quarantine(self, wake_signal_id: str, decision: QuarantineDecision, *, now: float) -> WakeQuarantineResult:
        target = self.storage.wake_quarantine_path(wake_signal_id)
        attempts = self.storage.wake_attempt_path(wake_signal_id)
        with locked_file_transition(target):
            pending = self._find_pending_path(wake_signal_id)
            if pending is None:
                return WakeQuarantineResult(None)
            payload, error = read_json_object_report(pending, context="conversation.wake_quarantine.read")
            if error is not None:
                return self._quarantine_unreadable(wake_signal_id, pending, now=now)
            ledger, preserved = self._ledger_for_quarantine(wake_signal_id, attempts)
            provenance = {"replay_count": _archive_count(self._replay_archive_dir(wake_signal_id)),
                          "ledger_preserved_at": preserved}
            record = {**payload, "status": WAKE_STATUS_FAILED_PERMANENTLY, "handled_at": now,
                      "quarantine": _quarantine_facts(decision, ledger, now, provenance=provenance)}
            write_json_file_atomic_unlocked(target, record)
            unlink_quietly(pending)
            if preserved:
                _move_aside(attempts, Path(preserved))
            unlink_quietly(attempts)
        settled = WakeSignal.from_dict(record)
        if settled.observation_id:
            self._mark_observations_handled([settled.observation_id], now=now)
        return WakeQuarantineResult(settled, ledger_preserved_at=preserved)

    # LLM: 只列结案目录顶层（不含 replayed 留档）；逐条坏账进 load_errors，不静默跳过；unreadable/ 下的留档也列进
    #   load_errors（带 wake_signal_id 与留档路径）。只返回结构化字段，不含摘要或正文。
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
        unreadable = self.storage.wake_quarantine_unreadable_dir
        errors.extend(_unreadable_source_report(path) for path in
                      (sorted(unreadable.glob("*.json")) if unreadable.is_dir() else ()))
        return rows, errors

    # LLM: 纯读、不取锁；判定在模块函数 wake_replay_source，replay（在锁内）与 /wakes replay 预览共用同一口径。
    # 函数用途: 读出一条已结案唤醒的结案记录，读不到时说明原因。
    def replay_source(self, wake_signal_id: str) -> tuple[str, dict[str, Any]]:
        return wake_replay_source(self.storage, wake_signal_id)

    # LLM: 只数文件、不读内容（quarantined_wake_count），给 /status 与 gateway_status 用。
    # 函数用途: 数本 owner 当前已结案（未归档）的唤醒条数。
    def quarantined_count(self) -> int:
        return quarantined_wake_count(self.storage)

    # LLM: 在结案记录的锁内完成：来源状态经 replay_source 判定（已归档、读不出、不存在各自拒绝），再核对 pending 没有
    #   同 ID 文件、领域未终态（由调用方传入判定），然后按原 ID 和冻结内容写回 pending，结案记录移入
    #   replayed/<id>/<n>.json 留档，尝试账清零。领域判定为 None 表示调用方不做领域检查。任何拒绝都不改文件，不抛异常。
    # 函数用途: 管理员人工重放一条已结案的唤醒；会写 pending、留档和尝试账，并删除结案记录。
    def replay(
        self, wake_signal_id: str, *, now: float, domain_terminal: Callable[[WakeSignal], bool] | None = None,
    ) -> WakeReplayResult:
        source = self.storage.wake_quarantine_path(wake_signal_id)
        with locked_file_transition(source):
            code, payload = self.replay_source(wake_signal_id)
            if code:
                return WakeReplayResult(False, code)
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

    # LLM: 信封读不出时的结案分支（在结案记录的锁内调用）：os.replace 把原字节原样移到 unreadable/<id>.json（同一
    #   文件系统上原子改名，字节不变），删尝试账，再按观察记录上的 wake_signal_id 结掉关联观察（不读坏信封）。
    #   不写结案记录、不在坏内容上补字段；quarantined() 把留档列进 load_errors，replay 对它拒绝。
    # 函数用途: 把一条读不出的 pending 唤醒移出队列并留档；会移、删文件并更新观察回执。
    def _quarantine_unreadable(self, wake_signal_id: str, pending: Path, *, now: float) -> WakeQuarantineResult:
        _move_aside(pending, self.storage.wake_quarantine_unreadable_path(wake_signal_id))
        unlink_quietly(self.storage.wake_attempt_path(wake_signal_id))
        if self._mark_observations_handled_for_wake is not None:
            self._mark_observations_handled_for_wake(wake_signal_id, now=now)
        return WakeQuarantineResult(None, source_unreadable=True)

    # LLM: 结案时读尝试账：读得出且形状合法就原样进结案记录（留档路径为空）；读不出或形状不对就只算出留档路径
    #   quarantine/ledger/<id>.json 返回（attempts 为空），不在这里移文件——移动由 quarantine 在结案记录落盘之后做，不清零重来。
    # 函数用途: 取结案记录要带的尝试账，坏账时给出预先算好的留档路径。
    def _ledger_for_quarantine(self, wake_signal_id: str, attempts: Path) -> tuple[dict[str, Any], str]:
        ledger, error = read_json_object_report(attempts, context="conversation.wake_quarantine.attempts")
        if error is None and _ledger_shape_ok(ledger):
            return ledger, ""
        return {}, str(self.storage.wake_quarantine_ledger_path(wake_signal_id))

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
        return self._checked_ledger(payload, signal)

    # LLM: 只校验形状（schema 与 state 能读回），不改内容；形状不对抛 DataCorruptionError，由调用方按坏账处理。
    # 函数用途: 确认一份已读出的尝试账形状合法。
    def _checked_ledger(self, payload: dict[str, Any], signal: WakeSignal) -> dict[str, Any]:
        try:
            _ledger_state(payload)
        except ValueError as exc:
            raise DataCorruptionError(f"wake attempts ledger for {signal.wake_signal_id} is invalid: {exc}") from exc
        return payload


# LLM: 纯读、不取锁；来源状态的唯一判定，WakeAttemptStore.replay（在锁内）与 /wakes replay 预览共用：已移进归档的返回
#   WAKE_REPLAY_ARCHIVED；信封当初读不出（留在 unreadable/）、结案记录本身读不出、记录还原不成信封或信封 ID 与请求的
#   ID 不一致，都返回 WAKE_REPLAY_SOURCE_UNREADABLE（不在坏内容上继续，也不抛异常）；都没有返回 WAKE_REPLAY_NOT_FOUND；
#   读得出返回空码和记录字典。
# 函数用途: 读出一条已结案唤醒的结案记录，读不到时说明原因。
def wake_replay_source(storage: ConversationStorage, wake_signal_id: str) -> tuple[str, dict[str, Any]]:
    source = storage.wake_quarantine_path(wake_signal_id)
    if _source_archived(storage, wake_signal_id, source):
        return WAKE_REPLAY_ARCHIVED, {}
    payload, error = read_json_object_report(source, context="conversation.wake_replay.read")
    if error is not None or _source_unreadable(storage, wake_signal_id, source):
        return WAKE_REPLAY_SOURCE_UNREADABLE, {}
    if not payload:
        return WAKE_REPLAY_NOT_FOUND, {}
    return ("", payload) if _restorable_signal(payload, wake_signal_id) else (WAKE_REPLAY_SOURCE_UNREADABLE, {})


# LLM: 与 WakeAttemptStore.replay 同一还原方式（去掉 quarantine 事实、status 改回 pending、handled_at 清零）；WakeSignal.from_dict
#   抛 ValueError/TypeError 或还原出的 wake_signal_id 与请求的不同都算坏记录。只读，不改文件。
# 函数用途: 判断一条结案记录能否还原成要重放的那条唤醒。
def _restorable_signal(payload: dict[str, Any], wake_signal_id: str) -> bool:
    restored = {key: value for key, value in payload.items() if key != "quarantine"}
    try:
        signal = WakeSignal.from_dict({**restored, "status": "pending", "handled_at": 0.0})
    except (ValueError, TypeError):
        return False
    return signal.wake_signal_id == wake_signal_id


# LLM: 只数文件、不读内容：顶层结案记录加读不出的信封留档，不含已归档与重放留档。
# 函数用途: 数一个会话存储里当前已结案（未归档）的唤醒条数。
def quarantined_wake_count(storage: ConversationStorage) -> int:
    directories = (storage.wake_quarantine_dir, storage.wake_quarantine_unreadable_dir)
    return sum(1 for directory in directories if directory.is_dir() for path in directory.glob("*.json") if path.is_file())


# LLM: 结案记录不存在而 unreadable/ 下有同 ID 留档，就是"信封当初读不出"的那条；两者都没有由调用方按 NOT_FOUND 处理。
# 函数用途: 判断一条唤醒的重放来源是否属于读不出的留档。
def _source_unreadable(storage: ConversationStorage, wake_signal_id: str, source: Path) -> bool:
    return not source.exists() and storage.wake_quarantine_unreadable_path(wake_signal_id).exists()


# LLM: 顶层结案记录不在、而归档目录里有同 ID 的记录或读不出的信封留档，就是已归档（store_wake_quarantine_archive 移过去的）。
# 函数用途: 判断一条唤醒的结案留档是否已经移进归档。
def _source_archived(storage: ConversationStorage, wake_signal_id: str, source: Path) -> bool:
    archived = (storage.wake_quarantine_archive_path(wake_signal_id),
                storage.wake_quarantine_archive_dir / "unreadable" / source.name)
    return not source.exists() and any(path.exists() for path in archived)


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


# LLM: 与 _ledger_state 同一口径，只把 ValueError 折成布尔，给"读得出但坏"的分支用。
# 函数用途: 判断一份尝试账字典形状是否合法。
def _ledger_shape_ok(ledger: dict[str, Any]) -> bool:
    try:
        _ledger_state(ledger)
    except ValueError:
        return False
    return True


# 函数用途: 数一条唤醒已有的重放留档个数。
def _archive_count(archive: Path) -> int:
    return len(list(archive.glob("*.json"))) if archive.is_dir() else 0


# LLM: in_flight 里的批大小只接受正整数；旧账或坏值按 1（单条）处理，不因缺字段放宽成批次。
# 函数用途: 读出上一次尝试的批大小。
def _batch_size(in_flight: object) -> int:
    value = in_flight.get("batch_size") if isinstance(in_flight, dict) else None
    return value if type(value) is int and value >= 1 else 1


# LLM: preflight/begin 共用：识别出中途死亡（abandoned）时从上一次在途记录带出持久化回合号，否则回合号为空；
#   结案判定按最新状态算。纯函数。
# 函数用途: 组装一次开始前补记后的结果。
def _attempt_outcome(
    state: WakePoisonState, previous: object, *, abandoned: bool, alert: WakeStallAlert | None = None,
) -> WakeAttemptOutcome:
    return WakeAttemptOutcome(state, quarantine_decision(state), abandoned=abandoned, stall_alert=alert,
                              abandoned_turn_id=_turn_id(previous) if abandoned else "")


# LLM: 回合号只取 in_flight 里持久化的字符串字段（C3 在 begin 时写入）；旧账、缺字段或非字符串按空串，调用方空串不收尾。
# 函数用途: 读出上一次在途尝试那一片的回合号。
def _turn_id(in_flight: object) -> str:
    value = in_flight.get("turn_id") if isinstance(in_flight, dict) else None
    return value.strip() if isinstance(value, str) else ""


# LLM: 只有结构化进程身份被确认已死（False）才算中途死亡；None（无法判断）与存活都不算。
# 函数用途: 判断上一条未收尾的尝试是否已随进程消失。
def _in_flight_abandoned(in_flight: object) -> bool:
    if not isinstance(in_flight, dict):
        return False
    return process_identity_is_live(in_flight.get("owner_process")) is False


# LLM: 随进程消失的尝试怎么记：in_flight 带有效的 stopping_at（优雅停机标记）→ 不计数的 attempt:gateway_stopped；
#   没有标记（被强杀）→ 计数的 attempt:abandoned。只看结构化字段。
# 函数用途: 给一次中途消失的尝试选判定。
def _abandoned_verdict(in_flight: object) -> WakeAttemptVerdict:
    return WAKE_ATTEMPT_GATEWAY_STOPPED if _stopping_marked(in_flight) else WAKE_ATTEMPT_ABANDONED


# LLM: stopping_at 必须是正数时间戳（bool 不算）；缺失、0 或坏值都当没标记。
# 函数用途: 判断 in_flight 是否带有效的停机标记。
def _stopping_marked(in_flight: object) -> bool:
    value = in_flight.get("stopping_at") if isinstance(in_flight, dict) else None
    return not isinstance(value, bool) and isinstance(value, (int, float)) and value > 0


# LLM: mark_stopping 的准入：claim_id 相同且 owner_process 与本进程身份完全相同（同 host、同 pid、同启动时间）。
# 函数用途: 判断一份 in_flight 是不是本进程正在跑的这次尝试。
def _in_flight_owned_here(in_flight: object, claim_id: str) -> bool:
    if not isinstance(in_flight, dict):
        return False
    return str(in_flight.get("claim_id") or "") == str(claim_id or "") and (
        in_flight.get("owner_process") == build_process_identity())


# LLM: 同一文件系统上的原子改名，字节不变；目标目录按需创建。源文件不存在时静默（没有可留档的内容）。
# 函数用途: 把一个文件原样移到留档位置。
def _move_aside(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, target)
    except FileNotFoundError:
        return


# LLM: 类型与 category 是结构化事实；消息只截断保存作诊断，任何判定都不读它。
# 函数用途: 整理最近一次计数失败的错误事实。
def _error_facts(error: BaseException) -> dict[str, str]:
    report = runtime_error_report(error)
    return {"type": type(error).__name__, "category": str(report.get("category") or ""),
            "error_code": str(getattr(error, "error_code", "") or ""),
            "message": compact_error_message(error)[:_WAKE_ERROR_SAMPLE_CHARS]}


# LLM: provenance 带 replay_count（此前重放次数）与 ledger_preserved_at（坏账留档路径，正常为空串）。
# 函数用途: 组装结案记录里的判定事实，带上结案时尝试账的状态、最近错误和来源事实。
def _quarantine_facts(
    decision: QuarantineDecision, ledger: dict[str, Any], now: float, *, provenance: dict[str, Any],
) -> dict[str, Any]:
    return {"schema": WAKE_QUARANTINE_SCHEMA, "quarantined_at": now, "decision": decision.to_dict(),
            "attempts": ledger.get("state") or {}, "last_error": ledger.get("last_error"), **provenance}


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


# LLM: 留档文件名就是 wake_signal_id；不读内容（它本来就读不出），只报路径、ID 与结构化类别。
# 函数用途: 把一份读不出的信封留档整理成列表里的结构化错误项。
def _unreadable_source_report(path: Path) -> dict[str, Any]:
    report = runtime_error_report(DataCorruptionError("pending wake envelope was unreadable and moved aside"),
                                  context="conversation.wake_quarantine.unreadable")
    report.update(path=str(path), wake_signal_id=path.stem, error_code=WAKE_REPLAY_SOURCE_UNREADABLE)
    return report


__all__ = [
    "WAKE_ATTEMPTS_SCHEMA",
    "WAKE_QUARANTINE_SCHEMA",
    "WAKE_REPLAY_ARCHIVED",
    "WAKE_REPLAY_DOMAIN_TERMINAL",
    "WAKE_REPLAY_NOT_FOUND",
    "WAKE_REPLAY_PENDING_CONFLICT",
    "WAKE_REPLAY_SOURCE_UNREADABLE",
    "WakeAttemptOutcome",
    "WakeAttemptStart",
    "WakeAttemptStore",
    "WakeQuarantineResult",
    "WakeReplayResult",
    "quarantined_wake_count",
    "wake_replay_source",
]
