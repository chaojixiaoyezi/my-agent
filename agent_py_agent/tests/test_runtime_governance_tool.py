# LLM: 临时安装/proc/systemd夹具覆盖真实工具入口；所有信号和systemctl均替换，不碰真实服务。
# 模块用途: 验证运行治理状态、双授权门、受保护PID、维护锁和服务配置。
import json
import os
import signal
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from agent_py_agent.agent.tooling import runtime_governance_tool as module
from agent_py_agent.agent.tooling.models import BaseTool, tool_effect_for_runtime_policy


# LLM: 安装标记与不可变版本按现有安装器格式构造，仅写pytest临时目录。
# 函数用途: 准备独立安装和服务工作区。
@pytest.fixture
def case(tmp_path, monkeypatch):
    prefix, workspace, proc = tmp_path / 'install', tmp_path / 'gateway', tmp_path / 'proc'
    exe = prefix / 'releases/v1/runtime/bin/python3'
    exe.parent.mkdir(parents=True)
    exe.write_text('fixture executable')
    (prefix / 'releases/v1/release.json').write_text(json.dumps({'version': 'v1'}))
    (prefix / 'current').symlink_to('releases/v1', target_is_directory=True)
    (prefix / '.my-agent-private-install.json').write_text(json.dumps({
        'schema': 'my-agent.private-release.v1', 'prefix': str(prefix), 'owner_uid': getattr(os, 'getuid', lambda: None)()}))
    workspace.mkdir()
    proc.mkdir()
    monkeypatch.setattr(module, 'PROC_ROOT', proc)
    monkeypatch.setattr(module.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path / 'user'))
    return NS(prefix=prefix, workspace=workspace, proc=proc, exe=exe, root=tmp_path)


# LLM: 身份和路径只通过宿主构造器注入，不来自工具参数。
# 函数用途: 创建可直接验收的治理工具。
def tool(case, **changes):
    return module.RuntimeGovernanceTool(gateway_workspace=case.workspace, prefix=case.prefix, **changes)


# LLM: 模拟Linux内核proc格式，含starttime与TTY字段，exe链接指向临时版本。
# 函数用途: 创建一条可扫描但不可真实发信号的进程记录。
def process(case, pid, *, tty=0, start=10, executable=None, arguments=None):
    folder = case.proc / str(pid)
    folder.mkdir(exist_ok=True)
    stat = ['S', '1', '1', '1', str(tty), *['0'] * 14, str(start), *['0'] * 30]
    (folder / 'stat').write_text(f'{pid} (fixture process) ' + ' '.join(stat))
    (folder / 'cmdline').write_bytes('\0'.join(arguments or ['python3', '-m', 'agent_py_agent']).encode() + b'\0')
    (folder / 'exe').symlink_to(executable or case.exe)
    return folder


# LLM: 只解包真实工具结果，错误不会被当成合法JSON成功。
# 函数用途: 读取成功动作的结构化输出。
def payload(result):
    assert result.ok, result.output
    return json.loads(result.output)


# LLM: pidfd用普通临时FD代替，信号/等待由边界替身记录；os.kill若被调用即失败。
# 函数用途: 安全观察TERM/KILL升级，不终止任何实际进程。
def signals(case, monkeypatch, *, timeout=False, on_open=None):
    sent = []
    # LLM: 返回真实可关闭FD，并允许在打开时注入PID重用或角色变化。
    # 函数用途: 替代内核pidfd打开。
    def open_pid(pid, flags=0):
        if on_open:
            on_open(pid)
        return os.open(case.exe, os.O_RDONLY)
    monkeypatch.setattr(module.os, 'pidfd_open', open_pid, raising=False)
    monkeypatch.setattr(module.signal, 'pidfd_send_signal', lambda fd, sig, *a: sent.append(sig), raising=False)
    waits = [([], [], []), ([1], [], [])] if timeout else [([1], [], [])]
    monkeypatch.setattr(module.select, 'select', lambda *a: waits.pop(0))
    monkeypatch.setattr(module.os, 'kill', lambda *a: pytest.fail('unsafe numeric PID signal'))
    return sent


