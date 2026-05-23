from __future__ import annotations

import tempfile
from pathlib import Path

from agent_py_agent.agent.agent_core.main_agent_delivery_closeout_artifact_repair import (
    append_artifact_finding_repair_actions,
)
from agent_py_agent.agent.agent_core.main_agent_delivery_closeout_recovery_models import (
    RecoveryActionLedger,
)
from agent_py_agent.tests.test_main_agent_delivery_closeout_staged import (
    _enriched_report,
    _research_pdf_contract,
    _write_json_file,
    _write_valid_pdf,
)


# LLM: Artifact mapping failures should repair the mapped text artifact, not the final binary wrapper.
# 函数用途: 验证 mapping finding 的 location 会成为 repair target，PDF/图片等二进制产物不会误导修复链路。
def test_delivery_closeout_uses_finding_location_as_repair_target_for_mapping_failures() -> None:
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        contract = _research_pdf_contract()
        validation_contract = contract["artifacts"][0]["validation_contract"]
        validation_contract["collection_contract"] = _mapping_collection_contract()
        _write_valid_pdf(workspace / "outputs/research_documents/research_documents_zh.pdf")
        _write_json_file(workspace / "outputs/research_documents/source_index.json", _mapped_rows_payload())
        draft = workspace / "outputs/research_documents/research_documents_zh.md"
        draft.write_text("# 翻译正文\n\n这里没有精确标题映射。", encoding="utf-8")

        _, actions = _enriched_report(workspace, contract)

        assert str(draft) in actions["ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED"]["repair_targets"]


# LLM: Missing mapping targets should resolve against the workspace root, not the final artifact directory twice.
# 函数用途: 验证 outputs/... 这类合同 ref 即使文件尚不存在，也不会被拼成 outputs/.../outputs/... 的假路径。
def test_delivery_closeout_resolves_missing_mapping_target_without_nested_output_prefix() -> None:
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        contract = _research_pdf_contract()
        validation_contract = contract["artifacts"][0]["validation_contract"]
        validation_contract["collection_contract"] = _mapping_collection_contract()
        _write_valid_pdf(workspace / "outputs/research_documents/research_documents_zh.pdf")
        _write_json_file(workspace / "outputs/research_documents/source_index.json", _mapped_rows_payload())

        _, actions = _enriched_report(workspace, contract)

        targets = actions["ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED"]["repair_targets"]
        expected = str(workspace / "outputs/research_documents/research_documents_zh.md")
        assert expected in targets
        assert not any("outputs/research_documents/outputs/research_documents" in item for item in targets)


def _mapping_collection_contract() -> dict[str, object]:
    return {
        "source_json_ref": "outputs/research_documents/source_index.json",
        "items_path": "rows",
        "min_items_total": 2,
        "required_item_fields": ["title", "url", "date"],
        "require_completion_evidence": True,
        "mapping": {
            "artifact_ref": "outputs/research_documents/research_documents_zh.md",
            "key_fields": ["title"],
            "min_mapped_items": 2,
        },
    }


def _mapped_rows_payload() -> dict[str, object]:
    return {
        "completion_evidence": {"scope": "complete"},
        "rows": [
            {"title": "Paper A", "url": "https://example.com/a", "date": "2026-01-01"},
            {"title": "Paper B", "url": "https://example.com/b", "date": "2026-01-02"},
        ],
    }


# LLM: JSON pointer fragments identify rows, not filesystem names.
# 函数用途: 验证 `source_index.json#1:field` 这类 finding location 会解析到真实 JSON 文件。
def test_delivery_closeout_strips_json_fragment_from_repair_target_locations() -> None:
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        contract = _research_pdf_contract()
        validation_contract = contract["artifacts"][0]["validation_contract"]
        validation_contract["collection_contract"] = _translated_collection_contract()
        source = workspace / "outputs/research_documents/source_index.json"
        _write_valid_pdf(workspace / "outputs/research_documents/research_documents_zh.pdf")
        _write_json_file(source, _translated_rows_payload())

        _, actions = _enriched_report(workspace, contract)

        action = actions["ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED"]
        assert str(source) in action["repair_targets"]
        assert not any("source_index.json#1" in item for item in action["repair_targets"])
        assert not any("outputs/research_documents/outputs/research_documents" in item for item in action["repair_targets"])


# LLM: Collection value mismatch findings should become structured JSON updates, not generic PDF edits.
# 函数用途: 验证 translated/status 这类集合机器字段不匹配时，closeout 生成可执行 checkpoint 更新动作。
def test_delivery_closeout_emits_collection_item_value_repair_action() -> None:
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        contract = _research_pdf_contract()
        validation_contract = contract["artifacts"][0]["validation_contract"]
        validation_contract["collection_contract"] = _translated_collection_contract()
        source = workspace / "outputs/research_documents/source_index.json"
        _write_valid_pdf(workspace / "outputs/research_documents/research_documents_zh.pdf")
        _write_json_file(source, _translated_rows_payload())

        _, actions = _enriched_report(workspace, contract)

        action = actions["COLLECTION_ITEM_VALUE_MISMATCH"]
        assert action["recommended_action"] == "repair_collection_item_values"
        assert action["checkpoint_ref"] == "outputs/research_documents/source_index.json"
        assert action["writer_tool"] == "write_structured_json"
        assert action["items_path"] == "rows"
        assert action["collection_item_updates"] == [
            {"item_index": 1, "field_path": "translated", "value": True},
        ]


