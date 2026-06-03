
from __future__ import annotations

from typing import Any


def tool_params_for_execution(
    normalized_payload: dict[str, Any],
    tool_name: str,
    allowed_tools: list[str] | None,
) -> dict[str, Any]:
    params = {key: value for key, value in normalized_payload.items() if key != "tool"}
    if tool_name == "read_file" and allowed_tools is not None:
        params["__allowed_tools"] = list(allowed_tools)
    return params
