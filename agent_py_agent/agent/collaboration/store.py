# LLM: CollaborationStore composes small ledgers; keep behavior in the split modules.
# 模块用途: 对外保留原 CollaborationStore 入口，内部按能力、case、request、evidence、status 分层实现。

from __future__ import annotations

from .request_status import request_has_required_evidence as _request_has_required_evidence
from .store_status import CollaborationStatusStore


class CollaborationStore(CollaborationStatusStore):
    """File-backed collaboration control plane."""


__all__ = ["CollaborationStore", "_request_has_required_evidence"]
