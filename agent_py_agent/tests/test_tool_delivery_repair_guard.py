from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.tests.tool_delivery_repair_fixtures import (
    _evidence_repair_closeout,
    _sheet_only_write_call,
    _structure_and_evidence_repair_closeout,
    _structured_evidence_write_call,
    _write_closeout,
)


# LLM: builder-ready delivery repair should treat read_artifact as inspection-only so the loop pivots to the builder tool.
# 函数用途: 验证进入 invoke_builder_tool 阶段后，单独 read_artifact 不再算推进动作。
def test_delivery_repair_guard_treats_read_artifact_as_nonproductive_when_builder_ready(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "recovery_actions": [
                    {
                        "code": "STAGING_BUILDER_READY",
                        "recommended_action": "invoke_builder_tool",
                        "builder_tool": "data_to_workbook",
                        "source_ref": "outputs/report/source.json",
                        "output_ref": "outputs/report/report.xlsx",
                    }
                ]
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path)

    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "read_artifact", "artifact_ref": "memory_archive/artifacts/tool_outputs/demo.json"}],
        )
        is False
    )


# LLM: missing-checkpoint repair should still allow read_artifact because the model may need fetched evidence before materializing the file.
# 函数用途: 验证 materialize_checkpoint 阶段不会把 read_artifact 误判为空转，避免过早拦住资料整理动作。
def test_delivery_repair_guard_allows_read_artifact_before_checkpoint_exists(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "recovery_actions": [
                    {
                        "code": "STAGING_CHECKPOINT_MISSING",
                        "recommended_action": "materialize_checkpoint",
                        "checkpoint_ref": "outputs/deepseek_papers/source_index.json",
                    }
                ]
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path)

    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "read_artifact", "artifact_ref": "memory_archive/artifacts/tool_outputs/demo.json"}],
        )
        is True
    )


def test_delivery_repair_guard_requires_write_action_after_repeated_no_progress(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "unchanged_failure_count": 9,
                "no_progress_block_threshold": 5,
                "recovery_actions": [
                    {
                        "code": "STAGED_JSON_NO_ROWS",
                        "recommended_action": "write_non_empty_structured_rows",
                        "checkpoint_ref": "outputs/github_star_growth/source_data.json",
                        "checkpoint_shape_hint": '{"sheets":[{"rows":[{"项目名":"..."}]}]}',
                        "required_columns": ["项目名", "地址"],
                    },
                    {
                        "code": "EVIDENCE_REQUIRED_FIELD_MISSING",
                        "recommended_action": "repair_evidence_refs",
                        "checkpoint_ref": "outputs/github_star_growth/source_data.json",
                    },
                ],
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path)

    assert is_delivery_repair_productive_call(agent, [{"tool": "run_command", "command": "curl https://example.com"}]) is False
    assert is_delivery_repair_productive_call(agent, [{"tool": "fetch_url", "url": "https://example.com"}]) is False
    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "write_file", "path": "outputs/github_star_growth/source_data.json", "content": "{}"}],
        )
        is True
    )
    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "write_structured_json", "path": "outputs/github_star_growth/source_data.json", "rows": [{"项目名": "demo"}]}],
        )
        is True
    )


# LLM: writer_tool is an executable machine contract for structured checkpoints, not just a hint.
# 函数用途: 验证已指定 write_structured_json 的阶段修复不能用 write_file 绕过结构化校验。
def test_delivery_repair_guard_requires_declared_writer_tool_for_structured_checkpoint(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "recovery_actions": [
                    {
                        "code": "STAGED_JSON_TOO_FEW_SHEETS",
                        "recommended_action": "repair_structured_checkpoint_json",
                        "checkpoint_ref": "outputs/github_star_growth/source_data.json",
                        "writer_tool": "write_structured_json",
                    }
                ]
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path)

    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "write_file", "path": "outputs/github_star_growth/source_data.json", "content": "{}"}],
        )
        is False
    )
    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "write_structured_json", "path": "outputs/github_star_growth/source_data.json", "rows": [{"项目名": "demo"}]}],
        )
        is True
    )


# LLM: empty structured writer calls must not satisfy a non-empty rows repair action.
# 函数用途: 验证 STAGED_JSON_NO_ROWS 这类修复必须真正提交非空 rows/sheets，而不能用空骨架骗过修复守门。
def test_delivery_repair_guard_rejects_empty_structured_rows_for_no_rows_action(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "recovery_actions": [
                    {
                        "code": "STAGED_JSON_NO_ROWS",
                        "recommended_action": "write_non_empty_structured_rows",
                        "checkpoint_ref": "outputs/report/source_data.json",
                        "writer_tool": "write_structured_json",
                    }
                ],
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path)
    empty_sheet_call = {
        "tool": "write_structured_json",
        "path": "outputs/report/source_data.json",
        "sheets": [{"name": "W01", "rows": []}],
    }
    non_empty_sheet_call = {
        "tool": "write_structured_json",
        "path": "outputs/report/source_data.json",
        "merge_existing": True,
        "sheets": [{"name": "W01", "rows": [{"项目名": "demo"}]}],
    }

    assert is_delivery_repair_productive_call(agent, [empty_sheet_call]) is False
    assert is_delivery_repair_productive_call(agent, [non_empty_sheet_call]) is True


