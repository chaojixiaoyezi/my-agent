# LLM: 原输入回执/完整摘要是唯一归属事实；C queued只传末writer和原reader阻塞选项，默认保Count0，busy/unknown不假进度，同步终态/steer回归。
# 模块用途: 保存并结算原普通消息的去向；处理来源失效时保原欠账，不重签输入或重放业务。
from __future__ import annotations

"""LLM: Own the durable disposition of one authenticated Gateway input.
实际C的首次owner/绑定版本在这里的完整摘要之前冻结；已有回执直接重放，不能以候选metadata覆盖摘要。
物化与准入须比较原回执的typed owner、owner_id和完整摘要，禁止由变更队列自洽重签归属。

模块用途: 把普通消息在“当前回合、下一轮队列、已消费、终态未知”之间的去向保存为
唯一回执；按原 guidance 的请求/任务目标对账，不用同名运输请求代替持久任务状态。
"""

import copy
import hashlib
import json
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..common.json_io import check_io_deadline
from ..common.request_execution_ports import (
    RequestExecutionOutcomeUncertainError,
    raise_if_transport_uncertain,
)
from ..conversation.store_guidance_recovery import (
    CLOSEOUT_REPLAY_QUEUED,
    CLOSEOUT_REPLAY_UNAVAILABLE,
    TurnEndSettlement,
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


# LLM: v3完整运输字段仅定位，不授执行；本记录仍唯一输入去向，v2编码不加字段；mutable快照由各原writer末CAS核。
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
    transport: dict[str, Any] | None = None

    # LLM: v2无运输字段保持原编码；v3完整保存同一次冻结运输，不丢定位字段或借投影授执行权。
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
        if self.transport is not None:
            from .input_transport import validated_input_transport

            payload["schema_version"] = "gateway_input_receipt.v3"
            payload["transport"] = validated_input_transport(self.transport)
        if self.reconcile_error:
            payload["reconcile_error"] = dict(self.reconcile_error)
        if self.migration:
            payload["migration"] = dict(self.migration)
        return payload

    # LLM: 旧v1仅显式修复；v2不得带运输，v3必须完整codec通过，坏运输不能静默降级为未绑定。
    # 函数用途: 校验并恢复普通消息回执，同时明确记录旧版字段含义的迁移决定。
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GatewayInputReceipt:
        schema = str(data.get("schema_version") or "").strip()
        transport = None
        if schema == "gateway_input_receipt.v3":
            from .input_transport import validated_input_transport

            transport = validated_input_transport(data.get("transport"))
        elif "transport" in data:
            raise DataCorruptionError("gateway input transport requires v3 receipt")
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
        elif schema in {_INPUT_RECEIPT_SCHEMA, "gateway_input_receipt.v3"}:
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
            transport=copy.deepcopy(transport),
        )


# LLM: Receipt filenames reveal no channel message id and are stable across retries.
# 函数用途: 返回一条 Gateway 普通消息唯一回执文件的位置。
def gateway_input_receipt_path(paths: GatewayPaths, request_id: str) -> Path:
    return paths.root / "input_receipts" / f"{str(request_id or '').strip()}.json"


# LLM: All state changes for one stable ingress id share the same cross-process lock.
# LLM: 默认仍原T；恢复同期source只在其真实目标/关联receipt T取得后核controller/lease，不持C门等待T，不改变原receipt权威。
# 函数用途: 返回普通消息原T短门，恢复仅接同一issued元数据来源。
def gateway_input_transition(paths: GatewayPaths, request_id: str):
    from .multiprocess_processing_recovery import (
        current_processing_recovery,
        processing_metadata_scope,
    )

    if current_processing_recovery() is None:
        return gateway_turn_transition(paths, request_id)
    return processing_metadata_scope(paths, request_id)



# LLM: 四类原canonical来源/退出未结及真实GlobalWriteError必须优先于普通坏账分支；只按实际type，不以同码字符串授信，不造claim/entered。
# 函数用途: 在输入best-effort catch中同对象保留未结责任，Count0普通错误沿原分流。
def _raise_input_transport_uncertain(error):
    from .multiprocess_global_write import GlobalWriteError
    from .multiprocess_input_reconcile import _UNCERTAIN

    if isinstance(error, (*_UNCERTAIN, GlobalWriteError)):
        raise error
    raise_if_transport_uncertain(error)