# LLM: Placeholder source fields should route back through the collection writer, not PDF artifact patching.
# 函数用途: 验证来源索引中残留 __FILL_* 时，closeout 生成 source checkpoint 返工动作，优先重新采集/重写结构化来源。
def test_delivery_closeout_emits_collection_placeholder_repair_action() -> None:
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        contract = _research_pdf_contract()
        validation_contract = contract["artifacts"][0]["validation_contract"]
        validation_contract["collection_contract"] = _translated_collection_contract()
        source = workspace / "outputs/research_documents/source_index.json"
        _write_valid_pdf(workspace / "outputs/research_documents/research_documents_zh.pdf")
        _write_json_file(source, _placeholder_rows_payload())

        _, actions = _enriched_report(workspace, contract)

        action = actions["COLLECTION_ITEM_PLACEHOLDER_VALUE"]
        assert action["recommended_action"] == "repair_structured_checkpoint_json"
        assert action["checkpoint_ref"] == "outputs/research_documents/source_index.json"
        assert action["writer_tool"] == "api_json_collection"
        assert action["write_tools"] == ["api_json_collection", "write_structured_json"]
        assert action["required_columns"] == ["title", "url", "date", "translated"]


def _translated_collection_contract() -> dict[str, object]:
    return {
        "source_json_ref": "outputs/research_documents/source_index.json",
        "items_path": "rows",
        "min_items_total": 2,
        "required_item_fields": ["title", "url", "date", "translated"],
        "required_item_values": {"translated": True},
    }


def _translated_rows_payload() -> dict[str, object]:
    return {
        "rows": [
            {"title": "Paper A", "url": "https://example.com/a", "date": "2026-01-01", "translated": True},
            {"title": "Paper B", "url": "https://example.com/b", "date": "2026-01-02", "translated": False},
        ],
    }


def _placeholder_rows_payload() -> dict[str, object]:
    return {
        "rows": [
            {"title": "Paper A", "url": "https://example.com/a", "date": "__FILL_3_date__", "translated": True},
            {"title": "Paper B", "url": "https://example.com/b", "date": "2026-01-02", "translated": True},
        ],
    }


# LLM: dotted API names are finding facts, not files to patch.
# 函数用途: 验证 `app.goBrowse` 这类 JS API finding value 不会被 closeout 拼成假 repair target。
def test_delivery_closeout_does_not_treat_dotted_api_findings_as_file_targets() -> None:
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        contract = _static_site_contract(workspace)
        site = workspace / "outputs/site"
        site.mkdir(parents=True)
        (site / "index.html").write_text(_site_html(), encoding="utf-8")
        (site / "app.js").write_text("const app = {};", encoding="utf-8")

        _, actions = _enriched_report(workspace, contract)

        action = actions["ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED"]
        assert str(site / "index.html") in action["repair_targets"]
        assert str(site / "app.js") in action["repair_targets"]
        assert not any(item.endswith("/app.goBrowse") for item in action["repair_targets"])


# LLM: Open-world artifact repair should not depend on a fixed suffix allowlist.
# 函数用途: 验证已存在的新格式产物文件会进入 repair_targets，而不是因为后缀不在表里被丢掉。
def test_delivery_closeout_repair_targets_accept_existing_open_world_files(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "outputs" / "dataset"
    artifact_dir.mkdir(parents=True)
    table = artifact_dir / "metrics.parquet"
    table.write_bytes(b"PAR1")
    ledger = RecoveryActionLedger(actions=[], seen=set())

    append_artifact_finding_repair_actions(
        {
            "artifacts": [
                {
                    "ok": False,
                    "artifact_id": "dataset",
                    "kind": "parquet",
                    "path": str(artifact_dir),
                    "acceptance_report": {
                        "findings": [
                            {
                                "code": "ARTIFACT_CONTENT_INVALID",
                                "location": "metrics.parquet:row=1",
                            }
                        ]
                    },
                }
            ]
        },
        ledger,
    )

    assert ledger.actions
    assert str(table) in ledger.actions[0]["repair_targets"]


def _static_site_contract(workspace: Path) -> dict[str, object]:
    return {
        "artifacts": [
            {
                "artifact_id": "site",
                "kind": "web_project",
                "path": str(workspace / "outputs/site"),
                "validation_contract": {"validator": "static_site_check", "required_files": ["index.html", "app.js"]},
            }
        ]
    }


def _site_html() -> str:
    return '<!doctype html><html><body><button onclick="app.goBrowse()">Go</button><script src="app.js"></script></body></html>'
