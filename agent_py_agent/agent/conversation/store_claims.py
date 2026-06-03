
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..gateway_parts.io import update_json_file_atomic
from ..runtime_errors import DataCorruptionError, runtime_error_report
from ..settings.defaults import default_config_value
from .models import new_id
from .store_common import float_value
from .store_common import now as current_time
from .store_progress import ConversationProgressStore


@dataclass(frozen=True)
class BackgroundClaimPayload:
    thread_id: str
    reason: str
    current: float
    lease: int
    task_id: str = ""


class ConversationClaimStore(ConversationProgressStore):
    def claim_background_run(self, request: dict) -> dict[str, Any] | None:
        thread_id = str(request.get("thread_id") or "")
        thread = self._require_thread(thread_id)
        current = current_time(request.get("now"))
        lease = _claim_lease_seconds(request.get("lease_seconds"))
        claim = _new_claim(BackgroundClaimPayload(
            thread_id=thread.thread_id,
            reason=str(request.get("reason") or ""),
            current=current,
            lease=lease,
            task_id=str(request.get("task_id") or ""),
        ))
        claimed = False

        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal claimed
            if str(data.get("status") or "") == "running" and float_value(data.get("expires_at")) > current:
                claimed = False
                return data
            claimed = True
            return {**claim, "previous_claim": _previous_claim_summary(data, current)}

        updated = update_json_file_atomic(self._background_claim_path(thread.thread_id), updater)
        return updated if claimed else None

    def load_background_run_claim(self, thread_id: str) -> dict[str, Any]:
        claim, load_error = self.load_background_run_claim_report(thread_id)
        return claim if not load_error else {"load_error": load_error}

    def load_background_run_claim_report(self, thread_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
        self._require_thread(str(thread_id or ""))
        return _read_claim_report(self._background_claim_path(str(thread_id or "")))

    def renew_background_run_claim(self, request: dict) -> dict[str, Any] | None:
        thread_id = str(request.get("thread_id") or "")
        claim_id = str(request.get("claim_id") or "")
        self._require_thread(thread_id)
        current = current_time(request.get("now"))
        lease = _claim_lease_seconds(request.get("lease_seconds"))
        renewed = False

        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal renewed
            if str(data.get("claim_id") or "") != str(claim_id or "") or str(data.get("status") or "") != "running":
                renewed = False
                return data
            renewed = True
            return {**data, "heartbeat_at": current, "expires_at": current + lease}

        updated = update_json_file_atomic(self._background_claim_path(thread_id), updater)
        return updated if renewed else None

    def finish_background_run(self, request: dict) -> dict[str, Any] | None:
        thread_id = str(request.get("thread_id") or "")
        claim_id = str(request.get("claim_id") or "")
        self._require_thread(thread_id)
        current = current_time(request.get("now"))
        status = _finish_status(request.get("status"))
        error = _error_payload(request.get("error"))
        task_id = str(request.get("task_id") or "")
        runtime_facts = request.get("runtime_facts") if isinstance(request.get("runtime_facts"), dict) else {}
        finished = False

        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal finished
            if str(data.get("claim_id") or "") != str(claim_id or ""):
                finished = False
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

        updated = update_json_file_atomic(self._background_claim_path(thread_id), updater)
        return updated if finished else None


def _new_claim(payload: BackgroundClaimPayload) -> dict[str, Any]:
    return {
        "schema_version": "background_run_claim.v1",
        "claim_id": new_id("bgclaim"),
        "thread_id": payload.thread_id,
        "task_id": payload.task_id,
        "reason": str(payload.reason or ""),
        "status": "running",
        "phase": "claimed",
        "started_at": payload.current,
        "heartbeat_at": payload.current,
        "expires_at": payload.current + payload.lease,
        "takeover": {"allowed": False, "reason": "claim_running"},
    }


def _claim_lease_seconds(value: object) -> int:
    if value is None or value == "":
        value = default_config_value("background_claim_ttl_seconds")
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return max(1, int(default_config_value("background_claim_ttl_seconds")))


def _finish_status(value: object) -> str:
    status = str(value or "finished").strip().lower()
    return status if status in {"finished", "failed", "cancelled"} else "finished"


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


def _takeover_payload(status: str) -> dict[str, Any]:
    if status == "failed":
        return {"allowed": True, "reason": "runtime_failed"}
    if status == "cancelled":
        return {"allowed": True, "reason": "runtime_cancelled"}
    return {"allowed": False, "reason": "run_finished"}


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
        "last_error": _error_payload(data.get("last_error")),
        "takeover": data.get("takeover") if isinstance(data.get("takeover"), dict) else _takeover_payload(status),
    }


def _read_claim_report(path: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if not path.exists():
        return {}, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {}, _claim_load_error(exc, path)
    if not isinstance(payload, dict):
        return {}, _claim_load_error(DataCorruptionError(f"background claim must be a JSON object: {path}"), path)
    return payload, None


def _claim_load_error(exc: BaseException, path: Path) -> dict[str, Any]:
    report = runtime_error_report(exc, context="conversation.background_claim.read")
    report["path"] = str(path)
    return report
