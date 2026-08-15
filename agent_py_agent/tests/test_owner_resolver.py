from __future__ import annotations

import json
from pathlib import Path


def test_resolve_local_main_owner_uses_v2_owner_home(tmp_path: Path):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home

    result = resolve_owner_home(tmp_path, OwnerIdentity.local_main())

    assert result.owner_id == "local/main"
    assert result.home_dir == tmp_path / "owners" / "local" / "main"
    assert result.daily_memory_dir == result.home_dir / "memory" / "daily"
    assert result.tasks_dir == result.home_dir / "tasks"
    assert result.scheduler_dir == result.home_dir / "data" / "scheduler"


def test_resolve_provider_user_owner_uses_v2_provider_home(tmp_path: Path):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home

    result = resolve_owner_home(tmp_path, OwnerIdentity.provider_user("feishu", "ou_123"))

    assert result.owner_id == "providers/feishu/users/ou_123"
    assert result.home_dir == tmp_path / "owners" / "providers" / "feishu" / "users" / "ou_123"
    assert result.identity.provider == "feishu"
    assert result.identity.owner_kind == "user"
    assert result.scheduler_store_json == result.home_dir / "data" / "scheduler" / "store.json"


def test_ensure_owner_home_creates_owner_seed_files(tmp_path: Path):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home

    result = ensure_owner_home(tmp_path, OwnerIdentity.provider_group("feishu", "oc_abc"))

    permissions = json.loads(result.permissions_json.read_text(encoding="utf-8"))
    assert result.agents_md.exists()
    assert result.memory_md.exists()
    assert result.skill_policy_json.exists()
    assert result.tool_policy_json.exists()
    assert result.scheduler_dir.is_dir()
    assert permissions["filesystem"]["access_mode"] == "workspace-write"


def test_prompt_home_context_reads_owner_entry_files(tmp_path: Path):
    from agent_py_agent.agent.prompting_parts.builder import _persona_home_context_chunks
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    paths = ensure_my_agent_home(tmp_path)
    paths.agents_md.write_text("current agents\n", encoding="utf-8")
    paths.owner_agents_md.write_text("owner agents\n", encoding="utf-8")
    paths.owner_soul_md.write_text("owner soul\n", encoding="utf-8")
    paths.owner_user_md.write_text("owner user\n", encoding="utf-8")
    paths.owner_memory_md.write_text("raw memory must not enter prompt\n", encoding="utf-8")
    paths.owner_memory_hot_md.write_text("raw hot must not enter prompt\n", encoding="utf-8")
    (paths.owner_memory_lessons_dir / "raw.md").write_text(
        "raw lesson must not enter prompt\n",
        encoding="utf-8",
    )

    rendered = "\n".join(_persona_home_context_chunks(paths))

    assert "owner agents" in rendered
    assert "owner soul" in rendered
    assert "owner user" in rendered
    assert "current agents" not in rendered
    assert "raw memory must not enter prompt" not in rendered
    assert "raw hot must not enter prompt" not in rendered
    assert "raw lesson must not enter prompt" not in rendered


def test_simple_agent_long_term_write_does_not_create_daily_mirror(tmp_path: Path):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.config import AgentConfig

    home = tmp_path / "home"
    agent = SimpleAgent(AgentConfig(my_agent_home=str(home), prompt_files=[]), tmp_path / "workspace")

    agent.memory.add("user", "只写正式长期记忆", kind="note")

    owner_daily = list((home / "owners" / "local" / "main" / "memory" / "daily").glob("*.jsonl"))
    previous_daily = list((home / "memory" / "daily").glob("*.jsonl"))
    assert owner_daily == []
    assert previous_daily == []


def test_simple_agent_uses_configured_provider_owner_home(tmp_path: Path):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.config import AgentConfig

    home = tmp_path / "home"
    agent = SimpleAgent(
        AgentConfig(
            my_agent_home=str(home),
            my_agent_owner_provider="feishu",
            my_agent_owner_kind="user",
            my_agent_owner_id="ou_123",
            prompt_files=[],
        ),
        tmp_path / "workspace",
    )

    agent.memory.add("user", "记录飞书用户长期事实", kind="note")

    provider_daily = list((home / "owners" / "providers" / "feishu" / "users" / "ou_123" / "memory" / "daily").glob("*.jsonl"))
    local_daily = list((home / "owners" / "local" / "main" / "memory" / "daily").glob("*.jsonl"))
    assert agent.home_paths.owner_home_dir == home / "owners" / "providers" / "feishu" / "users" / "ou_123"
    runtime_root = agent.home_paths.owner_home_dir / "workspace" / "runtime" / "workspaces"
    assert agent.local_store.db_path.is_relative_to(runtime_root)
    assert agent.local_store.db_path.name == "local.db"
    assert agent.subagents.workspace.is_relative_to(runtime_root)
    assert agent.subagents.workspace.name == "subagents"
    assert agent.conversation_store.root.is_relative_to(runtime_root)
    assert agent.conversation_store.root.name == "conversations"
    assert agent.collaboration_store.root.is_relative_to(runtime_root)
    assert agent.collaboration_store.root.name == "collaboration"
    assert not provider_daily
    assert not local_daily


def test_simple_agent_active_runtime_paths_use_owner_home_for_fresh_install(tmp_path: Path):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.config import AgentConfig

    repo = tmp_path / "repo"
    home = tmp_path / "home"

    agent = SimpleAgent(AgentConfig(my_agent_home=str(home), prompt_files=[]), repo)

    owner_home = home / "owners" / "local" / "main"
    runtime_root = owner_home / "workspace" / "runtime" / "workspaces"
    assert agent.local_store.db_path.is_relative_to(runtime_root)
    assert agent.local_store.db_path.name == "local.db"
    assert agent.local_store.files_dir == agent.local_store.db_path.parent / "files"
    assert agent.local_store.events_path == agent.local_store.db_path.parent / "events.jsonl"
    assert agent.subagents.workspace.is_relative_to(runtime_root)
    assert agent.subagents.workspace.name == "subagents"
    assert agent.conversation_store.root.is_relative_to(runtime_root)
    assert agent.conversation_store.root.name == "conversations"
    assert agent.collaboration_store.root.is_relative_to(runtime_root)
    assert agent.collaboration_store.root.name == "collaboration"
    assert str(agent.memory.path).startswith(str(owner_home / "memory" / "long_term"))
    assert not (repo / "data").exists()


def test_simple_agent_always_uses_owner_home_runtime(tmp_path: Path):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.config import AgentConfig

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"

    agent = SimpleAgent(AgentConfig(my_agent_home=str(home), prompt_files=[]), workspace)

    owner_home = home / "owners" / "local" / "main"
    assert agent.runtime_path_resolution.reason == "owner_home_runtime"
    assert agent.local_store.db_path.is_relative_to(owner_home / "workspace" / "runtime" / "workspaces")
    assert not (workspace / "data").exists()
