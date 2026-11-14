"""脚本工作区生产者、精确宿主屏蔽、目录发现与计时合同。"""
import json
from pathlib import Path

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.attempt import sandbox
from agent_py_agent.agent.workflows.script_runner import ScriptRunner


# LLM: 只向本次临时Owner写测试资产，不使用真实宿主配置。
# 函数用途: 经原脚本Store准备可运行样本。
def _save(home, code):
    ScriptStore(home).save_script(AssetDescriptor('sample', 'script', 'sample'), code)


# LLM: 真子进程核上下文生产者，不把该探针当作SDK或内核沙箱端到端验证。
# 函数用途: 证明十四个业务文件在工作区、进程cwd仍是独立临时目录。
def test_workspace_and_scratch_are_distinct_and_fourteen_business_files_are_readable(tmp_path):
    home, workspace = tmp_path / 'owner', tmp_path / 'workspace'
    workspace.mkdir()
    for index in range(14):
        (workspace / f'business-{index}.txt').write_text(str(index))
    _save(home, '''import os,json
from pathlib import Path
context=json.loads(os.environ['MY_AGENT_SCRIPT_WORK_CONTEXT_JSON'])
root=Path(context['workspace_root'])
print(json.dumps({'values':[int((root/f'business-{i}.txt').read_text()) for i in range(14)],
    'cwd':str(Path.cwd()),'scratch':context['scratch_dir'],
    'business_in_cwd':list(Path.cwd().glob('business-*')), 'context':context}))
''')
    runner = ScriptRunner(home, use_sandbox=False, workspace_root=workspace)
    result = runner.run_script('sample')
    assert result.ok, result.stderr_preview
    assert result.output_data['values'] == list(range(14))
    assert Path(result.output_data['cwd']).resolve() == Path(result.output_data['scratch']).resolve()
    assert Path(result.output_data['cwd']).resolve() != workspace.resolve()
    assert result.output_data['business_in_cwd'] == []
    assert result.output_data['context']['effective_path_scope']['read_roots'] == [str(workspace.resolve())]


# LLM: 只用无秘密测试值；准确来源名不得泄露，业务环境保持可用。
# 函数用途: 检查子进程环境按来源擦洗而非误删业务变量。
def test_exact_host_environment_names_are_scrubbed_without_hiding_business_values(tmp_path, monkeypatch):
    monkeypatch.setenv('REGION_CODE', 'host-only-fixture')
    monkeypatch.setenv('BUSINESS_REGION', 'west')
    _save(tmp_path, 'import os,json; print(json.dumps({"leak":"REGION_CODE" in os.environ,"business":os.environ["BUSINESS_REGION"]}))')
    result = ScriptRunner(tmp_path, use_sandbox=False, host_credential_env_names=frozenset({'REGION_CODE'})).run_script('sample')
    assert result.ok and result.output_data == {'leak': False, 'business': 'west'}


@pytest.mark.parametrize('restricted', [False, True])
# LLM: 运行真实Python守卫；普通与广范围视图都以精确专有路径优先拒绝。
# 函数用途: 核对宿主文件被挡住而同目录业务文件能正常读取。
def test_exclusive_source_cannot_be_read_even_under_broad_workspace(tmp_path, restricted):
    home, workspace = tmp_path / 'owner', tmp_path / 'workspace'
    workspace.mkdir()
    source = workspace / 'host-settings.data'
    source.write_text('private-fixture')
    (workspace / 'business.txt').write_text('allowed')
    _save(home, f'''import json
from pathlib import Path
try:
    Path({str(source)!r}).read_text()
    hidden=False
except PermissionError:
    hidden=True
print(json.dumps({{'hidden':hidden,'business':Path({str(workspace / 'business.txt')!r}).read_text()}}))
''')
    result = ScriptRunner(home, use_sandbox=False, workspace_root=workspace, restrict_to_roots=restricted,
        public_read_roots=(workspace,), host_exclusive_paths=(source,)).run_script('sample')
    assert result.ok, result.stderr_preview
    assert result.output_data == {'hidden': True, 'business': 'allowed'}


