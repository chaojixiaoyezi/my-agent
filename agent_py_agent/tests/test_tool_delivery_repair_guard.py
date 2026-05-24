from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.tests.tool_delivery_repair_fixtures import _write_closeout


def _agent(root: Path) -> SimpleNamespace:
    return SimpleNamespace(root=root)


def _agent_with_workspace(root: Path, workspace_root: Path) -> SimpleNamespace:
    return SimpleNamespace(root=root, tools=SimpleNamespace(workspace_root=workspace_root))


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


# LLM: zero delivery repair max means unlimited structured repair hints.
# 函数用途: 验证阶段产物返工提示的 0 次数预算不会在高 repairs 计数时消失。
def test_delivery_repair_context_zero_max_repairs_is_unlimited(tmp_path: Path, monkeypatch) -> None:
    from agent_py_agent.agent.agent_core import tool_delivery_repair_guard as guard

    _write_actions(tmp_path, [_builder_ready_action()])
    monkeypatch.setattr(guard, "_MAX_REPAIRS", 0)

    assert guard.delivery_repair_context(_agent(tmp_path), repairs=99)


def _builder_ready_action() -> dict[str, object]:
    return {
        "code": "STAGING_BUILDER_READY",
        "recommended_action": "invoke_builder_tool",
        "builder_tool": "data_to_workbook",
        "source_ref": "outputs/report/source.json",
        "output_ref": "outputs/report/report.xlsx",
    }


# LLM: missing-checkpoint repair may read declared artifact evidence before materializing a file.
# 函数用途: 验证 materialize_checkpoint 只放行 recovery action 明确声明的 artifact 来源。
def test_delivery_repair_guard_allows_declared_read_artifact_before_checkpoint_exists(tmp_path: Path) -> None:
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

    target = tmp_path / "outputs" / "document_delivery" / "source_index.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"rows":[{"translated":false}]}', encoding="utf-8")
    _write_actions(tmp_path, [_source_index_repair_action()])
    agent = _agent(tmp_path)
    read_call = {"tool": "read_file", "path": "outputs/document_delivery/source_index.json"}

    assert is_delivery_repair_productive_call(agent, [read_call]) is True
    assert is_delivery_repair_productive_call(agent, [read_call]) is False

    assert is_delivery_repair_productive_call(agent, [_source_index_write_call()]) is True
    target.write_text('{"rows":[{"translated":true}]}', encoding="utf-8")
    assert is_delivery_repair_productive_call(agent, [read_call]) is True


def test_delivery_repair_guard_does_not_drop_new_gathering_when_repeated_read_is_present(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    target = tmp_path / "outputs" / "document_delivery" / "source_index.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"rows":[{"title":""}]}', encoding="utf-8")
    _write_actions(tmp_path, [_source_checkpoint_missing_action()])
    agent = _agent(tmp_path)
    read_call = {"tool": "read_file", "path": "outputs/document_delivery/source_index.json"}
    search_call = {"tool": "web_search", "query": "source records", "limit": 5}
    inspection_call = {"tool": "list_files", "path": "outputs/document_delivery"}

    assert is_delivery_repair_productive_call(agent, [read_call]) is True
    assert is_delivery_repair_productive_call(agent, [read_call]) is False
    assert is_delivery_repair_productive_call(agent, [read_call, search_call]) is True
    assert is_delivery_repair_productive_call(agent, [read_call, inspection_call]) is False


def _missing_checkpoint_action() -> dict[str, object]:
    return {
        "code": "STAGING_CHECKPOINT_MISSING",
        "recommended_action": "materialize_checkpoint",
        "checkpoint_ref": "outputs/document_bundle/source_index.json",
        "source_artifact_refs": ["demo.json"],
    }


def _source_index_repair_action() -> dict[str, object]:
    return {
        "code": "COLLECTION_ITEM_VALUE_MISMATCH",
        "recommended_action": "repair_structured_checkpoint_json",
        "checkpoint_ref": "outputs/document_delivery/source_index.json",
        "writer_tool": "write_structured_json",
    }


def _source_index_write_call() -> dict[str, object]:
    return {
        "tool": "write_structured_json",
        "path": "outputs/document_delivery/source_index.json",
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


# LLM: staged repair writes must hit declared machine targets, not any sibling file in the output directory.
# 函数用途: 验证恢复阶段不能把非当前 repair target 的写入算作有效推进。
def test_delivery_repair_guard_rejects_write_to_undeclared_repair_target(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_actions(tmp_path, [_missing_markdown_action()])
    agent = _agent(tmp_path)

    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "write_file", "path": "outputs/document_delivery/source_index.json", "content": "{}"}],
        )
        is False
    )
    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "write_file", "path": "outputs/document_delivery/document_delivery_zh.md", "content": "# 译文"}],
        )
        is True
    )


