"""update_persona 工具钉子:用户表达长期人设/画像/称呼时,模型惯性走 remember 塞进 memory
(每轮不注入=不生效)。补 UpdatePersonaTool 后:直接写 owner 的 SOUL/USER/AGENTS.md(每轮注入)。"""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.capability.persona_repository import (
    PersonaMutationRequest,
    PersonaRepository,
)
from agent_py_agent.agent.capability.persona_tool import UpdatePersonaTool
from agent_py_agent.agent.conversation.authority import CONVERSATION_AUDIT_PREPARE_ATTR
from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call


def _agent_with_paths(tmp_path):
    soul, user, agents = tmp_path / "SOUL.md", tmp_path / "USER.md", tmp_path / "AGENTS.md"
    for p, head in (
        (soul, "# SOUL\n"),
        (user, "# USER\n\n## 画像\n- 称呼:\n"),
        (agents, "# AGENTS\n"),
    ):
        p.write_text(head, encoding="utf-8")
    home = SimpleNamespace(owner_soul_md=soul, owner_user_md=user, owner_agents_md=agents)
    return SimpleNamespace(home_paths=home), soul, user, agents


def test_update_persona_writes_user_file(tmp_path):
    agent, _soul, user, _agents = _agent_with_paths(tmp_path)
    result = UpdatePersonaTool(agent).execute({"target": "user", "content": "称呼:小王"})
    assert result.ok
    assert "称呼:小王" in user.read_text(encoding="utf-8")


def test_update_persona_is_not_exposed_in_named_audit_prepare(tmp_path):
    agent, _soul, _user, _agents = _agent_with_paths(tmp_path)
    agent._current_run_params = SimpleNamespace(
        task_attributes={CONVERSATION_AUDIT_PREPARE_ATTR: True}
    )

    availability = UpdatePersonaTool(agent).availability()

    assert availability.available is False
    assert "task-scoped" in availability.reason


def test_update_persona_targets_soul_and_agents_use_exact_approval_binding(tmp_path):
    agent, soul, _user, agents = _agent_with_paths(tmp_path)
    for target, content in (("soul", "语气偏活泼"), ("agents", "产物用 HTML")):
        tool = UpdatePersonaTool(agent)
        arguments = {"target": target, "content": content}
        first = execute_canonical_test_call(
            tmp_path,
            tools={"update_persona": tool},
            tool_name="update_persona",
            arguments=arguments,
        )
        assert first.result.status == "approval_required"
        assert first.result.handler_executed is False
        binding = {
            **dict(first.decision.approval_request or {}),
            "approval_id": f"approval-{target}",
            "status": "APPROVED",
        }
        approved = execute_canonical_test_call(
            tmp_path,
            tools={"update_persona": tool},
            tool_name="update_persona",
            arguments=arguments,
            write_boundary={"approved_actions": [binding]},
        )
        assert approved.result.ok is True
        assert approved.result.handler_executed is True
    assert "语气偏活泼" in soul.read_text(encoding="utf-8")
    assert "产物用 HTML" in agents.read_text(encoding="utf-8")


def test_update_persona_soul_agents_cannot_use_model_confirmed_flag(tmp_path):
    agent, soul, _user, agents = _agent_with_paths(tmp_path)
    for target in ("soul", "agents"):
        execution = execute_canonical_test_call(
            tmp_path,
            tools={"update_persona": UpdatePersonaTool(agent)},
            tool_name="update_persona",
            arguments={"target": target, "content": "别乱写", "confirmed": True},
        )
        assert execution.result.ok is False
        assert execution.result.error_code == "TOOL_INVALID_ARGUMENTS"
        assert execution.result.handler_executed is False
    assert "别乱写" not in soul.read_text(encoding="utf-8")
    assert "别乱写" not in agents.read_text(encoding="utf-8")


def test_update_persona_user_no_confirm_needed(tmp_path):
    # USER(用户画像)不受确认限制,直接写
    agent, _soul, user, _agents = _agent_with_paths(tmp_path)
    agent._current_user_prompt = "我是做电商的"
    assert (
        UpdatePersonaTool(agent)
        .execute({"target": "user", "content": "角色:电商", "source_quote": "我是做电商的"})
        .ok
    )
    assert "角色:电商" in user.read_text(encoding="utf-8")