# LLM: 状态须实际读取三层文件、严格不写盘，命令行和状态里的合成凭据都应脱敏。
# 函数用途: 验证状态、安装信息、维护锁、进程列表及普通脚本政策。
def test_status_is_read_only_and_sanitized(case, monkeypatch):
    secret = 'fixture-sensitive-value'
    for name, data in {'gateway.pid': 101, 'adapter.pid': {'pid': 102}, 'supervisor.pid': {'pid': 103},
                       'gateway_state.json': {'pid': 101, 'Authorization': 'Bearer ' + secret},
                       'gateway_heartbeat.json': {'pid': 101, 'updated_at': 10},
                       'adapter_state.json': {'pid': 102}, 'supervisor_state.json': {'pid': 103},
                       'supervisor_service.json': {'unit_name': 'my-agent-gw-supervisor.service'},
                       'maintenance_lock.json': {'reason': 'upgrade', 'expires_at': 2000}}.items():
        (case.workspace / name).write_text(json.dumps(data))
    for pid in (101, 102, 103, os.getpid()):
        process(case, pid)
    process(case, 201, arguments=['python3', 'Authorization: Bearer ' + secret])
    process(case, 202, tty=1)
    process(case, 203, executable=Path('/usr/bin/elsewhere'))
    checked = []
    monkeypatch.setattr(module.subprocess, 'run', lambda args, **kw: checked.append(args) or NS(returncode=0, stdout='active\n'))
    monkeypatch.setattr(module.time, 'time', lambda: 1000)
    before = {p: p.read_bytes() for p in case.workspace.iterdir()}
    data = payload(tool(case).execute({'action': 'status'}))
    assert data['release_info']['current_version'] == 'v1'
    assert data['release_info']['installed_versions'] == ['v1']
    assert data['layer1_os_service']['systemctl_status'] == 'active' and checked
    assert data['layer2_supervisor']['maintenance_lock_active'] is True
    assert data['layer2_supervisor']['maintenance_lock']['reason'] == 'upgrade'
    assert data['layer3_services']['gateway']['pid'] == 101 and data['layer3_services']['adapter']['pid'] == 102
    assert {row['pid'] for row in data['unmanaged_processes']} == {201, 202}
    assert next(row for row in data['unmanaged_processes'] if row['pid'] == 202)['interactive'] is True
    assert secret not in json.dumps(data)
    assert data['normal_script_policy'] == {'ad_hoc_scripts_restricted': False, 'first_class_script_scope': ['workflow', 'monitor', 'skill']}
    assert before == {p: p.read_bytes() for p in case.workspace.iterdir()}


# LLM: 缺proc或非Linux只缺失进程观察，不假报系统服务活动，也不创建文件。
# 函数用途: 验证平台降级与过期维护锁。
@pytest.mark.parametrize('platform', ['Darwin', 'Windows', 'Linux'])
def test_status_without_proc_and_expired_lock(case, monkeypatch, platform):
    monkeypatch.setattr(module.platform, 'system', lambda: platform)
    monkeypatch.setattr(module, 'PROC_ROOT', case.root / 'absent-proc')
    (case.workspace / 'maintenance_lock.json').write_text(json.dumps({'reason': 'expired', 'expires_at': 1}))
    data = payload(tool(case).execute({'action': 'status'}))
    assert data['unmanaged_processes'] == []
    assert data['layer2_supervisor']['maintenance_lock_active'] is False


# LLM: 所有写动作共享身份和严格True双门；调用体不能靠请求参数伪造管理员。
# 函数用途: 验证未授权写入在任何副作用前被拒绝。
@pytest.mark.parametrize('action', ['set_maintenance_lock', 'clear_maintenance_lock', 'terminate_orphan', 'configure_watchdog_service'])
@pytest.mark.parametrize('admin,authorized', [(False, True), (True, None), (True, False), (True, 1), (True, 'true')])
def test_write_requires_both_admin_gates(case, action, admin, authorized):
    result = tool(case, is_admin=admin, owner_id='external').execute({'action': action, 'admin_authorized': authorized})
    assert not result.ok and 'PERMISSION_DENIED' in result.output
    assert not list(case.workspace.iterdir()) and not (case.root / 'user').exists()


