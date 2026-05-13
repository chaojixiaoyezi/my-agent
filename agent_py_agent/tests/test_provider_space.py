from __future__ import annotations

from pathlib import Path


# LLM: provider roots are lazy-created only when a connector is enabled.
# 函数用途: 验证接入平台时只创建平台根目录和平台级配置，不提前创建用户/群目录。
def test_ensure_provider_root_is_lazy(tmp_path: Path):
    from agent_py_agent.agent.user_space.provider_space import ensure_provider_root

    result = ensure_provider_root(tmp_path, "qq")

    assert result.provider_dir == tmp_path / "providers" / "qq"
    assert result.provider_config == result.provider_dir / "provider.yaml"
    assert result.provider_dir.exists()
    assert not (result.provider_dir / "users").exists()
    assert not (result.provider_dir / "groups").exists()


# LLM: provider user spaces own their local tools, skills, templates, and workflows.
# 函数用途: 验证外部私聊用户首次使用时获得自己的隔离空间和可自定义能力目录。
def test_ensure_provider_user_space_creates_private_customization_dirs(tmp_path: Path):
    from agent_py_agent.agent.user_space.provider_space import (
        ProviderSpaceIdentity,
        ensure_provider_space,
    )

    paths = ensure_provider_space(tmp_path, ProviderSpaceIdentity("feishu", "user", "ou_123"))

    assert paths.root_dir == tmp_path / "providers" / "feishu" / "users" / "ou_123"
    for child in ("tools", "skills", "role_templates", "workflows", "downloads", "cache", "tmp", "trash"):
        assert (paths.root_dir / child).is_dir()


# LLM: group admins may manage only their own group scope; owner home remains out of scope.
# 函数用途: 验证群管理/群主只能在本群空间内做破坏性管理，不能越权到主目录。
def test_group_admin_scope_checks_do_not_allow_owner_home(tmp_path: Path):
    from agent_py_agent.agent.user_space.provider_space import (
        ProviderSpaceIdentity,
        can_manage_group_space,
        ensure_provider_space,
        path_is_within_provider_space,
    )

    paths = ensure_provider_space(tmp_path, ProviderSpaceIdentity("feishu", "group", "oc_abc"))

    assert can_manage_group_space("owner")
    assert can_manage_group_space("admin")
    assert not can_manage_group_space("member")
    assert path_is_within_provider_space(paths.root_dir / "tools" / "a.py", paths)
    assert not path_is_within_provider_space(tmp_path / "SOUL.md", paths)


# LLM: destructive group actions should move targets into the same group's dated trash.
# 函数用途: 验证群空间删除目标默认进入本群 trash/date，不直接物理删除。
def test_provider_space_trash_target_stays_in_same_group(tmp_path: Path):
    from agent_py_agent.agent.user_space.provider_space import (
        ProviderSpaceIdentity,
        ensure_provider_space,
        trash_target_for,
    )

    paths = ensure_provider_space(tmp_path, ProviderSpaceIdentity("qq", "group", "98765"))
    target = paths.root_dir / "tools" / "old-tool.py"
    trash_target = trash_target_for(paths, target, date="2026-05-13")

    assert trash_target.parent == paths.root_dir / "trash" / "2026-05-13"
    assert trash_target.name.startswith("old-tool")


# LLM: quota status is scoped per external user/group and uses MiB config values.
# 函数用途: 验证外部用户/群空间存储配额按当前目录统计并返回 M 单位状态。
def test_provider_space_quota_status_reports_limit_and_usage(tmp_path: Path):
    from agent_py_agent.agent.user_space.provider_space import (
        ProviderSpaceIdentity,
        ProviderSpaceQuota,
        ensure_provider_space,
        quota_status,
    )

    paths = ensure_provider_space(tmp_path, ProviderSpaceIdentity("qq", "user", "u1"))
    payload = paths.root_dir / "downloads" / "payload.bin"
    payload.write_bytes(b"x" * 1024)

    status = quota_status(paths, ProviderSpaceQuota(max_storage_mb=1, max_download_file_mb=1))

    assert status.max_storage_mb == 1
    assert status.used_bytes >= 1024
    assert not status.over_limit


# LLM: moving to provider trash must be recoverable and leave an audit event in the same scope.
# 函数用途: 验证外部用户/群空间内的删除会移动到 trash，并写入本空间审计流水。
def test_move_to_space_trash_moves_target_and_records_audit(tmp_path: Path):
    from agent_py_agent.agent.user_space.provider_space import (
        ProviderSpaceIdentity,
        ProviderTrashRequest,
        ensure_provider_space,
        move_to_space_trash,
        provider_audit_log_path,
    )

    paths = ensure_provider_space(tmp_path, ProviderSpaceIdentity("qq", "group", "98765"))
    target = paths.tools_dir / "old-tool.py"
    target.write_text("print('old')\n", encoding="utf-8")

    result = move_to_space_trash(
        ProviderTrashRequest(
            paths=paths,
            target=target,
            actor_id="group-admin",
            reason="cleanup",
            date="2026-05-13",
        )
    )

    assert result.moved
    assert not target.exists()
    assert result.trashed.exists()
    assert provider_audit_log_path(paths).read_text(encoding="utf-8").count("move_to_trash") == 1


# LLM: provider-space config values should come from AgentConfig, not scattered constants.
# 函数用途: 验证外部用户/群空间配额配置可以从后端真实配置归一化后生成。
def test_provider_space_quota_from_agent_config():
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.settings.config_normalize import normalize_agent_config
    from agent_py_agent.agent.user_space.provider_space import provider_quota_from_agent_config

    normalized, warnings = normalize_agent_config(
        {
            "provider_space_default_max_storage_mb": "512",
            "provider_space_max_download_file_mb": "64",
            "provider_space_trash_retention_days": "45",
        }
    )

    quota = provider_quota_from_agent_config(AgentConfig(**normalized))

    assert warnings == []
    assert quota.max_storage_mb == 512
    assert quota.max_download_file_mb == 64


# LLM: provider trash cleanup must use retention days from config and never leave the provider space.
# 函数用途: 验证外部用户/群空间 trash 可按配置保留天数清理旧日期目录。
def test_provider_trash_retention_from_agent_config_purges_old_days(tmp_path: Path):
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.settings.config_normalize import normalize_agent_config
    from agent_py_agent.agent.user_space.provider_space import (
        ProviderSpaceIdentity,
        ensure_provider_space,
        provider_trash_retention_days_from_agent_config,
        purge_provider_trash,
    )

    normalized, warnings = normalize_agent_config({"provider_space_trash_retention_days": "2"})
    paths = ensure_provider_space(tmp_path, ProviderSpaceIdentity("qq", "group", "g1"))
    old_file = paths.trash_dir / "2026-05-10" / "old.txt"
    recent_file = paths.trash_dir / "2026-05-12" / "recent.txt"
    old_file.parent.mkdir(parents=True)
    recent_file.parent.mkdir(parents=True)
    old_file.write_text("old\n", encoding="utf-8")
    recent_file.write_text("recent\n", encoding="utf-8")

    deleted = purge_provider_trash(
        paths,
        retention_days=provider_trash_retention_days_from_agent_config(AgentConfig(**normalized)),
        today="2026-05-13",
    )

    assert warnings == []
    assert old_file.parent in deleted
    assert not old_file.exists()
    assert recent_file.exists()
