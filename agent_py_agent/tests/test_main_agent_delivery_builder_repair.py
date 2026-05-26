from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.agent_core.main_agent_delivery_closeout import (
    DeliveryContractValidationRequest,
    DeliveryProgressContext,
    _enrich_delivery_progress,
    _validate_contract_artifacts,
)
from agent_py_agent.agent.agent_core.runtime_loop_models import RunParams


# LLM: Builder repair must wait when the builder source artifact is the failed mapping target.
# 函数用途: 验证通用 source->builder->final 链路中，源产物待修时不会继续重复调用 builder。
def test_builder_repair_waits_for_failed_source_artifact_mapping(tmp_path: Path) -> None:
    contract = _document_pdf_contract()
    _write_valid_pdf(tmp_path / "outputs/docs/out.pdf")
    _write_source_index(tmp_path / "outputs/docs/source_index.json")
    (tmp_path / "outputs/docs/draft.md").write_text("# 翻译正文\n\n只有骨架。", encoding="utf-8")

    actions = _actions(tmp_path, contract)

    assert "STAGING_BUILDER_READY" not in actions
    assert actions["ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED"]["recommended_action"] == "repair_artifact_against_findings"


def _actions(workspace: Path, contract: dict[str, object]) -> dict[str, dict[str, object]]:
    report = _validate_contract_artifacts(
        DeliveryContractValidationRequest(
            artifacts=contract["artifacts"],
            contract=contract,
            params=RunParams(delivery_contract=contract, save=False),
            workspace_root=workspace,
        )
    )
    enriched = _enrich_delivery_progress(report, {}, DeliveryProgressContext(workspace, contract))
    return {item["code"]: item for item in enriched["delivery_progress"]["recovery_actions"]}


def _document_pdf_contract() -> dict[str, object]:
    return {
        "artifacts": [
            {
                "artifact_id": "document_pdf",
                "kind": "pdf",
                "preferred_path": "outputs/docs/out.pdf",
                "validation_contract": {
                    "collection_contract": {
                        "items_path": "rows",
                        "mapping": {
                            "artifact_ref": "outputs/docs/draft.md",
                            "key_fields": ["title"],
                            "min_mapped_items": 1,
                        },
                        "min_items_total": 1,
                        "source_json_ref": "outputs/docs/source_index.json",
                    },
                    "staging_contract": {
                        "builder_tool": "markdown_to_pdf",
                        "checkpoint_refs": ["outputs/docs/source_index.json", "outputs/docs/draft.md", "outputs/docs/out.pdf"],
                        "pdf_ref": "outputs/docs/out.pdf",
                        "source_markdown_ref": "outputs/docs/draft.md",
                    },
                    "validator": "document_acceptance",
                },
            }
        ]
    }


def _write_source_index(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "completion_evidence": {"method": "fixture"},
                "rows": [{"date": "2026-01-01", "title": "MOSS: Self-Evolution", "url": "https://example.com/moss"}],
                "source_refs": [{"artifact_ref": "memory_archive/artifacts/tool_outputs/fetch.json", "source_id": "src-1"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_valid_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n")
