
from __future__ import annotations

from .request_status import request_has_required_evidence as _request_has_required_evidence
from .store_status import CollaborationStatusStore


class CollaborationStore(CollaborationStatusStore):
    """File-backed collaboration control plane."""


__all__ = ["CollaborationStore", "_request_has_required_evidence"]