# LLM: evidence gathering is useful for source checkpoints, but not for already-declared artifact writes.
# 函数用途: 验证恢复动作已经进入写 md/pdf 等产物阶段后，继续 web_search 不算修复进展。
def test_delivery_repair_guard_rejects_evidence_gathering_when_write_target_is_ready(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_actions(tmp_path, [_missing_markdown_action()])
    agent = _agent(tmp_path)

    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "web_search", "query": "open source llm papers 2025", "limit": 5}],
        )
        is False
    )
    assert is_delivery_repair_productive_call(agent, [_source_checkpoint_api_collection_call()]) is False


def test_delivery_repair_guard_allows_evidence_gathering_for_source_checkpoint_materialization(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_actions(tmp_path, [_source_checkpoint_missing_action()])
    agent = _agent(tmp_path)

    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "web_search", "query": "open source llm papers 2025", "limit": 5}],
        )
        is True
    )


# LLM: Materialized workbook contracts should repair through a source checkpoint before final xlsx writes.
# 函数用途: 验证入口自动派生的表格阶段合同会让缺失 workbook 先进入来源采集/JSON checkpoint 修复，而不是硬写最终 xlsx。
def test_delivery_repair_guard_does_not_invent_workbook_staging_for_missing_artifact(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
    )
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        delivery_repair_context,
        is_delivery_repair_productive_call,
    )

    contract = materialized_delivery_contract(
        {
            "artifacts": [
                {
                    "artifact_id": "weekly_workbook",
                    "kind": "xlsx",
                    "preferred_path": "outputs/result.xlsx",
                    "validation_contract": {
                        "required_columns": ["project", "metric"],
                    },
                }
            ]
        },
        workspace_root=tmp_path,
    )
    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "artifacts": [
                {
                    "artifact_id": "weekly_workbook",
                    "kind": "xlsx",
                    "path": str(tmp_path / "outputs/result.xlsx"),
                    "ok": False,
                    "acceptance_report": {
                        "findings": [
                            {
                                "code": "ARTIFACT_MISSING",
                                "location": str(tmp_path / "outputs/result.xlsx"),
                                "severity": "hard",
                            }
                        ]
                    },
                }
            ],
            "delivery_progress": {},
        },
    )
    agent = _agent(tmp_path)
    runtime_params = SimpleNamespace(delivery_contract=contract, task_attributes={})

    context = delivery_repair_context(agent, repairs=0, runtime_params=runtime_params)
    payload = json.loads(context.splitlines()[1])
    assert payload["required_actions"][0]["recommended_action"] == "repair_artifact_against_findings"
    assert payload["required_actions"][0]["artifact_path"] == str(tmp_path / "outputs/result.xlsx")
    assert payload["required_tool_calls"][0]["tool"] in {"write_file", "run_command"}
    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "web_search", "query": "fresh sources", "limit": 5}],
            runtime_params=runtime_params,
        )
        is False
    )
    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "write_file", "path": "outputs/result.xlsx", "content": "placeholder"}],
            runtime_params=runtime_params,
        )
        is True
    )


