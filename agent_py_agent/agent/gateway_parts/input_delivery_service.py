from __future__ import annotations

"""LLM: Own the durable disposition of one authenticated Gateway input.

模块用途: 把普通消息在“当前回合、下一轮队列、已消费、终态未知”之间的去向保存为
唯一回执，并在进程重启、响应丢失或回合结束后继续对账，避免 TUI、飞书和 Web 各自补一套。
"""

import hashlib
import json
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..conversation.store_guidance_recovery import (
    CLOSEOUT_REPLAY_QUEUED,
    CLOSEOUT_REPLAY_UNAVAILABLE,
)
from ..conversation.task_state import conversation_task_link_is_terminal
from ..runtime_errors import DataCorruptionError, RecoverableRuntimeError, runtime_error_report
from .io import (
    gateway_response_path,
    gateway_turn_transition,
    read_json_file_report,
    write_json_file_atomic,
)
from .paths import GatewayPaths
from .steer_closeout_replay import (
    SteerReplayQueue,
    notice_steer_closeout,
    queue_steer_replay,
    steer_replay_input,
)

_INPUT_RECEIPT_SCHEMA = "gateway_input_receipt.v2"
_INPUT_RECEIPT_STATES = {
    "pending",
    "active_pending",
    "consumed",
    "terminal_unknown",
    "queued",
}


# LLM: This record is the sole routing authority. Inbox files and active indexes are repairable
# projections; the original prepared request remains here so rejected input can queue without a client retry.
# 类用途: 保存一条普通消息的固定身份、原始排队请求、当前去向和对账错误。
@dataclass(frozen=True)
class GatewayInputReceipt:
    request_id: str
    client_input_digest: str
    client_message_id: str
    state: str
    prepared_request: dict[str, Any]
    prepared_payload_digest: str
    guidance_dedupe_key: str
    target_turn_id: str = ""
    updated_at: float = 0.0
    reconcile_error: dict[str, Any] = field(default_factory=dict)
    migration: dict[str, Any] = field(default_factory=dict)

    # LLM: Every persisted v2 receipt carries the complete recovery payload and typed state.
    # 函数用途: 把普通消息回执转换成原子 JSON 文件内容。
    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": _INPUT_RECEIPT_SCHEMA,
            "request_id": self.request_id,
            "client_input_digest": self.client_input_digest,
            "client_message_id": self.client_message_id,
            "state": self.state,
            "prepared_request": dict(self.prepared_request),
            "prepared_payload_digest": self.prepared_payload_digest,
            "guidance_dedupe_key": self.guidance_dedupe_key,
            "target_turn_id": self.target_turn_id,
            "updated_at": self.updated_at,
        }
        if self.reconcile_error:
            payload["reconcile_error"] = dict(self.reconcile_error)
        if self.migration:
            payload["migration"] = dict(self.migration)
        return payload

    # LLM: v1 has no prepared request and therefore migrates explicitly instead of pretending it
    # can auto-queue. Callers may repair it only with the same digest and current canonical input.
    # 函数用途: 校验并恢复普通消息回执，同时明确记录旧版字段含义的迁移决定。
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GatewayInputReceipt:
        schema = str(data.get("schema_version") or "").strip()
        if schema == "gateway_input_receipt.v1":
            legacy_disposition = str(data.get("disposition") or "pending").strip()
            state = {
                "pending": "pending",
                "active_turn": "terminal_unknown",
                "queued": "queued",
            }.get(legacy_disposition, "")
            migration = {
                "from_schema": "gateway_input_receipt.v1",
                "from_disposition": legacy_disposition,
                "decision": "prepared_request_missing_requires_same_digest_repair",
                "migrated_at": time.time(),
            }
            prepared_request: dict[str, Any] = {}
            prepared_payload_digest = ""
            guidance_key = ""
            client_message_id = ""
            target_turn_id = str(data.get("target_turn_id") or "").strip()
        elif schema == _INPUT_RECEIPT_SCHEMA:
            state = str(data.get("state") or "").strip()
            prepared = data.get("prepared_request")
            prepared_request = dict(prepared) if isinstance(prepared, dict) else {}
            prepared_payload_digest = str(data.get("prepared_payload_digest") or "").strip()
            guidance_key = str(data.get("guidance_dedupe_key") or "").strip()
            client_message_id = str(data.get("client_message_id") or "").strip()
            target_turn_id = str(data.get("target_turn_id") or "").strip()
            raw_migration = data.get("migration")
            migration = dict(raw_migration) if isinstance(raw_migration, dict) else {}
        else:
            state = ""
            prepared_request = {}
            prepared_payload_digest = ""
            guidance_key = ""
            client_message_id = ""
            target_turn_id = ""
            migration = {}
        request_id = str(data.get("request_id") or "").strip()
        digest = str(data.get("client_input_digest") or "").strip()
        if not request_id or not digest or state not in _INPUT_RECEIPT_STATES:
            raise DataCorruptionError("gateway input receipt is invalid")
        raw_error = data.get("reconcile_error")
        try:
            updated_at = float(data.get("updated_at") or 0.0)
        except (TypeError, ValueError) as exc:
            raise DataCorruptionError("gateway input receipt timestamp is invalid") from exc
        return cls(
            request_id=request_id,
            client_input_digest=digest,
            client_message_id=client_message_id,
            state=state,
            prepared_request=prepared_request,
            prepared_payload_digest=prepared_payload_digest,
            guidance_dedupe_key=guidance_key,
            target_turn_id=target_turn_id,
            updated_at=updated_at,
            reconcile_error=dict(raw_error) if isinstance(raw_error, dict) else {},
            migration=migration,
        )


