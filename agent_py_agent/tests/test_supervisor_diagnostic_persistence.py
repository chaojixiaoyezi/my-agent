"""监护观察与真实日志FD的持久证据，不启动Gateway。"""
import json
import os
import subprocess
import sys
from types import SimpleNamespace

from agent_py_agent.agent.gateway_parts import supervisor as module
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root


def observer(tmp_path):
    return SimpleNamespace(_paths=gateway_paths_from_root(tmp_path), _restart_count=0, _gateway_child=None)


def test_fresh_lifecycle_has_nullable_evidence_and_preserves_facts(tmp_path):
    item = observer(tmp_path)
    identity = {'pid': 42}
    module._write_supervisor_state(item, identity, 'running')
    fresh = json.loads(item._paths.supervisor_state.read_text())
    assert fresh['last_exit_code'] is None and fresh['last_failure_at'] is None
    assert fresh['last_failure_reason'] == '' and fresh['restart_attempts'] == 0
    module._observe_supervisor_failure(item, 'gateway_health_unconfirmed')
    observed = json.loads(item._paths.supervisor_state.read_text())
    item._restart_count = 2
    module._write_supervisor_state(item, identity, 'stopped')
    final = json.loads(item._paths.supervisor_state.read_text())
    assert final['first_failure_at'] == observed['first_failure_at']
    assert final['last_failure_reason'] == 'gateway_health_unconfirmed'
    assert final['restart_attempts'] == 2 and final['last_exit_code'] is None


def test_owned_child_exit_only_and_no_alert_from_observation(tmp_path, monkeypatch):
    item = observer(tmp_path)
    monkeypatch.setattr(module, '_deliver_supervisor_admin_alert', lambda *a: (_ for _ in ()).throw(AssertionError('unexpected alert')))
    module._observe_supervisor_failure(item, 'gateway_health_unconfirmed')
    assert json.loads(item._paths.supervisor_state.read_text())['last_exit_code'] is None
    from agent_py_agent.agent.gateway_parts.supervisor_generation import SpawnedGateway

    with subprocess.Popen([sys.executable, '-c', 'raise SystemExit(17)']) as process:
        process.wait(timeout=10)
        item._gateway_child = SpawnedGateway(process, {}, os.getpid())
        module._observe_supervisor_failure(item, 'gateway_start_child_exited')
        assert json.loads(item._paths.supervisor_state.read_text())['last_exit_code'] is None
        item._gateway_child_origin = item._gateway_child
        module._observe_supervisor_failure(item, 'gateway_start_child_exited')
    item._gateway_child = None
    module._observe_supervisor_failure(item)
    assert json.loads(item._paths.supervisor_state.read_text())['last_exit_code'] == 17


def test_direct_sink_and_rotation_write_each_event_once(tmp_path):
    log = tmp_path / 'supervisor.log'
    source = '''
import os
import sys
from pathlib import Path
from agent_py_agent.agent.observability import log_volume_rotation as r
from agent_py_agent.agent.gateway_parts.supervisor import GatewaySupervisor
path = Path(sys.argv[1])
with r.supervisor_log_sink(path):
    obj = object.__new__(GatewaySupervisor)
    obj.log_path = path
    obj._log_info('unique-event-one')
    r.DAEMON_LOG_VOLUME_MAX_BYTES = 1
    r.rotate_log_volumes_once(path, now=0)
    obj._log_info('unique-event-two')
print('restored-stdout')
'''
    result = subprocess.run([sys.executable, '-c', source, str(log)], text=True, capture_output=True, timeout=20,
                            env={**os.environ, 'PYTHONPATH': os.getcwd()})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'restored-stdout'
    data = ''.join(path.read_text() for path in tmp_path.iterdir() if path.name.startswith('supervisor.log') and not path.name.endswith('.lock'))
    assert data.count('unique-event-one') == data.count('unique-event-two') == 1
    assert 'unique-event-two' in log.read_text()


def test_sink_failure_reports_unpersisted_event_and_next_action(tmp_path, capsys):
    item = object.__new__(module.GatewaySupervisor)
    item.log_path = tmp_path
    item._append_log_file('event')
    error = capsys.readouterr().err
    assert 'SUPERVISOR_LOG_WRITE_FAILED' in error and 'IsADirectoryError' in error
    assert 'event_not_persisted' in error and '父目录权限' in error
