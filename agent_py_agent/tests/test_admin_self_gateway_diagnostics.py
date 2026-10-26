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


# LLM: 原五profile及运行账保持；仅管理员两个明确日志读例外，写与队列、凭据和跨owner边界不变。
# 函数用途: 经真实工具执行器验证管理员读日志且不能写，其他用户仍拒绝托管日志。
@pytest.mark.parametrize('profile', PROFILES)
def test_self_diagnostics_keep_canonical_authority_and_original_boundaries(tmp_path, profile):
    case = diagnostic_case(tmp_path, profile)
    own = invoke(case, 'read_file', {'path': str(case.own_log)})
    assert own.result.ok and _BODY in own.result.output
    protected = invoke(case, 'read_file', {'path': str(case.paths.log)})
    if profile.startswith('admin'):
        assert protected.result.ok and _BODY in protected.result.output
        write = invoke(case, 'write_file', {'path': str(case.paths.log), 'content': 'x'})
        assert not write.result.ok and write.result.error_code == 'PATH_HOST_STATE_WRITE_BLOCKED'
        queue = case.paths.inbox / 'req.json'
        queue.parent.mkdir(parents=True, exist_ok=True)
        queue.write_text('{"synthetic":"queue-private"}', encoding='utf-8')
        queued = invoke(case, 'read_file', {'path': str(queue)})
        assert not queued.result.ok and queued.result.error_code == 'PATH_HOST_MANAGED_STORE_BLOCKED'
    else:
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


# LLM: 使用原运行身份与schema，真实读取两个合成文件；只请求full_file时允许包含以前生命周期的行。
# 函数用途: 验证双日志快照、关键词过滤、凭据脱敏及默认不返原文的兼容行为。
@pytest.mark.parametrize('profile', ['admin-full', 'admin-auto', 'admin-ask'])
def test_batch4_admin_both_log_tail_full_file_is_scrubbed_and_queryable(tmp_path, profile):
    case = diagnostic_case(tmp_path, profile)
    supervisor = case.paths.root / 'supervisor.log'
    case.paths.log.write_text('OLD-MATCH token=SYNTHETIC_TOKEN_587\nnew plain\n', encoding='utf-8')
    supervisor.write_text('OLD-MATCH Authorization: Bearer SYNTHETIC_BEARER_587\nnew plain\n', encoding='utf-8')
    for path in (case.paths.log, supervisor):
        read = invoke(case, 'read_file', {'path': str(path)})
        assert read.result.ok and 'OLD-MATCH' in read.result.output
    result = invoke(case, 'gateway_status', {'include_log_tail': True, 'log_source': 'both',
        'log_scope': 'full_file', 'log_query': 'old-match', 'log_tail_lines': 20})
    assert result.result.ok, result.result.output
    payload = json.loads(result.result.output)
    assert payload['runtime']['supervisor_log_path'] == str(supervisor)
    diag = payload['log_diagnostics']
    assert diag['raw_log_included'] is True
    assert set(diag['log_tail']) == {'gateway', 'supervisor'}
    for row in diag['log_tail'].values():
        assert row['status'] == 'ok' and row['scope'] == 'full_file'
        assert len(row['lines']) == 1 and 'OLD-MATCH' in row['lines'][0]
    assert 'SYNTHETIC_TOKEN_587' not in result.result.output
    assert 'SYNTHETIC_BEARER_587' not in result.result.output
    for params in ({'include_log_diagnostics': True}, {'include_log_diagnostics': False}, {}):
        default = invoke(case, 'gateway_status', params)
        assert default.result.ok and 'OLD-MATCH' not in default.result.output
        data = json.loads(default.result.output)
        if params.get('include_log_diagnostics', True):
            assert data['log_diagnostics']['raw_log_included'] is False
            assert 'log_tail' not in data['log_diagnostics']
        else:
            assert 'log_diagnostics' not in data


