from __future__ import annotations


def tool_workflow_mode(explicit_mode: object, config_mode: object) -> str:
    del config_mode
    if isinstance(explicit_mode, str):
        normalized = explicit_mode.strip().lower()
        if normalized in {"off", "plan", "auto"}:
            return normalized
        if normalized:
            return "off"
    return "off"
