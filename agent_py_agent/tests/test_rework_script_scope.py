"""R3-1/F1：注册入口、原生spec与真实Python子进程共用冻结正负路径边界。"""
from __future__ import annotations

import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import self_map_bootstrap, tool_runtime_ledger
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.path_access_policy import (
    HostSandboxPaths,
    host_sandbox_paths,
    persona_process_protected_paths,
    task_host_state_paths,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.write_boundary import SandboxWriteProtection
from agent_py_agent.agent.workflows import script_runner


@pytest.fixture
def case(tmp_path, monkeypatch):
    # 指定venv有editable旧检出；不能把旧检出的绿灯算到当前工作树。
    expected = Path(__file__).resolve().parents[1]
    assert Path(script_runner.__file__).resolve().is_relative_to(expected)
    assert Path(self_map_bootstrap.__file__).resolve().is_relative_to(expected)
    project = tmp_path / 'project'
    project.mkdir()
    agent = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / 'home'), memory_path='memory.jsonl',
                                    orphan_supervision_interval_seconds=0), project)
    owner = Path(agent.home_paths.owner_home_dir).resolve()
    workspace = owner / 'tasks/2026-10-31/business'
    workspace.mkdir(parents=True)
    agent.effective_workspace_root = workspace
    agent._current_run_params = RunParams(run_id='scope-run', task_id='scope-task', attempt_id='scope-attempt',
                                          task_attributes={})
    captured = []
    original = script_runner._can_apply_sandbox

    def inspect_spec(sandbox, *, strict=False):
        captured.append(sandbox.spec)
        return original(sandbox, strict=strict)

    # 只选择已有非strict外层Seatbelt回退；spec和真实Python权限裁决都不替换。
    monkeypatch.setattr(script_runner, '_outer_seatbelt_active', lambda: True)
    monkeypatch.setattr(script_runner, '_can_apply_sandbox', inspect_spec)
    return SimpleNamespace(agent=agent, owner=owner, workspace=workspace, specs=captured)


def _save(case, code):
    ScriptStore(case.owner, workspace_root=case.workspace).save_script(
        AssetDescriptor('scope-check', 'script', 'scope-check'), code)


def _run(case, code, *, inputs=None):
    _save(case, code)
    tool = case.agent.tools.tools['scripts']
    assert tool.runner is None
    outcome = tool.execute({'action': 'run', 'script_id': 'scope-check', 'inputs': inputs or {}})
    return outcome, json.loads(outcome.output)


def _probe(writes=(), reads=(), mutations=()):
    # 每个操作是真实文件IO；预期错误只捕获PermissionError，其他错误必须导致测试失败。
    return f'''import json, os
from pathlib import Path
result={{}}
for name, raw in {list(writes)!r}:
    try:
        Path(raw).write_text('business-output')
        result[name]=True
    except PermissionError:
        result[name]=False
for name, raw in {list(reads)!r}:
    try:
        Path(raw).read_text()
        result[name]=True
    except PermissionError:
        result[name]=False
for name, raw in {list(mutations)!r}:
    try:
        os.chmod(raw, 0o700)
        result[name]=True
    except PermissionError:
        result[name]=False
print(json.dumps(result))
'''


def test_registered_script_writes_authorized_business_output(case):
    output = case.workspace / 'report.txt'
    outcome, data = _run(case, f'''import json, os
from pathlib import Path
context=json.loads(os.environ['MY_AGENT_SCRIPT_WORK_CONTEXT_JSON'])
Path({str(output)!r}).write_text('verified output')
print(json.dumps(context))
''')
    assert outcome.ok, data
    assert output.read_text() == 'verified output'
    assert str(case.owner) in data['output_data']['effective_path_scope']['write_roots']
    assert case.owner in case.specs[-1].extra_write_roots
    assert Path(data['output_data']['scratch_dir']) == case.specs[-1].attempt_view


def test_registered_workflow_script_step_writes_with_same_provider(case):
    output = case.workspace / 'workflow-report.txt'
    _save(case, _probe(writes=[('written', str(output))]))
    tool = case.agent.tools.tools['workflows']
    definition = {'descriptor': AssetDescriptor('scope-flow', 'workflow', 'scope-flow').to_dict(),
                  'steps': [{'step_id': 'write', 'name': 'write', 'mode': 'deterministic_script',
                             'script_id': 'scope-check', 'on_error': 'fail'}]}
    assert tool.execute({'action': 'save', 'workflow_id': 'scope-flow', 'definition': definition}).ok
    outcome = tool.execute({'action': 'run', 'workflow_id': 'scope-flow'})
    assert outcome.ok, outcome.output
    assert output.is_file(), outcome.output
    assert output.read_text() == 'business-output'
    assert case.owner in case.specs[-1].extra_write_roots


