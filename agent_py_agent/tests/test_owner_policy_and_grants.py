from __future__ import annotations

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
