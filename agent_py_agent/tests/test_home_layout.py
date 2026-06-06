from __future__ import annotations

import json
from pathlib import Path


def test_my_agent_home_defaults_to_dot_my_agent(monkeypatch):
    from agent_py_agent.agent.user_space.home_layout import resolve_my_agent_home

    monkeypatch.delenv("MY_AGENT_HOME", raising=False)

    assert resolve_my_agent_home().name == ".my-agent"


def test_my_agent_home_uses_env_override(tmp_path: Path, monkeypatch):
    from agent_py_agent.agent.user_space.home_layout import resolve_my_agent_home

    home = tmp_path / "custom-home"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))

    assert resolve_my_agent_home() == home.resolve()


def test_my_agent_home_env_beats_config_value(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_layout import resolve_my_agent_home

    env_home = tmp_path / "env-home"
    config_home = tmp_path / "config-home"

    assert resolve_my_agent_home(config_home, env={"MY_AGENT_HOME": str(env_home)}) == env_home.resolve()


def test_task_workspace_path_template_sanitizes_task_name(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_layout import task_workspace_path

    path = task_workspace_path(
        tmp_path,
        "tasks/{date}/{task_slug}",
        date="2026-05-13",
        task_name="示例网站 E2E / main",
    )

    assert path == tmp_path / "tasks" / "2026-05-13" / "示例网站-e2e-main"


def test_run_workspace_same_slug_different_prompt_gets_unique_dir(tmp_path: Path):
    from agent_py_agent.agent.user_space.run_workspace import (
        EnsureRunWorkspaceRequest,
        ensure_run_workspace,
    )

    first = ensure_run_workspace(
        EnsureRunWorkspaceRequest(
            home=tmp_path,
            template="tasks/{date}/{task_slug}",
            task_name="all-agent 源码分析",
            user_prompt="请你自己分析 all-agent。",
            created_at="2026-06-06T00:00:00+00:00",
        )
    )
    second = ensure_run_workspace(
        EnsureRunWorkspaceRequest(
            home=tmp_path,
            template="tasks/{date}/{task_slug}",
            task_name="all-agent 源码分析",
            user_prompt="请找帮手一起分析 all-agent。",
            created_at="2026-06-06T00:00:00+00:00",
        )
    )

    assert second.root != first.root
    assert second.root.name == f"{first.root.name}-2"


def test_run_workspace_reuses_same_structured_request_id(tmp_path: Path):
    from agent_py_agent.agent.user_space.run_workspace import (
        EnsureRunWorkspaceRequest,
        ensure_run_workspace,
    )

    first = ensure_run_workspace(
        EnsureRunWorkspaceRequest(
            home=tmp_path,
            template="tasks/{date}/{task_slug}",
            task_name="all-agent 源码分析",
            user_prompt="请你自己分析 all-agent。",
            request_id="req-same",
            created_at="2026-06-06T00:00:00+00:00",
        )
    )
    second = ensure_run_workspace(
        EnsureRunWorkspaceRequest(
            home=tmp_path,
            template="tasks/{date}/{task_slug}",
            task_name="all-agent 源码分析",
            user_prompt="请找帮手一起分析 all-agent。",
            request_id="req-same",
            created_at="2026-06-06T00:00:00+00:00",
        )
    )

    assert second.root == first.root


def test_run_workspace_identity_survives_task_state_updates(tmp_path: Path):
    import json

    from agent_py_agent.agent.user_space.run_workspace import (
        EnsureRunWorkspaceRequest,
        ensure_run_workspace,
    )

    first = ensure_run_workspace(
        EnsureRunWorkspaceRequest(
            home=tmp_path,
            template="tasks/{date}/{task_slug}",
            task_name="all-agent 架构分析",
            user_prompt="请你自己分析 all-agent。",
            request_id="req-a",
            run_id="run-a",
            created_at="2026-06-06T00:00:00+00:00",
        )
    )
    first.state_json.write_text(
        json.dumps({"task_id": "run-a", "primary_run_id": "run-a", "status": "RUNNING"}),
        encoding="utf-8",
    )
    second = ensure_run_workspace(
        EnsureRunWorkspaceRequest(
            home=tmp_path,
            template="tasks/{date}/{task_slug}",
            task_name="all-agent 架构分析",
            user_prompt="请找帮手一起分析 all-agent。",
            request_id="req-b",
            run_id="run-b",
            created_at="2026-06-06T00:00:00+00:00",
        )
    )

    assert second.root != first.root
    assert second.root.name == f"{first.root.name}-run-b"


def test_home_paths_exposes_core_dirs_without_creating(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_layout import home_paths

    paths = home_paths(tmp_path)

    assert paths.config_dir == tmp_path / "config"
    assert paths.scripts_dir == tmp_path / "scripts"
    assert paths.memory_daily_dir == tmp_path / "memory" / "daily"
    assert paths.providers_dir == tmp_path / "providers"
    assert paths.shared_skills_dir == tmp_path / "shared" / "skills"
    assert paths.owner_home_dir == tmp_path / "owners" / "local" / "main"
    assert paths.owner_memory_daily_dir == paths.owner_home_dir / "memory" / "daily"
    assert paths.owner_memory_store_jsonl == paths.owner_home_dir / "memory" / "store.jsonl"
    assert paths.owner_memory_ops_jsonl == paths.owner_home_dir / "memory" / "ops.jsonl"
    assert paths.owner_audit_dir == paths.owner_home_dir / "audit"
    assert paths.owner_blob_tool_outputs_dir == paths.owner_home_dir / "blobs" / "tool_outputs"
    assert paths.global_index_active_tasks_jsonl == tmp_path / "global_index" / "active_tasks.jsonl"
    assert paths.system_schema_version_json == tmp_path / "system" / "schema_version.json"
    assert not paths.config_dir.exists()


def test_ensure_my_agent_home_creates_dirs_and_keeps_existing_files(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    (tmp_path / "SOUL.md").write_text("custom soul\n", encoding="utf-8")

    paths = ensure_my_agent_home(tmp_path)

    assert paths.config_dir.is_dir()
    assert paths.scripts_dir.is_dir()
    assert not paths.workspace_tasks_dir.exists()
    assert paths.memory_daily_dir.is_dir()
    assert paths.memory_lessons_dir.is_dir()
    assert paths.memory_routing_dir.is_dir()
    assert paths.providers_dir.is_dir()
    assert paths.shared_indexes_dir.is_dir()
    assert paths.owner_tasks_dir.is_dir()
    assert paths.owner_memory_long_term_dir.is_dir()
    assert paths.owner_memory_store_jsonl.exists()
    assert paths.owner_memory_ops_jsonl.exists()
    assert paths.owner_audit_dir.is_dir()
    assert paths.owner_blob_tool_outputs_dir.is_dir()
    assert paths.owner_capability_requests_dir.is_dir()
    assert paths.global_index_dir.is_dir()
    assert paths.soul_md.read_text(encoding="utf-8") == "custom soul\n"
    assert paths.agents_md.exists()
    assert paths.memory_hot_md.exists()
    assert paths.memory_routing_index_md.exists()
    assert (paths.memory_lessons_dir / "real-tests.md").exists()
    assert (paths.memory_lessons_dir / "compact.md").exists()


def test_ensure_my_agent_home_creates_v2_owner_and_system_files(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    paths = ensure_my_agent_home(tmp_path)

    schema = json.loads(paths.system_schema_version_json.read_text(encoding="utf-8"))
    permissions = json.loads(paths.owner_permissions_json.read_text(encoding="utf-8"))
    quota = json.loads(paths.owner_quota_json.read_text(encoding="utf-8"))

    assert schema["schema_version"] == "my-agent-home.v2"
    assert schema["read_version"] == "my-agent-home.v2"
    assert permissions["filesystem"]["access_mode"] == "workspace-write"
    assert quota["max_subagents"] == 50
    assert paths.owner_skill_policy_json.exists()
    assert paths.owner_tool_policy_json.exists()
    assert paths.shared_indexes_skills_jsonl.exists()
    assert paths.linked_identities_jsonl.exists()
    assert paths.global_index_active_agents_jsonl.exists()
