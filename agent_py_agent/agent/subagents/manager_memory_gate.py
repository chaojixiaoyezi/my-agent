from __future__ import annotations

"""LLM: exposes explicit review operations for run-local memory gate candidates.

给人看的解释：
这里不写长期记忆，也不生成正式 skill。它只让父代理把某个候选标记成
approved/rejected/needs_evidence，后续提升必须走另一条显式流程。
"""

from pathlib import Path

from ..memory_archive.memory_gate import (
    list_memory_gate_candidates,
)
from ..memory_archive.memory_gate_review import (
    MemoryGateReviewRequest,
    MemoryGateReviewResult,
    record_memory_gate_review,
)
from .models import SubAgentTask


class SubAgentMemoryGateMixin:
    def list_memory_gate_candidates(self, run_id: str) -> list[dict[str, object]]:
        """Return current run-local gate candidates, creating the skeleton if needed."""

        task = self._ensure_memory_gate_workspace(run_id)
        return list_memory_gate_candidates(Path(task.agent_run_workspace_dir))

    def review_memory_gate_candidate(
        self,
        run_id: str,
        request: MemoryGateReviewRequest,
    ) -> MemoryGateReviewResult:
        """Record a review decision without exporting to long-term memory or skills."""

        task = self._ensure_memory_gate_workspace(run_id)
        # LLM: review writes only gate files/checkpoint refs; promotion remains a separate command.
        return record_memory_gate_review(
            Path(task.agent_run_workspace_dir),
            request,
        )

    def _ensure_memory_gate_workspace(self, run_id: str) -> SubAgentTask:
        task = self.load(run_id)
        if task.agent_run_workspace_dir and Path(task.agent_run_memory_candidates_jsonl).exists():
            return task
        self.save(task)
        return self.load(run_id)
