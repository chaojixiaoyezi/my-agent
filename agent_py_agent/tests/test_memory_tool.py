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
    candidate = agent.memory_candidates.list()[0]
    assert candidate.status == "promoted"
    # 矩阵1:非 batch 单条 add(user_explicit/tool_verified) 持久化 auto_eligible 且自动晋升。
    assert candidate.promotion_mode == "auto_eligible"


def test_remember_single_add_tool_verified_authorized_auto_but_evidence_gate_blocks(
    tmp_path,
):
    """矩阵1:单条 add tool_verified 同样被授权 auto_eligible;权限授权≠证据验证,
    单测环境无 Tool Agent 证据账本时证据闸拦为 blocked_missing_evidence。"""
    agent = _agent_with_current_user(tmp_path)
    result = RememberTool(agent).execute(
        {
            "content": "moneywise 构建产物已生成",
            "kind": "project",
            "origin": "tool_verified",
            "evidence_refs": ["call-build-1"],
            "subject_key": "project.moneywise.build",
            "scope": {"scope_type": "project", "scope_key": "project:moneywise"},
        }
    )
    assert result.ok
    payload = json.loads(result.output)
    assert payload["active_memory_changed"] is False
    candidate = agent.memory_candidates.list()[0]
    assert candidate.promotion_mode == "auto_eligible"
    assert candidate.status == "blocked_missing_evidence"
    assert payload["results"][0]["reason_code"] == "TOOL_EVIDENCE_MISSING"
    assert payload["required_repairs"][0]["action"] == "retry_remember"
    assert "不能把缺证据说成等待用户确认" in payload["hint"]
    assert agent.memory.all() == []


def test_remember_repairs_mislabeled_tool_origin_with_current_user_evidence(tmp_path):
    agent = _agent_with_current_user(tmp_path)
    tool = RememberTool(agent)
    common = {
        "content": "moneywise 项目使用 UTC 保存时间",
        "kind": "project",
        "subject_key": "project.moneywise.timezone",
        "scope": {"scope_type": "project", "scope_key": "project:moneywise"},
    }

    blocked = tool.execute({**common, "origin": "tool_verified"})
    repaired = tool.execute({**common, "origin": "user_explicit"})

    assert json.loads(blocked.output)["results"][0]["reason_code"] == "TOOL_EVIDENCE_MISSING"
    assert json.loads(repaired.output)["active_memory_changed"] is True
    candidate = agent.memory_candidates.list()[0]
    assert candidate.origin == "user_explicit"
    assert candidate.status == "promoted"
    assert len(agent.memory.all()) == 1


def test_remember_batch_adds_autonomously_promote_each_verified_fact(tmp_path):
    """batch 只保证候选原子入账；每条合格事实随后自主通过同一证据门晋升。"""
    agent = _agent_with_current_user(tmp_path)
    result = RememberTool(agent).execute(
        {
            "action": "batch",
            "operations": [
                {
                    "action": "add",
                    "content": "moneywise 项目使用 UTC 保存时间",
                    "kind": "project",
                    "origin": "user_explicit",
                    "subject_key": "project.moneywise.timezone",
                    "scope": {"scope_type": "project", "scope_key": "project:moneywise"},
                },
                {
                    "action": "add",
                    "content": "moneywise 项目使用三线城市部署",
                    "kind": "project",
                    "origin": "user_explicit",
                    "subject_key": "project.moneywise.deploy",
                    "scope": {"scope_type": "project", "scope_key": "project:moneywise"},
                },
            ],
        }
    )
    payload = json.loads(result.output)
    assert result.ok
    assert payload["active_memory_changed"] is True
    assert [item["reason_code"] for item in payload["results"]] == [
        "PROMOTED",
        "PROMOTED",
    ]
    candidates = agent.memory_candidates.list()
    assert len(candidates) == 2
    assert {candidate.promotion_mode for candidate in candidates} == {"auto_eligible"}
    assert {candidate.status for candidate in candidates} == {"promoted"}
    assert len(agent.memory.all()) == 2


