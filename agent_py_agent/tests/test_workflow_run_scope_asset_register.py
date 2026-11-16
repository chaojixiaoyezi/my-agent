# 模块用途: 空间内原生 workflows.run 的运行级上下文（逻辑 task_id 即本运行ID）产生的正式 RUN_OUT 文件，
#   可经 artifacts.get 读取并经 assets.register 登记为同空间业务资产；无业务上下文的运行仍拒绝。只用 tmp 隔离 home。
from __future__ import annotations

import http.client

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.cli.chat_client_context import (
    _gateway_request_identity,
    owner_identity_from_config,
)
from agent_py_agent.tests.test_business_v12_background_scheduling import (  # noqa: F401 - pytest fixture
    call,
    native,
    ok,
)

_SCRIPT = ("import json, os, pathlib\n"
           "pathlib.Path(os.environ['MY_AGENT_RUN_OUT'], 'result.json').write_text('{\"total\": 3}')\n"
           "print(json.dumps({'total': 3}))\n")


def _flow(name, scope=None):
    step = {'step_id': 'pack', 'name': 'pack', 'mode': 'deterministic_script', 'script_id': 'pack'}
    definition = {'descriptor': {'id': name, 'kind': 'workflow', 'name': name, 'access_targets': [],
                                 'data_sources': [], 'credential_refs': [], 'io_contract': {}}, 'steps': [step]}
    return {'definition': definition, **({'scope': scope} if scope else {})}


def _artifact(gateway, run_id):
    run = ok(gateway, 'workflow_runs.get', {'workflow_run_id': run_id})
    [item] = run['steps'][0]['output_artifacts']
    return run['run'], item


def _download(gateway, artifact_id, version_ref):
    server, agent = gateway
    identity = _gateway_request_identity(owner_identity_from_config(agent.config))
    conn = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=30)
    try:
        conn.request('GET', f'/client/business/artifacts/{artifact_id}/content?version_ref={version_ref}', headers={
            'X-Gateway-Token': server._local_client_credential, 'X-User-Id': identity['user_id'],
            'X-Channel': identity['channel']})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def test_space_run_artifact_reads_and_registers_in_its_frozen_space(native):  # noqa: F811
    _, agent = native
    agent.business_host_ports_enabled = True
    ScriptStore(agent.home_paths.owner_home_dir).save_script(AssetDescriptor('pack', 'script', 'pack'), _SCRIPT)
    space = ok(native, 'spaces.create', {'name': 'run-scope-assets'}, idempotency_key='l12-space-create-0001')['space_id']
    ok(native, 'workflows.create', _flow('l12-space', {'scope_mode': 'spaces', 'space_ids': [space]}),
       idempotency_key='l12-space-flow-00001')
    receipt = ok(native, 'workflows.run', {'workflow_ref': 'l12-space', 'space_id': space},
                 idempotency_key='l12-space-run-000001')
    view, item = _artifact(native, receipt['workflow_run_id'])
    assert (view['task_id'], view['space_id'], view['status']) == (None, space, 'succeeded')
    detail = ok(native, 'artifacts.get', {'artifact_id': item['artifact_id'], 'version_ref': item['sha256']})
    assert detail['artifact']['task_id'] is None and detail['artifact']['space_id'] == space
    assert detail['available'] is True
    assert _download(native, item['artifact_id'], item['sha256']) == (200, b'{"total": 3}')
    registered = ok(native, 'assets.register', {'space_id': space, 'name': 'run-scope-result', 'kind': 'document',
        'artifact_ref': {'artifact_id': item['artifact_id'], 'version_ref': item['sha256'],
                         'content_sha256': item['sha256']}}, idempotency_key='l12-asset-register-01')
    assert registered['version_ref'] == item['sha256']
    # 产物归属不随登记改变：其它空间仍拒绝。
    other = ok(native, 'spaces.create', {'name': 'run-scope-other'}, idempotency_key='l12-space-create-0002')['space_id']
    status, payload = call(native, 'assets.register', {'space_id': other, 'name': 'moved', 'kind': 'document',
        'artifact_ref': {'artifact_id': item['artifact_id'], 'version_ref': item['sha256']}},
        idempotency_key='l12-asset-register-02')
    assert status != 200 and payload['error']['code'] == 'SPACE_SCOPE_MISMATCH', payload


def test_owner_level_run_without_business_context_stays_unregistrable(native):  # noqa: F811
    _, agent = native
    agent.business_host_ports_enabled = True
    ScriptStore(agent.home_paths.owner_home_dir).save_script(AssetDescriptor('pack', 'script', 'pack'), _SCRIPT)
    space = ok(native, 'spaces.create', {'name': 'owner-level'}, idempotency_key='l12-space-create-0003')['space_id']
    ok(native, 'workflows.create', _flow('l12-owner'), idempotency_key='l12-owner-flow-00001')
    receipt = ok(native, 'workflows.run', {'workflow_ref': 'l12-owner'}, idempotency_key='l12-owner-run-000001')
    view, item = _artifact(native, receipt['workflow_run_id'])
    assert (view['task_id'], view['space_id']) == (None, None)
    status, payload = call(native, 'artifacts.get', {'artifact_id': item['artifact_id']})
    assert status == 404 and 'WORKFLOW_ARTIFACT_TASK_SCOPE_MISMATCH' in str(payload['error']), payload
    status, payload = call(native, 'assets.register', {'space_id': space, 'name': 'unscoped', 'kind': 'document',
        'artifact_ref': {'artifact_id': item['artifact_id'], 'version_ref': item['sha256']}},
        idempotency_key='l12-asset-register-03')
    assert status != 200 and payload['effect_outcome'] == 'not_started', payload
