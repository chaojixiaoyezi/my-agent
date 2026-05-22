from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.agent.tooling.structured_json_writer import StructuredJsonTool


# LLM: StructuredJsonTool lets models submit data objects instead of hand-escaped JSON text.
# 函数用途: 验证 rows/sheets 会由工具稳定序列化成有效 JSON checkpoint。
def test_structured_json_tool_writes_valid_json_from_sheets(tmp_path: Path) -> None:
    tool = StructuredJsonTool(tmp_path)

    result = tool.execute(
        {
            "path": "outputs/report/source_data.json",
            "sheets": [
                {
                    "name": "week-1",
                    "columns": ["项目名", "地址"],
                    "rows": [{"项目名": "demo", "地址": "https://example.com"}],
                }
            ],
        }
    )

    assert result.ok, result.output
    data = json.loads((tmp_path / "outputs/report/source_data.json").read_text(encoding="utf-8"))
    assert data["sheets"][0]["rows"][0]["项目名"] == "demo"
    assert result.result_envelope["artifact_ref"] == "outputs/report/source_data.json"


# LLM: Empty data checkpoints should fail before downstream builders trust them.
# 函数用途: 验证空 rows/sheets 不会被写成“看似存在”的无效 checkpoint。
def test_structured_json_tool_rejects_empty_rows(tmp_path: Path) -> None:
    tool = StructuredJsonTool(tmp_path)

    result = tool.execute({"path": "outputs/report/source_data.json", "rows": []})

    assert not result.ok
    assert result.error_code == "STAGED_JSON_NO_ROWS"
    assert not (tmp_path / "outputs/report/source_data.json").exists()


# LLM: Evidence repair should patch existing checkpoints instead of replacing their data sheets.
# 函数用途: 验证 merge_existing 会保留既有 sheets，同时追加 source_refs/claims 这类证据字段。
def test_structured_json_tool_merges_existing_checkpoint_metadata(tmp_path: Path) -> None:
    tool = StructuredJsonTool(tmp_path)
    target = tmp_path / "outputs/report/source_data.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {"sheets": [{"name": "榜单", "rows": [{"项目名": "demo"}]}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = tool.execute(
        {
            "path": "outputs/report/source_data.json",
            "merge_existing": True,
            "data": {
                "source_refs": [{"source_id": "src-1", "uri": "https://example.com"}],
                "claims": [{"field": "项目名", "source_ids": ["src-1"], "value": "demo"}],
            },
        }
    )

    assert result.ok, result.output
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["sheets"][0]["rows"][0]["项目名"] == "demo"
    assert data["source_refs"][0]["source_id"] == "src-1"
    assert data["claims"][0]["field"] == "项目名"


