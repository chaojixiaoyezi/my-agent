from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest


def _access(owner: Path, *, max_bytes: int, available: bool = True):
    from agent_py_agent.agent.tooling._filesystem_read import FileSystemAccessOptions

    return FileSystemAccessOptions(
        owner_scope_root=str(owner),
        owner_quota_max_bytes=max_bytes,
        owner_quota_policy_available=available,
    )


def test_owner_usage_counts_all_regular_files_but_not_symlink_targets(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.owner_quota import owner_logical_usage_bytes

    owner = tmp_path / "owner"
    outside = tmp_path / "outside.bin"
    owner.mkdir()
    outside.write_bytes(b"x" * 100)
    (owner / "a.bin").write_bytes(b"1234")
    (owner / "link").symlink_to(outside)

    assert owner_logical_usage_bytes(owner) == 4


def test_owner_usage_ignores_file_removed_after_directory_listing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_py_agent.agent.user_space.owner_quota import owner_logical_usage_bytes

    owner = tmp_path / "owner"
    owner.mkdir()
    stable = owner / "stable.bin"
    volatile = owner / ".state.json.atomic.tmp"
    stable.write_bytes(b"stable")
    volatile.write_bytes(b"volatile")
    real_stat = Path.stat
    removed = False

    def stat_with_atomic_removal(path: Path, *args, **kwargs):
        nonlocal removed
        if path == volatile and not removed:
            removed = True
            volatile.unlink()
            raise FileNotFoundError(os.fspath(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_with_atomic_removal)

    assert owner_logical_usage_bytes(owner) == len(b"stable")
    assert removed


def test_owner_usage_keeps_non_missing_stat_errors_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_py_agent.agent.user_space.owner_quota import (
        OwnerQuotaUnavailable,
        owner_logical_usage_bytes,
    )

    owner = tmp_path / "owner"
    owner.mkdir()
    blocked = owner / "blocked.bin"
    blocked.write_bytes(b"blocked")
    real_stat = Path.stat

    def stat_with_permission_failure(path: Path, *args, **kwargs):
        if path == blocked:
            raise PermissionError(os.fspath(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_with_permission_failure)

    with pytest.raises(OwnerQuotaUnavailable, match="scan failed"):
        owner_logical_usage_bytes(owner)


def test_owner_usage_root_permission_error_is_not_treated_as_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_py_agent.agent.user_space.owner_quota import (
        OwnerQuotaUnavailable,
        owner_logical_usage_bytes,
    )

    owner = (tmp_path / "owner").resolve()
    owner.mkdir()
    real_stat = Path.stat

    def stat_with_root_permission_failure(path: Path, *args, **kwargs):
        if path == owner:
            raise PermissionError(os.fspath(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_with_root_permission_failure)

    with pytest.raises(OwnerQuotaUnavailable, match="root is unavailable"):
        owner_logical_usage_bytes(owner)


def test_linux_admission_scan_uses_exact_native_regular_file_sizes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_py_agent.agent.user_space import owner_quota

    owner = tmp_path / "owner"
    owner.mkdir()
    observed: dict[str, object] = {}

    def native_run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="4\n7\n", stderr="")

    monkeypatch.setattr(owner_quota.sys, "platform", "linux")
    monkeypatch.setattr(owner_quota.os, "access", lambda _path, _mode: True)
    monkeypatch.setattr(owner_quota.subprocess, "run", native_run)

    assert owner_quota._owner_usage_bytes_for_admission(owner) == 11
    command = observed["command"]
    assert command[:4] == ["/usr/bin/find", str(owner), "-type", "f"]
    assert str(owner / ".owner-quota.lock") in command
    assert observed["kwargs"] == {
        "check": False,
        "capture_output": True,
        "text": True,
        "timeout": owner_quota._NATIVE_USAGE_SCAN_TIMEOUT_SECONDS,
    }


def test_linux_admission_scan_timeout_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_py_agent.agent.user_space import owner_quota

    owner = tmp_path / "owner"
    owner.mkdir()

    def timeout(*_args, **_kwargs):
        raise owner_quota.subprocess.TimeoutExpired("find", 15)

    monkeypatch.setattr(owner_quota.sys, "platform", "linux")
    monkeypatch.setattr(owner_quota.os, "access", lambda _path, _mode: True)
    monkeypatch.setattr(owner_quota.subprocess, "run", timeout)

    with pytest.raises(owner_quota.OwnerQuotaUnavailable, match="timed out"):
        owner_quota._owner_usage_bytes_for_admission(owner)


def test_linux_admission_scan_falls_back_when_find_is_not_supported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_py_agent.agent.user_space import owner_quota

    owner = tmp_path / "owner"
    owner.mkdir()
    (owner / "payload.bin").write_bytes(b"12345")
    monkeypatch.setattr(owner_quota.sys, "platform", "linux")
    monkeypatch.setattr(owner_quota.os, "access", lambda _path, _mode: True)
    monkeypatch.setattr(
        owner_quota.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="find: unknown predicate -printf",
        ),
    )

    assert owner_quota._owner_usage_bytes_for_admission(owner) == 5


def test_zero_quota_policy_disables_hot_path_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_py_agent.agent.user_space import owner_quota

    owner = tmp_path / "owner"
    owner.mkdir()
    (owner / "quota.json").write_text(
        '{"schema_version":"quota.v2","max_disk_mb":0}\n',
        encoding="utf-8",
    )
    enforcer = owner_quota.owner_quota_enforcer_from_policy(owner)
    monkeypatch.setattr(
        owner_quota,
        "_owner_usage_bytes_for_admission",
        lambda _root: (_ for _ in ()).throw(AssertionError("unlimited policy must not scan")),
    )

    with enforcer.admission() as admission:
        assert admission.enabled is False
        assert admission.check([owner_quota.OwnerQuotaChange(owner / "x", 1)]) is None


def test_unreadable_quota_policy_remains_fail_closed(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space import owner_quota

    owner = tmp_path / "owner"
    owner.mkdir()
    (owner / "quota.json").write_text("{bad-json}\n", encoding="utf-8")
    enforcer = owner_quota.owner_quota_enforcer_from_policy(owner)

    with pytest.raises(owner_quota.OwnerQuotaUnavailable, match="policy is unavailable"):
        with enforcer.admission():
            pass


def test_write_file_quota_is_cross_tool_instance_and_cross_thread_safe(tmp_path: Path) -> None:
    from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool, WriteFileToolOptions

    owner = tmp_path / "owner"
    owner.mkdir()
    options = WriteFileToolOptions(access_options=_access(owner, max_bytes=10))
    tools = [WriteFileTool(owner, [owner], options), WriteFileTool(owner, [owner], options)]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda item: item[0].execute(
                    {"path": item[1], "content": "12345678"}
                ),
                zip(tools, ("a.txt", "b.txt"), strict=True),
            )
        )

    assert sum(result.ok for result in results) == 1
    assert {result.error_code for result in results if not result.ok} == {
        "OWNER_DISK_QUOTA_EXCEEDED"
    }
    assert sum(path.stat().st_size for path in owner.glob("*.txt")) == 8


def test_concurrent_appends_project_current_size_under_owner_lock(tmp_path: Path) -> None:
    from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool, WriteFileToolOptions

    owner = tmp_path / "owner"
    owner.mkdir()
    target = owner / "shared.txt"
    target.write_text("1234", encoding="utf-8")
    options = WriteFileToolOptions(access_options=_access(owner, max_bytes=10))
    tools = [WriteFileTool(owner, [owner], options), WriteFileTool(owner, [owner], options)]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda tool: tool.execute(
                    {"path": "shared.txt", "content": "5678", "mode": "append"}
                ),
                tools,
            )
        )

    assert sum(result.ok for result in results) == 1
    assert {result.error_code for result in results if not result.ok} == {
        "OWNER_DISK_QUOTA_EXCEEDED"
    }
    assert target.stat().st_size == 8


def test_quota_policy_load_failure_blocks_owner_write(tmp_path: Path) -> None:
    from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool, WriteFileToolOptions

    owner = tmp_path / "owner"
    owner.mkdir()
    tool = WriteFileTool(
        owner,
        [owner],
        WriteFileToolOptions(access_options=_access(owner, max_bytes=100, available=False)),
    )

    result = tool.execute({"path": "blocked.txt", "content": "x"})

    assert not result.ok
    assert result.error_code == "OWNER_QUOTA_UNAVAILABLE"
    assert not (owner / "blocked.txt").exists()


def test_over_quota_owner_can_shrink_existing_file(tmp_path: Path) -> None:
    from agent_py_agent.agent.tooling._filesystem_edit import EditFileTool

    owner = tmp_path / "owner"
    owner.mkdir()
    target = owner / "large.txt"
    target.write_text("abcdefghij", encoding="utf-8")
    tool = EditFileTool(owner, [owner], _access(owner, max_bytes=5))

    result = tool.execute(
        {"path": "large.txt", "old_string": "abcdefghij", "new_string": "tiny"}
    )

    assert result.ok
    assert target.read_text(encoding="utf-8") == "tiny"


def test_apply_patch_rejects_over_quota_batch_before_first_file_changes(tmp_path: Path) -> None:
    from agent_py_agent.agent.tooling._filesystem_patch import ApplyPatchTool

    owner = tmp_path / "owner"
    owner.mkdir()
    tool = ApplyPatchTool(owner, [owner], _access(owner, max_bytes=7))
    patch = """*** Begin Patch
*** Add File: a.txt
+1234
*** Add File: b.txt
+5678
*** End Patch
"""

    result = tool.execute({"patch": patch})

    assert not result.ok
    assert result.error_code == "OWNER_DISK_QUOTA_EXCEEDED"
    assert not (owner / "a.txt").exists()
    assert not (owner / "b.txt").exists()


