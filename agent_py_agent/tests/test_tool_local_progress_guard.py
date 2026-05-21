from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace


# LLM: repeated remote-style exploration with unchanged closeout fingerprints should activate the local-progress guard.
# 函数用途: 验证连续两轮只读 artifact 且本地进展指纹没变时，会触发“回到本地写入/构建”的通用守门。
def test_local_progress_guard_redirects_after_repeated_exploration_without_local_progress(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_local_progress_guard import (
        has_required_local_progress_guard,
    )

    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "failure_fingerprint": "same-failure",
                "work_progress_fingerprint": "same-progress",
                "recovery_actions": [
                    {
                        "code": "STAGED_JSON_NO_ROWS",
                        "recommended_action": "write_non_empty_structured_rows",
                        "checkpoint_ref": "outputs/report/source.json",
                    }
                ],
                "pending_materialization_targets": [],
            },
        },
    )
    params = _params()
    agent = SimpleNamespace(root=tmp_path)
    exploratory_calls = [{"tool": "read_artifact", "artifact_ref": "memory_archive/artifacts/tool_outputs/demo.json"}]

    assert has_required_local_progress_guard(agent, params, exploratory_calls) is False
    assert has_required_local_progress_guard(agent, params, exploratory_calls) is True


# LLM: closeout no-progress threshold is the machine contract for how many exploration turns are allowed.
# 函数用途: 验证 local-progress guard 使用 closeout 里的结构化阈值，而不是硬编码拦截次数。
def test_local_progress_guard_uses_closeout_no_progress_threshold(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_local_progress_guard import (
        has_required_local_progress_guard,
    )

    payload = _closeout_payload(work_progress_fingerprint="same-progress")
    payload["delivery_progress"]["no_progress_block_threshold"] = 5
    _write_closeout(tmp_path, payload)
    params = _params()
    agent = SimpleNamespace(root=tmp_path)
    exploratory_calls = [{"tool": "fetch_url", "url": "https://example.test/data.json"}]

    for _ in range(4):
        assert has_required_local_progress_guard(agent, params, exploratory_calls) is False
    assert has_required_local_progress_guard(agent, params, exploratory_calls) is True


# LLM: a changed work-progress fingerprint should reset the guard budget instead of carrying old exploration debt forever.
# 函数用途: 验证只要 closeout 报告里的本地进展指纹变化了，local-progress guard 会重置计数，避免误伤后续合理探索。
def test_local_progress_guard_resets_when_work_progress_fingerprint_changes(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_local_progress_guard import (
        has_required_local_progress_guard,
    )

    params = _params()
    agent = SimpleNamespace(root=tmp_path)
    exploratory_calls = [{"tool": "read_artifact", "artifact_ref": "memory_archive/artifacts/tool_outputs/demo.json"}]

    _write_closeout(tmp_path, _closeout_payload(work_progress_fingerprint="progress-a"))
    assert has_required_local_progress_guard(agent, params, exploratory_calls) is False

    _write_closeout(tmp_path, _closeout_payload(work_progress_fingerprint="progress-b"))
    assert has_required_local_progress_guard(agent, params, exploratory_calls) is False


# LLM: local write/build actions should not be treated as remote exploration debt.
# 函数用途: 验证 builder 或写文件这类本地推进动作不会触发 local-progress guard。
def test_local_progress_guard_allows_local_progressive_calls(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_local_progress_guard import (
        has_required_local_progress_guard,
    )

    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "failure_fingerprint": "same-failure",
                "work_progress_fingerprint": "same-progress",
                "recovery_actions": [
                    {
                        "code": "STAGING_BUILDER_READY",
                        "recommended_action": "invoke_builder_tool",
                        "builder_tool": "data_to_workbook",
                        "source_ref": "outputs/report/source.json",
                        "output_ref": "outputs/report/report.xlsx",
                    }
                ],
                "pending_materialization_targets": [],
            },
        },
    )
    params = _params()
    agent = SimpleNamespace(root=tmp_path)

    assert (
        has_required_local_progress_guard(
            agent,
            params,
            [{"tool": "data_to_workbook", "source_json_path": "outputs/report/source.json", "path": "outputs/report/report.xlsx"}],
        )
        is False
    )


# LLM: writer_tool and document builder actions should reset local-progress debt through structured tool names.
# 函数用途: 验证 write_structured_json/markdown_to_pdf 这类通用构建工具不会被误判成空转。
def test_local_progress_guard_allows_writer_and_document_builder_tools(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_local_progress_guard import (
        has_required_local_progress_guard,
    )

    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "failure_fingerprint": "same-failure",
                "work_progress_fingerprint": "same-progress",
                "recovery_actions": [
                    {
                        "code": "STAGED_JSON_NO_ROWS",
                        "recommended_action": "write_non_empty_structured_rows",
                        "writer_tool": "write_structured_json",
                    },
                    {
                        "code": "STAGING_BUILDER_READY",
                        "recommended_action": "invoke_builder_tool",
                        "builder_tool": "markdown_to_pdf",
                    },
                ],
                "pending_materialization_targets": [],
            },
        },
    )
    params = _params()
    agent = SimpleNamespace(root=tmp_path)

    assert has_required_local_progress_guard(agent, params, [{"tool": "write_structured_json", "rows": [{"a": 1}]}]) is False
    assert has_required_local_progress_guard(agent, params, [{"tool": "markdown_to_pdf", "path": "out.pdf"}]) is False


def _params():
    from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams

    return ToolLoopExecuteParams(
        user_prompt="test",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        granted_capabilities=None,
        write_boundary=None,
        task_attributes={},
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        delivery_contract={},
    )


def _write_closeout(root: Path, payload: dict[str, object]) -> None:
    path = root / ".agent_delivery" / "closeout.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# LLM: _closeout_payload returns a minimal structured closeout report for local-progress guard tests.
# 函数用途: 生成带 failure/work 指纹和 write-first recovery action 的机器报告。
def _closeout_payload(*, work_progress_fingerprint: str) -> dict[str, object]:
    return {
        "ok": False,
        "delivery_progress": {
            "failure_fingerprint": "same-failure",
            "work_progress_fingerprint": work_progress_fingerprint,
            "recovery_actions": [
                {
                    "code": "STAGED_JSON_NO_ROWS",
                    "recommended_action": "write_non_empty_structured_rows",
                    "checkpoint_ref": "outputs/report/source.json",
                }
            ],
            "pending_materialization_targets": [],
        },
    }
