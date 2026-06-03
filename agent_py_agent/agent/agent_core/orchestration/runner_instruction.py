from __future__ import annotations

from pathlib import Path


def resolved_runner_instruction(agent: object, value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    workspace_root = _agent_workspace_root(agent)
    if workspace_root:
        text = text.replace("{workspace_root}", workspace_root).replace("{{workspace_root}}", workspace_root)
    return text


def _agent_workspace_root(agent: object) -> str:
    raw = getattr(getattr(agent, "subagents", None), "workspace_root", None)
    if not isinstance(raw, str | Path):
        return ""
    return str(Path(raw).expanduser().resolve(strict=False))
