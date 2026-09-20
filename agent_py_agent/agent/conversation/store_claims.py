# LLM: 前后台执行租约仅存于原 claim 文件；领取、续租、同任务恢复和终态提交保持原跨进程原子更新。
# 模块用途: 显式管理执行归属及旧租约归档，供 Gateway、调度器和恢复入口使用，不拥有线程或模型状态。
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..gateway_parts.daemon_metadata import build_process_identity, process_identity_is_live
from ..gateway_parts.io import update_json_file_atomic
from ..runtime_errors import DataCorruptionError, runtime_error_report
from ..settings.defaults import default_config_value
from .models import ConversationThread, new_id
from .store_io import LEDGER_ARCHIVE_DIR, archive_ledger_file, now
from .store_layout import ConversationStorage

_STORE_LOGGER = logging.getLogger("agent.conversation.store")


# LLM: 保留原租约数字归一规则，不能把解析失败转换成新的生命周期状态。
# 函数用途: 将持久时间数字转换为浮点数，无效值沿用原零值处理。
def float_value(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


_FINISH_STATUSES = {"finished", "failed", "cancelled"}
_INVALID_FINISH_STATUS = "invalid_status"


# LLM: Host-owned claim inputs; recovery affinity adds no permissions or new lease identity.
# 类用途: 表达执行租约及原请求恢复归属，由同一 claim 文件持久保存。
@dataclass(frozen=True)
class BackgroundClaimPayload:
    thread_id: str
    reason: str
    current: float
    lease: int
    task_id: str = ""
    claim_scope_id: str = ""
    owner_process: dict[str, object] = field(default_factory=dict)
    recover_same_task_only: bool = False


# LLM: Emit one canonical claim; omit disabled recovery affinity for ordinary background callers.
# 函数用途: 构造待落盘的租约；恢复专属标记只对宿主明确绑定的请求生效。
def _new_claim(payload: BackgroundClaimPayload) -> dict[str, Any]:
    return {
        "schema_version": "background_run_claim.v1",
        "claim_id": new_id("bgclaim"),
        "thread_id": payload.thread_id,
        "claim_scope_id": payload.claim_scope_id or payload.thread_id,
        "task_id": payload.task_id,
        "reason": str(payload.reason or ""),
        "status": "running",
        "phase": "claimed",
        "started_at": payload.current,
        "heartbeat_at": payload.current,
        "expires_at": payload.current + payload.lease,
        "owner_process": dict(payload.owner_process),
        "acquisition": {"reason": "new_claim"},
        "takeover": {"allowed": False, "reason": "claim_running"},
        **({"recover_same_task_only": True} if payload.recover_same_task_only else {}),
    }


# LLM: 缺省 TTL 仍读既有配置；显式值按原正整数边界归一。
# 函数用途: 计算当前租约长度，不写配置或更改默认值。
def _claim_lease_seconds(value: object) -> int:
    if value is None or value == "":
        value = default_config_value("background_claim_ttl_seconds")
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return max(1, int(default_config_value("background_claim_ttl_seconds")))


# LLM: 只接受协议中明确终态；不从自然语言推断成功或失败。
# 函数用途: 校验调用方提交的 claim 终态。
def _finish_status(value: object) -> str:
    status = str(value or "").strip()
    return status if status in _FINISH_STATUSES else _INVALID_FINISH_STATUS


# LLM: 异常只转诊断字段，不由错误正文决定执行或接管状态。
# 函数用途: 整理租约结束时的错误事实，保持原持久格式。
def _error_payload(value: object) -> dict[str, Any]:
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": str(value)}
    if isinstance(value, dict):
        return {
            "type": str(value.get("type") or value.get("error_type") or ""),
            "message": str(value.get("message") or value.get("error") or ""),
        }
    text = str(value or "").strip()
    return {"type": "", "message": text} if text else {}


# LLM: 接管提示仅由结构化终态派生，不改变租约或授予执行权。
# 函数用途: 为结束回执生成可恢复性说明。
def _takeover_payload(status: str) -> dict[str, Any]:
    if status == "failed":  # CLAIM_STATUS_FAILED
        return {"allowed": True, "reason": "runtime_failed"}
    if status == "cancelled":
        return {"allowed": False, "reason": "user_interrupted"}
    if status == _INVALID_FINISH_STATUS:
        return {"allowed": True, "reason": "runtime_invalid_status"}
    return {"allowed": False, "reason": "run_finished"}


# LLM: 前一租约身份、宿主和错误作为接管证据保留，不能替代当前 claim。
# 函数用途: 保存前一份租约的有限摘要，便于追踪真实接管原因。
def _previous_claim_summary(data: dict[str, Any], current: float) -> dict[str, Any]:
    if not data:
        return {}
    status = str(data.get("status") or "")
    expires_at = float_value(data.get("expires_at"))
    return {
        "claim_id": str(data.get("claim_id") or ""),
        "status": status,
        "reason": str(data.get("reason") or ""),
        "task_id": str(data.get("task_id") or ""),
        "heartbeat_at": float_value(data.get("heartbeat_at")),
        "expires_at": expires_at,
        "expired": bool(expires_at and expires_at <= current),
        "owner_process": dict(data.get("owner_process"))
        if isinstance(data.get("owner_process"), dict)
        else {},
        "last_error": _error_payload(data.get("last_error")),
        "takeover": data.get("takeover")
        if isinstance(data.get("takeover"), dict)
        else _takeover_payload(status),
    }


# LLM: 接管原因只来自旧 claim 的结构化状态与租约时钟；同进程域死进程的提前接管由调用方
# 单独标为 owner_process_stale，跨进程域/旧格式记录仍必须等 TTL。
# 函数用途: 给每次成功获取租约写清“新建、到期或终态接手”的机器原因。
def _claim_acquisition_reason(data: dict[str, Any], current: float) -> str:
    if not data:
        return "new_claim"
    status = str(data.get("status") or "")
    if status == "running" and float_value(data.get("expires_at")) <= current:
        return "lease_expired"
    return f"previous_{status or 'unknown'}"


# LLM: 损坏必须以 load_error 返回，不能伪装成正常空记录后抢占执行。
# 函数用途: 读取原 claim JSON 及明确损坏事实，不写文件。
def _read_claim_report(path: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if not path.exists():
        return {}, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {}, _claim_load_error(exc, path)
    if not isinstance(payload, dict):
        return {}, _claim_load_error(
            DataCorruptionError(f"background claim must be a JSON object: {path}"), path
        )
    return payload, None


# LLM: 使用既有错误分类及原路径，不复制另一份业务状态。
# 函数用途: 为读取租约失败附加定位信息。
def _claim_load_error(exc: BaseException, path: Path) -> dict[str, Any]:
    report = runtime_error_report(exc, context="conversation.background_claim.read")
    report["path"] = str(path)
    return report


# LLM: Serialize each claim in the owner store; death/TTL cannot transfer recoverable foreground
# ownership to another task. Terminal cleanup is exact-task CAS, not a directory or quality lock.
# 类用途: 管理前后台共用执行租约，确保重启恢复和请求结束都能正确交接。
class ClaimStore:
    # LLM: 与所有领域共用唯一 storage；线程能力只用于存在性验证，不建立运行或租约副本。
    # 函数用途: 连接执行租约与原线程读取入口，初始化不读写持久文件。
    def __init__(
        self,
        storage: ConversationStorage,
        *,
        require_thread: Callable[[str], ConversationThread],
        load_thread: Callable[[str], ConversationThread | None],
    ) -> None:
        self.storage = storage
        self._require_thread = require_thread
        self._load_thread = load_thread


    # LLM: Atomically acquire a claim; only the original host-bound task can resume a pinned claim.
    # 函数用途: 领取执行权；普通后台维持租约接管，尚未收尾的前台请求不因超时被另一执行者抢走。
    def acquire(self, request: dict) -> dict[str, Any] | None:
        thread_id = str(request.get("thread_id") or "")
        thread = self._require_thread(thread_id)
        claim_scope_id = str(request.get("claim_scope_id") or thread.thread_id).strip()
        current = now(request.get("now"))
        lease = _claim_lease_seconds(request.get("lease_seconds"))
        task_id = str(request.get("task_id") or "").strip()
        recover_same_task_only = request.get("recover_same_task_only") is True
        if recover_same_task_only and not task_id:
            raise ValueError("恢复专属执行租约必须绑定明确的 task_id")
        claim = _new_claim(
            BackgroundClaimPayload(
                thread_id=thread.thread_id,
                reason=str(request.get("reason") or ""),
                current=current,
                lease=lease,
                task_id=task_id,
                claim_scope_id=claim_scope_id,
                owner_process=build_process_identity(),
                recover_same_task_only=recover_same_task_only,
            )
        )
        claimed = False

        # LLM: Called under the claim JSON lock; affinity is checked before TTL/dead-owner takeover.
        # 函数用途: 原子核对旧租约归属，再按真实租约和宿主状态决定是否替换。
        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal claimed
            if (
                data.get("status") == "running"
                and data.get("recover_same_task_only") is True
                and (not recover_same_task_only or data.get("task_id") != task_id)
            ):
                return data
            active = (
                str(data.get("status") or "") == "running"
                and float_value(data.get("expires_at")) > current
            )
            owner_stale = active and process_identity_is_live(data.get("owner_process")) is False
            if active and not owner_stale:
                claimed = False
                return data
            claimed = True
            acquisition_reason = (
                "owner_process_stale" if owner_stale else _claim_acquisition_reason(data, current)
            )
            return {
                **claim,
                "acquisition": {"reason": acquisition_reason},
                "previous_claim": _previous_claim_summary(data, current),
            }

        updated = update_json_file_atomic(
            self.storage.background_claim_path(claim_scope_id),
            updater,
        )
        return updated if claimed else None

    # LLM: 保留显式 load_error 包装，调用方不能把不可读 claim 当成已释放。
    # 函数用途: 读取指定范围的执行租约。
    def load(
        self,
        thread_id: str,
        *,
        claim_scope_id: str = "",
    ) -> dict[str, Any]:
        claim, load_error = self.load_report(
            thread_id,
            claim_scope_id=claim_scope_id,
        )
        return claim if not load_error else {"load_error": load_error}

    # LLM: 先验证原线程存在，再读取同范围 claim，不改变租约。
    # 函数用途: 向需要区分空记录与坏账的调用方返回数据及错误。
    def load_report(
        self,
        thread_id: str,
        *,
        claim_scope_id: str = "",
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        self._require_thread(str(thread_id or ""))
        selected_scope = str(claim_scope_id or thread_id or "").strip()
        return _read_claim_report(self.storage.background_claim_path(selected_scope))

    # LLM: 只续原 running claim ID，线程不可读或身份冲突保持原失败边界。
    # 函数用途: 延长当前执行租约，并在原文件锁内校验归属。
    def renew(self, request: dict) -> dict[str, Any] | None:
        thread_id = str(request.get("thread_id") or "")
        claim_scope_id = str(request.get("claim_scope_id") or thread_id).strip()
        claim_id = str(request.get("claim_id") or "")
        # 续租只作用于 claim 租约文件（按 thread_id 定位），不需要线程对象。若线程此刻不可读
        # （边缘/竞态：外部清理、长跑中线程消失、极端下 store 根不一致），续租已无意义——返回 None
        # 让后台心跳线程按既有 `renewed is None → 停机` 契约优雅收尾，绝不抛 KeyError 裸崩 daemon 线程。
        if self._load_thread(thread_id) is None:
            # 真机诊断锚点：把此刻解析出的线程文件绝对路径打出来（=该 store 的会话库根），
            # 一旦真出现「续租时线程缺失」，日志即可证实/排除 supervisor 与请求路的 store 根是否不一致。
            _STORE_LOGGER.warning(
                "background claim renew skipped: conversation thread not readable thread=%s path=%s",
                thread_id,
                self.storage.thread_path(thread_id),
            )
            return None
        current = now(request.get("now"))
        lease = _claim_lease_seconds(request.get("lease_seconds"))
        renewed = False

        # LLM: 在原 claim 文件锁内检查 ID 和 running 状态，迟到心跳不能续新租约。
        # 函数用途: 只更新仍归本执行者的心跳和过期时间。
        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal renewed
            if (
                str(data.get("claim_id") or "") != str(claim_id or "")
                or str(data.get("status") or "") != "running"
            ):
                renewed = False
                return data
            renewed = True
            return {**data, "heartbeat_at": current, "expires_at": current + lease}

        updated = update_json_file_atomic(
            self.storage.background_claim_path(claim_scope_id),
            updater,
        )
        return updated if renewed else None

    # LLM: Finish by exact claim ID, or by host-published exact task at Gateway terminal commit.
    # Task-only cleanup requires recovery affinity and running state; it cannot close a later task.
    # 函数用途: 原子释放租约并保留真实终态；重启后的失败收尾不依赖线程仍能读取。
    def finish(self, request: dict) -> dict[str, Any] | None:
        thread_id = str(request.get("thread_id") or "")
        claim_scope_id = str(request.get("claim_scope_id") or thread_id).strip()
        claim_id = str(request.get("claim_id") or "")
        expected_task_id = str(request.get("expected_task_id") or "").strip()
        finish_recovery_task = request.get("recover_same_task_only") is True
        if not claim_id and not (expected_task_id and finish_recovery_task):
            return None
        # 收尾（释放租约/记失败事实）只作用于 claim 文件，不依赖线程仍可读。这条在 `_run_with_heartbeat`
        # 的 finally 里跑：若线程在长跑中变不可读还硬 `_require_thread`，会二次抛 KeyError 盖掉真正的 run
        # 错误、并再次崩后台清理。改为对已存在的 claim 文件收尾；无 claim 文件则无可收尾直接返回 None。
        claim_path = self.storage.background_claim_path(claim_scope_id)
        if not claim_path.exists():
            return None
        current = now(request.get("now"))
        raw_status = request.get("status")
        status = _finish_status(raw_status)
        error = _error_payload(request.get("error"))
        if status == _INVALID_FINISH_STATUS and not error:
            error = {
                "type": "InvalidBackgroundClaimStatus",
                "message": f"unsupported background claim finish status: {raw_status!r}",
            }
        task_id = str(request.get("task_id") or "")
        runtime_facts = (
            request.get("runtime_facts") if isinstance(request.get("runtime_facts"), dict) else {}
        )
        finished = False

        # LLM: Select and finish under one file lock; missing IDs and late task cleanup are no-ops.
        # 函数用途: 在同一原子更新中核对租约身份，避免迟到的旧请求结束新请求的执行权。
        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal finished
            if claim_id and data.get("claim_id") != claim_id:
                return data
            if expected_task_id and (
                data.get("task_id") != expected_task_id or data.get("thread_id") != thread_id
            ):
                return data
            if finish_recovery_task and (
                data.get("recover_same_task_only") is not True or data.get("status") != "running"
            ):
                return data
            finished = True
            payload = {
                **data,
                "status": status,
                "phase": status,
                "finished_at": current,
                "heartbeat_at": current,
                "takeover": _takeover_payload(status),
            }
            if runtime_facts:
                payload["last_runtime_facts"] = runtime_facts
            if task_id:
                payload["task_id"] = task_id
            if error:
                payload["last_error"] = error
            return payload

        updated = update_json_file_atomic(claim_path, updater)
        return updated if finished else None

    # LLM: 只归档已终态且超过原保留期的 claim；坏账、运行中租约和缺少结束时间的记录保持原位。
    # 函数用途: 在既有维护周期移动旧租约及其锁文件，不启动独立后台任务或影响执行权。
    def archive_stale(self, *, current: float, retention_seconds: float) -> int:
        archived_claims = 0
        for path in sorted(self.storage.background_claims_dir.glob("*.json")):
            claim, error = _read_claim_report(path)
            if error is not None or not claim:
                continue
            if str(claim.get("status") or "") not in _FINISH_STATUSES:
                continue
            finished_at = float(claim.get("finished_at") or 0.0)
            if finished_at <= 0 or current - finished_at <= retention_seconds:
                continue
            if archive_ledger_file(path, self.storage.background_claims_dir.parent / LEDGER_ARCHIVE_DIR / "claims"):
                archived_claims += 1
        return archived_claims
