from __future__ import annotations

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


def _missing_checkpoint_action() -> dict[str, object]:
    return {
        "code": "STAGING_CHECKPOINT_MISSING",
        "recommended_action": "materialize_checkpoint",
        "checkpoint_ref": "outputs/deepseek_papers/source_index.json",
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
