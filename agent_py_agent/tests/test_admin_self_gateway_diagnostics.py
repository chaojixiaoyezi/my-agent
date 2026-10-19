# LLM: 路径/运行/claim均在临时Home真实创建；合成stdlib命令只选原sandbox可见解释器，不启动Gateway/模型/网络或替换授权门。
# 模块用途: 经原生产SimpleAgent和ToolExecutor验证管理员诊断入口与文件/Shell边界。
import json
import os
import shlex
import shutil
import sys
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.agent_core.runtime_mixin import _bind_main_agent_authority
from agent_py_agent.agent.agent_core.tool_runtime_ledger import write_boundary_with_runtime_ledger
from agent_py_agent.agent.conversation.background_execution import _BackgroundRunControl
from agent_py_agent.agent.conversation.claim_execution_binding import (
    ClaimExecutionBinding,
    bind_claim_execution,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.admin_password import admin_password_path
from agent_py_agent.agent.user_space.approval_mode import execute_approval_mode_operation
from agent_py_agent.agent.user_space.home_layout import MyAgentHomePaths
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home
from agent_py_agent.tests._tool_runtime_harness import canonical_test_call

PROFILES = ['admin-full', 'admin-auto', 'admin-ask', 'ordinary', 'group']
_BODY = 'SYNTHETIC_SELF_LOG_587'


# LLM: 不构造权限鸭子或直接赋RuntimeDB；真实Home、task、claim、原binder同源发布main attempt；所有测试资料仅写临时home。
# 函数用途: 准备合成本地日志场景，保留完整原执行身份与当前模式。
def diagnostic_case(tmp_path, profile):
    root = tmp_path / 'home'
    provider, kind, owner = ('local', 'main', 'main') if profile.startswith('admin') else ('feishu', 'group' if profile == 'group' else 'user', 'diagnostic-room' if profile == 'group' else 'diagnostic-user')
    full = profile == 'admin-full'
    agent = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(root),
        my_agent_owner_provider=provider, my_agent_owner_kind=kind, my_agent_owner_id=owner,
        access_mode='full-access' if full else 'workspace-write', path_access_mode='full' if full else 'normal',
        enable_plugins=False, tool_vector_search_enabled=False), tmp_path / 'business')
    mode = 'full-access' if full else 'ask' if profile == 'admin-ask' else 'auto'
    if profile != 'group':
        execute_approval_mode_operation(agent.home_paths, 'set', mode)
    assert isinstance(agent.home_paths, MyAgentHomePaths)
    store = agent.conversation_store
    thread = store.threads.get_or_create({'canonical_user_id': 'diagnostic-user', 'owner_id': agent.home_paths.owner_id,
        'channel': 'chat', 'channel_conversation_id': 'diagnostic-chat', 'channel_user_id': 'diagnostic-user'})
    task_id = 'diagnostic-task'
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': task_id, 'goal': '合成自身日志诊断', 'status': 'active'})
    store.tasks.select_workspace_task({'thread_id': thread.thread_id, 'task_id': task_id})
    claim = store.claims.acquire({'thread_id': thread.thread_id, 'task_id': task_id, 'reason': 'thread_goal_continue'})
    control = _BackgroundRunControl('diagnostic-request', None)
    with bind_claim_execution(ClaimExecutionBinding(store.claims, thread.thread_id, claim['claim_id'], '')):
        params = _bind_main_agent_authority(agent, RunParams(request_id='diagnostic-request', run_id='diagnostic-core',
            task_id=task_id, task_attributes={'conversation_thread_id': thread.thread_id, 'conversation_task_id': task_id},
            conversation_task_binding_callback=control))
    agent._current_run_params = params
    repo = agent.subagents.runtime_db
    assert repo is not None and type(params) is RunParams and params.attempt_id
    main = repo.agent_run_for_run_id(params.run_id)
    assert main['role'] == 'main' and main['current_attempt_id'] == params.attempt_id
    assert repo.db_path == runtime_db_path(agent.home_paths.owner_home_dir)
    assert repo.task_id_for_run_id(params.run_id) == task_id
    current_claim = store.claims.load(thread.thread_id)
    assert current_claim['claim_id'] == claim['claim_id'] and current_claim['status'] == 'running'
    assert current_claim['runtime_authority']['attempt_id'] == params.attempt_id
    assert current_claim['runtime_authority']['run_id'] == params.run_id
    assert current_claim['runtime_authority']['task_id'] == task_id
    own_log = agent.home_paths.owner_logs_dir / 'gateway.log'
    own_log.parent.mkdir(parents=True, exist_ok=True)
    own_log.write_text(_BODY + '\n')
    paths = gateway_paths(agent)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.log.write_text('RuntimeError: ' + _BODY + '\n')
    foreign = ensure_owner_home(root, OwnerIdentity.provider_user('feishu', 'other-user')).home_dir / 'private-note.txt'
    foreign.write_text('SYNTHETIC_FOREIGN_587')
    credential = admin_password_path(agent.home_paths.root)
    credential.parent.mkdir(parents=True, exist_ok=True)
    credential.write_text('{"synthetic":"SYNTHETIC_CREDENTIAL_587"}')
    return SimpleNamespace(agent=agent, params=params, claim=claim, store=store, thread=thread,
                           own_log=own_log, paths=paths, foreign=foreign, credential=credential)


