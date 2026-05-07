from __future__ import annotations

"""Evidence file helpers for local log-analysis queries."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..storage.base import EvidenceRef, dict_to_model, stable_digest, utc_now


@dataclass(frozen=True)
class QueryEvidencePayload:
    query_id: str
    parameters: dict[str, Any]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    summary: dict[str, Any]


class LocalEvidenceStore:
    """Writes query evidence payloads under the local store root."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.evidence_dir = self.root / "evidence"
        self.evidence_dir.mkdir(parents=True, exist_ok=True)

    def write_query_result(
        self,
        *,
        payload: QueryEvidencePayload | None = None,
        query_id: str = "",
        parameters: dict[str, Any] | None = None,
        rows: list[dict[str, Any]] | None = None,
        row_count: int = 0,
        truncated: bool = False,
        summary: dict[str, Any] | None = None,
    ) -> EvidenceRef:
        evidence = payload or QueryEvidencePayload(
            query_id=str(query_id),
            parameters=dict(parameters or {}),
            rows=list(rows or []),
            row_count=int(row_count),
            truncated=bool(truncated),
            summary=dict(summary or {}),
        )
        evidence_id = evidence.query_id
        path = self.evidence_dir / f"{evidence_id}.json"
        payload = {
            "evidence_id": evidence_id,
            "kind": "query_result",
            "query_id": evidence.query_id,
            "created_at": utc_now(),
            "parameters": evidence.parameters,
            "row_count": evidence.row_count,
            "truncated": evidence.truncated,
            "summary": evidence.summary,
            "rows": evidence.rows,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
        path.write_text(encoded + "\n", encoding="utf-8")
        digest = stable_digest(payload)
        ref_payload = {
            "evidence_id": evidence_id,
            "kind": "query_result",
            "query_id": evidence.query_id,
            "uri": str(path),
            "path": str(path),
            "content_hash": digest,
            "sha256": digest,
            "row_count": evidence.row_count,
            "truncated": evidence.truncated,
            "created_at": payload["created_at"],
            "summary": f"query_result rows={evidence.row_count} truncated={evidence.truncated}",
            "metadata": {
                "evidence_path": str(path),
                "parameters": evidence.parameters,
                "summary": evidence.summary,
            },
        }
        return dict_to_model(EvidenceRef, ref_payload)

    def read_json(self, evidence_path: str | Path, *, max_bytes: int = 200_000) -> dict[str, Any]:
        path = Path(evidence_path)
        if not path.exists():
            raise FileNotFoundError(str(path))
        if path.stat().st_size > max_bytes:
            return {
                "path": str(path),
                "truncated": True,
                "size_bytes": path.stat().st_size,
            }
        return json.loads(path.read_text(encoding="utf-8"))