def test_remember_batch_single_add_uses_same_autonomous_policy(tmp_path):
    """入口形态不改变记忆权威；单项 batch 与普通 add 使用同一自主策略。"""
    agent = _agent_with_current_user(tmp_path)
    result = RememberTool(agent).execute(
        {
            "action": "batch",
            "operations": [
                {
                    "action": "add",
                    "content": "moneywise 项目使用 UTC 保存时间",
                    "kind": "project",
                    "origin": "user_explicit",
                    "subject_key": "project.moneywise.timezone",
                    "scope": {"scope_type": "project", "scope_key": "project:moneywise"},
                },
            ],
        }
    )
    payload = json.loads(result.output)
    assert result.ok
    assert payload["active_memory_changed"] is True
    assert payload["results"][0]["reason_code"] == "PROMOTED"
    candidate = agent.memory_candidates.list()[0]
    assert candidate.promotion_mode == "auto_eligible"
    assert candidate.status == "promoted"
    assert len(agent.memory.all()) == 1


def test_remember_batch_then_single_replay_stays_idempotently_promoted(tmp_path):
    """batch 不再污染长期权限；同一事实换入口重放仍只保留一条正式记忆。"""
    agent = _agent_with_current_user(tmp_path)
    operation = {
        "action": "add",
        "content": "moneywise 项目使用 UTC 保存时间",
        "kind": "project",
        "origin": "user_explicit",
        "subject_key": "project.moneywise.timezone",
        "scope": {"scope_type": "project", "scope_key": "project:moneywise"},
    }
    batch = RememberTool(agent).execute({"action": "batch", "operations": [operation]})
    assert json.loads(batch.output)["results"][0]["reason_code"] == "PROMOTED"

    replay = RememberTool(agent).execute({key: value for key, value in operation.items() if key != "action"})
    payload = json.loads(replay.output)

    assert payload["active_memory_changed"] is False
    assert payload["results"][0]["reason_code"] == "ALREADY_PROMOTED"
    candidate = agent.memory_candidates.list()[0]
    assert candidate.promotion_mode == "auto_eligible"
    assert candidate.occurrence_count == 1
    assert len(agent.memory.all()) == 1


def test_remember_replace_and_remove_autonomously_use_exact_entry_id(tmp_path):
    """replace/remove 有精确 entry_id 和用户证据时自主执行，不再等待人工审核。"""
    agent = _agent_with_current_user(tmp_path)
    first = RememberTool(agent).execute(
        {
            "content": "moneywise 项目使用 UTC 保存时间",
            "kind": "project",
            "origin": "user_explicit",
            "subject_key": "project.moneywise.timezone",
            "scope": {"scope_type": "project", "scope_key": "project:moneywise"},
        }
    )
    assert json.loads(first.output)["active_memory_changed"] is True
    entry_id = agent.memory.all()[0].entry_id

    replaced = RememberTool(agent).execute(
        {
            "action": "replace",
            "entry_id": entry_id,
            "content": "moneywise 项目使用 UTC 并支持本地化",
            "kind": "project",
            "origin": "user_explicit",
        }
    )
    replaced_payload = json.loads(replaced.output)
    assert replaced.ok
    assert replaced_payload["active_memory_changed"] is True
    assert replaced_payload["results"][0]["reason_code"] == "PROMOTED"
    assert replaced_payload["results"][0]["status"] == "promoted"

    removed = RememberTool(agent).execute(
        {
            "action": "remove",
            "entry_id": entry_id,
            "origin": "user_explicit",
        }
    )
    removed_payload = json.loads(removed.output)
    assert removed.ok
    assert removed_payload["active_memory_changed"] is True
    assert removed_payload["results"][0]["reason_code"] == "PROMOTED"
    assert removed_payload["results"][0]["status"] == "promoted"

    candidates = agent.memory_candidates.list()
    assert len(candidates) == 3
    assert {candidate.promotion_mode for candidate in candidates} == {"auto_eligible"}
    assert {candidate.status for candidate in candidates} == {"promoted"}
    assert agent.memory.all() == []


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
    # 确定性参数错误发生在任何写入之前：必须显式声明"未触发副作用"，
    # 否则 coordinator 因 handler_executed=True 误归 UNKNOWN（阻止自动重做）。
    assert result.effect_outcome == "not_started"


