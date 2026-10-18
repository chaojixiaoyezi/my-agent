"""原控制泵必须租用真实缓存对象；只替换控制业务，不伪造registry/claim或物理W。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.control_commands import ConversationControlResult
from agent_py_agent.agent.gateway_parts import control_operation_service as ops
from agent_py_agent.agent.gateway_parts.multiprocess_worker_dispatch import WorkerRequestPump
from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_gateway_multiprocess_control_worker import prepared_case


@pytest.mark.parametrize("reconcile", [False, True])
def test_control_execution_and_observation_pin_actual_cached_agent(tmp_path, monkeypatch, reconcile):
    # 原池在进入测试W模式前构造，builder只提供缓存身份；不是正式W的owner构造证明。
    pool = OwnerScopedAgentPool(AgentConfig(), tmp_path / "constructor")
    owner = OwnerIdentity.provider_user("feishu", "alice")
    monkeypatch.setattr(pool, "_builder", lambda *_args: SimpleNamespace())
    cached = pool.get(owner)
    paths, original, _scope, frozen, _fake_pool, _owners = prepared_case(tmp_path, monkeypatch)
    observed = []

    # LLM: 原控制claim/登记/完成仍由实际producer与泵处理，替身只记录业务回调时的真实缓存pin。
    # 函数用途: 观察原对象是否受到idle回收保护，不代行停止、授权或记忆提交。
    def effect(agent, _paths, command, _scope):
        observed.append(agent is cached and pool._pins.get(id(cached)) == 1)
        return ConversationControlResult(command.kind, True, "合成观察", delivery_status="unknown",
                                         guidance_dedupe_key="original-input")

    monkeypatch.setattr(ops, "execute_gateway_conversation_control", effect)
    pump = WorkerRequestPump(frozen, pool, 1)
    try:
        pump.poll()
    finally:
        pump.close()
    completed = ops.read_gateway_control_operation(paths, original.operation_id)
    assert completed.state == "completed"
    if reconcile:
        # LLM: 仅替换领域读观察；原W身份、dispatch executor、实际get/pin和末门仍执行。
        # 函数用途: 记录排空期间旧回执观察的实际租用对象，不制造已消费或完成结果。
        def observation(agent, _receipt):
            observed.append(agent is cached and pool._pins.get(id(cached)) == 1)
            return None

        monkeypatch.setattr(pump.controls, "_observe_delivery", observation)
        pump.controls._reconcile_delivery(completed)
    assert observed == ([True, True] if reconcile else [True])
    assert pool._pins == {}
