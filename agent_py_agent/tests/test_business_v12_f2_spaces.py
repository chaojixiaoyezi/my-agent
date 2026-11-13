"""W1 F2: real private persistence; external reference authorities use explicit port fakes."""
from __future__ import annotations

import importlib
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


def module(name):
    path = 'agent_py_agent.agent.conversation.' + name
    assert importlib.util.find_spec(path) is not None, f'W1 domain is missing: {name}'
    return importlib.import_module(path)


class World:
    def __init__(self, root: Path, owner='owner-a', *, governance=None):
        root.mkdir(parents=True, exist_ok=True)
        self.root, self.owner = root, owner
        self.denied, self.denied_spaces, self.blocks = set(), set(), []
        self.refs = {}
        self.lookups = []
        self.service = module('business_spaces').BusinessSpaceService(
            root, owner_id=owner, reference_resolver=self.reference,
            active_references=lambda space_id, actor: list(self.blocks), governance=governance, enabled=True)
        self.actor = module('business_access').BusinessActor(owner, owner + '-user', self.service, self.authorize,
            content_authorizer=lambda facts: True)  # 原内容采用端口的contract fake，不代表真实人工批准。

    def authorize(self, action, scope):
        return (scope['owner_id'] == self.owner and action not in self.denied
                and scope.get('space_id') not in self.denied_spaces)

    def reference(self, kind, ref, actor):
        self.lookups.append((kind, ref))
        return self.refs.get((kind, ref))

    def add_ref(self, kind, ref, *, space_id=None, project_ref=None, owner=None, **extra):
        self.refs[kind, ref] = dict(ref=ref, owner_id=owner or self.owner, space_id=space_id,
                                   project_ref=project_ref, state='available',
                                   version_ref=ref if kind == 'artifact_version' else 'v1', **extra)

    def call(self, action, params=None, *, revision=None, key=None):
        return self.service.call(action, params or {}, actor=self.actor,
                                 expected_revision=revision, idempotency_key=key)

    def create(self, name='作品', key='create', **extra):
        return self.service.create({'name': name, **extra}, actor=self.actor, idempotency_key=key)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path / 'owner-a')


def assert_code(code, callback):
    with pytest.raises(ValueError) as caught:
        callback()
    assert getattr(caught.value, 'error_code', None) == code


def test_create_is_durable_and_defaults_do_not_start_work(world):
    result = world.create()
    assert isinstance(result, dict)
    assert result['revision'] == 0 and result['state'] == 'active' and result['mounts'] == []
    assert world.service.get(result['space_id'], actor=world.actor)['space']['name'] == '作品'
    reopened = World(world.root)
    assert reopened.service.get(result['space_id'], actor=reopened.actor)['space']['space_id'] == result['space_id']
    assert not (world.root / 'runs').exists()


def test_create_replay_and_changed_intent(world):
    original = world.create()
    assert world.create() == original
    assert_code('IDEMPOTENCY_CONFLICT', lambda: world.create('另一个'))
    assert len(world.call('spaces.list')['items']) == 1


@pytest.mark.parametrize('name', [' 作品 ', '作品', 'ＡＢＣ', 'abc'])
def test_normalized_names_are_unique(world, name):
    world.create(name)
    normalized = name.strip().casefold()
    assert_code('SPACE_NAME_CONFLICT', lambda: world.create(normalized, key='other'))


def test_update_cas_replay_and_preserves_existing_task_selection(world):
    original = world.create()
    changed = world.service.update(original['space_id'], {'name': '新名字'}, actor=world.actor,
                                   expected_revision=0, idempotency_key='rename')
    assert changed['space']['revision'] == 1 and changed['affected_runs'] == []
    assert world.service.update(original['space_id'], {'name': '新名字'}, actor=world.actor,
                                expected_revision=0, idempotency_key='rename') == changed
    assert_code('REVISION_CONFLICT', lambda: world.service.update(original['space_id'], {'purpose': 'stale'},
                actor=world.actor, expected_revision=0, idempotency_key='stale'))


def test_archive_blockers_do_not_kill_or_archive(world):
    sid = world.create()['space_id']
    world.blocks = [{'ref': 'task-1', 'kind': 'task', 'reason_code': 'ACTIVE'}]
    result = world.call('spaces.archive', {'space_id': sid}, revision=0, key='archive')
    assert result['blockers'] == world.blocks and result['space']['state'] == 'active'
    world.blocks = []
    result = world.call('spaces.archive', {'space_id': sid}, revision=0, key='archive')
    assert result['space']['state'] == 'archived'
    world.create('作品', key='replacement')
    assert_code('SPACE_NAME_CONFLICT', lambda: world.call('spaces.restore', {'space_id': sid}, revision=1, key='restore'))