# LLM: 入口读失败分流：锁忙（含取锁身份核对重试耗尽）是暂时竞争，按可重试的锁失败抛出；只有
#   其余 load_error 才是数据损坏。调用方绝不把锁忙拒成“损坏”。
# 函数用途: 按报告里的锁忙/读取错误分流抛出，锁忙保持可重试语义。
def _raise_for_input_read_report(report, corruption_message: str) -> None:
    if report.lock_busy:
        raise BlockingIOError("gateway input lock is busy")
    if report.load_error is not None:
        raise DataCorruptionError(corruption_message)


# LLM: 先读原receipt再裁决首次prepare；实际C的owner/同次绑定版本必须在完整摘要前冻结，重放不能热读绑定或重签metadata。
#   Count0沿原请求不新增I/O；同id异digest在任何后续副作用前拒绝，原完整prepared摘要不得用候选字段回填绕过。
#   原8参公共入口保Count0默认资源路径；C通过同文件数据bundle入口明确选择原try资源，不把数据bundle当许可。
# 函数用途: 在原入口锁内保存首次完整输入；已有回执只复用原身份，旧版缺载荷修复才重新走可信准备。
def load_or_prepare_gateway_input_locked(
    paths: GatewayPaths,
    *,
    request_id: str,
    client_input_digest: str,
    client_message_id: str,
    guidance_dedupe_key: str,
    prepared_request: dict[str, Any],
    prepare_request: Callable | None = None,
) -> tuple[GatewayInputReceipt, bool]:
    request = GatewayInputPreparation(request_id, client_input_digest, client_message_id,
        guidance_dedupe_key, prepared_request, prepare_request)
    return load_or_prepare_gateway_input_request_locked(paths, request)


# LLM: 只聚合原准备入口的身份与完整载荷，不携写许可；C资源选项留在实际callee，不让数据对象成为来源。
# 类用途: 将同一次输入的原身份、正文和准备回调一起传入唯一实现，保原Count0公共签名。
@dataclass(frozen=True)
class GatewayInputPreparation:
    request_id: str
    client_input_digest: str
    client_message_id: str
    guidance_dedupe_key: str
    prepared_request: dict[str, Any]
    prepare_request: Callable | None = field(default=None, repr=False, compare=False)


# LLM: 此处是原load/repair实现，调用者持原T；C仅追加同一次固定writer与try-reader，数据bundle不授owner/执行权。
# 函数用途: 用同一套原完整摘要校验准备或读取输入，默认资源选项保Count0原行为。
def load_or_prepare_gateway_input_request_locked(paths: GatewayPaths, request: GatewayInputPreparation,
        *, commit_receipt: Callable | None = None, read_blocking: bool = True) -> tuple[GatewayInputReceipt, bool]:
    request_id, client_input_digest = request.request_id, request.client_input_digest
    client_message_id, guidance_dedupe_key = request.client_message_id, request.guidance_dedupe_key
    prepared_request, prepare_request = request.prepared_request, request.prepare_request
    path = gateway_input_receipt_path(paths, request_id)
    options = {} if read_blocking else {"blocking": False}
    report = read_json_file_report(path, context="gateway.input_receipt.read", **options)
    _raise_for_input_read_report(report, "gateway input receipt is unreadable")
    if not report.payload:
        if path.exists():
            raise DataCorruptionError("gateway input receipt is empty or invalid")
        receipt = GatewayInputReceipt(
            request_id=request_id,
            client_input_digest=client_input_digest,
            client_message_id=client_message_id,
            state="pending",
            prepared_request={},
            prepared_payload_digest="",
            guidance_dedupe_key=guidance_dedupe_key,
            updated_at=time.time(),
        )
        return _commit_fresh_gateway_input(paths, receipt, prepared_request, prepare_request, commit_receipt), True
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
            guidance_dedupe_key=receipt.guidance_dedupe_key or guidance_dedupe_key,
            updated_at=time.time(),
        )
        receipt = _commit_fresh_gateway_input(paths, receipt, prepared_request, prepare_request, commit_receipt)
    _validate_prepared_request(receipt)
    return receipt, False