# LLM: Receipt filenames reveal no channel message id and are stable across retries.
# 函数用途: 返回一条 Gateway 普通消息唯一回执文件的位置。
def gateway_input_receipt_path(paths: GatewayPaths, request_id: str) -> Path:
    return paths.root / "input_receipts" / f"{str(request_id or '').strip()}.json"


# LLM: All state changes for one stable ingress id share the same cross-process lock.
# 函数用途: 返回一条普通消息的原子状态转换锁。
def gateway_input_transition(paths: GatewayPaths, request_id: str):
    return gateway_turn_transition(paths, request_id)


# LLM: 入口读失败分流：锁忙（含取锁身份核对重试耗尽）是暂时竞争，按可重试的锁失败抛出；只有
#   其余 load_error 才是数据损坏。调用方绝不把锁忙拒成“损坏”。
# 函数用途: 按报告里的锁忙/读取错误分流抛出，锁忙保持可重试语义。
def _raise_for_input_read_report(report, corruption_message: str) -> None:
    if report.lock_busy:
        raise BlockingIOError("gateway input lock is busy")
    if report.load_error is not None:
        raise DataCorruptionError(corruption_message)


# LLM: A prepared receipt is committed before active routing or queue writes. Reusing the stable
# id with another digest always conflicts before any downstream side effect.
# 函数用途: 在已持有入口锁时读取或首次保存普通消息回执和完整排队请求。
def load_or_prepare_gateway_input_locked(
    paths: GatewayPaths,
    *,
    request_id: str,
    client_input_digest: str,
    client_message_id: str,
    guidance_dedupe_key: str,
    prepared_request: dict[str, Any],
) -> tuple[GatewayInputReceipt, bool]:
    path = gateway_input_receipt_path(paths, request_id)
    report = read_json_file_report(path, context="gateway.input_receipt.read")
    _raise_for_input_read_report(report, "gateway input receipt is unreadable")
    if not report.payload:
        if path.exists():
            raise DataCorruptionError("gateway input receipt is empty or invalid")
        receipt = GatewayInputReceipt(
            request_id=request_id,
            client_input_digest=client_input_digest,
            client_message_id=client_message_id,
            state="pending",
            prepared_request=dict(prepared_request),
            prepared_payload_digest=_prepared_payload_digest(prepared_request),
            guidance_dedupe_key=guidance_dedupe_key,
            updated_at=time.time(),
        )
        _validate_prepared_request(receipt)
        write_json_file_atomic(path, receipt.to_dict())
        return receipt, True
    receipt = GatewayInputReceipt.from_dict(report.payload)
    if (
        receipt.request_id != request_id
        or receipt.client_input_digest != client_input_digest
        or (receipt.client_message_id and receipt.client_message_id != client_message_id)
    ):
        raise ValueError("gateway client message id was reused with different input")
    if not receipt.prepared_request or not receipt.prepared_payload_digest:
        receipt = replace(
            receipt,
            client_message_id=client_message_id,
            prepared_request=dict(prepared_request),
            prepared_payload_digest=_prepared_payload_digest(prepared_request),
            guidance_dedupe_key=receipt.guidance_dedupe_key or guidance_dedupe_key,
            updated_at=time.time(),
        )
        write_json_file_atomic(path, receipt.to_dict())
    _validate_prepared_request(receipt)
    return receipt, False


# LLM: Reads never infer delivery from queue filenames. The receipt remains the only projection
# exposed to HTTP, TUI and adapter reconciliation.
# 函数用途: 读取一条普通消息回执；不存在时返回 None，损坏时明确报错。
def read_gateway_input_receipt(
    paths: GatewayPaths,
    request_id: str,
) -> GatewayInputReceipt | None:
    path = gateway_input_receipt_path(paths, request_id)
    report = read_json_file_report(path, context="gateway.input_receipt.read")
    _raise_for_input_read_report(report, "gateway input receipt is unreadable")
    if not report.payload:
        if path.exists():
            raise DataCorruptionError("gateway input receipt is empty or invalid")
        return None
    if report.payload.get("schema_version") != _INPUT_RECEIPT_SCHEMA:
        raise DataCorruptionError("legacy gateway input receipt requires same-input retry migration")
    receipt = GatewayInputReceipt.from_dict(report.payload)
    _validate_prepared_request(receipt)
    return receipt


# LLM: Active binding is written only after the exact target accepted or durably recorded the same
# guidance key. A per-turn ref is a bounded recovery index, not routing authority.
# 函数用途: 在已持有入口锁时把普通消息绑定到一个精确活动回合。
def bind_gateway_input_active_locked(
    paths: GatewayPaths,
    receipt: GatewayInputReceipt,
    *,
    target_turn_id: str,
    guidance_dedupe_key: str,
) -> GatewayInputReceipt:
    target = str(target_turn_id or "").strip()
    guidance_key = str(guidance_dedupe_key or receipt.guidance_dedupe_key).strip()
    if not target or not guidance_key:
        raise ValueError("active gateway input requires target turn and guidance receipt")
    if receipt.state not in {"pending", "active_pending", "terminal_unknown", "consumed"}:
        raise ValueError("queued gateway input cannot be rebound to an active turn")
    if receipt.target_turn_id and receipt.target_turn_id != target:
        raise ValueError("gateway input is already bound to another turn")
    state = "consumed" if receipt.state == "consumed" else "active_pending"
    updated = replace(
        receipt,
        state=state,
        target_turn_id=target,
        guidance_dedupe_key=guidance_key,
        updated_at=time.time(),
        reconcile_error={},
    )
    write_json_file_atomic(gateway_input_receipt_path(paths, receipt.request_id), updated.to_dict())
    _write_active_turn_ref(paths, updated)
    return updated


