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


def test_my_agent_home_explicit_value_beats_env(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_layout import resolve_my_agent_home

    env_home = tmp_path / "env-home"
    config_home = tmp_path / "config-home"

    assert resolve_my_agent_home(config_home, env={"MY_AGENT_HOME": str(env_home)}) == config_home.resolve()


def test_task_workspace_path_template_sanitizes_task_name(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_layout import task_workspace_path

    path = task_workspace_path(
        tmp_path,
        "tasks/{date}/{task_slug}",
        date="2026-05-13",
        task_name="示例网站 E2E / main",
    )

    assert path == tmp_path / "tasks" / "2026-05-13" / "示例网站-e2e-main"


def test_concise_task_title_uses_path_basename_without_host_home() -> None:
    from agent_py_agent.agent.user_space.task_title import concise_task_title

    title = concise_task_title(
        "你现在只做一件事：认真阅读 /Users/example/study-agent/all-agent 下面的项目，分析架构。"
    )

    assert title == "all-agent-架构分析"


def test_concise_task_title_handles_windows_paths() -> None:
    from agent_py_agent.agent.user_space.task_title import concise_task_title

    title = concise_task_title(
        r"请阅读 C:\Users\ada\study-agent\all-agent 下面的项目，分析架构并写报告。"
    )

    assert title == "all-agent-架构分析"


def test_machine_request_id_never_becomes_task_directory_name(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.run_workspace import (
        EnsureRunWorkspaceRequest,
        run_workspace_paths,
    )

    paths = run_workspace_paths(
        EnsureRunWorkspaceRequest(
            home=tmp_path,
            template="tasks/{date}/{task_slug}",
            task_name="req_1783992152809_3659711_3",
            task_id="req_1783992152809_3659711_3",
            user_prompt='请创建一个项目，项目叫“StarBridge”，并完成可运行版本。',
            created_at="2026-07-14T00:00:00+00:00",
        )
    )

    assert paths.root.name == "starbridge"


def test_machine_ids_with_underscore_are_recognized() -> None:
    from agent_py_agent.agent.user_space.task_title import looks_like_machine_id

    assert looks_like_machine_id("req_1783992152809_3659711_3") is True
    assert looks_like_machine_id("run_abc123") is True
    assert looks_like_machine_id("gwreq-1788132973-8ce54bd2f455446eb38b0b01c1ce5f21") is True


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
    assert not (first.work_dir / "compact").exists()
    assert not (second.work_dir / "compact").exists()


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
    assert paths.providers_dir == tmp_path / "providers"
    assert paths.shared_skills_dir == tmp_path / "shared" / "skills"
    assert paths.owner_home_dir == tmp_path / "owners" / "local" / "main"
    assert paths.owner_memory_daily_dir == paths.owner_home_dir / "memory" / "daily"
    assert paths.owner_memory_long_term_jsonl == paths.owner_home_dir / "memory" / "long_term" / "memory.jsonl"
    assert paths.owner_memory_candidates_jsonl == paths.owner_home_dir / "memory" / "candidates.jsonl"
    assert paths.owner_memory_ops_jsonl == paths.owner_home_dir / "memory" / "ops.jsonl"
    assert paths.owner_audit_dir == paths.owner_home_dir / "audit"
    assert paths.global_index_active_tasks_jsonl == tmp_path / "global_index" / "active_tasks.jsonl"
    assert paths.system_schema_version_json == tmp_path / "system" / "schema_version.json"
    assert not paths.config_dir.exists()


def test_ensure_my_agent_home_creates_dirs_and_keeps_existing_files(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    (tmp_path / "templates").mkdir(parents=True, exist_ok=True)
    (tmp_path / "templates" / "SOUL.md").write_text("custom soul\n", encoding="utf-8")

    paths = ensure_my_agent_home(tmp_path)

    assert paths.config_dir.is_dir()
    # 顶层 legacy 目录不再创建(规范位置迁到 shared/ 和 owners/,空的被 cleanup 删)
    assert not paths.scripts_dir.exists()
    assert not paths.skills_dir.exists()
    assert not paths.tools_dir.exists()
    assert not paths.role_templates_dir.exists()
    assert not paths.memory_archive_dir.exists()
    assert not paths.shared_optional_skills_dir.exists()
    assert not paths.workspace_tasks_dir.exists()
    assert not (tmp_path / "memory" / "daily").exists()
    assert paths.providers_dir.is_dir()
    assert paths.shared_indexes_dir.is_dir()
    assert paths.owner_tasks_dir.is_dir()
    assert paths.owner_memory_long_term_dir.is_dir()
    assert paths.owner_memory_long_term_jsonl.exists()
    assert paths.owner_memory_candidates_jsonl.exists()
    assert paths.owner_memory_ops_jsonl.exists()
    assert paths.owner_audit_dir.is_dir()
    assert (paths.owner_compact_dir / "conversations").is_dir()
    assert not (paths.owner_compact_dir / "by_task").exists()
    assert not (paths.owner_compact_dir / "by_run").exists()
    assert not (paths.owner_compact_dir / "by_agent").exists()
    assert not (paths.owner_home_dir / "blobs").exists()
    assert paths.owner_capability_requests_dir.is_dir()
    assert paths.global_index_dir.is_dir()
    # 种子模板收进 templates/,不再散落 home 根目录
    assert paths.soul_md == tmp_path / "templates" / "SOUL.md"
    assert paths.soul_md.read_text(encoding="utf-8") == "custom soul\n"
    assert not (tmp_path / "SOUL.md").exists()  # 根目录不再有散落的 SOUL.md
    # 种子模板:owner SOUL 从 templates 模板复制一份
    assert paths.owner_soul_md.read_text(encoding="utf-8") == "custom soul\n"
    assert paths.agents_md.exists()
    assert paths.memory_hot_md.exists()
    assert paths.owner_memory_routing_index_md.exists()
    assert list(paths.owner_memory_lessons_dir.glob("*.md")) == []


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
    assert quota["schema_version"] == "quota.v2"
    assert quota["max_disk_mb"] == 0
    assert paths.owner_skill_policy_json.exists()
    assert paths.owner_tool_policy_json.exists()
    assert paths.shared_indexes_skills_jsonl.exists()
    assert paths.linked_identities_jsonl.exists()
    assert paths.global_index_active_agents_jsonl.exists()


def test_ensure_my_agent_home_upgrades_only_untouched_legacy_quota_seed(
    tmp_path: Path,
) -> None:
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    paths = ensure_my_agent_home(tmp_path)
    legacy_default = {
        "schema_version": "quota.v1",
        "max_active_agents": 1000,
        "max_subagents": 50,
        "max_depth": 4,
        "max_disk_mb": 102400,
    }
    paths.owner_quota_json.write_text(
        json.dumps(legacy_default, sort_keys=True),
        encoding="utf-8",
    )

    ensure_my_agent_home(tmp_path)

    assert json.loads(paths.owner_quota_json.read_text(encoding="utf-8")) == {
        "schema_version": "quota.v2",
        "max_active_agents": 1000,
        "max_subagents": 50,
        "max_depth": 4,
        "max_disk_mb": 0,
    }

    custom = {**legacy_default, "max_disk_mb": 512}
    paths.owner_quota_json.write_text(json.dumps(custom, sort_keys=True), encoding="utf-8")

    ensure_my_agent_home(tmp_path)

    assert json.loads(paths.owner_quota_json.read_text(encoding="utf-8")) == custom


def test_cleanup_legacy_dirs_removes_empty_keeps_nonempty(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_layout import (
        _cleanup_legacy_dirs,
        ensure_my_agent_home,
    )

    paths = ensure_my_agent_home(tmp_path)
    paths.skills_dir.mkdir(parents=True, exist_ok=True)  # 残留的空 legacy 目录
    keep = paths.tools_dir / "x" / "keep.txt"
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_text("用户误放的东西", encoding="utf-8")  # 非空 legacy 目录

    _cleanup_legacy_dirs(paths)
    assert not paths.skills_dir.exists()  # 空 → 删
    assert keep.exists()  # 非空 → 保留,不误删


def test_concise_task_title_does_not_mistake_inline_slash_for_path():
    """R5c/R7c slug 实锤钉子:中文并列词"成功/失败统计"不是路径,任务目录名
    必须取任务主题而非 prompt 尾部碎词;真路径与词内斜杠两形态都要正确。"""
    from agent_py_agent.agent.user_space.task_title import concise_task_title

    prompt = (
        "请把 DeepSeek（深度求索）2026 年以来发布的每一篇论文找出来，翻译成中文。\n"
        "8. 最后汇报时写清交付目录的绝对路径、论文清单、每篇的处理结果和成功/失败统计。\n"
    )
    slug = concise_task_title(prompt)
    assert slug != "失败统计" and "deepseek" in slug, f"主题丢失: {slug}"
    # 真路径仍走路径标题分支
    assert "all-agent" in concise_task_title("请分析 /Users/example/study-agent/all-agent 的架构并出报告")
    # 词内斜杠(A/B)不误判为路径
    assert "a-b" in concise_task_title("做一个 A/B 测试方案对比转化率")
