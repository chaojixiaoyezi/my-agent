from __future__ import annotations

from ..cards import CardStore, LeaseCard


class WorkerPool:
    def __init__(self, cards: CardStore, *, max_task_agent_slots: int = 4):
        self.cards = cards
        self.max_task_agent_slots = max_task_agent_slots

    def acquire_task_agent_slot(self, worker_id: str) -> LeaseCard | None:
        active = [
            lease
            for lease in self.cards.list_leases()
            if lease.resource_type == "worker_slot"
            and lease.resource_id.startswith("task_agent:")
            and lease.is_active
        ]
        if len(active) >= self.max_task_agent_slots:
            return None
        active_slot_ids = {str(lease.metadata.get("slot_id")) for lease in active}
        slot_id = next((index for index in range(self.max_task_agent_slots) if str(index) not in active_slot_ids), len(active))
        return self.cards.acquire_lease(
            "worker_slot",
            f"task_agent:{slot_id}",
            worker_id,
            ttl_seconds=3600,
            metadata={"slot_type": "task_agent", "slot_id": slot_id},
        )

    def release_slot(self, lease_id: str) -> bool:
        return self.cards.release_lease(lease_id)
