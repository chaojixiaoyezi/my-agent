from __future__ import annotations

"""Value-free audit facts for canonical tool arguments."""

import hashlib
import json


def tool_input_facts(value: object) -> dict[str, object]:
    payload = value if isinstance(value, dict) else {}
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return {
        "field_names": sorted(str(key) for key in payload),
        "field_types": {
            str(key): type(payload[key]).__name__
            for key in sorted(payload, key=str)
        },
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


__all__ = ["tool_input_facts"]