def test_delivery_repair_payload_maps_source_artifact_fields_and_preserves_llm_fields(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_payload import (
        delivery_repair_payload,
    )

    tool_outputs = tmp_path / "memory_archive/artifacts/tool_outputs"
    tool_outputs.mkdir(parents=True)
    source_artifact = tool_outputs / "web_search-1.json"
    source_artifact.write_text(
        json.dumps(
            {
                "content": json.dumps(
                    {
                        "results": [
                            {
                                "title": "example/project",
                                "url": "https://example.test/project",
                                "snippet": "A source-backed project summary.",
                            }
                        ]
                    }
                )
            }
        ),
        encoding="utf-8",
    )
    (tool_outputs / "index.jsonl").write_text(
        json.dumps(
            {
                "tool": "web_search",
                "path": str(source_artifact),
                "call_id": "1-1",
                "scoped_call_id": "run-1:1-1",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _write_actions(
        tmp_path,
        [
            {
                "recommended_action": "materialize_checkpoint",
                "checkpoint_ref": "outputs/source_data.json",
                "checkpoint_materialization_mode": "source_evidence_first",
                "writer_tool": "api_json_collection",
                "write_tools": ["api_json_collection", "write_structured_json"],
                "required_columns": ["记录名", "来源地址", "中文说明", "说明依据"],
                "collection_contract": {
                    "source_json_ref": "outputs/source_data.json",
                    "required_item_fields": ["记录名", "来源地址", "中文说明", "说明依据"],
                    "required_item_evidence_fields": ["记录名", "来源地址"],
                    "llm_generated_fields": ["中文说明", "说明依据"],
                },
            }
        ],
    )

    payload = delivery_repair_payload(_agent(tmp_path))
    call = payload["required_tool_calls"][0]

    assert call["tool"] == "api_json_collection"
    assert call["fields"]["记录名"] == {"paths": ["full_name", "name", "title"]}
    assert call["fields"]["来源地址"] == {"paths": ["html_url", "url", "link", "uri"]}
    assert "中文说明" not in call["fields"]
    assert "说明依据" not in call["fields"]
    assert call["llm_generated_fields"] == ["中文说明", "说明依据"]
    assert call["evidence_fields"] == ["记录名", "来源地址"]
    assert call["source_artifacts"][0]["artifact_ref"] == str(source_artifact)


def test_delivery_repair_payload_api_collection_call_materializes_source_checkpoint(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_payload import (
        delivery_repair_payload,
    )
    from agent_py_agent.agent.tooling.api_json_collection import ApiJsonCollectionTool

    tool_outputs = tmp_path / "memory_archive/artifacts/tool_outputs"
    tool_outputs.mkdir(parents=True)
    source_artifact = tool_outputs / "web_search-1.json"
    source_artifact.write_text(
        json.dumps(
            {
                "content": json.dumps(
                    {
                        "results": [
                            {
                                "title": "example/project",
                                "url": "https://example.test/project",
                                "snippet": "A source-backed project summary.",
                            }
                        ]
                    }
                )
            }
        ),
        encoding="utf-8",
    )
    (tool_outputs / "index.jsonl").write_text(
        json.dumps(
            {
                "tool": "web_search",
                "path": str(source_artifact),
                "call_id": "1-1",
                "scoped_call_id": "run-1:1-1",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _write_actions(
        tmp_path,
        [
            {
                "recommended_action": "materialize_checkpoint",
                "checkpoint_ref": "outputs/source_data.json",
                "checkpoint_materialization_mode": "source_evidence_first",
                "writer_tool": "api_json_collection",
                "write_tools": ["api_json_collection", "write_structured_json"],
                "required_columns": ["记录名", "来源地址", "中文说明"],
                "collection_contract": {
                    "source_json_ref": "outputs/source_data.json",
                    "required_item_fields": ["记录名", "来源地址", "中文说明"],
                    "required_item_evidence_fields": ["记录名", "来源地址"],
                    "llm_generated_fields": ["中文说明"],
                },
            }
        ],
    )

    call = delivery_repair_payload(_agent(tmp_path))["required_tool_calls"][0]
    result = ApiJsonCollectionTool(tmp_path).execute(call)
    checkpoint = json.loads((tmp_path / "outputs/source_data.json").read_text(encoding="utf-8"))
    row = checkpoint["sheets"][0]["rows"][0]

    assert result.ok is True
    assert row["记录名"] == "example/project"
    assert row["来源地址"] == "https://example.test/project"
    assert row["中文说明"] == ""
    assert set(row["field_source_ids"]) == {"记录名", "来源地址"}


def _missing_markdown_action() -> dict[str, object]:
    return {
        "code": "STAGED_ARTIFACT_MISSING",
        "recommended_action": "materialize_checkpoint",
        "checkpoint_ref": "outputs/document_delivery/document_delivery_zh.md",
        "write_tools": ["write_file", "replace_in_file", "file_write_session"],
    }


def _source_checkpoint_api_collection_call() -> dict[str, object]:
    return {
        "tool": "api_json_collection",
        "path": "outputs/document_delivery/source_index.json",
        "source_artifacts": [{"artifact_ref": "/tmp/web_search.json", "item_path": "results", "source_id": "search-1"}],
        "fields": {"title": "title", "url": "url", "date": {"path": "date", "date_from_url": True}},
        "evidence_fields": ["title", "url", "date"],
    }


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
    assert required_call["path"] == "outputs/table_report/source_data.json"
    assert required_call["columns"] == ["记录名", "地址", "指标值", "中文说明", "说明依据"]
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
        "path": "outputs/document_delivery/source_index.json",
        "collection_item_updates": [{"item_index": 1, "field_path": "translated", "value": True}],
        "items_path": "rows",
        "merge_existing": True,
    }


# LLM: API collection repair calls should carry executable collection request hints from the contract.
# 函数用途: 验证返工上下文不会只给空字段骨架，而会传递结构化 request_ranges/url_template/fields。
def test_delivery_repair_context_renders_api_collection_request_hints(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        delivery_repair_context,
    )

    action = _api_collection_action()
    action["collection_contract"] = {**action["collection_contract"], "api_request": _api_collection_request_hints()}
    _write_actions(tmp_path, [action])

    payload = delivery_repair_context(_agent(tmp_path), repairs=0).splitlines()[1]
    required_call = json.loads(payload)["required_tool_calls"][0]

    assert required_call["tool"] == "api_json_collection"
    assert required_call["request_ranges"][0]["name_template"] == "2026-W{week}"
    assert required_call["request_ranges"][0]["reserved"]["window_start"] == "{start_date}"
    assert required_call["item_path"] == "items"
    assert required_call["limit_per_request"] == 10
    assert required_call["fields"]["记录名"] == "full_name"
    assert required_call["fields"]["中文说明"]["default_template"] == "{full_name}：{language}"
    assert required_call["evidence_fields"] == ["记录名", "地址", "指标值"]


def _api_collection_request_hints() -> dict[str, object]:
    return {
        "request_ranges": [_api_collection_range_hint()],
        "item_path": "items",
        "limit_per_request": 10,
        "fields": {
            "记录名": "full_name",
            "地址": "html_url",
            "指标值": "stargazers_count",
            "中文说明": {"path": "description", "default_template": "{full_name}：{language}"},
            "说明依据": {"template": "stars={stargazers_count}; topics={topics}"},
        },
        "evidence_fields": ["记录名", "地址", "指标值"],
    }


def _api_collection_range_hint() -> dict[str, object]:
    return {
        "start_date": "2026-01-01",
        "end_date": "2026-01-14",
        "step_days": 7,
        "name_template": "2026-W{week}",
        "reserved": {"metric_kind": "time_window_delta", "window_end": "{end_date}", "window_start": "{start_date}"},
        "source_id_template": "github-week-{week}",
        "url_template": "https://api.example.test/items?from={start}&to={end}",
    }


def _no_rows_action() -> dict[str, object]:
    return {
        "code": "STAGED_JSON_NO_ROWS",
        "recommended_action": "write_non_empty_structured_rows",
        "checkpoint_ref": "outputs/table_report/source_data.json",
        "required_columns": ["记录名", "地址"],
    }


def _evidence_action() -> dict[str, object]:
    return {
        "code": "EVIDENCE_REQUIRED_FIELD_MISSING",
        "recommended_action": "repair_evidence_refs",
        "checkpoint_ref": "outputs/table_report/source_data.json",
    }


def _api_collection_action() -> dict[str, object]:
    return {
        "code": "STAGED_JSON_REQUIRED_COLUMNS_MISSING",
        "recommended_action": "repair_structured_checkpoint_json",
        "checkpoint_ref": "outputs/table_report/source_data.json",
        "writer_tool": "api_json_collection",
        "write_tools": ["api_json_collection", "write_structured_json"],
        "required_columns": ["记录名", "地址", "指标值", "中文说明", "说明依据"],
        "collection_contract": {
            "source_json_ref": "outputs/table_report/source_data.json",
            "groups_path": "sheets",
            "items_path": "rows",
            "min_groups": 21,
            "min_items_per_group": 10,
            "required_item_fields": ["记录名", "地址", "指标值", "中文说明", "说明依据"],
        },
        "checkpoint_shape_hint": (
            '{"completion_evidence":{"scope":"year_to_date"},'
            '"sheets":[{"columns":["记录名","地址","指标值","中文说明","说明依据"],"rows":[]}]}'
        ),
    }


def _source_checkpoint_missing_action() -> dict[str, object]:
    return {
        "code": "STAGING_CHECKPOINT_MISSING",
        "recommended_action": "materialize_checkpoint",
        "checkpoint_ref": "outputs/document_delivery/source_index.json",
        "checkpoint_materialization_mode": "source_evidence_first",
        "writer_tool": "api_json_collection",
        "write_tools": ["api_json_collection", "write_structured_json"],
    }


def _empty_source_checkpoint_write_call() -> dict[str, object]:
    return {
        "tool": "write_structured_json",
        "path": "outputs/document_delivery/source_index.json",
        "data": {"completion_evidence": {"scope": "fixture"}, "rows": []},
    }


def _non_empty_source_checkpoint_write_call() -> dict[str, object]:
    return {
        "tool": "write_structured_json",
        "path": "outputs/document_delivery/source_index.json",
        "data": {"completion_evidence": {"scope": "fixture"}, "rows": [{"title": "Paper"}]},
    }


def _generated_rows_list_checkpoint_write_call() -> dict[str, object]:
    return {
        "tool": "write_structured_json",
        "path": "outputs/document_delivery/source_index.json",
        "generated_rows": [{"title": "Paper", "translated": True}],
        "completion_evidence": {"scope": "fixture"},
    }


def _collection_item_update_action() -> dict[str, object]:
    return {
        "code": "COLLECTION_ITEM_VALUE_MISMATCH",
        "recommended_action": "repair_collection_item_values",
        "checkpoint_ref": "outputs/document_delivery/source_index.json",
        "writer_tool": "write_structured_json",
        "items_path": "rows",
        "collection_item_updates": [{"item_index": 1, "field_path": "translated", "value": True}],
    }


def _write_file_call() -> dict[str, object]:
    return {"tool": "write_file", "path": "outputs/table_report/source_data.json", "content": "{}"}


def _structured_rows_call() -> dict[str, object]:
    return {
        "tool": "write_structured_json",
        "path": "outputs/table_report/source_data.json",
        "rows": [{"记录名": "demo"}],
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
        "checkpoint_ref": "outputs/table_report/source_data.json",
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


# LLM: mapping repair must reject artifact rewrites that do not contain required source keys.
# 函数用途: 验证 ARTIFACT_MAPPING_MISSING 后，写一个空壳 Markdown 不再算有效推进，必须包含结构化 missing_keys。
def test_delivery_repair_guard_rejects_mapping_repair_without_required_keys(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_actions(tmp_path, [_mapping_repair_action()])
    agent = _agent(tmp_path)

    assert is_delivery_repair_productive_call(agent, [_mapping_write_call("# 汇总\n\n待补充")]) is False
    assert is_delivery_repair_productive_call(
        agent,
        [_mapping_write_call("# 汇总\n\n- Paper A\n- Paper B\n- Paper C\n")],
    ) is True


# LLM: collection source repairs may gather evidence before rewriting the source checkpoint.
# 函数用途: 验证 source_index 占位符/缺字段返工时，web_search 这类证据采集不是空转，会进入后续 api_json_collection 写入桥接。
def test_delivery_repair_guard_allows_evidence_gathering_for_collection_source_repair(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_actions(tmp_path, [_collection_placeholder_repair_action()], strict_write_required=True)

    assert (
        is_delivery_repair_productive_call(
            _agent(tmp_path),
            [{"tool": "web_search", "query": "open source LLM papers after 2025", "limit": 10}],
        )
        is True
    )


# LLM: Once source artifacts are available, collection repair must use api_json_collection instead of more search.
# 函数用途: 验证来源证据已归档时，repair guard 会把模型拉回结构化采集工具，避免真实任务一直 web_search/read_file。
def test_delivery_repair_guard_rejects_more_search_when_api_collection_is_ready(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_web_search_tool_output_artifact(tmp_path)
    _write_actions(tmp_path, [_collection_placeholder_repair_action()], strict_write_required=True)
    agent = _agent(tmp_path)

    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "web_search", "query": "open source LLM papers after 2025", "limit": 10}],
        )
        is False
    )
    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "api_json_collection", "path": "outputs/document_delivery/source_index.json"}],
        )
        is True
    )


def test_delivery_repair_guard_rejects_more_search_when_api_collection_ready_with_evidence_repair(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_web_search_tool_output_artifact(tmp_path)
    _write_actions(
        tmp_path,
        [_collection_placeholder_repair_action(), _evidence_writer_action()],
        strict_write_required=True,
    )
    agent = _agent(tmp_path)

    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "web_search", "query": "open source LLM papers after 2025", "limit": 10}],
        )
        is False
    )
    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "api_json_collection", "path": "outputs/document_delivery/source_index.json"}],
        )
        is True
    )