# LLM: 宿主显式布尔与local/admin主代理可授权，但子代理和外部home永远不能升级身份。
# 函数用途: 验证身份回退与子代理排除。
@pytest.mark.parametrize('owner_type', ['subagent', 'worker', ''])
def test_subagent_never_controls_runtime(case, owner_type):
    result = tool(case, is_admin=True, owner_id='admin', owner_type=owner_type).execute({'action': 'set_maintenance_lock', 'admin_authorized': True})
    assert not result.ok and 'PERMISSION_DENIED' in result.output


# LLM: 维护锁存储必须真实落盘并带TTL及可信actor；清理不沿链接访问其他文件。
# 函数用途: 验证维护锁设置和清除的完整入口。
def test_set_and_clear_maintenance_lock(case, monkeypatch):
    monkeypatch.setattr(module.time, 'time', lambda: 1000)
    handler = tool(case, is_admin=True, owner_id='admin')
    payload(handler.execute({'action': 'set_maintenance_lock', 'admin_authorized': True, 'reason': 'rolling update'}))
    record = json.loads((case.workspace / 'maintenance_lock.json').read_text())
    assert record['reason'] == 'rolling update' and record['expires_at'] == 2800 and record['actor'] == 'admin'
    payload(handler.execute({'action': 'clear_maintenance_lock', 'admin_authorized': True}))
    assert not (case.workspace / 'maintenance_lock.json').exists()


# LLM: 管理员确认不等于任意PID的终止许可，所有服务PID和交互进程都不可杀。
# 函数用途: 验证保护名单与真实exe归属边界。
@pytest.mark.parametrize('target', ['gateway', 'adapter', 'supervisor', 'self', 'interactive', 'outside', 'missing'])
def test_terminate_rejects_protected_targets(case, monkeypatch, target):
    ids = {'gateway': 101, 'adapter': 102, 'supervisor': 103, 'self': os.getpid(), 'interactive': 201, 'outside': 202, 'missing': 203}
    for role in ('gateway', 'adapter', 'supervisor'):
        (case.workspace / f'{role}.pid').write_text(json.dumps({'pid': ids[role]}))
    for key, pid in ids.items():
        if key != 'missing':
            process(case, pid, tty=1 if key == 'interactive' else 0, executable=Path('/usr/bin/elsewhere') if key == 'outside' else None)
    sent = signals(case, monkeypatch)
    result = tool(case, is_admin=True).execute({'action': 'terminate_orphan', 'admin_authorized': True, 'target_pid': ids[target]})
    assert not result.ok and not sent


# LLM: 只允许核验非交互发行进程，使用准确pidfd发送，TERM未退出时才升级KILL。
# 函数用途: 验证两个终止分支及结构化确认。
@pytest.mark.parametrize('timeout', [False, True])
def test_terminate_verified_orphan(case, monkeypatch, timeout):
    process(case, 201)
    sent = signals(case, monkeypatch, timeout=timeout)
    data = payload(tool(case, is_admin=True).execute({'action': 'terminate_orphan', 'admin_authorized': True, 'target_pid': 201}))
    assert data['terminated'] is True and data['target_pid'] == 201
    assert sent == ([signal.SIGTERM, signal.SIGKILL] if timeout else [signal.SIGTERM])


# LLM: 打开pidfd期间进程出生或服务身份变化都必须在首个信号前拒绝。
# 函数用途: 验证PID重用和新服务PID不会被误杀。
@pytest.mark.parametrize('change', ['reused', 'protected'])
def test_terminate_rechecks_identity(case, monkeypatch, change):
    folder = process(case, 201)
    # LLM: 在原目标完成首次核验之后改变内核或服务夹具，不替换被测控制逻辑。
    # 函数用途: 注入危险的观察窗口变化。
    def mutate(pid):
        if change == 'reused':
            stat = (folder / 'stat').read_text().split(') ', 1)[1].split()
            stat[19] = '11'
            (folder / 'stat').write_text(f'{pid} (fixture process) ' + ' '.join(stat))
        else:
            (case.workspace / 'gateway.pid').write_text(str(pid))
    sent = signals(case, monkeypatch, on_open=mutate)
    result = tool(case, is_admin=True).execute({'action': 'terminate_orphan', 'admin_authorized': True, 'target_pid': 201})
    assert not result.ok and sent == []


