from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace


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


# LLM: _write_closeout keeps the delivery-repair fixture tiny and grounded in the same machine report the runtime uses.
# 函数用途: 向测试工作区写入 .agent_delivery/closeout.json，供 repair guard 直接读取。
def _write_closeout(root: Path, payload: dict[str, object]) -> None:
    path = root / ".agent_delivery" / "closeout.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