def test_update_persona_idempotent(tmp_path):
    agent, _soul, user, _agents = _agent_with_paths(tmp_path)
    agent._current_user_prompt = "以后叫我小王"
    params = {"target": "user", "content": "称呼:小王", "source_quote": "以后叫我小王"}
    UpdatePersonaTool(agent).execute(params)
    UpdatePersonaTool(agent).execute(params)
    assert user.read_text(encoding="utf-8").count("称呼:小王") == 1


def test_update_persona_lists_replaces_and_removes_by_entry_id(tmp_path):
    agent, _soul, user, _agents = _agent_with_paths(tmp_path)
    tool = UpdatePersonaTool(agent)
    agent._current_user_prompt = "以后和我沟通要结论先行"
    added = tool.execute(
        {
            "target": "user",
            "content": "沟通偏好:结论先行",
            "source_quote": "以后和我沟通要结论先行",
        }
    )
    assert added.ok
    entry_id = json.loads(added.output)["entry_id"]

    listed = tool.execute({"action": "list", "target": "user"})
    assert listed.ok
    entries = json.loads(listed.output)["entries"]
    assert {row["entry_id"] for row in entries} >= {entry_id}

    agent._current_user_prompt = "请改成先给结论，再给依据"
    replaced = tool.execute(
        {
            "action": "replace",
            "target": "user",
            "entry_id": entry_id,
            "content": "沟通偏好:先给结论，再给依据",
            "source_quote": "请改成先给结论，再给依据",
        }
    )
    assert replaced.ok
    replacement_id = json.loads(replaced.output)["entry_id"]
    assert "沟通偏好:结论先行" not in user.read_text(encoding="utf-8")
    assert "沟通偏好:先给结论，再给依据" in user.read_text(encoding="utf-8")

    agent._current_user_prompt = "删除先给结论，再给依据这个偏好"
    removed = tool.execute(
        {
            "action": "remove",
            "target": "user",
            "entry_id": replacement_id,
            "source_quote": "删除先给结论，再给依据这个偏好",
        }
    )
    assert removed.ok
    assert "沟通偏好:先给结论，再给依据" not in user.read_text(encoding="utf-8")


def test_update_persona_missing_entry_does_not_claim_success(tmp_path):
    agent, *_ = _agent_with_paths(tmp_path)
    agent._current_user_prompt = "删除不存在的画像"
    result = UpdatePersonaTool(agent).execute(
        {
            "action": "remove",
            "target": "user",
            "entry_id": "persona-does-not-exist",
            "source_quote": "删除不存在的画像",
        }
    )
    assert result.ok is False
    assert result.error_code == "PERSONA_ENTRY_NOT_FOUND"


def test_update_persona_invalid_target(tmp_path):
    agent, *_ = _agent_with_paths(tmp_path)
    result = UpdatePersonaTool(agent).execute({"target": "memory", "content": "x"})
    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"


def test_update_persona_missing_content(tmp_path):
    agent, *_ = _agent_with_paths(tmp_path)
    result = UpdatePersonaTool(agent).execute({"target": "user", "content": "  "})
    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"


def test_update_persona_rejects_invalid_rollback_version_at_tool_boundary(tmp_path):
    agent, *_ = _agent_with_paths(tmp_path)
    result = UpdatePersonaTool(agent).execute(
        {"action": "rollback", "target": "user", "rollback_version": "not-an-integer"}
    )
    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"


def test_update_persona_accepts_concise_paraphrase_with_exact_user_quote(tmp_path):
    agent, _soul, user, _agents = _agent_with_paths(tmp_path)
    agent._current_user_prompt = "以后每次回答我时都先给一句摘要"
    result = UpdatePersonaTool(agent).execute(
        {
            "target": "user",
            "content": "输出偏好:先给摘要",
            "source_quote": "以后每次回答我时都先给一句摘要",
        }
    )
    assert result.ok is True
    assert "输出偏好:先给摘要" in user.read_text(encoding="utf-8")


