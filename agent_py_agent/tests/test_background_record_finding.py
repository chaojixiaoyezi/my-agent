from agent_py_agent.agent.conversation.runtime import (
    SCHEDULED_BACKGROUND_ALLOWED_TOOLS,
    SUBAGENT_INTEGRATION_ALLOWED_TOOLS,
    _scheduled_continuation_prompt,
)


def test_record_finding_present_in_background_tool_profiles() -> None:
    assert "record_finding" in SCHEDULED_BACKGROUND_ALLOWED_TOOLS
    assert "record_finding" in SUBAGENT_INTEGRATION_ALLOWED_TOOLS


def test_scheduled_prompt_does_not_impose_an_audit_workflow() -> None:
    prompt = _scheduled_continuation_prompt("progress_policy_due")
    assert "record_finding" not in prompt
    assert "watch_stream" not in prompt
    assert "child count" not in prompt
    assert "do not prescribe analysis" in prompt
    assert "current evidence" in prompt
