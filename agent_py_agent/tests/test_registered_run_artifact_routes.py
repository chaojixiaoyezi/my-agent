"""已登记子产物沿真实业务Client/HTTP获取、列表和下载，不借父结果登记扩大权限。"""
import hashlib
import json

from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact
from agent_py_agent.tests import test_v13_w0_observation_readers as original
from agent_py_agent.tests.test_acceptance_observation_projections import (
    background_http,
    client_for,
    isolated_home,
    sampled_owner,
)
from agent_py_agent.tests.test_mega_business_api_actions import native_request

_FIXTURES = (background_http, isolated_home, sampled_owner)


def publish_child(services, task, runtime, parent):
    root = services.owner_home / 'runs' / 'registered-child'
    root.mkdir(parents=True)
    path = root / 'order.json'
    path.write_text(json.dumps({'grand_total_cents': 3000, 'row_count': 3}))
    runtime.record_run_creation(owner_id=services.owner_id, run_id='registered-child',
        thread_id=task.target_thread_id, host_parent_execution=runtime.parent_execution_ref(
            owner_id=services.owner_id, run_id='observed-' + task.task_id))
    services.host.conversation_store.tasks.bind({'thread_id': task.target_thread_id, 'task_id': 'registered-child',
        'task_path': str(root), 'goal': '原子产物'})
    record = register_artifact(ArtifactRegistration(root, path, run_id='registered-child', task_id=task.task_id,
        agent_id=services.owner_id, source='tool_result', created_by_tool='write_file', status='ready'))
    return root, path, record


def test_registered_child_get_list_download_hash_and_owner_scope(background_http, sampled_owner):  # noqa: F811
    services, task, runtime, parent = sampled_owner
    _, path, record = publish_child(services, task, runtime, parent)
    original.wire(services, runtime)
    client = client_for(background_http)
    detail = client.call('artifacts.get', {'artifact_id': record.artifact_id})
    assert detail['ok'], detail
    assert detail['data']['artifact']['task_id'] == task.task_id
    assert detail['data']['artifact']['content_sha256'] == record.sha256
    assert detail['data']['available'] is True
    listed = client.call('artifacts.list', {'filter': {'task_id': task.task_id}, 'limit': 1})
    assert listed['ok'], listed
    assert listed['data']['items'][0]['artifact_id'] == record.artifact_id
    url = '/client/business/artifacts/' + record.artifact_id + '/content?version_ref=' + record.sha256
    status, headers, data = native_request(background_http, 'GET', url)
    assert status == 200, (status, data)
    assert data == path.read_bytes() and hashlib.sha256(data).hexdigest() == record.sha256
    assert json.loads(data)['grand_total_cents'] == 3000
    status, _, foreign = native_request(background_http, 'GET', url, user='business-bob')
    assert status in {404, 503} and b'grand_total_cents' not in foreign
    wrong_version = client.call('artifacts.get', {'artifact_id': record.artifact_id, 'version_ref': '0' * 64})
    assert not wrong_version['ok'] and wrong_version['http_status'] == 404
    path.write_text('changed bytes')
    drift = client.call('artifacts.get', {'artifact_id': record.artifact_id})
    assert not drift['ok'] and drift['error']['code'] == 'STATE_CONFLICT'


def test_legacy_locator_progress_is_bounded_no_eof_rescan_and_new_highwater(tmp_path, monkeypatch):
    from agent_py_agent.agent.artifacts import registry_locator
    from agent_py_agent.agent.conversation import ConversationStore
    from agent_py_agent.agent.subagents.models import SubAgentTask, TaskStatus
    from agent_py_agent.agent.subagents.services.persistence.run_index import SubagentRunIndex

    owner = (tmp_path / 'app/owners/local/main').resolve()
    store = ConversationStore(owner / 'workspace/conversations')
    workspace = store.storage.root.parent / 'subagents'
    index = SubagentRunIndex(workspace)
    records = []
    for number in range(7):
        run_id = f'legacy-{number}'
        root = owner / 'runs' / run_id
        root.mkdir(parents=True)
        path = root / 'result.txt'
        path.write_text(str(number))
        task = SubAgentTask(id=run_id, goal='legacy', thought='', plan=[], owner='local/main',
            status=TaskStatus.DONE, agent_run_workspace_dir=str(root))
        index.upsert(task)
        directory = workspace / run_id
        directory.mkdir()
        (directory / 'task.json').write_text(json.dumps({'id': run_id, 'owner': 'local/main', 'agent_run_workspace_dir': str(root)}))
        if number == 0:
            (directory / 'task.json').write_text('{broken')
        record = register_artifact(ArtifactRegistration(root, path, run_id=run_id, task_id='parent', agent_id='local/main'))
        records.append(record)
    # 模拟升级前登记：清除派生定位，不碰原registry或内容。
    with registry_locator.connection(owner) as db:
        db.execute('DELETE FROM refs')
    reads = []
    original_read = registry_locator.read_object
    monkeypatch.setattr(registry_locator, 'read_object', lambda home, path: (reads.append(path), original_read(home, path))[1])
    registry_locator.discover(owner, store, 'local/main')
    assert len(reads) == 4 and registry_locator.lookup(owner, records[-1].artifact_id) is None
    registry_locator.discover(owner, store, 'local/main')
    assert len(reads) == 7 and registry_locator.lookup(owner, records[-1].artifact_id) is not None
    registry_locator.discover(owner, store, 'local/main')
    assert len(reads) == 7  # EOF持久高水位，不在每次GET从头扫。
    new = SubAgentTask(id='new-after-eof', goal='new', thought='', plan=[], owner='local/main', status=TaskStatus.DONE)
    index.upsert(new)
    registry_locator.discover(owner, store, 'local/main')
    assert len(reads) == 8 and str(reads[-1]).endswith('new-after-eof/task.json')


