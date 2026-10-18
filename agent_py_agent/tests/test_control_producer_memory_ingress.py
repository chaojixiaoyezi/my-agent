# LLM: 原HTTP issuer和真实C生命周期提供事实；不从dispatch恢复Memory封印，不替代用户确认。
# 模块用途: 验证新增C运输保留普通用户的候选人工确认入口及原重放结果。
from agent_py_agent.agent.gateway_parts import control_operation_service as ops
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import (
    bind_coordinator_generation,
    coordinator_guard,
)
from agent_py_agent.tests.test_memory_candidate_control import memory_control  # noqa: F401


def test_c_memory_confirmation_keeps_original_http_ingress(memory_control):  # noqa: F811
    control = memory_control
    with coordinator_guard(control.paths.root, 2):
        bind_coordinator_generation(control.paths.root, 2, "memory-c-generation")
        listing = control.send("/memory candidates")[1]
        assert listing["ok"] and listing["control_state"] == "completed"
        assert listing["details"]["candidates"][0]["candidate_id"] == control.candidate.candidate_id
        preview = control.send("/memory candidates preview " + control.candidate.candidate_id)[1]
        assert preview["ok"] and preview["details"]["state"] == "preview"
        text = preview["details"]["confirm_command"]
        confirmed = control.send(text, "memory-confirm-once")[1]
        assert confirmed["ok"] and confirmed["details"]["state"] == "completed"
        assert control.agent.memory.all()[0].content == control.candidate.content
        receipt = ops.read_gateway_control_operation(control.paths, confirmed["operation_id"])
        assert receipt.dispatch is None
        assert control.send(text, "memory-confirm-once")[1] == confirmed


def test_c_memory_missing_live_ingress_cannot_use_persisted_scope(memory_control):  # noqa: F811
    control = memory_control
    control.caller["authenticated"] = False
    with coordinator_guard(control.paths.root, 2):
        bind_coordinator_generation(control.paths.root, 2, "memory-c-generation")
        response = control.send("/memory candidates")[1]
        assert response["ok"] is False
        assert response["error_code"] == "MEMORY_CONTROL_NOT_USER_REQUEST"
        assert "operation_id" not in response
        assert control.agent.memory.all() == []
        assert not list((control.paths.root / "control_operations").glob("*.json"))
