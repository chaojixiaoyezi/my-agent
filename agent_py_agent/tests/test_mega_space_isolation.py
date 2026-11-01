"""Owner/space/project boundaries, real quota and private IO fault evidence for W1."""
from __future__ import annotations

import base64
import json
import os
import stat
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaExceeded
from agent_py_agent.tests.test_business_v12_f2_spaces import World, assert_code, module


def context(world, sid=None, *, project=None, binding=None, methods=(), assets=()):
    return module('business_context').resolve_business_context(actor=world.actor, task_id='stask-canonical',
        requested_space_id=sid, project_ref=project, binding_id=binding,
        requested_method_refs=methods, requested_asset_version_refs=assets)


def register(world, sid, *, ref='v1', project=None):
    world.add_ref('artifact_version', ref, space_id=sid, project_ref=project, artifact_id='artifact', content_sha256='a' * 64)
    params = {'space_id': sid, 'name': '角色', 'kind': 'character',
              'artifact_ref': {'artifact_id': 'artifact', 'version_ref': ref, 'content_sha256': 'a' * 64}}
    if project:
        world.add_ref('project', project, space_id=sid)
        params['project_ref'] = project
    return world.call('assets.register', params, key='register-' + ref)['asset_id']


def test_two_real_owner_roots_are_separate_and_actor_cannot_switch_service(tmp_path):
    a, b = World(tmp_path / 'a', 'owner-a'), World(tmp_path / 'b', 'owner-b')
    sa, sb = a.create()['space_id'], b.create()['space_id']
    assert sa != sb
    assert a.call('spaces.list')['items'][0]['space_id'] == sa
    assert b.call('spaces.list')['items'][0]['space_id'] == sb
    assert_code('SPACE_NOT_FOUND', lambda: a.service.get(sb, actor=a.actor))
    assert_code('SPACE_SCOPE_MISMATCH', lambda: b.service.get(sb, actor=a.actor))
    assert a.service.state_path != b.service.state_path


def test_same_owner_wrong_space_asset_and_controller_are_rejected(tmp_path):
    world = World(tmp_path / 'owner')
    one, two = world.create('一', key='one')['space_id'], world.create('二', key='two')['space_id']
    aid = register(world, two)
    assert_code('SPACE_SCOPE_MISMATCH', lambda: world.call('assets.get', {'space_id': one, 'asset_id': aid}))
    world.add_ref('surface', 'panel')
    world.add_ref('agent', 'agent-two', space_id=two)
    assert_code('SPACE_SCOPE_MISMATCH', lambda: world.call('interfaces.bind', {'surface_ref': 'panel', 'name': '入口',
        'space_ids': [one], 'default_space_id': one, 'controller': {'mode': 'agent', 'agent_id': 'agent-two'}}, key='bind'))


@pytest.mark.parametrize('kind', ['package', 'project', 'surface', 'method'])
def test_foreign_owner_reference_never_becomes_local_permission(tmp_path, kind):
    world = World(tmp_path / 'owner')
    sid = world.create()['space_id']
    world.add_ref(kind, 'foreign', owner='owner-b')
    before = world.service.state_path.read_bytes()
    def call():
        if kind == 'package':
            return world.call('spaces.packages.attach', {'space_id': sid, 'package_refs': ['foreign']}, revision=0, key='foreign')
        if kind == 'project':
            return context(world, sid, project='foreign')
        if kind == 'surface':
            return world.call('interfaces.bind', {'surface_ref': 'foreign', 'name': '入口', 'space_ids': [sid],
                'default_space_id': sid, 'controller': {'mode': 'global'}}, key='foreign')
        return context(world, sid, methods=('foreign',))
    assert_code('SPACE_SCOPE_MISMATCH', call)
    assert before == world.service.state_path.read_bytes()


def test_context_ambiguity_has_no_fallback_and_never_creates_task(tmp_path):
    world = World(tmp_path / 'owner')
    assert_code('CONTEXT_REQUIRED', lambda: context(world))
    one = world.create('一', key='one')['space_id']
    assert context(world).space_id == one
    two = world.create('二', key='two')['space_id']
    assert_code('CONTEXT_REQUIRED', lambda: context(world))
    world.denied_spaces.add(two)
    assert context(world).space_id == one
    assert_code('BUSINESS_ACCESS_DENIED', lambda: context(world, two))
    assert not (world.root / 'conversations').exists() and not (world.root / 'runs').exists()


