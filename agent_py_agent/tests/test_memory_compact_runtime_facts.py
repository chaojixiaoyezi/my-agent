# LLM: Runtime fact source tests prove real run --save can feed compact resume work_state fields.
# 模块用途: 验证真实 CLI run 写入显式验收、约束、测试事实源，并让 auto resume 在字段齐全时放行。

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
from agent_py_agent.agent.memory_archive.runtime_fact_source import (
    RuntimeFactSourceRequest,
    write_runtime_fact_source,
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


# LLM: test_runtime_fact_source_stops_sections_at_unknown_headings guards scoped facts from prompt prose bleed.
# 函数用途: 确认显式验收段落后遇到未知标题时会停止收集，避免实施步骤被当成验收事实。
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


# LLM: Generated compact continuation packets should carry facts into the next compact cycle.
# 函数用途: 验证 runtime facts 会解析受控 Compact Auto Continuation 注入块，但不会把后续标题下的内容串场。
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
