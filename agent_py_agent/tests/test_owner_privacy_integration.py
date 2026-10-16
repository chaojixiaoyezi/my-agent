from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.capability.memory_tool import RememberTool
from agent_py_agent.agent.capability.persona_repository import PersonaMutationRequest
from agent_py_agent.agent.capability.persona_tool import UpdatePersonaTool
from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
from agent_py_agent.tests.support.ci_host_context_fixtures import bind_im_task


def _write_skill(root: Path, name: str, description: str) -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{description}\n",
        encoding="utf-8",
    )
    return path


def test_group_runtime_never_loads_member_private_state_and_shared_stays_read_only(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    pool = OwnerScopedAgentPool(
        AgentConfig(
            model_backend="echo", my_agent_home=str(home), prompt_files=[]
        ),
        tmp_path / "service-checkout",
    )
    user_a = pool.get(OwnerIdentity.provider_user("feishu", "user-a"))
    user_b = pool.get(OwnerIdentity.provider_user("feishu", "user-b"))
    group = pool.get(OwnerIdentity.provider_group("feishu", "oc_group_1"))

    user_a.persona_repository.mutate(
        PersonaMutationRequest("user", "add", content="A 私人称呼：青竹", confirmed=True)
    )
    user_b.persona_repository.mutate(
        PersonaMutationRequest("user", "add", content="B 私人称呼：白露", confirmed=True)
    )
    from agent_py_agent.agent.user_space.group_authorization_context import group_capability_guard

    bind_im_task(group, actor_id="user-a", conversation_id="oc_group_1")
    with group_capability_guard(group, "assistant.configure"):
        group.persona_repository.mutate(
            PersonaMutationRequest("user", "add", content="群组称呼：项目组", confirmed=True)
        )
    user_a.memory.add("user", "A 私人记忆口令 alpha-only", kind="fact")
    user_b.memory.add("user", "B 私人记忆口令 beta-only", kind="fact")
    group.memory.add("user", "群组公开事实 group-only", kind="fact")

    _write_skill(user_a.home_paths.owner_home_dir / "skills", "private-a", "A 私有方法")
    _write_skill(user_b.home_paths.owner_home_dir / "skills", "private-b", "B 私有方法")
    shared_skill = _write_skill(home / "shared" / "skills", "public-review", "公共审阅方法")

    group_snapshot = group.skills_service.snapshot_for(force_reload=True)
    group_names = {entry.name for entry in group_snapshot.enabled_entries()}
    prompt = group.prompts.build(
        "回忆群组公开事实并使用公共审阅方法",
        memories=group.memory.search("群组公开事实"),
    )

    assert "public-review" in group_names
    assert "private-a" not in group_names
    assert "private-b" not in group_names
    assert "群组称呼：项目组" in prompt
    assert "群组公开事实 group-only" in prompt
    assert "青竹" not in prompt
    assert "白露" not in prompt
    assert "alpha-only" not in prompt
    assert "beta-only" not in prompt

    cross_read = execute_registry_test_call(
        group.tools,
        "read_file",
        {"path": str(user_a.home_paths.owner_user_md)},
        call_id="group-cross-owner-read",
    )
    shared_read = execute_registry_test_call(
        group.tools,
        "read_file",
        {"path": str(shared_skill)},
        call_id="group-shared-read",
        register_with=group,
    )
    shared_write = execute_registry_test_call(
        group.tools,
        "write_file",
        {"path": str(shared_skill), "content": "tampered"},
        call_id="group-shared-write",
        register_with=group,
    )

    assert not cross_read.ok
    assert cross_read.error_code == "PATH_CROSS_OWNER_BLOCKED"
    assert cross_read.failure_stage == "authorization"
    assert cross_read.handler_executed is False
    assert shared_read.ok
    assert not shared_write.ok
    assert shared_write.error_code == "WRITE_FORBIDDEN"
    assert "公共审阅方法" in shared_skill.read_text(encoding="utf-8")


def test_autonomous_memory_and_agents_updates_remain_owner_isolated(tmp_path: Path) -> None:
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.conversation.learning_source import user_dialogue_projection
    from agent_py_agent.agent.gateway_parts.agents_control_service import execute_agents_direct
    from agent_py_agent.tests.memory_confirmation_fixtures import confirm_candidate

    home = tmp_path / "home"
    pool = OwnerScopedAgentPool(
        AgentConfig(model_backend="echo", my_agent_home=str(home), prompt_files=[]),
        tmp_path / "service-checkout",
    )
    user_a = pool.get(OwnerIdentity.provider_user("feishu", "user-a"))
    user_b = pool.get(OwnerIdentity.provider_user("feishu", "user-b"))
    rules_before = {agent.home_paths.owner_id: agent.home_paths.owner_agents_md.read_bytes()
                    for agent in (user_a, user_b)}

    def bind(agent, request_id: str, content: str) -> None:
        source = user_dialogue_projection()
        thread = agent.conversation_store.threads.get_or_create(
            {
                "canonical_user_id": request_id,
                "channel": "internal",
                "channel_conversation_id": request_id,
                "channel_user_id": request_id,
                "owner_id": agent.home_paths.owner_id,
                "owner_home": str(agent.home_paths.owner_home_dir),
                "learning_source": source,
            }
        )
        agent.conversation_store.messages.append(
            {
                "thread_id": thread.thread_id,
                "role": "user",
                "content": content,
                "metadata": {"gateway_request_id": request_id, "learning_source": source},
            }
        )
        agent._current_run_params = SimpleNamespace(
            request_id=request_id,
            run_id=f"run-{request_id}",
            task_id=f"task-{request_id}",
            task_attributes={"conversation_thread_id": thread.thread_id, "learning_source": source},
        )

    bind(user_a, "owner-a", "请记住 A 的项目代号是 alpha-only")
    bind(user_b, "owner-b", "请记住 B 的项目代号是 beta-only")
    result_a = RememberTool(user_a).execute(
        {
            "content": "A 的项目代号是 alpha-only",
            "kind": "project",
            "origin": "user_explicit",
            "subject_key": "project.a.code",
            "scope": {"scope_type": "project", "scope_key": "project:a"},
        }
    )
    result_b = RememberTool(user_b).execute(
        {
            "content": "B 的项目代号是 beta-only",
            "kind": "project",
            "origin": "user_explicit",
            "subject_key": "project.b.code",
            "scope": {"scope_type": "project", "scope_key": "project:b"},
        }
    )
    for agent, result in ((user_a, result_a), (user_b, result_b)):
        payload = json.loads(result.output)
        assert payload["active_memory_changed"] is False
        assert payload["results"][0]["reason_code"] == "AUTO_PROMOTION_CONFIRMATION_REQUIRED"
        [candidate] = agent.memory_candidates.list()
        assert candidate.status == "pending_review" and candidate.learning_source["can_learn"] is True
        automatic = agent.memory_promotion.promote(candidate.candidate_id, automatic=True, confirmed=True)
        assert not automatic.promoted and automatic.reason_code == "AUTO_PROMOTION_CONFIRMATION_REQUIRED"
        unreviewed = agent.memory_promotion.promote(candidate.candidate_id, confirmed=True)
        assert not unreviewed.promoted and unreviewed.reason_code == "REVIEW_REQUIRED"
        assert agent.memory.all() == []
        denied = UpdatePersonaTool(agent).execute(
            {"target": "agents", "content": "模型不能替用户确认工作约定", "confirmed": True}
        )
        assert not denied.ok
        assert denied.reported_error_code == "AGENTS_WRITE_USER_ONLY"
        assert agent.home_paths.owner_agents_md.read_bytes() == rules_before[agent.home_paths.owner_id]

    [candidate_a], [candidate_b] = user_a.memory_candidates.list(), user_b.memory_candidates.list()
    assert candidate_a.candidate_id != candidate_b.candidate_id
    assert confirm_candidate(user_a.memory_promotion, candidate_a.candidate_id).promoted
    assert user_b.memory.all() == [], "A 的用户确认不能写入 B 的正式记忆"
    assert confirm_candidate(user_b.memory_promotion, candidate_b.candidate_id).promoted

    denied = execute_agents_direct(user_a, parse_conversation_control(
        f'/agents owner {user_b.home_paths.owner_id} write "不得跨 owner 写入"'), "privacy-a", "cross-owner")
    assert not denied.ok and denied.error_code == "AGENTS_WRITE_CROSS_OWNER_FORBIDDEN"
    for agent, label, project in ((user_a, "A", "alpha"), (user_b, "B", "beta")):
        # 原用户direct控制先预览再确认；与模型工具的confirmed字段分开，仍过原owner/CAS和来源保护。
        content = f"{label} 的工作约定只服务 {project} 项目"
        session_id = f"privacy-{label}"
        preview = execute_agents_direct(agent, parse_conversation_control(
            "/agents write " + json.dumps(content, ensure_ascii=False)), session_id, f"preview-{label}")
        assert preview.ok and preview.control_state == "preview", preview
        assert agent.home_paths.owner_agents_md.read_bytes() == rules_before[agent.home_paths.owner_id]
        confirmed = execute_agents_direct(agent, parse_conversation_control(
            preview.details["confirm_command"]), session_id, f"confirm-{label}")
        assert confirmed.ok and confirmed.details["state"] == "completed", confirmed
        assert agent.persona_repository.load("agents").content == content

    assert [item.content for item in user_a.memory.all()] == ["A 的项目代号是 alpha-only"]
    assert [item.content for item in user_b.memory.all()] == ["B 的项目代号是 beta-only"]
    assert "beta-only" not in user_a.persona_repository.load("agents").content
    assert "alpha-only" not in user_b.persona_repository.load("agents").content
    assert user_a.home_paths.owner_memory_candidates_jsonl != (
        user_b.home_paths.owner_memory_candidates_jsonl
    )
