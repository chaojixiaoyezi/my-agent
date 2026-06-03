
from __future__ import annotations


def _append_runner_repair_prompt(original_prompt: str, repair_prompt: str) -> str:
    return (
        f"{original_prompt}\n\n"
        "---\n\n"
        "# Structured Output Repair Prompt\n\n"
        f"{repair_prompt}"
    )


def _append_runner_repair_response(original_response: str, repair_response: str) -> str:
    return (
        f"{original_response}\n\n"
        "---\n\n"
        "# Structured Output Repair Response\n\n"
        f"{repair_response}"
    )


def _append_runner_repair_failure(original_response: str, exc: Exception) -> str:
    return (
        f"{original_response}\n\n"
        "---\n\n"
        "# Structured Output Repair Failure\n\n"
        f"{type(exc).__name__}: {exc}"
    )