# LLM: Evidence repair should add missing evidence without discarding earlier claims.
# 函数用途: 验证 merge_existing 对 claims/source_refs 做追加去重，避免修证据时覆盖已有表格证据。
def test_structured_json_tool_appends_evidence_metadata_when_merging(tmp_path: Path) -> None:
    tool = StructuredJsonTool(tmp_path)
    target = tmp_path / "outputs/report/source_data.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {
                "sheets": [{"name": "榜单", "rows": [{"项目名": "demo"}]}],
                "source_refs": [{"source_id": "src-1", "uri": "https://example.com/old"}],
                "claims": [{"field": "项目名", "source_ids": ["src-1"], "value": "demo"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = tool.execute(
        {
            "path": "outputs/report/source_data.json",
            "merge_existing": True,
            "data": {
                "source_refs": [
                    {"source_id": "src-1", "uri": "https://example.com/old"},
                    {"source_id": "src-2", "uri": "https://example.com/new"},
                ],
                "claims": [
                    {"field": "地址", "source_ids": ["src-2"], "value": "https://example.com/new"},
                    {"field": "项目名", "source_ids": ["src-1"], "value": "demo"},
                ],
            },
        }
    )

    assert result.ok, result.output
    data = json.loads(target.read_text(encoding="utf-8"))
    assert [item["source_id"] for item in data["source_refs"]] == ["src-1", "src-2"]
    assert [item["field"] for item in data["claims"]] == ["项目名", "地址"]
    assert data["sheets"][0]["rows"][0]["项目名"] == "demo"


# LLM: large spreadsheet-style checkpoints should be extendable one sheet at a time.
# 函数用途: 验证 merge_existing 对 sheets 使用追加/同名合并，而不是浅替换整份阶段数据。
def test_structured_json_tool_appends_sheets_when_merging_existing_checkpoint(tmp_path: Path) -> None:
    tool = StructuredJsonTool(tmp_path)
    target = tmp_path / "outputs/report/source_data.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {"sheets": [{"name": "2026-W01", "rows": [{"项目名": "demo-1"}]}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = tool.execute(
        {
            "path": "outputs/report/source_data.json",
            "merge_existing": True,
            "sheets": [{"name": "2026-W02", "rows": [{"项目名": "demo-2"}]}],
        }
    )

    assert result.ok, result.output
    data = json.loads(target.read_text(encoding="utf-8"))
    assert [sheet["name"] for sheet in data["sheets"]] == ["2026-W01", "2026-W02"]
    assert data["sheets"][1]["rows"][0]["项目名"] == "demo-2"


# LLM: retries for the same sheet should append rows without discarding previous rows.
# 函数用途: 验证同名 sheet 的 merge_existing 按 rows 追加，支撑大表分批写入。
def test_structured_json_tool_appends_rows_for_same_sheet_name(tmp_path: Path) -> None:
    tool = StructuredJsonTool(tmp_path)
    target = tmp_path / "outputs/report/source_data.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {"sheets": [{"name": "2026-W01", "rows": [{"项目名": "demo-1"}]}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = tool.execute(
        {
            "path": "outputs/report/source_data.json",
            "merge_existing": True,
            "sheets": [{"name": "2026-W01", "rows": [{"项目名": "demo-2"}]}],
        }
    )

    assert result.ok, result.output
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["sheets"][0]["rows"] == [{"项目名": "demo-1"}, {"项目名": "demo-2"}]


# LLM: merge_existing should behave as an upsert so the first chunk can create the checkpoint.
# 函数用途: 验证分批写入第一块时目标不存在也能创建，后续块再 merge 追加。
def test_structured_json_tool_merge_existing_creates_missing_checkpoint(tmp_path: Path) -> None:
    tool = StructuredJsonTool(tmp_path)

    result = tool.execute(
        {
            "path": "outputs/report/source_data.json",
            "merge_existing": True,
            "sheets": [{"name": "2026-W01", "rows": [{"项目名": "demo"}]}],
        }
    )

    assert result.ok, result.output
    data = json.loads((tmp_path / "outputs/report/source_data.json").read_text(encoding="utf-8"))
    assert data["sheets"][0]["rows"][0]["项目名"] == "demo"


# LLM: Empty data should not mask non-empty rows/sheets supplied in the same tool call.
# 函数用途: 验证模型误带 data={} 时，工具仍可使用同次调用里的非空 sheets 写出 checkpoint。
def test_structured_json_tool_uses_sheets_when_data_is_empty(tmp_path: Path) -> None:
    tool = StructuredJsonTool(tmp_path)

    result = tool.execute(
        {
            "path": "outputs/report/source_data.json",
            "data": {},
            "sheets": [{"name": "榜单", "rows": [{"项目名": "demo"}]}],
        }
    )

    assert result.ok, result.output
    data = json.loads((tmp_path / "outputs/report/source_data.json").read_text(encoding="utf-8"))
    assert data["sheets"][0]["rows"][0]["项目名"] == "demo"


# LLM: Non-empty metadata in data must not silently discard sibling sheets/rows.
# 函数用途: 验证同一次 write_structured_json 可同时写 completion_evidence/source_refs 和 sheets。
def test_structured_json_tool_merges_nonempty_data_with_sheets(tmp_path: Path) -> None:
    tool = StructuredJsonTool(tmp_path)

    result = tool.execute(
        {
            "path": "outputs/report/source_data.json",
            "data": {"completion_evidence": {"scope": "fixture"}},
            "sheets": [{"name": "榜单", "rows": [{"项目名": "demo"}]}],
        }
    )

    assert result.ok, result.output
    data = json.loads((tmp_path / "outputs/report/source_data.json").read_text(encoding="utf-8"))
    assert data["completion_evidence"]["scope"] == "fixture"
    assert data["sheets"][0]["rows"][0]["项目名"] == "demo"


