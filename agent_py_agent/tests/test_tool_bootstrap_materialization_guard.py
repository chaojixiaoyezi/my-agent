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
