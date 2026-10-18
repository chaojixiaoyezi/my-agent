# LLM: 使用真实C生命周期锁和规范回执；只替换业务执行观察点，不替代转发判断。
# 模块用途: 钉住远端控制只能入队、重复和查询不能在C物化owner。
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.control_commands import (
    ConversationControlResult,
    parse_conversation_control,
)
from agent_py_agent.agent.gateway_parts import control_operation_service as ops
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import (
    bind_coordinator_generation,
    coordinator_guard,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


@pytest.mark.parametrize("text", ["/stop", "/btw 合成插话", "/approve synthetic-token", "/admin status"])
def test_remote_control_prepares_once_without_c_effect_or_owner_construction(tmp_path, monkeypatch, text):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    scope = GatewayControlScope(user_id="alice", channel="feishu", conversation_id="room",
                                metadata={"message_id": "control-a", "expected_turn_id": "task-a",
                                          "expected_execution": {"schema_version": "gateway_execution_target.v1",
                                                                 "thread_id": "thread-a", "kind": "task", "turn_id": "task-a",
                                                                 "claim_id": "claim-a", "execution_attempt_id": "",
                                                                 "runtime_authority": {}, "task_id": "task-a"}},
                                resolved_owner=OwnerIdentity.provider_user("feishu", "alice"))
    agent = SimpleNamespace()
    calls = []

    def effect(_agent, _paths, command, _scope):
        calls.append("effect")
        return ConversationControlResult(command.kind, True, "执行了")

    def materialize(*_args):
        calls.append("owner")
        return agent

    monkeypatch.setattr(ops, "execute_gateway_conversation_control", effect)
    monkeypatch.setattr(ops, "_control_operation_agent", materialize)
    with coordinator_guard(paths.root, 2):
        bind_coordinator_generation(paths.root, 2, "control-generation")
        command = parse_conversation_control(text, reject_unknown_slash=True)
        first = ops.execute_gateway_control_operation(agent, paths, command, scope, command_text=text)
        assert calls == []
        assert first.state == "prepared"
        assert first.conversation_thread_id == ""
        assert first.to_dict()["dispatch"]["c_run_id"] == "control-generation"
        replay = ops.execute_gateway_control_operation(agent, paths, command, scope, command_text=text)
        status = ops.reconcile_gateway_control_operation(agent, paths, first.operation_id)
        assert replay.to_dict() == first.to_dict() == status.to_dict()
        assert calls == []
