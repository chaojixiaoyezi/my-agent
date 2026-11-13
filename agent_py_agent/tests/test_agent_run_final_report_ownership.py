"""宿主默认报告不能夺取用户/代理已发表文件的所有权。"""
import hashlib
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.memory_archive import agent_run_workspace as workspace
from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool, _atomic_write_bytes


def task(result='宿主默认结果'):
    return SimpleNamespace(id='child', status='DONE', result=result)


@pytest.mark.parametrize('original', ['代理已发布报告\n', '# Final Report\n\n待完成后填写\n'])
def test_host_closeout_preserves_existing_report_and_registered_hash(tmp_path, original):
    path = tmp_path / 'final_report.md'
    _atomic_write_bytes(path, original.encode())
    registered_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    workspace._write_final_report(path, task(), 'root')
    assert path.read_text() == original
    assert hashlib.sha256(path.read_bytes()).hexdigest() == registered_hash


def test_default_report_is_published_only_when_result_exists(tmp_path):
    path = tmp_path / 'final_report.md'
    workspace._write_final_report(path, task(''), 'root')
    assert '待完成后填写' in path.read_text()
    workspace._write_final_report(path, task(), 'root')
    assert '宿主默认结果' in path.read_text()
    assert '- task_id: root' in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    workspace._write_final_report(path, task('迟到不同结果'), 'root')
    assert '迟到不同结果' not in path.read_text()


def test_normal_atomic_write_wins_closeout_publication_race(tmp_path, monkeypatch):
    path = tmp_path / 'work' / 'agents' / 'subagent-child' / 'final_report.md'
    path.parent.mkdir(parents=True)
    workspace._write_final_report(path, task(''), 'root')
    ready, publish = Event(), Event()
    original_replace = workspace.os.replace
    errors = []

    def blocked_replace(source, target):
        if str(source).split('/')[-1].startswith('.final-report-'):
            ready.set()
            assert publish.wait(3)
        return original_replace(source, target)

    def closeout():
        try:
            workspace._write_final_report(path, task(), 'root')
        except BaseException as exc:
            errors.append(exc)

    def agent_write():
        try:
            _atomic_write_bytes(path, b'agent-published')
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(workspace.os, 'replace', blocked_replace)
    worker = Thread(target=closeout)
    worker.start()
    assert ready.wait(3)
    writer = Thread(target=agent_write)
    writer.start()
    publish.set()
    worker.join(3)
    writer.join(3)
    assert not worker.is_alive() and not writer.is_alive() and not errors
    assert path.read_bytes() == b'agent-published'
    assert not list(path.parent.glob('.final-report-*'))


def test_normal_file_write_does_not_create_sidecar(tmp_path):
    path = tmp_path / 'ordinary.md'
    _atomic_write_bytes(path, b'normal')
    assert list(tmp_path.iterdir()) == [path]


def test_exact_legacy_placeholder_migrates_but_different_identity_does_not(tmp_path):
    path = tmp_path / 'final_report.md'
    placeholder = '# Final Report\n\n- task_id: root\n- run_id: child\n- status: RUNNING\n\n## Result\n\n待完成后填写\n'
    path.write_text(placeholder)
    workspace._write_final_report(path, task(), 'root')
    assert '宿主默认结果' in path.read_text()
    path.write_text(placeholder.replace('task_id: root', 'task_id: other'))
    workspace._write_final_report(path, task(), 'root')
    assert path.read_text() == placeholder.replace('task_id: root', 'task_id: other')


def test_real_write_file_report_remains_authoritative_after_host_closeout(tmp_path):
    path = tmp_path / 'work' / 'agents' / 'subagent-child' / 'final_report.md'
    path.parent.mkdir(parents=True)
    workspace._write_final_report(path, task(''), 'root')
    result = WriteFileTool(tmp_path).execute({'path': str(path), 'content': '原代理报告，已登记。\n'})
    assert result.ok, result
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    workspace._write_final_report(path, task(), 'root')
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


@pytest.mark.parametrize('result', ['', '宿主默认结果'])
def test_existing_report_skip_never_prepares_or_fsyncs(tmp_path, monkeypatch, result):
    path = tmp_path / 'final_report.md'
    workspace._write_final_report(path, task(''), 'root')
    if result:
        path.write_text('已发表代理正文')

    def forbidden(*args, **kwargs):
        pytest.fail('unchanged report must not prepare or fsync a temporary file')

    monkeypatch.setattr(workspace.tempfile, 'mkstemp', forbidden)
    monkeypatch.setattr(workspace.os, 'fsync', forbidden)
    workspace._write_final_report(path, task(result), 'root')
