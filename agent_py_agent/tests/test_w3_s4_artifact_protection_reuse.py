"""S4: shell artifact protection reuses unchanged preimages and merges prepared/executing writes."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_py_agent.agent.artifacts import shell_operation_journal as journal
from agent_py_agent.agent.artifacts import shell_protection as protection
from agent_py_agent.agent.artifacts.registry import (
    ArtifactRegistration,
    latest_artifact_records,
    register_artifact,
)


# LLM: 写入并登记 N 份交付物后等过粗粒度文件系统时钟的一个刻度，首个快照的指纹因此不落在 racy 窗口。
# 函数用途: 建立多产物工作区与 owner 备份根。
def _case(tmp_path: Path, count: int = 4) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for index in range(count):
        path = workspace / f"report-{index}.custom"
        path.write_text(f"before-{index}", encoding="utf-8")
        register_artifact(ArtifactRegistration(
            workspace_root=workspace, path=path, artifact_id=f"artifact-{index}",
            kind="custom", status="ready",
        ))
    time.sleep(0.05)
    return workspace, tmp_path / "backups"


# 函数用途: 返回某次操作的运行范围。
def _scope(k: int) -> dict[str, str]:
    return {"owner_id": "local/main", "run_id": f"run-{k}", "task_id": f"task-{k}"}


# LLM: 与 tooling/shell.py 的顺序一致：快照 → executing → (命令) → 复核 → 完成 → 结算。
# 函数用途: 跑完整一次命令保护周期，between 在命令位置执行，返回快照与复核汇总。
def _cycle(workspace: Path, store: Path, k: int, between=None):
    snapshots = protection.snapshot_ready_artifacts(
        workspace, store, run_scope=_scope(k), operation_id=f"op-{k}",
    )
    key = protection.mark_shell_artifact_operation_executing(store, snapshots)
    if between is not None:
        between(snapshots)
    summary = protection.reconcile_shell_artifact_operation_items(
        workspace, snapshots, backup_store_root=store,
    )
    protection.complete_shell_artifact_operation(store, snapshots, {"tool": "run_command", "ok": True})
    assert protection.settle_shell_artifact_operation(
        store, run_scope=_scope(k), operation_id=f"op-{k}", operation_key=key,
    )
    return snapshots, summary


# 函数用途: 列出复用区里停放的前像文件。
def _parked(store: Path) -> list[Path]:
    return sorted((store / "v1" / ".reuse").glob("*.preimage"))


# LLM: 稳态（产物未变）命令启动前只有 1 次目录 fsync + 1 次 executing 清单（文件+目录），整周期 fsync 与 N 无关。
# 函数用途: 验证 12+N → 命令前 3、整周期 10 的 fsync 次数。
@pytest.mark.parametrize("count", [1, 8])
def test_w3_s4_unchanged_command_fsyncs_are_constant(tmp_path, monkeypatch, count):
    workspace, store = _case(tmp_path, count)
    _cycle(workspace, store, 0)
    calls: list[int] = []
    real_fsync = os.fsync

    # 函数用途: 计数并转发真实 fsync。
    def counting_fsync(fd):
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", counting_fsync)
    snapshots = protection.snapshot_ready_artifacts(workspace, store, run_scope=_scope(1), operation_id="op-1")
    key = protection.mark_shell_artifact_operation_executing(store, snapshots)
    before_command = len(calls)
    protection.reconcile_shell_artifact_operation_items(workspace, snapshots, backup_store_root=store)
    protection.complete_shell_artifact_operation(store, snapshots, {"tool": "run_command", "ok": True})
    protection.settle_shell_artifact_operation(store, run_scope=_scope(1), operation_id="op-1", operation_key=key)
    assert len(snapshots) == count
    assert before_command == 3
    assert len(calls) == 10


# LLM: 复用把上一次复核确认未变的停放前像改名移入本操作：不复制、不哈希；移入物是单链接 0600 普通文件且字节与产物一致。
# 函数用途: 验证复用路径免复制免哈希，且前像内容精确。
def test_w3_s4_unchanged_artifacts_reuse_preimage_without_copy_or_hash(tmp_path):
    workspace, store = _case(tmp_path, 3)
    _cycle(workspace, store, 0)
    assert len(_parked(store)) == 3
    checked: list[str] = []

    # 函数用途: 命令位置核对每份前像与实时字节一致、私有且独占。
    def verify(snapshots):
        assert not _parked(store)
        for snapshot in snapshots:
            backup = protection.resolve_shell_artifact_backup(store, snapshot.backup_ref)
            info = os.lstat(backup)
            assert info.st_nlink == 1
            if os.name == "posix":
                assert info.st_mode & 0o777 == 0o600
            assert backup.read_bytes() == Path(snapshot.path).read_bytes()
            assert hashlib.sha256(backup.read_bytes()).hexdigest() == snapshot.sha256
            checked.append(snapshot.artifact_id)

    with (
        patch.object(protection, "_write_atomic_backup", wraps=protection._write_atomic_backup) as copies,
        patch.object(protection, "_sha256_opened_source", wraps=protection._sha256_opened_source) as hashes,
    ):
        _snapshots, summary = _cycle(workspace, store, 1, between=verify)
    assert copies.call_count == 0
    assert hashes.call_count == 0
    assert sorted(checked) == ["artifact-0", "artifact-1", "artifact-2"]
    assert len(summary["unchanged"]) == 3 and not summary["changed"]
    assert len(_parked(store)) == 3
    assert not list(store.rglob("*.blob"))


# LLM: git racy 规则：时钟下界不晚于产物 ctime 时指纹不可信，快照照旧复制、复核照旧整份哈希，也不停放。
# 函数用途: 验证 racy 指纹强制复制与重新哈希。
def test_w3_s4_racy_fingerprint_is_copied_and_rehashed(tmp_path, monkeypatch):
    workspace, store = _case(tmp_path, 2)
    paths = [workspace / "report-0.custom", workspace / "report-1.custom"]
    floor = min(path.stat().st_ctime_ns for path in paths)
    real_clock = protection._filesystem_clock

    # 函数用途: 返回不早于产物 ctime 的下界（同设备），模拟同一时钟刻度内的观测。
    def racy_clock(store_root):
        clock = real_clock(store_root)
        return None if clock is None else (clock[0], floor)

    monkeypatch.setattr(protection, "_filesystem_clock", racy_clock)
    for k in range(2):
        with (
            patch.object(protection, "_write_atomic_backup", wraps=protection._write_atomic_backup) as copies,
            patch.object(protection, "_sha256_opened_source", wraps=protection._sha256_opened_source) as hashes,
        ):
            _cycle(workspace, store, k)
        assert copies.call_count == 2
        assert hashes.call_count == 2
        assert not _parked(store)


# LLM: 同大小改写并把 mtime 调回原值仍会推进 ctime，复核必须重新哈希并登记变更，前像保留改写前字节。
# 函数用途: 验证指纹中的 ctime 能识别保持大小与 mtime 的原地改写。
def test_w3_s4_same_size_rewrite_with_restored_mtime_is_detected(tmp_path):
    workspace, store = _case(tmp_path, 1)
    _cycle(workspace, store, 0)
    path = workspace / "report-0.custom"
    info = path.stat()

    # 函数用途: 命令位置做同大小改写并恢复 mtime。
    def rewrite(_snapshots):
        path.write_text("AFTER-0!", encoding="utf-8")
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))

    snapshots, summary = _cycle(workspace, store, 1, between=rewrite)
    assert [item["artifact_id"] for item in summary["changed"]] == ["artifact-0"]
    backup = protection.resolve_shell_artifact_backup(store, snapshots[0].backup_ref)
    assert backup.read_text(encoding="utf-8") == "before-0"
    assert latest_artifact_records(workspace)["artifact-0"].metadata["backup_ref"] == snapshots[0].backup_ref


# LLM: 变更项的前像仍是经校验的操作级 blob 并被登记簿引用；它离开复用索引，下一次命令只复制这一份。
# 函数用途: 验证复用后的变更登记、前像校验与索引移除。
def test_w3_s4_changed_artifact_keeps_verified_preimage_and_leaves_reuse(tmp_path):
    workspace, store = _case(tmp_path, 2)
    _cycle(workspace, store, 0)
    changed = workspace / "report-1.custom"
    with patch.object(protection, "_verify_backup_preimage", wraps=protection._verify_backup_preimage) as verified:
        snapshots, summary = _cycle(
            workspace, store, 1, between=lambda _s: changed.write_text("changed", encoding="utf-8"),
        )
    assert [item["artifact_id"] for item in summary["changed"]] == ["artifact-1"]
    assert verified.call_count == 1
    snapshot = next(item for item in snapshots if item.artifact_id == "artifact-1")
    backup = protection.resolve_shell_artifact_backup(store, snapshot.backup_ref)
    assert backup.read_text(encoding="utf-8") == "before-1"
    assert latest_artifact_records(workspace)["artifact-1"].metadata["backup_ref"] == snapshot.backup_ref
    assert str(changed) not in protection._read_reuse_index(store)
    with patch.object(protection, "_write_atomic_backup", wraps=protection._write_atomic_backup) as copies:
        _cycle(workspace, store, 2)
    assert copies.call_count == 1
    assert backup.read_text(encoding="utf-8") == "before-1"


# LLM: prepared 不再单独落盘；命令启动那一刻磁盘上已有唯一一份 executing 清单，且以默认耐久方式写入。
# 函数用途: 验证合并后的 executing 清单在 spawn 前耐久存在。
def test_w3_s4_merged_executing_manifest_is_durable_before_spawn(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

    workspace, store = _case(tmp_path, 2)
    tool = ShellTool(workspace, options=ShellToolOptions(artifact_backup_root=store, default_timeout=30))
    writes: list[tuple[str, bool]] = []
    real_write = journal.write_text_atomic_beneath

    # 函数用途: 记录清单写入时的状态与耐久参数。
    def recording_write(root, parts, text, *args, **kwargs):
        writes.append((json.loads(text)["state"], kwargs.get("durable", True)))
        return real_write(root, parts, text, *args, **kwargs)

    seen: list[list[str]] = []
    real_run = ShellTool._run_process_text

    # 函数用途: 在真正启动命令前读取磁盘上的清单状态。
    def spawn(self, *args, **kwargs):
        seen.append([
            json.loads(path.read_text(encoding="utf-8"))["state"]
            for path in (store / "v1").glob("*/operation.json")
        ])
        seen.append([state for state, _durable in writes])
        return real_run(self, *args, **kwargs)

    monkeypatch.setattr(journal, "write_text_atomic_beneath", recording_write)
    monkeypatch.setattr(ShellTool, "_run_process_text", spawn)
    result = tool.execute({"command": "true"})
    assert result.ok is True
    assert seen == [["executing"], ["executing"]]
    assert all(durable for _state, durable in writes)


# LLM: executing 清单写不成时命令不启动，本次移入或复制的前像全部回收，不留下可执行清单。
# 函数用途: 验证合并写失败仍失败关闭并回收前像。
def test_w3_s4_executing_write_failure_blocks_spawn_and_discards_preimages(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

    workspace, store = _case(tmp_path, 2)
    _cycle(workspace, store, 0)
    tool = ShellTool(workspace, options=ShellToolOptions(artifact_backup_root=store, default_timeout=30))

    # 函数用途: 模拟清单落盘失败。
    def fail_write(*_args, **_kwargs):
        raise OSError("manifest write failed")

    staged_before = set(protection._PENDING_MANIFESTS)
    with monkeypatch.context() as faults:
        faults.setattr(protection, "write_operation_manifest", fail_write)
        result = tool.execute({"command": "printf started > command-started.txt"})
    assert result.ok is False
    assert result.error_code == "ARTIFACT_BACKUP_FAILED"
    assert result.effect_outcome == "not_started"
    assert not (workspace / "command-started.txt").exists()
    assert not list(store.rglob("*.blob")) and not list(store.rglob("operation.json"))
    assert set(protection._PENDING_MANIFESTS) <= staged_before
    retry = tool.execute({"command": "printf started > command-started.txt"})
    assert retry.ok is True
    assert (workspace / "command-started.txt").read_text() == "started"


# LLM: 断电/重启后的恢复进程不信任清单里记录的指纹（即使与当前 stat 完全相同），一律整份哈希并登记真实变更。
# 函数用途: 验证跨进程恢复不走免哈希快路。
def test_w3_s4_recovery_in_another_process_rehashes_even_when_fingerprint_matches(tmp_path, monkeypatch):
    workspace, store = _case(tmp_path, 2)
    _cycle(workspace, store, 0)
    snapshots = protection.snapshot_ready_artifacts(workspace, store, run_scope=_scope(9), operation_id="op-9")
    key = protection.mark_shell_artifact_operation_executing(store, snapshots)
    protection.release_shell_artifact_operation(key)
    changed = workspace / "report-1.custom"
    changed.write_text("torn", encoding="utf-8")
    manifest = protection.read_operation_manifest(store, key)
    for item in manifest["items"]:
        live = protection._stat_fingerprint(Path(item["snapshot"]["path"]).stat())
        item["fingerprint"]["stat"] = live
        item["fingerprint"]["clock"] = [live[0], live[4] + 1]
    protection.write_operation_manifest(store, key, manifest)
    monkeypatch.setattr(protection, "_PROCESS_TOKEN", "another-process")
    with patch.object(protection, "_sha256_opened_source", wraps=protection._sha256_opened_source) as hashes:
        outcome = protection.reconcile_shell_artifact_operation(store, run_scope=_scope(9), operation_id="op-9")
    assert outcome["outcome"] == "unknown"
    assert hashes.call_count >= 2
    record = latest_artifact_records(workspace)["artifact-1"]
    assert record.metadata["backup_ref"] == next(s.backup_ref for s in snapshots if s.artifact_id == "artifact-1")
    backup = protection.resolve_shell_artifact_backup(store, record.metadata["backup_ref"])
    assert backup.read_text(encoding="utf-8") == "before-1"


# LLM: 停放区受条目上限约束：被逐出的前像随索引改写清扫掉，崩溃遗留的无主前像同样清掉。
# 函数用途: 验证复用区有界与孤儿清扫。
def test_w3_s4_reuse_store_is_bounded_and_sweeps_orphans(tmp_path, monkeypatch):
    monkeypatch.setattr(protection, "_REUSE_MAX_ENTRIES", 2)
    workspace, store = _case(tmp_path, 4)
    _cycle(workspace, store, 0)
    assert len(protection._read_reuse_index(store)) == 2
    assert len(_parked(store)) == 2
    orphan = store / "v1" / ".reuse" / f"{'0' * 16}-{'0' * 64}.preimage"
    orphan.write_bytes(b"orphan")
    _cycle(workspace, store, 1)
    assert not orphan.exists()
    assert len(protection._read_reuse_index(store)) == 2
    assert len(_parked(store)) == 2


# LLM: 索引损坏、停放前像被换成错误大小或符号链接时只放弃复用：照旧复制，前像字节仍与产物一致。
# 函数用途: 验证复用缓存的失败都退回复制而不降低前像可信度。
def test_w3_s4_corrupt_index_or_tampered_preimage_falls_back_to_copy(tmp_path):
    workspace, store = _case(tmp_path, 2)
    _cycle(workspace, store, 0)
    parked = _parked(store)
    parked[0].write_bytes(b"tampered and longer than before")
    parked[1].unlink()
    parked[1].symlink_to(workspace / "report-0.custom")

    # 函数用途: 命令位置核对前像是普通文件且字节正确。
    def verify(snapshots):
        for snapshot in snapshots:
            backup = protection.resolve_shell_artifact_backup(store, snapshot.backup_ref)
            assert not backup.is_symlink()
            assert backup.read_bytes() == Path(snapshot.path).read_bytes()

    with patch.object(protection, "_write_atomic_backup", wraps=protection._write_atomic_backup) as copies:
        _cycle(workspace, store, 1, between=verify)
    assert copies.call_count == 2
    (store / "v1" / ".reuse" / "index.json").write_text("{broken", encoding="utf-8")
    with patch.object(protection, "_write_atomic_backup", wraps=protection._write_atomic_backup) as copies:
        _cycle(workspace, store, 2, between=verify)
    assert copies.call_count == 2
    assert len(_parked(store)) == 2


# LLM: 清理步骤失败时停放的前像移回本操作目录，债务引用的前像真实存在；重试只清理该项，不重新观测。
# 函数用途: 验证停放与清理债务语义一致。
def test_w3_s4_cleanup_failure_moves_parked_preimage_back(tmp_path):
    workspace, store = _case(tmp_path, 2)
    _cycle(workspace, store, 0)
    snapshots = protection.snapshot_ready_artifacts(workspace, store, run_scope=_scope(1), operation_id="op-1")
    key = protection.mark_shell_artifact_operation_executing(store, snapshots)
    real_discard = protection._discard_backup
    failed_ref = snapshots[1].backup_ref

    # 函数用途: 只让一份前像的删除失败。
    def busy(root, ref):
        if ref == failed_ref:
            raise OSError("busy")
        real_discard(root, ref)

    with patch.object(protection, "_discard_backup", side_effect=busy):
        summary = protection.reconcile_shell_artifact_operation_items(workspace, snapshots, backup_store_root=store)
    assert summary["cleanup_deferred"] == [failed_ref]
    assert protection.resolve_shell_artifact_backup(store, failed_ref).read_text(encoding="utf-8") == "before-1"
    assert protection.read_operation_manifest(store, key)["items"][1]["state"] == "cleanup_pending"
    with patch.object(protection, "_post_shell_status", side_effect=AssertionError("must not reobserve")):
        summary = protection.reconcile_shell_artifact_operation_items(workspace, snapshots, backup_store_root=store)
    assert not summary["cleanup_deferred"]
    assert not list(store.rglob("*.blob"))


# LLM: 没有可保护产物的命令不读索引、不触碰时钟探针，也不创建任何备份目录。
# 函数用途: 验证零产物命令没有新增磁盘副作用。
def test_w3_s4_no_ready_artifacts_touches_no_backup_state(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = tmp_path / "backups"
    assert protection.snapshot_ready_artifacts(workspace, store) == []
    assert not store.exists()
