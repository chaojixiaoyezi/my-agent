from __future__ import annotations

"""Resolve durable task identity without confusing it with a request attempt."""

_MAIN_SCOPES = frozenset({"", "default"})


def _structured_conversation_task_id(params: object) -> str:
    attrs = getattr(params, "task_attributes", None)
    if not isinstance(attrs, dict):
        return ""
    return str(attrs.get("conversation_task_id") or "").strip()


def durable_task_id(params: object) -> str:
    """Use the selected durable task only for a main-agent context."""
    scope = str(getattr(params, "context_scope", "") or "default").strip().lower()
    selected = _structured_conversation_task_id(params) if scope in _MAIN_SCOPES else ""
    return selected or str(getattr(params, "task_id", "") or "").strip()


def run_scope_task_id(value: object) -> str:
    """Read the durable task identity from a trusted tool-call run scope."""
    if not isinstance(value, dict):
        return ""
    return str(value.get("root_task_id") or value.get("task_id") or "").strip()


def progress_ledger_id(agent: object, params: object, *, scoped_id: str = "") -> str:
    """Return the shared ledger key for tool writes, seeds and closeout reads."""
    scope = str(getattr(params, "context_scope", "") or "default").strip().lower()
    if scope in _MAIN_SCOPES:
        selected = _structured_conversation_task_id(params)
        if selected:
            return selected
    if str(getattr(params, "source", "") or "").strip() == "background_main_agent":
        task_id = str(getattr(params, "task_id", "") or "").strip()
        if task_id:
            return task_id
    if scoped_id:
        return str(scoped_id).strip()
    for value in (
        getattr(params, "run_id", ""),
        getattr(agent, "_main_agent_run_id", ""),
        getattr(params, "task_id", ""),
        getattr(agent, "_current_request_id", ""),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""


__all__ = ["durable_task_id", "progress_ledger_id", "run_scope_task_id"]
