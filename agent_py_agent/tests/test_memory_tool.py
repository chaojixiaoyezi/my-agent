"""remember 统一 Candidate/Promotion 主链测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.capability.memory_tool import (
    RememberTool,
    _normalize_tags,
    classify_memory_retention,
)
from agent_py_agent.agent.conversation.authority import CONVERSATION_AUDIT_PREPARE_ATTR
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings.config import AgentConfig


def _agent_with_current_user(tmp_path, content: str = "请记住 moneywise 项目使用 UTC 保存时间"):
    agent = SimpleAgent(
        AgentConfig(my_agent_home=str(tmp_path / "home"), prompt_files=[]),
        tmp_path / "workspace",
    )
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
        }
    )
    request_id = "req-memory-test"
    agent.conversation_store.append_message(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": content,
            "metadata": {"gateway_request_id": request_id},
        }
    )
    agent._current_run_params = SimpleNamespace(
        request_id=request_id,
        run_id="run-memory-test",
        task_id="task-memory-test",
        task_attributes={"conversation_thread_id": thread.thread_id},
    )
    return agent


def test_remember_user_explicit_goes_through_candidate_and_promotes(tmp_path):
    agent = _agent_with_current_user(tmp_path)
    result = RememberTool(agent).execute(
        {
            "content": "moneywise 项目使用 UTC 保存时间",
            "kind": "project",
            "tags": ["moneywise", "time"],
            "origin": "user_explicit",
            "subject_key": "project.moneywise.timezone",
            "scope": {"scope_type": "project", "scope_key": "project:moneywise"},
        }
    )

    payload = json.loads(result.output)
    assert result.ok
    assert payload["active_memory_changed"] is True
    assert payload["results"][0]["status"] == "promoted"
    assert len(agent.memory.all()) == 1
    assert agent.memory_candidates.list()[0].status == "promoted"


def test_remember_is_not_exposed_in_named_audit_prepare(tmp_path):
    agent = _agent_with_current_user(tmp_path)
    agent._current_run_params = SimpleNamespace(
        task_attributes={CONVERSATION_AUDIT_PREPARE_ATTR: True}
    )

    availability = RememberTool(agent).availability()

    assert availability.available is False
    assert "task-scoped" in availability.reason


def test_remember_missing_content_errors(tmp_path):
    agent = _agent_with_current_user(tmp_path)
    result = RememberTool(agent).execute(
        {
            "content": "  ",
            "origin": "user_explicit",
            "subject_key": "project.empty",
            "scope": {"scope_type": "project", "scope_key": "project:test"},
        }
    )
    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"


def test_remember_unavailable_when_no_memory():
    tool = RememberTool(SimpleNamespace(memory=None))
    result = tool.execute({"content": "x"})
    assert result.ok is False
    assert result.error_code == "TOOL_UNAVAILABLE"


def test_remember_rejects_temporary_unlock_or_verification_codes(tmp_path):
    agent = _agent_with_current_user(tmp_path)
    tool = RememberTool(agent)

    for index, content in enumerate(("卡片解锁码是 482913", "OTP: A1B2C3", "临时密码：Abcd1234")):
        result = tool.execute(
            {
                "content": content,
                "origin": "user_explicit",
                "subject_key": f"security.credential.{index}",
                "scope": {"scope_type": "personal", "scope_key": "personal"},
            }
        )
        assert result.ok is False
        assert result.error_code == "MEMORY_TRANSIENT_DATA_BLOCKED"
    assert agent.memory_candidates.list() == []
    assert agent.memory.all() == []


def test_memory_retention_does_not_block_normal_password_preferences():
    decision = classify_memory_retention("用户喜欢研究密码学和身份安全", ["interest"])
    assert decision.durable is True


def test_normalize_tags():
    assert _normalize_tags(["a", " b ", ""]) == ["a", "b"]
    assert _normalize_tags("solo") == ["solo"]
    assert _normalize_tags(None) == []
    assert _normalize_tags(123) == []


def test_remember_registered_in_agent_toolset(tmp_path):
    agent = SimpleAgent(
        AgentConfig(my_agent_home=str(tmp_path / "home"), prompt_files=[]),
        tmp_path / "workspace",
    )
    names = [getattr(spec, "name", "") for spec in agent.tools.specs()]
    assert "remember" in names
