from __future__ import annotations

"""Compatibility facade for gateway processing leases.

The lease implementation lives in :mod:`lease_service`. This module keeps the
old private names used by older tests and callers, while avoiding a second
heartbeat registry or a second JSON-reading path.
"""

from .lease_service import (
    LeaseRefreshReport,
    _active_heartbeat_request_ids,
    is_heartbeat_alive_for_request,
    refresh_processing_lease_report,
)
from .lease_service import (
    _lease_interval as _gateway_processing_lease_interval,
)
from .lease_service import (
    refresh_processing_lease as _touch_gateway_processing_lease,
)
from .lease_service import (
    start_lease_heartbeat as _start_gateway_processing_lease_heartbeat,
)

__all__ = [
    "LeaseRefreshReport",
    "_active_heartbeat_request_ids",
    "_gateway_processing_lease_interval",
    "_start_gateway_processing_lease_heartbeat",
    "_touch_gateway_processing_lease",
    "is_heartbeat_alive_for_request",
    "refresh_processing_lease_report",
]
