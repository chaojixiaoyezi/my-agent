
from __future__ import annotations

from pathlib import Path

from ..gateway_parts.io import write_json_file_atomic
from .models import CollaborationCase


class CollaborationBaseStore:
    """Shared directory layout for case/request/evidence ledgers."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.cases_dir = self.root / "cases"
        self.requests_dir = self.root / "requests"
        self.evidence_dir = self.root / "evidence"
        self.participants_dir = self.root / "participants"
        self.decisions_dir = self.root / "decisions"
        self.capabilities_path = self.root / "agent_capabilities.json"
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        for path in (
            self.cases_dir,
            self.requests_dir,
            self.evidence_dir,
            self.participants_dir,
            self.decisions_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def _write_case(self, case: CollaborationCase) -> None:
        write_json_file_atomic(self._case_path(case.case_id), case.to_dict())

    def _case_path(self, case_id: str) -> Path:
        return self.cases_dir / f"{case_id}.json"

    def _requests_path(self, case_id: str) -> Path:
        return self.requests_dir / f"{case_id}.jsonl"

    def _evidence_path(self, case_id: str) -> Path:
        return self.evidence_dir / f"{case_id}.jsonl"

    def _participants_path(self, case_id: str) -> Path:
        return self.participants_dir / f"{case_id}.jsonl"

    def _decisions_path(self, case_id: str) -> Path:
        return self.decisions_dir / f"{case_id}.jsonl"