# LLM: 配置只写临时用户unit和服务登记，不调用enable/start或重启任何真实服务。
# 函数用途: 验证稳定启动器、回退解释器与参数转义。
@pytest.mark.parametrize('stable', [True, False])
def test_configure_watchdog_service(case, stable):
    if stable:
        launcher = case.prefix / 'bin/my-agent'
        launcher.parent.mkdir()
        launcher.write_text('fixture launcher')
    handler = tool(case, is_admin=True, owner_id='local')
    data = payload(handler.execute({'action': 'configure_watchdog_service', 'admin_authorized': True}))
    unit = case.root / 'user/.config/systemd/user/my-agent-gw-supervisor.service'
    text = unit.read_text()
    record = json.loads((case.workspace / 'supervisor_service.json').read_text())
    assert 'ExecStart=' in text and 'gateway' in text and 'supervisor' in text and 'Restart=always' in text
    assert record['unit_name'] == unit.name and data['activated'] is False
    assert (str(case.prefix / 'bin/my-agent') in text) if stable else ('-m' in text and 'agent_py_agent' in text)


# LLM: 模型不能用unit名称注入路径或额外systemd指令；坏TTL也不能写锁。
# 函数用途: 验证写入参数在落盘前被拒绝。
@pytest.mark.parametrize('values', [dict(action='configure_watchdog_service', unit_name='../escape.service'),
    dict(action='configure_watchdog_service', unit_name='safe.service\nExecStart=bad'),
    dict(action='set_maintenance_lock', ttl_seconds=0), dict(action='set_maintenance_lock', ttl_seconds=True)])
def test_invalid_write_parameters_have_no_effect(case, values):
    result = tool(case, is_admin=True).execute({**values, 'admin_authorized': True})
    assert not result.ok and not list(case.workspace.iterdir()) and not (case.root / 'user').exists()


# LLM: 文件系统链接或损坏状态不能变成正常运行事实，更不能成为PID终止许可。
# 函数用途: 验证维护锁nofollow及损坏服务记录拒绝终止。
def test_symlink_and_corrupt_records_fail_closed(case, monkeypatch):
    outside = case.root / 'outside.json'
    outside.write_text('{"reason":"private-value","expires_at":999999999999}')
    (case.workspace / 'maintenance_lock.json').symlink_to(outside)
    handler = tool(case, is_admin=True)
    result = handler.execute({'action': 'set_maintenance_lock', 'admin_authorized': True})
    assert not result.ok and 'private-value' in outside.read_text()
    result = handler.execute({'action': 'clear_maintenance_lock', 'admin_authorized': True})
    assert not result.ok and outside.exists()
    (case.workspace / 'gateway.pid').write_text('broken')
    process(case, 201)
    sent = signals(case, monkeypatch)
    result = handler.execute({'action': 'terminate_orphan', 'admin_authorized': True, 'target_pid': 201})
    assert not result.ok and not sent


# LLM: 使用真实bootstrap但关闭自我目录；普通脚本/工作流照常注册，治理仅注册本机管理员，构造不能读运行状态。
# 函数用途: 验证装配接线和常规脚本能力不受限制。
@pytest.mark.parametrize('admin', [True, False])
def test_bootstrap_registers_only_admin_governance(case, admin):
    from agent_py_agent.agent.agent_core.self_map_bootstrap import register_self_map
    registered = []
    agent = NS(root=case.root, config=NS(enable_self_map=False),
               tools=NS(owner_type='main_agent', owner_id='local' if admin else 'external', register=registered.append),
               home_paths=NS(owner_home_dir=case.root, owner_provider='local' if admin else 'feishu', owner_kind='main' if admin else 'user'))
    register_self_map(agent)
    names = {item.model_spec.name for item in registered}
    assert {'scripts', 'workflows'} <= names
    assert ('runtime_governance' in names) is admin
    assert not list(case.workspace.iterdir())