# LLM: 调用者持原T；真正fresh/旧缺载荷修复才进入C末门，callback只能准备宿主快照，完整摘要校验先于唯一receipt写入。
#   可选writer仅由原HTTP固定元数据source同步使用；None完整保原Count0 writer，不在这里捕获/授予来源。
# 函数用途: 在同一真实C来源窗口内提交首次完整输入，准备异常不留下半份回执，Count0保持原writer。
def _commit_fresh_gateway_input(paths, receipt, payload, prepare_request, commit_receipt=None):
    from .control_plane import prepare_coordinator_input

    # LLM: 仅由上方原C/Count0同步调用；不另选owner或改摘要，写入前必须核request/client digest的完整一致性。
    # 函数用途: 用已经冻结的完整输入原子保存唯一回执，写失败沿原异常上抛。
    def commit(prepared):
        updated = replace(receipt, prepared_request=dict(prepared),
                          prepared_payload_digest=_prepared_payload_digest(prepared), updated_at=time.time())
        _validate_prepared_request(updated)
        if commit_receipt is None:
            write_json_file_atomic(gateway_input_receipt_path(paths, receipt.request_id), updated.to_dict())
            return updated
        return commit_receipt(updated)

    return prepare_coordinator_input(paths, payload, prepare_request=prepare_request, commit=commit)


# LLM: Reads never infer delivery from queue filenames. The receipt remains the only projection
# exposed to HTTP, TUI and adapter reconciliation.
#   C显式False只控制原JSON取锁等待；原锁忙仍是BlockingIOError，不当不存在或数据损坏，默认True不变。
# 函数用途: 读取原消息回执；C短门不等普通锁，缺失和损坏沿原分类。
def read_gateway_input_receipt(
    paths: GatewayPaths,
    request_id: str,
    *,
    blocking: bool = True,
) -> GatewayInputReceipt | None:
    path = gateway_input_receipt_path(paths, request_id)
    read_options = {} if blocking else {'blocking': False}
    report = read_json_file_report(path, context="gateway.input_receipt.read", **read_options)
    _raise_for_input_read_report(report, "gateway input receipt is unreadable")
    if not report.payload:
        if path.exists():
            raise DataCorruptionError("gateway input receipt is empty or invalid")
        return None
    if report.payload.get("schema_version") not in {_INPUT_RECEIPT_SCHEMA, "gateway_input_receipt.v3"}:
        raise DataCorruptionError("legacy gateway input receipt requires same-input retry migration")
    receipt = GatewayInputReceipt.from_dict(report.payload)
    _validate_prepared_request(receipt)
    return receipt


# LLM: 调用方已持原T；只读原InputReceipt比较不可变完整输入和typed owner，缺回执明确沿旧无receipt合同，不补账、不重签、不从队列自洽归属授权。
# 函数用途: 在准入前核对已有原回执与队列投影；返回是否存在回执，损坏/漂移原样拒绝。
def require_gateway_prepared_identity(paths: GatewayPaths, payload: dict[str, Any]) -> bool:
    receipt = read_gateway_input_receipt(paths, str(payload.get("id") or ""))
    if receipt is None:
        return False
    if not _prepared_input_matches(receipt, payload):
        raise ValueError("gateway client message id was reused with different input")
    return True


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