# LLM: Promotion writes/repairs the inbox projection before declaring queued. A crash at either
# edge is safe because the same stable id and digest make the next reconciliation idempotent.
# 函数用途: 在已持有入口锁时把明确未消费的普通消息排入下一轮且只写一次。
def queue_gateway_input_locked(
    paths: GatewayPaths,
    receipt: GatewayInputReceipt,
) -> tuple[GatewayInputReceipt, bool]:
    if receipt.state == "consumed":
        return receipt, False
    created = _materialize_prepared_request_locked(paths, receipt)
    updated = replace(
        receipt,
        state="queued",
        updated_at=time.time(),
        reconcile_error={},
    )
    write_json_file_atomic(gateway_input_receipt_path(paths, receipt.request_id), updated.to_dict())
    return updated, created


# LLM: 插话持久操作经 guidance 领域组件； Terminal settlement first lets ConversationStore reject only never-claimed guidance, then
# releases that mailbox lock before taking any ingress lock. This fixed order avoids target/ingress inversion.
# 函数用途: 回合结束后把未认领消息自动排到下一轮，把已消费或不确定消息收成对应状态。
def settle_gateway_inputs_for_turn(
    paths: GatewayPaths,
    *,
    target_turn_id: str,
    conversation_store: object | None,
) -> dict[str, int]:
    summary = {"consumed": 0, "queued": 0, "terminal_unknown": 0, "errors": 0}
    # 没有入口请求号的插话在收口时的两级结局计数：steer_replay_queued 已排成备用下一轮、steer_replay_unavailable 已提示重发。
    summary["steer_replay_queued"] = 0
    summary["steer_replay_unavailable"] = 0
    if conversation_store is None:
        return summary
    try:
        conversation_store.guidance.recovery.reject_pending(
            target_turn_id,
            reject_reserved=True,
        )
    except Exception:
        summary["errors"] += 1
    for request_id in _turn_input_request_ids(paths, target_turn_id):
        try:
            state = reconcile_gateway_input_request(
                paths,
                request_id=request_id,
                conversation_store=conversation_store,
                target_terminal=True,
            )
            if state in summary:
                summary[state] += 1
            elif state:
                summary["errors"] += 1
        except Exception:
            # A corrupt ingress receipt must remain observable as unknown, but it
            # cannot stop the request's canonical terminal response from being
            # archived and published.
            summary["errors"] += 1
    _settle_steer_closeout(paths, conversation_store, str(target_turn_id or "").strip(), summary)
    return summary


# LLM: 没有入口请求号的插话（/steer 控制命令，IM 多是这种）在收口时被拒收，入口对账管不到它们，内容会丢
#   （9b 2026-10-03 复审发现，不是本机制引入的）。这里补两级：能从回执里拿到可重放的结构化输入就排成
#   “备用下一轮”，拿不到就给该会话留一条“请重新发送”的宿主提示；两条出口（TUI 灰行、IM【提示】）共用提示表。
#   判据只看回执级标记和 scope 结构化字段，不读正文。逐条包异常：一条插话处理不了不能让整段收口失败。
# 函数用途: 给本轮被收口拒收、又没有入口回执的插话定结局：排备用下一轮，或提示用户重发。
def _settle_steer_closeout(
    paths: GatewayPaths,
    conversation_store: object,
    turn_id: str,
    summary: dict[str, int],
) -> None:
    if not turn_id:
        return
    for receipt in _closeout_replay_receipts(conversation_store, turn_id):
        try:
            _settle_one_steer_closeout(paths, conversation_store, receipt, summary)
        except Exception:
            summary["errors"] += 1


# LLM: 可重放的判据是“回执里有会话身份与正文”之外还拿得到排队请求：原插话回执的 entry 就是排队请求的正文来源，
#   Gateway 用同一条结构化入口（load_or_prepare + queue）把它排出去，请求号由插话身份推出，重放两次只排一轮。
# 函数用途: 处理一条等收口的插话：排备用下一轮或留提示，并回标已处理。
def _settle_one_steer_closeout(
    paths: GatewayPaths,
    conversation_store: object,
    receipt: object,
    summary: dict[str, int],
) -> None:
    replay = steer_replay_input(receipt)
    queued = False
    if replay is not None:
        request_id, digest = replay.identity()
        payload = _replay_request_payload(receipt, request_id, digest)
        queued = queue_steer_replay(paths, payload, replay, _steer_replay_queue())
    outcome = CLOSEOUT_REPLAY_QUEUED if queued else CLOSEOUT_REPLAY_UNAVAILABLE
    if queued:
        summary["steer_replay_queued"] = summary.get("steer_replay_queued", 0) + 1
    else:
        thread_id = replay.thread_id if replay is not None else _receipt_thread_id(receipt)
        notice_steer_closeout(conversation_store, thread_id)
        summary["steer_replay_unavailable"] = summary.get("steer_replay_unavailable", 0) + 1
    conversation_store.guidance.recovery.mark_closeout_replay(
        str(getattr(receipt, "dedupe_key", "") or "").strip(),
        outcome,
    )