def test_workspace_write_cannot_escape_owner_root_before_quota_check(tmp_path: Path) -> None:
    from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool, WriteFileToolOptions

    owner = tmp_path / "owner"
    workspace = tmp_path / "workspace"
    owner.mkdir()
    workspace.mkdir()
    tool = WriteFileTool(
        workspace,
        [workspace],
        WriteFileToolOptions(access_options=_access(owner, max_bytes=1)),
    )

    result = tool.execute({"path": "repo.txt", "content": "outside-owner"})

    assert not result.ok
    assert result.error_code == "WRITE_FORBIDDEN"
    assert not (workspace / "repo.txt").exists()


def test_max_active_agents_caps_main_plus_live_subagents() -> None:
    from agent_py_agent.agent.agent_core.orchestration_tools import _available_creation_slots

    agent = SimpleNamespace(
        config=SimpleNamespace(
            max_subagents=50,
            subagent_hierarchy_max_children_per_tool_call=20,
            task_max_subagents=0,
        ),
        owner_policy=SimpleNamespace(max_subagents=50, max_active_agents=2),
        subagents=SimpleNamespace(
            list_runs=lambda: [SimpleNamespace(id="subagent-1", status="RUNNING")]
        ),
    )

    slots, details = _available_creation_slots(agent)

    assert slots == 0
    assert details["active_agent_cap"] == 2
    assert details["owner_active"] == 1


def _finish_reconcile(root):
    from agent_py_agent.agent.user_space import owner_quota as quota

    for _ in range(100):
        result = quota.reconcile_owner_quota_slice(root, max_entries=128, max_ms=20)
        if result['complete']:
            return result
    pytest.fail('quota reconciliation did not converge')


@pytest.mark.parametrize('files', [125, 126, 160, 256])
def test_reconciled_finite_owner_allows_consecutive_growth(tmp_path, files):
    from agent_py_agent.agent.user_space import owner_quota as quota

    for index in range(files):
        (tmp_path / f'{index:04d}').write_bytes(b'x')
    assert _finish_reconcile(tmp_path)['used_bytes'] == files
    enforcer = quota.OwnerQuotaEnforcer(tmp_path, max_bytes=1_048_576)
    target = tmp_path / 'managed'
    for turn in range(4):
        with enforcer.reserve([quota.OwnerQuotaChange(target, 1, append=True)]) as projection:
            assert projection.used_bytes == files + turn
            with target.open('ab') as stream:
                stream.write(b'y')
        ledger = json.loads((tmp_path / '.owner-quota-ledger.json').read_text())
        assert ledger['used_bytes'] == files + turn + 1
        assert ledger['dirty_since'] is None and not ledger['reservations']
        assert ledger['reconcile_cursor']['ledger_revision'] == ledger['revision']
    with enforcer.admission() as admission:
        admission.check([quota.OwnerQuotaChange(target, 5)])
        target.write_bytes(b'12345')
    assert json.loads((tmp_path / '.owner-quota-ledger.json').read_text())['used_bytes'] == files + 5


@pytest.mark.parametrize('mutation', [
    'create', 'unlink', 'append', 'nested-append', 'symlink',
    'symlink-to-file', 'symlink-to-dir', 'nested-symlink-to-file',
])
def test_unmanaged_changes_invalidate_reconciled_growth(tmp_path, mutation):
    from agent_py_agent.agent.user_space import owner_quota as quota

    for index in range(160):
        (tmp_path / f'{index:04d}').write_bytes(b'x')
    (tmp_path / 'nested').mkdir()
    (tmp_path / 'nested' / 'data').write_bytes(b'x')
    link = tmp_path / ('nested/link' if mutation.startswith('nested-') else 'link')
    if 'symlink-to-' in mutation:
        link.symlink_to('/dev/null')
    _finish_reconcile(tmp_path)
    target = tmp_path / 'managed'
    enforcer = quota.OwnerQuotaEnforcer(tmp_path, max_bytes=1_048_576)
    if mutation == 'create':
        (tmp_path / 'external').write_bytes(b'external')
    elif mutation == 'unlink':
        (tmp_path / '0000').unlink()
    elif mutation in ('append', 'nested-append'):
        path = tmp_path / ('0000' if mutation == 'append' else 'nested/data')
        with path.open('ab') as stream:
            stream.write(b'external')
    elif 'symlink-to-' in mutation:
        link.unlink()
        if mutation == 'symlink-to-dir':
            link.mkdir()
            (link / 'untracked').write_bytes(b'external')
        else:
            link.write_bytes(b'external')
    else:
        (tmp_path / '0000').unlink()
        (tmp_path / '0000').symlink_to('/dev/null')
    with pytest.raises(quota.OwnerQuotaUnavailable, match='reconciliation pending'):
        with enforcer.reserve([quota.OwnerQuotaChange(target, 1)]):
            pytest.fail('unmanaged modification was trusted')
    assert not target.exists()
    assert json.loads((tmp_path / '.owner-quota-ledger.json').read_text())['dirty_since'] is not None
    _finish_reconcile(tmp_path)
    with enforcer.reserve([quota.OwnerQuotaChange(target, 1)]):
        target.write_bytes(b'y')


def test_reconciled_baseline_refuses_orphan_and_still_enforces_limit(tmp_path):
    from agent_py_agent.agent.user_space import owner_quota as quota

    for index in range(160):
        (tmp_path / str(index)).write_bytes(b'x')
    _finish_reconcile(tmp_path)
    enforcer = quota.OwnerQuotaEnforcer(tmp_path, max_bytes=160)
    with pytest.raises(quota.OwnerQuotaExceeded) as raised:
        with enforcer.reserve([quota.OwnerQuotaChange(tmp_path / 'new', 1)]):
            pytest.fail('quota bypassed')
    assert raised.value.projection.projected_bytes == 161
    path = tmp_path / '.owner-quota-ledger.json'
    ledger = json.loads(path.read_text())
    ledger.update(reservations={'crashed': {'bytes': 1, 'expires_at': 1}}, reserved_bytes=1)
    path.write_text(json.dumps(ledger))
    enforcer = quota.OwnerQuotaEnforcer(tmp_path, max_bytes=1_048_576)
    with pytest.raises(quota.OwnerQuotaUnavailable, match='reconciliation pending'):
        with enforcer.reserve([quota.OwnerQuotaChange(tmp_path / 'new', 1)]):
            pytest.fail('orphan reservation trusted')
    _finish_reconcile(tmp_path)
    with enforcer.reserve([quota.OwnerQuotaChange(tmp_path / 'new', 1)]):
        (tmp_path / 'new').write_bytes(b'y')


