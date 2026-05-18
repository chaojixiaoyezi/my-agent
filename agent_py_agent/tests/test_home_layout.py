from __future__ import annotations

from pathlib import Path


# LLM: home layout tests lock the install-time user home contract before runtime migration.
# 函数用途: 验证 MY_AGENT_HOME 默认值、环境变量覆盖和任务路径模板解析。
def test_my_agent_home_defaults_to_dot_my_agent(monkeypatch):
    from agent_py_agent.agent.user_space.home_layout import resolve_my_agent_home

    monkeypatch.delenv("MY_AGENT_HOME", raising=False)

    assert resolve_my_agent_home().name == ".my-agent"


# LLM: explicit MY_AGENT_HOME must win so packaged installs can isolate profiles like 长期助手.
# 函数用途: 验证用户可以用 MY_AGENT_HOME 指定 my-agent 家目录。
def test_my_agent_home_uses_env_override(tmp_path: Path, monkeypatch):
    from agent_py_agent.agent.user_space.home_layout import resolve_my_agent_home

    home = tmp_path / "custom-home"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))

    assert resolve_my_agent_home() == home.resolve()


# LLM: MY_AGENT_HOME must beat config so parallel real runs can isolate homes without editing user config.
# 函数用途: 验证配置文件里有默认 my_agent_home 时，测试/多 profile 仍可用环境变量隔离家目录。
def test_my_agent_home_env_beats_config_value(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_layout import resolve_my_agent_home

    env_home = tmp_path / "env-home"
    config_home = tmp_path / "config-home"

    assert resolve_my_agent_home(config_home, env={"MY_AGENT_HOME": str(env_home)}) == env_home.resolve()


# LLM: task paths should be date/task scoped and sanitize model-provided task names.
# 函数用途: 验证 workspace/tasks/{date}/{task_slug} 模板生成稳定安全的任务目录。
def test_task_workspace_path_template_sanitizes_task_name(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_layout import task_workspace_path

    path = task_workspace_path(
        tmp_path,
        "workspace/tasks/{date}/{task_slug}",
        date="2026-05-13",
        task_name="购物网站 E2E / main",
    )

    assert path == tmp_path / "workspace" / "tasks" / "2026-05-13" / "购物网站-e2e-main"


# LLM: home paths are a stable map for docs, setup, doctor, and later migration commands.
# 函数用途: 验证 home_paths 暴露家目录里的核心路径，不主动创建目录。
def test_home_paths_exposes_core_dirs_without_creating(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_layout import home_paths

    paths = home_paths(tmp_path)

    assert paths.config_dir == tmp_path / "config"
    assert paths.scripts_dir == tmp_path / "scripts"
    assert paths.memory_daily_dir == tmp_path / "memory" / "daily"
    assert paths.providers_dir == tmp_path / "providers"
    assert not paths.config_dir.exists()


# LLM: home initialization should create the owner home without overwriting user-edited identity files.
# 函数用途: 验证 my-agent 家目录初始化会创建标准目录和入口文件，但保留用户已有内容。
def test_ensure_my_agent_home_creates_dirs_and_keeps_existing_files(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    (tmp_path / "SOUL.md").write_text("custom soul\n", encoding="utf-8")

    paths = ensure_my_agent_home(tmp_path)

    assert paths.config_dir.is_dir()
    assert paths.scripts_dir.is_dir()
    assert paths.workspace_tasks_dir.is_dir()
    assert paths.memory_daily_dir.is_dir()
    assert paths.memory_lessons_dir.is_dir()
    assert paths.providers_dir.is_dir()
    assert paths.soul_md.read_text(encoding="utf-8") == "custom soul\n"
    assert paths.agents_md.exists()