# LLM: 重放要用的入口能力就在本模块里，但重放模块不能反向 import 本模块（会成环），所以在调用点组装一次；
#   三个函数都是模块级纯引用，构造便宜且幂等。
# 函数用途: 组装插话收口重放需要的入口排队能力。
def _steer_replay_queue() -> SteerReplayQueue:
    return SteerReplayQueue(
        prepare=load_or_prepare_gateway_input_locked,
        enqueue=queue_gateway_input_locked,
        transition=gateway_input_transition,
    )


# LLM: 插入回执的 entry 就是一整份“排队请求”的结构化投影（目标、正文、发送者、投递方式、元数据），
#   所以重放不需要另写正文，只把它转成 Gateway 请求形状。入口只认服务端写的身份：请求号必须落在 id 上，
#   客户端指纹也必须写进 metadata.client_input_digest（就是 identity() 算出的那个），否则回执校验不通过。
#   只属于旧回合的派生字段（expected_turn_id、gateway_input_request_id）要去掉：重放请求是新的一轮，不绑旧回合。
# 函数用途: 把插话回执的 entry 转成可重放的 Gateway 排队请求。
def _replay_request_payload(receipt: object, request_id: str, digest: str) -> dict[str, Any]:
    entry = getattr(receipt, "entry", None)
    metadata = getattr(entry, "metadata", None)
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    metadata.pop("expected_turn_id", None)
    metadata.pop("gateway_input_request_id", None)
    metadata["client_input_digest"] = digest
    return {
        "id": request_id,
        "request_id": request_id,
        "kind": "steer_closeout_replay",
        "goal": str(getattr(entry, "message", "") or ""),
        "user_id": str(getattr(entry, "sender", "") or ""),
        "metadata": metadata,
    }


