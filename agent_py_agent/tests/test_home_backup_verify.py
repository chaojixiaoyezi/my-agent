"""Phase 4B 备份完整性:SHA256 清单 + 恢复前校验 + 篡改/损坏拒绝恢复。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_py_agent.agent.user_space.home_backup import (
    create_home_backup_snapshot,
    restore_home_backup_snapshot_checked,
    verify_home_backup_snapshot,
)
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home


def _home_with_file(tmp_path: Path):
    home = ensure_my_agent_home(tmp_path)
    home.owner_memory_dir.mkdir(parents=True, exist_ok=True)
    (home.owner_memory_dir / "note.md").write_text("重要记忆原文", encoding="utf-8")
    return home


def test_snapshot_records_checksums_and_verifies_ok(tmp_path: Path) -> None:
    home = _home_with_file(tmp_path)
    snap = create_home_backup_snapshot(home, reason="test")
    verdict = verify_home_backup_snapshot(snap.backup_dir)
    assert verdict.ok is True
    assert verdict.checked >= 1
    assert verdict.mismatches == ()


def test_verify_detects_tampered_backup(tmp_path: Path) -> None:
    home = _home_with_file(tmp_path)
    snap = create_home_backup_snapshot(home, reason="test")
    # 篡改备份区文件 → sha256 不符
    tampered = next((snap.backup_dir / "files").rglob("note.md"))
    tampered.write_text("被篡改", encoding="utf-8")
    verdict = verify_home_backup_snapshot(snap.backup_dir)
    assert verdict.ok is False
    assert any("note.md" in m for m in verdict.mismatches)


def test_verify_detects_missing_backup_file(tmp_path: Path) -> None:
    home = _home_with_file(tmp_path)
    snap = create_home_backup_snapshot(home, reason="test")
    next((snap.backup_dir / "files").rglob("note.md")).unlink()  # 删掉 → 校验报缺失
    assert verify_home_backup_snapshot(snap.backup_dir).ok is False


def test_checked_restore_refuses_corrupted_backup(tmp_path: Path) -> None:
    home = _home_with_file(tmp_path)
    snap = create_home_backup_snapshot(home, reason="test")
    next((snap.backup_dir / "files").rglob("note.md")).write_text("坏了", encoding="utf-8")
    with pytest.raises(ValueError):  # fail-closed:不用损坏备份覆盖现状
        restore_home_backup_snapshot_checked(home, snap.backup_dir)


def test_checked_restore_succeeds_on_intact_backup(tmp_path: Path) -> None:
    home = _home_with_file(tmp_path)
    snap = create_home_backup_snapshot(home, reason="test")
    # 模拟现状被破坏后,从完好备份校验恢复
    (home.owner_memory_dir / "note.md").write_text("现状被改坏", encoding="utf-8")
    result = restore_home_backup_snapshot_checked(home, snap.backup_dir)
    assert result.restored_count >= 1
    assert (home.owner_memory_dir / "note.md").read_text(encoding="utf-8") == "重要记忆原文"
