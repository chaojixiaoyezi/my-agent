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
    assert agent.memory.all() == []


def test_remember_batch_adds_never_auto_promote(tmp_path):
    """矩阵2:batch 两条 add → 全部 manual_required,正式长期记忆 0 写入。"""
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
    assert payload["active_memory_changed"] is False
    assert [item["reason_code"] for item in payload["results"]] == [
        "REVIEW_REQUIRED",
        "REVIEW_REQUIRED",
    ]
    candidates = agent.memory_candidates.list()
    assert len(candidates) == 2
    assert {candidate.promotion_mode for candidate in candidates} == {"manual_required"}
    assert {candidate.status for candidate in candidates} == {"pending_review"}
    assert agent.memory.all() == []


def test_remember_batch_single_add_is_still_manual(tmp_path):
    """矩阵2边界:batch 即使只含一条 add 也一律 manual_required,不自动晋升。"""
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
    assert payload["active_memory_changed"] is False
    assert payload["results"][0]["reason_code"] == "REVIEW_REQUIRED"
    candidate = agent.memory_candidates.list()[0]
    assert candidate.promotion_mode == "manual_required"
    assert candidate.status == "pending_review"
    assert agent.memory.all() == []


def test_remember_manual_absorption_blocks_later_single_add_replay(tmp_path):
    """持久权限是事实源：同一候选先经 batch 变 manual，单条 add 重放不得重新自动晋升。"""
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
    assert json.loads(batch.output)["results"][0]["reason_code"] == "REVIEW_REQUIRED"

    replay = RememberTool(agent).execute({key: value for key, value in operation.items() if key != "action"})
    payload = json.loads(replay.output)

    assert payload["active_memory_changed"] is False
    assert payload["results"][0]["reason_code"] == "REVIEW_REQUIRED"
    candidate = agent.memory_candidates.list()[0]
    assert candidate.promotion_mode == "manual_required"
    assert candidate.occurrence_count == 1
    assert agent.memory.all() == []


def test_remember_replace_and_remove_never_auto_execute(tmp_path):
    """矩阵3:replace/remove 只形成 manual_required 候选,不自动执行正式变更。"""
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
    assert replaced_payload["active_memory_changed"] is False
    assert replaced_payload["results"][0]["reason_code"] == "REVIEW_REQUIRED"
    assert replaced_payload["results"][0]["status"] == "pending_review"

    removed = RememberTool(agent).execute(
        {
            "action": "remove",
            "entry_id": entry_id,
            "origin": "user_explicit",
        }
    )
    removed_payload = json.loads(removed.output)
    assert removed.ok
    assert removed_payload["active_memory_changed"] is False
    assert removed_payload["results"][0]["reason_code"] == "REVIEW_REQUIRED"
    assert removed_payload["results"][0]["status"] == "pending_review"

    candidates = agent.memory_candidates.list()
    assert len(candidates) == 3
    assert {candidate.promotion_mode for candidate in candidates} == {
        "auto_eligible",
        "manual_required",
    }
    manual = [candidate for candidate in candidates if candidate.promotion_mode == "manual_required"]
    assert len(manual) == 2
    assert {candidate.status for candidate in manual} == {"pending_review"}
    # 正式记忆保持 1 条且内容未被 replace/remove 触碰。
    formal = agent.memory.all()
    assert len(formal) == 1
    assert formal[0].content == "moneywise 项目使用 UTC 保存时间"


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
    assert agent.memory_candidates.list() == []
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
