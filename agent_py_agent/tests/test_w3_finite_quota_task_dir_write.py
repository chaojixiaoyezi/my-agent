"""A finite-quota owner can write files right after the host or a shell changed its tree outside the quota gate.

Live clone (2026-11-17, managed user u1, max_disk_mb=1024): in u1's first web task, write_file was refused three
times with OWNER_QUOTA_UNAVAILABLE.  The host had created the task directory outside the quota admission, so the
per-directory table was stale and admission answered reconciliation_pending until the next background
maintenance tick (up to a minute).  The model fell back to shell writes, which the gate does not see at all.
"""
from __future__ import annotations

import json

from agent_py_agent.agent.tooling._filesystem_read import FileSystemAccessOptions
from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool, WriteFileToolOptions
from agent_py_agent.agent.user_space.owner_quota import (
    LocalUserQuotaLimits,
    reconcile_owner_quota_slice,
    seed_local_user_quota,
)

_TASKS = ('tasks', '2026-11-17')


def _finite_owner(tmp_path):
    owner = tmp_path / 'owners' / 'local' / 'u1'
    owner.mkdir(parents=True)
    seed_local_user_quota(tmp_path, owner, LocalUserQuotaLimits(max_disk_mb=1024))
    # More entries than the fresh foreground count, so admission relies on the stored per-directory table.
    for index in range(160):
        folder = owner / 'workspace' / f'notes-{index // 16:02d}'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f'note-{index:03d}.md').write_text('draft\n')
    earlier = owner.joinpath(*_TASKS, 'stask-earlier', 'work')
    earlier.mkdir(parents=True)
    (earlier / 'done.txt').write_text('done\n')
    for _ in range(64):
        if reconcile_owner_quota_slice(owner, max_entries=4096, max_ms=5000)['complete']:
            return owner
    raise AssertionError('owner quota reconciliation did not complete')


def _write_tool(owner, max_bytes=1024 * 1024 * 1024):
    access = FileSystemAccessOptions(owner_scope_root=str(owner), owner_quota_max_bytes=max_bytes)
    return WriteFileTool(owner, [owner], WriteFileToolOptions(access_options=access))


def _ledger(owner):
    return json.loads((owner / '.owner-quota-ledger.json').read_text())


def _host_task_dir(owner, task_id):
    # Same shape as the host's task workspace provisioning: plain mkdir, no quota declaration.
    work = owner.joinpath(*_TASKS, task_id, 'work')
    work.mkdir(parents=True)
    return work


def test_write_file_into_a_new_host_task_dir_is_admitted(tmp_path):
    owner = _finite_owner(tmp_path)
    _host_task_dir(owner, 'stask-new')
    result = _write_tool(owner).execute({'path': 'tasks/2026-11-17/stask-new/work/q1.txt', 'content': 'one'})
    assert result.ok, (result.error_code, result.output)
    assert owner.joinpath(*_TASKS, 'stask-new', 'work', 'q1.txt').read_text() == 'one'
    ledger = _ledger(owner)
    assert ledger['dirty_since'] is None and ledger['usage_authoritative'] is True


def test_shell_growth_is_counted_before_the_next_write_is_admitted(tmp_path):
    owner = _finite_owner(tmp_path)
    work = _host_task_dir(owner, 'stask-shell')
    before = _ledger(owner)['used_bytes']
    (work / 'q2.txt').write_bytes(b'x' * 4096)  # a shell command writes outside the gate
    result = _write_tool(owner).execute({'path': 'tasks/2026-11-17/stask-shell/work/q3.txt', 'content': 'three'})
    assert result.ok, (result.error_code, result.output)
    assert _ledger(owner)['used_bytes'] >= before + 4096 + len('three')


def test_the_limit_still_applies_after_the_inline_reconcile(tmp_path):
    owner = _finite_owner(tmp_path)
    work = _host_task_dir(owner, 'stask-full')
    limit = _ledger(owner)['used_bytes'] + 1024
    (work / 'big.bin').write_bytes(b'x' * 4096)  # outside the gate, pushes usage over the limit
    result = _write_tool(owner, max_bytes=limit).execute(
        {'path': 'tasks/2026-11-17/stask-full/work/more.txt', 'content': 'more'})
    assert not result.ok and result.error_code == 'OWNER_DISK_QUOTA_EXCEEDED'
    assert not (work / 'more.txt').exists()


def test_refusal_stays_when_the_inline_reconcile_cannot_finish(tmp_path, monkeypatch):
    owner = _finite_owner(tmp_path)
    work = _host_task_dir(owner, 'stask-large')
    calls = []

    def unfinished(root, **budget):
        calls.append(budget)
        return {'complete': False}

    monkeypatch.setattr('agent_py_agent.agent.tooling._filesystem_read.reconcile_owner_quota_slice', unfinished)
    result = _write_tool(owner).execute({'path': 'tasks/2026-11-17/stask-large/work/q1.txt', 'content': 'one'})
    assert not result.ok and result.error_code == 'OWNER_QUOTA_UNAVAILABLE'
    assert not (work / 'q1.txt').exists()
    assert calls and all(call['max_entries'] == 4096 and 0 < call['max_ms'] <= 200 for call in calls)
