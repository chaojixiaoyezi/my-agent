
from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.__main__ import build_parser
from agent_py_agent.agent.memory_archive.compact import MemoryCompactPlanOptions
from agent_py_agent.agent.memory_archive.compact_apply import (
    MemoryCompactApplyOptions,
    apply_memory_compact,
)
from agent_py_agent.agent.memory_archive.compact_resume import (
    MemoryCompactResumeOptions,
    build_memory_compact_resume,
)
from agent_py_agent.tests.memory_compact_support import (
    workspace,
    write_compact_fixture,
    write_config,
)


def test_memory_fact_write_closes_compact_missing_fields(tmp_path: Path, capsys) -> None:
    config_path = write_config(tmp_path)
    root = workspace(config_path)
    write_compact_fixture(root)
    first_apply = _apply_scoped_compact(root)
    parser = build_parser()

    args = parser.parse_args([
        "--config", str(config_path), "memory-fact-write",
        "--from-compact", first_apply["apply_id"],
        "--acceptance", "compact resume can continue after approved facts",
        "--constraint", "do not touch user config",
        "--latest-test", "python3 -m pytest -q agent_py_agent/tests/test_memory_compact.py",
        "--json",
    ])
    code = args.func(args)
    payload = json.loads(capsys.readouterr().out)
    second_apply = _apply_scoped_compact(root)
    resume = build_memory_compact_resume(
        root,
        MemoryCompactResumeOptions(apply_ref=second_apply["apply_id"], resume_mode="auto"),
    )

    assert code == 0
    assert payload["fact_id"] == "request-compact"
    assert Path(payload["fact_source_path"], "task.json").exists()
    assert second_apply["work_state_snapshot"]["missing_fields"] == []
    assert resume["action_guard"]["status"] == "allow_automated_continue"


def _apply_scoped_compact(root: Path) -> dict:
    return apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )
