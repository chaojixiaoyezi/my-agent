"""Canonical my-agent Home layout and safe owner initialization."""

# LLM: This module defines canonical owner-home paths; legacy Memory directories must not return as runtime fallbacks.
# 模块用途: 解析、创建和播种每个 owner 的规范 Home、Memory、Persona 与运行目录。

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..common.json_io import write_json_file_atomic
from .home_layout_v2 import v2_home_directories, v2_home_path_fields, v2_seed_files, v2_seed_jsons
from .home_memory_seeds import (
    default_memory_hot_md,
    default_memory_md,
    default_memory_route_index_md,
)
from .owner_policy_seed_payloads import default_quota_payload
from .persona_templates import AGENTS_TEMPLATE, SOUL_TEMPLATE, USER_TEMPLATE

DEFAULT_ROUTE_INDEX = Path("memory") / "routing" / "INDEX.md"


# LLM: 每项 owner 持久权威有明确路径字段；插件与自学习 Skill 提案位于原受保护 data 内，调用方不能从包或用户正文重建地址。
# 类用途: 保存 my-agent 根目录和当前 owner 的全部规范文件/目录路径。
@dataclass(frozen=True)
class MyAgentHomePaths:
    root: Path
    templates_dir: Path
    soul_md: Path
    user_md: Path
    agents_md: Path
    memory_md: Path
    memory_hot_md: Path
    config_dir: Path
    scripts_dir: Path
    workspace_dir: Path
    workspace_tasks_dir: Path
    data_dir: Path
    providers_dir: Path
    memory_archive_dir: Path
    skills_dir: Path
    tools_dir: Path
    role_templates_dir: Path
    logs_dir: Path
    cache_dir: Path
    tmp_dir: Path
    # admin 级临时授权目录(my-agent home 根下,在所有 owner home 的上级、不属于任何 owner home、
    # 也不在 workspace_roots)。owner-scoped(降权)agent 写不到它(write_file 触发 ①归一被重定向、
    # run_command 经 bwrap 根视图无此 mount、PathAccessPolicy owner 墙直接拦),只有框架启动时读取判
    # bypass、以及真人 admin(真实文件权限)写授权不受 owner 墙限制 → 堵 bypass 自授权漏洞(F11④)。
    admin_grants_dir: Path
    shared_dir: Path
    shared_builtin_dir: Path
    shared_skills_dir: Path
    shared_optional_skills_dir: Path
    shared_role_templates_dir: Path
    shared_policy_templates_dir: Path
    shared_scripts_dir: Path
    shared_indexes_dir: Path
    shared_indexes_skills_jsonl: Path
    shared_indexes_role_templates_jsonl: Path
    owners_dir: Path
    local_owners_dir: Path
    owner_home_dir: Path
    owner_soul_md: Path
    owner_user_md: Path
    owner_agents_md: Path
    owner_memory_md: Path
    owner_memory_hot_md: Path
    owner_permissions_json: Path
    owner_quota_json: Path
    owner_retention_json: Path
    owner_memory_policy_json: Path
    owner_skill_policy_json: Path
    owner_tool_policy_json: Path
    owner_sessions_dir: Path
    owner_memory_dir: Path
    owner_memory_daily_dir: Path
    owner_memory_lessons_dir: Path
    owner_memory_routing_dir: Path
    owner_memory_routing_index_md: Path
    owner_memory_candidates_jsonl: Path
    owner_memory_ops_jsonl: Path
    owner_memory_long_term_dir: Path
    owner_memory_long_term_jsonl: Path
    owner_memory_curator_dir: Path
    owner_memory_curator_state_json: Path
    owner_memory_curator_runs_dir: Path
    owner_tasks_dir: Path
    owner_runs_dir: Path
    owner_agents_dir: Path
    owner_compact_dir: Path
    owner_workspace_dir: Path
    owner_artifacts_dir: Path
    owner_audit_dir: Path
    owner_data_dir: Path
    owner_plugins_dir: Path
    owner_artifact_backups_dir: Path
    owner_skill_proposals_dir: Path
    owner_skill_learning_dir: Path
    owner_scheduler_dir: Path
    owner_scheduler_store_json: Path
    owner_scheduler_history_jsonl: Path
    owner_logs_dir: Path
    owner_cache_dir: Path
    owner_tmp_dir: Path
    owner_trash_dir: Path
    owner_capability_requests_dir: Path
    owner_temporary_grants_dir: Path
    owner_audit_log_jsonl: Path
    identity_dir: Path
    canonical_users_dir: Path
    linked_identities_jsonl: Path
    provider_identity_dir: Path
    global_index_dir: Path
    global_index_owners_jsonl: Path
    global_index_active_tasks_jsonl: Path
    global_index_active_runs_jsonl: Path
    global_index_active_agents_jsonl: Path
    system_dir: Path
    system_schema_version_json: Path
    system_config_dir: Path
    system_audit_dir: Path
    system_metrics_dir: Path
    system_doctor_dir: Path
    system_backups_dir: Path
    owner_provider: str = ""
    owner_kind: str = ""
    owner_id: str = ""


@dataclass(frozen=True)
class RouteIndexTarget:
    path: Path
    authority_root: Path


def resolve_my_agent_home(value: str | Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    source = env if env is not None else os.environ
    env_value = source.get("MY_AGENT_HOME")
    raw = value if value is not None else env_value if env_value else "~/.my-agent"
    return Path(raw).expanduser().resolve()


def home_paths(root: str | Path | None = None) -> MyAgentHomePaths:
    home = resolve_my_agent_home(root)
    return MyAgentHomePaths(
        root=home,
        **_root_home_path_fields(home),
        **v2_home_path_fields(home),
    )


def _root_home_path_fields(home: Path) -> dict[str, Path]:
    workspace_dir = home / "workspace"
    templates_dir = home / "templates"
    return {
        # 种子模板收进 templates/,不散落在 home 根目录(对齐 参考实现 的干净根)
        "templates_dir": templates_dir,
        "soul_md": templates_dir / "SOUL.md",
        "user_md": templates_dir / "USER.md",
        "agents_md": templates_dir / "AGENTS.md",
        "memory_md": templates_dir / "memory.md",
        "memory_hot_md": templates_dir / "memory-hot.md",
        "config_dir": home / "config",
        "scripts_dir": home / "scripts",
        "workspace_dir": workspace_dir,
        "workspace_tasks_dir": workspace_dir / "tasks",
        "data_dir": home / "data",
        "providers_dir": home / "providers",
        "memory_archive_dir": home / "memory_archive",
        "skills_dir": home / "skills",
        "tools_dir": home / "tools",
        "role_templates_dir": home / "role_templates",
        "logs_dir": home / "logs",
        "cache_dir": home / "cache",
        "tmp_dir": home / "tmp",
        "admin_grants_dir": home / "admin_grants",
    }


# LLM: Initialization creates empty v2 authorities and never seeds unreviewed lessons/HOT or migrates legacy user data.
# 函数用途: 创建缺失的 Home 目录与安全空种子，并返回规范路径集合。
def ensure_my_agent_home(root: str | Path | None = None) -> MyAgentHomePaths:
    paths = home_paths(root)
    paths.root.mkdir(parents=True, exist_ok=True)
    for directory in _HOME_DIRECTORIES(paths):
        directory.mkdir(parents=True, exist_ok=True)
    _write_seed_file(paths.soul_md, SOUL_TEMPLATE)
    _write_seed_file(paths.user_md, USER_TEMPLATE)
    _write_seed_file(paths.agents_md, AGENTS_TEMPLATE)
    _upgrade_default_persona_templates(paths)  # 存量部署:旧空壳根模板升级为新模板(管理员自定义的不动)
    _write_seed_file(paths.memory_md, default_memory_md())
    _write_seed_file(paths.memory_hot_md, default_memory_hot_md())
    _write_seed_file(paths.owner_memory_routing_index_md, default_memory_route_index_md())
    for path, content in v2_seed_files(paths):
        _write_seed_file(path, content)
    for path, payload in v2_seed_jsons(paths):
        _write_seed_json(path, payload)
    _upgrade_default_quota_policy(paths)
    _sync_skill_index(paths)
    _sync_declarative_indexes(paths)
    _cleanup_legacy_dirs(paths)
    return paths


def _sync_declarative_indexes(paths: MyAgentHomePaths) -> None:
    """把共享 role template 扫进管理员 capability 索引。"""
    try:
        from ..capability.declarative_index import sync_role_template_index

        sync_role_template_index(
            paths.shared_role_templates_dir, paths.shared_indexes_role_templates_jsonl
        )
    except Exception:  # noqa: BLE001 - home 初始化健壮性优先,索引失败不影响加载器直接加载
        pass


def _sync_skill_index(paths: MyAgentHomePaths) -> None:
    """把内置 skill 镜像进 home/shared/builtin,并扫描内置 + 用户自定义(shared/skills)的
    skill 合并写进 skills.jsonl 索引,使其在 home 可见、可被 capability 发现。失败不阻塞
    home 初始化——检索仍可回退源码 registry,功能不丢。"""
    try:
        from ..capability.builtin_seed import sync_skill_index

        sync_skill_index(
            paths.shared_builtin_dir,
            paths.shared_skills_dir,
            paths.shared_indexes_skills_jsonl,
            paths.cache_dir / "skill_index.fingerprint",
        )
    except Exception:  # noqa: BLE001 - home 初始化健壮性优先于 seed,失败可回退源码加载
        pass


# 已废弃、代码不再读写的 legacy 目录(顶层规范位置已迁到 shared/ 和 owners/)。ensure 时
# 清掉(仅空目录,非空保留避免误删),并不再创建(见 _HOME_DIRECTORIES)。
_LEGACY_DIR_FIELDS = (
    "skills_dir",
    "tools_dir",
    "role_templates_dir",
    "scripts_dir",
    "memory_archive_dir",
    "shared_optional_skills_dir",
)


def _cleanup_legacy_dirs(paths: MyAgentHomePaths) -> None:
    """删除已废弃的 legacy 目录,仅删空目录——非空(用户误放了东西)则保留不动,避免误删。
    失败不阻塞启动。"""
    for field in _LEGACY_DIR_FIELDS:
        directory = getattr(paths, field, None)
        if directory is not None:
            _remove_dir_if_empty(Path(directory))


def _remove_dir_if_empty(directory: Path) -> None:
    if not directory.is_dir():
        return
    try:
        if any(item.is_file() for item in directory.rglob("*")):
            return  # 含文件 → 非空,保留不删
        shutil.rmtree(directory, ignore_errors=True)
    except OSError:
        pass


def _HOME_DIRECTORIES(paths: MyAgentHomePaths) -> tuple[Path, ...]:
    # 顶层 legacy 目录(skills/tools/role_templates/scripts/memory_archive)不再
    # 创建——规范位置已迁到 shared/ 和 owners/(废弃清理见 _cleanup_legacy_dirs)。
    return (
        paths.config_dir,
        paths.templates_dir,
        paths.data_dir,
        paths.providers_dir,
        paths.logs_dir,
        paths.cache_dir,
        paths.tmp_dir,
        paths.admin_grants_dir,
        *v2_home_directories(paths),
    )


def _write_seed_file(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def _upgrade_default_persona_templates(paths: MyAgentHomePaths) -> None:
    """把旧的空壳根模板(# SOUL / # USER / # AGENTS)升级为结构化新模板;管理员已改过的(非空壳)一律不动。
    _write_seed_file 幂等不覆盖既有文件,故存量部署的旧根模板需在此单独升级,新用户才能继承新模板。"""
    _upgrade_one_stub(paths.soul_md, "# SOUL", SOUL_TEMPLATE)
    _upgrade_one_stub(paths.user_md, "# USER", USER_TEMPLATE)
    _upgrade_one_stub(paths.agents_md, "# AGENTS", AGENTS_TEMPLATE)


def _upgrade_one_stub(path: Path, stub: str, template: str) -> None:
    """单个根模板:内容恰为旧空壳(strip 后等于 stub)才升级为 template,否则不动。"""
    try:
        if path.exists() and path.read_text(encoding="utf-8").strip() == stub:
            path.write_text(template, encoding="utf-8")
    except OSError:
        pass


def _write_seed_json(path: Path, payload: Mapping[str, object]) -> None:
    if path.exists():
        return
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# LLM: Only the byte-for-byte semantic legacy seed may migrate automatically. Any custom quota
# value is administrator policy and must remain untouched even when it equals a common round number.
# 函数用途: 把未修改过的 quota.v1 默认 100GB 应用扫描迁移成 quota.v2 默认不限容量。
def _upgrade_default_quota_policy(paths: MyAgentHomePaths) -> None:
    legacy_default = {
        "schema_version": "quota.v1",
        "max_active_agents": 1000,
        "max_subagents": 50,
        "max_depth": 4,
        "max_disk_mb": 102400,
    }
    try:
        payload = json.loads(paths.owner_quota_json.read_text(encoding="utf-8"))
        if payload == legacy_default:
            write_json_file_atomic(paths.owner_quota_json, default_quota_payload())
    except (OSError, json.JSONDecodeError):
        return


def safe_task_slug(task_name: str, *, max_chars: int = 80) -> str:
    text = str(task_name or "task").strip().lower()
    slug = _collapse_dashes("".join(_slug_char(char) for char in text)).strip("-_")
    return _trim_slug(slug, max_chars=max_chars)


def _slug_char(char: str) -> str:
    return char if char.isalnum() or char in {"_", "-"} else "-"


def _collapse_dashes(text: str) -> str:
    while "--" in text:
        text = text.replace("--", "-")
    return text


def _trim_slug(slug: str, *, max_chars: int) -> str:
    if not slug:
        return "task"
    return slug[:max_chars].strip("-_") or "task"


def task_workspace_path(
    home: str | Path,
    template: str,
    *,
    date: str,
    task_name: str,
) -> Path:
    task_slug = safe_task_slug(task_name)
    rendered = str(template or "tasks/{date}/{task_slug}").format(
        date=date,
        task_slug=task_slug,
        task_name=task_slug,
    )
    path = Path(rendered)
    if path.is_absolute():
        return path
    return Path(home) / path


def resolve_route_index_target(root: Path, raw_index: str | None, *, home_paths: object | None = None) -> RouteIndexTarget:
    candidate = Path(raw_index).expanduser() if raw_index else DEFAULT_ROUTE_INDEX
    if candidate.is_absolute():
        return RouteIndexTarget(path=candidate.resolve(), authority_root=root)
    workspace_index = (root / candidate).resolve()
    if raw_index or workspace_index.exists() or home_paths is None:
        return RouteIndexTarget(path=workspace_index, authority_root=root)
    home_index = getattr(home_paths, "owner_memory_routing_index_md", None)
    home_root = getattr(home_paths, "owner_home_dir", None)
    if home_index is not None and home_root is not None:
        return RouteIndexTarget(path=Path(home_index).resolve(), authority_root=Path(home_root).resolve())
    return RouteIndexTarget(path=workspace_index, authority_root=root)


# LLM: Runtime lesson routing must use the current owner's deterministic formal index; a
# workspace memory/routing file is legacy data for explicit migration, never a fallback authority.
# 函数用途: 返回 owner home 与其唯一 memory/routing/INDEX.md 相对路径。
def runtime_route_root_and_index(agent) -> tuple[Path, str]:
    home = getattr(agent, "home_paths", None)
    owner_root = getattr(home, "owner_home_dir", None)
    owner_index = getattr(home, "owner_memory_routing_index_md", None)
    if owner_root is None or owner_index is None:
        raise RuntimeError("owner memory routing authority is unavailable")
    root = Path(owner_root).resolve()
    index = Path(owner_index).resolve()
    try:
        relative_index = index.relative_to(root).as_posix()
    except ValueError as exc:
        raise RuntimeError("owner memory routing index escapes owner home") from exc
    return root, relative_index
