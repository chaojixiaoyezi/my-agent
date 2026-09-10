# LLM: Gateway 队列与租约的结构化状态是唯一调度事实；类型导入不能引入运行时循环依赖。
# 模块用途: 扫描持久队列、领取与恢复请求，协调租约和文件状态，不从展示文案推断任务状态。
"""Queue iteration and state management for gateway processing.

The service owns queue scanning, file state transitions, and lease management.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_py_agent.agent.core import SimpleAgent

import json
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from ..runtime_errors import DataCorruptionError, runtime_error_report
from .io import (
    GATEWAY_REQUEST_FINGERPRINT_SCHEMA,
    GatewayJsonReadReport,
    gateway_request_fingerprint,
    gateway_response_path,
    gateway_turn_transition,
    read_json_file_report,
    write_json_file_atomic,
)
from .lease_service import is_heartbeat_alive_for_request
from .logging import GatewayIndexPayloadOptions, _index_gateway_payload
from .paths import GatewayPaths, gateway_paths
from .recovery import (
    _gateway_request_attempts,
    terminalize_gateway_request_file,
)
from .status_rendering import (
    GatewayRunningReport,
    gateway_running,
    gateway_running_report,
    render_gateway_status,
    wait_for_gateway_running,
)

_CLAIM_LOCK = threading.Lock()


@dataclass(frozen=True)
class GatewayIndexRebuildReport:
    indexed_count: int
    load_errors: list[dict[str, Any]]


# LLM: A claimed path is insufficient because request ids are reused after recovery. This value
# carries the immutable execution fence from the T-locked claim into worker and error closeout.
# 类用途: 保存一次 Gateway 认领的文件、执行 ID、租约代次和所有者，防止旧 worker 误收口。
@dataclass(frozen=True)
class GatewayClaim:
    path: Path
    request_id: str
    execution_attempt_id: str
    lease_epoch: int
    lease_owner: str

    @property
    def stem(self) -> str:
        return self.path.stem

    @property
    def name(self) -> str:
        return self.path.name


# LLM: Worker closeout must distinguish a stale execution fence from a committed terminal fact,
# corruption, and retryable IO. Callers may publish projections only for committed dispositions.
# 类用途: 表示一次请求归档的结构化结果，避免旧 worker 把失败原因混成一个 False。
class GatewayArchiveDisposition(str, Enum):
    COMMITTED = "committed"
    ALREADY_COMMITTED = "already_committed"
    STALE_CLAIM = "stale_claim"
    CONFLICT = "conflict"
    RETRYABLE_IO = "retryable_io"


# LLM: The terminal path is present only when canonical authority is known to exist. Error text is
# operator evidence and never changes state-machine routing by prose parsing.
# 类用途: 携带归档状态、唯一终态路径和诊断信息，供 worker 决定是否发布结果投影。
@dataclass(frozen=True)
class GatewayArchiveResult:
    disposition: GatewayArchiveDisposition
    terminal_path: Path | None = None
    error: str = ""

    @property
    def terminal_committed(self) -> bool:
        return self.disposition in {
            GatewayArchiveDisposition.COMMITTED,
            GatewayArchiveDisposition.ALREADY_COMMITTED,
        }


def rebuild_gateway_index(agent: SimpleAgent) -> int:
    paths = gateway_paths(agent)
    return rebuild_gateway_index_report(agent, paths).indexed_count


def rebuild_gateway_index_report(agent: SimpleAgent, paths: GatewayPaths | None = None) -> GatewayIndexRebuildReport:
    resolved_paths = paths or gateway_paths(agent)
    history = _rebuild_gateway_history_index_report(agent, resolved_paths)
    requests = _rebuild_gateway_request_file_index_report(agent, resolved_paths)
    responses = _rebuild_gateway_response_index_report(agent, resolved_paths)
    return GatewayIndexRebuildReport(
        indexed_count=history.indexed_count + requests.indexed_count + responses.indexed_count,
        load_errors=[*history.load_errors, *requests.load_errors, *responses.load_errors],
    )


def _rebuild_gateway_history_index(agent: SimpleAgent, paths: GatewayPaths) -> int:
    return _rebuild_gateway_history_index_report(agent, paths).indexed_count


def _rebuild_gateway_history_index_report(agent: SimpleAgent, paths: GatewayPaths) -> GatewayIndexRebuildReport:
    count = 0
    load_errors: list[dict[str, Any]] = []
    if not paths.history.exists():
        return GatewayIndexRebuildReport(count, load_errors)
    try:
        lines = paths.history.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return GatewayIndexRebuildReport(
            count,
            [_gateway_index_load_error(paths.history, exc, "gateway.index.history.read")],
        )
    for line_number, line in enumerate(lines, start=1):
        payload_report = _payload_from_history_line_report(line, paths.history, line_number)
        if payload_report.load_error:
            load_errors.append(payload_report.load_error)
        payload = payload_report.payload
        if not payload:
            continue
        response_path = gateway_response_path(paths, str(payload.get("id") or ""))
        if _index_gateway_payload(agent, payload, GatewayIndexPayloadOptions(response_path=response_path)):
            count += 1
    return GatewayIndexRebuildReport(count, load_errors)


def _payload_from_history_line(line: str) -> dict:
    return _payload_from_history_line_report(line).payload


def _payload_from_history_line_report(
    line: str,
    path: Path | None = None,
    line_number: int = 0,
) -> GatewayJsonReadReport:
    if not line.strip():
        return GatewayJsonReadReport({})
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        return GatewayJsonReadReport(
            {},
            _gateway_index_load_error(path, exc, "gateway.index.history.read", line_number=line_number),
        )
    if isinstance(payload, dict):
        return GatewayJsonReadReport(payload)
    return GatewayJsonReadReport(
        {},
        _gateway_index_load_error(
            path,
            ValueError(f"gateway history row is {type(payload).__name__}, expected object"),
            "gateway.index.history.read",
            line_number=line_number,
        ),
    )


def _rebuild_gateway_request_file_index(agent: SimpleAgent, paths: GatewayPaths) -> int:
    return _rebuild_gateway_request_file_index_report(agent, paths).indexed_count


def _rebuild_gateway_request_file_index_report(agent: SimpleAgent, paths: GatewayPaths) -> GatewayIndexRebuildReport:
    count = 0
    load_errors: list[dict[str, Any]] = []
    for request_path in _iter_gateway_request_files(paths):
        indexed, errors = _index_gateway_request_file_report(agent, paths, request_path)
        load_errors.extend(errors)
        if indexed:
            count += 1
    return GatewayIndexRebuildReport(count, load_errors)


def _iter_gateway_request_files(paths: GatewayPaths):
    for folder in (paths.inbox, paths.processing, paths.done, paths.failed):
        yield from sorted(folder.glob("*.json"))


def _index_gateway_request_file(agent: SimpleAgent, paths: GatewayPaths, request_path: Path) -> bool:
    indexed, _ = _index_gateway_request_file_report(agent, paths, request_path)
    return indexed


def _index_gateway_request_file_report(
    agent: SimpleAgent,
    paths: GatewayPaths,
    request_path: Path,
) -> tuple[bool, list[dict[str, Any]]]:
    load_errors: list[dict[str, Any]] = []
    payload_report = read_json_file_report(request_path, context="gateway.index.request.read")
    if payload_report.load_error:
        load_errors.append(payload_report.load_error)
    payload = payload_report.payload
    if not payload:
        return False, load_errors
    request_id = str(payload.get("id") or request_path.stem)
    response_path = gateway_response_path(paths, request_id)
    response_report = read_json_file_report(response_path, context="gateway.index.response_for_request.read")
    if response_report.load_error:
        load_errors.append(response_report.load_error)
    response_payload = response_report.payload
    merged = {**payload, **response_payload} if response_payload else payload
    indexed = _index_gateway_payload(
        agent,
        merged,
        GatewayIndexPayloadOptions(request_path=request_path, response_path=response_path),
    )
    return indexed, load_errors


def _rebuild_gateway_response_index(agent: SimpleAgent, paths: GatewayPaths) -> int:
    return _rebuild_gateway_response_index_report(agent, paths).indexed_count


def _rebuild_gateway_response_index_report(agent: SimpleAgent, paths: GatewayPaths) -> GatewayIndexRebuildReport:
    count = 0
    load_errors: list[dict[str, Any]] = []
    for response_path in sorted(paths.responses.glob("*.json")):
        payload_report = read_json_file_report(response_path, context="gateway.index.response.read")
        if payload_report.load_error:
            load_errors.append(payload_report.load_error)
        payload = payload_report.payload
        if not payload:
            continue
        if _index_gateway_payload(agent, payload, GatewayIndexPayloadOptions(response_path=response_path)):
            count += 1
    return GatewayIndexRebuildReport(count, load_errors)


def _gateway_index_load_error(
    path: Path | None,
    exc: BaseException,
    context: str,
    *,
    line_number: int = 0,
) -> dict[str, Any]:
    report = runtime_error_report(exc, context=context)
    if path is not None:
        report["path"] = str(path)
    if line_number:
        report["line_number"] = line_number
    return report


def ensure_gateway_folders(paths: GatewayPaths) -> None:
    paths.inbox.mkdir(parents=True, exist_ok=True)
    paths.processing.mkdir(parents=True, exist_ok=True)
    paths.done.mkdir(parents=True, exist_ok=True)
    paths.failed.mkdir(parents=True, exist_ok=True)
    paths.terminal.mkdir(parents=True, exist_ok=True)
    paths.responses.mkdir(parents=True, exist_ok=True)


def claim_request(paths: GatewayPaths, request_path: Path) -> GatewayClaim | None:
    from .logging import _report_gateway_side_effect_error

    processing_path = paths.processing / request_path.name
    claim: GatewayClaim | None = None
    with _CLAIM_LOCK:
        with gateway_turn_transition(paths, request_path.stem):
            canonical = paths.terminal / request_path.name
            if not request_path.exists() or processing_path.exists() or canonical.exists():
                return None
            try:
                request_path.replace(processing_path)
            except OSError as exc:
                _report_gateway_side_effect_error("claim_gateway_request", request_path.stem, exc)
                return None
            report = read_json_file_report(
                processing_path,
                context="gateway.claim.request.read",
            )
            if report.load_error is not None or not report.payload:
                _report_gateway_side_effect_error(
                    "claim_gateway_request",
                    request_path.stem,
                    OSError(f"gateway claim payload is unreadable: {request_path.stem}"),
                )
                claim = GatewayClaim(processing_path, processing_path.stem, "", 0, "")
            else:
                try:
                    claimed_payload = _gateway_claim_payload(
                        report.payload,
                        processing_path,
                    )
                    write_json_file_atomic(processing_path, claimed_payload)
                except OSError as exc:
                    _report_gateway_side_effect_error(
                        "claim_gateway_request",
                        request_path.stem,
                        exc,
                    )
                    try:
                        if processing_path.exists() and not request_path.exists():
                            processing_path.replace(request_path)
                    except OSError as rollback_exc:
                        _report_gateway_side_effect_error(
                            "rollback_gateway_request_claim",
                            request_path.stem,
                            rollback_exc,
                        )
                    return None
                claim = GatewayClaim(
                    path=processing_path,
                    request_id=str(claimed_payload.get("id") or processing_path.stem),
                    execution_attempt_id=str(
                        claimed_payload.get("execution_attempt_id") or ""
                    ).strip(),
                    lease_epoch=_gateway_claim_epoch(claimed_payload),
                    lease_owner=str(claimed_payload.get("lease_owner") or "").strip(),
                )
    from ..observability.concurrency_metrics import gateway_request_claimed

    gateway_request_claimed()  # §6-A 认领计数(enqueued 涨而这个不跟=worker 槽饿死排队)
    return claim


# LLM: Inbox-to-processing and the immutable execution fence are one T-locked transition. The
# provider worker never observes an unfenced processing row that recovery could reclaim first.
# 函数用途: 为刚认领的请求写入唯一执行 ID、递增租约代次并开始首个心跳时间。
def _gateway_claim_payload(payload: dict, processing_path: Path) -> dict:
    from .daemon_metadata import build_process_identity

    claimed = dict(payload)
    identity_error = _gateway_request_identity_error(payload, processing_path)
    now = time.time()
    try:
        previous_epoch = max(0, int(claimed.get("lease_epoch") or 0))
    except (TypeError, ValueError):
        previous_epoch = 0
    try:
        created_at = float(claimed.get("created_at") or claimed.get("submitted_at") or 0)
    except (TypeError, ValueError):
        created_at = 0.0
    if created_at > 0:
        from ..observability.concurrency_metrics import record_gateway_queue_wait

        record_gateway_queue_wait(now - created_at)
    claim_id = f"gateway-attempt-{uuid.uuid4().hex}"
    claimed.update(
        {
            # 文件名是队列、锁和终态归档共同使用的唯一请求标识。发现冲突时保留
            # 诊断事实，但绝不能把 payload 内的另一个 ID 带进 worker。
            "id": processing_path.stem,
            "status": "processing",
            "turn_phase": "open",
            "attempts": _gateway_request_attempts(claimed) + 1,
            "lease_owner": claim_id,
            "lease_started_at": now,
            "lease_heartbeat_at": now,
            "lease_epoch": previous_epoch + 1,
            "execution_attempt_id": claim_id,
            "lease_process_identity": build_process_identity(),
            "updated_at": now,
        }
    )
    if "request_id" in claimed:
        claimed["request_id"] = processing_path.stem
    if identity_error is not None:
        claimed["request_identity_error"] = identity_error
    claimed["request_fingerprint_schema"] = GATEWAY_REQUEST_FINGERPRINT_SCHEMA
    claimed["request_fingerprint"] = gateway_request_fingerprint(
        claimed,
        processing_path.stem,
    )
    return claimed


# LLM: Queue filename is the request/turn authority used by T locks and terminal archives. A
# payload may never redirect an already claimed file to another id; missing/conflicting ids are
# persisted as a structured fail-closed marker for the worker to terminalize without provider IO.
# 函数用途: 校验队列文件名与请求正文中的 ID 是否完全一致，并生成可审计的损坏报告。
def _gateway_request_identity_error(payload: dict, processing_path: Path) -> dict | None:
    path_request_id = processing_path.stem
    payload_id = payload.get("id")
    payload_request_id = payload.get("request_id")
    declared_id = payload_id.strip() if isinstance(payload_id, str) else ""
    declared_request_id = (
        payload_request_id.strip() if isinstance(payload_request_id, str) else ""
    )
    invalid_id = declared_id != path_request_id
    invalid_alias = payload_request_id is not None and declared_request_id != path_request_id
    if not invalid_id and not invalid_alias:
        return None
    return {
        "schema_version": "gateway_request_identity_error.v1",
        "context": "gateway.claim.request.identity",
        "message": "gateway request filename and payload identity conflict",
        "path_request_id": path_request_id,
        "payload_id": payload_id,
        "payload_request_id": payload_request_id,
    }


# LLM: The fenced claim result never trusts Python truthiness for persisted numeric state.
# 函数用途: 从认领后的请求中读取非负租约代次，坏值按零处理。
def _gateway_claim_epoch(payload: dict) -> int:
    try:
        return max(0, int(payload.get("lease_epoch") or 0))
    except (TypeError, ValueError):
        return 0


def archive_request(
    paths: GatewayPaths,
    processing_path: Path,
    target_folder: Path,
    request_id: str,
    *,
    conversation_store: object | None = None,
    terminal_response: dict | None = None,
    expected_execution_attempt_id: str = "",
    expected_lease_epoch: int = 0,
    expected_lease_heartbeat_at: float | None = None,
) -> GatewayArchiveResult:
    from .logging import _report_gateway_side_effect_error

    canonical = paths.terminal / f"{request_id}.json"
    already_committed = canonical.is_file()
    try:
        terminal_path = terminalize_gateway_request_file(
            paths,
            processing_path,
            target_folder,
            request_id,
            conversation_store=conversation_store,
            terminal_response=terminal_response,
            expected_execution_attempt_id=expected_execution_attempt_id,
            expected_lease_epoch=expected_lease_epoch,
            expected_lease_heartbeat_at=expected_lease_heartbeat_at,
        )
        return GatewayArchiveResult(
            (
                GatewayArchiveDisposition.ALREADY_COMMITTED
                if already_committed
                else GatewayArchiveDisposition.COMMITTED
            ),
            terminal_path=terminal_path,
        )
    except InterruptedError as exc:
        return GatewayArchiveResult(
            GatewayArchiveDisposition.STALE_CLAIM,
            error=str(exc),
        )
    except FileNotFoundError as exc:
        disposition = (
            GatewayArchiveDisposition.STALE_CLAIM
            if expected_execution_attempt_id or expected_lease_epoch
            else GatewayArchiveDisposition.RETRYABLE_IO
        )
        if disposition is GatewayArchiveDisposition.RETRYABLE_IO:
            _report_gateway_side_effect_error("archive_gateway_request", request_id, exc)
        return GatewayArchiveResult(disposition, error=str(exc))
    except (DataCorruptionError, RuntimeError) as exc:
        _report_gateway_side_effect_error("archive_gateway_request_conflict", request_id, exc)
        return GatewayArchiveResult(
            GatewayArchiveDisposition.CONFLICT,
            error=str(exc),
        )
    except OSError as exc:
        _report_gateway_side_effect_error("archive_gateway_request", request_id, exc)
        return GatewayArchiveResult(
            GatewayArchiveDisposition.RETRYABLE_IO,
            error=str(exc),
        )
