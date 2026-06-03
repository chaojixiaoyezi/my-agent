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


def test_resolve_provider_user_owner_uses_v2_provider_home(tmp_path: Path):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home

    result = resolve_owner_home(tmp_path, OwnerIdentity.provider_user("feishu", "ou_123"))

    assert result.owner_id == "providers/feishu/users/ou_123"
    assert result.home_dir == tmp_path / "owners" / "providers" / "feishu" / "users" / "ou_123"
    assert result.identity.provider == "feishu"
    assert result.identity.owner_kind == "user"


def test_ensure_owner_home_creates_owner_seed_files(tmp_path: Path):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home

    result = ensure_owner_home(tmp_path, OwnerIdentity.provider_group("feishu", "oc_abc"))

    permissions = json.loads(result.permissions_json.read_text(encoding="utf-8"))
    assert result.agents_md.exists()
    assert result.memory_md.exists()
    assert result.skill_policy_json.exists()
    assert result.tool_policy_json.exists()
    assert permissions["filesystem"]["access_mode"] == "workspace-write"


def test_prompt_home_context_reads_owner_entry_files(tmp_path: Path):
    from agent_py_agent.agent.prompting_parts.builder import _home_entry_context_chunks
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    paths = ensure_my_agent_home(tmp_path)
    paths.agents_md.write_text("legacy agents\n", encoding="utf-8")
    paths.owner_agents_md.write_text("owner agents\n", encoding="utf-8")

    rendered = "\n".join(_home_entry_context_chunks(paths))

    assert "owner agents" in rendered
    assert "legacy agents" not in rendered


def test_simple_agent_daily_memory_mirror_uses_owner_home(tmp_path: Path):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.config import AgentConfig

    home = tmp_path / "home"
    agent = SimpleAgent(AgentConfig(my_agent_home=str(home), prompt_files=[]), tmp_path / "workspace")

    agent.memory.add("user", "记录 owner daily", kind="note")

    owner_daily = list((home / "owners" / "local" / "main" / "memory" / "daily").glob("*.jsonl"))
    legacy_daily = list((home / "memory" / "daily").glob("*.jsonl"))
    assert owner_daily
    assert legacy_daily == []


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

    agent.memory.add("user", "记录飞书用户 daily", kind="note")

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
    assert provider_daily
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


def test_simple_agent_reports_legacy_runtime_when_home_runtime_disabled(tmp_path: Path, caplog):
    import logging

    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.config import AgentConfig

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    caplog.set_level(logging.WARNING)

    agent = SimpleAgent(
        AgentConfig(my_agent_home=str(home), home_runtime_bootstrap_enabled=False, prompt_files=[]),
        workspace,
    )

    assert agent.using_legacy_paths is True
    assert agent.runtime_path_resolution.using_legacy_paths is True
    assert agent.runtime_path_resolution.reason == "home_runtime_disabled"
    assert agent.local_store.db_path == workspace / "data" / "local_store" / "local.db"
    assert "legacy runtime paths" in caplog.text
