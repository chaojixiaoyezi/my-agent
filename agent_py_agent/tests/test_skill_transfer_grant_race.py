"""真实原registry与锁验证来源撤回和目标提交可线性化，不造第二授权账。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from threading import Barrier, Event, current_thread

import pytest

from agent_py_agent.agent.capability import skill_transfer, skill_transfer_import
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_skill_transfer import control, prepare
from agent_py_agent.tests.test_skill_transfer import transfer_case as _base_transfer_case


@pytest.fixture
def transfer_case(tmp_path,monkeypatch):
    return _base_transfer_case.__wrapped__(tmp_path,monkeypatch)


def test_revoke_cannot_report_success_before_inflight_accept_commits(transfer_case,monkeypatch):
    source,target,*_ = transfer_case
    offer = prepare(transfer_case)
    assert control(source,'authorize',offer['reference']).ok
    entered,released,revoke_attempt = Event(),Event(),Event()
    write = skill_transfer_import._write_entries
    lock = SkillLearningStore.state_lock
    def paused_write(host,*args):
        if host is target:
            entered.set()
            assert released.wait(5)
        return write(host,*args)
    @contextmanager
    def observed_lock(store):
        if store.skills_root.parent == source.home_paths.owner_home_dir and current_thread().name.startswith('revoke'):
            revoke_attempt.set()
        with lock(store) as admission:
            yield admission
    monkeypatch.setattr(skill_transfer_import,'_write_entries',paused_write)
    monkeypatch.setattr(SkillLearningStore,'state_lock',observed_lock)
    with ThreadPoolExecutor(max_workers=1,thread_name_prefix='accept') as accepter, ThreadPoolExecutor(max_workers=1,thread_name_prefix='revoke') as revoker:
        accepted = accepter.submit(control,target,'accept',offer['reference'])
        assert entered.wait(5)
        revoked = revoker.submit(control,source,'revoke',offer['reference'])
        try:
            assert revoke_attempt.wait(5)
            assert not revoked.done(), '来源撤回不能在目标尚未写入时先报告成功'
        finally:
            released.set()
        assert accepted.result(timeout=5).ok and revoked.result(timeout=5).ok
    assert len(SkillLearningStore.for_home(target.home_paths).load_registry().skills) == 1


def test_revoke_completed_before_accept_lock_prevents_import(transfer_case,monkeypatch):
    source,target,*_ = transfer_case
    offer = prepare(transfer_case)
    assert control(source,'authorize',offer['reference']).ok
    entered,released = Event(),Event()
    verify = skill_transfer._verify_reference
    def pause_first_verify(root,reference):
        result = verify(root,reference)
        if current_thread().name.startswith('accept') and not entered.is_set():
            entered.set()
            assert released.wait(5)
        return result
    monkeypatch.setattr(skill_transfer,'_verify_reference',pause_first_verify)
    with ThreadPoolExecutor(max_workers=1,thread_name_prefix='accept') as pool:
        accepted = pool.submit(control,target,'accept',offer['reference'])
        assert entered.wait(5)
        assert control(source,'revoke',offer['reference']).ok
        released.set()
        assert not accepted.result(timeout=5).ok
    assert not SkillLearningStore.for_home(target.home_paths).load_registry().skills


def test_opposite_owner_transfers_have_one_global_lock_order(transfer_case,monkeypatch):
    source,target,*_ = transfer_case
    first = prepare(transfer_case)
    assert control(source,'authorize',first['reference']).ok
    path = target.home_paths.owner_home_dir/'skills'/'other'/'SKILL.md'
    path.parent.mkdir(parents=True)
    path.write_text('---\nname: other\ndescription: 安全方法\n---\n\n独立方法。\n')
    entry = target.skills_service.snapshot_for(force_reload=True).resolve('other')
    reverse = skill_transfer.prepare_transfer(target,asdict(OwnerIdentity.provider_user('feishu','ou_source')),
        [{'stable_id':entry.stable_id,'content_sha256':entry.content_sha256}])
    assert control(target,'authorize',reverse['reference']).ok
    start = Barrier(2)
    verify = skill_transfer._verify_reference
    seen = set()
    def simultaneous_verify(root,reference):
        result = verify(root,reference)
        name = current_thread().name
        if name.startswith('opposite') and name not in seen:
            seen.add(name)
            start.wait(timeout=5)
        return result
    monkeypatch.setattr(skill_transfer,'_verify_reference',simultaneous_verify)
    with ThreadPoolExecutor(max_workers=2,thread_name_prefix='opposite') as pool:
        a = pool.submit(control,target,'accept',first['reference'])
        b = pool.submit(control,source,'accept',reverse['reference'])
        assert a.result(timeout=10).ok and b.result(timeout=10).ok


def test_export_resource_never_promotes_nested_parent_to_trusted_root(transfer_case,tmp_path,monkeypatch):
    from agent_py_agent.agent.capability import skill_transfer_manifest
    source,target,*_ = transfer_case
    parent = source.home_paths.owner_home_dir/'skills'/'nested'
    skill = parent/'sample2'
    skill.mkdir(parents=True)
    (skill/'SKILL.md').write_text('---\nname: sample2\ndescription: 安全方法\n---\n\n安全方法。\n')
    (skill/'asset.txt').write_text('allowed')
    entry = source.skills_service.snapshot_for(force_reload=True).resolve('sample2')
    outside = tmp_path/'outside'/'sample2'
    outside.mkdir(parents=True)
    (outside/'asset.txt').write_text('PRIVATE_OUTSIDE_OWNER_MARKER')
    read = skill_transfer_manifest._read_export_file
    def swapped(root,parts):
        raw = read(root,parts)
        if tuple(parts) == ('nested','sample2','SKILL.md'):
            parent.rename(parent.with_name('nested.saved'))
            parent.symlink_to(outside.parent,target_is_directory=True)
        return raw
    monkeypatch.setattr(skill_transfer_manifest,'_read_export_file',swapped)
    with pytest.raises(skill_transfer_manifest.SkillTransferError):
        skill_transfer.prepare_transfer(source,asdict(OwnerIdentity.provider_group('feishu','oc_target')),
            [{'stable_id':entry.stable_id,'content_sha256':entry.content_sha256,'resources':['asset.txt']}])
    assert (outside/'asset.txt').read_text() == 'PRIVATE_OUTSIDE_OWNER_MARKER'
    assert not SkillLearningStore.for_home(source.home_paths).load_registry().import_operations


def test_rollback_never_removes_outside_tree_through_parent_link(transfer_case,tmp_path):
    import hashlib

    source,target,*_ = transfer_case
    store = SkillLearningStore.for_home(target.home_paths)
    outside = tmp_path/'outside'/'sample'
    outside.mkdir(parents=True)
    raw = b'synthetic identical file'
    (outside/'SKILL.md').write_bytes(raw)
    store.learned_root.symlink_to(outside.parent,target_is_directory=True)
    operation = {'target_names':['sample'],'files':{'sample':{'SKILL.md':hashlib.sha256(raw).hexdigest()}}}
    with pytest.raises((OSError,ValueError)):
        skill_transfer_import._rollback_files(store,operation)
    assert (outside/'SKILL.md').read_bytes() == raw


def test_staging_cleanup_never_removes_outside_tree_through_parent_link(transfer_case,tmp_path,monkeypatch):
    source,target,*_ = transfer_case
    offer = prepare(transfer_case)
    assert control(source,'authorize',offer['reference']).ok
    name = offer['manifest']['skills'][0]['name']
    outside = tmp_path/'outside'/name
    outside.mkdir(parents=True)
    marker = outside/'private-file.txt'
    marker.write_text('outside')
    def swapped_parse(path,**kwargs):
        staging = path.parent.parent
        staging.rename(staging.with_name('transfer_staging.saved'))
        staging.symlink_to(outside.parent,target_is_directory=True)
        raise ValueError('synthetic failure')
    monkeypatch.setattr(skill_transfer_import,'parse_skill_file',swapped_parse)
    assert not control(target,'accept',offer['reference']).ok
    assert marker.read_text() == 'outside'


def test_transfer_schema_is_deferred_but_discoverable():
    from agent_py_agent.agent.tooling.skill_transfer_tool import SkillTransferTool
    hints = SkillTransferTool.model_spec.hints
    assert hints.default_deferred is True and hints.category == 'capability'
    assert hints.deferred_summary and 'transfer' in hints.keywords