# LLM: 探针记录真实枚举调用，不用最终去重结果冒充未重复扫描。
# 函数用途: 核对重叠根共用发现缓存且仍能找到深层Git标记。
def test_overlapping_git_discovery_roots_enumerate_each_directory_once(tmp_path, monkeypatch):
    nested = tmp_path / 'project/sub/deep'
    nested.mkdir(parents=True)
    (nested / '.git').mkdir()
    calls = []
    original = sandbox._macos_git_scan_directory

    # LLM: 计数后仍调用原枚举器，不替换目录内容。
    # 函数用途: 收集本次真实枚举过的规范目录。
    def scan(directory, **kwargs):
        calls.append(Path(directory).resolve())
        return original(directory, **kwargs)

    monkeypatch.setattr(sandbox, '_macos_git_scan_directory', scan)
    spec = sandbox.AttemptSandboxSpec(tmp_path, tmp_path, tmp_path, tmp_path,
        extra_write_roots=(tmp_path / 'project', nested))
    markers = sandbox._macos_git_markers(spec)
    assert nested / '.git' in markers
    assert len(calls) == len(set(calls))


# LLM: 通过实际脚本运行取得五段计时；启动与执行必须有真实耗时。
# 函数用途: 防止仅声明计时字段而未记录执行阶段。
def test_five_preparation_timing_fields_describe_actual_script_execution(tmp_path):
    _save(tmp_path, 'print("{}")')
    result = ScriptRunner(tmp_path, use_sandbox=False).run_script('sample')
    assert result.ok
    timings = getattr(result, 'preparation_timings', {})
    assert set(timings) == {'configuration_load_ms', 'directory_discovery_ms', 'environment_setup_ms',
                           'process_start_ms', 'execution_ms'}
    assert all(type(value) in (int, float) and value >= 0 for value in timings.values())
    assert timings['process_start_ms'] > 0 and timings['execution_ms'] > 0


@pytest.mark.parametrize('exit_code,output', [
    (2, {'protocol_version': 'script-output.v1', 'result': {}, 'delivery_intents': []}),
    (0, {'protocol_version': 'wrong', 'result': {}, 'delivery_intents': []}),
    (0, {'protocol_version': 'script-output.v1', 'result': [], 'delivery_intents': []}),
])
# LLM: 坏信封或异常退出时只准零次宿主受理，不能先发送后校验。
# 函数用途: 覆盖退出码、协议版本及业务输出形状早拒。
def test_invalid_or_abnormal_versioned_output_never_admits_delivery(tmp_path, exit_code, output):
    calls = []
    _save(tmp_path, f'import sys; print({json.dumps(output)!r}); sys.exit({exit_code})')
    result = ScriptRunner(tmp_path, use_sandbox=False,
        delivery_handler=lambda **kwargs: calls.append(kwargs)).run_script('sample')
    assert not result.ok and calls == []


# LLM: 没有显式版本字段的同名业务键不得取得控制语义。
# 函数用途: 确保老脚本输出原样保留且不会触发投递。
def test_legacy_business_json_is_not_reinterpreted_as_delivery_control(tmp_path):
    calls = []
    value = {'delivery_intents': [{'request_key': 'business-field'}]}
    _save(tmp_path, f'print({json.dumps(value)!r})')
    result = ScriptRunner(tmp_path, use_sandbox=False,
        delivery_handler=lambda **kwargs: calls.append(kwargs)).run_script('sample')
    assert result.ok and result.output_data == value and calls == []


@pytest.mark.parametrize('owner_wall', [False, True])
# LLM: 仅检查原沙箱规格的接线，不声称执行了内核沙箱或外部Shell链路。
# 函数用途: 核对普通Owner与Full Access都把专有来源投影到hidden集合。
def test_shell_exclusive_paths_are_hidden_in_both_owner_and_full_views(tmp_path, monkeypatch, owner_wall):
    from agent_py_agent.agent.tooling import shell

    hidden = tmp_path / 'host-configuration'
    captured = []

    class ProbeSandbox:
        # LLM: 只采集规格，不创建进程或替代生产权限裁决。
        # 函数用途: 保存Shell交来的沙箱构造参数。
        def __init__(self, spec):
            captured.append(spec)

        # LLM: 探针仅让待检查入口正常返回，不提供隔离保证。
        # 函数用途: 原样返回命令供规格测试使用。
        def build_argv(self, command):
            return command

    monkeypatch.setattr(sandbox, 'AttemptExecutionSandbox', ProbeSandbox)
    policy_type = sandbox.SandboxHostPolicy
    assert 'host_exclusive_paths' in policy_type.__dataclass_fields__
    policy = policy_type(host_exclusive_paths=(hidden,))
    shell._sandbox_exec('pwd', tmp_path, str(tmp_path) if owner_wall else '', host_policy=policy)
    assert hidden in captured[0].hidden_paths