def test_registry_publication_failure_preserves_original_and_exact_pending_recovers(background_http, sampled_owner, monkeypatch):  # noqa: F811
    from agent_py_agent.agent.artifacts import registry_locator

    services, task, runtime, parent = sampled_owner
    original_publish = registry_locator.publish_record
    monkeypatch.setattr(registry_locator, 'publish_record', lambda *args: (_ for _ in ()).throw(OSError('index interrupted')))
    root, path, record = publish_child(services, task, runtime, parent)
    assert path.is_file() and (root / 'data/artifacts/registry.jsonl').is_file()
    # 即使这个原run不在旧发现页中，精确pending ID仍可核原登记恢复，不靠扫描全部根。
    monkeypatch.setattr(registry_locator, 'discover', lambda *args: [])
    monkeypatch.setattr(registry_locator, 'publish_record', original_publish)
    original.wire(services, runtime)
    result = client_for(background_http).call('artifacts.get', {'artifact_id': record.artifact_id})
    assert result['ok'], result
    assert result['data']['artifact']['content_sha256'] == record.sha256
    assert registry_locator.lookup(services.owner_home, record.artifact_id) is not None


def test_denied_space_or_thread_precedes_content_or_drift_and_budget_is_explicit(background_http, sampled_owner, monkeypatch):  # noqa: F811
    import pytest

    from agent_py_agent.agent.conversation import registered_artifact_observation as observed
    from agent_py_agent.agent.conversation.business_access import BusinessActor, BusinessError
    from agent_py_agent.agent.gateway_parts import business_artifacts_http as media
    from agent_py_agent.agent.gateway_parts.business_api import bind_business_context
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    services, task, runtime, parent = sampled_owner
    _, path, record = publish_child(services, task, runtime, parent)
    original.wire(services, runtime)
    def forbidden(*args, **kwargs):
        pytest.fail('unapproved space/thread must not read registry or content bytes')
    original_read = observed._read_source
    monkeypatch.setattr(observed, '_read_source', forbidden)
    for denied_field in ('space_id', 'thread_id'):
        actor = BusinessActor(services.owner_id, 'restricted', services.observation,
            lambda action, facts: facts.get(denied_field) is None)
        with pytest.raises(BusinessError) as caught:
            services.observation.query('artifacts.get', {'artifact_id': record.artifact_id}, actor=actor)
        assert caught.value.code == 'OBSERVATION_NOT_FOUND'
        assert 'DRIFT' not in repr(caught.value.details)
    context = bind_business_context(services.host, authorize=lambda action, facts: facts.get('space_id') is None)
    with pytest.raises(HostAPIError) as caught:
        media.artifact_content(record.artifact_id, record.sha256, context, services)
    assert caught.value.status == 404 and 'DRIFT' not in str(caught.value)
    monkeypatch.setattr(observed, '_read_source', original_read)
    monkeypatch.setattr(observed, '_CONTENT_BYTES', 8)
    # 正常文件仍与原hash一致；只是本次读取预算不足，不能误报漂移。
    assert hashlib.sha256(path.read_bytes()).hexdigest() == record.sha256
    result = client_for(background_http).call('artifacts.get', {'artifact_id': record.artifact_id})
    assert not result['ok'] and result['http_status'] == 503
    assert 'ARTIFACT_CONTENT_READ_BUDGET' in json.dumps(result['error'])
    listed = client_for(background_http).call('artifacts.list', {'filter': {'task_id': task.task_id}, 'limit': 1})
    assert listed['ok'] and listed['data']['items'][0]['available'] is True  # 仅元数据可达，不宣称校验hash。


def test_proven_empty_original_sources_return_not_found(tmp_path):
    import pytest

    from agent_py_agent.agent.conversation import ConversationStore
    from agent_py_agent.agent.conversation.business_access import BusinessActor, BusinessError
    from agent_py_agent.agent.conversation.business_observability import BusinessObservationService
    from agent_py_agent.agent.gateway_parts.business_api_v13_readers import (
        default_observation_readers,
    )
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    from agent_py_agent.agent.subagents.services.persistence.run_index import SubagentRunIndex

    owner = (tmp_path / 'app/owners/local/main').resolve()
    store = ConversationStore(owner / 'workspace/conversations')
    SubagentRunIndex(store.storage.root.parent / 'subagents')
    runtime = RuntimeRepository(owner / 'runtime.db')
    # 显式就绪的空媒体来源；缺失中间目录属于未核准来源而非完整空集合。
    (owner / 'artifacts/business/data/artifacts').mkdir(parents=True)
    service = BusinessObservationService(owner, owner_id='local/main', store=store, runtime=runtime,
        readers=default_observation_readers(owner, 'local/main', store, runtime))
    actor = BusinessActor('local/main', 'local', service, lambda *args: True)
    with pytest.raises(BusinessError) as caught:
        service.query('artifacts.get', {'artifact_id': 'genuinely-absent'}, actor=actor)
    assert caught.value.code == 'OBSERVATION_NOT_FOUND'