def test_default_project_methods_and_asset_selection_are_exact_snapshots(tmp_path):
    world = World(tmp_path / 'owner')
    world.add_ref('project', 'project-one')
    world.add_ref('package', 'package-one')
    sid = world.create(default_project_ref='project-one', mounts=[{'package_ref': 'package-one'}])['space_id']
    aid = register(world, sid, project='project-one')
    world.call('assets.versions.accept', {'space_id': sid, 'asset_id': aid, 'version_ref': 'v1'}, revision=0, key='accept')
    snapshot = context(world, sid)
    assert snapshot.task_id == 'stask-canonical' and snapshot.project_ref == 'project-one'
    assert snapshot.selected_method_refs == ('package-one',) and snapshot.asset_version_refs == ('v1',)
    world.add_ref('project', 'project-two', space_id=sid)
    world.service.update(sid, {'name': '改名', 'default_project_ref': 'project-two'}, actor=world.actor,
                         expected_revision=0, idempotency_key='new-default')
    assert snapshot.space_revision == 0 and snapshot.project_ref == 'project-one'
    assert context(world, sid).project_ref == 'project-two' and context(world, sid).asset_version_refs == ()
    assert_code('REFERENCE_NOT_FOUND', lambda: context(world, sid, project='project-two', assets=('v1',)))


def test_current_hash_and_visibility_are_rechecked_without_copying_media(tmp_path):
    world = World(tmp_path / 'owner')
    sid = world.create()['space_id']
    aid = register(world, sid)
    world.call('assets.versions.accept', {'space_id': sid, 'asset_id': aid, 'version_ref': 'v1'}, revision=0, key='accept')
    before = world.service.state_path.read_bytes()
    world.refs['artifact_version', 'v1']['content_sha256'] = 'b' * 64
    assert_code('REFERENCE_NOT_FOUND', lambda: context(world, sid))
    assert_code('REFERENCE_NOT_FOUND', lambda: world.call('assets.get', {'space_id': sid, 'asset_id': aid}))
    assert before == world.service.state_path.read_bytes() and 'data_base64' not in before.decode()


def test_unlink_is_blocked_by_original_active_work_and_does_not_delete_version(tmp_path):
    world = World(tmp_path / 'owner')
    sid = world.create()['space_id']
    aid = register(world, sid)
    world.blocks = [{'ref': 'task-active', 'kind': 'task', 'reason_code': 'ASSET_IN_USE'}]
    assert world.call('assets.unlink', {'space_id': sid, 'asset_id': aid}, revision=0, key='unlink')['state'] == 'blocked'
    world.blocks = []
    result = world.call('assets.unlink', {'space_id': sid, 'asset_id': aid}, revision=0, key='unlink')
    assert result['state'] == 'unlinked' and ('artifact_version', 'v1') in world.refs
    assert world.call('assets.unlink', {'space_id': sid, 'asset_id': aid}, revision=0, key='unlink') == result


def test_actor_type_and_projection_tags_cannot_grant_authority(tmp_path):
    world = World(tmp_path / 'owner')
    sid = world.create()['space_id']
    forged = {'owner_id': world.owner, 'subject_id': world.actor.subject_id, 'role': 'human', 'allowed_actions': ['*']}
    assert_code('ACTOR_REQUIRED', lambda: world.service.get(sid, actor=forged))
    world.denied_spaces.add(sid)
    assert_code('BUSINESS_ACCESS_DENIED', lambda: world.service.get(sid, actor=world.actor))
    assert world.call('spaces.list')['items'] == []
    wrong = replace(world.actor, owner_id='owner-b')
    assert_code('SPACE_SCOPE_MISMATCH', lambda: world.service.get(sid, actor=wrong))