# LLM: 原执行器必须把status判只读，所有控制判危险，仍走宿主审批，参数确认不能代替审批。
# 函数用途: 验证治理工具沿既有执行合同而非旁路。
def test_tool_contract_retains_host_approval(case):
    handler = tool(case)
    assert isinstance(handler, BaseTool) and handler.model_spec.name == 'runtime_governance'
    assert tool_effect_for_runtime_policy(handler.runtime_policy, {'action': 'status'}) == 'read_only'
    for action in ('set_maintenance_lock', 'clear_maintenance_lock', 'terminate_orphan', 'configure_watchdog_service'):
        assert tool_effect_for_runtime_policy(handler.runtime_policy, {'action': action}) == 'dangerous'
    assert handler.runtime_policy.approval_policy.mode == 'always'


# LLM: 没有控制终端但stdin仍连PTY的TUI也属于交互进程，不能当孤儿终止。
# 函数用途: 验证TTY=0时仍保护交互输入。
def test_terminate_protects_terminal_stdin(case, monkeypatch):
    folder = process(case, 201)
    (folder / 'fd').mkdir()
    (folder / 'fd/0').symlink_to('/dev/pts/9')
    sent = signals(case, monkeypatch)
    result = tool(case, is_admin=True).execute({'action': 'terminate_orphan', 'admin_authorized': True, 'target_pid': 201})
    assert not result.ok and not sent


# LLM: releases本身不能是任意外部目录别名；路径归属不能通过resolve扩大为系统Python目录。
# 函数用途: 验证安装目录软链不会授予外部进程终止权。
def test_release_symlink_never_authorizes_external_process(case, monkeypatch):
    outside = case.root / 'unrelated'
    outside.mkdir()
    exe = outside / 'python3'
    exe.write_text('unrelated executable')
    (case.prefix / 'releases').rename(case.prefix / 'original-releases')
    (case.prefix / 'releases').symlink_to(outside, target_is_directory=True)
    process(case, 201, executable=exe)
    case.exe = exe
    sent = signals(case, monkeypatch)
    result = tool(case, is_admin=True).execute({'action': 'terminate_orphan', 'admin_authorized': True, 'target_pid': 201})
    assert not result.ok and sent == []


# LLM: 合法JSON但非对象的服务记录不是可用配置；状态动作应保留未知观察，不抛异常或误报active。
# 函数用途: 验证坏服务登记安全降级。
def test_nonobject_service_record_is_unknown(case):
    (case.workspace / 'supervisor_service.json').write_text('1')
    data = payload(tool(case).execute({'action': 'status'}))
    assert data['layer1_os_service']['systemctl_status'] == 'unknown'


# LLM: JSON类型损坏的PID记录不能使已登记服务跌出保护名单。
# 函数用途: 验证服务PID为字符串时保守拒绝终止。
def test_invalid_pid_record_prevents_termination(case, monkeypatch):
    (case.workspace / 'gateway.pid').write_text('{"pid":"201"}')
    process(case, 201)
    sent = signals(case, monkeypatch)
    result = tool(case, is_admin=True).execute({'action': 'terminate_orphan', 'admin_authorized': True, 'target_pid': 201})
    assert not result.ok and not sent


# LLM: 服务管理器不继承交互shell的MY_AGENT_HOME或cwd；fallback必须保留原配置与Home绑定，不能启动第二实例。
# 函数用途: 验证回退服务启动保留当前实例语义及systemd转义。
def test_service_fallback_preserves_home_and_relative_config(case):
    project = case.root / 'project %$ spaces'
    project.mkdir()
    home = case.root / 'original-home'
    agent = NS(root=project, config=NS(config_path='settings.yaml'),
               tools=NS(owner_type='main_agent', owner_id='local'),
               home_paths=NS(root=home, owner_home_dir=home, owner_provider='local', owner_kind='main'))
    data = payload(tool(case, agent=agent, is_admin=True).execute({'action': 'configure_watchdog_service', 'admin_authorized': True}))
    text = Path(data['unit_path']).read_text()
    assert str(project / 'settings.yaml') in data['command']
    assert 'MY_AGENT_HOME=' + str(home) in text
    assert '%%$$' in text


