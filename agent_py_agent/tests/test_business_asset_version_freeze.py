"""本地真实HTTP任务、原采用批准输入及canonical子树的版本冻结验证。"""
import json
from io import BytesIO
from uuid import uuid4

from agent_py_agent.agent.gateway_parts.business_api import local_business_context
from agent_py_agent.agent.gateway_parts.business_artifacts_http import upload_artifact
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
from agent_py_agent.cli.space_commands import BusinessClient
from agent_py_agent.tests.test_business_model_tools import agent as agent
from agent_py_agent.tests.test_content_adoption_human_approval import (
    approve_through_original_input,
    execute,
    setup_adoption,
)
from agent_py_agent.tests.test_mega_business_api_actions import native_request
from agent_py_agent.tests.test_model_profiles import add


def approve(agent, tmp_path, tool, params):
    tmp_path.mkdir()
    pending = execute(agent, tool, params)
    assert pending.decision.status == 'ask'
    binding = approve_through_original_input(tmp_path, pending.call, agent.home_paths.owner_id)
    result = execute(agent, tool, params, [binding])
    assert result.result.ok, result.result.output


def test_real_http_accepted_versions_freeze_across_adoption_model_and_agent(agent, tmp_path):
    services, _, accepted, tool = setup_adoption(agent, tmp_path)
    approve(agent, tmp_path / 'approve-v1', tool, accepted)
    sid, aid, v1 = (accepted['params'][k] for k in ('space_id', 'asset_id', 'version_ref'))
    first_profile, _ = add(agent, model_name='offline-first')
    second_profile, _ = add(agent, model_name='offline-second')
    server = GatewayHTTPServer(0, gateway_paths_from_root(tmp_path / 'http'), params=GatewayHTTPServerParams(agent=agent))
    server.start()
    receipts = []
    try:
        def transport(path, request, **kwargs):
            status, _, raw = native_request((server, agent), 'POST', path, request,
                user='admin', headers={'X-Channel': 'chat'})
            reply = json.loads(raw)
            receipts.append({'request': request, 'status': status, 'response': reply})
            return status, reply
        client = BusinessClient(transport)
        def call(action, params, **options):
            result = client.send(client.prepare(action, params, **options))
            assert result['ok'], result
            return result['data']
        def task_context(task_id):
            task = agent.conversation_store.session_tasks.require(task_id)
            return task.metadata['business_context']
        one = call('background_agents.create', {'space_id': sid, 'name': 'first-worker', 'model_profile_id': first_profile})
        two = call('background_agents.create', {'space_id': sid, 'name': 'second-worker', 'model_profile_id': second_profile})
        one_id, two_id = one['agent']['agent_id'], two['agent']['agent_id']
        old = call('tasks.assign', {'space_id': sid, 'agent_id': one_id, 'instruction': '冻结版本一，测试不执行模型'})
        sibling = call('tasks.assign', {'space_id': sid, 'agent_id': two_id, 'instruction': '另一模型读取同一采用版本'})
        frozen = task_context(old['task_id'])
        assert frozen['asset_version_refs'] == [v1]
        assert task_context(sibling['task_id'])['asset_version_refs'] == [v1]
        repository = RuntimeRepository(runtime_db_path(agent.home_paths.owner_home_dir))
        root = repository.record_run_creation(owner_id=agent.home_paths.owner_id, run_id='freeze-root',
            thread_id=agent.conversation_store.session_tasks.require(old['task_id']).target_thread_id, task_run_metadata={
                'business_context': frozen, 'session_task_ref': old['task_id']})
        child = repository.record_run_creation(owner_id=agent.home_paths.owner_id, run_id='freeze-child',
            host_parent_execution=repository.parent_execution_ref(owner_id=agent.home_paths.owner_id, run_id='freeze-root'))
        grand = repository.record_run_creation(owner_id=agent.home_paths.owner_id, run_id='freeze-grandchild',
            host_parent_execution=repository.parent_execution_ref(owner_id=agent.home_paths.owner_id, run_id='freeze-child'))
        context, current = local_business_context(agent, model=True)
        v2 = upload_artifact(BytesIO(b'updated material version two'), {'space_id': sid, 'idempotency_key': 'second-content-upload'},
            'version-two.txt', context, current)
        ref = {k: v2[k] for k in ('artifact_id', 'version_ref', 'content_sha256')}
        call('assets.versions.add', {'space_id': sid, 'asset_id': aid, 'artifact_ref': ref}, expected_revision=1)
        assert call('assets.get', {'space_id': sid, 'asset_id': aid})['accepted_version'] == v1
        accepted2 = {**accepted, 'request_id': 'content-v2', 'idempotency_key': 'content-adoption-v2',
            'expected_revision': 2, 'params': {**accepted['params'], 'version_ref': ref['version_ref']}}
        approve(agent, tmp_path / 'approve-v2', tool, accepted2)
        assert call('assets.get', {'space_id': sid, 'asset_id': aid})['accepted_version'] == ref['version_ref']
        model_before = call('background_agents.model.get', {'agent_id': one_id})
        call('background_agents.model.set', {'agent_id': one_id, 'model_profile_id': second_profile},
            expected_revision=model_before['selection_revision'])
        assert call('background_agents.model.get', {'agent_id': one_id})['model_profile_id'] == second_profile
        fresh = call('tasks.assign', {'space_id': sid, 'agent_id': one_id, 'instruction': '新任务采用版本二'})
        iteration = call('tasks.follow_up', {'task_id': old['task_id'], 'instruction': '新受理迭代读取当前采用版本'})
        assert task_context(old['task_id']) == frozen
        assert task_context(sibling['task_id'])['asset_version_refs'] == [v1]
        assert task_context(fresh['task_id'])['asset_version_refs'] == [ref['version_ref']]
        assert task_context(iteration['new_task_id'])['asset_version_refs'] == [ref['version_ref']]
        for run in (root, child, grand):
            assert run['task_run_id'] == root['task_run_id']
            metadata = json.loads(repository.get_task_run(run['task_run_id'])['metadata_json'])
            assert metadata['business_context'] == frozen
        call('tasks.get', {'task_id': old['task_id']})
        call('tasks.get', {'task_id': fresh['task_id']})
        (tmp_path / 'freeze-receipts.json').write_text(json.dumps(receipts, ensure_ascii=False, indent=2))
        (tmp_path / 'freeze-result.json').write_text(json.dumps({
            'old_task': task_context(old['task_id']), 'second_agent_task': task_context(sibling['task_id']),
            'new_task': task_context(fresh['task_id']), 'follow_up_task': task_context(iteration['new_task_id']),
            'original_version': v1, 'new_version': ref['version_ref'],
            'runtime_subtree': [{**{k: r[k] for k in ('agent_run_id', 'task_run_id')},
                'run_id': repository.get_agent_run(r['agent_run_id'])['run_id']} for r in (root, child, grand)],
            'model_before': model_before, 'model_after': call('background_agents.model.get', {'agent_id': one_id}),
            'scope': 'local controlled human-input simulation; no actual model execution or user click'},
            ensure_ascii=False, indent=2))
    finally:
        server.stop()