# LLM: 请求终态只结算原 request guidance；task guidance 按自己的生命周期对账。邮箱锁必须先于入口锁释放。
#   联测前后台交接及 test_steer_closeout_replay，不能因同名运输结束拒绝活动后台任务的消息。
# 带reconcile_error的unknown仍属失败欠账；逐回执 helper 只更新原计数，不能改变异常隔离或投递状态。
# 函数用途: 请求结束后按原投递目标结算消息，保留仍属持久任务的插话与读取错误。
# LLM: 普通坏账按原计数隔离，但同一typed运输未知须上抛给原请求/P3，不写成已完成输入结算。
# LLM: 输入catch优先保实际canonical来源/退出未结与GlobalWriteError同对象；同码普通异常仍沿本函数原隔离/轮转，不补claim或释放。
def settle_gateway_inputs_for_turn(
    paths: GatewayPaths,
    *,
    target_turn_id: str,
    conversation_store: object | None,
) -> dict[str, int]:
    summary = {"consumed": 0, "queued": 0, "active_pending": 0, "terminal_unknown": 0, "errors": 0}
    # 没有入口请求号的插话在收口时的两级结局计数：steer_replay_queued 已排成备用下一轮、steer_replay_unavailable 已提示重发。
    summary["steer_replay_queued"] = 0
    summary["steer_replay_unavailable"] = 0
    if conversation_store is None:
        return summary
    try:
        conversation_store.guidance.recovery.reject_pending(
            target_turn_id,
            reject_reserved=True,
            target=("request", target_turn_id),
        )
    except Exception as exc:
        _raise_input_transport_uncertain(exc)
        summary["errors"] += 1
    for request_id in _turn_input_request_ids(paths, target_turn_id, summary):
        try:
            _settle_bound_input_receipt(paths, conversation_store, request_id, summary)
        except Exception as exc:
            _raise_input_transport_uncertain(exc)
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
# LLM: 读取和逐条结算的普通异常仍隔离；typed来源未知不能变空回执/普通错误，也不能借机排新输入。
# LLM: 输入catch优先保实际canonical来源/退出未结与GlobalWriteError同对象；同码普通异常仍沿本函数原隔离/轮转，不补claim或释放。
def _settle_steer_closeout(
    paths: GatewayPaths,
    conversation_store: object,
    turn_id: str,
    summary: dict[str, int],
) -> None:
    if not turn_id:
        return
    try:
        receipts = _closeout_replay_receipts(conversation_store, turn_id)
    except Exception as exc:
        _raise_input_transport_uncertain(exc)
        summary["errors"] += 1
        return
    for receipt in receipts:
        try:
            _settle_one_steer_closeout(paths, conversation_store, receipt, summary)
        except Exception as exc:
            _raise_input_transport_uncertain(exc)
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
#   宿主owner仅从原guidance冻结字段经同一规范codec恢复，不从sender/路径/新绑定推断；旧Count0缺字段仍沿原显式迁移边界。
#   gateway_input_transport与expected_turn_id同属旧turn派生定位，重排不携回旧fence/运输字段。
# 函数用途: 把插话回执的 entry 转成可重放的 Gateway 排队请求。
def _replay_request_payload(receipt: object, request_id: str, digest: str) -> dict[str, Any]:
    entry = getattr(receipt, "entry", None)
    metadata = getattr(entry, "metadata", None)
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    metadata.pop("expected_turn_id", None)
    metadata.pop("gateway_input_request_id", None)
    metadata.pop("gateway_input_transport", None)
    metadata["client_input_digest"] = digest
    payload = {
        "id": request_id,
        "request_id": request_id,
        "kind": "steer_closeout_replay",
        "goal": str(getattr(entry, "message", "") or ""),
        "user_id": str(getattr(entry, "sender", "") or ""),
        "metadata": metadata,
    }
    from dataclasses import asdict

    from .request_owner_binding import REQUEST_OWNER_KEY, frozen_request_owner

    owner = frozen_request_owner({REQUEST_OWNER_KEY: metadata.get(REQUEST_OWNER_KEY)})
    if owner is not None:
        payload[REQUEST_OWNER_KEY] = asdict(owner)
    return payload


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
# LLM: 空集合只代表原普通不可读语义，不能吞原typed权限未结；该事件仍交请求出口保责任。
# LLM: 输入catch优先保实际canonical来源/退出未结与GlobalWriteError同对象；同码普通异常仍沿本函数原隔离/轮转，不补claim或释放。
def _closeout_replay_receipts(conversation_store: object, turn_id: str) -> list[object]:
    try:
        return list(conversation_store.guidance.recovery.closeout_replay_receipts(turn_id))
    except Exception as exc:
        _raise_input_transport_uncertain(exc)
        return []


