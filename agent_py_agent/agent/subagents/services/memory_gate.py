from __future__ import annotations

"""Explicit review service for run-local memory gate candidates.

The service does not auto-promote memories or install skills. It only exposes
review, export, retention, and verifier operations for already generated
run-local candidates.
"""

from pathlib import Path

from ...memory_archive.memory_gate import list_memory_gate_candidates
from ...memory_archive.memory_gate.export import (
    MemoryGateExportRequest,
    MemoryGateExportResult,
    export_approved_memory_candidates,
    export_approved_skill_sparks,
)
from ...memory_archive.memory_gate.retention import (
    MemoryGateRetentionRequest,
    MemoryGateRetentionResult,
    run_memory_gate_retention,
)
from ...memory_archive.memory_gate.review import (
    MemoryGateReviewRequest,
    MemoryGateReviewResult,
    record_memory_gate_review,
)
from ...memory_archive.memory_gate.verifier import (
    MemoryGateVerifierResult,
    verify_memory_gate_boundary,
)
from ..models import SubAgentTask


class SubAgentMemoryGateService:
    """Runs explicit memory-gate review operations for one manager."""

    def __init__(self, manager: object) -> None:
        self.manager = manager

    def list_memory_gate_candidates(self, run_id: str) -> list[dict[str, object]]:
        task = self._ensure_memory_gate_workspace(run_id)
        return list_memory_gate_candidates(Path(task.agent_run_workspace_dir))

    def review_memory_gate_candidate(
        self,
        run_id: str,
        request: MemoryGateReviewRequest,
    ) -> MemoryGateReviewResult:
        task = self._ensure_memory_gate_workspace(run_id)
        return record_memory_gate_review(Path(task.agent_run_workspace_dir), request)

    def run_memory_gate_retention(
        self,
        run_id: str,
        request: MemoryGateRetentionRequest,
    ) -> MemoryGateRetentionResult:
        task = self._ensure_memory_gate_workspace(run_id)
        return run_memory_gate_retention(Path(task.agent_run_workspace_dir), request)

    def export_memory_gate_candidates_to_memory(
        self,
        run_id: str,
        *,
        memory_path: str | Path | None = None,
        request: MemoryGateExportRequest,
    ) -> MemoryGateExportResult:
        task = self._ensure_memory_gate_workspace(run_id)
        bundled = export_request_with_paths(request, memory_path=memory_path)
        return export_approved_memory_candidates(Path(task.agent_run_workspace_dir), request=bundled)

    def export_memory_gate_candidates_to_skill_drafts(
        self,
        run_id: str,
        *,
        output_dir: str | Path | None = None,
        request: MemoryGateExportRequest,
    ) -> MemoryGateExportResult:
        task = self._ensure_memory_gate_workspace(run_id)
        bundled = export_request_with_paths(request, output_dir=output_dir)
        return export_approved_skill_sparks(Path(task.agent_run_workspace_dir), request=bundled)

    def verify_memory_gate_boundary(self, run_id: str) -> MemoryGateVerifierResult:
        task = self._ensure_memory_gate_workspace(run_id)
        return verify_memory_gate_boundary(Path(task.agent_run_workspace_dir))

    def _ensure_memory_gate_workspace(self, run_id: str) -> SubAgentTask:
        task = self.manager.load(run_id)
        if task.agent_run_workspace_dir and Path(task.agent_run_memory_candidates_jsonl).exists():
            return task
        self.manager.save(task)
        return self.manager.load(run_id)


def export_request_with_paths(
    request: MemoryGateExportRequest,
    *,
    memory_path: str | Path | None = None,
    output_dir: str | Path | None = None,
) -> MemoryGateExportRequest:
    return MemoryGateExportRequest(
        candidate_id=request.candidate_id,
        reviewer=request.reviewer,
        now=request.now,
        memory_path=memory_path if memory_path is not None else request.memory_path,
        output_dir=output_dir if output_dir is not None else request.output_dir,
    )


__all__ = [
    "SubAgentMemoryGateService",
    "export_request_with_paths",
]
