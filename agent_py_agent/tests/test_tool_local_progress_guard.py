from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace


def test_local_progress_guard_warns_at_fixed_interval_without_blocking(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_guard.local_progress import (
        has_required_local_progress_guard,
        local_progress_guard_context,
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
    exploratory_calls = [{"tool": "read_artifact", "artifact_ref": "blobs/tool_outputs/demo.json"}]

    _assert_guard_false_for_rounds(has_required_local_progress_guard, (agent, params, exploratory_calls), 9)
    assert has_required_local_progress_guard(agent, params, exploratory_calls) is True
    context = local_progress_guard_context(agent, redirects=0)
    assert "第 10 轮本地进展固定提醒" in context
    assert "local_progress_hint_interval" in context

    _assert_guard_false_for_rounds(has_required_local_progress_guard, (agent, params, exploratory_calls), 9)
    assert has_required_local_progress_guard(agent, params, exploratory_calls) is True
    context = local_progress_guard_context(agent, redirects=0)
    assert "第 20 轮本地进展固定提醒" in context

    _assert_guard_false_for_rounds(has_required_local_progress_guard, (agent, params, exploratory_calls), 9)
    assert has_required_local_progress_guard(agent, params, exploratory_calls) is True
    assert "第 30 轮本地进展固定提醒" in local_progress_guard_context(agent, redirects=99)


def _assert_guard_false_for_rounds(func, args: tuple[object, object, object], rounds: int) -> None:
    for _ in range(rounds):
        assert func(*args) is False

def test_local_progress_guard_unlimited_hint_interval_is_configurable(tmp_path: Path):
    from agent_py_agent.agent.agent_core.exploration_fuse_config import ExplorationFuseConfig
    from agent_py_agent.agent.agent_core.tool_guard.local_progress import (
        has_required_local_progress_guard,
        local_progress_guard_context,
    )

    _write_closeout(tmp_path, _closeout_payload(work_progress_fingerprint="same-progress"))
    params = _params()
    agent = SimpleNamespace(
        root=tmp_path,
        _exploration_fuse_config=ExplorationFuseConfig(
            local_progress_unlimited_hint_interval=7,
        ),
    )
    exploratory_calls = [{"tool": "web_fetch", "url": "https://example.test/data.json"}]

    for _ in range(6):
        assert has_required_local_progress_guard(agent, params, exploratory_calls) is False
    assert has_required_local_progress_guard(agent, params, exploratory_calls) is True
    assert "第 7 轮本地进展固定提醒" in local_progress_guard_context(agent, redirects=99)


def test_local_progress_guard_resets_when_work_progress_fingerprint_changes(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_guard.local_progress import (
        has_required_local_progress_guard,
    )

    params = _params()
    agent = SimpleNamespace(root=tmp_path)
    exploratory_calls = [{"tool": "read_artifact", "artifact_ref": "blobs/tool_outputs/demo.json"}]

    _write_closeout(tmp_path, _closeout_payload(work_progress_fingerprint="progress-a"))
    assert has_required_local_progress_guard(agent, params, exploratory_calls) is False

    _write_closeout(tmp_path, _closeout_payload(work_progress_fingerprint="progress-b"))
    assert has_required_local_progress_guard(agent, params, exploratory_calls) is False


def test_local_progress_guard_state_prefers_current_task_work_dir(tmp_path: Path):
    from dataclasses import replace

    from agent_py_agent.agent.agent_core.tool_guard.local_progress import (
        has_required_local_progress_guard,
    )

    workspace_root = tmp_path / "source-workspace"
    task_root = tmp_path / "tasks" / "2026-06-07" / "delivery"
    work_dir = task_root / "work"
    _write_closeout(task_root, _closeout_payload(work_progress_fingerprint="same-progress"))
    params = replace(
        _params(),
        task_attributes={
            "run_workspace": {
                "task_root": str(task_root),
                "output_dir": str(task_root / "output"),
                "work_dir": str(work_dir),
            }
        },
    )
    agent = SimpleNamespace(root=workspace_root)

    assert has_required_local_progress_guard(agent, params, [{"tool": "read_file", "path": "input.txt"}]) is False

    assert (work_dir / ".agent_delivery" / "local_progress_guard.json").exists()
    assert not (workspace_root / ".agent_delivery" / "local_progress_guard.json").exists()


def test_local_progress_guard_allows_local_progressive_calls(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_guard.local_progress import (
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
                        "builder_tool": "write_file",
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
            [{"tool": "write_file", "source_json_path": "outputs/report/source.json", "path": "outputs/report/report.xlsx"}],
        )
        is False
    )


def test_local_progress_guard_allows_writer_and_document_builder_tools(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_guard.local_progress import (
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
                        "writer_tool": "write_file",
                    },
                    {
                        "code": "STAGING_BUILDER_READY",
                        "recommended_action": "invoke_builder_tool",
                        "builder_tool": "write_file",
                    },
                ],
                "pending_materialization_targets": [],
            },
        },
    )
    params = _params()
    agent = SimpleNamespace(root=tmp_path)

    assert has_required_local_progress_guard(agent, params, [{"tool": "write_file", "rows": [{"a": 1}]}]) is False
    assert has_required_local_progress_guard(agent, params, [{"tool": "write_file", "path": "out.pdf"}]) is False


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