def test_delivery_repair_guard_allows_api_collection_when_same_checkpoint_has_evidence_repair(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    collection_action = _collection_placeholder_repair_action()
    evidence_action = _evidence_writer_action()
    evidence_action["checkpoint_ref"] = collection_action["checkpoint_ref"]
    _write_web_search_tool_output_artifact(tmp_path)
    _write_actions(tmp_path, [collection_action, evidence_action], strict_write_required=True)

    assert (
        is_delivery_repair_productive_call(
            _agent(tmp_path),
            [{"tool": "api_json_collection", "path": "outputs/document_delivery/source_index.json"}],
        )
        is True
    )


# LLM: real task execution stores closeout under tools.workspace_root, not always agent.root.
# 函数用途: 验证工具循环根目录和代理根目录不一致时，delivery repair guard 仍读取真实工作区的 closeout。
def test_delivery_repair_guard_uses_tool_workspace_root_for_closeout(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    agent_root = tmp_path / "agent-root"
    workspace_root = tmp_path / "task-workspace"
    agent_root.mkdir()
    workspace_root.mkdir()
    _write_web_search_tool_output_artifact(workspace_root)
    _write_actions(workspace_root, [_collection_placeholder_repair_action()], strict_write_required=True)
    agent = _agent_with_workspace(agent_root, workspace_root)

    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "web_search", "query": "open source LLM papers after 2025", "limit": 10}],
        )
        is False
    )
    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "api_json_collection", "path": "outputs/document_delivery/source_index.json"}],
        )
        is True
    )


