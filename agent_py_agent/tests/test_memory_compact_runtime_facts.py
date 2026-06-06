
from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.memory_archive.compact import MemoryCompactPlanOptions
from agent_py_agent.agent.memory_archive.compact_apply import (
    MemoryCompactApplyOptions,
    apply_memory_compact,
)
from agent_py_agent.agent.memory_archive.runtime_fact_source import (
    RuntimeFactSourceRequest,
    write_runtime_fact_source,
)
from agent_py_agent.cli.parser import build_parser


def test_real_run_runtime_fact_source_keeps_prompt_sections_as_goal_only(tmp_path: Path, capsys) -> None:
    config_path = _write_config(tmp_path)
    run_args = build_parser().parse_args(["--config", str(config_path), "run", _explicit_prompt(), "--save"])

    assert run_args.func(run_args) == 0
    capsys.readouterr()

    fact_dirs = list((_owner_home(config_path) / "memory_archive" / "runtime_facts").glob("*"))
    assert fact_dirs
    payload = json.loads((fact_dirs[0] / "task.json").read_text(encoding="utf-8"))
    assert "验收条件" in payload["goal"]
    assert payload["acceptance"] == []
    assert payload["constraints"] == []
    assert payload["latest_tests"] == []


def test_runtime_fact_source_does_not_parse_prompt_sections_as_machine_facts(tmp_path: Path) -> None:
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
    assert payload["acceptance"] == []
    assert payload["constraints"] == []
    assert "only approved acceptance" in payload["goal"]
    assert "only approved constraint" in payload["goal"]


def test_runtime_fact_source_does_not_promote_succeeded_status_alias_to_final(tmp_path: Path) -> None:
    root = tmp_path / "workspace"

    write_runtime_fact_source(
        RuntimeFactSourceRequest(
            root=root,
            request_id="req-status-alias",
            user_prompt="测试状态别名",
            status="succeeded",
        )
    )

    payload = json.loads(
        (root / "memory_archive" / "runtime_facts" / "req-status-alias" / "task.json").read_text(encoding="utf-8")
    )
    assert payload["runtime_progress"]["phase"] == "running"


def test_runtime_fact_source_does_not_parse_compact_auto_continuation_markdown(tmp_path: Path) -> None:
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
    assert payload["acceptance"] == []
    assert payload["constraints"] == []
    assert payload["latest_tests"] == []


def test_runtime_fact_source_keeps_test_commands_from_tool_records(tmp_path: Path) -> None:
    root = tmp_path / "workspace"

    write_runtime_fact_source(
        RuntimeFactSourceRequest(
            root=root,
            request_id="req-tool-test",
            user_prompt="整理测试结果。",
            archive_tool_calls=[
                {
                    "tool": "shell",
                    "ok": True,
                    "parameters": {
                        "command": "python3 -m pytest -q agent_py_agent/tests/test_memory_compact.py"
                    },
                }
            ],
        )
    )

    payload = json.loads(
        (root / "memory_archive" / "runtime_facts" / "req-tool-test" / "task.json").read_text(encoding="utf-8")
    )
    assert len(payload["latest_tests"]) == 1
    assert "pytest" in payload["latest_tests"][0]


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
