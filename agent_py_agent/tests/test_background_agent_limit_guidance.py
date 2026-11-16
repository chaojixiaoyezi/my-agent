# 模块用途: 后台身份额度满时，模型看到的恢复建议指向真实可行的路径：同空间复用 available 助手，
#   或以 drain 退役不再需要的助手释放名额；额度口径与退役语义不变。只用 tmp 真实 Store，不运行模型。
from __future__ import annotations

import pytest

from agent_py_agent.agent.contracts.error_taxonomy import error_contract
from agent_py_agent.agent.conversation.background_agents import BackgroundAgentService
from agent_py_agent.agent.tooling.runtime_contracts import ToolResult
from agent_py_agent.tests.test_business_v12_f3_agents import (
    business_host,  # noqa: F401 - pytest fixture
)


def test_limit_hint_names_reuse_and_drain_retire():
    hint = error_contract("BACKGROUND_AGENT_LIMIT_REACHED").recovery_hint
    for phrase in ("background_agents.list", "available", "tasks.assign", "background_agents.retire", "drain",
                   "征得用户确认", "不转前台、换owner或停止无关任务"):
        assert phrase in hint, phrase
    result = ToolResult(call_id="c1", tool_name="business_execution", status="failed", handler_executed=True,
                        error_code="BACKGROUND_AGENT_LIMIT_REACHED", effect_outcome="not_started")
    assert result.recovery_hint == hint and result.retryable is False


def test_drain_retire_frees_the_slot_the_hint_points_to(business_host):  # noqa: F811
    host, actor = business_host
    host.config.max_background_agents = 1
    service = BackgroundAgentService(host)
    first = service.create({"space_id": "space-A", "name": "idle"}, actor=actor, idempotency_key="idle-a")["agent"]
    with pytest.raises(ValueError, match="BACKGROUND_AGENT_LIMIT_REACHED") as caught:
        service.create({"space_id": "space-B", "name": "new"}, actor=actor, idempotency_key="new-b")
    # 空闲身份仍占名额，且错误细节如实给出可复用身份数。
    assert caught.value.details["used"] == 1 and caught.value.details["available_agents"] == 1
    retired = service.transition(first["agent_id"], "retire", actor=actor, expected_revision=first["revision"],
                                 idempotency_key="retire-idle-a", disposition="drain")["agent"]
    assert retired["identity_state"] == "retired"
    created = service.create({"space_id": "space-B", "name": "new"}, actor=actor, idempotency_key="new-b")["agent"]
    assert created["space_id"] == "space-B" and created["identity_state"] == "available"
