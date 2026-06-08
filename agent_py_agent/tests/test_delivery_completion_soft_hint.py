from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.delivery_completion_soft_hint import (
    maybe_append_delivery_completion_soft_hint,
)


def _params(
    *,
    contract: dict | None = None,
    task_attributes: dict | None = None,
    user_prompt: str = "请完成任务。",
) -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt=user_prompt,
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


def test_delivery_completion_hint_ignores_read_only_non_target_tool(tmp_path):
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "report.md").write_text("完成内容", encoding="utf-8")
    (tmp_path / "notes.md").write_text("过程内容", encoding="utf-8")
    params = _params()

    maybe_append_delivery_completion_soft_hint(
        SimpleNamespace(root=str(tmp_path)),
        params,
        {"tool": "read_file", "parameters": {"path": str(tmp_path / "notes.md")}},
        tool_ok=True,
    )

    assert params.tool_context == []


def test_delivery_completion_hint_after_reading_declared_artifact(tmp_path):
    report = tmp_path / "output" / "report.md"
    report.parent.mkdir()
    report.write_text("完成内容", encoding="utf-8")
    params = _params()

    maybe_append_delivery_completion_soft_hint(
        SimpleNamespace(root=str(tmp_path)),
        params,
        {"tool": "read_file", "parameters": {"path": str(report)}},
        tool_ok=True,
    )

    assert len(params.tool_context) == 1
    assert "[delivery-completion-soft-hint]" in params.tool_context[0]
    assert "submit_for_acceptance" in params.tool_context[0]


def test_delivery_completion_hint_waits_for_required_target_coverage(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("abcdef", encoding="utf-8")
    report = tmp_path / "output" / "report.md"
    report.parent.mkdir()
    report.write_text("初稿", encoding="utf-8")
    write_record = {"tool": "write_file", "ok": True, "path": str(report)}
    params = _params(contract=_contract_with_required_source_coverage(source))
    params.archive_tool_calls.append(write_record)

    maybe_append_delivery_completion_soft_hint(
        SimpleNamespace(root=str(tmp_path)),
        params,
        write_record,
        tool_ok=True,
    )

    assert len(params.tool_context) == 1
    assert "[delivery-coverage-check]" in params.tool_context[0]
    assert "target_coverage_status" in params.tool_context[0]
    assert "cover_missing_targets_before_submit" in params.tool_context[0]


def test_delivery_completion_hint_after_required_target_coverage_complete(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("abcdef", encoding="utf-8")
    report = tmp_path / "output" / "report.md"
    report.parent.mkdir()
    report.write_text("终稿", encoding="utf-8")
    read_record = _read_window_record(source, offset=0, next_offset=6, total=6)
    params = _params(contract=_contract_with_required_source_coverage(source))
    params.archive_tool_calls.extend([{"tool": "write_file", "ok": True, "path": str(report)}, read_record])
    params.tool_context.append("[delivery-coverage-check]\n{\"stale\": true}")

    maybe_append_delivery_completion_soft_hint(
        SimpleNamespace(root=str(tmp_path)),
        params,
        read_record,
        tool_ok=True,
    )

    assert len(params.tool_context) == 1
    assert "[delivery-completion-soft-hint]" in params.tool_context[0]
    assert "[delivery-coverage-check]" not in params.tool_context[0]
    assert str(report) in params.tool_context[0]


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


def test_delivery_completion_hint_for_explicit_user_requested_output_path_without_contract(tmp_path):
    output_dir = tmp_path / "external-output"
    report = output_dir / "all-agent-最终验收报告.md"
    report.parent.mkdir(parents=True)
    report.write_text("完成内容", encoding="utf-8")
    params = _params(
        contract={},
        user_prompt=f"请把最终报告写到 {report}，写完就提交验收。",
        task_attributes={
            "run_workspace": {
                "output_dir": str(tmp_path / "task" / "output"),
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


def test_delivery_completion_hint_for_explicit_file_does_not_accept_sibling_report(tmp_path):
    output_dir = tmp_path / "external-output"
    requested = output_dir / "requested-final.md"
    sibling = output_dir / "other-final.md"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("完成内容", encoding="utf-8")
    params = _params(
        contract={},
        user_prompt=f"请把最终报告写到 {requested}。",
        task_attributes={
            "run_workspace": {
                "output_dir": str(tmp_path / "task" / "output"),
                "work_dir": str(tmp_path / "task" / "work"),
                "task_root": str(tmp_path / "task"),
            }
        },
    )

    maybe_append_delivery_completion_soft_hint(
        SimpleNamespace(root=str(tmp_path)),
        params,
        {"tool": "write_file", "path": str(sibling)},
        tool_ok=True,
    )

    assert params.tool_context == []


def _contract_with_required_source_coverage(source: object) -> dict[str, object]:
    return {
        "artifacts": [
            {
                "artifact_id": "report",
                "required": True,
                "preferred_path": "output/report.md",
            }
        ],
        "target_coverage_contract": {
            "enforcement": "required",
            "coverage_requirement": "full_source_read",
            "target_items": [
                {
                    "target_id": str(source),
                    "source_path": str(source),
                    "coverage_kind": "full_source_read",
                    "enforcement": "required",
                }
            ],
        },
    }


def _read_window_record(source: object, *, offset: int, next_offset: int, total: int) -> dict[str, object]:
    return {
        "tool": "read_file",
        "ok": True,
        "parameters": {"path": str(source)},
        "read_window": {
            "kind": "char_window",
            "offset": offset,
            "next_offset": next_offset,
            "total_chars": total,
        },
    }


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
