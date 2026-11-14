"""工作流文件产物：真实执行、原账本投影、失败原子性与有界资源合同。"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
from dataclasses import fields
from pathlib import Path

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import WorkflowStore
from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaEnforcer
from agent_py_agent.agent.workflows import models
from agent_py_agent.agent.workflows import script_runner as runner_module
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.script_runner import ScriptRunner


# LLM: 源码只保存在pytest临时Owner，原资产Store负责合法描述符与IO合同。
# 函数用途: 保存一个输出value的真实Python脚本。
def save_script(home, body):
    return ScriptStore(home).save_script(AssetDescriptor.from_dict({
        'id': 'produce', 'name': 'produce', 'kind': 'script', 'access_targets': [],
        'data_sources': [], 'credential_refs': [], 'io_contract': {'outputs': ['value']},
    }), body, interpreter='python3', entrypoint='main.py')


# LLM: 新参数未实现时仍运行旧代码，红证据必须停在行为断言而非导入/构造失败。
# 函数用途: 创建有可选归档配额的真实执行器。
def runner(home, workspace, *, quota=True, restricted=False):
    options = {'use_sandbox': False, 'restrict_to_roots': restricted, 'workspace_root': workspace}
    if 'run_out_quota' in inspect.signature(ScriptRunner).parameters and quota:
        options['run_out_quota'] = (OwnerQuotaEnforcer(home, max_bytes=0) if quota is True else quota)
    return ScriptRunner(home, **options)


# LLM: 不改变步骤模式或脚本错误处理合同。
# 函数用途: 构造最小正式脚本工作流。
def definition():
    return {'descriptor': {'id': 'out-flow', 'name': 'out-flow', 'kind': 'workflow', 'io_contract': {}},
            'steps': [{'step_id': 'produce', 'name': 'produce', 'mode': 'deterministic_script',
                       'script_id': 'produce', 'on_error': 'fail'}]}


# LLM: 每个Owner和workspace均在pytest临时目录，不读取生产home。
# 函数用途: 准备真实脚本引擎、存储和工作区。
def setup(tmp_path, body, *, quota=True, restricted=False):
    home, workspace = tmp_path / 'global' / 'owners' / 'fixture', tmp_path / 'business'
    workspace.mkdir(parents=True, exist_ok=True)
    save_script(home, body)
    engine = WorkflowEngine(home, workspace_root=workspace)
    engine.runner = runner(home, workspace, quota=quota, restricted=restricted)
    return home, workspace, engine


# LLM: 缺少新环境变量时仍写旧scratch；从而观察文件被清理的原始缺陷。
# 函数用途: 生成可复现的真实脚本内容。
def code(body="(out / 'result.txt').write_text('中文产物', encoding='utf-8')", ending=None):
    return ("import json, os\nfrom pathlib import Path\n"
            "out = Path(os.environ.get('MY_AGENT_RUN_OUT', str(Path.home() / 'legacy')))\n"
            "out.mkdir(parents=True, exist_ok=True)\n" + body + '\n' +
            (ending if ending is not None else
             "print(json.dumps({'value':7, 'injected': 'MY_AGENT_RUN_OUT' in os.environ, "
             "'scratch': str(Path.home()), 'context': json.loads(os.environ.get('MY_AGENT_SCRIPT_WORK_CONTEXT_JSON', '{}'))}))\n"))


# LLM: 检查全部文件集合而非只测目录存在，不把失效scratch当产物。
# 函数用途: 对照真实字节、相对路径、大小和SHA256。
def assert_artifacts(row, home, run_id):
    assert getattr(row, 'output_dir', ''), '工作流文件产物没有归档目录'
    output = Path(row.output_dir)
    assert output.parent.parent == home / 'data' / 'workflow_runs' / run_id / 'artifacts'
    assert output.parent.name == 'produce'
    assert len(row.output_artifacts) == 1
    item = row.output_artifacts[0]
    raw = (output / item['path']).read_bytes()
    name_digest = hashlib.sha256(b'result.txt').hexdigest()[:24]
    assert item == {'path': 'result.txt', 'size_bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                    'artifact_id': f'wfart-{run_id}-{output.name}-{name_digest}'}
    assert raw == '中文产物'.encode()
    assert not list(output.parent.glob('.staging-*'))


# LLM: WorkflowsTool、Engine、ScriptRunner、RunStore均真实；观察包装器只记录真实返回值。
# 函数用途: 验证工具运行到持久步骤、重启后工具投影的完整链路。
def test_real_tool_run_harvests_and_restart_lists_artifacts(tmp_path):
    home, workspace, engine = setup(tmp_path, code())
    WorkflowStore(home).save_workflow(definition())
    results, original = [], engine.runner.run_script

    # LLM: 不替换正式runner结果，只额外保存构造及透传后的对象供DTO核对。
    # 函数用途: 捕获真实脚本执行结果。
    def observe(*args, **kwargs):
        result = original(*args, **kwargs)
        results.append(result)
        return result

    engine.runner.run_script = observe
    tool = WorkflowsTool(home, engine=engine, workspace_root=workspace)
    outcome = tool.execute({'action': 'run', 'workflow_id': 'out-flow'})
    assert outcome.ok, outcome.output
    value = json.loads(outcome.output)
    run = engine.store.load_run(value['workflow_run_id'])
    assert run.status == 'succeeded' and results[0].ok
    assert_artifacts(run.steps[0], home, run.workflow_run_id)
    assert_artifacts(results[0], home, run.workflow_run_id)
    assert results[0].output_artifacts == run.steps[0].output_artifacts
    context = run.steps[0].output_snapshot['context']
    assert context['run_out_dir'] == str(Path(context['scratch_dir']) / 'run_out')
    assert not Path(context['scratch_dir']).exists()
    restarted = WorkflowsTool(home, engine=WorkflowEngine(home, workspace_root=workspace))
    inspected = restarted.execute({'action': 'inspect_run', 'workflow_run_id': run.workflow_run_id})
    assert inspected.ok
    assert json.loads(inspected.output)['steps'][0]['output_artifacts'] == list(run.steps[0].output_artifacts)


# LLM: 不把数据源或脚本错误换成归档成功；失败不能发布部分目录。
# 函数用途: 验证链接、特殊文件、上限、配额和脚本失败均有明确结果。
@pytest.mark.parametrize('body,ending,expected', [
    ("(out / 'first').write_text('ok'); (out / 'link').symlink_to(out / 'first')", None, 'SCRIPT_RUN_OUT_LINK_REJECTED'),
    ("os.mkfifo(out / 'pipe')", None, 'SCRIPT_RUN_OUT_LINK_REJECTED'),
    ("[(out / str(i)).write_bytes(b'x') for i in range(65)]", None, 'SCRIPT_RUN_OUT_LIMIT_EXCEEDED'),
    ("with (out / 'huge').open('wb') as f: f.truncate(64 * 1024 * 1024 + 1)", None, 'SCRIPT_RUN_OUT_LIMIT_EXCEEDED'),
    ("(out / 'result.txt').write_text('ok')", "raise SystemExit(4)", 'SCRIPT_EXIT_NONZERO'),
    ("(out / 'result.txt').write_text('ok')", "print('{invalid')", 'SCRIPT_INVALID_JSON'),
])
def test_invalid_or_failed_execution_publishes_nothing(tmp_path, body, ending, expected):
    home, _, engine = setup(tmp_path, code(body, ending))
    run = engine.run_workflow(definition())
    assert run.status == 'failed', run.to_dict()
    step = run.steps[0]
    assert step.error_code == expected, step.error_message
    assert not getattr(step, 'output_artifacts', ()) and not getattr(step, 'output_dir', '')
    assert not list((home / 'data' / 'workflow_runs' / run.workflow_run_id).glob('artifacts/produce/*'))


# LLM: 有限配额为真实OwnerQuotaEnforcer，不以假check代替计费。
# 函数用途: 验证磁盘配额拒绝后没有暂存或正式产物。
def test_quota_exhaustion_is_explicit_and_atomic(tmp_path):
    home, workspace, engine = setup(tmp_path, code())
    engine.runner = runner(home, workspace, quota=OwnerQuotaEnforcer(home, max_bytes=1))
    run = engine.run_workflow(definition())
    assert run.status == 'failed'
    assert run.steps[0].error_code == 'SCRIPT_RUN_OUT_QUOTA_EXCEEDED'
    assert not list((home / 'data' / 'workflow_runs' / run.workflow_run_id).glob('artifacts/produce/*'))


# LLM: 没有目标或没有配额都不注入；不把外部环境串到另一次脚本。
# 函数用途: 固定独立scripts.run、普通无quota工作流的旧行为及环境隔离。
@pytest.mark.parametrize('standalone', [False, True])
def test_no_target_or_no_quota_keeps_legacy_context(tmp_path, standalone):
    home, workspace, engine = setup(tmp_path, code(), quota=standalone)
    if standalone:
        result = engine.runner.run_script('produce', record_summary=False)
        assert result.ok
        output, serialized = result.output_data, models.payload(result)
    else:
        run = engine.run_workflow(definition())
        assert run.status == 'succeeded'
        output, serialized = run.steps[0].output_snapshot, run.steps[0].to_dict()
    assert output['injected'] is False and 'run_out_dir' not in output['context']
    assert 'output_dir' not in serialized and 'output_artifacts' not in serialized
    assert set(output['context']) == {'schema_version', 'workspace_root', 'scratch_dir', 'run_id', 'task_id', 'effective_path_scope'}


# LLM: 两个DTO扩展必须keyword-only，空字段不污染旧字典，未知键仍拒绝。
# 函数用途: 固定产物字段兼容及往返合同。
def test_dto_artifact_fields_and_roundtrip():
    for cls in (models.ScriptExecutionResult, models.StepExecutionRecord):
        selected = {item.name: item for item in fields(cls)}
        assert {'output_dir', 'output_artifacts'} <= selected.keys()
        assert selected['output_dir'].kw_only and selected['output_artifacts'].kw_only
    step = models.StepExecutionRecord('s', 's', 'deterministic_script', 'succeeded', 0, 0, 0, False)
    original = step.to_dict()
    assert 'output_dir' not in original and 'output_artifacts' not in original
    step.output_dir, step.output_artifacts = '/archive', ({'path': 'a', 'size_bytes': 1, 'sha256': 'x'},)
    assert models.StepExecutionRecord.from_dict(step.to_dict()) == step
    result = models.ScriptExecutionResult('s', True, 0, 0, 0, 0, {}, '', '')
    assert 'output_artifacts' not in result.to_dict()
    result.output_dir, result.output_artifacts = step.output_dir, step.output_artifacts
    assert models.ScriptExecutionResult.from_dict(result.to_dict()) == result
    with pytest.raises(TypeError):
        models.StepExecutionRecord.from_dict({**original, 'unknown': True})


# LLM: 宿主若拒绝os.link，显式模拟nlink元数据，不冒称真实硬链接端到端通过；正文读取仍必须为零。
# 函数用途: 在真实可创建硬链接的平台或受控nlink夹具上验证同一归档拒绝门。
def test_hardlink_rejected_before_any_content_read(tmp_path, monkeypatch, record_property):
    from types import SimpleNamespace

    from agent_py_agent.agent.workflows import run_artifacts as artifacts

    scratch = tmp_path / 'scratch'
    out = scratch / 'run_out'
    out.mkdir(parents=True)
    source = out / 'first'
    source.write_bytes(b'x')
    try:
        os.link(source, out / 'hard')
        record_property('hardlink_fixture', 'native')
    except PermissionError:
        record_property('hardlink_fixture', 'simulated_nlink_after_native_permission_denial')
        original = artifacts.os.scandir

        # LLM: 仅在原生硬链接权限拒绝后模拟nlink元数据，不把降级fixture称为原生E2E。
        # 类用途: 为硬链接拒绝门保留真实目录枚举和受控链接数。
        class Children:
            # LLM: 沿原描述符创建枚举器，不额外读取文件正文。
            # 函数用途: 持有可显式关闭的目录枚举器。
            def __init__(self, descriptor):
                self.children = original(descriptor)

            # LLM: 只改链接数观察值，其余目录入口沿真实枚举器返回。
            # 函数用途: 提供会被拒绝门识别的受控元数据。
            def __enter__(self):
                return (SimpleNamespace(name=entry.name, stat=lambda **kw: SimpleNamespace(
                    st_mode=source.stat().st_mode, st_nlink=2, st_size=1)) for entry in self.children)

            # LLM: 异常与正常退出都关闭真实目录描述符。
            # 函数用途: 不让降级测试夹具泄漏FD。
            def __exit__(self, *args):
                self.children.close()

        monkeypatch.setattr(artifacts.os, 'scandir', Children)

    # LLM: 发生任何正文读取都直接报错，不能以已返回链接码掩盖提前读取。
    # 函数用途: 固定链接拒绝发生在正文读取之前。
    def no_read(*args, **kwargs):
        pytest.fail('链接拒绝之前不应读取任何正文')

    monkeypatch.setattr(artifacts, 'read_bytes_beneath', no_read)
    with pytest.raises(artifacts.RunOutError) as caught:
        artifacts._manifest(scratch, 64, 64 * 1024 * 1024)
    assert caught.value.error_code == 'SCRIPT_RUN_OUT_LINK_REJECTED'


# LLM: 对真实目录制造超额，任何正文读取都使测试失败；不是先全量读取再断言计数。
# 函数用途: 固定文件数及字节热路径上限。
@pytest.mark.parametrize('kind', ['files', 'bytes'])
def test_caps_stop_before_reading_content(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent.workflows import run_artifacts as artifacts

    scratch = tmp_path / 'scratch'
    out = scratch / 'run_out'
    out.mkdir(parents=True)
    if kind == 'files':
        for number in range(65):
            (out / str(number)).write_bytes(b'x')
    else:
        with (out / 'large').open('wb') as stream:
            stream.truncate(ScriptRunner.RUN_OUT_MAX_BYTES + 1)

    # LLM: 超额清单必须在读取正文之前结束，替身不能返回伪造数据使断言变绿。
    # 函数用途: 拦截超额后的任何正文读取。
    def no_read(*args, **kwargs):
        pytest.fail('超额清单不允许进入正文读取')

    monkeypatch.setattr(artifacts, 'read_bytes_beneath', no_read)
    with pytest.raises(artifacts.RunOutError) as caught:
        artifacts._manifest(scratch, ScriptRunner.RUN_OUT_MAX_FILES, ScriptRunner.RUN_OUT_MAX_BYTES)
    assert caught.value.error_code == 'SCRIPT_RUN_OUT_LIMIT_EXCEEDED'


# LLM: 故障发生在真实暂存已有文件之后或真实rename之后，要求整个集合回滚；不允许空执行假成功。
# 函数用途: 验证复制、rename/ACK及fsync失败不发表部分产物。
@pytest.mark.parametrize('phase', ['copy', 'rename_before', 'rename_after', 'fsync'])
def test_harvest_failure_rolls_back_whole_attempt(tmp_path, monkeypatch, phase):
    from types import SimpleNamespace

    from agent_py_agent.agent.workflows import run_artifacts as artifacts

    home, _, engine = setup(tmp_path, code("(out / 'a').write_bytes(b'a'); (out / 'b').write_bytes(b'b')"))
    original_write, original_rename, original_fsync = artifacts.write_bytes_atomic_beneath, artifacts.os.rename, artifacts.os.fsync
    state = {'copies': 0, 'faults': 0, 'parent_identity': None}

    # LLM: 只在第二份复制注入一次明确I/O错误，其余复制沿正式nofollow写入。
    # 函数用途: 验证集合中途复制失败不会发布半套产物。
    def copy(*args, **kwargs):
        state['copies'] += 1
        if phase == 'copy' and state['copies'] == 2:
            state['faults'] += 1
            raise OSError('simulated I/O error')
        return original_write(*args, **kwargs)

    # LLM: 区分rename执行前失败和已执行后ACK失败，不影响无关rename。
    # 函数用途: 覆盖原子发布两侧的失败与回滚。
    def rename(source, target, **kwargs):
        if not str(source).startswith('.staging-'):
            return original_rename(source, target, **kwargs)
        if phase == 'rename_before':
            state['faults'] += 1
            raise OSError('simulated rename error')
        result = original_rename(source, target, **kwargs)
        info = os.fstat(kwargs['src_dir_fd'])
        state['parent_identity'] = (info.st_dev, info.st_ino)
        if phase == 'rename_after':
            state['faults'] += 1
            raise OSError('simulated rename acknowledgement error')
        return result

    # LLM: 仅目标父目录第一次确认刷盘失败，其余fsync沿原端口。
    # 函数用途: 验证发布确认失败也撤回整个集合。
    def fsync(descriptor):
        info = os.fstat(descriptor)
        if phase == 'fsync' and not state['faults'] and (info.st_dev, info.st_ino) == state['parent_identity']:
            state['faults'] += 1
            raise OSError('simulated fsync error')
        return original_fsync(descriptor)

    monkeypatch.setattr(artifacts, 'write_bytes_atomic_beneath', copy)
    # 只替换归档模块端口；修改全局os.rename会破坏nofollow平台能力集合的函数身份。
    monkeypatch.setattr(artifacts, 'os', SimpleNamespace(**{**vars(os), 'rename': rename, 'fsync': fsync}))
    run = engine.run_workflow(definition())
    assert state['copies'] == 2 and state['faults'] == 1
    assert run.status == 'failed' and run.steps[0].error_code == 'SCRIPT_RUN_OUT_HARVEST_FAILED'
    assert not run.steps[0].output_artifacts
    assert not list((home / 'data' / 'workflow_runs' / run.workflow_run_id).glob('artifacts/produce/*'))


# LLM: 数据源清理真实执行后注入明确失败，不能因已暂存就发布成功。
# 函数用途: 验证scratch退出失败会删除已有暂存。
@pytest.mark.parametrize('policy', ['fail', 'skip', 'escalate_to_agent'])
def test_datasource_cleanup_failure_discards_staged_files(tmp_path, monkeypatch, policy):
    home, _, engine = setup(tmp_path, code())
    close = runner_module.ScriptDatasourceRuntime.close

    # LLM: 先运行真实清理，再抛明确错误，不伪造资源已关闭的事实。
    # 函数用途: 固定数据源清理失败的归档硬失败出口。
    def fail_close(runtime):
        close(runtime)
        raise OSError('simulated cleanup error')

    monkeypatch.setattr(runner_module.ScriptDatasourceRuntime, 'close', fail_close)
    flow, repairs = definition(), []
    flow['steps'][0]['on_error'] = policy
    engine.escalation_handler = lambda *args: repairs.append(1) or {'value': 7}
    run = engine.run_workflow(flow)
    assert run.status == 'failed' and run.steps[0].status == 'failed'
    assert run.steps[0].error_code == 'SCRIPT_RUN_OUT_HARVEST_FAILED'
    assert repairs == [] and not run.steps[0].output_artifacts
    assert not list((home / 'data' / 'workflow_runs' / run.workflow_run_id).glob('artifacts/produce/*'))


# LLM: 100次都经过真实脚本、复制、rename与RunStore；先预热解释器及摘要缓存，再比较同一FD观察点。
# 函数用途: 固定批量归档无文件描述符泄漏。
def test_hundred_harvested_runs_have_zero_fd_delta(tmp_path, record_property):
    home, _, engine = setup(tmp_path, code())
    assert engine.run_workflow(definition()).status == 'succeeded'
    descriptors = '/dev/fd' if Path('/dev/fd').is_dir() else '/proc/self/fd'
    before = len(os.listdir(descriptors))
    for _ in range(100):
        run = engine.run_workflow(definition())
        assert run.status == 'succeeded'
        assert_artifacts(run.steps[0], home, run.workflow_run_id)
    delta = len(os.listdir(descriptors)) - before
    record_property('fd_delta', delta)
    assert delta == 0


# LLM: Python路径守卫真实运行，run_out只是既有scratch子目录，不增加Owner脚本或tasks写根。
# 函数用途: 验证归档启用后原拒写范围仍生效。
def test_run_out_does_not_widen_script_write_scope(tmp_path):
    home = tmp_path / 'global' / 'owners' / 'fixture'
    (home / 'tasks').mkdir(parents=True)
    body = (
        "blocked = 0\n"
        f"for name in {list(map(str, (home / 'tasks' / 'probe', home / 'scripts' / 'produce' / 'main.py')))!r}:\n"
        "    try:\n        Path(name).write_text('unexpected')\n"
        "    except PermissionError:\n        blocked += 1\n"
        "assert blocked == 2\n"
        "(out / 'result.txt').write_text('中文产物', encoding='utf-8')"
    )
    home, _, engine = setup(tmp_path, code(body), restricted=True)
    run = engine.run_workflow(definition())
    assert run.status == 'succeeded', run.to_dict()
    assert_artifacts(run.steps[0], home, run.workflow_run_id)
    assert not (home / 'tasks' / 'probe').exists()
    assert ScriptStore(home).get_script('produce')['code'] == code(body)


# LLM: 全程使用真实账本/执行器，Event只控制首个create到执行锁的交错点。
# 函数用途: 固定取消后重启不复活，新运行归档后再重启仍可见。
def test_headless_cancel_restart_resume_then_artifact_continuity(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    home, workspace, engine = setup(tmp_path, code())
    committed, release = threading.Event(), threading.Event()
    original = engine.store.create_run

    # LLM: 真实运行提交后暂停，下一步只由Event放行，不改持久状态。
    # 函数用途: 为重启连续性用例固定启动取消窗口。
    def create(*args, **kwargs):
        original(*args, **kwargs)
        committed.set()
        assert release.wait(10)

    monkeypatch.setattr(engine.store, 'create_run', create)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(engine.run_workflow, definition(), workflow_run_id='cancel-window')
        try:
            assert committed.wait(5)
            row = engine.store.load_run('cancel-window')
            assert engine.cancel_workflow_run(row.workflow_run_id, expected_revision=row.revision, operation_key='cancel') == []
        finally:
            release.set()
        assert future.result(5).status == 'cancelled'
    restart = WorkflowEngine(home, workspace_root=workspace)
    with pytest.raises(ValueError, match='WORKFLOW_CANCELLED'):
        restart.resume_workflow_run('cancel-window')
    restart.runner = runner(home, workspace)
    new = restart.run_workflow(definition())
    assert new.status == 'succeeded'
    second_restart = WorkflowEngine(home, workspace_root=workspace)
    assert second_restart.store.load_run('cancel-window').status == 'cancelled'
    assert_artifacts(second_restart.store.load_run(new.workflow_run_id).steps[0], home, new.workflow_run_id)


# LLM: 记录check实参但仍执行真实有限配额准入与结算，不替换投影结果。
# 函数用途: 验证每个复制字节计费以及发表迁移同时记删除和增加。
def test_real_quota_accounts_for_stage_and_publish_bytes(tmp_path):
    from contextlib import contextmanager

    home, workspace, engine = setup(tmp_path, code())
    observed = []

    # LLM: 继承真实配额执行器，仅观察check实参，不放宽限额或替代结算。
    # 类用途: 捕获暂存与发布的逐字节配额变化。
    class RecordingQuota(OwnerQuotaEnforcer):
        # LLM: 原准入事务完整进入和退出，观察器不能吞掉拒绝。
        # 函数用途: 在真实准入中安装只读记账探针。
        @contextmanager
        def admission(self, **kwargs):
            with super().admission(**kwargs) as admission:
                check = admission.check

                # LLM: 保留原check返回值和错误，记录的数据是同一批变更。
                # 函数用途: 观察配额变化集合而不修改实际计算。
                def record(changes, **options):
                    changes = tuple(changes)
                    observed.append(changes)
                    return check(changes, **options)

                admission.check = record
                yield admission

    engine.runner = runner(home, workspace, quota=RecordingQuota(home, max_bytes=10 * 1024 * 1024))
    run = engine.run_workflow(definition())
    assert run.status == 'succeeded', run.to_dict()
    assert_artifacts(run.steps[0], home, run.workflow_run_id)
    size = len('中文产物'.encode())
    assert len(observed) == 2
    assert [change.final_size for change in observed[0]] == [size]
    assert '.staging-' in str(observed[0][0].path)
    assert [change.final_size for change in observed[1]] == [None, size]
    assert str(observed[1][1].path.parent) == run.steps[0].output_dir


# LLM: 文件归档损失不是可跳过或可由模型伪造输出恢复的成功，不改变其它脚本错误政策。
# 函数用途: 验证归档硬失败保留机器码且不会被skip/escalation吞掉。
@pytest.mark.parametrize('policy', ['fail', 'skip', 'escalate_to_agent'])
def test_harvest_failure_is_not_silently_skipped_or_recovered(tmp_path, policy):
    home, _, engine = setup(tmp_path, code("(out / 'source').write_bytes(b'x'); (out / 'link').symlink_to(out / 'source')"))
    flow = definition()
    flow['steps'][0]['on_error'] = policy
    repairs = []
    engine.escalation_handler = lambda *args: repairs.append(1) or {'value': 7}
    run = engine.run_workflow(flow)
    assert run.status == 'failed' and run.steps[0].status == 'failed'
    assert run.steps[0].error_code == 'SCRIPT_RUN_OUT_LINK_REJECTED'
    assert repairs == []
    assert not list((home / 'data' / 'workflow_runs' / run.workflow_run_id).glob('artifacts/produce/*'))


# LLM: 显式debug可执行脚本但没有正式运行目标，不能建立永久归档或污染正式运行账。
# 函数用途: 固定单步调试仍不注入run_out。
def test_ephemeral_debug_does_not_inject_or_harvest(tmp_path):
    home, _, engine = setup(tmp_path, code())
    WorkflowStore(home).save_workflow(definition())
    result = engine.debug_step('out-flow', 'produce', allow_side_effects=True)
    assert result['status'] == 'succeeded', result
    assert result['output_data']['injected'] is False
    assert 'run_out_dir' not in result['output_data']['context']
    assert not (home / 'data' / 'workflow_runs').exists()


# LLM: 宿主环境的同名目录不代表本次授权；无正式目标/无quota/debug都不得继承或归档它。
# 函数用途: 验证三种非正式归档调用先清除继承run-out环境。
@pytest.mark.parametrize('mode', ['standalone', 'no_quota', 'debug'])
def test_inactive_run_out_ignores_inherited_environment(tmp_path, monkeypatch, mode):
    inherited = tmp_path / 'inherited'
    inherited.mkdir()
    monkeypatch.setenv('MY_AGENT_RUN_OUT', str(inherited))
    home, workspace, engine = setup(tmp_path, code())
    if mode == 'standalone':
        result = engine.runner.run_script('produce')
        assert result.ok, result.to_dict()
        output = result.output_data
    elif mode == 'no_quota':
        engine.runner = runner(home, workspace, quota=None)
        result = engine.run_workflow(definition())
        assert result.status == 'succeeded', result.to_dict()
        output = result.steps[0].output_snapshot
    else:
        WorkflowStore(home).save_workflow(definition())
        result = engine.debug_step('out-flow', 'produce', allow_side_effects=True)
        assert result['status'] == 'succeeded', result
        output = result['output_data']
    assert output['injected'] is False
    assert 'run_out_dir' not in output['context']
    assert list(inherited.iterdir()) == []


# LLM: rename已经生效后发生首因，再让递归清理持续失败；最终命名空间必须先原子撤回，稳定码和首因不得丢。
# 函数用途: 验证回滚自身失败只残留私有staging，不留下半套正式产物。
@pytest.mark.parametrize('phase', ['ack', 'fsync'])
def test_publish_rollback_failure_withdraws_final_set_before_cleanup(tmp_path, monkeypatch, phase):
    from types import SimpleNamespace

    from agent_py_agent.agent.workflows import run_artifacts as artifacts

    home, scratch = tmp_path / 'owner', tmp_path / 'scratch'
    home.mkdir()
    (scratch / 'run_out').mkdir(parents=True)
    for name in ('a', 'b'):
        (scratch / 'run_out' / name).write_bytes(name.encode())
    batch = artifacts.RunArtifactBatch(artifacts.RunOutTarget(home, 'run', 'step', 'attempt'),
                                      OwnerQuotaEnforcer(home, max_bytes=1024 * 1024))
    assert batch.stage(scratch, max_files=64, max_bytes=1024)
    original_os, original_shutil = artifacts.os, artifacts.shutil
    primary = OSError('simulated publish error')
    state, removed = {'renamed': False, 'failed': False}, []

    # LLM: 仅正式发布rename后注入ACK失败，撤回rename保持真实。
    # 函数用途: 制造已生效但未确认的发布首因。
    def rename(source, target, **kwargs):
        result = original_os.rename(source, target, **kwargs)
        if source == batch.staging_name and target == batch.target.attempt_id:
            state['renamed'] = True
            if phase == 'ack':
                state['failed'] = True
                raise primary
        return result

    # LLM: 发布后的首个父目录刷盘失败一次，撤回清理的刷盘不替换首因。
    # 函数用途: 注入发布耐久性失败。
    def fsync(descriptor):
        if phase == 'fsync' and state['renamed'] and not state['failed']:
            state['failed'] = True
            raise primary
        return original_os.fsync(descriptor)

    # LLM: 先真实删除一个叶子，再持续拒绝其余清理；若在final名称递归删，就会观察到残缺正式集合。
    # 函数用途: 固定回滚清理也失败的I/O控制。
    def fail_cleanup(name, *, dir_fd):
        removed.append(name)
        child = original_os.open(name, original_os.O_RDONLY | original_os.O_DIRECTORY, dir_fd=dir_fd)
        try:
            try:
                original_os.unlink('a', dir_fd=child)
            except FileNotFoundError:
                pass
        finally:
            original_os.close(child)
        raise OSError('simulated cleanup error')

    monkeypatch.setattr(artifacts, 'os', SimpleNamespace(**{**vars(original_os), 'rename': rename, 'fsync': fsync}))
    monkeypatch.setattr(artifacts, 'shutil', SimpleNamespace(**{**vars(original_shutil), 'rmtree': fail_cleanup}))
    try:
        batch.publish()
    except (OSError, ValueError) as exc:
        error = exc
    else:
        pytest.fail('归档失败不能返回成功引用')
    parent = home.joinpath(*batch.parent_parts)
    assert state['failed'] and removed
    assert not (parent / 'attempt').exists()
    assert all(name != 'attempt' for name in removed)
    assert isinstance(error, artifacts.RunOutError) and error.error_code == 'SCRIPT_RUN_OUT_HARVEST_FAILED'
    assert error.__cause__ is primary and not batch.published
    assert (parent / batch.staging_name / 'b').read_bytes() == b'b'
    monkeypatch.setattr(artifacts, 'shutil', original_shutil)
    batch.discard()
    assert list(parent.iterdir()) == []


# LLM: 仅在正式归档端口注入ACK/fsync及回滚I/O失败，runner、engine、Store都真实执行。
# 函数用途: 固定原始errno与回滚诊断从runner DTO到耐久步骤的脱敏透传。
@pytest.mark.parametrize('phase', ['ack', 'fsync'])
def test_workflow_preserves_publish_cause_and_rollback_diagnostics(tmp_path, monkeypatch, phase):
    import errno
    from types import SimpleNamespace

    from agent_py_agent.agent.workflows import run_artifacts as artifacts

    home, _, engine = setup(tmp_path, code())
    original_os, original_shutil = artifacts.os, artifacts.shutil
    run_script, results = engine.runner.run_script, []
    state = {'renamed': False, 'failed': False, 'cleanups': 0}
    primary = OSError(errno.EIO, 'private diagnostic text')

    # LLM: 原执行结果不被替换；捕获同一正式runner返回对象。
    # 函数用途: 核对runner而非仅batch层的诊断。
    def observe(*args, **kwargs):
        result = run_script(*args, **kwargs)
        results.append(result)
        return result

    # LLM: 仅staging到final的rename后失败，回滚撤回仍执行真实原子rename。
    # 函数用途: 注入已生效但未确认的归档发布。
    def rename(source, target, **kwargs):
        result = original_os.rename(source, target, **kwargs)
        if str(source).startswith('.staging-'):
            state['renamed'] = True
            if phase == 'ack':
                state['failed'] = True
                raise primary
        return result

    # LLM: 只让发布后首次fsync失败，后续撤回的刷盘不阻塞。
    # 函数用途: 以真实OSError errno固定发布耐久性首因。
    def fsync(descriptor):
        if phase == 'fsync' and state['renamed'] and not state['failed']:
            state['failed'] = True
            raise primary
        return original_os.fsync(descriptor)

    # LLM: 只替换归档模块清理端口，不妨碍真实scratch及数据源清理。
    # 函数用途: 在发布首因之后持续制造私有staging清理失败。
    def fail_cleanup(*args, **kwargs):
        state['cleanups'] += 1
        raise OSError(errno.EPERM, 'private rollback text')

    monkeypatch.setattr(engine.runner, 'run_script', observe)
    monkeypatch.setattr(artifacts, 'os', SimpleNamespace(**{**vars(original_os), 'rename': rename, 'fsync': fsync}))
    monkeypatch.setattr(artifacts, 'shutil', SimpleNamespace(**{**vars(original_shutil), 'rmtree': fail_cleanup}))
    run = engine.run_workflow(definition())
    assert state['failed'] and state['cleanups'] and len(results) == 1
    assert run.status == 'failed' and results[0].error_code == 'SCRIPT_RUN_OUT_HARVEST_FAILED'
    diagnostic = results[0].escalation_context['execution_error']
    assert diagnostic.get('cause_chain') == [{'exception_type': 'OSError', 'errno': errno.EIO}]
    assert diagnostic.get('rollback_incomplete') is True
    assert 'private diagnostic text' not in json.dumps(diagnostic)
    assert 'private rollback text' not in json.dumps(diagnostic)
    persisted = engine.store.load_run(run.workflow_run_id)
    assert persisted.steps[0].escalation_result['execution_error'] == diagnostic
    assert not persisted.steps[0].output_artifacts and not results[0].output_artifacts
    parent = home / 'data' / 'workflow_runs' / run.workflow_run_id / 'artifacts' / 'produce'
    remaining = list(parent.iterdir())
    assert remaining and all(path.name.startswith('.staging-') for path in remaining)
    for path in remaining:
        original_shutil.rmtree(path)


@pytest.mark.parametrize('phase,path_role', [
    ('run_out_prepare', 'run_out_dir'),
    ('manifest_read', 'run_out_dir'),
    ('data_source_close', 'datasource_manifest'),
    ('scratch_cleanup', 'staging_root'),
])
def test_archive_missing_leaf_reports_safe_exact_phase(tmp_path, monkeypatch, phase, path_role):
    """同一errno须定位到宿主实际阶段；不回显路径，也不把执行后故障说成未启动。"""
    from agent_py_agent.agent.workflows import datasource_runtime, run_artifacts

    home, _, engine = setup(tmp_path, 'import json\nprint(json.dumps({"value":7}))')
    WorkflowStore(home).save_workflow(definition())

    def missing():
        return FileNotFoundError(2, 'private customer secret', '/private/customer/token-secret')

    if phase == 'run_out_prepare':
        original = runner_module.open_directory_beneath

        def open_directory(root, parts, **options):
            if parts == ('run_out',) and options.get('create'):
                raise missing()
            return original(root, parts, **options)

        monkeypatch.setattr(runner_module, 'open_directory_beneath', open_directory)
    elif phase == 'manifest_read':
        def manifest(*_args):
            raise missing()

        monkeypatch.setattr(run_artifacts, '_manifest', manifest)
    elif phase == 'data_source_close':
        original = datasource_runtime.ScriptDatasourceRuntime.close

        def close(runtime):
            original(runtime)
            raise missing()

        monkeypatch.setattr(datasource_runtime.ScriptDatasourceRuntime, 'close', close)
    else:
        original = runner_module.shutil.rmtree

        def remove(path, *args, **options):
            result = original(path, *args, **options)
            if Path(path).name.startswith('produce_'):
                raise missing()
            return result

        monkeypatch.setattr(runner_module.shutil, 'rmtree', remove)
    record = engine.run_workflow(definition(), owner_id='local/main')
    step = record.steps[0]
    assert record.status == 'failed' and step.error_code == 'SCRIPT_RUN_OUT_HARVEST_FAILED'
    facts = step.escalation_result['execution_error']
    assert facts['archive_phase'] == phase and facts['path_role'] == path_role
    assert {'exception_type': 'FileNotFoundError', 'errno': 2} in facts['cause_chain']
    expected = 'not_started' if phase == 'run_out_prepare' else 'unknown'
    assert step.escalation_result['effect_outcome'] == expected
    assert 'customer' not in json.dumps(record.to_dict()) and 'token-secret' not in json.dumps(record.to_dict())


@pytest.mark.parametrize('operation', ['publish', 'discard'])
def test_archive_rethrow_keeps_original_io_cause(tmp_path, monkeypatch, operation):
    """回滚重复遇同一RunOutError时不能用自身覆盖__cause__，丢掉原errno。"""
    from agent_py_agent.agent.workflows.run_artifacts import (
        RunArtifactBatch,
        RunOutError,
        RunOutTarget,
    )

    home, scratch = tmp_path / 'owner', tmp_path / 'scratch'
    home.mkdir()
    (scratch / 'run_out').mkdir(parents=True)
    (scratch / 'run_out/item.txt').write_text('synthetic')
    batch = RunArtifactBatch(RunOutTarget(home, 'new-diagnostic', 'produce', 'a' * 32),
                             OwnerQuotaEnforcer(home, max_bytes=0))
    assert batch.stage(scratch, max_files=64, max_bytes=1024)
    original = batch._parent
    cause = FileNotFoundError(2, 'private path', '/private/customer/secret')
    failure = RunOutError('SCRIPT_RUN_OUT_HARVEST_FAILED')
    failure.__cause__ = cause

    def missing_parent(**_options):
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(batch, '_parent', missing_parent)
        with pytest.raises(RunOutError) as caught:
            getattr(batch, operation)()
        assert caught.value is failure and failure.__cause__ is cause
        assert {'exception_type': 'FileNotFoundError', 'errno': 2} in failure.diagnostics()['cause_chain']
    assert batch._parent == original
    batch.discard()
