"""原生执行、逐批持久化与真实BusinessClient/HTTP的产物交付往返。"""
import hashlib
import json

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import WorkflowStore
from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.tests import test_v13_w0_observation_readers as original
from agent_py_agent.tests.test_acceptance_observation_projections import (
    background_http,
    client_for,
    isolated_home,
)
from agent_py_agent.tests.test_mega_business_api_actions import native_request

_FIXTURES = (background_http, isolated_home)


def real_run(background, *, foreach=False, pause=False):
    services, task = original.observed_task(background)
    code = ('import os,json,sys\nfrom pathlib import Path\nx=json.load(sys.stdin)\n'
            'p=Path(os.environ["MY_AGENT_RUN_OUT"])/"part.txt"\n'
            'p.write_text(json.dumps(x.get("items", [7])))\n'
            'print(json.dumps({"value":7' + (',"_pause_for_resume":True' if pause else '') + '}))')
    ScriptStore(services.owner_home).save_script(AssetDescriptor(id='formal-part', kind='script', name='正式分片'), code)
    step = {'step_id': 'produce', 'name': '产出', 'mode': 'deterministic_script', 'script_id': 'formal-part'}
    if foreach:
        step['foreach'] = {'from': 'inputs.records', 'batch_size': 1}
    definition = {'descriptor': {'id': 'formal-batches', 'kind': 'workflow', 'name': '分批产物'}, 'steps': [step]}
    WorkflowStore(services.owner_home).save_workflow(definition)
    engine = WorkflowEngine(services.owner_home, run_authorizer=lambda *_: None)
    from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaEnforcer
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner
    engine.runner = ScriptRunner(services.owner_home, run_out_quota=OwnerQuotaEnforcer(services.owner_home, max_bytes=0))
    record = engine.run_workflow(definition, {'records': [1, 2]}, owner_id=services.owner_id,
        business_context=BusinessExecutionContext.from_dict(task.metadata['business_context']))
    return services, task, engine, record


def assert_deliverable(background, artifact, expected):
    client = client_for(background)
    result = client.call('artifacts.get', {'artifact_id': artifact['artifact_id'], 'version_ref': artifact['sha256']})
    assert result['ok'] and result['data']['available'] is True, result
    route = '/client/business/artifacts/' + artifact['artifact_id'] + '/content?version_ref=' + artifact['sha256']
    status, headers, raw = native_request(background, 'GET', route)
    assert status == 200 and raw == expected
    assert hashlib.sha256(raw).hexdigest() == artifact['sha256']
    assert headers['ETag'] == '"' + artifact['sha256'] + '"'


def test_pause_confirm_preserves_original_manifest_and_download(background_http):  # noqa: F811
    _, _, engine, run = real_run(background_http, pause=True)
    step = run.steps[0]
    assert step.status == 'paused_for_resume'
    artifacts = step.output_artifacts
    attempt = step.escalation_result['attempt_id']
    resumed = engine.resume_workflow_run(run.workflow_run_id, patched_step_output={'value': 8})
    assert resumed.status == 'succeeded'
    assert resumed.steps[0].status == 'escalated_recovered'
    assert resumed.steps[0].escalation_result['attempt_id'] == attempt
    assert resumed.steps[0].output_artifacts == artifacts
    assert_deliverable(background_http, artifacts[0], b'[7]')


def test_foreach_each_native_batch_manifest_is_persistent_and_downloadable(background_http):  # noqa: F811
    services, _, engine, run = real_run(background_http, foreach=True)
    assert run.status == 'succeeded', run.to_dict()
    batches = WorkflowRunStore(services.owner_home).load_run(run.workflow_run_id).steps[0].batches
    assert len(batches) == 2
    detail = client_for(background_http).call('workflow_runs.get', {'workflow_run_id': run.workflow_run_id})
    assert detail['ok'], detail
    refs = detail['data']['steps'][0]['output_artifacts']
    assert [item['artifact_id'] for item in refs] == [batch['output_artifacts'][0]['artifact_id'] for batch in batches]
    assert detail['data']['steps'][0]['output_artifacts_coverage'] == 'complete'
    assert batches[0]['attempt_id'] != batches[1]['attempt_id']
    for i, batch in enumerate(batches):
        assert batch['status'] == 'succeeded'
        assert len(batch['output_artifacts']) == 1 and batch['output_dir']
        assert_deliverable(background_http, batch['output_artifacts'][0], json.dumps([i + 1]).encode())
    # 已成功的批不能因单纯恢复重放或更换归档attempt。
    resumed = engine.resume_workflow_run(run.workflow_run_id)
    assert resumed.steps[0].batches == batches


def test_duplicate_batch_attempt_is_refused_not_first_match(background_http):  # noqa: F811
    services, _, _, run = real_run(background_http, foreach=True)
    batch = run.steps[0].batches[0]
    run.steps[0].batches[1]['attempt_id'] = batch['attempt_id']
    WorkflowRunStore(services.owner_home).save_run(run)
    reply = client_for(background_http).call('artifacts.get', {'artifact_id': batch['output_artifacts'][0]['artifact_id']})
    assert not reply['ok'] and 'WORKFLOW_ARTIFACT_ATTEMPT_NOT_FOUND' in str(reply['error'])


def test_step_artifact_schema_is_optional_and_bounded():
    from agent_py_agent.agent.plugin_host.business_action_specs import (
        business_defs,
        standalone_schema,
    )
    from agent_py_agent.agent.plugin_host.schemas import validate_object

    schema = standalone_schema(business_defs()['WorkflowStepView'])
    old = {'step_id': 'step', 'mode': 'deterministic_script', 'status': 'succeeded',
           'assignment_ref': None, 'run_id': None, 'result_ref': None, 'error': None}
    validate_object(old, schema)
    item = {'artifact_id': 'wfart-file', 'sha256': 'a' * 64, 'attempt_id': 'attempt', 'batch_index': 0}
    validate_object({**old, 'output_artifacts': [item], 'output_artifacts_coverage': 'complete'}, schema)
    with pytest.raises(ValueError):
        validate_object({**old, 'output_artifacts': [item] * 65}, schema)
