from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.capability.persona_repository import PersonaMutationRequest
from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call


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
            tool_protocol="text", model_backend="echo", my_agent_home=str(home), prompt_files=[]
        ),
        tmp_path / "service-checkout",
    )
    user_a = pool.get(OwnerIdentity.provider_user("feishu", "user-a"))
    user_b = pool.get(OwnerIdentity.provider_user("feishu", "user-b"))
    group = pool.get(OwnerIdentity.provider_group("feishu", "group-1"))

    user_a.persona_repository.mutate(
        PersonaMutationRequest("user", "add", content="A 私人称呼：青竹", confirmed=True)
    )
    user_b.persona_repository.mutate(
        PersonaMutationRequest("user", "add", content="B 私人称呼：白露", confirmed=True)
    )
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
    )
    shared_write = execute_registry_test_call(
        group.tools,
        "write_file",
        {"path": str(shared_skill), "content": "tampered"},
        call_id="group-shared-write",
    )

    assert not cross_read.ok
    assert cross_read.error_code == "PATH_CROSS_OWNER_BLOCKED"
    assert cross_read.failure_stage == "authorization"
    assert cross_read.handler_executed is False
    assert shared_read.ok
    assert not shared_write.ok
    assert shared_write.error_code == "WRITE_FORBIDDEN"
    assert "公共审阅方法" in shared_skill.read_text(encoding="utf-8")
