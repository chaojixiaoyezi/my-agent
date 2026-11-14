"""已登记子产物沿真实业务Client/HTTP获取、列表和下载，不借父结果登记扩大权限。"""
import hashlib
import json

import pytest

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


@pytest.mark.parametrize('template', ['tasks/{date}/{task_slug}', 'business-output/{task_slug}/{date}'])
def test_declared_task_output_roundtrips_without_expanding_owner(background_http, sampled_owner, template):  # noqa: F811
    from dataclasses import replace
    from datetime import datetime, timezone

    from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate
    from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool
    from agent_py_agent.agent.tooling.write_boundary import validate_write_boundary
    from agent_py_agent.agent.user_space.home_layout import task_workspace_path

    services, task, runtime, parent = sampled_owner
    background_http[1].config = replace(background_http[1].config, workspace_task_path_template=template)
    services.host.config = background_http[1].config
    root, _, _ = publish_child(services, task, runtime, parent)
    output = task_workspace_path(services.owner_home, services.host.config.workspace_task_path_template,
        date=datetime.fromtimestamp(task.created_at, timezone.utc).date().isoformat(), task_name=task.task_id) / 'output'
    output.mkdir(parents=True)
    path = output / 'probe.json'
    path.write_text('{"ok":true}')
    work = root / 'work'
    work.mkdir()
    contract = {'schema_version': 'run_workspace.v1', 'owner_id': services.owner_id,
        'owner_home': str(services.owner_home), 'run_id': 'registered-child', 'task_id': task.task_id,
        'task_root': str(root), 'delivery_contract': {'task_workspace': {'output_dir': str(output)}}}
    (work / 'run_workspace.json').write_text(json.dumps(contract))
    record = register_artifact(ArtifactRegistration(root, path, run_id='registered-child',
        task_id=task.task_id, agent_id=services.owner_id, status='ready'))
    services.host.conversation_store.session_tasks.advance(task.task_id, SessionTaskUpdate(status='accepted'))
    services.host.conversation_store.session_tasks.advance(task.task_id, SessionTaskUpdate(status='done',
        result_refs=(str(path),), result_artifacts=({'artifact_id': record.artifact_id, 'path': str(path),
            'sha256': record.sha256, 'size_bytes': record.size_bytes},)))
    original.wire(services, runtime)
    client = client_for(background_http)
    detail = client.call('artifacts.get', {'artifact_id': record.artifact_id})
    assert detail['ok'] and detail['data']['available'], detail
    listed = client.call('artifacts.list', {'filter': {'task_id': task.task_id}, 'limit': 10})
    assert listed['ok'] and record.artifact_id in [r['artifact_id'] for r in listed['data']['items']]
    url = '/client/business/artifacts/' + record.artifact_id + '/content?version_ref=' + record.sha256
    status, _, body = native_request(background_http, 'GET', url)
    assert status == 200 and body == path.read_bytes(), (status, body)
    # 普通工作区manifest确实可被模型工具写入；它不能给Owner配置/另一任务扩权。
    writer = WriteFileTool(root)
    manifest = work / 'run_workspace.json'
    assert validate_write_boundary('write_file', {'path': str(manifest)}, workspace_root=root,
        write_boundary={'allowed_write_roots': [str(root)],
                        'forbidden_write_roots': [str(services.owner_home / 'runs')]}) == ''
    for untrusted_output in (services.owner_home / 'config', services.owner_home / 'data',
                             services.owner_home / 'tasks/other-task/output'):
        contract['delivery_contract']['task_workspace']['output_dir'] = str(untrusted_output)
        written = writer.execute({'path': str(manifest), 'content': json.dumps(contract)})
        assert written.ok, written
        secret = untrusted_output / 'private-record.txt'
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_text('isolated-owner-private-content')
        forged = register_artifact(ArtifactRegistration(root, secret, run_id='registered-child',
            task_id=task.task_id, agent_id=services.owner_id, status='ready'))
        result = client.call('artifacts.get', {'artifact_id': forged.artifact_id})
        assert not result['ok'] and 'ARTIFACT_WORKSPACE_CONTRACT_MISMATCH' in json.dumps(result)
        forged_url = '/client/business/artifacts/' + forged.artifact_id + '/content?version_ref=' + forged.sha256
        status, _, body = native_request(background_http, 'GET', forged_url)
        assert status != 200 and b'isolated-owner-private-content' not in body
    contract['delivery_contract']['task_workspace']['output_dir'] = str(output)
    assert writer.execute({'path': str(manifest), 'content': json.dumps(contract)}).ok
    # 内容和可写registry一起重登后，仍不能覆盖任务冻结的hash。
    path.write_text('{"changed":true}')
    changed = register_artifact(ArtifactRegistration(root, path, run_id='registered-child',
        task_id=task.task_id, agent_id=services.owner_id, status='ready'))
    result = client.call('artifacts.get', {'artifact_id': changed.artifact_id})
    assert not result['ok'] and 'ARTIFACT_TASK_PIN_MISMATCH' in json.dumps(result)
    path.write_text('{"ok":true}')
    register_artifact(ArtifactRegistration(root, path, run_id='registered-child',
        task_id=task.task_id, agent_id=services.owner_id, status='ready'))
    # 相邻任务即使属于同 Owner、且被原 run 登记，也不是该合同的交付目录。
    foreign = services.owner_home / 'tasks' / 'other-task' / 'output' / 'secret.json'
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text('private-other-task')
    denied = register_artifact(ArtifactRegistration(root, foreign, run_id='registered-child',
        task_id=task.task_id, agent_id=services.owner_id, status='ready'))
    result = client.call('artifacts.get', {'artifact_id': denied.artifact_id})
    assert not result['ok'] and 'ARTIFACT_CONTENT_OUTSIDE_RUN' in json.dumps(result)
    # 登记后替换叶或父目录为链接，元数据和下载也必须拒绝。
    path.unlink()
    path.symlink_to(foreign)
    result = client.call('artifacts.get', {'artifact_id': record.artifact_id})
    assert not result['ok'] and 'private-other-task' not in json.dumps(result)
    status, _, body = native_request(background_http, 'GET', url)
    assert status != 200 and b'private-other-task' not in body
    path.unlink()
    output.rmdir()
    output.symlink_to(foreign.parent, target_is_directory=True)
    result = client.call('artifacts.get', {'artifact_id': record.artifact_id})
    assert not result['ok']
    # 正式合同身份不匹配时不能靠相同 Owner 获得权限。
    contract['task_id'] = 'other-task'
    (work / 'run_workspace.json').write_text(json.dumps(contract))
    result = client.call('artifacts.get', {'artifact_id': record.artifact_id})
    assert not result['ok'] and 'ARTIFACT_WORKSPACE_CONTRACT_MISMATCH' in json.dumps(result)