# LLM: 原registry只组装原ToolExecutorRequest，调用使用canonical schema/attempt及真实运行账，不跳过prehandler或操作claim。
# 函数用途: 发出一个明确的只读工具调用，返回真实裁决和结果。
def invoke(case, tool, arguments):
    registry, params = case.agent.tools, case.params
    snapshot = registry.runtime_snapshot(run_id=params.run_id)
    call = canonical_test_call(snapshot, tool, arguments, attempt_id=params.attempt_id)
    boundary = write_boundary_with_runtime_ledger(case.agent, params)
    return registry.execute_tool(call, write_boundary=boundary, runtime_snapshot=snapshot)


@pytest.mark.parametrize('profile', PROFILES)
def test_self_diagnostics_keep_canonical_authority_and_original_boundaries(tmp_path, profile):
    case = diagnostic_case(tmp_path, profile)
    own = invoke(case, 'read_file', {'path': str(case.own_log)})
    assert own.result.ok and _BODY in own.result.output
    protected = invoke(case, 'read_file', {'path': str(case.paths.log)})
    assert not protected.result.ok and _BODY not in protected.result.output
    assert protected.result.error_code == 'PATH_HOST_MANAGED_STORE_BLOCKED'
    secret = invoke(case, 'read_file', {'path': str(case.credential)})
    assert not secret.result.ok and 'SYNTHETIC_CREDENTIAL_587' not in secret.result.output
    if profile != 'admin-full':
        foreign = invoke(case, 'read_file', {'path': str(case.foreign)})
        assert not foreign.result.ok and 'SYNTHETIC_FOREIGN_587' not in foreign.result.output
    if profile.startswith('admin'):
        status = invoke(case, 'gateway_status', {'include_log_diagnostics': True})
        assert status.result.ok
        data = json.loads(status.result.output)
        assert data['schema'] == 'gateway_runtime_snapshot.v2'
        assert data['log_diagnostics']['status'] == 'noise_observed'
        assert data['log_diagnostics']['exception_counts'] == {'RuntimeError': 1}
        assert data['log_diagnostics']['raw_log_included'] is False
        assert _BODY not in status.result.output
        assert data['alive'] is False
        metadata = invoke(case, 'gateway_status', {'metadata_only': True})
        assert metadata.result.ok and _BODY not in metadata.result.output
    else:
        assert 'gateway_status' not in case.agent.tools.tools


