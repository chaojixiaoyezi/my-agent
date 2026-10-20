"""私卡原503诊断仅输出有界结构元数据，不能改变原响应或访问秘密。"""
import errno
import io
import json
import sqlite3
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import private_interaction_service as service
from agent_py_agent.agent.gateway_parts.multiprocess_global_write import GlobalWriteError

SECRET = 'synthetic-secret-body-password-url-reference'


def invoke(monkeypatch, error=None, result=None):
    seen = []
    original = []
    handler = SimpleNamespace(headers={'X-Channel': 'feishu', 'X-User-Id': SECRET},
        _auth_middleware=SimpleNamespace(has_gateway_credential=lambda headers: True),
        _read_json=lambda: {'fields': {'password': SECRET}, 'reference': SECRET},
        _send_json=lambda status, body: seen.append((status, body)))
    server = SimpleNamespace(agent=object(), paths=object())

    def resolve(*args, **kwargs):
        original.append((args, kwargs))
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(service, 'resolve_private_interaction', resolve)
    service.handle_private_interaction(handler, server)
    assert len(original) == 1 and original[0][0][1]['fields']['password'] == SECRET
    return seen


def diagnostic(capsys):
    raw = capsys.readouterr().err
    assert SECRET not in raw and 'password' not in raw and 'reference' not in raw
    lines = raw.splitlines()
    assert len(lines) == 1 and lines[0].startswith('[gateway-private-interaction-diagnostic] ')
    return json.loads(lines[0].split('] ', 1)[1])


@pytest.mark.parametrize('kind', ['os', 'runtime', 'sqlite', 'global'])
def test_original_503_has_only_safe_exception_facts(monkeypatch, capsys, kind):
    errors = {'os': OSError(errno.EACCES, SECRET, '/synthetic/' + SECRET),
        'runtime': RuntimeError(SECRET), 'sqlite': sqlite3.OperationalError(SECRET),
        'global': GlobalWriteError(SECRET, SECRET)}
    error = errors[kind]
    seen = invoke(monkeypatch, error)
    assert seen == [(503, {'ok': False, 'error_code': 'IM_INTERACTION_HOST_UNAVAILABLE',
        'error_stage': 'execution', 'message': '宿主暂时无法处理卡片，请稍后重试。'})]
    row = diagnostic(capsys)
    assert set(row) == {'schema_version', 'error_type', 'error_code', 'errno', 'origin'}
    assert row['error_type'] == type(error).__name__
    assert row['error_code'] == ('GLOBAL_WRITE_REJECTED' if kind == 'global' else None)
    assert row['errno'] == (errno.EACCES if kind == 'os' else None)
    assert row['origin'] and all(set(frame) == {'function', 'line'} for frame in row['origin'])
    assert len(row['origin']) <= 12 and row['origin'][-1]['function'] == 'resolve'


def test_unknown_exception_hooks_and_arbitrary_code_are_never_read(monkeypatch, capsys):
    class Hostile(RuntimeError):
        error_code = 'SYNTHETIC_SECRET_MACHINE_CODE'
        def __getattribute__(self, name):
            if name in {'args', '__dict__', '__traceback__', 'errno', 'error_code'}:
                raise AssertionError('must not read custom attributes')
            return super().__getattribute__(name)
        def __str__(self):
            raise AssertionError('must not stringify')
        def __repr__(self):
            raise AssertionError('must not repr')
    assert invoke(monkeypatch, Hostile(SECRET))[0][0] == 503
    row = diagnostic(capsys)
    assert row['error_type'] == 'Hostile' and row['error_code'] is None and row['errno'] is None


@pytest.mark.parametrize('fault', ['write', 'flush', 'json', 'interrupt'])
def test_diagnostic_sink_failure_never_prevents_original_503(monkeypatch, fault):
    class Sink(io.StringIO):
        def write(self, value):
            if fault in {'write', 'interrupt'}:
                raise KeyboardInterrupt() if fault == 'interrupt' else OSError(SECRET)
            return super().write(value)
        def flush(self):
            if fault == 'flush':
                raise OSError(SECRET)
            return super().flush()
    monkeypatch.setattr(service.sys, 'stderr', Sink())
    if fault == 'json':
        def fail(*args, **kwargs):
            raise RuntimeError(SECRET)
        monkeypatch.setattr(service.json, 'dumps', fail)
    assert invoke(monkeypatch, RuntimeError(SECRET))[0][0] == 503


@pytest.mark.parametrize('kind', ['invalid', 'form', 'success'])
def test_original_non503_paths_do_not_emit_diagnostic(monkeypatch, capsys, kind):
    from agent_py_agent.agent.user_space.task_credential_forms import CredentialFormValidationError
    error = ValueError(SECRET) if kind == 'invalid' else CredentialFormValidationError('fields') if kind == 'form' else None
    result = {'ok': True, 'decision': 'ready'}
    seen = invoke(monkeypatch, error, result)
    assert seen[0][0] == {'invalid': 403, 'form': 400, 'success': 200}[kind]
    if kind == 'success':
        assert seen[0][1] is result
    assert capsys.readouterr().err == ''
