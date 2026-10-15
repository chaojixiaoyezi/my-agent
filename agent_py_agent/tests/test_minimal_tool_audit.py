"""后台工具审计与聊天保存解耦，投影中不增加参数、单元格或秘密副本。"""
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.agent_core.runtime import minimal_tool_audit
from agent_py_agent.agent.agent_core.runtime.live_archive import archive_tool_call_if_enabled
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_feishu_cloud_tool import _agent
from agent_py_agent.tests.test_ingestion_task_credential_scope import _active


@pytest.mark.parametrize('kind', ['user', 'group', 'main'])
def test_save_false_preserves_only_minimal_owner_tool_facts(tmp_path, kind):
    identity = {'user':OwnerIdentity.provider_user('feishu','ou_synthetic'),
        'group':OwnerIdentity.provider_group('feishu','oc_synthetic'), 'main':OwnerIdentity.local_main()}[kind]
    agent = _agent(tmp_path, identity)
    thread, actor, _claim = _active(agent)
    params = replace(agent._current_run_params, save=False, source='background_main_agent',
        task_attributes={'conversation_thread_id':thread.thread_id, 'scheduler_job_id':'job-one', 'scheduler_rule_id':'rule-one'})
    record = {'tool':'feishu_cloud', 'id':'call-one', 'operation_id':'tool_call:original', 'ok':False,
        'error_code':'TOOL_OPERATION_OUTCOME_UNKNOWN', 'reported_error_code':'FEISHU_CLOUD_RESPONSE_UNKNOWN',
        'effect_outcome':'unknown', 'failure_stage':'handler', 'handler_executed':True,
        'parameters':{'action':'sheet_append', 'values':[['SYNTHETIC_SECRET_CELL']], 'token':'SYNTHETIC_SECRET_TOKEN'},
        'output':'SYNTHETIC_SECRET_OUTPUT', 'model_summary':'SYNTHETIC_SECRET_SUMMARY',
        'tool_result_envelope':{'cloud_error':{'http_status':503, 'provider_code':777777,
            'request_stage':'provider_response', 'reason':'provider_rejected', 'raw':'SYNTHETIC_SECRET_RAW'}}}
    archive_tool_call_if_enabled(agent, params, record, tool_round=1, tool_index=1)
    path = agent.home_paths.owner_home_dir / 'audit'
    text = '\n'.join(file.read_text() for file in path.glob('*.jsonl'))
    assert 'SYNTHETIC_SECRET' not in text
    event = json.loads(text)
    facts = json.loads(event['content_preview'])
    assert event['source'] == 'minimal_tool_audit'
    assert event['operation_id'] == 'tool_call:original' and event['effect_outcome'] == 'unknown'
    assert facts['action'] == 'sheet_append' and facts['http_status'] == 503 and facts['provider_code'] == 777777
    assert facts['owner_kind'] == kind and facts['actor_id'] == actor.actor_id
    assert facts['thread_id'] == thread.thread_id and facts['job_id'] == 'job-one' and facts['rule_id'] == 'rule-one'
    assert not list((agent.home_paths.owner_home_dir / 'memory').rglob('*runtime_fact*'))


def test_minimal_audit_failure_is_safe_and_does_not_change_operation(tmp_path, monkeypatch, caplog):
    agent = _agent(tmp_path, OwnerIdentity.local_main())
    _active(agent)
    record = {'tool':'feishu_cloud', 'id':'original', 'operation_id':'original', 'ok':False, 'effect_outcome':'unknown'}
    def failed(*_a):
        raise OSError(28, 'SYNTHETIC_SECRET_FAILURE')
    monkeypatch.setattr(minimal_tool_audit, 'append_raw_event', failed)
    archive_tool_call_if_enabled(agent, replace(agent._current_run_params, save=False), record, tool_round=1, tool_index=1)
    assert record['raw_archive_error_type'] == 'OSError'
    assert record['effect_outcome'] == 'unknown'
    assert 'SYNTHETIC_SECRET' not in caplog.text and 'SYNTHETIC_SECRET' not in str(record)


def test_open_action_text_is_never_copied_to_minimal_audit(tmp_path):
    agent = _agent(tmp_path, OwnerIdentity.local_main())
    _active(agent)
    record = {'tool':'feishu_cloud', 'id':'original', 'parameters':{'action':'SYNTHETIC_SECRET_ACTION'}, 'ok':False,
        'reported_error_code':'SYNTHETIC_SECRET_REPORTED_CODE'}
    archive_tool_call_if_enabled(agent, replace(agent._current_run_params, save=False), record, tool_round=1, tool_index=1)
    text = '\n'.join(path.read_text() for path in (agent.home_paths.owner_home_dir/'audit').glob('*.jsonl'))
    assert 'SYNTHETIC_SECRET' not in text


def test_minimal_audit_uses_original_operation_and_canonical_rule(tmp_path):
    from types import SimpleNamespace
    agent = _agent(tmp_path, OwnerIdentity.local_main())
    _active(agent)
    current = agent._current_run_params
    agent.scheduler_repository = SimpleNamespace(get_active_run=lambda task_id:
        {'run_id':task_id, 'job_id':'canonical-job', 'rule_id':'canonical-rule'})
    record = {'tool':'feishu_cloud', 'id':'new-call-id', 'operation_id':'new-request-operation', 'ok':False,
        'tool_result_envelope':{'tool_operation':{'operation_id':'original-unknown-operation', 'status':'unknown', 'action':'reconcile'}}}
    archive_tool_call_if_enabled(agent, replace(current, save=False), record, tool_round=1, tool_index=1)
    event = json.loads(next((agent.home_paths.owner_home_dir/'audit').glob('*.jsonl')).read_text())
    facts = json.loads(event['content_preview'])
    assert event['operation_id'] == 'original-unknown-operation'
    assert facts['job_id'] == 'canonical-job' and facts['rule_id'] == 'canonical-rule'
    assert facts['operation_status'] == 'unknown'
