
from __future__ import annotations

"""Shared pending-capability status helpers.

Only the current runner protocol status is machine-authoritative here. Older
or descriptive labels such as NEEDS_TOOL remain plain status text and must not
trigger capability routing.
"""


def is_pending_capability_status(status: str) -> bool:
    normalized = str(status or "").upper().strip()
    return normalized in _EXPLICIT_PENDING_CAPABILITY_STATUSES


_EXPLICIT_PENDING_CAPABILITY_STATUSES = {
    "PENDING_CAPABILITY_REQUEST",
}
