from __future__ import annotations

"""Evidence file helpers for local log-analysis queries."""

import json
from pathlib import Path
from typing import Any

from ..storage.base import EvidenceRef, dict_to_model, stable_digest, utc_now


class LocalEvidenceStore:
    """Writes query evidence payloads under the local store root."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.evidence_dir = self.root / "evidence"
        self.evidence_dir.mkdir(parents=True, exist_ok=True)

    def write_query_result(
        self,
        *,
        query_id: str,
        parameters: dict[str, Any],
        rows: list[dict[str, Any]],
        row_count: int,
        truncated: bool,
        summary: dict[str, Any],
    ) -> EvidenceRef:
        evidence_id = query_id
        path = self.evidence_dir / f"{evidence_id}.json"
        payload = {
            "evidence_id": evidence_id,
            "kind": "query_result",
            "query_id": query_id,
            "created_at": utc_now(),
            "parameters": parameters,
            "row_count": row_count,
            "truncated": truncated,
            "summary": summary,
            "rows": rows,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
        path.write_text(encoded + "\n", encoding="utf-8")
        digest = stable_digest(payload)
        ref_payload = {
            "evidence_id": evidence_id,
            "kind": "query_result",
            "query_id": query_id,
            "uri": str(path),
            "path": str(path),
            "content_hash": digest,
            "sha256": digest,
            "row_count": row_count,
            "truncated": truncated,
            "created_at": payload["created_at"],
            "summary": f"query_result rows={row_count} truncated={truncated}",
            "metadata": {
                "evidence_path": str(path),
                "parameters": parameters,
                "summary": summary,
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