def test_main_registry_discovery_bounded_and_unproven_links_remain_unknown(background_http, sampled_owner, monkeypatch):  # noqa: F811
    from types import SimpleNamespace

    from agent_py_agent.agent.artifacts import registry_locator
    from agent_py_agent.agent.conversation.registered_artifact_observation import (
        discover_registered,
    )

    services, task, runtime, parent = sampled_owner
    root, path, _ = publish_child(services, task, runtime, parent)
    with runtime._runtime_connection() as db:
        db.execute("UPDATE agent_runs SET role='main' WHERE run_id=?", ('observed-' + task.task_id,))
        db.commit()
    record = register_artifact(ArtifactRegistration(root, path, run_id='observed-' + task.task_id,
        task_id=task.task_id, agent_id=services.owner_id))
    store = services.host.conversation_store
    source = SimpleNamespace(home=services.owner_home, store=store, owner=services.owner_id, repo=runtime)
    # 原主任务缺 TaskLink 时不可将现有子run登记当全量覆盖证明。
    assert discover_registered(source)['coverage'] == 'unknown'
    store.tasks.bind({'thread_id': task.target_thread_id, 'task_id': task.task_id,
        'task_path': str(root), 'goal': '原主任务登记关联'})
    # 模拟升级丢弃定位派生索引；不修改原登记或补结果。
    with registry_locator.connection(services.owner_home) as db:
        db.execute('DELETE FROM refs')
    assert registry_locator.lookup(services.owner_home, record.artifact_id) is None
    report = registry_locator.discover_main(services.owner_home, store, services.owner_id, runtime)
    assert report['coverage'] == 'complete', report
    assert registry_locator.lookup(services.owner_home, record.artifact_id) is not None
    def forbidden(*args, **kwargs):
        pytest.fail('EOF must not reread historical registry')
    monkeypatch.setattr(registry_locator, 'read_bytes_beneath', forbidden)
    assert registry_locator.discover_main(services.owner_home, store, services.owner_id, runtime)['coverage'] == 'complete'


