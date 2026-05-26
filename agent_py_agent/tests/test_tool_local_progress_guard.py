from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace


# LLM: repeated remote-style exploration should warn at configured local-progress budget ratios.
# 函数用途: 验证 closeout 失败后默认 50 轮本地进展预算会在 1/3、2/3 处提示并在上限阻断。
def test_local_progress_guard_warns_at_default_budget_ratios_then_blocks(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_local_progress_guard import (
        has_required_local_progress_guard,
        local_progress_guard_block_response,
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
    exploratory_calls = [{"tool": "read_artifact", "artifact_ref": "memory_archive/artifacts/tool_outputs/demo.json"}]

    _assert_guard_false_for_rounds(has_required_local_progress_guard, (agent, params, exploratory_calls), 15)
    assert has_required_local_progress_guard(agent, params, exploratory_calls) is True
    context = local_progress_guard_context(agent, redirects=0)
    assert "33%" in context
    assert "16/50" in context

    _assert_guard_false_for_rounds(has_required_local_progress_guard, (agent, params, exploratory_calls), 16)
    assert has_required_local_progress_guard(agent, params, exploratory_calls) is True
    context = local_progress_guard_context(agent, redirects=0)
    assert "66%" in context
    assert "33/50" in context

    _assert_guard_false_for_rounds(has_required_local_progress_guard, (agent, params, exploratory_calls), 16)
    assert has_required_local_progress_guard(agent, params, exploratory_calls) is True
    assert local_progress_guard_context(agent, redirects=0) == ""
    assert "[LOCAL_PROGRESS_GUARD_BLOCKED]" in local_progress_guard_block_response(agent).text


def _assert_guard_false_for_rounds(func, args: tuple[object, object, object], rounds: int) -> None:
    for _ in range(rounds):
        assert func(*args) is False


# LLM: closeout no-progress threshold is the machine contract for how many exploration turns are allowed.
# 函数用途: 验证 local-progress guard 使用 closeout 里的结构化阈值，而不是硬编码拦截次数。
def test_local_progress_guard_uses_closeout_no_progress_threshold(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_local_progress_guard import (
        has_required_local_progress_guard,
        local_progress_guard_context,
    )

    payload = _closeout_payload(work_progress_fingerprint="same-progress")
    payload["delivery_progress"]["no_progress_block_threshold"] = 6
    _write_closeout(tmp_path, payload)
    params = _params()
    agent = SimpleNamespace(root=tmp_path)
    exploratory_calls = [{"tool": "fetch_url", "url": "https://example.test/data.json"}]

    assert has_required_local_progress_guard(agent, params, exploratory_calls) is False
    assert has_required_local_progress_guard(agent, params, exploratory_calls) is True
    assert "33%" in local_progress_guard_context(agent, redirects=0)

    for _ in range(1):
        assert has_required_local_progress_guard(agent, params, exploratory_calls) is False
    assert has_required_local_progress_guard(agent, params, exploratory_calls) is True


# LLM: zero no-progress thresholds should mean no local-progress cap, not immediate or default blocking.
# 函数用途: 验证结构化 no_progress_block_threshold=0 时不会因为连续只读轮次触发阻断。
def test_local_progress_guard_zero_threshold_is_unlimited(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_local_progress_guard import (
        has_required_local_progress_guard,
        local_progress_guard_context,
    )

    payload = _closeout_payload(work_progress_fingerprint="same-progress")
    payload["delivery_progress"]["no_progress_block_threshold"] = 0
    _write_closeout(tmp_path, payload)
    params = _params()
    agent = SimpleNamespace(root=tmp_path)
    exploratory_calls = [{"tool": "fetch_url", "url": "https://example.test/data.json"}]

    for _ in range(9):
        assert has_required_local_progress_guard(agent, params, exploratory_calls) is False
    assert has_required_local_progress_guard(agent, params, exploratory_calls) is True
    context = local_progress_guard_context(agent, redirects=0)
    assert "不会因次数阻断" in context
    assert "第 10 轮固定提醒" in context

    for _ in range(9):
        assert has_required_local_progress_guard(agent, params, exploratory_calls) is False
    assert has_required_local_progress_guard(agent, params, exploratory_calls) is True
    assert "第 20 轮固定提醒" in local_progress_guard_context(agent, redirects=0)


# LLM: local-progress guard can tune unlimited reminder intervals from the shared config object.
# 函数用途: 验证本地进展门无限模式的固定提醒间隔由同一个探索/进展配置控制。
def test_local_progress_guard_unlimited_hint_interval_is_configurable(tmp_path: Path):
    from agent_py_agent.agent.agent_core.exploration_fuse_config import ExplorationFuseConfig
    from agent_py_agent.agent.agent_core.tool_local_progress_guard import (
        has_required_local_progress_guard,
        local_progress_guard_context,
    )

    payload = _closeout_payload(work_progress_fingerprint="same-progress")
    payload["delivery_progress"]["no_progress_block_threshold"] = 0
    _write_closeout(tmp_path, payload)
    params = _params()
    agent = SimpleNamespace(
        root=tmp_path,
        _exploration_fuse_config=ExplorationFuseConfig(
            local_progress_round_threshold=0,
            local_progress_unlimited_hint_interval=7,
        ),
    )
    exploratory_calls = [{"tool": "fetch_url", "url": "https://example.test/data.json"}]

    for _ in range(6):
        assert has_required_local_progress_guard(agent, params, exploratory_calls) is False
    assert has_required_local_progress_guard(agent, params, exploratory_calls) is True
    assert "第 7 轮固定提醒" in local_progress_guard_context(agent, redirects=99)


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
