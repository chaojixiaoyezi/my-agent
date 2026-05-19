from __future__ import annotations

# LLM: WorkerPool controls physical execution slots separately from logical TaskCards.
# 模块用途: 提供后台 TaskAgent worker slot 的并发限制和释放能力。
from ..cards import CardStore, LeaseCard


 # LLM: WorkerPool grants bounded worker slots through LeaseCards.
 # 类用途: 管理后台 TaskAgent 同时执行的工位数量。
class WorkerPool:
    # LLM: WorkerPool.__init__ stores card persistence and slot capacity.
    # 函数用途: 初始化 worker pool 的 CardStore 和最大 TaskAgent 并发数。
    def __init__(self, cards: CardStore, *, max_task_agent_slots: int = 4):
        self.cards = cards
        self.max_task_agent_slots = max_task_agent_slots

    # LLM: acquire_task_agent_slot consumes one available TaskAgent slot lease.
    # 函数用途: 为 worker 获取一个后台 TaskAgent 工位；满额时返回 None。
    def acquire_task_agent_slot(self, worker_id: str, *, task_id: str | None = None) -> LeaseCard | None:
        with self.cards.lock("worker_pool:task_agent"):
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
            lease = self.cards.acquire_lease(
                "worker_slot",
                f"task_agent:{slot_id}",
                worker_id,
                task_id=task_id,
                ttl_seconds=3600,
                metadata={"slot_type": "task_agent", "slot_id": slot_id},
            )
            if lease is not None and task_id:
                self.cards.create_worker_run(task_id=task_id, worker_id=worker_id, lease_id=lease.lease_id)
            return lease

    # LLM: release_slot frees a worker slot without deleting lease history.
    # 函数用途: 释放指定 worker slot 租约。
    def release_slot(self, lease_id: str) -> bool:
        return self.cards.release_lease(lease_id)
