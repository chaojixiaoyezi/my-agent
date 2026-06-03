
from __future__ import annotations

"""Capability-request recovery for runner structured output."""

from ..capability_status import is_pending_capability_status
from .values import _dict_list


def capability_requests_from_payload(payload: dict[str, object]) -> list[dict[str, object]]:
    explicit = _dict_list(payload.get("capability_requests", []))
    if explicit:
        return explicit
    status = str(payload.get("status", "") or "")
    if not is_pending_capability_status(status):
        return []
    text = _pending_capability_text(payload)
    request = _pending_capability_request(status, text)
    return [request] if request else []


def _pending_capability_text(payload: dict[str, object]) -> str:
    parts = [
        str(payload.get("status", "") or ""),
        str(payload.get("summary", "") or ""),
        str(payload.get("blocked_reason", "") or ""),
    ]
    parts.extend(_pending_step_texts(payload.get("pending_steps", [])))
    return "\n".join(part for part in parts if part.strip())


def _pending_step_texts(value: object) -> list[str]:
    return [
        " ".join(str(item.get(key, "") or "") for key in ("action", "step", "name", "status"))
        for item in _dict_list(value)
    ]


def _pending_capability_request(status: str, text: str) -> dict[str, object] | None:
    commands = _requested_commands_from_pending(text)
    tools = _requested_tools_from_pending(text, commands)
    if not commands and not tools:
        return None
    needed = "controlled_exec" if "controlled_exec" in tools else "shell"
    return {
        "problem": f"模型状态为 {status}，但未填写 capability_requests；系统从 pending_steps 兜底生成。",
        "needed_capability": needed,
        "capability_type": "shell" if commands or "controlled_exec" in tools else "generic",
        "expected_output": "父级路由可用能力后重新运行当前子任务。",
        "requested_tools": tools,
        "requested_commands": commands,
        "path_scope": [],
        "output_budget": {"stdout_bytes": 65536, "stderr_bytes": 32768},
        "risk_level": "medium" if "rm" in commands else "low",
        "evidence": [text[:1000]] if text.strip() else [],
    }


def _requested_tools_from_pending(text: str, commands: list[str]) -> list[str]:
    lowered = text.lower()
    if "controlled_exec" in lowered or commands:
        return ["controlled_exec"]
    return []


def _requested_commands_from_pending(text: str) -> list[str]:
    lowered = text.lower()
    commands = []
    for command in ("pwd", "python3", "python", "rm", "curl", "pytest", "npm", "node"):
        if command in lowered and command not in commands:
            commands.append(command)
    if "python3" in commands and "python" in commands:
        commands.remove("python")
    return commands
