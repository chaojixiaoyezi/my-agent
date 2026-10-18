"""具体全局写入口的原模式拒绝合同；默认正例走真文件，非真实Gateway/管理员验收。"""
import hashlib
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.global_write_guard import GlobalWriteInWorkerError
from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
from agent_py_agent.agent.gateway_parts.local_client_token import ensure_local_client_credential
from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition
from agent_py_agent.agent.settings.config_io import _write_config_lines
from agent_py_agent.agent.settings.model_profiles import _save_profiles, model_profiles_path
from agent_py_agent.agent.settings.shared_model_catalog import _save_catalog
from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity
from agent_py_agent.agent.user_space.home_indexes import register_owner_ref
from agent_py_agent.agent.user_space.identity_store import (
    CanonicalMemoryNoteRequest,
    ProviderIdentityRecord,
    ensure_canonical_user_profile,
    link_provider_identity,
    write_canonical_memory_note,
)
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home


# LLM: 只用于核副作用是否真的未发生，不打印文件内容或凭据；整个树明确为pytest临时输入。
# 函数用途: 收集临时目录名、权限和文件摘要，发现拒绝前的mkdir/锁文件或半写。
def physical_snapshot(root):
    return {str(path.relative_to(root)): (path.stat().st_mode,
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
            for path in sorted(root.rglob('*'))}


# LLM: 输入由测试者在受限模式前构造，所有物理路径先断言隔离；原调用不换handler或伪造成功。
# 函数用途: 为各全局入口提供正常参数，不用坏参数或缺权限掩盖worker拒绝缺失。
def original_write(kind, root):
    assert root.is_absolute()
    config = root / 'config'
    config.mkdir()
    home = SimpleNamespace(root=root, config_dir=config, provider_identity_dir=root / 'provider-identities')
    actions = {
        'config': lambda: _write_config_lines(config / 'agent.yaml', ['request_timeout: 12']),
        'models': lambda: _save_catalog(home, {'profiles': []}),
        'admin_binding': lambda: bind_admin_channel_identity(root, 'feishu', 'isolated-boundary-user'),
        'provider_identity': lambda: link_provider_identity(home, ProviderIdentityRecord('feishu', 'subject', 'user', 'owner')),
        'credential': lambda: ensure_local_client_credential(root),
    }
    return actions[kind]


@pytest.mark.parametrize('kind', ['config', 'models', 'admin_binding', 'provider_identity', 'credential'])
def test_worker_cannot_directly_write_global_facts(kind, tmp_path, monkeypatch):
    write = original_write(kind, tmp_path)
    before = physical_snapshot(tmp_path)
    monkeypatch.setattr(mode, '_WORKER_PARTITION', WorkerPartition(0, 2, 'boundary-C'))
    with pytest.raises(GlobalWriteInWorkerError) as error:
        write()
    assert error.value.error_code == 'GLOBAL_WRITE_IN_WORKER'
    assert physical_snapshot(tmp_path) == before


@pytest.mark.parametrize('kind', ['config', 'models', 'admin_binding', 'provider_identity', 'credential'])
def test_default_mode_keeps_actual_original_global_writer(kind, tmp_path, monkeypatch):
    write = original_write(kind, tmp_path)
    before = physical_snapshot(tmp_path)
    monkeypatch.setattr(mode, '_WORKER_PARTITION', None)
    write()
    assert physical_snapshot(tmp_path) != before


def test_worker_can_save_own_private_model_profile(tmp_path, monkeypatch):
    home = SimpleNamespace(config_dir=tmp_path / 'config', owner_provider='test', owner_kind='user', owner_id='test-user')
    path = model_profiles_path(home)
    assert path.is_relative_to(tmp_path)
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(mode, '_WORKER_PARTITION', WorkerPartition(0, 2, 'boundary-C'))
    _save_profiles(path, {'profiles': {}, 'providers': {}})
    assert path.is_file() and path.read_text().count('catalog_generation') == 1


def test_worker_keeps_original_controlled_owner_index_append(tmp_path, monkeypatch):
    home = SimpleNamespace(root=tmp_path, global_index_owners_jsonl=tmp_path / 'global_index/owners.jsonl')
    owner = resolve_owner_home(tmp_path, OwnerIdentity.provider_user('test', 'index-user'))
    assert owner.home_dir.is_relative_to(tmp_path)
    monkeypatch.setattr(mode, '_WORKER_PARTITION', WorkerPartition(0, 2, 'boundary-C'))
    row = register_owner_ref(home, owner)
    assert row['owner_id'] == owner.owner_id and home.global_index_owners_jsonl.is_file()


def test_worker_existing_canonical_profile_keeps_source_owner_note_content(tmp_path, monkeypatch):
    home = SimpleNamespace(root=tmp_path, canonical_users_dir=tmp_path / 'canonical-users')
    profile = ensure_canonical_user_profile(home, 'isolated-canonical')
    before = profile.read_bytes()
    monkeypatch.setattr(mode, '_WORKER_PARTITION', WorkerPartition(0, 2, 'boundary-C'))
    note = write_canonical_memory_note(home, CanonicalMemoryNoteRequest(
        'isolated-canonical', 'fact', '合成内容', 'providers/test/users/note-owner'))
    assert note.path.is_relative_to(tmp_path) and note.path.is_file() and profile.read_bytes() == before


def test_worker_note_cannot_create_missing_canonical_identity(tmp_path, monkeypatch):
    home = SimpleNamespace(root=tmp_path, canonical_users_dir=tmp_path / 'canonical-users')
    before = physical_snapshot(tmp_path)
    monkeypatch.setattr(mode, '_WORKER_PARTITION', WorkerPartition(0, 2, 'boundary-C'))
    with pytest.raises(GlobalWriteInWorkerError):
        write_canonical_memory_note(home, CanonicalMemoryNoteRequest(
            'missing-canonical', 'fact', '合成内容', 'providers/test/users/note-owner'))
    assert physical_snapshot(tmp_path) == before