@pytest.mark.parametrize('read_roots', [[], ['workspace']])
def test_explicit_empty_or_readonly_scope_does_not_grant_business_write(case, read_roots):
    params = case.agent._current_run_params
    params.context_scope = 'control_plane'
    params.write_boundary = {'allowed_read_roots': [str(case.workspace)] if read_roots else [],
                             'allowed_write_roots': []}
    output = case.workspace / 'denied.txt'
    outcome, data = _run(case, _probe(writes=[('written', str(output))]),
                         inputs={'allowed_write_roots': [str(case.owner)]})
    assert outcome.ok, data
    assert data['output_data'] == {'written': False}
    assert not output.exists()
    options = case.agent.tools.tools['scripts'].runner_options_provider()
    assert options.get('public_read_roots') == ((case.workspace,) if read_roots else ())
    assert options.get('extra_write_roots') == ()
    assert options['work_context']['effective_path_scope']['write_roots'] == []
    assert case.specs[-1].extra_write_roots == (case.specs[-1].attempt_view,)


def test_ordinary_forbidden_roots_keep_specificity_and_deny_ties(case):
    ordinary = case.owner / 'ordinary'
    reopened = ordinary / 'reopened'
    tied, deeper = ordinary / 'tied', reopened / 'deeper'
    for path in (ordinary, reopened, tied, deeper):
        path.mkdir(parents=True, exist_ok=True)
    case.agent._current_run_params.write_boundary = {
        'allowed_write_roots': [str(reopened), str(tied)],
        'forbidden_write_roots': [str(ordinary), str(tied), str(deeper)]}
    paths = {'ancestor-deny': ordinary / 'x.txt', 'narrow-allow': reopened / 'x.txt',
             'tie-deny': tied / 'x.txt', 'deeper-deny': deeper / 'x.txt'}
    outcome, data = _run(case, _probe(writes=[(name, str(path)) for name, path in paths.items()]))
    assert outcome.ok, data
    assert data['output_data'] == {'ancestor-deny': False, 'narrow-allow': True,
                                   'tie-deny': False, 'deeper-deny': False}
    assert set(case.specs[-1].write_scope_denied_roots) >= {ordinary, tied, deeper}


@pytest.mark.parametrize('exit_code', [0, 3])
def test_actual_scratch_is_private_writable_and_cleaned_on_success_and_error(case, exit_code):
    case.agent._current_run_params.context_scope = 'control_plane'
    case.agent._current_run_params.write_boundary = {'allowed_read_roots': [], 'allowed_write_roots': []}
    outcome, data = _run(case, f'''import os,json,sys
from pathlib import Path
context=json.loads(os.environ['MY_AGENT_SCRIPT_WORK_CONTEXT_JSON'])
scratch=Path(context['scratch_dir'])
(scratch/'temporary.txt').write_text('scratch works')
try:
    (scratch.parent/'sibling.txt').write_text('not authorized')
    sibling=True
except PermissionError:
    sibling=False
print(json.dumps({{'scratch':str(scratch),'cwd':str(Path.cwd()),
    'mode':scratch.stat().st_mode & 0o777,'value':(scratch/'temporary.txt').read_text(),'sibling':sibling}}))
sys.exit({exit_code})
''')
    assert outcome.ok == (exit_code == 0), data
    assert data['exit_code'] == exit_code
    if exit_code:
        assert data['error_code'] == 'SCRIPT_EXIT_NONZERO'
    emitted = json.loads(data['stdout_preview'])
    scratch = Path(emitted['scratch'])
    assert scratch == Path(emitted['cwd']) == case.specs[-1].attempt_view
    assert scratch.parent == case.owner / 'tmp/script_exec'
    assert not scratch.is_relative_to(case.workspace)
    assert emitted['mode'] == 0o700 and emitted['value'] == 'scratch works'
    assert emitted['sibling'] is False and not scratch.exists()


