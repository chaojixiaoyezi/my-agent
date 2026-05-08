# LLM: memory-fact-write tests cover the manual bridge from completion prompt to auto resume.
# 模块用途: 验证用户确认的 compact 补全事实能写入 runtime_facts 并被下一次 apply 读取。

from __future__ import annotations

import json
from pathlib import Path

from test_memory_compact import _workspace, _write_compact_fixture, _write_config

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


# LLM: test_memory_fact_write_closes_compact_missing_fields verifies the semi-auto manual fact loop.
# 函数用途: 先让 compact resume 因缺字段阻断，再写入用户确认事实并重新 apply，确认 auto guard 放行。
def test_memory_fact_write_closes_compact_missing_fields(tmp_path: Path, capsys) -> None:
    config_path = _write_config(tmp_path)
    root = _workspace(config_path)
    _write_compact_fixture(root)
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


# LLM: _apply_scoped_compact keeps the test fact source tied to the same request scope.
# 函数用途: 使用 request/session 过滤重复执行 compact apply，确保 runtime_facts/<request_id> 可被读取。
def _apply_scoped_compact(root: Path) -> dict:
    return apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )
