"""R2：真实调度工具栈不得把历史正文当作当前授权记录。"""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import time
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.assets.descriptor import (
    AccessTarget,
    AssetDescriptor,
    CredentialBindingRef,
    DataSourceSpec,
)
from agent_py_agent.agent.assets.revision_history import blob_get, latest_version, record_version
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.scheduler.execution import _bind_pinned_tool, _execute_target_tool
from agent_py_agent.agent.scheduler.execution_policy import (
    ExecutionTargetError,
    resolve_execution_target,
)
from agent_py_agent.agent.scheduler.schedule import ScheduleValidationError
from agent_py_agent.agent.workflows import script_runner
from agent_py_agent.agent.workflows.datasource_runtime import (
    bind_script_execution_record,
    script_execution_record,
    script_history_record,
)
from agent_py_agent.agent.workflows.script_runner import ScriptRunner
from agent_py_agent.tests.test_schedule_control import _agent

SOURCE = 'import json\nprint(json.dumps({"version": 1}))\n'


# LLM: 发布、历史与工具运行均用原实现；计数包装只观察并委托真实进程执行。
# 函数用途: 构建隔离的原宿主和首发 revision=1 脚本。
@pytest.fixture
def case(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    home = agent.home_paths.owner_home_dir
    tool = agent.tools.runtime_snapshot(allowed_tools=['scripts']).runtime('scripts').handler
    store = ScriptStore(home, workspace_root=tool._workspace_root())
    descriptor = AssetDescriptor(id='binding', kind='script', name='调度绑定')
    current = store.save_script(descriptor, SOURCE)
    assert current['revision'] == 1
    version = latest_version(home, 'script', 'binding')
    assert version['version'] == version['storage_revision'] == 1
    payload = json.loads(blob_get(home, version['payload_blob']))
    assert 'revision' not in payload
    calls, processes = [], []
    original = ScriptRunner.run_script
    execute_process = script_runner._execute

    # LLM: 记录原数据源 resolver 绑定的执行记录，不替代 runner 或进程边界。
    # 函数用途: 观察 runner 次数、有效权限和历史身份。
    def counted(runner, script_id, inputs=None, **options):
        calls.append((script_execution_record(home, script_id, runner.workspace_root),
                      script_history_record(home, script_id, runner.workspace_root), options))
        return original(runner, script_id, inputs, **options)

    monkeypatch.setattr(ScriptRunner, 'run_script', counted)

    # LLM: 不替代子进程或返回码，所有业务输出仍由真实脚本产生。
    # 函数用途: 区分 runner 准备次数和真正进程执行次数。
    def counted_process(*args, **kwargs):
        processes.append(args[1])
        return execute_process(*args, **kwargs)

    monkeypatch.setattr(script_runner, '_execute', counted_process)
    yield SimpleNamespace(agent=agent, home=home, tool=tool, store=store,
        descriptor=descriptor, version=version, calls=calls, processes=processes,
        target={'kind': 'script', 'asset_id': 'binding', 'pinned_version': 1,
                'input_ref': {'inputs': {}}})
    agent.scheduler_service.monitor_pool.shutdown()


# LLM: 真实宿主轮创建并结算准确 attempt；目标和输入经过真实 runtime/handler。
# 函数用途: 调用与生产相同的定时工具入口。
def _execute(case, *, request_id='r2-first', target=None):
    return _execute_target_tool(case.agent, RunParams(request_id=request_id,
        run_id=request_id, task_id=request_id,
        root_user_prompt='固定脚本执行', source='scheduler_tool'), target or case.target)


# LLM: 此反例保留旧生产绑定顺序；缺失或非法当前存储身份不能被相同执行引用重新授权。
# 函数用途: 证明历史正文不是当前授权记录，所有无效 revision 均零 runner 调用。
@pytest.mark.parametrize('bound_revision', [None, 0, True, -1, '1'])
def test_revision_stripped_history_is_not_current_authority(case, bound_revision):
    runtime = case.agent.tools.runtime_snapshot(allowed_tools=['scripts']).runtime('scripts')
    target, historical = resolve_execution_target(case.agent, case.target, runtime=runtime)
    assert 'revision' not in historical
    assert target['expected_script_pin']['revision'] == 1
    pinned = _bind_pinned_tool(case.tool, target, historical)
    if bound_revision is not None:
        historical = {**historical, 'revision': bound_revision}
    with bind_script_execution_record(case.home, 'binding', case.tool._workspace_root(), historical):
        result = pinned.execute({'action': 'run', 'script_id': 'binding', 'inputs': {}})
    data = json.loads(result.output)
    assert not result.ok and data.get('error_code') == 'SCRIPT_REVISION_DRIFT', data
    assert case.calls == []
    assert script_execution_record(case.home, 'binding', case.tool._workspace_root()) is None


# LLM: 首发也传完整发布版本，不走current-match捷径；原ContextVar、宿主工具栈及runner均保留。
# 函数用途: 核对第一次发布的精确历史执行及当前权限身份，真实进程只启动一次。
def test_first_publication_reaches_runner_once_through_runtime(case):
    execution, output = _execute(case)
    data = json.loads(output)
    assert (data.get('error_code', ''), len(case.calls)) == ('', 1)
    assert execution.result.ok, data
    assert data['ok'] and data['exit_code'] == 0
    assert data['output_data'] == {'version': 1}
    assert data['source_check']['pin_resolution'] == {'mode': 'history', 'version': 1,
        'code_sha256': case.version['code_sha256']}
    assert len(case.calls) == 1
    current, history, _options = case.calls[0]
    assert current['revision'] == 1 and history == case.version
    assert len(case.processes) == 1


# LLM: 当前定义有真实后继发布时，内容仍取选中的旧 SHA，不以当前代码替换。
# 函数用途: 证明编辑后的定时执行确实输出旧版本且保留当前存储身份。
def test_edited_current_runs_historical_code_once(case):
    current = case.store.save_script(case.descriptor, SOURCE.replace('1', '2'))
    execution, output = _execute(case)
    data = json.loads(output)
    assert execution.result.ok and data['output_data'] == {'version': 1}, data
    assert data['source_check']['pin_resolution'] == {'mode': 'history', 'version': 1,
        'code_sha256': case.version['code_sha256']}
    assert len(case.calls) == len(case.processes) == 1
    effective, historical, _options = case.calls[0]
    assert effective['revision'] == current['revision']
    assert historical['version'] == historical['storage_revision'] == 1
    assert hashlib.sha256(effective['code'].encode()).hexdigest() == case.version['code_sha256']
    assert case.store.get_script('binding')['code'] == current['code']


# LLM: 同代码 SHA 的不同正式执行定义仍是不同历史版本，不能把 pin 按 SHA 重选为最新合同。
# 函数用途: 用真实 Store 发布反例，确保旧版本仍执行自己的 IO 合同而非同 SHA 后继。
def test_same_code_sha_keeps_exact_selected_execution_definition(case):
    changed = replace(case.descriptor,
        io_contract=replace(case.descriptor.io_contract, outputs=('not_in_v1',)))
    case.store.save_script(changed, SOURCE)
    same_code = latest_version(case.home, 'script', 'binding')
    assert same_code['code_sha256'] == case.version['code_sha256']
    assert same_code['version'] != case.version['version']
    case.store.save_script(case.descriptor, SOURCE.replace('1', '3'))
    execution, output = _execute(case)
    assert case.calls[0][1]['version'] == case.version['version']
    assert execution.result.ok and json.loads(output)['output_data'] == {'version': 1}, output


# LLM: 侧文件通过真实发布接口进入历史，不用 runner 替身伪造读取结果。
# 函数用途: 为同一当前脚本发布带精确侧文件的内容，按用例需要再创建不同代码后继。
def _side_history(case, *, edit_current=True):
    code = ('import json\nfrom pathlib import Path\n'
            'print(json.dumps({"side": Path("rows.json").read_text()}))\n')
    first = case.store.save_script(case.descriptor, code)
    history = record_version(case.home, 'script', 'binding', payload=first, code=code.encode(),
        storage_revision=first['revision'], execution_definition_ref=first['execution_definition_ref'],
        side_files={'rows.json': 'historical-rows\n'})['record']
    if edit_current:
        case.store.save_script(case.descriptor, SOURCE.replace('1', '3'))
    return history, {**case.target, 'pinned_version': history['version']}


# LLM: 历史物化必须保留代码和侧文件精确字节；临时目录在真实进程退出后清理。
# 函数用途: 证明调度入口不会丢历史侧文件或读取当前目录替代品。
def test_history_side_files_are_exact_and_staging_is_removed(case):
    history, target = _side_history(case)
    execution, output = _execute(case, target=target)
    data = json.loads(output)
    assert execution.result.ok and data['output_data'] == {'side': 'historical-rows\n'}, data
    assert case.calls[0][1] == history
    assert len(case.calls) == len(case.processes) == 1
    assert not Path(case.processes[0]).exists()
    assert not list((case.home / 'tmp/script_exec').iterdir())


# LLM: 历史侧文件属于被选发布，即使当前 revision/代码正好匹配也不能省掉准确物化。
# 函数用途: 防止 current-match 快路丢失已选历史发布的侧文件。
def test_current_match_still_materializes_selected_history_side_files(case):
    history, target = _side_history(case, edit_current=False)
    execution, output = _execute(case, target=target)
    data = json.loads(output)
    assert execution.result.ok and data['output_data'] == {'side': 'historical-rows\n'}, {
        'error_code': data.get('error_code'), 'stderr': data.get('stderr_preview'),
        'pin_resolution': data['source_check']['pin_resolution'], 'history': case.calls[0][1],
    }
    assert case.calls[0][1] == history
    assert len(case.calls) == len(case.processes) == 1
    assert not Path(case.processes[0]).exists()


# LLM: 首次解析前任意必需 blob 缺失或损坏都由原历史门关闭，不接触 runner。
# 函数用途: 防止失败后把当前代码当历史执行。
@pytest.mark.parametrize('field', ['code_sha256', 'payload_blob', 'side_blob'])
@pytest.mark.parametrize('damage', ['missing', 'corrupt'])
def test_missing_or_corrupt_history_fails_closed(case, field, damage):
    history, target = _side_history(case)
    digest = history['side_blobs']['rows.json'] if field == 'side_blob' else history[field]
    blob = case.home / 'data/asset_evolution/blobs' / digest[:2] / digest
    if damage == 'missing':
        blob.unlink()
    else:
        blob.write_bytes(b'not-the-published-bytes')
    with pytest.raises(ExecutionTargetError) as caught:
        _execute(case, target=target)
    assert caught.value.error_code == 'SCHEDULER_EXECUTION_DEFINITION_STALE'
    assert caught.value.reason == 'blob_missing'
    assert not case.calls and not case.processes


# LLM: 删除当前资产不为旧发布记录恢复新权限，历史仍可解析但执行不能凭空恢复当前授权。
# 函数用途: 固定已有当前记录缺失时的失败关闭策略，零 runner 调用。
def test_deleted_script_does_not_recreate_current_authority(case):
    assert case.store.delete_script('binding')
    _target, historical = resolve_execution_target(case.agent, case.target)
    assert historical['code'] == SOURCE
    execution, output = _execute(case)
    assert not execution.result.ok
    assert json.loads(output)['error_code'] == 'SCRIPT_REVISION_DRIFT'
    assert not case.calls and not case.processes


# LLM: 历史 enabled=True 不能撤销当前停用；必须使用原 runner 的当前开关检查。
# 函数用途: 验证旧正文仍被当前 Owner 的停用决定阻止启动。
def test_current_disabled_still_blocks_historical_execution(case):
    case.store.save_script(case.descriptor, SOURCE.replace('1', '2'))
    case.store.set_enabled('binding', False)
    execution, output = _execute(case)
    data = json.loads(output)
    assert not execution.result.ok and data['error_code'] == 'ASSET_DISABLED_BY_OWNER', data
    assert data['source_check']['pin_resolution']['mode'] == 'history'
    assert case.calls[0][0]['enabled'] is False
    assert not case.processes


# LLM: runtime 的 owner 校验必须先于执行，不能把另一 Owner 的 handler 与本地历史拼装。
# 函数用途: 核对跨 Owner 的负例在真实调度入口被拒绝。
def test_foreign_owner_handler_is_rejected(case, tmp_path, monkeypatch):
    monkeypatch.setattr(case.tool, 'owner_home', tmp_path / 'foreign')
    with pytest.raises(ScheduleValidationError, match='owner'):
        _execute(case)
    assert not case.calls and not case.processes


# LLM: 同名项目资产不能借 Owner 历史 pin 获得执行资格；真实覆盖 Store 决定 source_scope。
# 函数用途: 核对 workspace 身份变化的负例，历史 SHA 不重新授权项目内容。
def test_workspace_shadow_cannot_consume_owner_history_pin(case):
    workspace = case.tool._workspace_root()
    assert workspace is not None
    case.store.save_script(case.descriptor, SOURCE.replace('1', '9'), target_scope='workspace')
    assert case.store.get_script('binding')['source_scope'] == 'workspace'
    execution, output = _execute(case)
    data = json.loads(output)
    assert not execution.result.ok and data['error_code'] == 'SCRIPT_REVISION_DRIFT', data
    assert not case.calls and not case.processes


# LLM: storage revision 是可变存储身份，不是执行定义；相同正文/合同仍须执行原发布，不能错误漂移或重选最新。
# 函数用途: 核对正常同内容写回后仍保留精确旧发布及当前权限身份。
def test_same_code_storage_revision_drift_keeps_exact_published_pin(case):
    case.store.save_script(case.descriptor, SOURCE)
    current = case.store.get_script('binding')
    assert current['revision'] == 2
    assert current['execution_definition_ref'] == case.version['execution_definition_ref']
    assert latest_version(case.home, 'script', 'binding') == case.version
    execution, output = _execute(case)
    data = json.loads(output)
    assert execution.result.ok and data['output_data'] == {'version': 1}, data
    assert data['source_check']['pin_resolution'] == {'mode': 'history', 'version': 1,
        'code_sha256': case.version['code_sha256']}
    assert len(case.calls) == len(case.processes) == 1
    effective, history, _options = case.calls[0]
    assert effective['revision'] == current['revision'] and history == case.version


# LLM: retry metadata 不进入执行引用但会生成正式版本；精确 pin 必须保留旧重试许可，不能按相同 SHA/ref 重选。
# 函数用途: 证伪只传执行引用却丢失明确发布版本的调度绑定。
def test_scheduler_pin_keeps_selected_retry_contract_when_ref_and_sha_repeat(case):
    changed = replace(case.descriptor, metadata={'retry_policy': {'idempotent': True}})
    case.store.save_script(changed, SOURCE)
    newer = latest_version(case.home, 'script', 'binding')
    assert newer['version'] > case.version['version']
    for key in ('code_sha256', 'execution_definition_ref'):
        assert newer[key] == case.version[key]
    execution, output = _execute(case)
    assert execution.result.ok and json.loads(output)['output_data'] == {'version': 1}, output
    assert len(case.calls) == len(case.processes) == 1
    effective, history, _options = case.calls[0]
    assert history == case.version
    assert effective['metadata']['retry_policy']['idempotent'] is False
    assert case.store.get_script('binding')['metadata']['retry_policy']['idempotent'] is True


# LLM: 权限数据来自真实 Store 发布与当前读取；观察器只委托原 runner，子进程仍使用原 SDK。
# 函数用途: 证明旧内容不能借历史网络、凭据或可写源权限扩大当前权限。
def test_historical_execution_intersects_current_access_ceiling(case):
    from agent_py_agent.agent.assets.datasource_store import DatasourceStore

    for identity in ('events-a', 'events-b', 'events-c'):
        DatasourceStore(case.home).save_datasource({'datasource_id': identity, 'kind': 'custom', 'title': identity})
    old = replace(case.descriptor,
        access_targets=(AccessTarget('api', 'http', 'https://example.invalid', method='GET'),),
        data_sources=(DataSourceSpec('events-a', 'custom', query_scope={'access': 'read_write'}),
                      DataSourceSpec('events-b', 'custom')),
        credential_refs=(CredentialBindingRef('cref-old'), CredentialBindingRef('cref-shared')))
    current = replace(case.descriptor,
        access_targets=(AccessTarget('local', 'file', 'input.json', extra={'read_only': True}),),
        data_sources=(DataSourceSpec('events-a', 'custom', query_scope={'access': 'read'}),
                      DataSourceSpec('events-c', 'custom')),
        credential_refs=(CredentialBindingRef('cref-new'), CredentialBindingRef('cref-shared')))
    code = '''import json, socket
from agent_py_agent.agent.workflows.datasource_runtime import current_datasource_bindings
try:
    with socket.socket() as client:
        client.connect(("127.0.0.1", 9))
    denied = False
except PermissionError:
    denied = True
print(json.dumps({"access": {key: row["access"] for key, row in current_datasource_bindings().items()}, "network_denied": denied}))
'''
    case.store.save_script(old, code)
    history = latest_version(case.home, 'script', 'binding')
    selected = {**case.target, 'pinned_version': history['version']}
    case.store.save_script(current, SOURCE.replace('1', '2'))
    execution, output = _execute(case, target=selected)
    data = json.loads(output)
    assert execution.result.ok, data
    assert data['output_data'] == {'access': {'events-a': 'read'}, 'network_denied': True}
    effective, record, options = case.calls[0]
    assert record['version'] == history['version'] and options['network_access'] is False
    assert effective['access_targets'] == []
    assert [row['credential_ref'] for row in effective['credential_refs']] == ['cref-shared']
    assert [(row['source_id'], row['query_scope']['access']) for row in effective['data_sources']] == [('events-a', 'read')]
    assert len(case.processes) == 1


# LLM: 静态规则覆盖 scheduler 的所有绑定装配入口；历史 payload 必须留在选择层，不另造当前记录绑定。
# 函数用途: 在允许的测试文件中沉淀同类缺陷的 AST 防线。
def test_scheduler_never_prebinds_historical_script_as_current():
    from agent_py_agent.agent.scheduler import execution

    tree = ast.parse(inspect.getsource(execution))
    assembly = {'_execute_target_tool', '_bind_pinned_tool', '_pinned_script_dispatch'}
    found = set()
    for function in tree.body:
        if isinstance(function, ast.FunctionDef) and function.name in assembly:
            found.add(function.name)
            calls = [ast.unparse(node.func) for node in ast.walk(function) if isinstance(node, ast.Call)]
            assert not any(name.split('.')[-1] == 'bind_script_execution_record' for name in calls)
            assert not any(name.split('.')[-1] in {'walk', 'rglob', 'list_versions'} for name in calls)
    assert found == assembly


# LLM: FD 观察为本进程真实内核目录，不从对象计数推算资源；预热后才测本次改动路径。
# 函数用途: 返回有界的当前文件描述符数量。
def _fd_count():
    root = '/proc/self/fd' if Path('/proc/self/fd').is_dir() else '/dev/fd'
    return len(os.listdir(root))


# LLM: 检查原 ExitStack/当前 prompt/ContextVar 的收尾，不以成功返回代替资源核验。
# 函数用途: 验证成功、当前停用及进程准备异常均 fd_delta=0 且清理本栈绑定。
@pytest.mark.parametrize('mode', ['success', 'disabled', 'process_error'])
def test_runtime_cleanup_has_zero_fd_delta(case, monkeypatch, mode):
    execution, _output = _execute(case, request_id='r2-warm')
    assert execution.result.ok
    case.store.save_script(case.descriptor, SOURCE.replace('1', '2'))
    if mode == 'disabled':
        case.store.set_enabled('binding', False)
    elif mode == 'process_error':
        # LLM: 此反例只替换进程启动边界，真实 resolver、runner 准备与 finally 全部保留。
        # 函数用途: 在已经创建 staging 后产生一次可复现的 I/O 失败。
        def unavailable(*args, **kwargs):
            raise OSError('simulated I/O error')

        monkeypatch.setattr(script_runner, '_execute', unavailable)
    before_params = getattr(case.agent, '_current_run_params', None)
    before = _fd_count()
    for index in range(3):
        execution, output = _execute(case, request_id=f'r2-{mode}-{index}')
        data = json.loads(output)
        assert execution.result.ok is (mode == 'success'), data
        if mode != 'success':
            expected = 'ASSET_DISABLED_BY_OWNER' if mode == 'disabled' else 'SCRIPT_CONFIGURATION_ERROR'
            assert data['error_code'] == expected
        assert script_execution_record(case.home, 'binding', case.tool._workspace_root()) is None
        assert script_history_record(case.home, 'binding', case.tool._workspace_root()) is None
        assert getattr(case.agent, '_current_run_params', None) is before_params
    fd_delta = _fd_count() - before
    assert fd_delta == 0, {'mode': mode, 'fd_delta': fd_delta}
    assert not list((case.home / 'tmp/script_exec').iterdir())


# LLM: 在 scheduler 已选历史、工具实际执行之前丢失 blob，原数据源 resolver 仍须再次失败关闭。
# 函数用途: 核对 ExitStack 内后段失败不执行代码、不漏 FD、不留下可信绑定。
def test_history_lost_after_scheduler_resolution_is_closed_and_clean(case, monkeypatch):
    from agent_py_agent.agent.tooling.registry import ToolRegistry

    assert _execute(case, request_id='r2-warm')[0].result.ok
    case.store.save_script(case.descriptor, SOURCE.replace('1', '2'))
    original = ToolRegistry.execute_tool
    removed = []

    # LLM: 执行器保持原实现，只控制既存发布 blob 在真实模块边界消失的时机。
    # 函数用途: 给第二次历史读取制造确定性的缺失反例。
    def remove_before_handler(registry, call, **kwargs):
        if call.tool_name == 'scripts' and not removed:
            digest = case.version['code_sha256']
            (case.home / 'data/asset_evolution/blobs' / digest[:2] / digest).unlink()
            removed.append(digest)
        return original(registry, call, **kwargs)

    monkeypatch.setattr(ToolRegistry, 'execute_tool', remove_before_handler)
    before = _fd_count()
    execution, output = _execute(case, request_id='r2-lost')
    data = json.loads(output)
    assert removed and not execution.result.ok and data['error_code'] == 'SCRIPT_REVISION_DRIFT', data
    assert data['source_check']['pin_resolution']['mode'] == 'missing'
    assert len(case.calls) == len(case.processes) == 1  # 只有预热实际运行。
    assert _fd_count() - before == 0
    assert script_execution_record(case.home, 'binding', case.tool._workspace_root()) is None


# LLM: Event 控制真实 runner 返回后的晚到窗口；只包原 runner，不生成替代业务结果。
# 函数用途: 验证取消终态不可逆、同 scope 活跃数不重叠、宿主 attempt 收口和本栈清理。
def test_cancelled_late_result_cannot_revive_task_or_overlap_scope(case, monkeypatch):
    from agent_py_agent.agent.concurrency.interrupt import InterruptHandle, register_interruptible

    completed, release = Event(), Event()
    original = ScriptRunner.run_script
    cleaned = []
    identifier = 'r2-late'
    thread = case.agent.conversation_store.threads.get_or_create({'channel': 'local',
        'channel_user_id': 'r2', 'canonical_user_id': 'r2',
        'owner_id': case.agent.home_paths.owner_id, 'owner_home': str(case.home)})
    tasks = case.agent.conversation_store.tasks
    tasks.bind({'thread_id': thread.thread_id, 'task_id': identifier, 'goal': '晚到结果', 'status': 'active'})

    # LLM: 原进程已退出、结果尚未交给工具时等待显式释放，不使用固定 sleep 猜时序。
    # 函数用途: 固定一次取消与结果交付的交错。
    def delayed(runner, script_id, inputs=None, **options):
        result = original(runner, script_id, inputs, **options)
        completed.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(ScriptRunner, 'run_script', delayed)
    handle = InterruptHandle()
    handle.claim()

    # LLM: 使用原中断登记，退出核验 ContextVar 已恢复；取消事实不由返回值决定。
    # 函数用途: 在真实监控池里执行同一个目标。
    def dispatch():
        with register_interruptible(identifier, handle=handle):
            try:
                return _execute(case, request_id=identifier)
            finally:
                cleaned.append((script_execution_record(case.home, 'binding', case.tool._workspace_root()),
                                script_history_record(case.home, 'binding', case.tool._workspace_root())))

    pool = case.agent.scheduler_service.monitor_pool
    future = pool.submit(case.home, 'same-monitor', identifier, dispatch)
    try:
        assert completed.wait(10)
        duplicate = pool.submit(case.home, 'same-monitor', 'r2-duplicate', dispatch)
        assert duplicate.result(timeout=2)['reason'] == 'already_running'
        tasks.update_status({'task_id': identifier, 'status': 'cancelled', 'expected_status': 'active'})
        handle.cancel()
        release.set()
        with pytest.raises(InterruptedError):
            future.result(timeout=10)
        assert handle.wait_cleanup_until(time.monotonic() + 5)
        assert handle.cleanup_pending is False
        assert tasks.load(identifier).status == 'cancelled'
        repo = case.agent.subagents.runtime_db
        row = repo.agent_run_for_run_id(identifier)
        attempt_id = row['current_attempt_id']
        assert row['status'] == repo.get_attempt(attempt_id)['status'] == 'cancelled'
        with pytest.raises(InterruptedError):
            _execute(case, request_id=identifier)
        assert repo.agent_run_for_run_id(identifier)['current_attempt_id'] == attempt_id
        assert tasks.load(identifier).status == 'cancelled'
        assert len(case.calls) == len(case.processes) == 1
        assert cleaned == [(None, None)]
        assert not list((case.home / 'tmp/script_exec').iterdir())
    finally:
        release.set()
        pool.shutdown()


# LLM: 唯一替代项是外发渠道回执；调度预约、真实脚本、原 TaskLink/outbox 与恢复均走产品实现。
# 函数用途: 从真实 scheduler tick 验证冻结重投及欠发期间取消，原终态绝不复活，业务不重跑。
@pytest.mark.parametrize('cancel_after_freeze', [False, True])
def test_scheduler_notification_retry_never_reexecutes_script(case, cancel_after_freeze):
    from agent_py_agent.agent.conversation import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
    )
    from agent_py_agent.agent.scheduler.execution_policy import execution_definition_ref
    from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest
    from agent_py_agent.tests.test_scheduler_runtime import _RetryDelivery

    now = time.time()
    store = case.agent.conversation_store
    thread = store.threads.get_or_create({'channel': 'feishu', 'channel_user_id': 'ou_r2_actor',
        'channel_conversation_id': 'oc_r2_chat', 'canonical_user_id': 'ou_r2_actor',
        'owner_id': case.agent.home_paths.owner_id, 'owner_home': str(case.home), 'now': now - 100})
    target = {**case.target, 'execution_definition_ref': execution_definition_ref('script', 'binding',
        case.store.get_script('binding'))}
    repo, service = case.agent.scheduler_repository, case.agent.scheduler_service
    service.monitor_jitter_window_seconds = 0
    job = repo.create_job(SchedulerJobCreateRequest(name='通知恢复', prompt='', thread_id=thread.thread_id,
        execution_target=target, delivery={'mode': 'origin'}, creator={'local': True},
        schedule={'kind': 'every', 'every_seconds': 600, 'anchor_at': now, 'timezone': 'UTC'}, now=now - 100))[0]
    channels = _RetryDelivery()
    # 工具型调度交付直接使用宿主服务；与 runtime 共用同一个外发边界替身。
    case.agent.delivery_service = channels
    runtime = BackgroundMainAgentRuntime(agent=case.agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': store, 'scheduler_service': service})
    service.enqueue_ready_runs(now=now)
    signal = store.wakes.pending()[0]
    scheduler.tick(now=now)
    assert len(case.calls) == len(case.processes) == len(channels.attempts) == 1
    assert store.tasks.load(signal.root_task_id).status == 'completed'
    pending = store.wakes.pending_one(signal.wake_signal_id)
    assert pending.metadata['owner_delivery']['external_sent'] is False
    queued = repo.get_active_run(signal.root_task_id)
    assert queued['status'] == 'queued' and queued['claim_id'] == ''
    assert queued['structured_result']['output_data'] == {'version': 1}
    assert repo.history(job_id=job['job_id'])[0] == []
    if cancel_after_freeze:
        from agent_py_agent.agent.scheduler.control import cancel_monitor

        current = repo.get_job(job['job_id'])
        cancelled = cancel_monitor(case.agent, repo, job['job_id'], current['version'])
        assert cancelled['cancellation_status'] == 'stopped', cancelled
        rows, errors = repo.history(job_id=job['job_id'])
        assert not errors and len(rows) == 1 and rows[0]['status'] == 'cancelled', rows
        assert repo.get_active_run(signal.root_task_id) is None
        channels.status = 'sent'
        assert scheduler.tick(now=now + 31) == []
        assert len(case.calls) == len(case.processes) == len(channels.attempts) == 1
        assert store.tasks.load(signal.root_task_id).status == 'completed'
        assert repo.release_run_claim(signal.root_task_id, 'obsolete-claim', now=now + 32) is False
        assert repo.history(job_id=job['job_id'])[0] == rows
        assert store.wakes.pending_one(signal.wake_signal_id) is None
        return
    channels.status = 'sent'
    reports = scheduler.tick(now=now + 31)
    assert len(case.calls) == len(case.processes) == 1
    retry_history = [(row['status'], row.get('delivery_status'), row.get('delivery_reason'), row.get('error_code'))
                     for row in repo.history(job_id=job['job_id'])[0]]
    assert len(channels.attempts) == 2, (retry_history, reports)
    assert channels.attempts[0][1] == channels.attempts[1][1]
    assert channels.attempts[0][0].idempotency_key == channels.attempts[1][0].idempotency_key
    assert store.tasks.load(signal.root_task_id).status == 'completed'
    assert store.wakes.pending_one(signal.wake_signal_id) is None
    rows, errors = repo.history(job_id=job['job_id'])
    assert not errors and len(rows) == 1 and rows[0]['status'] == 'done', rows
    assert rows[0]['model_call_count'] == 0
