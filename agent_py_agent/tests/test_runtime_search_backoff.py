"""本地归档剪枝、部分搜索结果及真实静默管道的有界回收，不访问用户数据。"""
from __future__ import annotations

import gc
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_py_agent.agent.tooling import _filesystem_find as find
from agent_py_agent.agent.tooling import _filesystem_helpers as helpers
from agent_py_agent.agent.tooling import _filesystem_search as search
from agent_py_agent.agent.tooling import shell
from agent_py_agent.agent.tooling._filesystem_read import FileSystemAccessOptions

ARCHIVES = ('runs', 'compact', 'memory_archive', 'blobs', '.background_jobs', 'releases',
            '.git', 'node_modules', '__pycache__', '.venv')


def _fd_count():
    gc.collect()
    directory = Path('/dev/fd') if Path('/dev/fd').is_dir() else Path('/proc/self/fd')
    if not directory.is_dir():
        return None
    with os.scandir(directory) as entries:
        return len(list(entries))


@pytest.fixture
def fd_balance():
    gc.collect()
    before = _fd_count()
    yield
    if before is not None:
        gc.collect()
        assert _fd_count() - before <= 0


def _tool(root, kind):
    cls = find.FindFilesTool if kind == 'find' else search.SearchTextTool
    return cls(root, max_matches=100, access_options=FileSystemAccessOptions(path_access_mode='full'))


def _request(kind, **extra):
    return ({'pattern': '*.txt'} if kind == 'find' else
            {'query': 'needle', 'literal': True, 'output_mode': 'count' if kind == 'count' else 'content'}) | extra


def _facts(result, kind):
    return result.result_envelope['file_search_result' if kind == 'find' else 'search_result']


@pytest.mark.parametrize('kind', ['find', 'search'])
@pytest.mark.parametrize('backend', ['python', 'rg'])
def test_runtime_archives_are_pruned_before_descent_but_explicit_paths_work(tmp_path, monkeypatch, kind, backend):
    if backend == 'rg' and not shutil.which('rg'):
        pytest.skip('本机没有 rg；Python 路径由另一个参数覆盖')
    if backend == 'python':
        monkeypatch.setattr(shutil, 'which', lambda _name: None)
    hidden = [tmp_path / name for name in ARCHIVES]
    hidden.append(tmp_path / 'owners/local/main/workspace/runtime')
    for directory in hidden:
        directory.mkdir(parents=True)
        (directory / 'hidden.txt').write_text('needle\n', encoding='utf-8')
    (tmp_path / 'visible.txt').write_text('needle\n', encoding='utf-8')
    entered = []
    scandir = os.scandir

    def record(path):
        entered.append(Path(path))
        return scandir(path)

    monkeypatch.setattr(helpers.os, 'scandir', record)
    tool = _tool(tmp_path, kind)
    result = tool.execute(_request(kind))
    assert result.ok and 'visible.txt' in result.output and 'hidden.txt' not in result.output
    assert not set(hidden) & set(entered)
    assert _facts(result, kind)['scan_complete'] is True
    for directory in hidden:
        explicit = tool.execute(_request(kind, path=str(directory)))
        assert explicit.ok and 'hidden.txt' in explicit.output, (directory, explicit.output)
    included = tool.execute(_request(kind, include_ignored=True))
    assert included.ok and 'runs/hidden.txt' in included.output


@pytest.mark.parametrize('kind', ['find', 'search', 'count'])
def test_python_scan_budget_keeps_partial_matches_and_offending_directory(tmp_path, monkeypatch, kind):
    first, slow = tmp_path / 'a', tmp_path / 'z'
    first.mkdir()
    slow.mkdir()
    (first / 'match.txt').write_text('needle\n', encoding='utf-8')
    (slow / 'late.txt').write_text('needle\n', encoding='utf-8')
    clock = [0.0]
    scandir = os.scandir

    def advance(path):
        if Path(path) == slow:
            clock[0] = 9.0
        return scandir(path)

    tool = _tool(tmp_path, kind)
    with monkeypatch.context() as patch:
        patch.setattr(shutil, 'which', lambda _name: None)
        patch.setattr(helpers.time, 'monotonic', lambda: clock[0])
        patch.setattr(helpers.os, 'scandir', advance)
        result = tool.execute(_request(kind))
    facts = _facts(result, kind)
    assert result.ok and 'a/match.txt' in result.output and 'z/late.txt' not in result.output
    assert facts['status'] == 'incomplete' and facts['scan_complete'] is False
    assert facts['scan_budget']['exhausted'] is True
    assert facts['scan_budget']['budget_seconds'] == 8.0
    assert facts['scan_budget']['limit_path'] == 'z'
    assert '缩小' in result.output


def test_snapshot_directory_fingerprint_is_also_budgeted(tmp_path, monkeypatch, fd_balance):
    for index in range(32):
        (tmp_path / f'{index}.txt').write_text('needle\n', encoding='utf-8')
    scandir, clock, seen = os.scandir, [0.0], []

    @contextmanager
    def timed_entries(path):
        with scandir(path) as entries:
            def ticks():
                for entry in entries:
                    seen.append(entry.name)
                    clock[0] += 1
                    yield entry
            yield ticks()

    tool = _tool(tmp_path, 'find')
    with monkeypatch.context() as patch:
        patch.setattr(find.time, 'monotonic', lambda: clock[0])
        patch.setattr(find.os, 'scandir', timed_entries)
        result = tool.execute(_request('find', snapshot=True))
    assert not result.ok and result.error_code == 'STALE_VERSION'
    assert result.result_envelope['snapshot']['limit_reason'] == 'validation_budget'
    assert len(seen) < 32 and '缩小' in result.output


