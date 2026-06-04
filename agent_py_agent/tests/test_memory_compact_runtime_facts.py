
from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.memory_archive.compact import MemoryCompactPlanOptions
from agent_py_agent.agent.memory_archive.compact_apply import (
    MemoryCompactApplyOptions,
    apply_memory_compact,
)
from agent_py_agent.agent.memory_archive.compact_resume import (
    MemoryCompactResumeOptions,
    build_memory_compact_resume,
)
from agent_py_agent.agent.memory_archive.runtime_fact_source import (
    RuntimeFactSourceRequest,
    write_runtime_fact_source,
)
from agent_py_agent.cli.parser import build_parser


def test_real_run_runtime_fact_source_allows_complete_compact_resume(tmp_path: Path, capsys) -> None:
    config_path = _write_config(tmp_path)
    run_args = build_parser().parse_args(["--config", str(config_path), "run", _explicit_prompt(), "--save"])

    assert run_args.func(run_args) == 0
    capsys.readouterr()
    result = apply_memory_compact(_owner_home(config_path), MemoryCompactApplyOptions(MemoryCompactPlanOptions()))
    resume = build_memory_compact_resume(
        _owner_home(config_path),
        MemoryCompactResumeOptions(apply_ref=result["apply_id"], resume_mode="auto"),
    )

    assert list((_owner_home(config_path) / "memory_archive" / "runtime_facts").glob("*"))
    assert result["work_state_snapshot"]["acceptance"]["items"] == ["compact resume can restore explicit facts"]
    assert result["work_state_snapshot"]["constraints"]["items"] == ["do not touch user config"]
    assert result["work_state_snapshot"]["latest_tests"]["items"] == [
        "python3 -m pytest -q agent_py_agent/tests/test_memory_compact.py"
    ]
    assert result["work_state_snapshot"]["missing_fields"] == []
    assert resume["action_guard"]["status"] == "allow_automated_continue"
    assert resume["action_guard"]["allowed_to_continue"] is True
    assert resume["completion_prompt"]["status"] == "complete"


def test_runtime_fact_source_stops_sections_at_unknown_headings(tmp_path: Path) -> None:
    root = tmp_path / "workspace"

    write_runtime_fact_source(
        RuntimeFactSourceRequest(
            root=root,
            request_id="req-section-stop",
            user_prompt=(
                "验收条件:\n"
                "- only approved acceptance\n\n"
                "实施步骤:\n"
                "- do not treat this as acceptance\n\n"
                "约束:\n"
                "- only approved constraint\n"
            ),
            response_text="ok",
            backend="echo",
            status="ok",
            next_actions=[],
            archive_tool_calls=[],
        )
    )

    payload = json.loads(
        (root / "memory_archive" / "runtime_facts" / "req-section-stop" / "task.json").read_text(encoding="utf-8")
    )
    assert payload["acceptance"] == ["only approved acceptance"]
    assert payload["constraints"] == ["only approved constraint"]
    assert "do not treat this as acceptance" not in payload["acceptance"]


def test_runtime_fact_source_reads_compact_auto_continuation_injection(tmp_path: Path) -> None:
    root = tmp_path / "workspace"

    write_runtime_fact_source(
        RuntimeFactSourceRequest(
            root=root,
            request_id="req-compact-continuation",
            user_prompt="继续执行 Compact Auto Continuation 包里的 Next Step。",
            response_text="ok",
            backend="echo",
            status="ok",
            next_actions=["continue"],
            archive_tool_calls=[],
            runtime_injections=(
                "# Compact Auto Continuation\n"
                "## Acceptance\n"
                "- compact packet remains complete\n"
                "## Constraints\n"
                "- do not redo completed work\n"
                "## Latest Tests\n"
                "Status: recorded\n"
                "- focused compact continuation test\n"
                "## Changed Files\n"
                "- should not become latest_tests\n",
            ),
        )
    )

    payload = json.loads(
        (root / "memory_archive" / "runtime_facts" / "req-compact-continuation" / "task.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["acceptance"] == ["compact packet remains complete"]
    assert payload["constraints"] == ["do not redo completed work"]
    assert payload["latest_tests"] == ["focused compact continuation test"]


def test_compact_work_state_does_not_import_workspace_root_checklist(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "TEST_CHECKLIST.md").write_text("- unrelated repository checklist\n", encoding="utf-8")
    write_runtime_fact_source(
        RuntimeFactSourceRequest(
            root=root,
            request_id="req-root-checklist",
            user_prompt="请整理几个项目并写一份报告。",
            response_text="running",
            backend="echo",
            status="running",
        )
    )

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            MemoryCompactPlanOptions(request_id="req-root-checklist", run_id="req-root-checklist", task_id="req-root-checklist")
        ),
    )

    work_state = result["work_state_snapshot"]
    assert work_state["goal"] == "请整理几个项目并写一份报告。"
    assert work_state["latest_tests"]["items"] == []
    assert work_state["latest_tests"]["source_paths"] == []
    assert work_state["read_files"] == []


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "agent_config.yaml"
    config_path.write_text(
        'workspace_root: "workspace"\n'
        f'my_agent_home: "{(tmp_path / "home").as_posix()}"\n'
        'model_backend: "echo"\n'
        'subagent_workspace: "subagents"\n'
        'local_store_path: "local_store/local.db"\n'
        'local_store_files_dir: "local_store/files"\n'
        'local_store_events_path: "local_store/events.jsonl"\n',
        encoding="utf-8",
    )
    return config_path


def _workspace(config_path: Path) -> Path:
    return config_path.parent / "workspace"


def _owner_home(config_path: Path) -> Path:
    return config_path.parent / "home" / "owners" / "local" / "main"


def _explicit_prompt() -> str:
    return (
        "真实运行 compact/resume 可用性测试。\n"
        "验收条件:\n"
        "- compact resume can restore explicit facts\n"
        "约束:\n"
        "- do not touch user config\n"
        "测试:\n"
        "- python3 -m pytest -q agent_py_agent/tests/test_memory_compact.py\n"
    )
