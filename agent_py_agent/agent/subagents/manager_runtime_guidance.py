
from __future__ import annotations


def runtime_guidance_context(manager: object, run_id: str) -> list[dict[str, object]]:
    store = getattr(manager, "conversation_store", None)
    if store is None:
        return []
    entries = store.pending_guidance("agent_run", run_id, limit=20)
    if not entries:
        return []
    store.mark_guidance_delivered([entry.guidance_id for entry in entries])
    return [entry.to_dict() for entry in entries]


def attach_runtime_guidance(bundle: dict[str, object], guidance: list[dict[str, object]]) -> None:
    if not guidance:
        return
    reserved = bundle.get("reserved")
    if not isinstance(reserved, dict):
        reserved = {}
    reserved["runtime_guidance"] = guidance
    bundle["reserved"] = reserved