def test_proven_empty_artifact_list_tail_complete_and_unknown_is_preserved(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import ConversationStore, registered_artifact_observation
    from agent_py_agent.agent.conversation.business_access import BusinessActor
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
    (owner / 'artifacts/business/data/artifacts').mkdir(parents=True)
    service = BusinessObservationService(owner, owner_id='local/main', store=store, runtime=runtime,
        readers=default_observation_readers(owner, 'local/main', store, runtime))
    actor = BusinessActor('local/main', 'local', service, lambda *args: True)
    page = service.query('artifacts.list', {'limit': 1}, actor=actor)
    assert page['coverage'] == 'complete', page
    assert page['items'] == [] and page['next_cursor'] is None
    monkeypatch.setattr(registered_artifact_observation, 'discover_registered',
        lambda source: {'coverage': 'unknown', 'after': 0})
    page = service.query('artifacts.list', {'limit': 1}, actor=actor)
    assert page['coverage'] == 'unknown', page


def test_foreground_main_original_origin_reads_only_own_run(background_http, sampled_owner):  # noqa: F811
    services, _, runtime, _ = sampled_owner
    store = services.host.conversation_store
    thread = store.threads.create(owner_id=services.owner_id, owner_home=str(services.owner_home),
        canonical_user_id='business-alice', workspace_root=str(services.owner_home))
    run_id = 'foreground-main-origin'
    root = services.owner_home / 'runs' / run_id
    root.mkdir(parents=True)
    path = root / 'output.txt'
    path.write_text('foreground original')
    runtime.record_run_creation(owner_id=services.owner_id, run_id=run_id, conversation_task_id=run_id,
        thread_id=thread.thread_id, role='main')
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': run_id, 'task_path': str(root)})
    # 夹具提供原宿主来源，不代模型执行任务；公开读取仍必须逐项核对。
    link_path = store.storage.task_path(run_id)
    raw = json.loads(link_path.read_text())
    raw['host_user_origin'] = {'owner_id': services.owner_id, 'thread_id': thread.thread_id,
        'task_id': run_id, 'request_id': run_id, 'user_message_id': 'original-message'}
    link_path.write_text(json.dumps(raw))
    record = register_artifact(ArtifactRegistration(root, path, run_id=run_id, task_id=run_id,
        agent_id=services.owner_id, status='ready'))
    original.wire(services, runtime)
    client = client_for(background_http)
    detail = client.call('artifacts.get', {'artifact_id': record.artifact_id})
    assert detail['ok'] and detail['data']['available'], detail
    url = '/client/business/artifacts/' + record.artifact_id + '/content?version_ref=' + record.sha256
    transport = native_request(background_http, 'GET', url)
    assert transport[0] == 200 and transport[2] == path.read_bytes(), transport
    from datetime import datetime, timezone

    from agent_py_agent.agent.user_space.home_layout import task_workspace_path

    day = datetime.fromtimestamp(raw['created_at'], timezone.utc).date().isoformat()
    output = task_workspace_path(services.owner_home, services.host.config.workspace_task_path_template,
        date=day, task_name=run_id) / 'output'
    output.mkdir(parents=True)
    delivered = output / 'delivered.txt'
    delivered.write_text('native canonical task output')
    (root / 'work').mkdir()
    manifest = root / 'work/run_workspace.json'
    contract = {'schema_version': 'run_workspace.v1', 'owner_id': services.owner_id,
        'owner_home': str(services.owner_home), 'run_id': run_id, 'task_id': run_id,
        'task_root': str(root), 'delivery_contract': {'task_workspace': {'output_dir': str(output)}}}
    manifest.write_text(json.dumps(contract))
    delivered_record = register_artifact(ArtifactRegistration(root, delivered, run_id=run_id,
        task_id=run_id, agent_id=services.owner_id, status='ready'))
    detail = client.call('artifacts.get', {'artifact_id': delivered_record.artifact_id})
    assert detail['ok'] and detail['data']['available'], detail
    delivered_url = '/client/business/artifacts/' + delivered_record.artifact_id + '/content?version_ref=' + delivered_record.sha256
    transport = native_request(background_http, 'GET', delivered_url)
    assert transport[0] == 200 and transport[2] == delivered.read_bytes(), transport
    # 同Owner别的任务、无宿主冻结的外部工作区与错误宿主请求均不能扩权。
    other = services.owner_home / 'other-task.txt'
    other.write_text('foreign-task-private')
    outside = register_artifact(ArtifactRegistration(root, other, run_id=run_id, task_id=run_id,
        agent_id=services.owner_id, status='ready'))
    denied = client.call('artifacts.get', {'artifact_id': outside.artifact_id})
    assert not denied['ok'] and 'ARTIFACT_CONTENT_OUTSIDE_RUN' in json.dumps(denied)
    raw['host_user_origin']['request_id'] = 'other-request'
    link_path.write_text(json.dumps(raw))
    denied = client.call('artifacts.get', {'artifact_id': record.artifact_id})
    assert not denied['ok'] and 'ARTIFACT_MAIN_ORIGIN_MISMATCH' in json.dumps(denied)


def test_main_discovery_four_root_budget_resume_and_new_highwater(background_http, sampled_owner, monkeypatch):  # noqa: F811
    from agent_py_agent.agent.artifacts import registry_locator

    services, _, runtime, _ = sampled_owner
    with runtime._runtime_connection() as db:
        db.execute("UPDATE agent_runs SET role='subagent' WHERE role=''")
        db.commit()
    store = services.host.conversation_store
    thread = store.threads.create(owner_id=services.owner_id, owner_home=str(services.owner_home),
        canonical_user_id='business-alice', workspace_root=str(services.owner_home))
    records = []
    def setup(number):
        run_id = f'main-page-{number}'
        root = services.owner_home / 'runs' / run_id
        root.mkdir(parents=True)
        path = root / 'result.txt'
        path.write_text(str(number))
        runtime.record_run_creation(owner_id=services.owner_id, run_id=run_id, conversation_task_id=run_id,
            thread_id=thread.thread_id, role='main')
        store.tasks.bind({'thread_id': thread.thread_id, 'task_id': run_id, 'task_path': str(root)})
        record = register_artifact(ArtifactRegistration(root, path, run_id=run_id, task_id=run_id,
            agent_id=services.owner_id))
        records.append(record)
    for number in range(7):
        setup(number)
    with registry_locator.connection(services.owner_home) as db:
        db.execute('DELETE FROM refs')
    reads = []
    reader = registry_locator.read_bytes_beneath
    def traced(*args, **kwargs):
        reads.append(args[1])
        return reader(*args, **kwargs)
    monkeypatch.setattr(registry_locator, 'read_bytes_beneath', traced)
    first = registry_locator.discover_main(services.owner_home, store, services.owner_id, runtime)
    assert first['coverage'] == 'partial' and len(reads) <= 4 and first['examined'] == 4
    second = registry_locator.discover_main(services.owner_home, store, services.owner_id, runtime)
    assert second['coverage'] == 'complete' and len(reads) == 7
    registry_locator.discover_main(services.owner_home, store, services.owner_id, runtime)
    assert len(reads) == 7
    setup(7)
    reads.clear()
    assert registry_locator.discover_main(services.owner_home, store, services.owner_id, runtime)['coverage'] == 'complete'
    assert len(reads) == 1


def test_media_paging_rejects_original_registry_mutation(background_http, sampled_owner):  # noqa: F811
    from agent_py_agent.agent.artifacts.registry import register_observed_artifact

    services, task, runtime, _ = sampled_owner
    root = services.owner_home / 'artifacts/business'
    root.mkdir(parents=True)
    def publish(number):
        path = root / f'media-{number}.blob'
        path.write_bytes(b'original media bytes')
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        register_observed_artifact(ArtifactRegistration(root, path, artifact_id=f'media-{number}',
            status='ready', source='business_upload', mime_type='text/plain', metadata={
                'owner_id': services.owner_id, 'space_id': task.metadata['business_context']['space_id'],
                'version_ref': 'version-' + sha[:40], 'display_name': f'media-{number}.txt'}),
            observed_sha256=sha, observed_size_bytes=path.stat().st_size, atomic_publish=True)
    publish(1)
    publish(2)
    original.wire(services, runtime)
    client = client_for(background_http)
    first = client.call('artifacts.list', {'limit': 1})
    assert first['ok'] and first['data']['items'][0]['artifact_id'] == 'media-1', first
    cursor = first['data']['next_cursor']
    assert cursor
    publish(3)
    changed = client.call('artifacts.list', {'limit': 1, 'cursor': cursor})
    assert not changed['ok'] and changed['error']['code'] == 'CURSOR_EXPIRED', changed
