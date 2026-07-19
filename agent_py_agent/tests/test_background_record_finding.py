from agent_py_agent.agent.conversation.runtime import (
    SCHEDULED_BACKGROUND_ALLOWED_TOOLS,
    SUBAGENT_INTEGRATION_ALLOWED_TOOLS,
    _scheduled_continuation_prompt,
)


def test_record_finding_present_in_background_tool_profiles() -> None:
    assert "record_finding" in SCHEDULED_BACKGROUND_ALLOWED_TOOLS
    assert "record_finding" in SUBAGENT_INTEGRATION_ALLOWED_TOOLS


def test_scheduled_prompt_demands_per_item_findings() -> None:
    prompt = _scheduled_continuation_prompt("progress_policy_due")
    assert "record_finding" in prompt
    assert "逐条" in prompt
    assert "结论账是主代理内部续跑和收口依据" in prompt
    assert "不要把账本标题、记录 ID 或内部记录块原样发给用户" in prompt
    assert "自动送达用户面" not in prompt