# LLM: moderately sized JSON repair targets should still include a bounded preview.
# 函数用途: 验证 12KB 以上但仍可控的 source checkpoint 会给模型预览，减少重复 read_file 空转。
def test_delivery_repair_context_previews_moderate_source_checkpoint(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        delivery_repair_context,
    )

    source = tmp_path / "outputs/document_delivery/source_index.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"rows": [{"title": f"Paper {index}", "date": "__FILL_3_date__"} for index in range(700)]}), encoding="utf-8")
    assert source.stat().st_size > 12_000
    _write_actions(tmp_path, [_collection_placeholder_repair_action()])

    payload = json.loads(delivery_repair_context(_agent(tmp_path), repairs=0).splitlines()[1])
    snapshots = payload["repair_target_snapshots"]

    assert snapshots[0]["path"] == "outputs/document_delivery/source_index.json"
    assert "preview" in snapshots[0]
    assert len(snapshots[0]["preview"]) <= 2400


def test_delivery_repair_context_maps_common_source_date_without_placeholder(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        delivery_repair_context,
    )

    _write_source_checkpoint_with_constant_field(tmp_path)
    _write_web_search_tool_output_artifact(tmp_path)
    _write_actions(tmp_path, [_collection_placeholder_repair_action()])

    payload = delivery_repair_context(_agent(tmp_path), repairs=0).splitlines()[1]
    required_call = json.loads(payload)["required_tool_calls"][0]

    assert required_call["tool"] == "api_json_collection"
    assert "__FILL" not in json.dumps(required_call, ensure_ascii=False)
    assert required_call["fields"]["date"] == {
        "date_from_url": True,
        "paths": ["date", "published", "published_at", "updated"],
    }
    assert required_call["fields"]["translated"] == {"value": True}