# LLM: 插话持久操作经 guidance 领域组件； Guidance discovery and correlation have one implementation for initial HTTP routing and
# background reconciliation. A readable miss proves no active side effect; read failure stays unknown.
# 函数用途: 查找普通消息对应的 guidance 回执并校验入口、客户端消息和精确回合完全一致。
def gateway_input_guidance_binding(
    receipt: GatewayInputReceipt,
    conversation_store: object,
) -> tuple[object, str] | None:
    if receipt.transport is not None:
        from .input_guidance_payload import original_input_guidance

        row = original_input_guidance(conversation_store, receipt)
        return (row, receipt.transport['target']['turn_id']) if row is not None else None
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
# LLM: 状态迁移始终在原输入T；v3冻结运输缺guidance仍pending，不能按旧v2缺席queue；恢复关联owner/actor/thread由原source核，typed拒绝不改unknown回执。
# LLM: 输入catch优先保实际canonical来源/退出未结与GlobalWriteError同对象；同码普通异常仍沿本函数原隔离/轮转，不补claim或释放。
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
            _raise_input_transport_uncertain(exc)
            _write_terminal_unknown(paths, receipt, exc)
            return "terminal_unknown"
        if guidance is None:
            if receipt.transport is not None:
                return receipt.state
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
# LLM: typed运输未知立即保原cursor位置并上抛，不以finally推进假进度；普通单条坏账仍沿原轮转，不让一条错误饿死后续。
# LLM: 输入catch优先保实际canonical来源/退出未结与GlobalWriteError同对象；同码普通异常仍沿本函数原隔离/轮转，不补claim或释放。
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
        advance_cursor = True
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
        except RequestExecutionOutcomeUncertainError:
            advance_cursor = False
            raise
        except Exception as exc:
            _raise_input_transport_uncertain(exc)
            summary["errors"] += 1
        finally:
            if advance_cursor:
                try:
                    _commit_reconcile_cursor(paths, path)
                except Exception as exc:
                    _raise_input_transport_uncertain(exc)
                    summary["errors"] += 1
    return summary


# LLM: 对账先解析原 owner 和已经校验绑定的 guidance；目标类型/编号只取原账，不能从同名请求文件猜测。
#   目标已终态时拒绝未领取的 guidance 行，再走 reconcile_gateway_input_request 的结构化状态机并按返回状态计数；返回
#   terminal_unknown 时把持久化的对账错误记进 summary。异常向上抛，由调用方计入 errors。改动时同步看
#   test_steer_delivery_recovery 与 test_guidance_receipt_digest_compat。
# 函数用途: reconcile_gateway_input_receipts 的单条回执处理，把"解析目标 -> 拒绝 -> 状态迁移 -> 计数"从循环里拆出来。
def _reconcile_unsettled_receipt(
    paths: GatewayPaths, agent: object, receipt: GatewayInputReceipt, summary: dict[str, Any]
) -> None:
    from .request_worker import _resolve_request_agent

    scoped = _resolve_request_agent(agent, receipt.prepared_request)
    state = _reconcile_bound_input(paths, scoped.conversation_store, receipt)
    if state in summary:
        summary[state] += 1
    if state == "terminal_unknown":
        _note_terminal_unknown_error(paths, receipt.request_id, summary)


