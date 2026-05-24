from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace


# LLM: generated_rows repair skeletons must be directly executable by write_structured_json.
# 函数用途: 验证大表返工时，shape hint 会拆成 data/generated_rows 参数，而不是塞进 data 里让模型猜。
def test_required_tool_calls_expand_generated_rows_shape_hint() -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_required_calls import (
        required_tool_calls,
    )

    hint = {
        "data": {"completion_evidence": {"scope": "synthetic_analysis_dataset", "row_count": 1000}},
        "generated_rows": {
            "count": 1000,
            "columns": ["记录ID", "月份", "地区", "品类", "销售额", "利润"],
            "fields": {
                "记录ID": {"format": "REC-{index:04d}", "start": 1},
                "月份": {"cycle": ["2026-01", "2026-02"]},
                "地区": {"cycle": ["华东", "华南"]},
                "品类": {"cycle": ["电子产品", "服装"]},
                "销售额": {"number": {"start": 1000, "step": 73}},
                "利润": {"multiply": {"source": "销售额", "factor": 0.22, "decimals": 2}},
            },
            "sheets": {"count": 3, "prefix": "原始数据"},
        },
    }

    calls = required_tool_calls(
        [
            {
                "recommended_action": "repair_structured_checkpoint_json",
                "checkpoint_ref": "outputs/data_analysis/source_data.json",
                "checkpoint_shape_hint": json.dumps(hint, ensure_ascii=False),
                "writer_tool": "write_structured_json",
                "write_tools": ["write_structured_json", "api_json_collection"],
            }
        ]
    )

    assert calls == [
        {
            "tool": "write_structured_json",
            "path": "outputs/data_analysis/source_data.json",
            "data": hint["data"],
            "generated_rows": hint["generated_rows"],
        }
    ]


# LLM: source-evidence repairs should bridge archived search results into api_json_collection.
# 函数用途: 验证来源型 checkpoint 的 required call 会携带最新工具 artifact，而不是要求模型凭记忆手写 source_refs。
def test_delivery_repair_payload_enriches_api_collection_with_source_artifacts(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_payload import delivery_repair_payload

    artifact_path = _write_tool_output_artifact(tmp_path)
    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "recovery_actions": [
                    {
                        "checkpoint_materialization_mode": "source_evidence_first",
                        "checkpoint_ref": "outputs/research/source_index.json",
                        "collection_contract": {
                            "required_item_evidence_fields": ["title", "url"],
                            "required_item_fields": ["title", "url", "abstract", "translated"],
                            "required_item_values": {"translated": True},
                        },
                        "recommended_action": "materialize_checkpoint",
                        "requires_auditable_source_evidence": True,
                        "writer_tool": "api_json_collection",
                    }
                ]
            },
        },
    )

    payload = delivery_repair_payload(SimpleNamespace(root=tmp_path))
    call = payload["required_tool_calls"][0]

    assert call["tool"] == "api_json_collection"
    assert call["source_artifacts"][0]["artifact_ref"] == str(artifact_path)
    assert call["source_artifacts"][0]["item_path"] == "results"
    assert call["fields"]["title"] == "title"
    assert call["fields"]["url"] == "url"
    assert call["fields"]["abstract"]["path"] == "snippet"
    assert call["fields"]["translated"] == {"value": True}
    assert call["evidence_fields"] == ["title", "url"]


def test_delivery_repair_payload_keeps_analysis_columns_for_llm(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_payload import delivery_repair_payload

    artifact_path = _write_tool_output_artifact(tmp_path)
    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "recovery_actions": [
                    {
                        "checkpoint_materialization_mode": "source_evidence_first",
                        "checkpoint_ref": "outputs/report/collected.json",
                        "collection_contract": {
                            "required_item_evidence_fields": ["title", "url"],
                            "required_item_fields": ["title", "url", "中文说明", "说明依据", "summary"],
                        },
                        "recommended_action": "materialize_checkpoint",
                        "requires_auditable_source_evidence": True,
                        "writer_tool": "api_json_collection",
                    }
                ]
            },
        },
    )

    payload = delivery_repair_payload(SimpleNamespace(root=tmp_path))
    call = payload["required_tool_calls"][0]

    assert call["source_artifacts"][0]["artifact_ref"] == str(artifact_path)
    assert call["fields"]["title"] == "title"
    assert call["fields"]["url"] == "url"
    assert "中文说明" not in call["fields"]
    assert "说明依据" not in call["fields"]
    assert "summary" not in call["fields"]


