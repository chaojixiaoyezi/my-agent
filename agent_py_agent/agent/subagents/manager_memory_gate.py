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
from ..memory_archive.memory_gate_export import (
    MemoryGateExportRequest,
    MemoryGateExportResult,
    export_approved_memory_candidates,
    export_approved_skill_sparks,
)
from ..memory_archive.memory_gate_retention import (
    MemoryGateRetentionRequest,
    MemoryGateRetentionResult,
    run_memory_gate_retention,
)
from ..memory_archive.memory_gate_review import (
    MemoryGateReviewRequest,
    MemoryGateReviewResult,
    record_memory_gate_review,
)
from ..memory_archive.memory_gate_verifier import (
    MemoryGateVerifierResult,
    verify_memory_gate_boundary,
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

    def run_memory_gate_retention(
        self,
        run_id: str,
        request: MemoryGateRetentionRequest,
    ) -> MemoryGateRetentionResult:
        """Plan or apply queue retention without deleting audit facts."""

        task = self._ensure_memory_gate_workspace(run_id)
        # LLM: retention may compact the active queue, but candidate/decision audit files stay put.
        return run_memory_gate_retention(Path(task.agent_run_workspace_dir), request)

    def export_memory_gate_candidates_to_memory(
        self,
        run_id: str,
        *,
        memory_path: str | Path | None = None,
        request: MemoryGateExportRequest,
    ) -> MemoryGateExportResult:
        """Export approved memory candidates only after explicit review approval."""

        task = self._ensure_memory_gate_workspace(run_id)
        bundled = _export_request_with_paths(request, memory_path=memory_path)
        return export_approved_memory_candidates(Path(task.agent_run_workspace_dir), request=bundled)

    def export_memory_gate_candidates_to_skill_drafts(
        self,
        run_id: str,
        *,
        output_dir: str | Path | None = None,
        request: MemoryGateExportRequest,
    ) -> MemoryGateExportResult:
        """Export approved skill candidates as local drafts, never as installed skills."""

        task = self._ensure_memory_gate_workspace(run_id)
        bundled = _export_request_with_paths(request, output_dir=output_dir)
        return export_approved_skill_sparks(Path(task.agent_run_workspace_dir), request=bundled)

    def verify_memory_gate_boundary(self, run_id: str) -> MemoryGateVerifierResult:
        """Run deterministic checks for no-auto-promotion gate boundaries."""

        task = self._ensure_memory_gate_workspace(run_id)
        return verify_memory_gate_boundary(Path(task.agent_run_workspace_dir))

    def _ensure_memory_gate_workspace(self, run_id: str) -> SubAgentTask:
        task = self.load(run_id)
        if task.agent_run_workspace_dir and Path(task.agent_run_memory_candidates_jsonl).exists():
            return task
        self.save(task)
        return self.load(run_id)


def _export_request_with_paths(
    request: MemoryGateExportRequest,
    *,
    memory_path: str | Path | None = None,
    output_dir: str | Path | None = None,
) -> MemoryGateExportRequest:
    # LLM: old manager fields stay compatible while the core export API receives one bundle.
    return MemoryGateExportRequest(
        candidate_id=request.candidate_id,
        reviewer=request.reviewer,
        now=request.now,
        memory_path=memory_path if memory_path is not None else request.memory_path,
        output_dir=output_dir if output_dir is not None else request.output_dir,
    )
