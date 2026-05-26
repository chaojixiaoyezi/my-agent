# LLM: Background run claims are local leases with heartbeat renewal.
# 模块用途: 为后台主代理 tick 提供抢占、续约和释放语义。

from __future__ import annotations

from typing import Any

from ..gateway_parts.io import update_json_file_atomic
from .models import new_id
from .store_common import float_value
from .store_common import now as current_time
from .store_progress import ConversationProgressStore


class ConversationClaimStore(ConversationProgressStore):
    def claim_background_run(self, request: dict) -> dict[str, Any] | None:
        thread_id = str(request.get("thread_id") or "")
        thread = self._require_thread(thread_id)
        current = current_time(request.get("now"))
        lease = max(1, int(request.get("lease_seconds") or 900))
        claim = _new_claim(thread.thread_id, str(request.get("reason") or ""), current, lease)
        claimed = False

        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal claimed
            if str(data.get("status") or "") == "running" and float_value(data.get("expires_at")) > current:
                claimed = False
                return data
            claimed = True
            return claim

        updated = update_json_file_atomic(self._background_claim_path(thread.thread_id), updater)
        return updated if claimed else None

    def renew_background_run_claim(self, request: dict) -> dict[str, Any] | None:
        thread_id = str(request.get("thread_id") or "")
        claim_id = str(request.get("claim_id") or "")
        self._require_thread(thread_id)
        current = current_time(request.get("now"))
        lease = max(1, int(request.get("lease_seconds") or 900))
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
        finished = False

        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal finished
            if str(data.get("claim_id") or "") != str(claim_id or ""):
                finished = False
                return data
            finished = True
            return {**data, "status": "finished", "finished_at": current, "heartbeat_at": current}

        updated = update_json_file_atomic(self._background_claim_path(thread_id), updater)
        return updated if finished else None


def _new_claim(thread_id: str, reason: str, current: float, lease: int) -> dict[str, Any]:
    return {"schema_version": "background_run_claim.v1", "claim_id": new_id("bgclaim"), "thread_id": thread_id, "reason": str(reason or ""), "status": "running", "started_at": current, "heartbeat_at": current, "expires_at": current + lease}