# LLM: Refreshed closeout repairs should turn collection count failures into source collection calls.
# 函数用途: 验证旧 closeout 缺少新动作时，payload 能从当前合同和 finding 重新推导 api_json_collection 返工。
def test_delivery_repair_payload_refreshes_collection_count_failure(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_payload import delivery_repair_payload

    artifact_path = _write_tool_output_artifact(tmp_path)
    current_contract = _collection_delivery_contract()
    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {"recovery_actions": []},
            "artifacts": [
                {
                    "acceptance_report": {
                        "findings": [
                            {
                                "code": "COLLECTION_TOO_FEW_ITEMS",
                                "location": "outputs/research/source_index.json",
                                "value": "2",
                            }
                        ],
                        "ok": False,
                    },
                    "artifact_id": "research_pdf",
                    "kind": "pdf",
                    "ok": False,
                    "path": str(tmp_path / "outputs/research/research.pdf"),
                }
            ],
        },
    )

    payload = delivery_repair_payload(SimpleNamespace(root=tmp_path), current_contract)
    call = next(item for item in payload["required_tool_calls"] if item["tool"] == "api_json_collection")

    assert call["path"] == "outputs/research/source_index.json"
    assert call["source_artifacts"][0]["artifact_ref"] == str(artifact_path)
    assert call["fields"]["title"] == "title"
    assert call["fields"]["url"] == "url"
    assert call["fields"]["translated"] == {"value": True}
    assert call["collection_contract"]["min_items_total"] == 3


# LLM: Date-bound collection findings should route back through source collection, not manual prose repair.
# 函数用途: 验证时间窗口不合格时返工调用携带 item_date_bounds，让来源采集工具能过滤越界条目。
def test_delivery_repair_payload_refreshes_collection_date_failure(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_payload import delivery_repair_payload

    _write_tool_output_artifact(tmp_path)
    current_contract = _collection_delivery_contract()
    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {"recovery_actions": []},
            "artifacts": [
                {
                    "acceptance_report": {
                        "findings": [
                            {
                                "code": "COLLECTION_ITEM_DATE_BEFORE_MIN",
                                "location": "outputs/research/source_index.json#1:date",
                                "value": json.dumps({"actual": "2024-12-01", "min": "2025-01-01"}),
                            }
                        ],
                        "ok": False,
                    },
                    "artifact_id": "research_pdf",
                    "ok": False,
                    "path": str(tmp_path / "outputs/research/research.pdf"),
                }
            ],
        },
    )

    payload = delivery_repair_payload(SimpleNamespace(root=tmp_path), current_contract)
    call = next(item for item in payload["required_tool_calls"] if item["tool"] == "api_json_collection")

    assert call["path"] == "outputs/research/source_index.json"
    assert call["item_date_bounds"] == {"field": "date", "min": "2025-01-01"}
    assert call["collection_contract"]["item_date_bounds"] == {"field": "date", "min": "2025-01-01"}


# LLM: Source collection repairs must precede builders when both are present.
# 函数用途: 验证来源清单没达标时，required_tool_calls 先给 api_json_collection，而不是先构建派生产物。
def test_delivery_repair_payload_prioritizes_source_collection_before_builder(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_payload import delivery_repair_payload

    _write_tool_output_artifact(tmp_path)
    current_contract = _collection_delivery_contract()
    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "recovery_actions": [
                    {
                        "builder_tool": "markdown_to_pdf",
                        "code": "STAGING_BUILDER_READY",
                        "output_ref": "outputs/research/research.pdf",
                        "recommended_action": "invoke_builder_tool",
                        "source_ref": "outputs/research/research.md",
                    }
                ]
            },
            "artifacts": [
                {
                    "acceptance_report": {
                        "findings": [
                            {
                                "code": "COLLECTION_TOO_FEW_ITEMS",
                                "location": "outputs/research/source_index.json",
                                "value": "2",
                            }
                        ],
                        "ok": False,
                    },
                    "artifact_id": "research_pdf",
                    "ok": False,
                    "path": str(tmp_path / "outputs/research/research.pdf"),
                }
            ],
        },
    )

    payload = delivery_repair_payload(SimpleNamespace(root=tmp_path), current_contract)

    assert payload["required_tool_calls"][0]["tool"] == "api_json_collection"
    assert payload["required_tool_calls"][0]["path"] == "outputs/research/source_index.json"