def test_remember_rejects_noncanonical_personal_scope_before_candidate_write(tmp_path):
    agent = _agent_with_current_user(tmp_path, "请记住本地环境使用 UTC")

    result = RememberTool(agent).execute(
        {
            "content": "本地环境使用 UTC",
            "origin": "user_explicit",
            "subject_key": "environment.timezone",
            "scope": {"scope_type": "personal", "scope_key": "local"},
        }
    )

    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert result.effect_outcome == "not_started"
    assert "个人记忆请省略 scope" in json.loads(result.output)["hint"]
    assert agent.memory_candidates.list() == []
    assert agent.memory.all() == []


def test_remember_omitted_scope_is_host_bound_to_owner_personal(tmp_path):
    """普通个人记忆不要求模型猜 owner id 或内部 scope_key。"""

    agent = _agent_with_current_user(tmp_path, "请记住我偏好先给风险再给方案")

    result = RememberTool(agent).execute(
        {
            "content": "我偏好先给风险再给方案",
            "kind": "fact",
            "origin": "user_explicit",
            "subject_key": "preference.answer_order",
        }
    )

    payload = json.loads(result.output)
    assert result.ok is True
    assert payload["active_memory_changed"] is True
    assert payload["results"][0]["memory_scope"] == {
        "scope_type": "personal",
        "scope_key": "personal",
        "automatic_recall": "same_owner_across_sessions",
        "storage_authority": "canonical_formal_memory",
    }
    assert "当前用户的个人正式记忆" in payload["hint"]
    record = agent.memory.all()[0]
    assert record.attributes["scope_type"] == "personal"
    assert record.attributes["scope_key"] == "personal"


def test_remember_session_scope_is_bound_to_current_thread(tmp_path):
    """会话记忆只声明类型，真实 scope key 由当前 thread 身份生成。"""

    agent = _agent_with_current_user(tmp_path, "请在本会话记住苍穹折页-420871")

    result = RememberTool(agent).execute(
        {
            "content": "本会话校验词是苍穹折页-420871",
            "kind": "fact",
            "origin": "user_explicit",
            "subject_key": "session.validation_word",
            "scope": {"scope_type": "session"},
        }
    )

    assert result.ok is True
    payload = json.loads(result.output)
    result_scope = payload["results"][0]["memory_scope"]
    assert result_scope["scope_type"] == "session"
    assert result_scope["scope_key"].startswith("session:thread-")
    assert result_scope["automatic_recall"] == "current_session_only"
    assert "只在当前会话自动召回" in payload["hint"]
    assert "不表示跨会话生效" in payload["hint"]
    record = agent.memory.all()[0]
    assert record.attributes["scope_type"] == "session"
    assert record.attributes["scope_key"].startswith("session:thread-")
    assert record.expires_at == 0.0


def test_remember_session_scope_rejects_model_invented_calendar_expiry(tmp_path):
    """当前会话不是“今天”；模型不能用日历时间缩短宿主拥有的 thread 生命周期。"""

    agent = _agent_with_current_user(tmp_path, "请只在本会话记住苍穹折页-420871")

    result = RememberTool(agent).execute(
        {
            "content": "本会话校验词是苍穹折页-420871",
            "kind": "fact",
            "origin": "user_explicit",
            "subject_key": "session.validation_word",
            "scope": {"scope_type": "session"},
            "valid_until": "2026-08-30T23:59:59+08:00",
        }
    )

    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"
    payload = json.loads(result.output)
    assert "当前真实会话决定" in payload["error"]
    assert "temporary scope" in payload["hint"]
    assert agent.memory_candidates.list() == []
    assert agent.memory.all() == []


def test_remember_session_scope_rejects_cross_thread_key(tmp_path):
    """模型不能用 remember 向当前 thread 以外的 session 写记忆。"""

    agent = _agent_with_current_user(tmp_path, "请在本会话记住苍穹折页-420871")

    result = RememberTool(agent).execute(
        {
            "content": "本会话校验词是苍穹折页-420871",
            "kind": "fact",
            "origin": "user_explicit",
            "subject_key": "session.validation_word",
            "scope": {"scope_type": "session", "scope_key": "session:other"},
        }
    )

    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert "当前会话记忆只写" in json.loads(result.output)["hint"]
    assert agent.memory.all() == []


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