# LLM: Top-level metadata fields are structured tool facts and must not be dropped in rows mode.
# 函数用途: 验证 completion_evidence/source_refs/claims 写在工具顶层时，也会进入 JSON checkpoint。
def test_structured_json_tool_preserves_top_level_metadata_with_rows(tmp_path: Path) -> None:
    tool = StructuredJsonTool(tmp_path)

    result = tool.execute(
        {
            "path": "outputs/docs/source_index.json",
            "completion_evidence": {"scope": "all-public-documents"},
            "source_refs": [
                {
                    "source_id": "src-1",
                    "uri": "https://example.com/paper",
                    "reserved": {"tool_call_id": "call-1"},
                }
            ],
            "claims": [
                {
                    "field": "title",
                    "source_ids": ["src-1"],
                    "value": "Paper",
                    "reserved": {"item_index": 0},
                }
            ],
            "rows": [
                {
                    "title": "Paper",
                    "url": "https://example.com/paper",
                    "field_source_ids": {"title": ["src-1"]},
                }
            ],
        }
    )

    assert result.ok, result.output
    data = json.loads((tmp_path / "outputs/docs/source_index.json").read_text(encoding="utf-8"))
    assert data["completion_evidence"]["scope"] == "all-public-documents"
    assert data["source_refs"][0]["source_id"] == "src-1"
    assert data["claims"][0]["field"] == "title"
    assert data["rows"][0]["title"] == "Paper"


# LLM: Some model adapters stringify nested JSON args; the writer should normalize that machine payload.
# 函数用途: 验证 data 为 JSON 字符串时仍按结构化对象落地，避免 metadata/completion_evidence 被丢弃。
def test_structured_json_tool_accepts_json_string_data_payload(tmp_path: Path) -> None:
    tool = StructuredJsonTool(tmp_path)

    result = tool.execute(
        {
            "path": "outputs/docs/source_index.json",
            "data": json.dumps(
                {
                    "completion_evidence": {"scope": "all-public-documents"},
                    "rows": [{"title": "Paper", "url": "https://example.com", "date": "2026-01-01"}],
                },
                ensure_ascii=False,
            ),
        }
    )

    assert result.ok, result.output
    data = json.loads((tmp_path / "outputs/docs/source_index.json").read_text(encoding="utf-8"))
    assert data["completion_evidence"]["scope"] == "all-public-documents"
    assert data["rows"][0]["title"] == "Paper"