def test_cursor_cannot_cross_owner_subject_filter_or_be_forged(tmp_path):
    a, b = World(tmp_path / 'a', 'owner-a'), World(tmp_path / 'b', 'owner-b')
    for i in range(2):
        a.create(str(i), key=str(i))
        b.create(str(i), key=str(i))
    first = a.call('spaces.list', {'limit': 1})
    assert_code('CURSOR_EXPIRED', lambda: b.call('spaces.list', {'limit': 1, 'cursor': first['next_cursor']}))
    actor = replace(a.actor, subject_id='another-subject')
    assert_code('CURSOR_EXPIRED', lambda: a.service.list({'limit': 1, 'cursor': first['next_cursor']}, actor=actor))
    decoded = json.loads(base64.urlsafe_b64decode(first['next_cursor']))
    decoded['position'] = 0
    forged = base64.urlsafe_b64encode(json.dumps(decoded).encode()).decode()
    assert_code('CURSOR_EXPIRED', lambda: a.call('spaces.list', {'limit': 1, 'cursor': forged}))


def test_private_birth_modes_and_nofollow_descriptor_boundary(tmp_path, monkeypatch, record_testsuite_property):
    from agent_py_agent.agent.common import nofollow_fs as fs

    world = World(tmp_path / 'owner')
    sid = world.create()['space_id']
    assert stat.S_IMODE(world.service.state_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(world.service.state_path.parent.stat().st_mode) == 0o700
    try:
        os.link(world.service.state_path, world.root / 'catalog-hardlink')
    except PermissionError as error:
        # 原生样本确实被拒绝，不能绕开OS限制或声称原生硬链验证成功。精确注入一个
        # fstat多链接事实，仍运行真实服务→原nofollow→close，且必须在读正文前拒绝。
        record_testsuite_property('w1_native_hardlink', f'NOT_VERIFIED: os.link denied errno={error.errno}; descriptor fault injection only')
        original = os.fstat
        identity = world.service.state_path.stat().st_ino
        descriptors = []
        def linked(descriptor):
            metadata = original(descriptor)
            if metadata.st_ino == identity:
                values = list(metadata)
                values[3] = 2
                descriptors.append(descriptor)
                return os.stat_result(values)
            return metadata
        def forbidden_read(*args, **kwargs):
            raise AssertionError('unsafe descriptor reached body read')
        with monkeypatch.context() as patch:
            patch.setattr(os, 'fstat', linked)
            patch.setattr(fs, '_read_descriptor', forbidden_read)
            with pytest.raises(fs.NoFollowPathError):
                world.service.get(sid, actor=world.actor)
        assert descriptors
        for descriptor in descriptors:
            with pytest.raises(OSError):
                original(descriptor)
        return
    record_testsuite_property('w1_native_hardlink', 'VERIFIED: real multi-link leaf rejected')
    with pytest.raises((OSError, ValueError)):
        world.service.get(sid, actor=world.actor)


def test_symlinked_internal_directory_never_writes_to_target(tmp_path):
    world = World(tmp_path / 'owner')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (world.root / 'data').symlink_to(outside, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        world.create()
    assert list(outside.iterdir()) == []


def test_corrupt_catalog_is_not_replaced_with_empty_success(tmp_path):
    world = World(tmp_path / 'owner')
    world.create()
    world.service.state_path.write_bytes(b'{broken')
    assert_code('STORE_UNAVAILABLE', lambda: world.create('new', key='new'))
    assert world.service.state_path.read_bytes() == b'{broken'


def test_structurally_corrupt_row_is_a_safe_store_failure(tmp_path):
    world = World(tmp_path / 'owner')
    sid = world.create()['space_id']
    state = json.loads(world.service.state_path.read_bytes())
    state['spaces'][sid].pop('name')
    world.service.state_path.write_text(json.dumps(state))
    assert_code('STORE_UNAVAILABLE', lambda: world.call('spaces.list'))


def test_real_100mb_quota_rejects_growth_without_partial_business_commit(tmp_path):
    world = World(tmp_path / 'owner')
    (world.root / 'quota.json').write_text(json.dumps({'max_disk_mb': 100}))
    world.create()
    before = world.service.state_path.read_bytes()
    with (world.root / 'production-sized-sparse.bin').open('wb') as handle:
        handle.truncate(100 * 1024 * 1024)
    with pytest.raises(OwnerQuotaExceeded):
        world.create('over-quota', key='over-quota')
    assert before == world.service.state_path.read_bytes()


def test_real_atomic_writer_failure_before_and_after_commit_replays_once(tmp_path, monkeypatch):
    world = World(tmp_path / 'owner')
    spaces = module('business_spaces')
    real = spaces.write_bytes_atomic_beneath
    calls = []
    def after(root, parts, data):
        real(root, parts, data)
        calls.append(True)
        raise OSError('simulated directory fsync failure after rename')
    with monkeypatch.context() as patch:
        patch.setattr(spaces, 'write_bytes_atomic_beneath', after)
        with pytest.raises(OSError):
            world.create()
    result = world.create()
    assert len(calls) == 1 and result['space_id'] == world.call('spaces.list')['items'][0]['space_id']
    committed = world.service.state_path.read_bytes()
    def before(*args, **kwargs):
        raise OSError('simulated pre-replace IO failure')
    with monkeypatch.context() as patch:
        patch.setattr(spaces, 'write_bytes_atomic_beneath', before)
        with pytest.raises(OSError):
            world.create('not-committed', key='before')
    assert committed == world.service.state_path.read_bytes()


def test_live_authority_is_rechecked_after_real_quota_reservation(tmp_path, monkeypatch):
    world = World(tmp_path / 'owner')
    (world.root / 'quota.json').write_text(json.dumps({'max_disk_mb': 100}))
    sid = world.create()['space_id']
    before = world.service.state_path.read_bytes()
    spaces = module('business_spaces')
    real = spaces.owner_quota_enforcer_from_policy
    class Gate:
        @contextmanager
        def admission(self):
            with real(world.root).admission() as admission:
                def check(changes):
                    result = admission.check(changes)
                    world.denied.add('spaces.update')
                    return result
                yield SimpleNamespace(check=check)
    monkeypatch.setattr(spaces, 'owner_quota_enforcer_from_policy', lambda root: Gate())
    assert_code('BUSINESS_ACCESS_DENIED', lambda: world.service.update(sid, {'name': 'revoked'}, actor=world.actor,
        expected_revision=0, idempotency_key='revoked'))
    assert before == world.service.state_path.read_bytes()


def test_content_gate_revoked_during_real_quota_reservation_does_not_adopt(tmp_path, monkeypatch):
    world = World(tmp_path / 'owner')
    (world.root / 'quota.json').write_text(json.dumps({'max_disk_mb': 100}))
    sid = world.create()['space_id']
    aid = register(world, sid)
    permission = {'allowed': True}
    world.actor = replace(world.actor, content_authorizer=lambda facts: permission['allowed'])
    before = world.service.state_path.read_bytes()
    spaces = module('business_spaces')
    real = spaces.owner_quota_enforcer_from_policy
    class Gate:
        @contextmanager
        def admission(self):
            with real(world.root).admission() as admission:
                def check(changes):
                    result = admission.check(changes)
                    permission['allowed'] = False
                    return result
                yield SimpleNamespace(check=check)
    monkeypatch.setattr(spaces, 'owner_quota_enforcer_from_policy', lambda root: Gate())
    assert_code('HUMAN_RESPONSE_REQUIRED', lambda: world.call('assets.versions.accept',
        {'space_id': sid, 'asset_id': aid, 'version_ref': 'v1'}, revision=0, key='adopt-revoked'))
    assert before == world.service.state_path.read_bytes()
    assert world.call('assets.get', {'space_id': sid, 'asset_id': aid})['accepted_version'] is None


def test_space_detail_cannot_silently_truncate_reverse_bindings(tmp_path):
    from copy import deepcopy

    world = World(tmp_path / 'owner')
    sid = world.create()['space_id']
    world.add_ref('surface', 'panel')
    binding = world.call('interfaces.bind', {'surface_ref': 'panel', 'name': '入口', 'space_ids': [sid],
        'default_space_id': sid, 'controller': {'mode': 'global'}}, key='seed')['binding']
    state = json.loads(world.service.state_path.read_bytes())
    original = state['bindings'][binding['binding_id']]
    # 从真实单一绑定记录构造合法的大历史样本，避免用101次创建代替对读入口的检查。
    for i in range(100):
        row = deepcopy(original)
        row.update(binding_id=f'binding-{i}', surface_ref=f'panel-{i}')
        world.add_ref('surface', f'panel-{i}')
        state['bindings'][row['binding_id']] = row
    world.service.state_path.write_text(json.dumps(state))
    assert_code('RESULT_LIMIT_EXCEEDED', lambda: world.service.get(sid, actor=world.actor))
    page = world.call('interfaces.list', {'space_id': sid, 'limit': 100})
    assert len(page['items']) == 100 and page['coverage'] == 'partial' and page['next_cursor']
