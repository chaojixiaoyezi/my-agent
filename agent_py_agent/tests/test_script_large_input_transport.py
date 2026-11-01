"""真实 subprocess 验证 UTF-8 大输入，不把无 E2BIG 外推成平台隔离验收。"""
import hashlib
import json

import pytest

from agent_py_agent.agent.workflows import ScriptRunner, WorkflowEngine
from agent_py_agent.tests.test_workflow_script_engine import script, step, workflow


def test_exact_1mib_output_maps_to_complete_unicode_input(tmp_path, monkeypatch):
    monkeypatch.setenv('HOST_TRANSPORT_SECRET', 'host-only-value')
    script(tmp_path, '''import sys
prefix, suffix = '{"value":"', '"}'
size = 1024 * 1024 - len(prefix.encode()) - len(suffix.encode())
text = '鲸' * (size // 3) + 'x' * (size % 3)
sys.stdout.write(prefix + text + suffix)
''')
    script(tmp_path, '''import hashlib, json, os, pathlib, stat, sys
raw = sys.stdin.buffer.read()
inputs = json.loads(raw)
assert 'MY_AGENT_SCRIPT_INPUT_JSON' not in os.environ
assert os.environ['MY_AGENT_SCRIPT_INPUT_PROTOCOL'] == 'script-input.v2'
assert os.environ['MY_AGENT_SCRIPT_INPUT_MODE'] == 'file'
path = pathlib.Path(os.environ['MY_AGENT_SCRIPT_INPUT_FILE'])
assert path.read_bytes() == raw
assert stat.S_IMODE(path.stat().st_mode) == 0o600
assert path.parent == pathlib.Path.cwd()
assert 'HOST_TRANSPORT_SECRET' not in os.environ
print(json.dumps({'value': {'chars': len(inputs['value']),
    'bytes': len(inputs['value'].encode()), 'sha256': hashlib.sha256(inputs['value'].encode()).hexdigest()}}))
''', script_id='consume', metadata={'script_input_protocol': 'script-input.v2'})
    result = WorkflowEngine(tmp_path).run_workflow(workflow(step(), step('consume', script_id='consume',
        inputs_map={'value': {'step_id': 'collect', 'key': 'value'}})))
    assert result.status == 'succeeded', result.to_dict()
    original = result.steps[0].output_snapshot['value']
    assert len(json.dumps({'value': original}, ensure_ascii=False, separators=(',', ':')).encode()) == 1024 * 1024
    assert result.final_output['value'] == {'chars': len(original), 'bytes': len(original.encode()),
                                           'sha256': hashlib.sha256(original.encode()).hexdigest()}


@pytest.mark.parametrize('length', [8192 - len('{"value": ""}'), 8193 - len('{"value": ""}')])
def test_input_inline_threshold_and_original_stdin(tmp_path, length):
    script(tmp_path, '''import json, os, sys
raw = sys.stdin.read()
if len(raw.encode()) <= 8192:
    assert os.environ['MY_AGENT_SCRIPT_INPUT_JSON'] == raw
    assert os.environ['MY_AGENT_SCRIPT_INPUT_MODE'] == 'inline'
else:
    assert 'MY_AGENT_SCRIPT_INPUT_JSON' not in os.environ
    assert os.environ['MY_AGENT_SCRIPT_INPUT_MODE'] == 'file'
print(json.dumps({'value': len(json.loads(raw)['value'])}))
''', metadata={'script_input_protocol': 'script-input.v2'})
    result = ScriptRunner(tmp_path).run_script('collect', {'value': 'x' * length})
    assert result.ok, result.escalation_context
    assert result.output_data == {'value': length}


def test_legacy_large_env_consumer_gets_explicit_migration_error(tmp_path):
    script(tmp_path, 'import os; print(os.environ.get("MY_AGENT_SCRIPT_INPUT_JSON", "{}"))')
    result = ScriptRunner(tmp_path).run_script('collect', {'value': 'x' * 200000})
    assert result.error_code == 'SCRIPT_INPUT_PROTOCOL_REQUIRED'
    assert result.phase == 'preflight' and result.effect_outcome == 'not_started'


def test_one_byte_over_output_limit_still_rejected(tmp_path):
    script(tmp_path, 'import sys; sys.stdout.write("x" * (1024 * 1024 + 1))')
    result = ScriptRunner(tmp_path).run_script('collect')
    assert result.error_code == 'SCRIPT_OUTPUT_LIMIT'
