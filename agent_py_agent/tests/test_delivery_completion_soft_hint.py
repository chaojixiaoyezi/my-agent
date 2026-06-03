from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.delivery_completion_soft_hint import (
    maybe_append_delivery_completion_soft_hint,
)


def _params(*, contract: dict | None = None, task_attributes: dict | None = None) -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt="请完成任务。",
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
        task_attributes=task_attributes or {},
        request_id="request-1",
        run_id="run-1",
        task_id="task-1",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        delivery_contract=contract
        if contract is not None
        else {
            "artifacts": [
                {
                    "artifact_id": "report",
                    "required": True,
                    "preferred_path": "output/report.md",
                }
            ]
        },
    )


def test_delivery_completion_hint_after_all_declared_artifacts_exist(tmp_path):
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "report.md").write_text("完成内容", encoding="utf-8")
    params = _params()

    maybe_append_delivery_completion_soft_hint(
        SimpleNamespace(root=str(tmp_path)),
        params,
        {"tool": "write_file"},
        tool_ok=True,
    )

    assert len(params.tool_context) == 1
    assert "[delivery-completion-soft-hint]" in params.tool_context[0]
    assert "submit_for_acceptance" in params.tool_context[0]


def test_delivery_completion_hint_waits_for_missing_declared_artifacts(tmp_path):
    params = _params()

    maybe_append_delivery_completion_soft_hint(
        SimpleNamespace(root=str(tmp_path)),
        params,
        {"tool": "write_file"},
        tool_ok=True,
    )

    assert params.tool_context == []


def test_delivery_completion_hint_ignores_failed_mutating_tool(tmp_path):
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "report.md").write_text("旧内容", encoding="utf-8")
    params = _params()

    maybe_append_delivery_completion_soft_hint(
        SimpleNamespace(root=str(tmp_path)),
        params,
        {"tool": "write_file"},
        tool_ok=False,
    )

    assert params.tool_context == []


def test_delivery_completion_hint_ignores_read_only_tool(tmp_path):
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "report.md").write_text("完成内容", encoding="utf-8")
    params = _params()

    maybe_append_delivery_completion_soft_hint(
        SimpleNamespace(root=str(tmp_path)),
        params,
        {"tool": "read_file"},
        tool_ok=True,
    )

    assert params.tool_context == []


def test_delivery_completion_hint_is_one_shot(tmp_path):
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "report.md").write_text("完成内容", encoding="utf-8")
    params = _params()

    for _ in range(2):
        maybe_append_delivery_completion_soft_hint(
            SimpleNamespace(root=str(tmp_path)),
            params,
            {"tool": "write_file"},
            tool_ok=True,
        )

    assert len(params.tool_context) == 1


def test_delivery_completion_hint_for_task_output_report_without_contract(tmp_path):
    output_dir = tmp_path / "task" / "output"
    report = output_dir / "all_agent_architecture_report.md"
    report.parent.mkdir(parents=True)
    report.write_text("完成内容", encoding="utf-8")
    params = _params(
        contract={},
        task_attributes={
            "run_workspace": {
                "output_dir": str(output_dir),
                "work_dir": str(tmp_path / "task" / "work"),
                "task_root": str(tmp_path / "task"),
            }
        },
    )

    maybe_append_delivery_completion_soft_hint(
        SimpleNamespace(root=str(tmp_path)),
        params,
        {"tool": "write_file", "path": str(report)},
        tool_ok=True,
    )

    assert len(params.tool_context) == 1
    assert "[delivery-completion-soft-hint]" in params.tool_context[0]
    assert str(report) in params.tool_context[0]


def test_delivery_completion_hint_without_contract_ignores_non_output_scratch_file(tmp_path):
    output_dir = tmp_path / "task" / "output"
    scratch = tmp_path / "task" / "work" / "notes.md"
    scratch.parent.mkdir(parents=True)
    scratch.write_text("过程笔记", encoding="utf-8")
    params = _params(
        contract={},
        task_attributes={
            "run_workspace": {
                "output_dir": str(output_dir),
                "work_dir": str(tmp_path / "task" / "work"),
                "task_root": str(tmp_path / "task"),
            }
        },
    )

    maybe_append_delivery_completion_soft_hint(
        SimpleNamespace(root=str(tmp_path)),
        params,
        {"tool": "write_file", "path": str(scratch)},
        tool_ok=True,
    )

    assert params.tool_context == []
