from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace


# LLM: Bootstrap repair context should expose structured startup actions and checkpoint shape hints.
# 函数用途: 验证开工阶段模型拿到的是机器合同字段，而不是只能靠自然语言猜要写什么文件形状。
def test_bootstrap_materialization_context_includes_startup_actions_and_shape_hints(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_bootstrap_materialization_guard import (
        bootstrap_materialization_context,
    )

    params = _params(delivery_contract=_delivery_contract_with_shape_hint())
    agent = SimpleNamespace(root=tmp_path)

    context = bootstrap_materialization_context(agent, params, repairs=0)
    payload_text = context.splitlines()[1]
    payload = json.loads(payload_text)

    assert payload["startup_actions"][0]["action"] == "materialize_checkpoint"
    assert payload["pending_materialization_targets"][0]["checkpoint_shape_hint"]
    assert payload["checkpoint_shape_hints"]["outputs/report/source_data.json"]


# LLM: Generic artifact writer and builder tools should count as bootstrap materialization attempts.
# 函数用途: 验证主代理调用通用 writer/builder 时不会被开工 guard 误判成“没有执行创建/写入动作”。
def test_bootstrap_materialization_allows_generic_artifact_tools(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_bootstrap_materialization_guard import (
        has_required_bootstrap_materialization,
        is_bootstrap_materialization_productive_call,
    )

    params = _params(delivery_contract=_delivery_contract_with_shape_hint())
    agent = SimpleNamespace(root=tmp_path)
    calls = [
        {
            "tool": "write_structured_json",
            "path": "outputs/report/source_data.json",
            "sheets": [{"name": "榜单", "rows": [{"项目名": "demo"}]}],
        },
        {
            "tool": "data_to_workbook",
            "source_json_path": "outputs/report/source_data.json",
            "path": "outputs/report/report.xlsx",
        },
        {
            "tool": "markdown_to_pdf",
            "source_markdown_path": "outputs/report/report.md",
            "path": "outputs/report/report.pdf",
        },
    ]

    assert is_bootstrap_materialization_productive_call(calls)
    assert has_required_bootstrap_materialization(agent, params, calls) is False


# LLM: Shell command classification should inspect all command segments, not only the first token.
# 函数用途: 验证 "ls; mkdir" 这类混合命令不会因为先检查目录就被误判成纯 bootstrap 空转。
def test_bootstrap_materialization_treats_mixed_shell_mutation_as_productive(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_bootstrap_materialization_guard import (
        has_required_bootstrap_materialization,
        is_bootstrap_materialization_productive_call,
    )

    params = _params(delivery_contract=_delivery_contract_with_shape_hint())
    agent = SimpleNamespace(root=tmp_path)
    calls = [{"tool": "run_command", "command": "ls -la outputs; mkdir -p outputs/report"}]

    assert is_bootstrap_materialization_productive_call(calls)
    assert has_required_bootstrap_materialization(agent, params, calls) is False


# LLM: Bootstrap may need remote evidence before it can write a non-empty structured checkpoint.
# 函数用途: 验证证据采集工具不会在 bootstrap 阶段被误当成空转，但仍保持“纯本地检查先纠偏”的边界。
def test_bootstrap_materialization_allows_evidence_tool_calls_to_run(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_loop_repair_counters import ToolLoopRepairCounters
    from agent_py_agent.agent.agent_core.tool_loop_response_decision import (
        ToolLoopResponseDecisionRequest,
        tool_loop_response_decision,
    )
    from agent_py_agent.agent.backend import ModelResponse

    agent = _agent(tmp_path, {"CALL_FETCH": [{"tool": "fetch_url", "url": "https://example.test/data.json"}]})
    params = _params(delivery_contract=_delivery_contract_with_shape_hint())

    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="CALL_FETCH", backend="fake"),
            counters=ToolLoopRepairCounters(),
        )
    )

    assert decision.action == "run_tools"
    assert decision.calls == [{"tool": "fetch_url", "url": "https://example.test/data.json"}]
    assert params.tool_context == []


# LLM: Pure inspection still should not consume tool turns before the first bootstrap target exists.
# 函数用途: 验证允许证据采集后，list_files/read_file 这类纯检查仍会收到结构化开工纠偏。
def test_bootstrap_materialization_still_redirects_inspection_tool_calls(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_loop_repair_counters import ToolLoopRepairCounters
    from agent_py_agent.agent.agent_core.tool_loop_response_decision import (
        ToolLoopResponseDecisionRequest,
        tool_loop_response_decision,
    )
    from agent_py_agent.agent.backend import ModelResponse

    agent = _agent(tmp_path, {"CALL_LIST": [{"tool": "list_files", "path": "."}]})
    params = _params(delivery_contract=_delivery_contract_with_shape_hint())

    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="CALL_LIST", backend="fake"),
            counters=ToolLoopRepairCounters(),
        )
    )

    assert decision.action == "continue"
    assert decision.calls == []
    assert decision.counters.bootstrap_materialization_redirects == 1
    assert any("bootstrap-materialization" in item for item in params.tool_context)


# LLM: _delivery_contract_with_shape_hint is the structured bootstrap fixture for shape-hint tests.
# 函数用途: 声明一个 checkpoint target、startup action 和对应的 checkpoint_shape_hints。
def _delivery_contract_with_shape_hint() -> dict[str, object]:
    return {
        "bootstrap_contract": {
            "materialization_targets": [_checkpoint_target()],
            "startup_actions": [
                {
                    "action": "materialize_checkpoint",
                    "checkpoint_ref": "outputs/report/source_data.json",
                    "priority": 1,
                }
            ],
        },
        "artifacts": [_artifact_with_shape_hint()],
    }


# LLM: _checkpoint_target returns the bootstrap materialization target fixture.
# 函数用途: 给测试提供一个机器声明的 checkpoint 目标路径。
def _checkpoint_target() -> dict[str, object]:
    return {
        "artifact_id": "report",
        "kind": "xlsx",
        "target_type": "checkpoint",
        "workspace_relative_path": "outputs/report/source_data.json",
    }


# LLM: _artifact_with_shape_hint returns the artifact staging contract fixture.
# 函数用途: 给 bootstrap guard 提供 checkpoint_shape_hints，不依赖自然语言提示。
def _artifact_with_shape_hint() -> dict[str, object]:
    return {
        "artifact_id": "report",
        "kind": "xlsx",
        "validation_contract": {
            "staging_contract": {
                "checkpoint_shape_hints": {
                    "outputs/report/source_data.json": '{"sheets":[{"name":"榜单","rows":[{"项目名":"..."}]}]}'
                }
            }
        },
    }


def _params(delivery_contract: dict[str, object]):
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
        delivery_contract=delivery_contract,
    )


def _agent(root: Path, calls_by_text: dict[str, list[dict[str, object]]]):
    class _Tools:
        workspace_root = root

        def parse_tool_calls(self, text: str):
            return calls_by_text.get(text, [])

    return SimpleNamespace(
        backend=SimpleNamespace(name="fake"),
        config=SimpleNamespace(enable_tools=True),
        root=root,
        tools=_Tools(),
    )