# LLM: 目标来自已校验 guidance；按 Gateway T -> guidance M -> task 锁复查终态，再仅结算该目标。
#   释放这些锁后才取入口锁，避免与模型提交及插话入口锁反转。读取损坏由原入口对账写出错误事实。
# LLM: 目标T由原通用短锁producer取得，恢复post阶段不沿用已持旧T；M/task顺序与入口锁分段原样。
# 函数用途: 周期对账与请求收尾结算同一原目标，先释放目标T/M再取入口T。
# LLM: 目标T改由同一原输入/恢复producer取得；同typed未知不补绑定、不得转读失败后继续排队。
# LLM: 输入catch优先保实际canonical来源/退出未结与GlobalWriteError同对象；同码普通异常仍沿本函数原隔离/轮转，不补claim或释放。
def _reconcile_bound_input(paths: GatewayPaths, store: object, receipt: GatewayInputReceipt) -> str:
    try:
        binding = gateway_input_guidance_binding(receipt, store)
    except Exception as exc:
        _raise_input_transport_uncertain(exc)
        # 下方原对账入口会持入口锁记录读取错误；此处证据不足不得先拒绝消息。
        binding = None
    entry = getattr(binding[0], "entry", None) if binding is not None else None
    target_type = str(getattr(entry, "target_type", "") or "")
    target_id = str(getattr(entry, "target_id", "") or "")
    turn_id = binding[1] if binding is not None else ""
    lifecycle = "unknown"
    if turn_id and target_type in {"request", "task"} and target_id:
        with ExitStack() as locks:
            locks.enter_context(gateway_input_transition(paths, turn_id))
            locks.enter_context(store.guidance.ledger.turn_guard(turn_id))
            if target_type == "task":
                locks.enter_context(store.tasks.transition_guard(target_id))
            lifecycle = _input_target_lifecycle(paths, store, target_id, target_type=target_type)
            if lifecycle == "terminal":
                store.guidance.recovery.settle_turn_locked(
                    turn_id, TurnEndSettlement(reject_reserved=True, target=(target_type, target_id)),
                )
    return reconcile_gateway_input_request(
        paths,
        request_id=receipt.request_id,
        conversation_store=store,
        target_terminal=(lifecycle == "terminal"),
    )



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


# LLM: 原by_turn仅查找投影不是权限；C可在transport前显式传target，不改canonical binding/ACK；默认参数完整保Count0，固定末门只试原锁。
# 函数用途: 保存一条活动回合到普通消息回执的索引引用。
# LLM: 可选deadline随原reader/writer到锁后末提交，不将预ref当授权；Count0 None保持原字节。
# 函数用途: 写原turn查找投影，过期或锁忙保原欠账。
def _write_active_turn_ref(paths: GatewayPaths, receipt: GatewayInputReceipt, *, blocking: bool = True,
                           target_turn_id: str | None = None, deadline: float | None = None) -> None:
    turn_id = receipt.target_turn_id if target_turn_id is None else target_turn_id
    turn_digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
    request_digest = hashlib.sha256(receipt.request_id.encode("utf-8")).hexdigest()
    path = paths.root / "input_receipts" / "by_turn" / turn_digest / f"{request_digest}.json"
    expected = {
        "schema_version": "gateway_input_turn_ref.v1",
        "request_id": receipt.request_id,
        "target_turn_id": turn_id,
        "client_message_id": receipt.client_message_id,
    }
    check_io_deadline(deadline)
    options = {} if blocking and deadline is None else {"blocking": blocking, **({} if deadline is None else {"deadline": deadline})}
    report = read_json_file_report(path, context="gateway.input_turn_ref.read", **options)
    _raise_for_input_read_report(report, "gateway input turn index is unreadable")
    if report.payload and report.payload != expected:
        raise DataCorruptionError("gateway input turn index conflicts")
    if not report.payload:
        write_json_file_atomic(path, expected, **options)


# LLM: W只读取自身turn哈希目录；引用只定位canonical，不授执行；锁忙/坏结构不能当空，原Count0宽读函数不改。
# 函数用途: 严格列出一个原回合已发布的input查找引用，不扫描共享receipt域。
# LLM: 每个本turn ref读前后核同一纯deadline；过期抛资源忙，由原source处理，不能返回空列表。
# 函数用途: 在原定位目录检查全部ref，普通读延迟后不越过预算。
def transport_turn_input_references(paths: GatewayPaths, target_turn_id: str, *, deadline: float | None = None) -> tuple[dict, ...]:
    turn_digest = hashlib.sha256(target_turn_id.encode('utf-8')).hexdigest()
    folder = paths.root / 'input_receipts' / 'by_turn' / turn_digest
    result = []
    for path in sorted(folder.glob('*.json')):
        check_io_deadline(deadline)
        report = read_json_file_report(path, context='gateway.input_turn_ref.read', blocking=False, **({} if deadline is None else {'deadline': deadline}))
        check_io_deadline(deadline)
        _raise_for_input_read_report(report, 'gateway input turn index is unreadable')
        row = report.payload
        request_id = row.get('request_id')
        if (set(row) != {'schema_version', 'request_id', 'target_turn_id', 'client_message_id'}
                or row.get('schema_version') != 'gateway_input_turn_ref.v1'
                or row.get('target_turn_id') != target_turn_id or type(request_id) is not str or not request_id
                or '/' in request_id or '\\' in request_id or type(row.get('client_message_id')) is not str
                or path.name != hashlib.sha256(request_id.encode('utf-8')).hexdigest() + '.json'):
            raise DataCorruptionError('gateway input turn reference is invalid')
        result.append(row)
    check_io_deadline(deadline)
    return tuple(result)


