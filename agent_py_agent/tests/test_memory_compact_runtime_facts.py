# LLM: Runtime fact source tests prove real run --save can feed compact resume work_state fields.
# 模块用途: 验证真实 CLI run 写入显式验收、约束、测试事实源，并让 auto resume 在字段齐全时放行。

from __future__ import annotations

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


# LLM: test_real_run_runtime_fact_source_allows_complete_compact_resume covers the saved-run CLI path.
# 函数用途: 跑真实 echo backend run --save，再确认 compact apply 读取 runtime_facts 并让 auto guard 放行。
def test_real_run_runtime_fact_source_allows_complete_compact_resume(tmp_path: Path, capsys) -> None:
    config_path = _write_config(tmp_path)
    run_args = build_parser().parse_args(["--config", str(config_path), "run", _explicit_prompt(), "--save"])

    assert run_args.func(run_args) == 0
    capsys.readouterr()
    result = apply_memory_compact(_workspace(config_path), MemoryCompactApplyOptions(MemoryCompactPlanOptions()))
    resume = build_memory_compact_resume(
        _workspace(config_path),
        MemoryCompactResumeOptions(apply_ref=result["apply_id"], resume_mode="auto"),
    )

    assert list((_workspace(config_path) / "memory_archive" / "runtime_facts").glob("*"))
    assert result["work_state_snapshot"]["acceptance"]["items"] == ["compact resume can restore explicit facts"]
    assert result["work_state_snapshot"]["constraints"]["items"] == ["do not touch user config"]
    assert result["work_state_snapshot"]["latest_tests"]["items"] == [
        "python3 -m pytest -q agent_py_agent/tests/test_memory_compact.py"
    ]
    assert result["work_state_snapshot"]["missing_fields"] == []
    assert resume["action_guard"]["status"] == "allow_automated_continue"
    assert resume["action_guard"]["allowed_to_continue"] is True
    assert resume["completion_prompt"]["status"] == "complete"


# LLM: _write_config keeps the real-run test isolated from repository and user config.
# 函数用途: 写入临时 echo backend 配置，让测试只使用 tmp_path workspace。
def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "agent_config.yaml"
    config_path.write_text(
        'workspace_root: "workspace"\n'
        'model_backend: "echo"\n'
        'subagent_workspace: "subagents"\n'
        'local_store_path: "local_store/local.db"\n'
        'local_store_files_dir: "local_store/files"\n'
        'local_store_events_path: "local_store/events.jsonl"\n',
        encoding="utf-8",
    )
    return config_path


# LLM: _workspace mirrors CLI workspace root resolution for the temporary config.
# 函数用途: 返回临时配置对应的 workspace 路径，供 compact apply/resume 直接读取。
def _workspace(config_path: Path) -> Path:
    return config_path.parent / "workspace"


# LLM: _explicit_prompt includes labeled fields that runtime_fact_source is allowed to persist.
# 函数用途: 构造带显式验收、约束、测试章节的用户输入，避免测试依赖自然语言猜测。
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