# LLM: 冲突在写资产前拒绝，缺省kind仍走旧脚本合同。
# 函数用途: 检查默认类型和结构化冲突回执的兼容性。
def test_script_kind_defaults_but_explicit_conflict_is_structured(tmp_path):
    from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool

    descriptor = {'name': 'sample', 'access_targets': [], 'data_sources': [], 'credential_refs': [], 'io_contract': {}}
    tool = ScriptsTool(tmp_path)
    conflict = tool.execute({'action': 'register', 'script_id': 'sample', 'descriptor': {**descriptor, 'kind': 'workflow'}, 'code': 'pass'})
    assert not conflict.ok and conflict.effect_outcome == 'not_started'
    data = json.loads(conflict.output)
    assert conflict.error_code == 'TOOL_INVALID_ARGUMENTS'
    assert data['issues'] == [{'keyword': 'const', 'path': '$.descriptor.kind',
                              'expected': 'script', 'actual_type': 'string'}]
    assert data['effect_outcome'] == 'not_started' and data['next_action']
    assert data['issues_truncated'] is False
    from agent_py_agent.agent.assets.script_store import ScriptStore

    assert ScriptStore(tmp_path).get_script('sample') is None
    saved = tool.execute({'action': 'register', 'script_id': 'sample', 'descriptor': descriptor, 'code': 'print("{}")'})
    assert saved.ok and json.loads(saved.output)['kind'] == 'script'


# LLM: 使用原产物注册器与内容校验，受理端口仅为显式注入的测试探针。
# 函数用途: 准备绑定原run且声明合法输出引用的信封脚本。
def _delivery_runner(tmp_path, *, intents, handler):
    from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact
    from agent_py_agent.agent.assets.descriptor import IOContract

    home, workspace = tmp_path / 'owner', tmp_path / 'owner/tasks/workspace'
    workspace.mkdir(parents=True)
    report = workspace / 'report.txt'
    report.write_text('verified artifact')
    register_artifact(ArtifactRegistration(workspace, report, artifact_id='report-1', run_id='run-1', status='ready'))
    envelope = {'protocol_version': 'script-output.v1', 'result': {'report': 'report-1'}, 'delivery_intents': intents}
    ScriptStore(home).save_script(AssetDescriptor('sample', 'script', 'sample', io_contract=IOContract(outputs=('report',))),
        f'print({json.dumps(envelope)!r})')
    return ScriptRunner(home, use_sandbox=False, workspace_root=workspace, work_context={'run_id': 'run-1'}, delivery_handler=handler)


# LLM: 验证转交合同和本次声明去重，不把探针回执当作真实持久投递记录。
# 函数用途: 检查登记产物经核验后只被转交一次且保留原运行身份。
def test_versioned_delivery_validates_registered_content_and_deduplicates_same_key(tmp_path):
    calls = []
    intent = {'request_key': 'report', 'message': 'report', 'output_refs': ['report-1'], 'status': 'requested'}

    # LLM: 仅模拟已经提交的端口答复，测试本身不调用DeliveryService。
    # 函数用途: 记录一次合法受理转交。
    def admit(**kwargs):
        calls.append(kwargs)
        return {'operation_ref': 'original-operation-1', 'status': 'accepted'}

    runner = _delivery_runner(tmp_path, intents=[intent, intent], handler=admit)
    result = runner.run_script('sample')
    assert result.ok, result.stderr_preview
    assert result.output_data == {'report': 'report-1'}
    assert len(calls) == 1 and calls[0]['run_id'] == 'run-1'
    assert calls[0]['attachments'][0].artifact_id == 'report-1'
    assert result.delivery_receipts == ({'operation_ref': 'original-operation-1', 'status': 'accepted'},)


# LLM: 后续组件失败不能丢掉先前受理回执，也不能把整批误标not_started。
# 函数用途: 检查部分受理后的失败保留原操作引用与unknown结果。
def test_later_delivery_admission_failure_preserves_confirmed_original_operation(tmp_path):
    calls = []

    # LLM: 第二次调用定点故障，第一份原操作回执必须留存。
    # 函数用途: 模拟部分受理后端口不可用。
    def admit(**kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise OSError('second admission unavailable')
        return {'operation_ref': 'original-operation-1', 'status': 'accepted'}

    intents = [{'request_key': key, 'message': key, 'output_refs': ['report-1']} for key in ('first', 'second')]
    result = _delivery_runner(tmp_path, intents=intents, handler=admit).run_script('sample')
    assert not result.ok and len(calls) == 2
    assert result.delivery_receipts == ({'operation_ref': 'original-operation-1', 'status': 'accepted'},)
    assert result.effect_outcome == 'unknown'