# LLM: Turn refs只发现本回合输入，不授权执行；逐引用读取错误记入原summary，不能把发现失败当空账。
# 函数用途: 返回精确回合的可读请求ID，并保留坏引用的补交欠账。
# LLM: 原by_turn只是查找投影；坏行计原errors，不能因为漏读输入就宣布结算追平，不新建索引权威。
def _turn_input_request_ids(paths: GatewayPaths, target_turn_id: str, summary: dict[str, int]) -> tuple[str, ...]:
    turn_id = str(target_turn_id or "").strip()
    if not turn_id:
        return ()
    digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
    index_dir = paths.root / "input_receipts" / "by_turn" / digest
    result: list[str] = []
    for path in sorted(index_dir.glob("*.json")):
        report = read_json_file_report(path, context="gateway.input_turn_ref.read")
        request_id = str(report.payload.get("request_id") or "").strip()
        if report.load_error is not None or not request_id:
            summary["errors"] += 1
            continue
        if request_id not in result:
            result.append(request_id)
    return tuple(result)



# LLM: Queue replay accepts only hot request files or the canonical terminal record as lifecycle
# authority. done/failed/response are projections; an orphan projection is corruption, never proof
# that a request finished or permission to overwrite an unrelated old result.
#   顶层owner与owner_id须另比原receipt，不能因原摘要未含顶层身份就接受队列自行换属。
#   C元数据消费者可传同源末writer；只改变实际原子写边界，None保原路径，不能借参数重prepare或覆盖canonical。
#   read_blocking只透传原候选JSON reader；C在原短门内显式不等普通文件锁，busy分类不变。
# 函数用途: 在入口锁内修复唯一inbox；可选不等原候选读锁，不改变原摘要或canonical校验。
def _materialize_prepared_request_locked(
    paths: GatewayPaths,
    receipt: GatewayInputReceipt,
    *,
    write_request: Callable | None = None,
    read_blocking: bool = True,
) -> bool:
    _validate_prepared_request(receipt)
    read_options = {} if read_blocking else {'blocking': False}
    for folder in (paths.inbox, paths.processing, paths.terminal):
        candidate = folder / f"{receipt.request_id}.json"
        if not candidate.is_file():
            continue
        report = read_json_file_report(candidate, context="gateway.input_request.read", **read_options)
        _raise_for_input_read_report(report, "existing gateway input request is unreadable")
        if not _prepared_input_matches(receipt, report.payload):
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
    writer = write_json_file_atomic if write_request is None else write_request
    writer(target, receipt.prepared_request)
    from ..observability.concurrency_metrics import gateway_request_enqueued

    gateway_request_enqueued()
    return True


# LLM: 原摘要/协议不换版；原宿主metadata断言若存在必须与顶层typed owner一致，旧无断言仍按原完整摘要验证，不补默认身份。
# 函数用途: 校验回执里的完整排队请求属于同一个固定请求和内容指纹。
def _validate_prepared_request(receipt: GatewayInputReceipt) -> None:
    from .request_owner_binding import REQUEST_OWNER_KEY, frozen_request_owner

    metadata = receipt.prepared_request.get("metadata")
    if (isinstance(metadata, dict) and metadata.get(REQUEST_OWNER_KEY) is not None
            and frozen_request_owner(receipt.prepared_request)
            != frozen_request_owner({REQUEST_OWNER_KEY: metadata[REQUEST_OWNER_KEY]})):
        raise DataCorruptionError("gateway input prepared owner identity is invalid")
    if (
        str(receipt.prepared_request.get("id") or "") != receipt.request_id
        or _prepared_request_digest(receipt.prepared_request) != receipt.client_input_digest
        or _prepared_payload_digest(receipt.prepared_request) != receipt.prepared_payload_digest
    ):
        raise DataCorruptionError("gateway input prepared request identity is invalid")