# LLM: 只使用合成真实文件与原路径策略；硬链接反例用元数据替身，不伪称真实os.link可用。
# 函数用途: 锁住精确文件名、regular形状、符号链接与单链接例外边界。
@pytest.mark.parametrize('kind', ['regular', 'directory', 'symlink_queue', 'multiple_links'])
def test_batch4_log_exception_is_exact_regular_and_not_a_queue_alias(tmp_path, monkeypatch, kind):
    from pathlib import Path

    from agent_py_agent.agent.path_access_policy import PathAccessPolicy

    root = tmp_path / 'home'
    service = root / 'owners/local/main/workspace/runtime/services/gateway'
    service.mkdir(parents=True)
    log = service / 'gateway.log'
    queue = service / 'requests/pending/req.json'
    queue.parent.mkdir(parents=True)
    queue.write_text('private-queue', encoding='utf-8')
    if kind == 'directory':
        log.mkdir()
    elif kind == 'symlink_queue':
        log.symlink_to(queue)
    else:
        log.write_text('public-service-log', encoding='utf-8')
    if kind == 'multiple_links':
        original = Path.stat

        # LLM: 仅这一个候选文件报告nlink=2，保原设备/inode及其他路径的真实stat。
        # 函数用途: 在不支持真实硬链接的环境仍核验例外拒绝多链接文件。
        def linked(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path == log:
                values = list(result)
                values[3] = 2
                return os.stat_result(values)
            return result

        monkeypatch.setattr(Path, 'stat', linked)
    policy = PathAccessPolicy.from_values(mode='full', agent_home_root=root)
    assert policy.check(log).allowed is (kind == 'regular')
    for relative in ('gateway.log.1', 'gateway_state.json', 'coordinator.lock', 'responses/answer.json'):
        path = service / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('private-control', encoding='utf-8')
        assert policy.check(path).code == 'PATH_HOST_MANAGED_STORE_BLOCKED'
    assert policy.check(queue).code == 'PATH_HOST_MANAGED_STORE_BLOCKED'
    if kind == 'regular':
        assert policy.check_write(log).code == 'PATH_HOST_STATE_WRITE_BLOCKED'


# LLM: 当前Gateway代次以原字节offset截断；supervisor本无同一offset，按本代started_at筛其带时间日志事件。
# 函数用途: 验证当前生命周期不会混入旧Gateway日志或旧监护事件，full_file仍能显式回看。
@pytest.mark.parametrize('scope', ['current_lifecycle', 'full_file'])
def test_batch4_log_tail_lifecycle_filter_has_no_cross_restart_rows(tmp_path, scope):
    from datetime import datetime

    case = diagnostic_case(tmp_path, 'admin-full')
    old = 'OLD_LIFECYCLE_587\n'
    case.paths.log.write_text(old + 'CURRENT_LIFECYCLE_587\n', encoding='utf-8')
    (case.paths.root / 'supervisor.log').write_text('[2026-10-25 03:59:59] OLD_LIFECYCLE_587\n'
        '[2026-10-25 04:00:01] CURRENT_LIFECYCLE_587\n', encoding='utf-8')
    case.paths.state.write_text(json.dumps({'started_at': datetime(2026, 10, 25, 4).timestamp(),
        'log_start_offset_bytes': len(old.encode('utf-8'))}), encoding='utf-8')
    result = invoke(case, 'gateway_status', {'include_log_tail': True, 'log_source': 'both', 'log_scope': scope})
    assert result.result.ok, result.result.output
    tails = json.loads(result.result.output)['log_diagnostics']['log_tail']
    for row in tails.values():
        assert 'CURRENT_LIFECYCLE_587' in '\n'.join(row['lines'])
        assert ('OLD_LIFECYCLE_587' in '\n'.join(row['lines'])) is (scope == 'full_file')


# LLM: 有界文件尾读不承诺全文件关键词搜索，UTF-8行与两个来源共用32KiB输入/返文预算。
# 函数用途: 验证行数夹取、字节上限和查无匹配只代表采样窗口。
@pytest.mark.parametrize('requested,limit', [(0, 1), (2, 2), (10000, 200)])
def test_batch4_log_tail_is_bounded_by_bytes_and_clamped_lines(tmp_path, requested, limit):
    case = diagnostic_case(tmp_path, 'admin-full')
    text = 'outside-bounded-window\n' + ''.join(f'匹配行-{i:04d}-' + '字' * 40 + '\n' for i in range(1000))
    case.paths.log.write_text(text, encoding='utf-8')
    (case.paths.root / 'supervisor.log').write_text(text, encoding='utf-8')
    result = invoke(case, 'gateway_status', {'include_log_tail': True, 'log_source': 'both',
        'log_scope': 'full_file', 'log_tail_lines': requested})
    assert result.result.ok, result.result.output
    tails = json.loads(result.result.output)['log_diagnostics']['log_tail']
    assert sum(row['sampled_bytes'] for row in tails.values()) <= 32768
    assert sum(len('\n'.join(row['lines']).encode('utf-8')) for row in tails.values()) <= 32768
    for row in tails.values():
        assert 0 < len(row['lines']) <= limit and row['sample_truncated'] is True
    empty = invoke(case, 'gateway_status', {'include_log_tail': True, 'log_scope': 'full_file',
        'log_query': 'outside-bounded-window'})
    assert empty.result.ok and json.loads(empty.result.output)['log_diagnostics']['log_tail']['gateway']['lines'] == []


# LLM: 元数据模式不能因显式tail参数读取日志；直接handler测试拦截底层日志读口，不靠最终投影掩盖读取。
# 函数用途: 验证metadata_only始终零日志I/O及无raw字段。
def test_batch4_metadata_only_disables_explicit_log_tail(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import status_rendering as rendering

    case = diagnostic_case(tmp_path, 'admin-full')

    # LLM: 任何实际日志诊断调用都令本例失败；原状态/队列读取保持真实。
    # 函数用途: 防止先读后删伪装成纯元数据。
    def forbidden(*args, **kwargs):
        pytest.fail('metadata_only不得读取日志')

    monkeypatch.setattr(rendering, 'gateway_log_diagnostics', forbidden)
    monkeypatch.setattr(rendering, '_gateway_log_tails', forbidden, raising=False)
    result = invoke(case, 'gateway_status', {'metadata_only': True, 'include_log_tail': True,
        'log_source': 'both', 'log_scope': 'full_file'})
    assert result.result.ok, result.result.output
    payload = json.loads(result.result.output)
    assert payload['metadata_only'] is True and 'log_diagnostics' not in payload and 'runtime' not in payload


# LLM: 新输入在handler直接调用也须严格验证；错误不回显参数或进入文件读取。
# 函数用途: 验证非法来源、范围、行数和布尔参数没有隐式放行。
@pytest.mark.parametrize('params', [{'include_log_tail': 'yes'}, {'log_source': '../queue'},
    {'log_scope': 'all_owners'}, {'log_tail_lines': True}, {'log_tail_lines': '80'}, {'log_query': []}])
def test_batch4_log_tail_rejects_invalid_arguments_without_echo(tmp_path, params):
    from agent_py_agent.agent.tooling.gateway_status import GatewayStatusTool

    case = diagnostic_case(tmp_path, 'admin-full')
    result = GatewayStatusTool(case.agent).execute(params)
    assert not result.ok and result.error_code == 'TOOL_INVALID_ARGUMENTS'


# LLM: 缺失/链接日志不降级读其他文件；原受保护队列正文绝不进入新tail出口。
# 函数用途: 验证只读尾采样对缺文件和符号链接明确报告不可读，正常来源仍返回。
@pytest.mark.parametrize('fault', ['missing', 'symlink'])
def test_batch4_tail_unavailable_source_never_reads_a_queue(tmp_path, fault):
    case = diagnostic_case(tmp_path, 'admin-full')
    if fault == 'symlink':
        queue = case.paths.inbox / 'request.json'
        queue.parent.mkdir(parents=True, exist_ok=True)
        queue.write_text('PRIVATE_QUEUE_NONCE_587', encoding='utf-8')
        (case.paths.root / 'supervisor.log').symlink_to(queue)
    result = invoke(case, 'gateway_status', {'include_log_diagnostics': False, 'include_log_tail': True,
        'log_source': 'both', 'log_scope': 'full_file'})
    assert result.result.ok, result.result.output
    payload = json.loads(result.result.output)
    tails = payload['log_diagnostics']['log_tail']
    assert tails['gateway']['status'] == 'ok' and _BODY in '\n'.join(tails['gateway']['lines'])
    assert tails['supervisor']['status'] == 'unavailable'
    assert 'PRIVATE_QUEUE_NONCE_587' not in result.result.output


# LLM: 所有密钥均合成；字节窗可能截在PEM中部或普通长行中，不能回显失去标签的凭据片段。
# 函数用途: 覆盖整块/截断私钥及首个不完整行的保守脱敏。
@pytest.mark.parametrize('shape', ['whole_key', 'clipped_key', 'clipped_line'])
def test_batch4_tail_scrubs_credentials_even_at_sample_boundaries(tmp_path, shape):
    case = diagnostic_case(tmp_path, 'admin-full')
    private = 'SYNTHETIC_PRIVATE_KEY_BODY_587'
    if shape == 'clipped_line':
        text = 'token=' + private * 2000 + '\nSAFE_END_587\n'
    else:
        text = '-----BEGIN PRIVATE KEY-----\n' + (private + '\n') * (2000 if shape == 'clipped_key' else 3)
        text += '-----END PRIVATE KEY-----\nSAFE_END_587\n'
    case.paths.log.write_text(text, encoding='utf-8')
    result = invoke(case, 'gateway_status', {'include_log_tail': True, 'log_scope': 'full_file', 'log_tail_lines': 200})
    assert result.result.ok, result.result.output
    assert private not in result.result.output and 'SAFE_END_587' in result.result.output


# LLM: 只用宿主冻结的owner根识别管理员，不让非main租户从private helper取得例外；跨owner与写门仍真实检查。
# 函数用途: 验证Full/Auto/Ask共用的管理员识别在helper内部也排除其他本地或Provider用户。
@pytest.mark.parametrize('scope', [None, 'owners/local/main', 'owners/local/other', 'owners/providers/feishu/users/other'])
def test_steer_admin_log_helper_checks_frozen_owner_scope(tmp_path, scope):
    from agent_py_agent.agent.path_access_policy import PathAccessPolicy

    home = tmp_path / 'home'
    log = home / 'owners/local/main/workspace/runtime/services/gateway/supervisor.log'
    log.parent.mkdir(parents=True)
    log.write_text('synthetic service log', encoding='utf-8')
    policy = PathAccessPolicy.from_values(mode='full', agent_home_root=home,
        owner_scope_root=home / scope if scope else None)
    admin = scope in (None, 'owners/local/main')
    assert policy._is_admin_readable_service_log(log) is admin
    assert policy.check(log).allowed is admin
    if admin:
        assert policy.check_write(log).code == 'PATH_HOST_STATE_WRITE_BLOCKED'
    else:
        assert policy.check(log).code == 'PATH_HOST_MANAGED_STORE_BLOCKED'


# LLM: 首行完整性按实际采样offset前一个字节判定，边界探针也计入同一I/O预算；两个长度覆盖对齐及截行。
# 函数用途: 防止无条件删掉完整首行，同时核实边界探针没有偷加读取字节。
@pytest.mark.parametrize('suffix', [b'z\n' * 62, b'z\n' * 61 + b'z'])
def test_review_log_tail_preserves_confirmed_first_line_within_budget(tmp_path, monkeypatch, suffix):
    from agent_py_agent.agent.gateway_parts import status_rendering as rendering

    content = b'x\nhit\n' + suffix
    path = tmp_path / 'gateway.log'
    path.write_bytes(content)
    original = os.read
    reads = []

    # LLM: 包装真实os.read，只核对本次同步采样实际读取量，不模拟文件内容或字节边界。
    # 函数用途: 让边界检查与正文共享128字节，而不是额外多读一字节。
    def counted(descriptor, count):
        data = original(descriptor, count)
        reads.append(len(data))
        return data

    monkeypatch.setattr(rendering.os, 'read', counted)
    raw, metadata = rendering._read_log_tail_sample(path, 0, 128)
    start = metadata['sample_start_offset_bytes']
    expected = content[start:]
    if start and content[start - 1:start] != b'\n':
        expected = expected.partition(b'\n')[2]
    assert raw == expected
    assert metadata['boundary_probe_bytes'] == 1
    assert metadata['sampled_bytes'] == sum(reads) <= 128


# LLM: 秒级日志不能证明启动当秒的记录先后；当前代必须报告歧义，full_file仍允许显式检查相同记录。
# 函数用途: 防止丢掉启动当秒日志后仍报ok，也不靠向下取整混入上一代。
@pytest.mark.parametrize('scope', ['current_lifecycle', 'full_file'])
def test_review_supervisor_start_second_is_explicitly_ambiguous(tmp_path, scope):
    from datetime import datetime

    case = diagnostic_case(tmp_path, 'admin-full')
    started_at = datetime(2026, 10, 25, 4).timestamp() + 0.5
    (case.paths.root / 'supervisor.log').write_text('[2026-10-25 04:00:00] AMBIGUOUS_BOUNDARY_587\n'
        'ambiguous continuation\n[2026-10-25 04:00:01] VERIFIED_CURRENT_587\n', encoding='utf-8')
    case.paths.state.write_text(json.dumps({'started_at': started_at}), encoding='utf-8')
    result = invoke(case, 'gateway_status', {'include_log_diagnostics': False, 'include_log_tail': True,
        'log_source': 'supervisor', 'log_scope': scope})
    assert result.result.ok, result.result.output
    row = json.loads(result.result.output)['log_diagnostics']['log_tail']['supervisor']
    assert 'VERIFIED_CURRENT_587' in '\n'.join(row['lines'])
    if scope == 'current_lifecycle':
        assert row['status'] == 'lifecycle_boundary_ambiguous'
        assert row['lifecycle_boundary_ambiguous_lines'] == 2
        assert row['lifecycle_timestamp_precision'] == 'seconds'
        assert 'AMBIGUOUS_BOUNDARY_587' not in '\n'.join(row['lines'])
    else:
        assert row['status'] == 'ok' and 'AMBIGUOUS_BOUNDARY_587' in '\n'.join(row['lines'])


# LLM: 所有数据为合成Base64形状，模拟尾窗两端都无PEM标记；不把无标签高熵长行当普通可返正文。
# 函数用途: 防止长私钥正文落在采样中部时失去BEGIN/END标签就未经脱敏返出。
def test_review_log_tail_scrubs_unframed_base64_lines(tmp_path):
    case = diagnostic_case(tmp_path, 'admin-full')
    body = 'QWxwaGFCZXRh' * 6
    case.paths.log.write_text(body + '\nSAFE_END_587\n', encoding='utf-8')
    result = invoke(case, 'gateway_status', {'include_log_diagnostics': False, 'include_log_tail': True,
        'log_scope': 'full_file'})
    assert result.result.ok, result.result.output
    assert body not in result.result.output and 'SAFE_END_587' in result.result.output
