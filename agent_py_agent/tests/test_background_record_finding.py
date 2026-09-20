from agent_py_agent.agent.conversation.background_tool_policy import (
    SCHEDULED_BACKGROUND_ALLOWED_TOOLS,
    SUBAGENT_INTEGRATION_ALLOWED_TOOLS,
)
from agent_py_agent.agent.conversation.runtime import _scheduled_continuation_prompt


def test_record_finding_absent_from_background_tool_profiles() -> None:
    """record_finding 工具已下线: 结论落账走宿主审计链, 后台 profile 不再暴露它。"""
    assert "record_finding" not in SCHEDULED_BACKGROUND_ALLOWED_TOOLS
    assert "record_finding" not in SUBAGENT_INTEGRATION_ALLOWED_TOOLS


def test_scheduled_prompt_does_not_impose_an_audit_workflow() -> None:
    prompt = _scheduled_continuation_prompt("progress_policy_due")
    assert "record_finding" not in prompt
    assert "watch_stream" not in prompt
    assert "child count" not in prompt
    assert "do not prescribe analysis" in prompt
    assert "current evidence" in prompt