# LLM: evidence repairs require source_refs/claims machine fields, not arbitrary table rows.
# 函数用途: 验证缺证据恢复动作不会把普通 rows/sheets 写入误判成完成了证据修复。
def test_delivery_repair_guard_requires_evidence_shape_for_evidence_repair(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_closeout(tmp_path, _evidence_repair_closeout())
    agent = SimpleNamespace(root=tmp_path)

    assert is_delivery_repair_productive_call(agent, [_sheet_only_write_call()]) is False
    assert is_delivery_repair_productive_call(agent, [_structured_evidence_write_call()]) is True


# LLM: Structure+evidence repair must happen as one machine checkpoint update.
# 函数用途: 验证同一 checkpoint 同时缺表格结构和证据时，不能只写 rows/sheets 而漏掉 source_refs/claims。
def test_delivery_repair_guard_requires_evidence_shape_for_combined_checkpoint_repair(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_closeout(tmp_path, _structure_and_evidence_repair_closeout())
    agent = SimpleNamespace(root=tmp_path)

    assert is_delivery_repair_productive_call(agent, [_sheet_only_write_call()]) is False
    assert is_delivery_repair_productive_call(agent, [_structured_evidence_write_call()]) is True


def test_delivery_repair_guard_allows_evidence_gathering_before_strict_write_mode(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "unchanged_failure_count": 1,
                "no_progress_block_threshold": 5,
                "recovery_actions": [
                    {
                        "code": "STAGED_JSON_NO_ROWS",
                        "recommended_action": "write_non_empty_structured_rows",
                        "checkpoint_ref": "outputs/report/source_data.json",
                    }
                ],
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path)

    assert is_delivery_repair_productive_call(agent, [{"tool": "fetch_url", "url": "https://example.com/data"}]) is True
    assert is_delivery_repair_productive_call(agent, [{"tool": "search", "query": "release notes"}]) is True
    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "read_artifact", "artifact_ref": "memory_archive/artifacts/tool_outputs/fetch-1.json"}],
        )
        is True
    )
    assert is_delivery_repair_productive_call(agent, [{"tool": "list_files", "path": "outputs/report"}]) is False


def test_delivery_repair_context_includes_checkpoint_shape_hint(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        delivery_repair_context,
    )

    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "recovery_actions": [
                    {
                        "code": "STAGED_JSON_REQUIRED_COLUMNS_MISSING",
                        "recommended_action": "write_non_empty_structured_rows",
                        "checkpoint_ref": "outputs/report/source_data.json",
                        "checkpoint_shape_hint": '{"sheets":[{"name":"榜单","rows":[{"项目名":"..."}]}]}',
                        "required_columns": ["项目名", "地址"],
                        "missing_columns": "项目名,地址",
                        "writer_tool": "write_structured_json",
                    }
                ],
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path)

    context = delivery_repair_context(agent, repairs=0)

    assert "checkpoint_shape_hint" in context
    assert "required_columns" in context
    assert "missing_columns" in context
    assert "required_tool_calls" in context
    assert '"tool": "write_structured_json"' in context
    assert '"path": "outputs/report/source_data.json"' in context


# LLM: rejected repair calls should come back as machine-readable feedback, not disappear silently.
# 函数用途: 验证阶段修复时被拦截的检查类调用会生成结构化拒绝上下文，下一轮可直接看到 required_tool_calls。
def test_delivery_repair_rejection_context_names_rejected_and_required_calls(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        delivery_repair_rejection_context,
    )

    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "recovery_actions": [
                    {
                        "code": "STAGING_CHECKPOINT_MISSING",
                        "recommended_action": "materialize_checkpoint",
                        "checkpoint_ref": "outputs/report/source_data.json",
                        "checkpoint_shape_hint": '{"sheets":[{"name":"榜单","rows":[{"项目名":"..."}]}]}',
                        "writer_tool": "write_structured_json",
                    }
                ],
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path, backend=SimpleNamespace(name="fake"))

    context = delivery_repair_rejection_context(
        agent,
        [{"tool": "list_files", "path": "outputs/report"}],
        repairs=0,
    )
    payload = json.loads(context.splitlines()[1])

    assert "delivery-required-repair-rejected" in context
    assert payload["rejected_tool_calls"] == [{"path": "outputs/report", "tool": "list_files"}]
    assert payload["required_tool_calls"][0]["tool"] == "write_structured_json"
    assert payload["required_tool_calls"][0]["path"] == "outputs/report/source_data.json"
    assert payload["required_tool_calls"][0]["merge_existing"] is True


# LLM: structured checkpoint repair may inspect the checkpoint it is about to rewrite.
# 函数用途: 验证严格修复模式下只放行 checkpoint_ref 本身的 read_file，不放行普通 artifact 检查。
def test_delivery_repair_guard_allows_checkpoint_read_for_structured_repair(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "unchanged_failure_count": 5,
                "no_progress_block_threshold": 5,
                "recovery_actions": [
                    {
                        "code": "STAGED_JSON_TOO_FEW_SHEETS",
                        "recommended_action": "repair_structured_checkpoint_json",
                        "checkpoint_ref": "outputs/report/source_data.json",
                        "writer_tool": "write_structured_json",
                    }
                ],
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path)

    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "read_file", "path": "outputs/report/source_data.json"}],
        )
        is True
    )
    assert is_delivery_repair_productive_call(agent, [{"tool": "read_file", "path": "outputs/site/index.html"}]) is False


# LLM: evidence repair may gather fresh sources before writing claims.
# 函数用途: 验证 strict repair 不会拦住为 source_refs/claims 收集证据的结构化抓取工具。
def test_delivery_repair_guard_allows_evidence_gathering_for_evidence_repair(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "unchanged_failure_count": 5,
                "no_progress_block_threshold": 5,
                "recovery_actions": [
                    {
                        "code": "EVIDENCE_REQUIRED_FIELD_MISSING",
                        "recommended_action": "repair_evidence_refs",
                        "checkpoint_ref": "outputs/report/source_data.json",
                        "required_fields": ["项目名"],
                        "writer_tool": "write_structured_json",
                    }
                ],
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path)

    assert is_delivery_repair_productive_call(agent, [{"tool": "fetch_url", "url": "https://example.com"}]) is True