# LLM: 唯一比较基准是原receipt已保存的输入；owner由main codec解码，owner_id逐类型/值核对，完整metadata/actor/media仍用原摘要，运行字段不参与。
# 函数用途: 检查队列或终态投影是否仍属于同一原输入，不修改任何记录。
def _prepared_input_matches(receipt: GatewayInputReceipt, payload: dict[str, Any]) -> bool:
    from .request_owner_binding import frozen_request_owner

    original = receipt.prepared_request
    return (frozen_request_owner(payload) == frozen_request_owner(original)
            and type(payload.get("owner_id")) is type(original.get("owner_id"))
            and payload.get("owner_id") == original.get("owner_id")
            and _prepared_request_digest(payload) == receipt.client_input_digest
            and _prepared_payload_digest(payload) == receipt.prepared_payload_digest)


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


# LLM: 生命周期只按已验证 guidance 的 target_type/target_id 查询；task 与 request 可同名且状态不同。
#   task 非终态、缺失或不可读均不证明可结算，也不授予执行权；新输入准入仍由控制入口核对 claim。
#   本函数只读；修改时联测 test_gateway_input_target_lifecycle 与 test_steer_delivery_recovery。
# 函数用途: 分别读取原请求或持久任务的生命周期，避免前台结束误拒后台插话。
# LLM: 普通任务账不可读仍unknown，typed原执行源未知须保持同对象上抛，不能以unknown状态吞掉请求责任。
# LLM: 输入catch优先保实际canonical来源/退出未结与GlobalWriteError同对象；同码普通异常仍沿本函数原隔离/轮转，不补claim或释放。
def _input_target_lifecycle(
    paths: GatewayPaths, store: object, target_id: str, *, target_type: str,
) -> str:
    if not target_id:
        return "unknown"
    if target_type == "request":
        return _gateway_turn_lifecycle_locked(paths, target_id)
    if target_type != "task":
        return "unknown"
    try:
        link = store.tasks.load(target_id)
    except Exception as exc:
        _raise_input_transport_uncertain(exc)
        return "unknown"
    return "terminal" if link is not None and conversation_task_link_is_terminal(link.status) else "unknown"


# LLM: 调用方已经持精确 Gateway 回合锁；本函数不重入文件锁，只读规范终态、处理中或待恢复的请求事实。
# 函数用途: 在原请求锁内读取生命周期，避免把待恢复请求误当作已结束。
def _gateway_turn_lifecycle_locked(paths: GatewayPaths, target_turn_id: str) -> str:
    turn_id = str(target_turn_id or "").strip()
    if not turn_id:
        return "unknown"
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
#   C只传本轮原来源的末writer；延期可记examined但不是settled，typed失源调用方不推进。None沿原Count0原子写。
# 函数用途: 在一条入口回执处理完以后，原子记录下一轮扫描起点。
def _commit_reconcile_cursor(paths: GatewayPaths, processed_path: Path, *, write_cursor: Callable | None = None) -> None:
    cursor_path = paths.root / "input_receipts" / "state" / "reconcile_cursor.json"
    writer = write_json_file_atomic if write_cursor is None else write_cursor
    writer(
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
    "require_gateway_prepared_identity",
    "settle_gateway_inputs_for_turn",
]


# LLM: 只在原终态结算循环内调用；复用原回执对账和重读，不新建投递状态。异常由外层逐条隔离。
# 函数用途: 结算一个已绑定输入并统计未知回执的真实读取错误。
def _settle_bound_input_receipt(paths, conversation_store, request_id, summary):
    receipt = read_gateway_input_receipt(paths, request_id)
    if receipt is None:
        return
    state = _reconcile_bound_input(paths, conversation_store, receipt)
    if state not in summary:
        if state:
            summary["errors"] += 1
        return
    summary[state] += 1
    if state == "terminal_unknown":
        updated = read_gateway_input_receipt(paths, request_id)
        if updated is None or updated.reconcile_error:
            summary["errors"] += 1
