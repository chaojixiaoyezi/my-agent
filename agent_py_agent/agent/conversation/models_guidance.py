
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class GuidanceEntry:
    guidance_id: str
    target_type: str
    target_id: str
    message: str
    sender: str = ""
    priority: str = "normal"
    delivery: str = "next_turn"
    created_at: float = 0.0
    delivered_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GuidanceEntry:
        metadata = data.get("metadata")
        return cls(
            guidance_id=str(data.get("guidance_id") or ""),
            target_type=str(data.get("target_type") or ""),
            target_id=str(data.get("target_id") or ""),
            message=str(data.get("message") or ""),
            sender=str(data.get("sender") or ""),
            priority=str(data.get("priority") or "normal"),
            delivery=str(data.get("delivery") or "next_turn"),
            created_at=float(data.get("created_at") or 0.0),
            delivered_at=float(data.get("delivered_at") or 0.0),
            metadata=metadata if isinstance(metadata, dict) else {},
        )
