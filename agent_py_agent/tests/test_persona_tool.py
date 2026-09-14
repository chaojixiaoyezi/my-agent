"""update_persona 工具钉子:用户表达长期人设/画像/称呼时,模型惯性走 remember 塞进 memory
(每轮不注入=不生效)。补 UpdatePersonaTool 后:直接写 owner 的 SOUL/USER/AGENTS.md(每轮注入)。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.persona_repository import (
    PersonaMutationRequest,
    PersonaRepository,
)
from agent_py_agent.agent.capability.persona_tool import UpdatePersonaTool


@pytest.fixture(autouse=True)
def _reset_persona_rate_limit():
    from agent_py_agent.agent.capability.persona_tool import _reset_persona_update_rate_limit
    _reset_persona_update_rate_limit()
    yield
    _reset_persona_update_rate_limit()
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


def test_update_persona_soul_uses_exact_approval_binding(tmp_path):
    agent, soul, _user, agents = _agent_with_paths(tmp_path)
    tool = UpdatePersonaTool(agent)
    arguments = {"target": "soul", "content": "语气偏活泼"}
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
        "approval_id": "approval-soul",
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
    assert "产物用 HTML" not in agents.read_text(encoding="utf-8")


def test_update_persona_agents_is_autonomous_without_approval(tmp_path):
    agent, _soul, _user, agents = _agent_with_paths(tmp_path)

    execution = execute_canonical_test_call(
        tmp_path,
        tools={"update_persona": UpdatePersonaTool(agent)},
        tool_name="update_persona",
        arguments={"target": "agents", "content": "产物用 HTML"},
    )

    assert execution.result.ok is True
    assert execution.result.handler_executed is True
    assert execution.decision.resolved_effect == "mutating"
    assert "产物用 HTML" in agents.read_text(encoding="utf-8")


def test_update_persona_soul_read_is_read_only_without_approval(tmp_path):
    agent, _soul, _user, _agents = _agent_with_paths(tmp_path)

    execution = execute_canonical_test_call(
        tmp_path,
        tools={"update_persona": UpdatePersonaTool(agent)},
        tool_name="update_persona",
        arguments={"action": "list", "target": "soul"},
    )

    assert execution.result.ok is True
    assert execution.result.handler_executed is True
    assert execution.decision.resolved_effect == "read_only"


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


@pytest.mark.parametrize("arguments,code", [
    ({"action": "replace", "entry_id": "missing", "content": "报告先列问题"}, "PERSONA_ENTRY_NOT_FOUND"),
    ({"action": "rollback", "rollback_version": 99}, "PERSONA_ENTRY_NOT_FOUND"),
    ({"action": "add", "content": "报告先列问题", "expected_sha256": "stale"}, "PERSONA_VERSION_CONFLICT"),
    ({"operations": [{"action": "add", "content": "日期用年月日"},
                     {"action": "remove", "entry_id": "missing"}]}, "PERSONA_ENTRY_NOT_FOUND"),
])
def test_persona_precommit_rejection_stays_failed_through_operation_store(tmp_path, arguments, code):
    from agent_py_agent.agent.local_storage import LocalStore

    agent, _soul, user, _agents = _agent_with_paths(tmp_path)
    before = user.read_bytes()
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    result = execute_canonical_test_call(
        tmp_path, tools={"update_persona": UpdatePersonaTool(agent)},
        tool_name="update_persona", arguments={"target": "user", **arguments},
        operation_store=store,
    ).result

    assert result.error_code == code
    assert result.effect_outcome == "not_started"
    assert result.failure_stage == "validation"
    assert result.handler_executed is True
    assert result.operation.status == "failed"
    assert user.read_bytes() == before
    assert PersonaRepository.from_home_paths(agent.home_paths).history("user") == []


def test_persona_write_then_error_remains_unknown(tmp_path, monkeypatch):
    from agent_py_agent.agent.local_storage import LocalStore

    agent, _soul, user, _agents = _agent_with_paths(tmp_path)
    repository = PersonaRepository.from_home_paths(agent.home_paths)
    agent.persona_repository = repository

    def interrupted_commit(request):
        user.write_text("partially committed", encoding="utf-8")
        raise OSError("audit persistence interrupted")

    monkeypatch.setattr(repository, "mutate", interrupted_commit)
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    result = execute_canonical_test_call(
        tmp_path, tools={"update_persona": UpdatePersonaTool(agent)},
        tool_name="update_persona", arguments={"target": "user", "content": "报告先列问题"},
        operation_store=store,
    ).result

    assert result.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert result.effect_outcome == "unknown"
    assert result.operation.status == "unknown"
    assert user.read_text(encoding="utf-8") == "partially committed"


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


def test_update_persona_rolls_first_change_back_to_version_zero(tmp_path):
    agent, _soul, user, _agents = _agent_with_paths(tmp_path)
    tool = UpdatePersonaTool(agent)
    original = user.read_text(encoding="utf-8")

    added = tool.execute({"target": "user", "content": "沟通偏好:先给结论"})
    history = tool.execute({"action": "history", "target": "user"})
    rolled_back = tool.execute(
        {"action": "rollback", "target": "user", "rollback_version": 0}
    )

    assert added.ok is True
    assert [row["version"] for row in json.loads(history.output)["versions"]] == [0, 1]
    assert rolled_back.ok is True
    assert json.loads(rolled_back.output)["version"] == 2
    assert user.read_text(encoding="utf-8") == original


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
    agent._current_user_prompt = "以后请叫我小明，回答时尽量简洁"

    result = UpdatePersonaTool(agent).execute(
        {
            "action": "batch",
            "target": "user",
            "operations": [
                {
                    "action": "add",
                    "content": "称呼:小明",
                    "source_quote": "以后请叫我小明",
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
    assert "- 称呼:小明" in content
    assert "- 回答偏好:尽量简洁" in content
    assert content.count("称呼:小明") == 1


def test_update_persona_batch_action_requires_operations(tmp_path):
    agent, *_ = _agent_with_paths(tmp_path)

    result = UpdatePersonaTool(agent).execute({"action": "batch", "target": "user"})

    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"


def test_update_persona_batch_does_not_require_natural_language_quotes(tmp_path):
    agent, _soul, user, _agents = _agent_with_paths(tmp_path)
    agent._current_user_prompt = "以后请叫我小明"
    result = UpdatePersonaTool(agent).execute(
        {
            "target": "user",
            "operations": [
                {
                    "action": "add",
                    "content": "称呼:小明",
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
    assert "称呼:小明" in content
    assert "回答偏好:尽量简洁" in content


def test_update_persona_rate_limit_is_isolated_per_owner(tmp_path):
    (tmp_path / "owner-a").mkdir()
    (tmp_path / "owner-b").mkdir()
    owner_a, _soul_a, user_a, _agents_a = _agent_with_paths(tmp_path / "owner-a")
    owner_b, _soul_b, user_b, _agents_b = _agent_with_paths(tmp_path / "owner-b")
    tool_a = UpdatePersonaTool(owner_a)
    tool_b = UpdatePersonaTool(owner_b)

    for index in range(3):
        result = tool_a.execute(
            {"target": "user", "content": f"A用户偏好{index}:值{index}"}
        )
        assert result.ok is True

    other_owner = tool_b.execute(
        {"target": "user", "content": "B用户偏好:不受A用户限频影响"}
    )
    limited = tool_a.execute({"target": "user", "content": "A用户偏好4:应被限频"})

    assert other_owner.ok is True
    assert "B用户偏好:不受A用户限频影响" in user_b.read_text(encoding="utf-8")
    assert limited.ok is False
    assert limited.error_code == "PERSONA_UPDATE_RATE_LIMITED"
    assert limited.effect_outcome == "not_started"
    assert "A用户偏好4:应被限频" not in user_a.read_text(encoding="utf-8")


def test_update_persona_rate_limit_is_terminal_not_unknown(tmp_path):
    agent, _soul, user, _agents = _agent_with_paths(tmp_path)
    tool = UpdatePersonaTool(agent)
    for index in range(3):
        assert tool.execute(
            {"target": "user", "content": f"沟通偏好{index}:值{index}"}
        ).ok

    execution = execute_canonical_test_call(
        tmp_path,
        tools={"update_persona": tool},
        tool_name="update_persona",
        arguments={"target": "user", "content": "沟通偏好4:应被限频"},
    )

    assert execution.result.ok is False
    assert execution.result.error_code == "PERSONA_UPDATE_RATE_LIMITED"
    assert execution.result.effect_outcome == "not_started"
    assert execution.result.status == "failed"
    assert "沟通偏好4:应被限频" not in user.read_text(encoding="utf-8")


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