def test_restore_keeps_identity(world):
    sid = world.create()['space_id']
    world.call('spaces.archive', {'space_id': sid}, revision=0, key='archive')
    restored = world.call('spaces.restore', {'space_id': sid}, revision=1, key='restore')
    assert restored['space']['space_id'] == sid and restored['space']['revision'] == 2


def test_bulk_mount_failure_is_atomic_and_validates_whole_batch(world):
    sid = world.create()['space_id']
    world.add_ref('package', 'pkg-a')
    assert_code('REFERENCE_NOT_FOUND', lambda: world.call('spaces.packages.set',
        {'space_id': sid, 'mounts': [{'package_ref': 'pkg-a'}, {'package_ref': 'missing'}]}, revision=0, key='mount'))
    unchanged = world.service.get(sid, actor=world.actor)['space']
    assert unchanged['revision'] == 0 and unchanged['mounts'] == []
    world.add_ref('package', 'missing')
    result = world.call('spaces.packages.set', {'space_id': sid, 'mounts': [{'package_ref': 'pkg-a'}, {'package_ref': 'missing'}]},
                        revision=0, key='mount')
    assert [r['package_ref'] for r in result['mounts']] == ['pkg-a', 'missing']


def test_mount_order_and_one_algorithm_for_convenience_actions(world):
    sid = world.create()['space_id']
    for name in ('a', 'b', 'c'):
        world.add_ref('package', name)
    world.call('spaces.packages.attach', {'space_id': sid, 'package_refs': ['a', 'b']}, revision=0, key='attach')
    result = world.call('spaces.packages.reorder', {'space_id': sid, 'package_refs': ['b', 'a']}, revision=1, key='reorder')
    assert [r['package_ref'] for r in result['mounts']] == ['b', 'a']
    assert_code('INVALID_SCHEMA', lambda: world.call('spaces.packages.reorder',
        {'space_id': sid, 'package_refs': ['a', 'c']}, revision=2, key='wrong-order'))
    result = world.call('spaces.packages.detach', {'space_id': sid, 'package_refs': ['b']}, revision=2, key='detach')
    assert [r['package_ref'] for r in result['mounts']] == ['a']


def test_duplicate_mount_identity_rejected_even_with_different_flags(world):
    sid = world.create()['space_id']
    world.add_ref('package', 'pkg')
    assert_code('INVALID_SCHEMA', lambda: world.call('spaces.packages.set', {'space_id': sid,
        'mounts': [{'package_ref': 'pkg'}, {'package_ref': 'pkg', 'enabled': False}]}, revision=0, key='duplicates'))


def test_mount_list_displays_current_authorized_name_without_changing_binding(world):
    sid = world.create()['space_id']
    world.add_ref('package', 'pkg', name='任务巡检')
    world.call('spaces.packages.set', {'space_id': sid, 'mounts': [{'package_ref': 'pkg', 'enabled': False}]},
               revision=0, key='mount')
    first = world.call('spaces.packages.list', {'space_id': sid})['items'][0]
    assert (first['name'], first['package_ref'], first['enabled']) == ('任务巡检', 'pkg', False)
    world.add_ref('package', 'pkg', name='另一空间的名称', space_id='other-space')
    denied = world.call('spaces.packages.list', {'space_id': sid})['items'][0]
    assert denied['availability']['state'] == 'denied' and 'name' not in denied


def test_mount_preview_and_reads_do_not_write(world):
    sid = world.create()['space_id']
    before = {str(p): p.read_bytes() for p in world.root.rglob('*') if p.is_file()}
    world.add_ref('package', 'pkg')
    result = world.call('spaces.packages.preview', {'space_id': sid, 'mounts': [{'package_ref': 'pkg'}]})
    assert result['diff'] and result['affected_runs'] == []
    world.call('spaces.packages.list', {'space_id': sid})
    after = {str(p): p.read_bytes() for p in world.root.rglob('*') if p.is_file()}
    assert before == after


def test_paging_is_bounded_and_cursor_is_query_and_revision_bound(world):
    for i in range(3):
        world.create(f'工作{i}', key=f'c{i}')
    first = world.call('spaces.list', {'limit': 2})
    assert len(first['items']) == 2 and first['next_cursor'] and first['coverage'] == 'partial'
    second = world.call('spaces.list', {'limit': 2, 'cursor': first['next_cursor']})
    assert len(second['items']) == 1 and second['next_cursor'] is None and second['coverage'] == 'complete'
    assert_code('CURSOR_EXPIRED', lambda: world.call('spaces.list', {'limit': 2, 'query': 'changed', 'cursor': first['next_cursor']}))
    world.create('新工作', key='new')
    assert_code('CURSOR_EXPIRED', lambda: world.call('spaces.list', {'limit': 2, 'cursor': first['next_cursor']}))


@pytest.mark.parametrize('limit', [0, 101, True, '5'])
def test_invalid_limit_is_rejected_not_clamped(world, limit):
    assert_code('INVALID_SCHEMA', lambda: world.call('spaces.list', {'limit': limit}))