# LLM: 提示要有地方送：优先用会话身份里可重放的 thread_id；重放不了的回执常常连着 thread_id 也没有，
#   这时只能留空，调用方按“没有会话就不发提示”处理，绝不猜一个会话。
# 函数用途: 从回执里取这条插话所属的会话编号。
def _receipt_thread_id(receipt: object) -> str:
    entry = getattr(receipt, "entry", None)
    metadata = getattr(entry, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    return str(metadata.get("thread_id") or "").strip()


# LLM: 会话层的读取只按回执级标记挑（closeout_replay=pending），这里再包一层异常保护：
#   会话存储读不出来时按“这一轮没有可处理的插话”继续，不影响入口对账结果。
# 函数用途: 读出本轮被收口拒收、还没定结局的插话回执。
def _closeout_replay_receipts(conversation_store: object, turn_id: str) -> list[object]:
    try:
        return list(conversation_store.guidance.recovery.closeout_replay_receipts(turn_id))
    except Exception:
        return []


# LLM: 插话持久操作经 guidance 领域组件； Guidance discovery and correlation have one implementation for initial HTTP routing and
# background reconciliation. A readable miss proves no active side effect; read failure stays unknown.
# 函数用途: 查找普通消息对应的 guidance 回执并校验入口、客户端消息和精确回合完全一致。
def gateway_input_guidance_binding(
    receipt: GatewayInputReceipt,
    conversation_store: object,
) -> tuple[object, str] | None:
    guidance = None
    recovered_turn_id = ""
    if receipt.guidance_dedupe_key:
        guidance = conversation_store.guidance.receipt(receipt.guidance_dedupe_key)
        if guidance is not None:
            recovered_turn_id = _guidance_turn_id(guidance)
    if guidance is None:
        guidance, recovered_turn_id = conversation_store.guidance.receipt_for_input(
            receipt.request_id
        )
    if guidance is None:
        return None
    _validate_guidance_binding(receipt, guidance, recovered_turn_id)
    return guidance, recovered_turn_id


# LLM: One reconciliation reads the authoritative guidance receipt under the ingress lock. It may
# repair a missing active binding from the precomputed key but never guesses from message text.
# 函数用途: 对账一条普通消息；明确拒绝才排队，已认领但未确认则保持终态未知。
def reconcile_gateway_input_request(
    paths: GatewayPaths,
    *,
    request_id: str,
    conversation_store: object,
    target_terminal: bool = False,
) -> str:
    with gateway_input_transition(paths, request_id):
        receipt = read_gateway_input_receipt(paths, request_id)
        if receipt is None:
            return ""
        if receipt.state == "queued":
            _materialize_prepared_request_locked(paths, receipt)
            return "queued"
        if receipt.state == "consumed":
            return "consumed"
        guidance = None
        recovered_turn_id = ""
        try:
            binding = gateway_input_guidance_binding(receipt, conversation_store)
            if binding is not None:
                guidance, recovered_turn_id = binding
                if receipt.state == "pending" or not receipt.target_turn_id:
                    receipt = bind_gateway_input_active_locked(
                        paths,
                        receipt,
                        target_turn_id=recovered_turn_id,
                        guidance_dedupe_key=guidance.dedupe_key,
                    )
                else:
                    # Receipt commit precedes this bounded index projection. A crash
                    # between them is repaired on every successful binding read.
                    _write_active_turn_ref(paths, receipt)
        except Exception as exc:
            _write_terminal_unknown(paths, receipt, exc)
            return "terminal_unknown"
        if guidance is None:
            if receipt.state == "pending" and not receipt.target_turn_id:
                queued, _created = queue_gateway_input_locked(paths, receipt)
                return queued.state
            if target_terminal:
                _write_terminal_unknown(paths, receipt, None)
                return "terminal_unknown"
            return receipt.state
        if guidance.status == "consumed":
            updated = replace(
                receipt,
                state="consumed",
                updated_at=time.time(),
                reconcile_error={},
            )
            write_json_file_atomic(
                gateway_input_receipt_path(paths, request_id), updated.to_dict()
            )
            return "consumed"
        if guidance.status == "rejected":
            queued, _created = queue_gateway_input_locked(paths, receipt)
            return queued.state
        if target_terminal or receipt.state == "terminal_unknown":
            _write_terminal_unknown(paths, receipt, None)
            return "terminal_unknown"
        return "active_pending"


# LLM: 插话持久操作经 guidance 领域组件； The Gateway loop performs bounded durable repair without one thread per message. Owner
# resolution uses the stored authenticated request and never falls back to another user's store.
#   返回的 summary 是结构化事实：除各状态计数外，terminal_unknown_errors/last_terminal_unknown_error 表示
#   "试过但因坏账/IO 无法收口"的回执，调用方（派发者对账段）必须经 raise_if_input_reconcile_unsettled 上报；
#   单条回执的处理在 _reconcile_unsettled_receipt。
# 函数用途: 周期性扫描少量未完成入口回执，修复进程崩溃留下的 active 或 queued 投影，并汇总对账结果。
def reconcile_gateway_input_receipts(
    paths: GatewayPaths,
    agent: object,
    *,
    limit: int = 64,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "checked": 0, "consumed": 0, "queued": 0, "terminal_unknown": 0, "errors": 0,
        # 带对账错误的终态未知：对账"试过但因坏账/IO 失败无法收口"的回执数与最近一条结构化错误，
        # 供 raise_if_input_reconcile_unsettled 抛给循环守卫，进 loop_health 与日志，不再静默。
        "terminal_unknown_errors": 0,
        "last_terminal_unknown_error": {},
    }
    for path in _reconcile_receipt_paths(paths, limit=max(1, int(limit or 1))):
        try:
            receipt = read_gateway_input_receipt(paths, path.stem)
            if receipt is None:
                continue
            summary["checked"] += 1
            if receipt.state == "queued":
                with gateway_input_transition(paths, receipt.request_id):
                    _materialize_prepared_request_locked(paths, receipt)
                summary["queued"] += 1
                continue
            if receipt.state == "consumed":
                summary["consumed"] += 1
                continue
            _reconcile_unsettled_receipt(paths, agent, receipt, summary)
        except Exception:
            summary["errors"] += 1
        finally:
            try:
                _commit_reconcile_cursor(paths, path)
            except Exception:
                summary["errors"] += 1
    return summary


# LLM: 对账一条尚未收口（pending/active_pending/terminal_unknown）的回执：先解析目标 owner 的 agent，目标已终态时
#   拒绝未领取的 guidance 行，再走 reconcile_gateway_input_request 的结构化状态机并按返回状态计数；返回
#   terminal_unknown 时把持久化的对账错误记进 summary。异常向上抛，由调用方计入 errors。改动时同步看
#   test_steer_delivery_recovery 与 test_guidance_receipt_digest_compat。
# 函数用途: reconcile_gateway_input_receipts 的单条回执处理，把"解析目标 -> 拒绝 -> 状态迁移 -> 计数"从循环里拆出来。
def _reconcile_unsettled_receipt(
    paths: GatewayPaths, agent: object, receipt: GatewayInputReceipt, summary: dict[str, Any]
) -> None:
    from .request_worker import _resolve_request_agent

    scoped = _resolve_request_agent(agent, receipt.prepared_request)
    lifecycle = _input_target_lifecycle(paths, scoped, receipt.target_turn_id)
    if lifecycle == "terminal" and receipt.target_turn_id:
        # The owner store is now available, so a never-claimed row can
        # be rejected safely and promoted. Claimed remains unknown.
        scoped.conversation_store.guidance.recovery.reject_pending(
            receipt.target_turn_id,
            reject_reserved=True,
        )
    state = reconcile_gateway_input_request(
        paths,
        request_id=receipt.request_id,
        conversation_store=scoped.conversation_store,
        target_terminal=(lifecycle == "terminal"),
    )
    if state in summary:
        summary[state] += 1
    if state == "terminal_unknown":
        _note_terminal_unknown_error(paths, receipt.request_id, summary)


# LLM: 只看回执里持久化的结构化 reconcile_error（error_type/category/context），不解析文字；没有错误的终态未知
#   （目标已结束、无法证明已消费）是合法状态，不计入。
# 函数用途: 把一条"带对账错误的终态未知"计进 summary，并记下最近一条错误的结构化子集。
def _note_terminal_unknown_error(paths: GatewayPaths, request_id: str, summary: dict[str, Any]) -> None:
    receipt = read_gateway_input_receipt(paths, request_id)
    error = receipt.reconcile_error if receipt is not None and isinstance(receipt.reconcile_error, dict) else {}
    if not error:
        return
    summary["terminal_unknown_errors"] += 1
    summary["last_terminal_unknown_error"] = {
        "request_id": request_id,
        **{key: str(error.get(key) or "") for key in ("error_type", "category", "context")},
    }


# LLM: 对账结果里的错误必须让循环守卫看见（loop_health 计数 + 限流打印），否则坏账回执只会每轮被静默重读。
#   category 取最近一条持久化错误的类别（data_corruption/io…），而不是笼统的 programmer_bug；派发段守卫接住它后只
#   推迟对账段自己的下次到期，不影响派发。
# 类用途: 表示一轮对账里有回执因错误无法收口，携带结构化 summary。
class GatewayInputReconcileUnsettledError(RecoverableRuntimeError):
    def __init__(self, summary: dict[str, Any]) -> None:
        last = summary.get("last_terminal_unknown_error") or {}
        self.summary = dict(summary)
        self.category = str(last.get("category") or "") or "recoverable_runtime"
        super().__init__(
            "gateway input receipts could not be settled: "
            f"terminal_unknown_errors={int(summary.get('terminal_unknown_errors') or 0)} "
            f"errors={int(summary.get('errors') or 0)} "
            f"last={last.get('request_id', '')}:{last.get('error_type', '')}"
        )


# 函数用途: 对账 summary 里有错误就抛出结构化异常，交给循环守卫记账；没有就什么都不做。
def raise_if_input_reconcile_unsettled(summary: dict[str, Any]) -> None:
    if int(summary.get("terminal_unknown_errors") or 0) or int(summary.get("errors") or 0):
        raise GatewayInputReconcileUnsettledError(summary)


# LLM: HTTP status projection exposes stable ingress identity and exact target separately. It never
# treats terminal_unknown as rejection or success.
# 函数用途: 把普通消息回执转换成 TUI、飞书和 Web 共用的结构化状态。
def gateway_input_status_payload(receipt: GatewayInputReceipt) -> dict[str, object]:
    delivery_status = {
        "consumed": "accepted",
        "queued": "accepted",
        "pending": "unknown",
        "active_pending": "unknown",
        "terminal_unknown": "unknown",
    }[receipt.state]
    disposition = "queued" if receipt.state == "queued" else "active_turn_input"
    return {
        "request_id": receipt.request_id,
        "target_turn_id": receipt.target_turn_id,
        "status": (
            "queued"
            if receipt.state == "queued"
            else "steered"
            if receipt.state == "consumed"
            else "delivery_unknown"
        ),
        "disposition": disposition,
        "delivery_status": delivery_status,
        "input_state": receipt.state,
    }


# LLM: The active index is a bounded list of ingress receipt ids for one target turn.
# 函数用途: 保存一条活动回合到普通消息回执的索引引用。
def _write_active_turn_ref(paths: GatewayPaths, receipt: GatewayInputReceipt) -> None:
    turn_digest = hashlib.sha256(receipt.target_turn_id.encode("utf-8")).hexdigest()
    request_digest = hashlib.sha256(receipt.request_id.encode("utf-8")).hexdigest()
    path = paths.root / "input_receipts" / "by_turn" / turn_digest / f"{request_digest}.json"
    expected = {
        "schema_version": "gateway_input_turn_ref.v1",
        "request_id": receipt.request_id,
        "target_turn_id": receipt.target_turn_id,
        "client_message_id": receipt.client_message_id,
    }
    report = read_json_file_report(path, context="gateway.input_turn_ref.read")
    _raise_for_input_read_report(report, "gateway input turn index is unreadable")
    if report.payload and report.payload != expected:
        raise DataCorruptionError("gateway input turn index conflicts")
    if not report.payload:
        write_json_file_atomic(path, expected)


# LLM: Turn refs bound terminal work to this turn only; corrupt refs are skipped and handled by the
# bounded global reconciler instead of poisoning unrelated requests.
# 函数用途: 读取某个精确回合关联的普通消息请求 ID 列表。
def _turn_input_request_ids(paths: GatewayPaths, target_turn_id: str) -> tuple[str, ...]:
    turn_id = str(target_turn_id or "").strip()
    if not turn_id:
        return ()
    digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
    index_dir = paths.root / "input_receipts" / "by_turn" / digest
    result: list[str] = []
    for path in sorted(index_dir.glob("*.json")):
        report = read_json_file_report(path, context="gateway.input_turn_ref.read")
        request_id = str(report.payload.get("request_id") or "").strip()
        if report.load_error is None and request_id and request_id not in result:
            result.append(request_id)
    return tuple(result)


# LLM: Queue replay accepts only hot request files or the canonical terminal record as lifecycle
# authority. done/failed/response are projections; an orphan projection is corruption, never proof
# that a request finished or permission to overwrite an unrelated old result.
# 函数用途: 在入口锁内修复或创建唯一 inbox 请求，并返回本次是否新建。
def _materialize_prepared_request_locked(
    paths: GatewayPaths,
    receipt: GatewayInputReceipt,
) -> bool:
    _validate_prepared_request(receipt)
    for folder in (paths.inbox, paths.processing, paths.terminal):
        candidate = folder / f"{receipt.request_id}.json"
        if not candidate.is_file():
            continue
        report = read_json_file_report(candidate, context="gateway.input_request.read")
        _raise_for_input_read_report(report, "existing gateway input request is unreadable")
        if (
            _prepared_request_digest(report.payload) != receipt.client_input_digest
            or _prepared_payload_digest(report.payload) != receipt.prepared_payload_digest
        ):
            raise ValueError("gateway client message id was reused with different input")
        return False
    orphan_projections = (
        paths.done / f"{receipt.request_id}.json",
        paths.failed / f"{receipt.request_id}.json",
        gateway_response_path(paths, receipt.request_id),
    )
    if any(path.exists() for path in orphan_projections):
        raise DataCorruptionError(
            "gateway terminal projection exists without canonical authority"
        )
    target = paths.inbox / f"{receipt.request_id}.json"
    write_json_file_atomic(target, receipt.prepared_request)
    from ..observability.concurrency_metrics import gateway_request_enqueued

    gateway_request_enqueued()
    return True


# LLM: Prepared payload identity is server-authored and must match the receipt before any replay.
# 函数用途: 校验回执里的完整排队请求属于同一个固定请求和内容指纹。
def _validate_prepared_request(receipt: GatewayInputReceipt) -> None:
    if (
        str(receipt.prepared_request.get("id") or "") != receipt.request_id
        or _prepared_request_digest(receipt.prepared_request) != receipt.client_input_digest
        or _prepared_payload_digest(receipt.prepared_request) != receipt.prepared_payload_digest
    ):
        raise DataCorruptionError("gateway input prepared request identity is invalid")


# LLM: Guidance metadata is the exact-turn authority after an append-before-bind crash. Every
# correlation field must agree with the ingress receipt before delivery can be promoted.
# 函数用途: 校验 guidance 确实属于这条普通消息和首次选中的回合，防止跨回合误认。
def _validate_guidance_binding(
    receipt: GatewayInputReceipt,
    guidance: object,
    recovered_turn_id: str,
) -> None:
    entry = getattr(guidance, "entry", None)
    metadata = getattr(entry, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    guidance_turn_id = str(metadata.get("expected_turn_id") or "").strip()
    input_request_id = str(metadata.get("gateway_input_request_id") or "").strip()
    client_message_id = str(metadata.get("channel_message_id") or "").strip()
    actual_turn_id = str(recovered_turn_id or guidance_turn_id).strip()
    guidance_key = str(getattr(guidance, "dedupe_key", "") or "").strip()
    if (
        not actual_turn_id
        or guidance_turn_id != actual_turn_id
        or input_request_id != receipt.request_id
        or guidance_key != receipt.guidance_dedupe_key
        or (receipt.client_message_id and client_message_id != receipt.client_message_id)
        or (receipt.target_turn_id and receipt.target_turn_id != actual_turn_id)
    ):
        raise DataCorruptionError("gateway input guidance binding conflicts")


# LLM: Guidance receipts expose their exact target only through structured entry metadata; callers
# must never infer it from the current active request or queue ordering.
# 函数用途: 从 guidance 回执读取首次绑定的精确 Gateway 回合 ID。
def _guidance_turn_id(guidance: object) -> str:
    entry = getattr(guidance, "entry", None)
    metadata = getattr(entry, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    return str(metadata.get("expected_turn_id") or "").strip()


# LLM: The digest is an authenticated HTTP ingress fact copied into the prepared request metadata.
# 函数用途: 读取排队请求中的稳定客户端输入指纹。
def _prepared_request_digest(payload: dict[str, Any]) -> str:
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    return str(metadata.get("client_input_digest") or "").strip()


# LLM: Recompute immutable prepared-request facts instead of trusting the copied client digest.
# Runtime lease/status fields are excluded because queue workers legitimately add them later.
# 函数用途: 计算排队请求身份、正文、附件 refs 的服务端指纹，防止重试替换附件。
def _prepared_payload_digest(payload: dict[str, Any]) -> str:
    immutable = {
        key: payload.get(key)
        for key in (
            "id",
            "request_id",
            "kind",
            "goal",
            "metadata",
            "source",
            "user_id",
            "conversation",
            "system_task",
            "inject",
            "prompt_files",
            "input_media",
            "save",
            "include_prompt",
            "resume_context",
            "client_capabilities",
        )
        if key in payload
    }
    encoded = json.dumps(immutable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# LLM: terminal_unknown is a durable no-guess state. It preserves possible provider delivery and
# records structured reconciliation failure without creating a duplicate queued request.
#   已经是终态未知、且错误的结构化字段（error_type/category/context）没变时不重写：否则每轮对账都会刷新
#   updated_at 把同一条坏账回执改写一遍（09-29 生产实锤：一条 09-27 的插话回执每 15 秒被重写一次）。
# 函数用途: 把无法证明已消费或未消费的消息保存为终态未知；状态与错误都没变时原样保留，返回是否写了。
def _write_terminal_unknown(
    paths: GatewayPaths,
    receipt: GatewayInputReceipt,
    error: BaseException | None,
) -> bool:
    report = (
        runtime_error_report(error, context="gateway.input_receipt.reconcile")
        if error is not None
        else {}
    )
    if receipt.state == "terminal_unknown" and _same_reconcile_error(receipt.reconcile_error, report):
        return False
    updated = replace(
        receipt,
        state="terminal_unknown",
        updated_at=time.time(),
        reconcile_error=report,
    )
    write_json_file_atomic(gateway_input_receipt_path(paths, receipt.request_id), updated.to_dict())
    return True


# LLM: 只比较结构化字段，不比较 message 文字（文字里可能带路径或计数，每次都不同）；两边都空也算相同。
# 函数用途: 判断两份对账错误报告是否是同一种错误。
def _same_reconcile_error(previous: object, current: dict[str, Any]) -> bool:
    before = previous if isinstance(previous, dict) else {}
    keys = ("error_type", "category", "context")
    return all(str(before.get(key) or "") == str(current.get(key) or "") for key in keys)


# LLM: Gateway 请求文件是前台回合的权威事实，有文件时照旧只看它。没有请求文件的目标（如定时任务 srun_*）只由
#   同名会话任务的尝试读取插话：任务链接进入规范终态（task_state.conversation_task_link_is_terminal）后
#   不会再有尝试认领，才算 terminal；读不到、没有记录或未终态（例如两次尝试之间）一律 unknown。只读。
# 函数用途: 判定一条插话的目标回合是否已经结束，前台看请求文件，后台看会话任务状态。
def _input_target_lifecycle(paths: GatewayPaths, agent: object, target_turn_id: str) -> str:
    if not target_turn_id:
        return "unknown"
    lifecycle = _gateway_turn_lifecycle(paths, target_turn_id)
    if lifecycle != "unknown":
        return lifecycle
    try:
        link = agent.conversation_store.tasks.load(target_turn_id)
    except Exception:
        return "unknown"
    return "terminal" if link is not None and conversation_task_link_is_terminal(link.status) else "unknown"


# LLM: Reconciliation distinguishes a live provider turn, a recoverable inbox claim, canonical
# terminal evidence, and missing/corrupt evidence. done/failed/response projections never settle input.
# 函数用途: 在精确回合锁内读取 Gateway 生命周期，避免把待恢复请求误当作已结束。
def _gateway_turn_lifecycle(paths: GatewayPaths, target_turn_id: str) -> str:
    turn_id = str(target_turn_id or "").strip()
    if not turn_id:
        return "unknown"
    with gateway_turn_transition(paths, turn_id):
        canonical = paths.terminal / f"{turn_id}.json"
        if canonical.exists():
            report = read_json_file_report(
                canonical,
                context="gateway.input_target.terminal",
            )
            return "terminal" if report.load_error is None and report.payload else "unknown"
        processing = paths.processing / f"{turn_id}.json"
        if processing.exists():
            report = read_json_file_report(processing, context="gateway.input_target.processing")
            if report.load_error is not None or not report.payload:
                return "unknown"
            phase = str(report.payload.get("turn_phase") or "open").strip().lower()
            status = str(report.payload.get("status") or "processing").strip().lower()
            if phase == "open" and status not in {
                "done",
                "failed",
                "cancelled",
                "stopped",
            }:
                return "live"
            return "terminal"
        inbox = paths.inbox / f"{turn_id}.json"
        if inbox.exists():
            report = read_json_file_report(inbox, context="gateway.input_target.inbox")
            return "recoverable" if report.load_error is None and report.payload else "unknown"
    return "unknown"


# LLM: A durable cursor rotates through the whole receipt directory; a long prefix of completed
# inputs can never starve newer pending work. Reprocessing after a cursor-write crash is idempotent.
# 函数用途: 从上次位置继续领取有界回执扫描页，到目录末尾后循环。
def _reconcile_receipt_paths(paths: GatewayPaths, *, limit: int) -> tuple[Path, ...]:
    receipt_dir = paths.root / "input_receipts"
    candidates = sorted(receipt_dir.glob("*.json"), key=lambda item: item.name)
    if not candidates:
        return ()
    cursor_path = receipt_dir / "state" / "reconcile_cursor.json"
    report = read_json_file_report(cursor_path, context="gateway.input_reconcile_cursor.read")
    previous = str(report.payload.get("last_name") or "") if report.load_error is None else ""
    start = next(
        (index for index, path in enumerate(candidates) if path.name > previous),
        0,
    )
    ordered = candidates[start:] + candidates[:start]
    return tuple(ordered[:limit])


# LLM: Cursor advancement happens only after the selected receipt was processed. A crash may
# repeat one idempotent transition, but it can never skip the rest of a selected page.
# 函数用途: 在一条入口回执处理完以后，原子记录下一轮扫描起点。
def _commit_reconcile_cursor(paths: GatewayPaths, processed_path: Path) -> None:
    cursor_path = paths.root / "input_receipts" / "state" / "reconcile_cursor.json"
    write_json_file_atomic(
        cursor_path,
        {
            "schema_version": "gateway_input_reconcile_cursor.v1",
            "last_name": processed_path.name,
            "updated_at": time.time(),
        },
    )


__all__ = [
    "GatewayInputReceipt",
    "bind_gateway_input_active_locked",
    "gateway_input_receipt_path",
    "gateway_input_status_payload",
    "gateway_input_transition",
    "load_or_prepare_gateway_input_locked",
    "queue_gateway_input_locked",
    "read_gateway_input_receipt",
    "reconcile_gateway_input_receipts",
    "reconcile_gateway_input_request",
    "settle_gateway_inputs_for_turn",
]
