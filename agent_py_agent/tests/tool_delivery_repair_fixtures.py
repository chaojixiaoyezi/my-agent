from __future__ import annotations

import json
from pathlib import Path


def _evidence_repair_closeout() -> dict[str, object]:
    return {
        "ok": False,
        "delivery_progress": {
            "recovery_actions": [
                {
                    "code": "EVIDENCE_REQUIRED_FIELD_MISSING",
                    "recommended_action": "repair_evidence_refs",
                    "checkpoint_ref": "outputs/report/source_data.json",
                    "required_fields": ["项目名", "地址"],
                    "writer_tool": "write_structured_json",
                }
            ]
        },
    }


def _structure_and_evidence_repair_closeout() -> dict[str, object]:
    closeout = _evidence_repair_closeout()
    actions = closeout["delivery_progress"]["recovery_actions"]
    actions.insert(
        0,
        {
            "code": "STAGED_JSON_TOO_FEW_SHEETS",
            "recommended_action": "repair_structured_checkpoint_json",
            "checkpoint_ref": "outputs/report/source_data.json",
            "required_columns": ["项目名", "地址"],
            "writer_tool": "write_structured_json",
        },
    )
    return closeout


def _sheet_only_write_call() -> dict[str, object]:
    return {
        "tool": "write_structured_json",
        "path": "outputs/report/source_data.json",
        "sheets": [{"name": "榜单", "rows": [{"项目名": "demo", "地址": "https://example.com"}]}],
    }


def _structured_evidence_write_call() -> dict[str, object]:
    return {
        "tool": "write_structured_json",
        "path": "outputs/report/source_data.json",
        "data": {
            "sheets": [{"name": "榜单", "rows": [{"项目名": "demo", "地址": "https://example.com"}]}],
            "source_refs": [{"source_id": "src-1", "uri": "https://example.com"}],
            "claims": [
                {"field": "项目名", "value": "demo", "source_ids": ["src-1"], "verification_status": "VERIFIED"},
                {"field": "地址", "value": "https://example.com", "source_ids": ["src-1"], "verification_status": "VERIFIED"},
            ],
        },
    }


def _write_closeout(root: Path, payload: dict[str, object]) -> None:
    path = root / ".agent_delivery" / "closeout.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