# LLM: 仅合成纯stdlib Shell测试选择原sandbox系统读根中的真实解释器；不授予/build读取权，不替代父进程pyc业务导入或放宽任何profile。
# 函数用途: Linux包内sys.executable可能在owner墙外，用系统默认搜索路径的Python读取合成日志；其它平台保原解释器，缺依赖明确失败。
def _sandbox_stdlib_python() -> str:
    if sys.platform != 'linux':
        return sys.executable
    executable = shutil.which('python3', path=os.defpath)
    if executable is None:
        raise RuntimeError('合成Shell只读测试需要原系统可见路径中的Python3，不能回退沙箱外的包解释器')
    return executable


# LLM: 沿原五profile的真实工具/运行账和原权限读取own日志，解释器选择不改变允许根或业务导入来源。
# 函数用途: 验自身合成日志原本可读，避免将包解释器不可见误判为普通用户读权限缺陷。
@pytest.mark.parametrize('profile', PROFILES)
def test_real_shell_can_read_only_synthetic_own_log_under_original_mode(tmp_path, profile):
    case = diagnostic_case(tmp_path, profile)
    command = shlex.join([_sandbox_stdlib_python(), '-c', 'from pathlib import Path; import sys; print(Path(sys.argv[1]).read_text())', str(case.own_log)])
    result = invoke(case, 'run_command', {'command': command})
    assert result.result.ok and _BODY in result.result.output


# LLM: 同一可见stdlib解释器仍必须被原托管日志门拒绝；不能靠解释器本身启动失败凑出安全负例。
# 函数用途: 保持Full Access及普通管理员对Gateway托管日志的真实拒读断言。
@pytest.mark.parametrize('profile', ['admin-full', 'admin-auto'])
def test_real_shell_does_not_read_managed_gateway_queue_log(tmp_path, profile):
    case = diagnostic_case(tmp_path, profile)
    command = shlex.join([_sandbox_stdlib_python(), '-c', 'from pathlib import Path; import sys; print(Path(sys.argv[1]).read_text())', str(case.paths.log)])
    result = invoke(case, 'run_command', {'command': command})
    assert not result.result.ok and _BODY not in result.result.output


@pytest.mark.parametrize('query', ['gateway_status', '网关日志'])
def test_admin_can_discover_existing_gateway_diagnostic_tool(tmp_path, query):
    case = diagnostic_case(tmp_path, 'admin-full')
    result = invoke(case, 'tool_search', {'query': query, 'limit': 5})
    assert result.result.ok
    data = json.loads(result.result.output)
    found = next(tool for tool in data['tools'] if tool['name'] == 'gateway_status')
    assert '日志' in found['description'] and 'gateway_status' in data['loaded_for_next_model_call']


@pytest.mark.parametrize('resource', ['owner_home', 'owner_directory_0'])
def test_managed_admin_tool_reports_native_resource_failure_without_raw(tmp_path, resource):
    import io
    from contextlib import redirect_stderr

    from agent_py_agent.agent.gateway_parts.logging import report_native_transport_diagnostic
    from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap_resources import (
        WorkerSharedResourceError,
    )

    case = diagnostic_case(tmp_path, 'admin-full')
    out = io.StringIO()
    with redirect_stderr(out):
        report_native_transport_diagnostic('dispatch', WorkerSharedResourceError('resource_missing', resource),
            op='materialize', seq=1, request_id='SYNTHETIC_PRIVATE_REQUEST_587')
    case.paths.log.write_text(out.getvalue())
    result = invoke(case, 'gateway_status', {'include_log_diagnostics': True})
    assert result.result.ok
    payload = json.loads(result.result.output)
    assert payload['log_diagnostics']['status'] == 'noise_observed'
    row = payload['log_diagnostics']['native_diagnostics'][0]
    assert row['stage'] == 'dispatch' and row['op'] == 'materialize'
    assert row['error_code'] == 'WORKER_SHARED_RESOURCE_INVALID'
    assert row['reason'] == 'resource_missing' and row['field'] == resource
    assert 'SYNTHETIC_PRIVATE_REQUEST_587' not in result.result.output
    assert set(row) == {'stage', 'op', 'error_type', 'error_code', 'reason', 'field'}
