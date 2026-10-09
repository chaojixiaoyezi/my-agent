"""迁移快照不推断文件类型：保留二进制、空文件和原始换行字节。"""

import hashlib
import json
import stat
from pathlib import Path

import pytest

from agent_py_agent.agent.memory_store import agents_migration as migration
from agent_py_agent.tests.test_agents_cleanup_migration import _apply, _mixed


# LLM: 只在 tmp_path 隔离 owner 里造数据，任意文件名都必须得到相同字节复制语义。
# 函数用途: 往原有 backups 添加非 UTF-8 文件与空锁文件，不触碰真实 owner。
def _seed_binary(home, filename):
    root = home.owner_home_dir / "persona" / "backups"
    binary = root / filename
    content = bytes(range(256)) + b"\x87\xff\x00\r\n"
    binary.write_bytes(content)
    (root / "empty.md.lock").write_bytes(b"")
    return content


@pytest.mark.parametrize("filename", [".DS_Store", "opaque.bin", "arbitrary.md"])
def test_apply_copies_every_regular_backup_as_exact_bytes(tmp_path, filename):
    home, repo, candidates, _first, _second = _mixed(tmp_path)
    original = _seed_binary(home, filename)
    report = _apply(home, repo, candidates, migration_id="binary")
    directory = Path(report["snapshot_dir"])
    target = directory / "snapshot" / "backups" / filename
    assert report["ok"] is True
    assert report["removed_count"] == 2
    assert target.read_bytes() == original
    assert (directory / "snapshot" / "backups" / "empty.md.lock").read_bytes() == b""
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    manifest = json.loads((directory / "snapshot.json").read_text(encoding="utf-8"))
    assert manifest["files"][f"backups/{filename}"] == {
        "sha256": hashlib.sha256(original).hexdigest(), "bytes": len(original),
    }
    assert manifest["files"]["backups/empty.md.lock"] == {
        "sha256": hashlib.sha256(b"").hexdigest(), "bytes": 0,
    }
    migration._verify_snapshot_manifest(directory, directory / "snapshot.json")


# LLM: 故障点在快照完成后、删除前；续作必须复核原快照，不能重建为文本副本。
# 函数用途: 模拟已保存快照后的中断，让公共 apply 入口真实进入恢复分支。
def _interrupt_removal(*_args):
    raise RuntimeError("injected pause after snapshot")


def test_binary_snapshot_survives_interrupted_apply_and_resume(tmp_path, monkeypatch):
    home, repo, candidates, _first, _second = _mixed(tmp_path)
    original = _seed_binary(home, "non-utf8.blob")
    remove = migration._remove_batch
    monkeypatch.setattr(migration, "_remove_batch", _interrupt_removal)
    with pytest.raises(RuntimeError, match="injected pause after snapshot"):
        _apply(home, repo, candidates, migration_id="binary-resume")
    directory = home.owner_home_dir / "persona" / "migrations" / "agents-migration-binary-resume"
    meta_before = (directory / "snapshot.json").read_bytes()
    assert len(repo.list_entries("agents")["entries"]) == 4
    monkeypatch.setattr(migration, "_remove_batch", remove)
    report = migration.apply_agents_cleanup(
        home_paths=home, persona=repo, candidate_service=candidates,
        request=migration.AgentsCleanupRequest(migration_id="binary-resume"),
    )
    assert report["ok"] is True
    assert report["status"] == "completed"
    assert report["removed_count"] == 2
    assert (directory / "snapshot.json").read_bytes() == meta_before
    assert (directory / "snapshot" / "backups" / "non-utf8.blob").read_bytes() == original
    migration._verify_snapshot_manifest(directory, directory / "snapshot.json")


def test_text_crlf_and_lone_cr_are_not_normalized(tmp_path):
    source, target = tmp_path / "source.md", tmp_path / "snapshot.md"
    content = "中文原文\r\n第二行\r结尾\n".encode()
    source.write_bytes(content)
    manifest = {}
    migration._copy_snapshot_file(source, target, manifest, "original.md")
    assert target.read_bytes() == content
    assert manifest["original.md"] == {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}


def test_legacy_text_manifest_fields_remain_compatible(tmp_path):
    text = "# AGENTS\n- 原有 UTF-8 快照。\n"
    target = tmp_path / "snapshot" / "legacy.md"
    target.parent.mkdir()
    target.write_bytes(text.encode("utf-8"))
    old_encoded = target.read_text(encoding="utf-8").encode("utf-8")
    assert hashlib.sha256(old_encoded).hexdigest() == hashlib.sha256(target.read_bytes()).hexdigest()
    manifest = {"files": {"legacy.md": {"sha256": hashlib.sha256(old_encoded).hexdigest(), "bytes": len(old_encoded)}}}
    meta = tmp_path / "snapshot.json"
    meta.write_text(json.dumps(manifest), encoding="utf-8")
    migration._verify_snapshot_manifest(tmp_path, meta)


def test_binary_snapshot_tampering_is_rejected_by_raw_hash(tmp_path):
    target = tmp_path / "snapshot" / "backup.bin"
    target.parent.mkdir()
    content = b"\x87\xff\x00"
    target.write_bytes(content)
    meta = tmp_path / "snapshot.json"
    meta.write_text(json.dumps({"files": {"backup.bin": {
        "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content),
    }}}), encoding="utf-8")
    migration._verify_snapshot_manifest(tmp_path, meta)
    target.write_bytes(b"\x87\xfe\x00")
    with pytest.raises(migration.AgentsCleanupError) as error:
        migration._verify_snapshot_manifest(tmp_path, meta)
    assert error.value.reason_code == "AGENTS_CLEANUP_SNAPSHOT_TAMPERED"