@pytest.mark.parametrize('kind', ['find', 'search', 'count'])
@pytest.mark.parametrize('emit_match', [False, True])
def test_native_search_silent_pipe_has_a_deadline_and_reaps(tmp_path, monkeypatch, fd_balance, kind, emit_match):
    source = tmp_path / 'match.txt'
    source.write_text('needle\n', encoding='utf-8')
    processes = []
    monotonic = time.monotonic
    monkeypatch.setattr(helpers, '_FALLBACK_SCAN_MAX_SECONDS', 0.8)
    if kind == 'find':
        monkeypatch.setattr(shutil, 'which', lambda _name: 'rg')
    event = str(source) if kind == 'find' else json.dumps({'type': 'match', 'data': {
        'path': {'text': str(source)}, 'lines': {'text': 'needle\n'}, 'line_number': 1}})

    def spawn(*_args):
        # 只替换外部二进制：仍通过真实 OS 管道消费协议并由生产终止路径回收。
        script = 'import time\n'
        if emit_match:
            script += f'print({event!r}, flush=True)\n'
        script += 'time.sleep(3)\n'
        process = subprocess.Popen([sys.executable, '-u', '-c', script], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=kind == 'find')
        processes.append(process)
        return process

    monkeypatch.setattr(find if kind == 'find' else search,
        '_start_rg_find_process' if kind == 'find' else '_start_rg_process', spawn)
    started = monotonic()
    result = _tool(tmp_path, kind).execute(_request(kind))
    elapsed = monotonic() - started
    facts = _facts(result, kind)
    assert elapsed < 2.5
    assert result.ok and facts['status'] == 'incomplete' and facts['scan_complete'] is False
    assert facts['scan_budget']['exhausted'] is True
    assert facts['scan_budget']['limit_path'] == '.'
    assert ('match.txt' in result.output) is emit_match
    assert processes and all(process.poll() is not None and process.stdout.closed for process in processes)


@pytest.mark.parametrize('command,where,guarded', [
    ("grep -rl 'needle' {root} --include=*.json", 'project', True),
    ('rg needle {root}', 'project', True),
    ('rg --files {root}', 'project', True),
    ("find {root} -name '*.json'", 'project', True),
    ('grep -r -e needle {owner}', 'project', True),
    ("rg --glob '*.json' needle {owner}", 'project', True),
    ('grep -r needle .', 'owner', True),
    ('rg needle', 'owner', True),
    ('find .', 'owner', True),
    ('grep needle {root}', 'project', False),
    ('echo {root}', 'project', False),
    ('rg {root} notes', 'project', False),
    ('find {root} -exec echo checked \\;', 'project', False),
    ('rg needle {root} && echo done', 'project', False),
    ('rg --pre formatter needle {root}', 'project', False),
    ('rg needle {root}/runs', 'project', False),
    ('rg needle {root}/config', 'project', False),
    ('grep -rl needle .', 'project', False),
])
def test_shell_backoff_only_matches_readonly_wide_runtime_targets(tmp_path, monkeypatch, command, where, guarded):
    root = tmp_path / 'data'
    owner = root / 'owners/local/main'
    project = tmp_path / 'project'
    owner.mkdir(parents=True)
    project.mkdir()
    monkeypatch.setenv('MY_AGENT_HOME', str(root))
    tool = shell.ShellTool(project)
    text = command.format(root=shlex.quote(str(root)), owner=shlex.quote(str(owner)))
    facts = shell._runtime_search_backoff(tool, text, owner if where == 'owner' else project, 240)
    assert (facts is not None) is guarded
    if guarded:
        assert facts['budget_seconds'] == 15 and facts['requested_timeout_seconds'] == 240
        assert set(ARCHIVES) <= set(facts['excluded_directories_hint'])


def test_shell_runtime_search_returns_partial_output_and_structured_backoff(tmp_path, monkeypatch, fd_balance):
    root = tmp_path / 'data'
    root.mkdir()
    monkeypatch.setenv('MY_AGENT_HOME', str(root))
    monkeypatch.setattr(shell, '_RUNTIME_SEARCH_MAX_SECONDS', 0.2)
    tool = shell.ShellTool(tmp_path, options=shell.ShellToolOptions(path_access_mode='full', default_timeout=240))
    command = 'rg needle ' + shlex.quote(str(root))
    observed, processes = {}, []

    def run(command_text, target, timeout, *_roots):
        observed.update(command=command_text, timeout=timeout)
        process = subprocess.Popen([sys.executable, '-u', '-c',
            'import time; print("partial match", flush=True); time.sleep(2)'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=os.name == 'posix')
        processes.append(process)
        try:
            return shell._communicate_process(process, command=command_text, timeout=timeout)
        finally:
            shell._close_foreground_pipes(process)

    monkeypatch.setattr(tool, '_run_command', run)
    result = tool.execute({'command': command, 'timeout': 240})
    assert observed == {'command': command, 'timeout': 0.2}
    assert not result.ok and result.error_code == 'TOOL_TIMEOUT'
    facts = result.result_envelope['process']
    assert facts['search_backoff']['target_paths'] == [str(root)]
    assert facts['search_backoff']['reason'] == 'runtime_root_search_budget'
    assert facts['termination']['confirmed'] is True
    assert 'partial match' in result.output and 'search_text / find_files' in result.output
    assert all(process.poll() is not None for process in processes)


def test_search_tool_contracts_explain_archives_budgets_and_narrowing(tmp_path):
    for tool in (_tool(tmp_path, 'find'), _tool(tmp_path, 'search'), shell.ShellTool(tmp_path)):
        description = tool.model_spec.description
        assert '~/.my-agent' in description and 'owner_home' in description
        assert 'grep -r' in description and '归档' in description
        assert '8 秒' in description if tool.model_spec.name != 'run_command' else '15 秒' in description