def test_concurrent_normalized_create_has_one_winner(world):
    def create(i):
        try:
            return world.create('唯一空间', key=f'c{i}')['space_id']
        except ValueError as error:
            return error.error_code
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(create, range(4)))
    assert values.count('SPACE_NAME_CONFLICT') == 3
    assert len(world.call('spaces.list')['items']) == 1


def test_interface_binding_is_single_authority_and_unbind_is_not_delete(world):
    sid = world.create()['space_id']
    world.add_ref('surface', 'panel')
    params = {'surface_ref': 'panel', 'name': '入口', 'space_ids': [sid], 'default_space_id': sid, 'controller': {'mode': 'global'}}
    response = world.call('interfaces.bind', params, key='bind')
    assert isinstance(response, dict) and isinstance(response.get('binding'), dict)
    binding = response['binding']
    assert world.call('interfaces.get', {'binding_id': binding['binding_id']})['binding'] == binding
    assert world.service.get(sid, actor=world.actor)['interface_refs'] == [binding['binding_id']]
    assert_code('BINDING_CONFLICT', lambda: world.call('interfaces.bind', params, key='other-bind'))
    world.call('interfaces.unbind', {'binding_id': binding['binding_id']}, revision=0, key='unbind')
    assert world.service.get(sid, actor=world.actor)['interface_refs'] == []
    assert_code('BINDING_NOT_FOUND', lambda: world.call('interfaces.resolve', {'binding_id': binding['binding_id']}))


def test_unknown_surface_and_outside_default_are_rejected(world):
    sid = world.create()['space_id']
    params = {'surface_ref': 'not-existing', 'name': '入口', 'space_ids': [sid], 'default_space_id': sid, 'controller': {'mode': 'global'}}
    assert_code('REFERENCE_NOT_FOUND', lambda: world.call('interfaces.bind', params, key='unknown'))
    world.add_ref('surface', 'not-existing')
    params['default_space_id'] = 'not-member'
    assert_code('SPACE_SCOPE_MISMATCH', lambda: world.call('interfaces.bind', params, key='wrong-default'))


def test_context_uses_exact_host_task_and_binding_default(world):
    one = world.create('一', key='one')['space_id']
    two = world.create('二', key='two')['space_id']
    resolver = module('business_context').resolve_business_context
    assert_code('CONTEXT_REQUIRED', lambda: resolver(actor=world.actor, task_id='stask-exact',
        requested_space_id=None, project_ref=None, binding_id=None))
    world.add_ref('surface', 'panel')
    binding = world.call('interfaces.bind', {'surface_ref': 'panel', 'name': '入口', 'space_ids': [one, two],
        'default_space_id': two, 'controller': {'mode': 'global'}}, key='binding')['binding']
    context = resolver(actor=world.actor, task_id='stask-exact', requested_space_id=None,
                       project_ref=None, binding_id=binding['binding_id'])
    assert context.task_id == 'stask-exact' and context.space_id == two and context.space_revision == 0
    world.service.update(two, {'name': '改名'}, actor=world.actor, expected_revision=0, idempotency_key='rename')
    assert context.space_revision == 0 and context.space_id == two


def test_assets_are_original_refs_candidates_cannot_replace_accepted(world):
    sid = world.create()['space_id']
    for ref in ('av1', 'av2'):
        world.add_ref('artifact_version', ref, space_id=sid, artifact_id='artifact', content_sha256='a' * 64)
    registered = world.call('assets.register', {'space_id': sid, 'name': '角色', 'kind': 'character',
        'artifact_ref': {'artifact_id': 'artifact', 'version_ref': 'av1'}}, key='register')
    assert isinstance(registered, dict) and registered.get('asset_id')
    aid = registered['asset_id']
    current = world.call('assets.get', {'space_id': sid, 'asset_id': aid})
    assert current['accepted_version'] is None
    world.call('assets.versions.accept', {'space_id': sid, 'asset_id': aid, 'version_ref': 'av1'}, revision=0, key='accept')
    world.call('assets.versions.add', {'space_id': sid, 'asset_id': aid,
        'artifact_ref': {'artifact_id': 'artifact', 'version_ref': 'av2'}}, revision=1, key='candidate')
    current = world.call('assets.get', {'space_id': sid, 'asset_id': aid})
    assert current['accepted_version'] == 'av1' and current['asset']['revision'] == 2
    versions = world.call('assets.versions.list', {'space_id': sid, 'asset_id': aid})['items']
    assert [(v['version_ref'], v['state']) for v in versions] == [('av1', 'accepted'), ('av2', 'candidate')]
    context = module('business_context').resolve_business_context(actor=world.actor, task_id='stask-x',
        requested_space_id=sid, project_ref=None, binding_id=None)
    assert context.asset_version_refs == ('av1',)
    assert 'data_base64' not in world.service.state_path.read_text()
