from __future__ import annotations

import json
from pathlib import Path


def test_owner_policy_reads_seed_files_and_usage(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_policy import (
        owner_disk_usage,
        read_owner_policy_bundle,
    )

    home = ensure_my_agent_home(tmp_path)
    (home.owner_workspace_dir / "note.txt").write_text("hello", encoding="utf-8")

    bundle = read_owner_policy_bundle(home)
    usage = owner_disk_usage(home)

    assert bundle.permissions["filesystem"]["access_mode"] == "workspace-write"
    assert bundle.quota["max_subagents"] == 50
    assert bundle.retention["raw_days"] == 90
    assert usage.total_bytes >= 5
    assert str(home.owner_workspace_dir) in usage.by_root


def test_effective_owner_policy_disables_tools_and_keeps_grants(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_policy import resolve_effective_owner_policy
    from agent_py_agent.agent.user_space.temporary_grants import (
        CreateTemporaryGrant,
        create_temporary_grant,
    )

    home = ensure_my_agent_home(tmp_path)
    home.owner_tool_policy_json.write_text(
        json.dumps({"schema_version": "tool-policy.v1", "disabled_tools": ["list_files"]}),
        encoding="utf-8",
    )
    create_temporary_grant(
        home,
        CreateTemporaryGrant(
            granted_to="agent-1",
            capability="filesystem.write",
            path_prefix=str(tmp_path / "out"),
            expires_at="2026-06-01T00:00:00+00:00",
        ),
    )

    policy = resolve_effective_owner_policy(home)

    assert policy.owner_id == "local:main:main"
    assert "list_files" in policy.disabled_tools
    assert policy.active_grants[0]["granted_to"] == "agent-1"


def test_tool_registry_respects_owner_disabled_tools(tmp_path: Path) -> None:
    from agent_py_agent.agent.config import AgentConfig
    from agent_py_agent.agent.core import SimpleAgent

    home = tmp_path / "home"
    agent = SimpleAgent(AgentConfig(my_agent_home=str(home), prompt_files=[]), tmp_path / "repo")
    agent.home_paths.owner_tool_policy_json.write_text(
        json.dumps({"schema_version": "tool-policy.v1", "disabled_tools": ["list_files"]}),
        encoding="utf-8",
    )
    agent = SimpleAgent(AgentConfig(my_agent_home=str(home), prompt_files=[]), tmp_path / "repo")

    assert "list_files" not in {spec.name for spec in agent.tools.specs(include_orchestration=True)}
    result = agent.tools.execute_call({"tool": "list_files", "path": "."})
    assert not result.ok
    assert "owner 策略禁用" in result.output


def test_subagent_inherits_owner_policy_snapshot_and_projection(tmp_path: Path) -> None:
    from agent_py_agent.agent.config import AgentConfig
    from agent_py_agent.agent.core import SimpleAgent

    home = tmp_path / "home"
    agent = SimpleAgent(
        AgentConfig(
            my_agent_home=str(home),
            my_agent_owner_provider="feishu",
            my_agent_owner_kind="user",
            my_agent_owner_id="u002",
            access_mode="full-access",
            prompt_files=[],
        ),
        tmp_path / "repo",
    )
    task = agent.subagents.create_run(goal="查资料", thought="执行", plan=["读", "写"])

    assert task.owner == "providers/feishu/users/u002"
    assert task.runtime_identity.service_owner_id == "providers/feishu/users/u002"
    assert task.effective_permissions["owner_id"] == "providers/feishu/users/u002"
    assert task.effective_permissions["shell_access_mode"] == "workspace-write"
    projection = home / "owners" / "providers" / "feishu" / "users" / "u002" / "agents" / task.id
    assert (projection / "state.json").exists()
    refs = json.loads((projection / "refs.json").read_text(encoding="utf-8"))
    assert refs["run_id"] == task.id


def test_owner_capability_resolver_caches_run_short_name(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.capability_resolver import (
        CapabilityResolveOptions,
        resolve_owner_capability,
    )
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path)
    (home.owner_home_dir / "skills" / "audit-helper").mkdir(parents=True)

    options = CapabilityResolveOptions(run_id="run-1")
    first = resolve_owner_capability(home, "audit-helper", options)
    second = resolve_owner_capability(home, "audit-helper", options)

    assert first.status == "resolved"
    assert first.source == "owner"
    assert second.status == "resolved"
    assert second.cache_path == first.cache_path
    assert first.cache_path and first.cache_path.exists()


def test_temporary_grant_expires_without_deleting_record(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.temporary_grants import (
        CreateTemporaryGrant,
        create_temporary_grant,
        expire_temporary_grants,
        list_temporary_grants,
    )

    home = ensure_my_agent_home(tmp_path)
    created = create_temporary_grant(
        home,
        CreateTemporaryGrant(
            granted_to="agent-1",
            capability="filesystem.write",
            path_prefix=str(tmp_path / "out"),
            expires_at="2026-05-31T00:00:00+00:00",
            reason="用户本次允许写输出目录",
        ),
    )

    expired = expire_temporary_grants(home, now="2026-06-01T00:00:00+00:00")
    all_grants = list_temporary_grants(home)

    assert created.status == "active"
    assert expired[0].grant_id == created.grant_id
    assert all_grants[0].status == "expired"
