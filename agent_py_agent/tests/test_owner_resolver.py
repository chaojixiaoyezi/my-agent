from __future__ import annotations

import json
from pathlib import Path


# LLM: owner resolver is the runtime bridge from CLI/provider identity to one isolated owner home.
# 函数用途: 验证本地 CLI 默认 owner 使用 owners/local/main，并带回 daily/raw/task 等 V2 目录。
def test_resolve_local_main_owner_uses_v2_owner_home(tmp_path: Path):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home

    result = resolve_owner_home(tmp_path, OwnerIdentity.local_main())

    assert result.owner_id == "local/main"
    assert result.home_dir == tmp_path / "owners" / "local" / "main"
    assert result.daily_memory_dir == result.home_dir / "memory" / "daily"
    assert result.tasks_dir == result.home_dir / "tasks"


# LLM: provider owners must live under owners/providers, not the legacy providers directory.
# 函数用途: 验证飞书/微信等外部用户 owner 解析到 V2 provider owner home。
def test_resolve_provider_user_owner_uses_v2_provider_home(tmp_path: Path):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home

    result = resolve_owner_home(tmp_path, OwnerIdentity.provider_user("feishu", "ou_123"))

    assert result.owner_id == "providers/feishu/users/ou_123"
    assert result.home_dir == tmp_path / "owners" / "providers" / "feishu" / "users" / "ou_123"
    assert result.identity.provider == "feishu"
    assert result.identity.owner_kind == "user"


# LLM: owner resolver should materialize owner policy and schema files without overwriting public shared files.
# 函数用途: 验证解析 owner 时会补齐该 owner 的入口文件和策略文件，供未来 provider/session 直接使用。
def test_ensure_owner_home_creates_owner_seed_files(tmp_path: Path):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home

    result = ensure_owner_home(tmp_path, OwnerIdentity.provider_group("feishu", "oc_abc"))

    permissions = json.loads(result.permissions_json.read_text(encoding="utf-8"))
    assert result.agents_md.exists()
    assert result.memory_md.exists()
    assert result.skill_policy_json.exists()
    assert result.tool_policy_json.exists()
    assert permissions["filesystem"]["access_mode"] == "workspace-write"


# LLM: Prompt home context should include owner files while legacy files remain readable during migration.
# 函数用途: 验证 prompt 家目录上下文读取 owner_home，同时保留顶层兼容入口，避免迁移期丢失旧用户配置。
def test_prompt_home_context_reads_owner_entry_files(tmp_path: Path):
    from agent_py_agent.agent.prompting_parts.builder import _home_entry_context_chunks
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    paths = ensure_my_agent_home(tmp_path)
    paths.agents_md.write_text("legacy agents\n", encoding="utf-8")
    paths.owner_agents_md.write_text("owner agents\n", encoding="utf-8")

    rendered = "\n".join(_home_entry_context_chunks(paths))

    assert "owner agents" in rendered
    assert "legacy agents" in rendered


# LLM: SimpleAgent mirrors daily memory to the current owner home.
# 函数用途: 验证主代理运行时 daily memory 写入 owner 目录，不再额外写旧顶层 daily。
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


# LLM: Runtime owner config should switch all owner-backed writes to the provider user home.
# 函数用途: 验证配置指定 provider owner 后，SimpleAgent 的 daily memory 和 home paths 不再落到 local/main。
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
    assert provider_daily
    assert not local_daily
