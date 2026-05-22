from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.tests.tool_delivery_repair_fixtures import _write_closeout


def _agent(root: Path) -> SimpleNamespace:
    return SimpleNamespace(root=root)


def _write_actions(root: Path, actions: list[dict[str, object]], **progress: object) -> None:
    _write_closeout(root, {"ok": False, "delivery_progress": {"recovery_actions": actions, **progress}})


# LLM: builder-ready delivery repair treats read_artifact as inspection-only.
# 函数用途: 验证进入 invoke_builder_tool 阶段后，单独 read_artifact 不再算推进动作。
def test_delivery_repair_guard_treats_read_artifact_as_nonproductive_when_builder_ready(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_actions(tmp_path, [_builder_ready_action()])

    assert is_delivery_repair_productive_call(_agent(tmp_path), [{"tool": "read_artifact", "artifact_ref": "demo.json"}]) is False


def _builder_ready_action() -> dict[str, object]:
    return {
        "code": "STAGING_BUILDER_READY",
        "recommended_action": "invoke_builder_tool",
        "builder_tool": "data_to_workbook",
        "source_ref": "outputs/report/source.json",
        "output_ref": "outputs/report/report.xlsx",
    }


# LLM: missing-checkpoint repair may still read artifact evidence before materializing a file.
# 函数用途: 验证 materialize_checkpoint 阶段不会把 read_artifact 误判为空转。
def test_delivery_repair_guard_allows_read_artifact_before_checkpoint_exists(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_actions(tmp_path, [_missing_checkpoint_action()])

    assert is_delivery_repair_productive_call(_agent(tmp_path), [{"tool": "read_artifact", "artifact_ref": "demo.json"}]) is True


# LLM: declared repair target reads are a bounded context allowance, not endless progress.
# 函数用途: 验证同一修复目标在文件没变化时只能读取一次，避免恢复任务反复读旧产物不写入。
def test_delivery_repair_guard_bounds_repeated_declared_target_reads(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    target = tmp_path / "outputs" / "research_documents" / "source_index.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"rows":[{"translated":false}]}', encoding="utf-8")
    _write_actions(tmp_path, [_source_index_repair_action()])
    agent = _agent(tmp_path)
    read_call = {"tool": "read_file", "path": "outputs/research_documents/source_index.json"}

    assert is_delivery_repair_productive_call(agent, [read_call]) is True
    assert is_delivery_repair_productive_call(agent, [read_call]) is False

    assert is_delivery_repair_productive_call(agent, [_source_index_write_call()]) is True
    target.write_text('{"rows":[{"translated":true}]}', encoding="utf-8")
    assert is_delivery_repair_productive_call(agent, [read_call]) is True


def _missing_checkpoint_action() -> dict[str, object]:
    return {
        "code": "STAGING_CHECKPOINT_MISSING",
        "recommended_action": "materialize_checkpoint",
        "checkpoint_ref": "outputs/deepseek_papers/source_index.json",
    }


def _source_index_repair_action() -> dict[str, object]:
    return {
        "code": "COLLECTION_ITEM_VALUE_MISMATCH",
        "recommended_action": "repair_structured_checkpoint_json",
        "checkpoint_ref": "outputs/research_documents/source_index.json",
        "writer_tool": "write_structured_json",
    }


def _source_index_write_call() -> dict[str, object]:
    return {
        "tool": "write_structured_json",
        "path": "outputs/research_documents/source_index.json",
        "data": {"rows": [{"translated": True}]},
    }


# LLM: repeated delivery-repair no-progress must require a write/build action.
# 函数用途: 验证取数类工具不能无限循环，真实写入类调用才算推进。
def test_delivery_repair_guard_requires_write_action_after_repeated_no_progress(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_actions(tmp_path, [_no_rows_action(), _evidence_action()], unchanged_failure_count=9, no_progress_block_threshold=5)
    agent = _agent(tmp_path)

    assert is_delivery_repair_productive_call(agent, [{"tool": "run_command", "command": "curl https://example.com"}]) is False
    assert is_delivery_repair_productive_call(agent, [{"tool": "fetch_url", "url": "https://example.com"}]) is False
    assert is_delivery_repair_productive_call(agent, [_write_file_call()]) is True
    assert is_delivery_repair_productive_call(agent, [_structured_rows_call()]) is True


# LLM: source checkpoint materialization must write real rows, not an empty skeleton.
# 函数用途: 验证 materialize_checkpoint 来源数据修复时，空 rows 的 write_structured_json 不算推进。
def test_delivery_repair_guard_rejects_empty_source_checkpoint_write(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_actions(tmp_path, [_source_checkpoint_missing_action()])
    agent = _agent(tmp_path)

    assert is_delivery_repair_productive_call(agent, [_empty_source_checkpoint_write_call()]) is False
    assert is_delivery_repair_productive_call(agent, [_non_empty_source_checkpoint_write_call()]) is True
    assert is_delivery_repair_productive_call(agent, [_generated_rows_list_checkpoint_write_call()]) is True


# LLM: api_json_collection repair skeletons must use collection params, not invalid checkpoint data payloads.
# 函数用途: 验证来源采集恢复上下文给模型的是 api_json_collection 参数骨架，不是 write_structured_json 的 data 形状。
def test_delivery_repair_context_renders_api_collection_required_call(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        delivery_repair_context,
    )

    _write_actions(tmp_path, [_api_collection_action()])
    payload = delivery_repair_context(_agent(tmp_path), repairs=0).splitlines()[1]
    required_call = json.loads(payload)["required_tool_calls"][0]

    assert required_call["tool"] == "api_json_collection"
    assert required_call["path"] == "outputs/github_star_growth/source_data.json"
    assert required_call["columns"] == ["项目名", "地址", "上升 star 数", "中文解释", "推荐理由"]
    assert "data" not in required_call
    assert "fields" in required_call
    assert "collection_contract" in required_call


# LLM: collection value repairs should render executable write_structured_json update calls.
# 函数用途: 验证 required_tool_calls 暴露的是结构化集合更新参数，而不是让模型猜怎么改 JSON 文本。
def test_delivery_repair_context_renders_collection_item_update_call(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        delivery_repair_context,
    )

    _write_actions(tmp_path, [_collection_item_update_action()])
    payload = delivery_repair_context(_agent(tmp_path), repairs=0).splitlines()[1]
    required_call = json.loads(payload)["required_tool_calls"][0]

    assert required_call == {
        "tool": "write_structured_json",
        "path": "outputs/research_documents/source_index.json",
        "collection_item_updates": [{"item_index": 1, "field_path": "translated", "value": True}],
        "items_path": "rows",
        "merge_existing": True,
    }


def _no_rows_action() -> dict[str, object]:
    return {
        "code": "STAGED_JSON_NO_ROWS",
        "recommended_action": "write_non_empty_structured_rows",
        "checkpoint_ref": "outputs/github_star_growth/source_data.json",
        "required_columns": ["项目名", "地址"],
    }


def _evidence_action() -> dict[str, object]:
    return {
        "code": "EVIDENCE_REQUIRED_FIELD_MISSING",
        "recommended_action": "repair_evidence_refs",
        "checkpoint_ref": "outputs/github_star_growth/source_data.json",
    }


def _api_collection_action() -> dict[str, object]:
    return {
        "code": "STAGED_JSON_REQUIRED_COLUMNS_MISSING",
        "recommended_action": "repair_structured_checkpoint_json",
        "checkpoint_ref": "outputs/github_star_growth/source_data.json",
        "writer_tool": "api_json_collection",
        "write_tools": ["api_json_collection", "write_structured_json"],
        "required_columns": ["项目名", "地址", "上升 star 数", "中文解释", "推荐理由"],
        "collection_contract": {
            "source_json_ref": "outputs/github_star_growth/source_data.json",
            "groups_path": "sheets",
            "items_path": "rows",
            "min_groups": 21,
            "min_items_per_group": 10,
            "required_item_fields": ["项目名", "地址", "上升 star 数", "中文解释", "推荐理由"],
        },
        "checkpoint_shape_hint": (
            '{"completion_evidence":{"scope":"year_to_date"},'
            '"sheets":[{"columns":["项目名","地址","上升 star 数","中文解释","推荐理由"],"rows":[]}]}'
        ),
    }


def _source_checkpoint_missing_action() -> dict[str, object]:
    return {
        "code": "STAGING_CHECKPOINT_MISSING",
        "recommended_action": "materialize_checkpoint",
        "checkpoint_ref": "outputs/research_documents/source_index.json",
        "checkpoint_materialization_mode": "source_evidence_first",
        "writer_tool": "api_json_collection",
        "write_tools": ["api_json_collection", "write_structured_json"],
    }


def _empty_source_checkpoint_write_call() -> dict[str, object]:
    return {
        "tool": "write_structured_json",
        "path": "outputs/research_documents/source_index.json",
        "data": {"completion_evidence": {"scope": "fixture"}, "rows": []},
    }


def _non_empty_source_checkpoint_write_call() -> dict[str, object]:
    return {
        "tool": "write_structured_json",
        "path": "outputs/research_documents/source_index.json",
        "data": {"completion_evidence": {"scope": "fixture"}, "rows": [{"title": "Paper"}]},
    }


def _generated_rows_list_checkpoint_write_call() -> dict[str, object]:
    return {
        "tool": "write_structured_json",
        "path": "outputs/research_documents/source_index.json",
        "generated_rows": [{"title": "Paper", "translated": True}],
        "completion_evidence": {"scope": "fixture"},
    }


def _collection_item_update_action() -> dict[str, object]:
    return {
        "code": "COLLECTION_ITEM_VALUE_MISMATCH",
        "recommended_action": "repair_collection_item_values",
        "checkpoint_ref": "outputs/research_documents/source_index.json",
        "writer_tool": "write_structured_json",
        "items_path": "rows",
        "collection_item_updates": [{"item_index": 1, "field_path": "translated", "value": True}],
    }


def _write_file_call() -> dict[str, object]:
    return {"tool": "write_file", "path": "outputs/github_star_growth/source_data.json", "content": "{}"}


def _structured_rows_call() -> dict[str, object]:
    return {
        "tool": "write_structured_json",
        "path": "outputs/github_star_growth/source_data.json",
        "rows": [{"项目名": "demo"}],
    }


# LLM: writer_tool is an executable machine contract for structured checkpoints.
# 函数用途: 验证指定 write_structured_json 的阶段修复不能用 write_file 绕过结构化校验。
def test_delivery_repair_guard_requires_declared_writer_tool_for_structured_checkpoint(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_actions(tmp_path, [_structured_repair_action()])
    agent = _agent(tmp_path)

    assert is_delivery_repair_productive_call(agent, [_write_file_call()]) is False
    assert is_delivery_repair_productive_call(agent, [_structured_rows_call()]) is True


def _structured_repair_action() -> dict[str, object]:
    return {
        "code": "STAGED_JSON_TOO_FEW_SHEETS",
        "recommended_action": "repair_structured_checkpoint_json",
        "checkpoint_ref": "outputs/github_star_growth/source_data.json",
        "writer_tool": "write_structured_json",
    }


# LLM: Evidence repair must merge metadata into an existing checkpoint.
# 函数用途: 验证 repair_evidence_refs 的写入必须带 merge_existing，防止补证据时覆盖已有阶段数据。
def test_delivery_repair_guard_requires_merge_existing_for_evidence_repair(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_actions(tmp_path, [_evidence_writer_action()])
    agent = _agent(tmp_path)

    assert is_delivery_repair_productive_call(agent, [_evidence_write_call(merge_existing=False)]) is False
    assert is_delivery_repair_productive_call(agent, [_evidence_write_call(merge_existing=True)]) is True


def _evidence_writer_action() -> dict[str, object]:
    return {
        "code": "EVIDENCE_REQUIRED_FIELD_MISSING",
        "recommended_action": "repair_evidence_refs",
        "checkpoint_ref": "outputs/github_star_growth/source_data.json",
        "writer_tool": "write_structured_json",
        "required_fields": ["地址"],
    }


def _evidence_write_call(*, merge_existing: bool) -> dict[str, object]:
    return {
        "tool": "write_structured_json",
        "path": "outputs/github_star_growth/source_data.json",
        "merge_existing": merge_existing,
        "data": {
            "source_refs": [{"source_id": "src-1", "uri": "https://example.com"}],
            "claims": [{"field": "地址", "source_ids": ["src-1"], "value": "https://example.com"}],
        },
    }