def test_update_persona_treats_source_quote_as_audit_only(tmp_path):
    agent, _soul, user, _agents = _agent_with_paths(tmp_path)
    agent._current_user_prompt = "记住，我喜欢青柠味"
    result = UpdatePersonaTool(agent).execute(
        {
            "target": "user",
            "content": "称呼:小王",
            "source_quote": "以后请叫我小王",
        }
    )
    assert result.ok is True
    assert "小王" in user.read_text(encoding="utf-8")


def test_update_persona_rejects_multiple_user_facts_in_one_call(tmp_path):
    agent, _soul, user, _agents = _agent_with_paths(tmp_path)
    agent._current_user_prompt = "我喜欢青柠味"
    result = UpdatePersonaTool(agent).execute(
        {
            "target": "user",
            "content": "称呼:小王\n偏好:喜欢青柠味",
            "source_quote": "我喜欢青柠味",
        }
    )
    assert result.ok is False
    assert result.error_code == "PERSONA_CONTENT_NOT_ATOMIC"
    assert "青柠味" not in user.read_text(encoding="utf-8")


def test_update_persona_batch_writes_all_grounded_user_facts_atomically(tmp_path):
    agent, _soul, user, _agents = _agent_with_paths(tmp_path)
    agent._current_user_prompt = "以后请叫我青禾，回答时尽量简洁"

    result = UpdatePersonaTool(agent).execute(
        {
            "action": "batch",
            "target": "user",
            "operations": [
                {
                    "action": "add",
                    "content": "称呼:青禾",
                    "source_quote": "以后请叫我青禾",
                },
                {
                    "action": "add",
                    "content": "回答偏好:尽量简洁",
                    "source_quote": "回答时尽量简洁",
                },
            ],
        }
    )

    assert result.ok
    payload = json.loads(result.output)
    assert payload["action"] == "batch"
    assert len(payload["operations"]) == 2
    content = user.read_text(encoding="utf-8")
    assert "- 称呼:青禾" in content
    assert "- 回答偏好:尽量简洁" in content
    assert content.count("称呼:青禾") == 1


def test_update_persona_batch_action_requires_operations(tmp_path):
    agent, *_ = _agent_with_paths(tmp_path)

    result = UpdatePersonaTool(agent).execute({"action": "batch", "target": "user"})

    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"


def test_update_persona_batch_does_not_require_natural_language_quotes(tmp_path):
    agent, _soul, user, _agents = _agent_with_paths(tmp_path)
    agent._current_user_prompt = "以后请叫我青禾"
    result = UpdatePersonaTool(agent).execute(
        {
            "target": "user",
            "operations": [
                {
                    "action": "add",
                    "content": "称呼:青禾",
                },
                {
                    "action": "add",
                    "content": "回答偏好:尽量简洁",
                },
            ],
        }
    )

    assert result.ok is True
    content = user.read_text(encoding="utf-8")
    assert "称呼:青禾" in content
    assert "回答偏好:尽量简洁" in content


def test_append_persona_line_no_trailing_newline(tmp_path):
    p = tmp_path / "x.md"
    p.write_text("# USER", encoding="utf-8")  # 无末尾换行
    repository = PersonaRepository(
        owner_home=tmp_path,
        soul_path=tmp_path / "SOUL.md",
        user_path=p,
        agents_path=tmp_path / "AGENTS.md",
    )
    repository.mutate(
        PersonaMutationRequest(
            target="user",
            action="add",
            content="称呼:小李",
            confirmed=True,
            source="test",
        )
    )
    txt = p.read_text(encoding="utf-8")
    assert txt == "# USER\n- 称呼:小李\n"


def test_update_persona_registered_in_agent_toolset(tmp_path):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.defaults import default_agent_config

    agent = SimpleAgent(default_agent_config(), tmp_path)
    names = [getattr(s, "name", "") for s in agent.tools.specs()]
    assert "update_persona" in names