def _collection_placeholder_repair_action() -> dict[str, object]:
    return {
        "checkpoint_ref": "outputs/document_delivery/source_index.json",
        "code": "COLLECTION_ITEM_PLACEHOLDER_VALUE",
        "collection_contract": {
            "required_item_fields": ["title", "url", "date", "translated"],
            "source_json_ref": "outputs/document_delivery/source_index.json",
        },
        "recommended_action": "repair_structured_checkpoint_json",
        "required_columns": ["title", "url", "date", "translated"],
        "write_tools": ["api_json_collection", "write_structured_json"],
        "writer_tool": "api_json_collection",
    }


def _mapping_repair_action() -> dict[str, object]:
    return {
        "recommended_action": "repair_artifact_against_findings",
        "artifact_path": "outputs/document_delivery/document_delivery_zh.md",
        "finding_codes": ["ARTIFACT_MAPPING_MISSING"],
        "finding_values": [
            json.dumps(
                {
                    "mapped": 0,
                    "required": 3,
                    "missing_keys": [{"title": "Paper A"}, {"title": "Paper B"}, {"title": "Paper C"}],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        ],
        "write_tools": ["write_file", "replace_in_file"],
    }


def _mapping_write_call(content: str) -> dict[str, object]:
    return {
        "tool": "write_file",
        "path": "outputs/document_delivery/document_delivery_zh.md",
        "content": content,
    }


def _write_source_checkpoint_with_constant_field(root: Path) -> None:
    path = root / "outputs/document_delivery/source_index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "rows": [
                    {"title": "Paper", "url": "https://arxiv.org/abs/2501.12948", "date": "__FILL_date__", "translated": True}
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_web_search_tool_output_artifact(root: Path) -> None:
    artifact_dir = root / "memory_archive/artifacts/tool_outputs"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / "web_search-1.json"
    artifact_path.write_text(
        json.dumps(
            {
                "content": json.dumps(
                    {
                        "results": [
                            {
                                "snippet": "arXiv paper page",
                                "title": "Paper",
                                "url": "https://arxiv.org/abs/2501.12948",
                            }
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


def _evidence_writer_action() -> dict[str, object]:
    return {
        "code": "EVIDENCE_REQUIRED_FIELD_MISSING",
        "recommended_action": "repair_evidence_refs",
        "checkpoint_ref": "outputs/table_report/source_data.json",
        "writer_tool": "write_structured_json",
        "required_fields": ["地址"],
    }


def _evidence_write_call(*, merge_existing: bool) -> dict[str, object]:
    return {
        "tool": "write_structured_json",
        "path": "outputs/table_report/source_data.json",
        "merge_existing": merge_existing,
        "data": {
            "source_refs": [{"source_id": "src-1", "uri": "https://example.com"}],
            "claims": [{"field": "地址", "source_ids": ["src-1"], "value": "https://example.com"}],
        },
    }