# LLM: 不以伪造上下文替代Registry实际冻结快照；只写pytest隔离home里的维护锁，不碰宿主服务。
# 函数用途: 验证真实Agent装配后的工具可沿正式调用上下文执行授权控制。
def test_real_agent_snapshot_allows_admin_control(case, monkeypatch):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.tooling.models import ToolInvocationContext
    home = case.root / 'real-home'
    monkeypatch.setenv('MY_AGENT_HOME', str(home))
    agent = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(home), execution_mode='local_unmanaged'), case.root / 'real-project')
    handler = agent.tools.tools['runtime_governance']
    data = payload(handler.execute_scoped({'action': 'set_maintenance_lock', 'admin_authorized': True}, ToolInvocationContext(agent.tools.runtime_snapshot())))
    assert data['maintenance_lock_active'] is True


# LLM: 原始PERMISSION_DENIED必须保留，内部分类也须沿宿主已知权限错误合同，不能退化UNKNOWN_ERROR。
# 函数用途: 验证权限拒绝是结构化权限事实。
def test_permission_denial_has_known_runtime_contract(case):
    result = tool(case).execute({'action': 'clear_maintenance_lock', 'admin_authorized': True})
    assert not result.ok and result.error_code != 'UNKNOWN_ERROR'
    assert result.reported_error_code == 'PERMISSION_DENIED'


# LLM: 通过独立解释器禁用site，排除开发机editable安装兜底；只查询模块spec，不启动服务。
# 函数用途: 验证fallback unit在自己的工作目录仍能导入当前源码包。
def test_fallback_unit_preserves_source_import_root(case):
    handler = tool(case, is_admin=True)
    data = payload(handler.execute({'action': 'configure_watchdog_service', 'admin_authorized': True}))
    text = Path(data['unit_path']).read_text()
    working = json.loads(next(line.split('=', 1)[1] for line in text.splitlines() if line.startswith('WorkingDirectory=')))
    Path(working).mkdir(parents=True, exist_ok=True)
    observed = module.subprocess.run([module.sys.executable, '-S', '-B', '-c',
        'import importlib.util,json; s=importlib.util.find_spec("agent_py_agent"); print(json.dumps(s.origin if s else None))'],
        cwd=working, capture_output=True, text=True, timeout=10, check=True)
    assert json.loads(observed.stdout) == str(Path(module.__file__).resolve().parents[2] / '__init__.py')


# LLM: 损坏维护锁不得摧毁其他层的只读状态，超大JSON整数应标未知而非转换浮点溢出。
# 函数用途: 验证异常时间戳的局部安全降级。
def test_huge_lock_timestamp_does_not_break_status(case):
    (case.workspace / 'maintenance_lock.json').write_text(json.dumps({'expires_at': 10**400}))
    data = payload(tool(case).execute({'action': 'status'}))
    assert data['layer2_supervisor']['maintenance_lock_active'] is None
    assert data['release_info']['current_version'] == 'v1'


# LLM: 原管理员对象保持不变，分别由真实子代理快照/ContextVar收紧权限，所有动作须在IO前拒绝。
# 函数用途: 验证继承父工具对象也不能控制运行时。
@pytest.mark.parametrize('mode', ['snapshot', 'current_context'])
@pytest.mark.parametrize('action', ['set_maintenance_lock', 'clear_maintenance_lock', 'terminate_orphan', 'configure_watchdog_service'])
def test_inherited_parent_tool_rejects_child_context(case, monkeypatch, mode, action):
    from dataclasses import replace

    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.runtime_context import (
        restore_current_subagent_context,
        set_current_subagent_context,
    )
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.tooling.models import ToolInvocationContext
    home = case.root / 'inherited-home'
    monkeypatch.setenv('MY_AGENT_HOME', str(home))
    agent = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(home), execution_mode='local_unmanaged'), case.root / 'project')
    handler = agent.tools.tools['runtime_governance']
    snapshot = agent.tools.runtime_snapshot()
    if mode == 'snapshot':
        snapshot = replace(snapshot, owner_type='subagent', snapshot_hash='')
    previous = set_current_subagent_context(agent, run_id='review-child') if mode == 'current_context' else None
    try:
        result = handler.execute_scoped({'action': action, 'admin_authorized': True, 'target_pid': 201}, ToolInvocationContext(snapshot))
        assert not result.ok and 'PERMISSION_DENIED' in result.output
        assert not (handler.workspace / 'maintenance_lock.json').exists()
        assert not (case.root / 'user').exists()
    finally:
        if mode == 'current_context':
            restore_current_subagent_context(agent, previous)