def test_bounded_snapshot_detects_eof_at_exact_entry_budget(tmp_path):
    from agent_py_agent.agent.user_space import owner_quota as quota

    for index in range(128):
        (tmp_path / str(index)).write_bytes(b'x')
    assert quota._bounded_quota_snapshot(tmp_path, max_entries=128, max_ms=1000) == (128, 128, True)
    (tmp_path / 'overflow').write_bytes(b'xx')
    total, count, complete = quota._bounded_quota_snapshot(tmp_path, max_entries=128, max_ms=1000)
    assert not complete and count == 128 and 128 <= total <= 129


@pytest.mark.parametrize('layout', ['flat-2100', 'dirs-300'])
def test_reconciled_growth_beyond_legacy_tracking_limits_and_fast_admission(tmp_path, monkeypatch, layout):
    from agent_py_agent.agent.user_space import owner_quota as quota

    files = 2100 if layout == 'flat-2100' else 300
    for index in range(files):
        parent = tmp_path if layout == 'flat-2100' else tmp_path / f'd{index:04d}'
        parent.mkdir(exist_ok=True)
        (parent / f'f{index:04d}').write_bytes(b'x')
    result = _finish_reconcile(tmp_path)
    ledger_path = tmp_path / '.owner-quota-ledger.json'
    ledger = json.loads(ledger_path.read_text())
    assert result['complete'] and ledger['dirty_since'] is None
    assert ledger['reconcile_cursor']['metadata_complete'] is True
    assert ledger_path.stat().st_size < 65536
    original_verify, original_stat = quota._verify_quota_metadata, quota._quota_sorted_stat
    calls = {'verify': 0, 'stat': 0}

    def verify(*args):
        calls['verify'] += 1
        return original_verify(*args)

    def child_stat(*args):
        calls['stat'] += 1
        return original_stat(*args)

    monkeypatch.setattr(quota, '_verify_quota_metadata', verify)
    monkeypatch.setattr(quota, '_quota_sorted_stat', child_stat)
    enforcer = quota.OwnerQuotaEnforcer(tmp_path, max_bytes=1_048_576)
    target = tmp_path / 'managed'
    for turn in range(3):
        with enforcer.reserve([quota.OwnerQuotaChange(target, 1, append=True)]) as projection:
            assert projection.used_bytes == files + turn
            with target.open('ab') as stream:
                stream.write(b'y')
        ledger = json.loads(ledger_path.read_text())
        assert ledger['used_bytes'] == files + turn + 1
        assert ledger['dirty_since'] is None and not ledger['reservations']
        assert ledger_path.stat().st_size < 65536
    assert calls['verify'] == 3  # 结算不能再跑一次整树验证。
    assert calls['stat'] <= 12  # nofollow重开仅限声明路径，不逐个重开全部文件。


def test_reconciled_nested_batch_keeps_algebraic_baseline(tmp_path):
    from agent_py_agent.agent.user_space import owner_quota as quota

    for index in range(160):
        (tmp_path / f'{index:04d}').write_bytes(b'x')
    _finish_reconcile(tmp_path)
    first, second = tmp_path / 'new/deep/a', tmp_path / 'new/deep/b'
    enforcer = quota.OwnerQuotaEnforcer(tmp_path, max_bytes=1_048_576)
    with enforcer.admission() as admission:
        admission.check([quota.OwnerQuotaChange(first, 3), quota.OwnerQuotaChange(second, 4)])
        first.parent.mkdir(parents=True)
        first.write_bytes(b'123')
        second.write_bytes(b'1234')
        admission.check([quota.OwnerQuotaChange(first, 1, append=True)])
        with first.open('ab') as stream:
            stream.write(b'5')
    with enforcer.reserve([quota.OwnerQuotaChange(second, None)]):
        second.unlink()
    ledger = json.loads((tmp_path / '.owner-quota-ledger.json').read_text())
    assert ledger['used_bytes'] == 164 and ledger['inode_count'] == 161
    assert quota._verified_quota_baseline(tmp_path, ledger)