# LLM: collection item updates repair row-level machine fields without rewriting whole checkpoints.
# 函数用途: 验证 write_structured_json 能按结构化 item_index/field_path 更新已有集合行，支撑验收 finding 的通用修复。
def test_structured_json_tool_applies_collection_item_updates(tmp_path: Path) -> None:
    tool = StructuredJsonTool(tmp_path)
    source = tmp_path / "outputs/docs/source_index.json"
    source.parent.mkdir(parents=True)
    source.write_text(
        json.dumps(
            {
                "rows": [
                    {"title": "Paper A", "translated": False},
                    {"title": "Paper B", "translated": False},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = tool.execute(
        {
            "path": "outputs/docs/source_index.json",
            "collection_item_updates": [
                {"item_index": 1, "field_path": "translated", "value": True},
            ],
        }
    )

    assert result.ok, result.output
    data = json.loads(source.read_text(encoding="utf-8"))
    assert data["rows"][0]["translated"] is False
    assert data["rows"][1]["translated"] is True


# LLM: Generated rows are a generic structured alternative to hand-emitting huge JSON payloads.
# 函数用途: 验证 write_structured_json 可按机器规则生成大表，并同步生成多 sheet checkpoint。
def test_structured_json_tool_generates_rows_from_structured_series(tmp_path: Path) -> None:
    tool = StructuredJsonTool(tmp_path)

    result = tool.execute(
        {
            "path": "outputs/report/source_data.json",
            "data": {"completion_evidence": {"scope": "fixture", "row_count": 1000}},
            "generated_rows": {
                "count": 1000,
                "columns": ["订单ID", "月份", "地区", "销售额", "利润"],
                "fields": {
                    "订单ID": {"format": "ORD-{index:04d}", "start": 1},
                    "月份": {"cycle": ["2026-01", "2026-02"]},
                    "地区": {"cycle": ["华东", "华南", "华北"]},
                    "销售额": {"number": {"start": 1000, "step": 37, "modulo": 9000}},
                    "利润": {"multiply": {"source": "销售额", "factor": 0.2, "decimals": 2}},
                },
                "sheets": {"count": 3, "prefix": "数据"},
            },
        }
    )

    assert result.ok, result.output
    data = json.loads((tmp_path / "outputs/report/source_data.json").read_text(encoding="utf-8"))
    assert len(data["rows"]) == 1000
    assert data["rows"][0]["订单ID"] == "ORD-0001"
    assert data["rows"][1]["月份"] == "2026-02"
    assert data["rows"][0]["利润"] == 200
    assert [sheet["name"] for sheet in data["sheets"]] == ["数据1", "数据2", "数据3"]
    assert sum(len(sheet["rows"]) for sheet in data["sheets"]) == 1000


# LLM: Generated-row aliases should tolerate compact model-produced rule shapes without silent blanks.
# 函数用途: 验证 source/factor 和顶层 modulo 这类结构化别名会被规范化，而不是写出空字段。
def test_structured_json_tool_accepts_generated_rule_aliases(tmp_path: Path) -> None:
    tool = StructuredJsonTool(tmp_path)

    result = tool.execute(
        {
            "path": "outputs/report/source_data.json",
            "generated_rows": {
                "count": 3,
                "columns": ["销售额", "利润"],
                "fields": {
                    "销售额": {"number": {"start": 1000, "step": 500}, "modulo": 1200},
                    "利润": {"source": "销售额", "factor": 0.2, "decimals": 2},
                },
            },
        }
    )

    assert result.ok, result.output
    data = json.loads((tmp_path / "outputs/report/source_data.json").read_text(encoding="utf-8"))
    assert data["rows"] == [
        {"销售额": 1000, "利润": 200},
        {"销售额": 300, "利润": 60},
        {"销售额": 800, "利润": 160},
    ]


# LLM: Some providers put literal row lists in generated_rows; normalize that at the tool boundary.
# 函数用途: 验证 generated_rows 误放对象数组时按 rows 处理，避免模型参数别名导致 checkpoint 不落盘。
def test_structured_json_tool_treats_generated_rows_list_as_rows(tmp_path: Path) -> None:
    tool = StructuredJsonTool(tmp_path)

    result = tool.execute(
        {
            "path": "outputs/research_documents/source_index.json",
            "generated_rows": [
                {
                    "title": "DeepSeek-R1",
                    "url": "https://arxiv.org/abs/2501.12948",
                    "date": "2025-01-22",
                    "translated": True,
                }
            ],
            "completion_evidence": {"scope": "fixture"},
        }
    )

    assert result.ok, result.output
    data = json.loads((tmp_path / "outputs/research_documents/source_index.json").read_text(encoding="utf-8"))
    assert data["completion_evidence"] == {"scope": "fixture"}
    assert data["rows"][0]["translated"] is True


# LLM: Tool registry visibility lets staged repair actions point to the writer tool.
# 函数用途: 验证主代理默认可以检索到 write_structured_json。
def test_tool_registry_registers_structured_json_tool(tmp_path: Path) -> None:
    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=tmp_path,
            max_chars=1000,
            max_entries=50,
            max_matches=20,
            web_max_chars=1000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=10,
            vector_search_enabled=False,
        )
    )

    assert "write_structured_json" in registry.tools
    hits = registry.find_relevant_specs("写 source_data JSON rows sheets")
    assert any(spec.name == "write_structured_json" for spec in hits)