def test_broad_owner_grant_cannot_mutate_absolute_protections_or_their_ancestors(case, monkeypatch):
    from agent_py_agent.agent.agent_core import runtime_write_guards

    install = case.owner / 'runtime-install'
    locked = case.workspace / 'locked/record.txt'
    task_records = task_host_state_paths(case.workspace)
    targets = {'data': case.owner / 'data/business.txt', 'config': case.owner / 'config/business.txt',
               'locked': locked, 'install': install / 'module.py',
               'persona': case.owner / 'AGENTS.md', 'persona-control': case.owner / 'persona/record.txt',
               'task-record': task_records[0] / 'record.txt',
               'foreign-owner': case.owner.parent / 'another-owner/record.txt'}
    for path in targets.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text('original')
    monkeypatch.setattr(runtime_write_guards, 'running_install_roots', lambda: (install,))
    case.agent._current_run_params.write_boundary = {
        'allowed_write_roots': [str(case.owner), str(locked.parent), str(install)],
        'locked_files': [str(locked)], 'task_root': str(case.workspace)}
    before = {key: path.read_bytes() for key, path in targets.items()}
    mutations = [('locked-parent', str(locked.parent)), ('install-parent', str(install)),
                 ('task-record-parent', str(task_records[0].parent)), ('owner-node', str(case.owner))]
    outcome, data = _run(case, _probe(writes=[(name, str(path)) for name, path in targets.items()],
                                     mutations=mutations))
    assert outcome.ok, data
    assert data['output_data'] == dict.fromkeys([*targets, *(name for name, _ in mutations)], False)
    assert {key: path.read_bytes() for key, path in targets.items()} == before
    spec = case.specs[-1]
    host = host_sandbox_paths(case.agent.home_paths.root, case.owner, config=case.agent.config)
    assert set(spec.protected_write_paths) >= {locked, install, *task_records, *host.readonly}
    assert spec.protected_persona_root == case.owner
    assert spec.protected_write_patterns == host.patterns
    assert spec.protected_directory_write_patterns == host.directory_write_patterns
    assert spec.protected_directory_unlink_patterns == host.directory_unlink_patterns


def test_host_hidden_and_exclusive_sources_stay_inaccessible_under_broad_grant(case):
    hidden = case.owner / 'data/plugins/private.txt'
    hidden.parent.mkdir(parents=True, exist_ok=True)
    hidden.write_text('hidden fixture')
    source = case.workspace / 'exclusive-source.txt'
    source.write_text('exclusive fixture')
    case.agent.config.host_exclusive_path_roots = [str(source)]
    options = case.agent.tools.tools['scripts'].runner_options_provider()
    assert isinstance(options.get('host_path_protection'), HostSandboxPaths)
    exclusive = options['host_exclusive_paths']
    paths = [('hidden', str(hidden)), ('exclusive', str(source))]
    # 只使用测试隔离home中真实创建的来源，不读写操作者配置。
    for index, path in enumerate(exclusive):
        path = Path(path)
        if path.is_relative_to(case.agent.home_paths.root) and path.is_file():
            paths.append((f'exclusive-{index}', str(path)))
    writes = [(f'write-{name}', path) for name, path in paths]
    reads = [(f'read-{name}', path) for name, path in paths]
    outcome, data = _run(case, _probe(writes=writes, reads=reads))
    assert outcome.ok, data
    assert data['output_data'] == dict.fromkeys((name for name, _ in [*writes, *reads]), False)
    assert hidden.read_text() == 'hidden fixture' and source.read_text() == 'exclusive fixture'
    spec = case.specs[-1]
    assert set(spec.hidden_paths) >= {hidden.parent, *map(Path, exclusive)}
    assert str(source) not in json.dumps(options['work_context'])


