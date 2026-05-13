# LLM: Home layout helpers define the install-time ~/.my-agent contract; keep them side-effect free unless named ensure_*.
# 模块用途: 解析 my-agent 家目录、核心路径和任务工作区路径模板。

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


# LLM: MyAgentHomePaths is the stable path map used by setup, docs, doctor, and later migrations.
# 类用途: 保存 ~/.my-agent 下各类目录和关键文件的位置；只描述路径，不负责创建。
@dataclass(frozen=True)
class MyAgentHomePaths:
    root: Path
    soul_md: Path
    user_md: Path
    agents_md: Path
    memory_md: Path
    config_dir: Path
    scripts_dir: Path
    workspace_dir: Path
    workspace_tasks_dir: Path
    memory_dir: Path
    memory_daily_dir: Path
    memory_raw_dir: Path
    memory_hooks_dir: Path
    memory_lessons_dir: Path
    memory_indexes_dir: Path
    data_dir: Path
    providers_dir: Path
    memory_archive_dir: Path
    skills_dir: Path
    tools_dir: Path
    role_templates_dir: Path
    workflows_dir: Path
    logs_dir: Path
    cache_dir: Path
    tmp_dir: Path


# LLM: resolve_my_agent_home centralizes MY_AGENT_HOME precedence without creating directories.
# 函数用途: 根据显式参数、环境变量或默认值解析 my-agent 家目录路径。
def resolve_my_agent_home(value: str | Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    source = env if env is not None else os.environ
    raw = value if value is not None else source.get("MY_AGENT_HOME", "~/.my-agent")
    return Path(raw).expanduser().resolve()


# LLM: home_paths keeps the tree discoverable without touching disk.
# 函数用途: 返回 my-agent 家目录下的标准文件和目录路径。
def home_paths(root: str | Path | None = None) -> MyAgentHomePaths:
    home = resolve_my_agent_home(root)
    memory_dir = home / "memory"
    workspace_dir = home / "workspace"
    return MyAgentHomePaths(
        root=home,
        soul_md=home / "SOUL.md",
        user_md=home / "USER.md",
        agents_md=home / "AGENTS.md",
        memory_md=home / "memory.md",
        config_dir=home / "config",
        scripts_dir=home / "scripts",
        workspace_dir=workspace_dir,
        workspace_tasks_dir=workspace_dir / "tasks",
        memory_dir=memory_dir,
        memory_daily_dir=memory_dir / "daily",
        memory_raw_dir=memory_dir / "raw",
        memory_hooks_dir=memory_dir / "hooks",
        memory_lessons_dir=memory_dir / "lessons",
        memory_indexes_dir=memory_dir / "indexes",
        data_dir=home / "data",
        providers_dir=home / "providers",
        memory_archive_dir=home / "memory_archive",
        skills_dir=home / "skills",
        tools_dir=home / "tools",
        role_templates_dir=home / "role_templates",
        workflows_dir=home / "workflows",
        logs_dir=home / "logs",
        cache_dir=home / "cache",
        tmp_dir=home / "tmp",
    )


# LLM: ensure_my_agent_home materializes the owner profile without overwriting human-maintained files.
# 函数用途: 创建 my-agent 家目录基础目录和入口文件，供安装、doctor 或迁移命令调用。
def ensure_my_agent_home(root: str | Path | None = None) -> MyAgentHomePaths:
    paths = home_paths(root)
    paths.root.mkdir(parents=True, exist_ok=True)
    for directory in _HOME_DIRECTORIES(paths):
        directory.mkdir(parents=True, exist_ok=True)
    _write_seed_file(paths.soul_md, "# SOUL\n\n")
    _write_seed_file(paths.user_md, "# USER\n\n")
    _write_seed_file(paths.agents_md, "# AGENTS\n\n")
    _write_seed_file(paths.memory_md, "# Memory\n\n")
    return paths


# LLM: _HOME_DIRECTORIES keeps ensure_my_agent_home declarative and easy to extend.
# 函数用途: 返回初始化家目录时需要创建的目录列表。
def _HOME_DIRECTORIES(paths: MyAgentHomePaths) -> tuple[Path, ...]:
    return (
        paths.config_dir,
        paths.scripts_dir,
        paths.workspace_tasks_dir,
        paths.memory_daily_dir,
        paths.memory_raw_dir,
        paths.memory_hooks_dir,
        paths.memory_lessons_dir,
        paths.memory_indexes_dir,
        paths.data_dir,
        paths.providers_dir,
        paths.memory_archive_dir / "artifacts",
        paths.memory_archive_dir / "compact_applies",
        paths.memory_archive_dir / "snapshots",
        paths.memory_archive_dir / "tokens",
        paths.skills_dir,
        paths.tools_dir,
        paths.role_templates_dir,
        paths.workflows_dir,
        paths.logs_dir,
        paths.cache_dir,
        paths.tmp_dir,
    )


# LLM: _write_seed_file keeps user-owned identity files stable after first initialization.
# 函数用途: 文件不存在时写入种子内容，已存在时不覆盖。
def _write_seed_file(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


# LLM: safe_task_slug accepts multilingual task names and removes path-control characters.
# 函数用途: 把用户/模型给出的任务名转换成适合放进目录名的短 slug。
def safe_task_slug(task_name: str, *, max_chars: int = 80) -> str:
    text = str(task_name or "task").strip().lower()
    slug = _collapse_dashes("".join(_slug_char(char) for char in text)).strip("-_")
    return _trim_slug(slug, max_chars=max_chars)


# LLM: _slug_char keeps multilingual task names readable while removing path-control characters.
# 函数用途: 把单个字符转换成 slug 可用字符，不可用字符统一变成横线。
def _slug_char(char: str) -> str:
    return char if char.isalnum() or char in {"_", "-"} else "-"


# LLM: _collapse_dashes avoids noisy task directory names after punctuation replacement.
# 函数用途: 把连续横线折叠成一个横线。
def _collapse_dashes(text: str) -> str:
    while "--" in text:
        text = text.replace("--", "-")
    return text


# LLM: _trim_slug applies length and empty fallback rules for task directory names.
# 函数用途: 裁剪任务 slug，并在空结果时回退成 task。
def _trim_slug(slug: str, *, max_chars: int) -> str:
    if not slug:
        return "task"
    return slug[:max_chars].strip("-_") or "task"


# LLM: task_workspace_path expands the admin-configured template while keeping task names sanitized.
# 函数用途: 根据 workspace/tasks/{date}/{task_slug} 这类模板计算任务工作区路径。
def task_workspace_path(
    home: str | Path,
    template: str,
    *,
    date: str,
    task_name: str,
) -> Path:
    task_slug = safe_task_slug(task_name)
    rendered = str(template or "workspace/tasks/{date}/{task_slug}").format(
        date=date,
        task_slug=task_slug,
        task_name=task_slug,
    )
    path = Path(rendered)
    if path.is_absolute():
        return path
    return Path(home) / path
