
from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..gateway_parts.io import read_json_file, update_json_file_atomic
from ..io.jsonl import append_jsonl
from ..runtime_errors import runtime_error_report
from .models import new_id
from .models_guidance import GuidanceEntry
from .store_common import now as current_time
from .store_common import read_jsonl_report, safe_file_stem
from .store_observations import ConversationObservationStore


class ConversationGuidanceStore(ConversationObservationStore):
    def append_guidance(self, request: dict[str, Any]) -> GuidanceEntry:
        target_type = normalize_guidance_target_type(request.get("target_type") or request.get("type"))
        target_id = str(request.get("target_id") or request.get("id") or "").strip()
        message = str(request.get("message") or request.get("body") or request.get("prompt") or "").strip()
        if not target_type or not target_id:
            raise ValueError("target_type and target_id are required")
        if not message:
            raise ValueError("guidance message is required")
        entry = GuidanceEntry(
            guidance_id=new_id("guidance"),
            target_type=target_type,
            target_id=target_id,
            message=message,
            sender=str(request.get("sender") or "").strip(),
            priority=str(request.get("priority") or "normal").strip() or "normal",
            delivery=str(request.get("delivery") or "next_turn").strip() or "next_turn",
            created_at=current_time(request.get("now")),
            metadata=request.get("metadata") if isinstance(request.get("metadata"), dict) else {},
        )
        append_jsonl(self._guidance_path(target_type, target_id), entry.to_dict(), sort_keys=True)
        return entry

    def recent_guidance(
        self,
        target_type: str,
        target_id: str,
        *,
        limit: int = 20,
        include_delivered: bool = True,
    ) -> list[GuidanceEntry]:
        entries, _errors = self.recent_guidance_report(
            target_type,
            target_id,
            limit=limit,
            include_delivered=include_delivered,
        )
        return entries

    def recent_guidance_report(
        self,
        target_type: str,
        target_id: str,
        *,
        limit: int = 20,
        include_delivered: bool = True,
    ) -> tuple[list[GuidanceEntry], list[dict[str, Any]]]:
        normalized_type = normalize_guidance_target_type(target_type)
        delivered = self._read_guidance_delivered()
        report = read_jsonl_report(
            self._guidance_path(normalized_type, str(target_id)),
            context="conversation.guidance.read",
        )
        entries, parse_errors = _guidance_entries(report.rows, delivered)
        if not include_delivered:
            entries = [item for item in entries if item.delivered_at <= 0]
        selected = entries if limit <= 0 else entries[-limit:]
        return selected, [*report.load_errors, *parse_errors]

    def pending_guidance(self, target_type: str, target_id: str, *, limit: int = 20) -> list[GuidanceEntry]:
        return self.recent_guidance(target_type, target_id, limit=limit, include_delivered=False)

    def pending_guidance_report(
        self,
        target_type: str,
        target_id: str,
        *,
        limit: int = 20,
    ) -> tuple[list[GuidanceEntry], list[dict[str, Any]]]:
        return self.recent_guidance_report(
            target_type,
            target_id,
            limit=limit,
            include_delivered=False,
        )

    def mark_guidance_delivered(
        self,
        guidance_ids: list[str] | tuple[str, ...],
        *,
        now: float | None = None,
    ) -> None:
        ids = [str(item).strip() for item in guidance_ids if str(item or "").strip()]
        if not ids:
            return
        delivered_at = current_time(now)
        update_json_file_atomic(
            self.guidance_delivered_path,
            lambda data: {**data, **dict.fromkeys(ids, delivered_at)},
        )

    def _read_guidance_delivered(self) -> dict[str, float]:
        delivered: dict[str, float] = {}
        for key, value in read_json_file(self.guidance_delivered_path).items():
            try:
                delivered[str(key)] = float(value or 0.0)
            except (TypeError, ValueError):
                continue
        return delivered


def normalize_guidance_target_type(value: object) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "run": "agent_run",
        "runner": "agent_run",
        "subagent": "agent_run",
        "subagent_run": "agent_run",
        "agent": "agent_run",
        "agent_run": "agent_run",
        "thread": "thread",
        "conversation": "thread",
        "task": "task",
        "case": "case",
    }
    return aliases.get(text, safe_file_stem(text))


def _with_guidance_delivered_at(entry: GuidanceEntry, delivered: dict[str, float]) -> GuidanceEntry:
    return replace(entry, delivered_at=float(delivered.get(entry.guidance_id) or 0.0))


def _guidance_entries(
    rows: list[dict[str, Any]],
    delivered: dict[str, float],
) -> tuple[list[GuidanceEntry], list[dict[str, Any]]]:
    entries: list[GuidanceEntry] = []
    errors: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        try:
            entries.append(_with_guidance_delivered_at(GuidanceEntry.from_dict(row), delivered))
        except Exception as exc:
            report = runtime_error_report(exc, context="conversation.guidance.parse")
            report["row_index"] = index
            errors.append(report)
    return entries, errors