@pytest.mark.parametrize('read_roots', [[], ['.', 'nested/..']])
def test_provider_freezes_one_boundary_and_normalizes_identical_public_and_execution_roots(case, monkeypatch, read_roots):
    calls = []
    frozen = {'execution_cwd': str(case.workspace), 'allowed_read_roots': read_roots,
              'allowed_write_roots': ['out/..', '.'], 'locked_files': ['locked.txt'],
              'runtime_install_roots': ['runtime'], 'forbidden_write_roots': ['ordinary'],
              'task_root': str(case.workspace)}

    def boundary(_agent, _params):
        calls.append(True)
        return frozen if len(calls) == 1 else {'execution_cwd': str(case.owner / 'wrong')}

    # 单元层只替换可信边界的供应；上面的集成测试仍走真实运行账本与默认runner。
    monkeypatch.setattr(tool_runtime_ledger, 'write_boundary_with_runtime_ledger', boundary)
    options = self_map_bootstrap._script_runtime_options(case.agent)
    assert calls == [True]
    reads = (case.workspace,) if read_roots else ()
    assert options.get('public_read_roots') == reads
    assert options.get('extra_write_roots') == (case.workspace,)
    assert options['work_context']['effective_path_scope'] == {
        'read_roots': [str(path) for path in reads], 'write_roots': [str(case.workspace)]}
    protection = options.get('write_protection')
    assert isinstance(protection, SandboxWriteProtection)
    assert set(protection.absolute_paths) == {case.workspace / 'locked.txt', case.workspace / 'runtime',
                                             *task_host_state_paths(case.workspace)}
    assert protection.denied_roots == (case.workspace / 'ordinary',)
    public = json.dumps(options['work_context'])
    assert 'host_exclusive_paths' not in public and 'credential' not in public
    frozen['allowed_write_roots'].append(str(case.owner / 'later'))
    assert options['extra_write_roots'] == (case.workspace,)


def test_direct_runner_defaults_remain_compatible_and_explicit_protections_enable_guard(case):
    _save(case, 'print("{}")')
    runner = script_runner.ScriptRunner(case.owner, use_sandbox=False, workspace_root=case.workspace)
    assert not runner.restrict_to_roots and runner.extra_write_roots == () and runner.public_read_roots == ()
    assert runner.run_script('scope-check').ok
    protected = case.workspace / 'locked.txt'
    protected.write_text('original')
    _save(case, _probe(writes=[('locked', str(protected)), ('business', str(case.workspace / 'allowed.txt'))]))
    options = case.agent.tools.tools['scripts'].runner_options_provider()
    assert 'write_protection' in options
    options['write_protection'] = SandboxWriteProtection(absolute_paths=(protected,))
    runner = script_runner.ScriptRunner(case.owner, use_sandbox=False, workspace_root=case.workspace, **options)
    result = runner.run_script('scope-check')
    assert result.ok, result.stderr_preview
    assert result.output_data == {'locked': False, 'business': True}
    assert protected.read_text() == 'original'
    assert set(persona_process_protected_paths(case.owner))


def test_guard_checks_actual_dir_fd_and_file_descriptor_mutation_targets(case):
    state = case.owner / 'data'
    original = state / 'fd-record.txt'
    original.write_text('original')
    for name in ('unlink-source.txt', 'rename-source.txt'):
        (state / name).write_text('original')
    outcome, data = _run(case, f'''import os,json
from pathlib import Path
state={str(state)!r}
directory=os.open(state, os.O_RDONLY | os.O_DIRECTORY)
record=os.open({str(original)!r}, os.O_RDONLY)
results={{}}
operations={{
    'mkdir-dirfd':lambda:os.mkdir('injected',dir_fd=directory),
    'unlink-dirfd':lambda:os.unlink('unlink-source.txt',dir_fd=directory),
    'rename-dirfd':lambda:os.rename('rename-source.txt','moved.txt',src_dir_fd=directory,dst_dir_fd=directory),
    'chmod-dirfd':lambda:os.chmod('fd-record.txt',0o700,dir_fd=directory),
    'fchmod':lambda:os.fchmod(record,0o700),
}}
for key, operation in operations.items():
    try:
        operation()
        results[key]=True
    except PermissionError:
        results[key]=False
os.close(record)
os.close(directory)
print(json.dumps(results))
''')
    assert outcome.ok, data
    assert data['output_data'] == dict.fromkeys(['mkdir-dirfd', 'unlink-dirfd', 'rename-dirfd', 'chmod-dirfd', 'fchmod'], False)
    assert original.read_text() == 'original' and not (state / 'injected').exists()


def test_hidden_sources_cannot_become_metadata_ancestors_of_positive_roots(case):
    hidden = case.workspace / 'host-source'
    leaf = hidden / 'nested/record.txt'
    leaf.parent.mkdir(parents=True)
    leaf.write_text('private fixture')
    case.agent.config.host_exclusive_path_roots = [str(hidden)]
    case.agent._current_run_params.write_boundary = {'allowed_read_roots': [str(leaf)]}
    outcome, data = _run(case, f'''import os,json
from pathlib import Path
hidden=Path({str(hidden)!r})
results={{}}
for name,operation in {{'stat':hidden.stat,'lstat':hidden.lstat,
                       'open-directory':lambda:os.open(hidden,os.O_RDONLY|os.O_DIRECTORY)}}.items():
    try:
        value=operation()
        if name=='open-directory':os.close(value)
        results[name]=True
    except PermissionError:
        results[name]=False
print(json.dumps(results))
''')
    assert outcome.ok, data
    assert data['output_data'] == {'stat': False, 'lstat': False, 'open-directory': False}


