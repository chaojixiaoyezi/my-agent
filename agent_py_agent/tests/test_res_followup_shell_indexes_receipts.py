"""RES-FOLLOWUP-C：仅在临时目录验证私有挂载点与全局索引投影自愈。"""
from __future__ import annotations

import json
import shutil
import stat
from dataclasses import replace

import pytest

from agent_py_agent.agent.attempt.sandbox import AttemptExecutionSandbox
from agent_py_agent.agent.tooling import shell
from agent_py_agent.agent.user_space import home_indexes as indexes
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home


class _LinuxArgvOnlySandbox(AttemptExecutionSandbox):
    """复用真实Linux参数构造，不把本机平台/沙箱可用性冒充实际进程验证。"""

    def __init__(self, spec):
        super().__init__(replace(spec, bwrap_path='/usr/bin/bwrap'))

    def require_ready(self):
        return None

    def build_argv(self, command_argv):
        return self._linux_argv(command_argv)


@pytest.mark.parametrize('full_access', [False, True])
def test_deleted_private_mount_is_recreated_before_linux_argv(tmp_path, full_access):
    owner = ensure_owner_home(tmp_path / 'home', OwnerIdentity.provider_user('chat', 'alice')).home_dir
    private = owner / 'data' / 'private_interactions'
    shutil.rmtree(private)

    argv, use_shell = shell._sandbox_exec(
        'true', owner, None if full_access else owner, owner,
        sandbox_factory=_LinuxArgvOnlySandbox,
    )

    assert private.is_dir(), 'deleted private mount point must be restored before path projection'
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    assert not use_shell
    assert any(argv[i:i + 4] == ['--tmpfs', str(private), '--remount-ro', str(private)]
               for i in range(len(argv) - 3)), 'private path must still have a real hidden mount'


def test_private_mount_recreation_rejects_symlink_parent(tmp_path):
    owner = ensure_owner_home(tmp_path / 'home', OwnerIdentity.provider_user('chat', 'alice')).home_dir
    data = owner / 'data'
    data.rename(owner / 'data-original')
    outside = tmp_path / 'outside'
    outside.mkdir()
    data.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        shell._sandbox_exec('true', owner, owner, owner, sandbox_factory=_LinuxArgvOnlySandbox)
    assert list(outside.iterdir()) == []


def _registrations(tmp_path, kind):
    """给四种公开登记入口各准备两条不同实体，不直接往私有缓存塞测试值。"""
    home = home_paths(tmp_path / 'home')
    owners = tuple(ensure_owner_home(home.root, OwnerIdentity.provider_user('chat', name))
                   for name in ('alice', 'bob'))
    refs = {
        'owners': owners,
        'tasks': tuple(indexes.TaskIndexRef(owner.owner_id, 'task', owner.tasks_dir / 'task', 'running')
                       for owner in owners),
        'runs': tuple(indexes.RunIndexRef(owner.owner_id, 'run', 'task', owner.runs_dir / 'run', 'running')
                      for owner in owners),
        'agents': tuple(indexes.AgentIndexRef(owner.owner_id, 'agent', 'task', owner.agents_dir / 'agent', 'running')
                        for owner in owners),
    }
    register, path = {
        'owners': (indexes.register_owner_ref, home.global_index_owners_jsonl),
        'tasks': (indexes.register_task_ref, home.global_index_active_tasks_jsonl),
        'runs': (indexes.register_run_ref, home.global_index_active_runs_jsonl),
        'agents': (indexes.register_agent_ref, home.global_index_active_agents_jsonl),
    }[kind]
    return home, refs, path, lambda: [register(home, ref) for ref in refs[kind]]


@pytest.mark.parametrize('kind', ['owners', 'tasks', 'runs', 'agents'])
@pytest.mark.parametrize('mutation', ['missing', 'replaced'])
def test_unchanged_registrations_restore_deleted_or_replaced_index(tmp_path, kind, mutation):
    _home, _refs, path, register = _registrations(tmp_path, kind)
    expected = register()
    before = path.read_bytes()
    register()
    assert path.read_bytes() == before, 'unchanged registrations must still dedupe while file identity is stable'
    identity = path.stat()
    if mutation == 'missing':
        path.unlink()
    else:
        path.rename(path.with_suffix('.saved'))
        path.write_bytes(b'')
        assert (identity.st_dev, identity.st_ino) != (path.stat().st_dev, path.stat().st_ino)

    register()

    assert path.is_file(), 'warm fingerprint cache must not suppress recreation'
    actual = [json.loads(line) for line in path.read_text().splitlines()]
    def without_time(rows):
        return [{key: value for key, value in row.items() if key != 'updated_at'} for row in rows]

    assert without_time(actual) == without_time(expected), 'all cached entities for this index must be republished'
    restored = path.read_bytes()
    register()
    assert path.read_bytes() == restored
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize('kind', ['owners', 'tasks', 'runs', 'agents'])
def test_snapshot_reseeded_cache_also_tracks_file_identity(tmp_path, kind):
    home, refs, path, register = _registrations(tmp_path, kind)
    indexes.replace_home_index_snapshots(
        home, owners=refs['owners'], task_refs=refs['tasks'],
        run_refs=refs['runs'], agent_refs=refs['agents'],
    )
    before = path.read_bytes()
    register()
    assert path.read_bytes() == before, 'snapshot cache seeding must retain change-only writes'
    path.unlink()

    register()

    assert path.is_file(), 'snapshot-seeded fingerprints must not survive a deleted projection'
    assert len(path.read_text().splitlines()) == 2


def test_replacing_one_index_does_not_disable_dedupe_for_another(tmp_path):
    home = home_paths(tmp_path / 'home')
    task = indexes.TaskIndexRef('chat/alice', 'task', tmp_path / 'task', 'running')
    run = indexes.RunIndexRef('chat/alice', 'run', 'task', tmp_path / 'run', 'running')
    indexes.register_task_ref(home, task)
    indexes.register_run_ref(home, run)
    run_before = home.global_index_active_runs_jsonl.read_bytes()
    home.global_index_active_tasks_jsonl.unlink()

    indexes.register_task_ref(home, task)
    indexes.register_run_ref(home, run)

    assert home.global_index_active_tasks_jsonl.is_file()
    assert home.global_index_active_runs_jsonl.read_bytes() == run_before
