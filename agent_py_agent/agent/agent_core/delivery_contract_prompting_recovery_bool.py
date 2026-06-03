
from __future__ import annotations


def _json_bool(value: object, *, default: bool) -> str:
    return "true" if bool(default if value is None else value) else "false"