@pytest.mark.parametrize('active_run', [False, True])
def test_missing_read_scope_does_not_create_authority_from_workspace(case, monkeypatch, active_run):
    if not active_run:
        case.agent._current_run_params = None
    monkeypatch.setattr(tool_runtime_ledger, 'write_boundary_with_runtime_ledger', lambda *_: {})
    options = self_map_bootstrap._script_runtime_options(case.agent)
    assert options['work_context']['workspace_root'] == str(case.workspace)
    assert options['work_context']['effective_path_scope'] == {'read_roots': [], 'write_roots': []}
    assert options['public_read_roots'] == options['extra_write_roots'] == ()


@pytest.mark.parametrize('interpreter', ['bash', 'sh'])
def test_registered_non_python_script_fails_closed_without_native_scope_enforcement(case, interpreter):
    target = case.workspace / 'shell-locked.txt'
    target.write_text('original')
    case.agent._current_run_params.write_boundary = {'locked_files': [str(target)]}
    code = f'printf changed > {shlex.quote(str(target))}\nprintf \'{{"written":true}}\'\n'
    ScriptStore(case.owner, workspace_root=case.workspace).save_script(
        AssetDescriptor('shell-scope', 'script', 'shell-scope'), code, interpreter=interpreter)
    outcome = case.agent.tools.tools['scripts'].execute({'action': 'run', 'script_id': 'shell-scope'})
    assert not outcome.ok, outcome.output
    assert 'NESTED_SHELL_SANDBOX_UNAVAILABLE' in outcome.output
    assert target.read_text() == 'original'


@pytest.mark.parametrize('interpreter', ['bash', 'sh'])
def test_direct_non_python_runner_without_scope_keeps_legacy_defaults(case, interpreter):
    ScriptStore(case.owner).save_script(AssetDescriptor('legacy-shell', 'script', 'legacy-shell'),
                                        'printf \'{"ok":true}\'\n', interpreter=interpreter)
    runner = script_runner.ScriptRunner(case.owner, use_sandbox=False)
    result = runner.run_script('legacy-shell')
    assert result.ok and result.output_data == {'ok': True}, result.stderr_preview
    assert not runner.restrict_to_roots


def test_python_fallback_enforces_host_record_and_directory_patterns(case):
    host = host_sandbox_paths(case.agent.home_paths.root, case.owner, system='Darwin', config=case.agent.config)
    other = case.owner / 'tasks/2026-10-31/other-task'
    record = other / 'data/pack_verification/record.txt'
    record.parent.mkdir(parents=True)
    record.write_text('original')
    business = case.workspace / 'ordinary.txt'
    _save(case, f'''import os,json
from pathlib import Path
result={{}}
for name,operation in {{
    'record-write':lambda:Path({str(record)!r}).write_text('changed'),
    'rename-ancestor':lambda:os.rename({str(other)!r},{str(other.with_name('moved-task'))!r}),
    'create-host-root':lambda:Path({str(case.owner / 'runs/2026-10-31/new-run')!r}).mkdir(parents=True),
    'business-file':lambda:Path({str(business)!r}).write_text('business'),
    'layout-regular-file':lambda:Path({str(case.owner / 'runs/notes.txt')!r}).write_text('ordinary file, not a directory'),
    'create-business-dir':lambda:Path({str(case.workspace / 'new-dir')!r}).mkdir(),
}}.items():
    try:
        operation()
        result[name]=True
    except PermissionError:
        result[name]=False
print(json.dumps(result))
''')
    runner = script_runner.ScriptRunner(case.owner, use_sandbox=False, workspace_root=case.workspace,
        extra_write_roots=(case.owner,), host_path_protection=HostSandboxPaths(patterns=host.patterns,
            directory_write_patterns=host.directory_write_patterns, directory_unlink_patterns=host.directory_unlink_patterns))
    result = runner.run_script('scope-check')
    assert result.ok, result.stderr_preview
    assert result.output_data == {'record-write': False, 'rename-ancestor': False, 'create-host-root': False,
                                  'business-file': True, 'layout-regular-file': True, 'create-business-dir': True}
    assert record.read_text() == 'original' and business.read_text() == 'business'