# LLM: mapping repairs must expose source item keys as machine fields, not prose.
# 函数用途: 验证 ARTIFACT_MAPPING_MISSING 会把缺失 key 带进 required call，后续 guard 才能判断空壳写入。
def test_required_tool_calls_include_mapping_required_keys() -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_required_calls import (
        required_tool_calls,
    )

    calls = required_tool_calls([_mapping_repair_action()])

    assert calls == [
        {
            "tool": "write_file",
            "path": "outputs/document_delivery/document_delivery_zh.md",
            "finding_values": [_mapping_finding_value()],
            "mapping_required_count": 3,
            "mapping_required_keys": [
                {"title": "Paper A"},
                {"title": "Paper B"},
                {"title": "Paper C"},
            ],
            "mutation_intent": "rewrite",
        }
    ]


# LLM: Mapping repair should become executable once the source collection exists.
# 函数用途: 验证集合映射失败时，payload 会从 source_json_ref 生成最小 Markdown 写入内容，而不是只给模型空壳提示。
def test_delivery_repair_payload_generates_markdown_from_collection_mapping(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_payload import delivery_repair_payload

    _write_source_index(tmp_path)
    current_contract = _collection_delivery_contract()
    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {"recovery_actions": []},
            "artifacts": [
                {
                    "acceptance_report": {
                        "findings": [
                            {
                                "code": "ARTIFACT_MAPPING_MISSING",
                                "location": "outputs/research/research.md",
                                "value": _mapping_finding_value(),
                            }
                        ],
                        "ok": False,
                    },
                    "artifact_id": "research_pdf",
                    "ok": False,
                    "path": str(tmp_path / "outputs/research/research.pdf"),
                }
            ],
        },
    )

    payload = delivery_repair_payload(SimpleNamespace(root=tmp_path), current_contract)
    call = next(item for item in payload["required_tool_calls"] if item.get("path") == "outputs/research/research.md")

    assert call["tool"] == "write_file"
    assert call["source_ref"] == "outputs/research/source_index.json"
    assert "content" in call
    assert "Paper A" in call["content"]
    assert "https://example.com/a" in call["content"]
    assert "Paper C" in call["content"]


def _mapping_repair_action() -> dict[str, object]:
    return {
        "recommended_action": "repair_artifact_against_findings",
        "artifact_path": "outputs/document_delivery/document_delivery_zh.md",
        "finding_codes": ["ARTIFACT_MAPPING_MISSING"],
        "finding_values": [_mapping_finding_value()],
        "write_tools": ["write_file", "replace_in_file"],
    }


def _mapping_finding_value() -> str:
    return json.dumps(
        {
            "mapped": 0,
            "required": 3,
            "missing_keys": [
                {"title": "Paper A"},
                {"title": "Paper B"},
                {"title": "Paper C"},
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _collection_delivery_contract() -> dict[str, object]:
    return {
        "artifacts": [
            {
                "artifact_id": "research_pdf",
                "path": "outputs/research/research.pdf",
                "validation_contract": {
                    "collection_contract": {
                        "source_json_ref": "outputs/research/source_index.json",
                        "items_path": "rows",
                        "item_date_bounds": {"field": "date", "min": "2025-01-01"},
                        "mapping": {
                            "artifact_ref": "outputs/research/research.md",
                            "key_fields": ["title"],
                            "min_mapped_items": 3,
                        },
                        "min_items_total": 3,
                        "required_item_evidence_fields": ["title", "url"],
                        "required_item_fields": ["title", "url", "abstract", "translated"],
                        "required_item_values": {"translated": True},
                    }
                },
            }
        ]
    }


def _write_tool_output_artifact(root: Path) -> Path:
    artifact_dir = root / "memory_archive/artifacts/tool_outputs"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / "web_search-1.json"
    artifact_path.write_text(
        json.dumps(
            {
                "content": json.dumps(
                    {
                        "results": [
                            {"snippet": "摘要", "title": "Paper", "url": "https://example.com/paper"}
                        ]
                    },
                    ensure_ascii=False,
                ),
                "ok": True,
                "tool": "web_search",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (artifact_dir / "index.jsonl").write_text(
        json.dumps(
            {
                "call_id": "1-1",
                "path": str(artifact_path),
                "scoped_call_id": "run-1:1-1",
                "sha256": "abc",
                "tool": "web_search",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return artifact_path


def _write_source_index(root: Path) -> None:
    path = root / "outputs/research/source_index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"title": "Paper A", "url": "https://example.com/a", "date": "2025-01-01", "translated": True},
        {"title": "Paper B", "url": "https://example.com/b", "date": "2025-02-01", "translated": True},
        {"title": "Paper C", "url": "https://example.com/c", "date": "2025-03-01", "translated": True},
    ]
    path.write_text(json.dumps({"sheets": [{"name": "source", "rows": rows}]}, ensure_ascii=False), encoding="utf-8")


def _write_closeout(root: Path, payload: dict[str, object]) -> None:
    path = root / ".agent_delivery/closeout.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
