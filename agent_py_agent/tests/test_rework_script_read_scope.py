"""R4-P1-01：外层Seatbelt跳过内层沙箱时，普通owner的注册脚本与工作流步骤仍受冻结owner读取边界约束。

Agent、self-map注册、运行账本、ScriptRunner与Python守卫均为生产实现；只把外层Seatbelt探针固定为已生效，
以选择受支持的非strict跳过分支。owner home都经原owner_resolver在临时数据根内规范创建。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import self_map_bootstrap, tool_runtime_ledger
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home
from agent_py_agent.agent.workflows import script_runner

# 子进程探针：每项都是真实文件系统调用，只把PermissionError记为拒绝，其它异常让脚本失败。
_PROBE = '''import json, os
from pathlib import Path


def at(base, operation):
    descriptor = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return operation(descriptor)
    finally:
        os.close(descriptor)


operations = {
    'read': lambda path: Path(path).read_text(),
    'write': lambda path: Path(path).write_text('business output'),
    'stat': os.stat,
    'lstat': os.lstat,
    'list': os.listdir,
    'open-dir': lambda path: os.close(os.open(path, os.O_RDONLY | os.O_DIRECTORY)),
    'resolve': lambda path: Path(path).resolve(strict=True),
    'read-at': lambda base, name: at(base, lambda fd: os.close(os.open(name, os.O_RDONLY, dir_fd=fd))),
    'stat-at': lambda base, name: at(base, lambda fd: os.stat(name, dir_fd=fd)),
    'list-fd': lambda base: at(base, os.listdir),
}
result = {}
for name, (kind, *args) in PLAN.items():
    try:
        operations[kind](*args)
        result[name] = True
    except PermissionError:
        result[name] = False
if REPORT:
    Path(REPORT).write_text(json.dumps(result))
print(json.dumps(result))
'''


# LLM: 只在隔离临时数据根创建Agent；owner身份来自结构化配置，不读操作者home。
# 函数用途: 构造经原self-map注册scripts/workflows的真实Agent。
def _agent(tmp_path, **config):
    (tmp_path / 'project').mkdir(parents=True, exist_ok=True)
    return SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / 'home'), memory_path='memory.jsonl',
                                   orphan_supervision_interval_seconds=0, **config), tmp_path / 'project')


# LLM: 只写测试自建的合成内容，不触碰宿主真实文件。
# 函数用途: 创建父目录并写入一个夹具文件。
def _file(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# LLM: 计划只含操作种类与路径参数；报告文件供工作流步骤回传同一份结果。
# 函数用途: 生成执行真实文件操作的脚本代码。
def _probe(plan, report=''):
    frozen = {name: (kind, *map(str, args)) for name, (kind, *args) in plan.items()}
    return f'PLAN = {frozen!r}\nREPORT = {str(report)!r}\n' + _PROBE


# LLM: 经原ScriptStore保存，脚本归属由owner home决定，不由脚本内容声明。
# 函数用途: 保存或覆盖探针脚本。
def _save(owner, workspace, code):
    ScriptStore(owner, workspace_root=workspace).save_script(AssetDescriptor('read-scope', 'script', 'read-scope'), code)


# LLM: 每次都是一次新的注册scripts工具调用，runner由注册时的provider当场冻结。
# 函数用途: 运行探针并返回脚本输出。
def _run(case, plan):
    _save(case.owner, case.workspace, _probe(plan))
    outcome = case.agent.tools.tools['scripts'].execute({'action': 'run', 'script_id': 'read-scope'})
    data = json.loads(outcome.output)
    assert outcome.ok, data
    return data['output_data']


# LLM: 普通feishu用户与另一个规范owner共用临时数据根；只固定外层Seatbelt探针，spec与守卫裁决保持真实。
# 函数用途: 准备普通owner、外部owner业务文件、shared引用与数据根控制面夹具。
@pytest.fixture
def case(tmp_path, monkeypatch):
    # 指定venv有editable旧检出；不能把旧检出的绿灯算到当前工作树。
    expected = Path(__file__).resolve().parents[1]
    assert Path(script_runner.__file__).resolve().is_relative_to(expected)
    assert Path(self_map_bootstrap.__file__).resolve().is_relative_to(expected)
    agent = _agent(tmp_path, my_agent_owner_provider='feishu', my_agent_owner_kind='user',
                   my_agent_owner_id='ou_owner')
    home = Path(agent.home_paths.root).resolve()
    owner = Path(agent.home_paths.owner_home_dir).resolve()
    workspace = owner / 'tasks/2026-11-01/business'
    workspace.mkdir(parents=True)
    agent.effective_workspace_root = workspace
    agent._current_run_params = RunParams(run_id='read-run', task_id='read-task', attempt_id='read-attempt',
                                          task_attributes={})
    foreign = ensure_owner_home(home, OwnerIdentity.provider_user('feishu', 'ou_other')).home_dir.resolve()
    launches = []
    original = script_runner._can_apply_sandbox

    def record(sandbox, *, strict=False):
        launches.append((sandbox.spec, original(sandbox, strict=strict)))
        return launches[-1][1]

    monkeypatch.setattr(script_runner, '_outer_seatbelt_active', lambda: True)
    monkeypatch.setattr(script_runner, '_can_apply_sandbox', record)
    return SimpleNamespace(agent=agent, home=home, owner=owner, workspace=workspace, foreign=foreign,
                           secret=_file(foreign / 'workspace/report.txt', 'foreign business'),
                           shared=_file(home / 'shared/skills/common/SKILL.md', 'shared reference'),
                           control=_file(home / 'system/control-fixture.json', '{}'), launches=launches)


def test_registered_script_reads_own_and_shared_but_not_another_owner(case):
    source = _file(case.workspace / 'input.csv', 'own input')
    output = case.workspace / 'out/report.txt'
    output.parent.mkdir()
    policy = PathAccessPolicy.from_config(case.agent.config, agent_home_root=case.home, owner_scope_root=case.owner)
    assert policy.check(case.secret).code == 'PATH_CROSS_OWNER_BLOCKED'
    assert policy.check(case.control).code == 'PATH_OWNER_SCOPE_BLOCKED'
    assert policy.check(source).allowed and policy.check(case.shared).allowed
    result = _run(case, {'own-input': ('read', source), 'own-output': ('write', output),
                         'shared': ('read', case.shared), 'foreign-read': ('read', case.secret),
                         'foreign-stat': ('stat', case.secret), 'foreign-home': ('stat', case.foreign),
                         'foreign-list': ('list', case.foreign / 'workspace'), 'control': ('read', case.control)})
    assert result == {'own-input': True, 'own-output': True, 'shared': True, 'foreign-read': False,
                      'foreign-stat': False, 'foreign-home': False, 'foreign-list': False, 'control': False}
    assert output.read_text() == 'business output'
    spec, applied = case.launches[-1]
    # 只走外层Seatbelt跳过分支；原生spec的共享根与私有读根保持原非restrict形态。
    assert applied is False
    assert spec.shared_workspace == spec.owner_home and spec.private_read_roots == ()


def test_registered_workflow_step_applies_the_same_owner_boundary(case):
    source = _file(case.workspace / 'input.csv', 'own input')
    report = case.workspace / 'workflow-report.json'
    _save(case.owner, case.workspace, _probe({'own': ('read', source), 'shared': ('read', case.shared),
                                              'foreign': ('read', case.secret)}, report))
    tool = case.agent.tools.tools['workflows']
    definition = {'descriptor': AssetDescriptor('read-flow', 'workflow', 'read-flow').to_dict(),
                  'steps': [{'step_id': 'probe', 'name': 'probe', 'mode': 'deterministic_script',
                             'script_id': 'read-scope', 'on_error': 'fail'}]}
    assert tool.execute({'action': 'save', 'workflow_id': 'read-flow', 'definition': definition}).ok
    outcome = tool.execute({'action': 'run', 'workflow_id': 'read-flow'})
    assert outcome.ok, outcome.output
    assert json.loads(report.read_text()) == {'own': True, 'shared': True, 'foreign': False}
    assert [applied for _, applied in case.launches] == [False]


def test_positive_grants_and_symlink_aliases_do_not_reopen_another_owner(case):
    granted = _file(case.workspace / 'granted/input.txt', 'granted input')
    nested = case.foreign / 'workspace/project'
    nested.mkdir()
    alias = case.workspace / 'foreign-alias'
    alias.symlink_to(case.foreign, target_is_directory=True)
    link = case.workspace / 'foreign-link.txt'
    link.symlink_to(case.secret)
    own_link = case.workspace / 'own-link.txt'
    own_link.symlink_to(granted)
    case.agent._current_run_params.write_boundary = {
        'allowed_read_roots': [str(granted.parent), str(alias), str(nested)]}
    options = case.agent.tools.tools['scripts'].runner_options_provider()
    # 可信边界把别名解析成整个外部owner home，正向授权确实到达执行器。
    assert {granted.parent, case.foreign, nested} <= set(options['public_read_roots'])
    result = _run(case, {
        'granted': ('read', granted), 'own-link': ('read', own_link), 'own-link-lstat': ('lstat', own_link),
        'alias-read': ('read', alias / 'workspace/report.txt'), 'alias-stat': ('stat', alias),
        'alias-lstat': ('lstat', alias), 'alias-open-dir': ('open-dir', alias),
        'link-read': ('read', link), 'link-stat': ('stat', link), 'link-lstat': ('lstat', link),
        'grant-list': ('list', nested), 'grant-parent-stat': ('stat', nested.parent),
        'grant-parent-lstat': ('lstat', nested.parent), 'grant-parent-open-dir': ('open-dir', nested.parent)})
    assert result == {'granted': True, 'own-link': True, 'own-link-lstat': True,
                      'alias-read': False, 'alias-stat': False, 'alias-lstat': True, 'alias-open-dir': False,
                      'link-read': False, 'link-stat': False, 'link-lstat': True,
                      'grant-list': False, 'grant-parent-stat': False, 'grant-parent-lstat': False,
                      'grant-parent-open-dir': False}


def test_host_exclusive_source_stays_denied_inside_own_scope(case):
    source = _file(case.workspace / 'host-source.data', 'host only')
    business = _file(case.workspace / 'business.txt', 'business')
    case.agent.config.host_exclusive_path_roots = [str(source)]
    case.agent._current_run_params.write_boundary = {'allowed_read_roots': [str(source), str(case.workspace)]}
    result = _run(case, {'source-read': ('read', source), 'source-stat': ('stat', source),
                         'business': ('read', business)})
    assert result == {'source-read': False, 'source-stat': False, 'business': True}
    assert source.read_text() == 'host only'


def test_owner_ancestors_keep_metadata_without_directory_contents(case):
    own = _file(case.workspace / 'nested/own.txt', 'own')
    ancestors = [path for path in case.owner.parents if path == case.home or case.home in path.parents]
    assert ancestors[-2:] == [case.home / 'owners', case.home]
    owners = case.home / 'owners'
    plan = {'resolve-own': ('resolve', own), 'list-own': ('list', own.parent),
            'open-owners': ('open-dir', owners), 'read-at-own': ('read-at', owners, own.relative_to(owners))}
    expected = dict.fromkeys(plan, True)
    # 祖先目录句柄只服务nofollow链；经dir_fd枚举或打开外部owner仍按真实目标拒绝。
    plan.update({'read-at-foreign': ('read-at', owners, case.secret.relative_to(owners)),
                 'stat-at-foreign': ('stat-at', owners, case.foreign.relative_to(owners)),
                 'list-fd-owners': ('list-fd', owners)})
    expected.update(dict.fromkeys(['read-at-foreign', 'stat-at-foreign', 'list-fd-owners'], False))
    for index, path in enumerate(ancestors):
        plan.update({f'stat-{index}': ('stat', path), f'lstat-{index}': ('lstat', path),
                     f'list-{index}': ('list', path)})
        expected.update({f'stat-{index}': True, f'lstat-{index}': True, f'list-{index}': False})
    assert _run(case, plan) == expected


def test_missing_run_params_keeps_ordinary_owner_scope(case):
    own = _file(case.workspace / 'own.txt', 'own')
    case.agent._current_run_params = None
    options = self_map_bootstrap._script_runtime_options(case.agent)
    assert Path(options['owner_scope_root']) == case.owner
    assert Path(options['agent_home_root']).resolve() == case.home
    result = _run(case, {'own': ('read', own), 'shared': ('read', case.shared), 'foreign': ('read', case.secret)})
    assert result == {'own': True, 'shared': True, 'foreign': False}


def test_provider_takes_owner_scope_from_the_single_boundary_snapshot(case, monkeypatch):
    real = self_map_bootstrap._script_runtime_options(case.agent)
    assert Path(real['owner_scope_root']) == case.owner
    assert Path(real['agent_home_root']).resolve() == case.home
    calls = []

    def boundary(_agent, params):
        calls.append(params)
        return {'execution_cwd': str(case.workspace), 'effective_owner_scope_root': str(case.workspace)}

    monkeypatch.setattr(tool_runtime_ledger, 'write_boundary_with_runtime_ledger', boundary)
    options = self_map_bootstrap._script_runtime_options(case.agent)
    assert calls == [case.agent._current_run_params]
    assert Path(options['owner_scope_root']) == case.workspace


def test_runner_with_only_the_owner_boundary_enforces_it(case):
    own = _file(case.workspace / 'direct.txt', 'own')
    _save(case.owner, case.workspace, _probe({'own': ('read', own), 'shared': ('read', case.shared),
                                              'foreign': ('read', case.secret), 'control': ('read', case.control)}))
    runner = script_runner.ScriptRunner(case.owner, use_sandbox=False, workspace_root=case.workspace,
                                        owner_scope_root=case.owner, agent_home_root=case.home)
    assert not runner.restrict_to_roots and runner.public_read_roots == runner.extra_write_roots == ()
    assert (runner.owner_scope_root, runner.agent_home_root) == (case.owner, case.home)
    result = runner.run_script('read-scope')
    assert result.ok, result.stderr_preview
    assert result.output_data == {'own': True, 'shared': True, 'foreign': False, 'control': False}


@pytest.mark.parametrize('kind', ['missing', 'blank', 'relative', 'unrelated', 'owners-container'])
def test_owner_boundary_requires_a_valid_canonical_app_home(case, kind):
    scope = case.home / 'owners' if kind == 'owners-container' else case.owner
    home = {'missing': None, 'blank': '', 'relative': 'home', 'unrelated': case.home.parent / 'other-home',
            'owners-container': case.home}[kind]
    with pytest.raises(ValueError):
        script_runner.ScriptRunner(case.owner, owner_scope_root=scope, agent_home_root=home)


def test_no_owner_wall_keeps_direct_runner_and_full_access_admin_behavior(case, tmp_path):
    _save(case.owner, case.workspace, _probe({'foreign': ('read', case.secret)}))
    for options in ({}, {'owner_scope_root': None, 'agent_home_root': case.home}, {'owner_scope_root': ''}):
        runner = script_runner.ScriptRunner(case.owner, use_sandbox=False, **options)
        assert runner.owner_scope_root is None
        result = runner.run_script('read-scope')
        assert result.ok and result.output_data == {'foreign': True}, result.stderr_preview
    admin = _agent(tmp_path / 'admin', access_mode='full-access')
    options = self_map_bootstrap._script_runtime_options(admin)
    assert options['owner_scope_root'] is None
    other = ensure_owner_home(admin.home_paths.root, OwnerIdentity.provider_user('feishu', 'ou_other')).home_dir
    secret = _file(Path(other) / 'workspace/report.txt', 'foreign business')
    _save(Path(admin.home_paths.owner_home_dir), admin.effective_workspace_root, _probe({'foreign': ('read', secret)}))
    outcome = admin.tools.tools['scripts'].execute({'action': 'run', 'script_id': 'read-scope'})
    assert outcome.ok, outcome.output
    assert json.loads(outcome.output)['output_data'] == {'foreign': True}


def test_case_and_firmlink_spellings_map_back_to_the_canonical_owner_boundary(case):
    own = _file(case.workspace / 'own.txt', 'own')
    spellings = {'case': case.home.with_name(case.home.name.upper()),
                 'firmlink': Path('/System/Volumes/Data', *case.home.parts[1:])}
    plan, expected = {}, {}
    for name, alias_home in spellings.items():
        foreign = alias_home / case.secret.relative_to(case.home)
        # 只使用宿主确认指向同一文件的拼写；大小写敏感或没有firmlink的平台自然跳过该项。
        if not (foreign.exists() and os.path.samefile(foreign, case.secret)):
            continue
        plan.update({f'{name}-read': ('read', foreign), f'{name}-stat': ('stat', foreign),
                     f'{name}-list': ('list', foreign.parent)})
        expected.update(dict.fromkeys([f'{name}-read', f'{name}-stat', f'{name}-list'], False))
        if name == 'firmlink':
            plan['firmlink-own'] = ('read', alias_home / own.relative_to(case.home))
            expected['firmlink-own'] = True
    if not plan:
        pytest.skip('当前平台没有数据根的别名拼写')
    assert _run(case, plan) == expected


def test_interpreter_runtime_inside_app_home_stays_readable_without_other_owner_data(case, monkeypatch):
    runtime = _file(case.home / 'runtime/python/lib/site.py', 'runtime')
    foreign_runtime = _file(case.foreign / 'venv/lib/site.py', 'foreign runtime')
    executable, roots = script_runner._interpreter_runtime('python3')
    # 宿主计算的解释器根可含数据根内安装；外部owner内的根与覆盖整个数据根的宽根都不能借此放行数据。
    extra = (runtime.parents[1], foreign_runtime.parents[1], case.home.parent)
    monkeypatch.setattr(script_runner, '_interpreter_runtime', lambda interpreter: (executable, (*roots, *extra)))
    result = _run(case, {'runtime': ('read', runtime), 'runtime-parent': ('stat', case.home / 'runtime'),
                         'runtime-parent-list': ('list', case.home / 'runtime'),
                         'foreign-runtime': ('read', foreign_runtime), 'control': ('read', case.control),
                         'foreign': ('read', case.secret)})
    assert result == {'runtime': True, 'runtime-parent': True, 'runtime-parent-list': False,
                      'foreign-runtime': False, 'control': False, 'foreign': False}
