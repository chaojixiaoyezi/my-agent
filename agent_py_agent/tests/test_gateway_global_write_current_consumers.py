# LLM: 只诊断冻结产品；真实临时C备料/P4池/Echo，不改读门或全局门，不启Gateway/模型/native进程。
# 模块用途: 检查W构造及模型Skill读取是否暗写共享树，并记录due-index实际越界消费的原红。

import pytest

from agent_py_agent.agent.common.global_write_guard import GlobalWriteInWorkerError
from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
from agent_py_agent.agent.gateway_parts.multiprocess_worker_agent import create_worker_pool
from agent_py_agent.agent.gateway_parts.multiprocess_worker_resources import WorkerSchedulerDueIndex
from agent_py_agent.agent.scheduler import SchedulerDueIndex
from agent_py_agent.agent.settings.model_profiles import model_profiles_path, read_model_profiles
from agent_py_agent.agent.user_space.owner_resolver import ensure_owner_home
from agent_py_agent.tests.test_gateway_multiprocess_worker_agent import _shared_snapshot
from agent_py_agent.tests.test_gateway_multiprocess_worker_agent import worker as worker


def test_real_worker_construct_and_model_skill_reads_preserve_shared_bytes(worker):
    frozen, config, root, owner, _ = worker
    home = frozen.launch.owner_root
    before = _shared_snapshot(home)
    ensure_owner_home(home, owner)  # 仅原夹具的C准备输出，不称物理C生产链。
    agent = create_worker_pool(frozen, config, root).get(owner)
    assert type(agent.scheduler_repository.due_index) is WorkerSchedulerDueIndex
    profiles = read_model_profiles(model_profiles_path(agent.home_paths))
    assert profiles['profiles'] == {} and profiles['catalog_generation'] is None
    snapshot = agent.skills_service.snapshot_for()
    assert snapshot is not None
    assert _shared_snapshot(home) == before


@pytest.mark.parametrize('action', ['claim', 'repair'])
def test_real_worker_due_adapter_rejects_c_only_shared_mutations(worker, monkeypatch, action):
    frozen, config, root, owner, _ = worker
    ensure_owner_home(frozen.launch.owner_root, owner)
    agent = create_worker_pool(frozen, config, root).get(owner)
    index = agent.scheduler_repository.due_index
    assert type(index) is WorkerSchedulerDueIndex
    with monkeypatch.context() as setup:
        setup.setattr(mode, '_WORKER_PARTITION', None)
        original_c_index = SchedulerDueIndex(index.path)
        original_c_index.sync_owner({'provider': 'test', 'kind': 'user', 'id': 'other-index-owner'},
            {'jobs': {'synthetic-job': {'status': 'active', 'next_run_at': 100}}, 'runs': {}}, now=50)
    caught, result = None, None
    try:
        result = index.claim_due_owners(now=100, lease_seconds=60) if action == 'claim' else index.repair_legacy_owner_ledgers(frozen.launch.owner_root / 'owners')
    except Exception as error:
        caught = error
    facts = {'action': action, 'error_type': None if caught is None else type(caught).__name__,
        'worker_adapter': type(index).__name__, 'partition_present': mode.current_worker_partition() is not None,
        'result': [row.owner_id for row in result] if action == 'claim' and result is not None else result}
    assert isinstance(caught, GlobalWriteInWorkerError), facts


def test_count_zero_canonical_profile_keeps_original_directory_creation_and_reuse(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.user_space.identity_store import ensure_canonical_user_profile
    home = SimpleNamespace(canonical_users_dir=tmp_path / 'canonical-users')
    monkeypatch.setattr(mode, '_WORKER_PARTITION', None)
    profile = ensure_canonical_user_profile(home, 'synthetic')
    assert profile.parent.stat().st_mode & 0o777 == 0o700
    before = profile.read_bytes()
    profile.parent.chmod(0o755)
    assert ensure_canonical_user_profile(home, 'synthetic') == profile
    assert profile.read_bytes() == before and profile.parent.stat().st_mode & 0o777 == 0o755


def test_worker_existing_canonical_profile_is_read_only_without_chmod(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition
    from agent_py_agent.agent.user_space.identity_store import ensure_canonical_user_profile
    home = SimpleNamespace(canonical_users_dir=tmp_path / 'canonical-users')
    monkeypatch.setattr(mode, '_WORKER_PARTITION', None)
    profile = ensure_canonical_user_profile(home, 'synthetic')
    before = profile.read_bytes()
    profile.parent.chmod(0o755)
    monkeypatch.setattr(mode, '_WORKER_PARTITION', WorkerPartition(0, 2, 'synthetic-c'))
    assert ensure_canonical_user_profile(home, 'synthetic') == profile
    assert profile.read_bytes() == before and profile.parent.stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize('entry', ['admin_remove', 'admin_leaf', 'credential_leaf', 'canonical_link'])
def test_other_original_global_leaf_cannot_bypass_same_process_guard(tmp_path, monkeypatch, entry):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts.local_client_token import _ensure_credential_locked
    from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition
    from agent_py_agent.agent.user_space.admin_channel_identity import (
        _write_identities,
        remove_admin_channel_identity,
    )
    from agent_py_agent.agent.user_space.identity_store import (
        CanonicalIdentityLinkRequest,
        link_canonical_identity,
    )
    from agent_py_agent.tests.test_gateway_worker_global_write_boundaries import physical_snapshot
    home = SimpleNamespace(root=tmp_path, canonical_users_dir=tmp_path / 'canonical-users',
        linked_identities_jsonl=tmp_path / 'global_index/linked-identities.jsonl')
    calls = {
        'admin_remove': lambda: remove_admin_channel_identity(tmp_path, 'feishu', 'synthetic'),
        'admin_leaf': lambda: _write_identities(tmp_path, []),
        'credential_leaf': lambda: _ensure_credential_locked(tmp_path),
        'canonical_link': lambda: link_canonical_identity(home, CanonicalIdentityLinkRequest('synthetic', 'test', 'subject', 'owner')),
    }
    before = physical_snapshot(tmp_path)
    monkeypatch.setattr(mode, '_WORKER_PARTITION', WorkerPartition(0, 2, 'synthetic-c'))
    with pytest.raises(GlobalWriteInWorkerError):
        calls[entry]()
    assert physical_snapshot(tmp_path) == before


@pytest.mark.parametrize('action', ['claim', 'repair'])
def test_original_due_parent_class_also_refuses_worker(tmp_path, monkeypatch, action):
    from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition
    from agent_py_agent.tests.test_gateway_worker_global_write_boundaries import physical_snapshot
    monkeypatch.setattr(mode, '_WORKER_PARTITION', None)
    from contextlib import closing

    index = SchedulerDueIndex(tmp_path / 'scheduler.sqlite3')
    # 保原真实连接覆盖整个观察窗，GC关闭其他旧连接不能在rglob和stat之间删最后的SQLite共享文件。
    with closing(index._connect()) as observer:
        assert observer.execute("SELECT 1").fetchone()[0] == 1
        before = physical_snapshot(tmp_path)
        monkeypatch.setattr(mode, '_WORKER_PARTITION', WorkerPartition(0, 2, 'synthetic-c'))
        with pytest.raises(GlobalWriteInWorkerError):
            index.claim_due_owners(now=100) if action == 'claim' else index.repair_legacy_owner_ledgers(tmp_path / 'owners')
        assert physical_snapshot(tmp_path) == before
