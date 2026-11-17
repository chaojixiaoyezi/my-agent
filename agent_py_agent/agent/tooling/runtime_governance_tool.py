# LLM: 管理员治理仅观察原服务记录并执行逐次显式授权动作；不接管调度、不限制普通脚本，不以PID文件冒充进程生年代。
# 模块用途: 查询三层看门狗、发行版本和未受管进程，安全管理维护锁、孤儿进程与用户级服务配置。
from __future__ import annotations

import json
import math
import os
import platform
import re
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

from ..common.nofollow_fs import (
    ensure_private_dir,
    read_text_beneath,
    unlink_file_beneath,
    write_text_atomic_beneath,
)
from ..runtime_context import current_subagent_run_id
from ..user_space.approval_mode import is_permission_admin
from ..user_space.runtime_paths import runtime_paths_for_agent
from ..workflows.models import redact_bearer_values
from .input_schema import validate_tool_input
from .models import (
    ApprovalPolicy,
    BaseTool,
    ConcurrencyPolicy,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    SandboxPolicy,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)

PROC_ROOT = Path('/proc')
TOOL_NAME = 'runtime_governance'
WRITES = ('set_maintenance_lock', 'clear_maintenance_lock', 'terminate_orphan', 'configure_watchdog_service', 'reconcile_apply',
          'quarantined_wakes_dismiss', 'quarantined_wakes_replay')
DENIED = 'PERMISSION_DENIED: Watchdog and runtime control actions require admin identity and explicit admin_authorized=True.'
MARKER = '.my-agent-private-install.json'
DEFAULT_UNIT = 'my-agent-gw-supervisor.service'
SERVICE_FILES = ('gateway.pid', 'gateway_state.json', 'gateway_heartbeat.json', 'adapter.pid', 'adapter_state.json',
                 'supervisor.pid', 'supervisor_state.json')


# LLM: 文件缺失与读取损坏分开；nofollow禁止链接外读，错误只公开异常类型，不包含文件正文。
# 函数用途: 只读加载一个固定名字的JSON或数字PID记录。
def _read(root, name):
    if root is None:
        return None
    try:
        text = read_text_beneath(root, (name,))
        if text is None:
            return None
        data = json.loads(text)
        if name.endswith('.pid') and _pid(data) is None:
            raise ValueError('invalid PID record')
        return data
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError) as exc:
        return {'read_error': type(exc).__name__}


# LLM: 只操作宿主绑定根以下的普通文件，准备缺失私有目录；不绕过宿主审批或跟随叶子链接。
# 函数用途: 原子保存治理状态JSON或unit文本。
def _write(root, name, data):
    if root is None:
        raise ValueError('缺少宿主Gateway工作区')
    ensure_private_dir(root, canonical_root=root.anchor)
    text = data if isinstance(data, str) else json.dumps(redact_bearer_values(data), ensure_ascii=False, allow_nan=False)
    write_text_atomic_beneath(root, (name,), text)


_reconcile_cursor_keys = frozenset(('phase', 'after', 'created_at', 'owner', 'signature'))
_reconcile_cursor_phases = frozenset(('capabilities', 'task_runs', 'indexes', 'projections'))
_wake_cursor_keys = frozenset(('binding', 'unreadable_seen', 'phase', 'scan', 'pending', 'complete'))


# LLM: 续页游标里的64位摘要只是owner/过滤/代次的绑定值，不是凭据；仅当整体符合两种已知游标文法、且除绑定摘要外其余字段
#   脱敏前后不变时才原样放行，其它形状照常脱敏。
# 函数用途: 判断next_cursor能否免于脱敏原样返回。
def _exempt_cursor(value):
    import base64

    if not isinstance(value, str) or not value or len(value) > 65536:
        return None
    try:
        if value.startswith('{'):
            token, reconcile = json.loads(value), True
        else:
            # 对账游标与唤醒游标都以 base64url 输出；按解出的 phase 选择各自文法。
            token = json.loads(base64.urlsafe_b64decode(value))
            reconcile = isinstance(token, dict) and token.get('phase') in _reconcile_cursor_phases
        keys, digest = (_reconcile_cursor_keys, 'signature') if reconcile else (_wake_cursor_keys, 'binding')
        phases = _reconcile_cursor_phases if reconcile else ('quarantine', 'unreadable')
        valid = isinstance(token, dict) and token.get('phase') in phases
    except (ValueError, TypeError):
        return None
    if not valid or not set(token) <= keys or not re.fullmatch(r'[0-9a-f]{64}', str(token.get(digest) or '')):
        return None
    rest = {key: item for key, item in token.items() if key != digest}
    return value if redact_bearer_values(rest) == rest else None


# LLM: 所有公开字段走工作流共用的递归脱敏；宿主结果使用现有ToolHandlerOutcome，不引入平行ToolResult协议。
#   唯一例外是通过文法核验的next_cursor（见 _exempt_cursor），否则第二页永远验不过绑定摘要。
# 函数用途: 生成结构化治理回执和明确失败状态。
def _reply(data, error='', *, effect='not_started'):
    cursor = _exempt_cursor(data.get('next_cursor')) if isinstance(data, dict) else None
    data = redact_bearer_values(data)
    if cursor is not None:
        data['next_cursor'] = cursor
    return ToolHandlerOutcome(TOOL_NAME, not error, json.dumps(data, ensure_ascii=False),
                              error_code='ADMIN_CONTROL_DENIED' if error == 'PERMISSION_DENIED' else error, reported_error_code=error,
                              effect_outcome=effect if error else '', result_envelope={TOOL_NAME: data})


# LLM: 单元名称只能是一个.service文件名，既用于systemctl参数也用于写入，不能注入路径或systemd指令。
# 函数用途: 校验用户级服务名称。
def _unit_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}\.service', value):
        raise ValueError('unit_name必须为单段安全.service名称')
    return value


# LLM: 发行只从当前解释器/模块祖先或宿主显式prefix发现，不扫业务目录；标记须匹配安装器schema、真实prefix及系统账户。
# 函数用途: 找到当前私有发行的可信安装目录。
def _installation(explicit=None):
    candidates = [Path(explicit)] if explicit is not None else [*Path(sys.executable).resolve().parents, *Path(__file__).resolve().parents]
    for path in candidates:
        path = path.absolute()
        marker = _read(path, MARKER)
        if (isinstance(marker, dict) and marker.get('schema') == 'my-agent.private-release.v1'
                and marker.get('prefix') == str(path) and marker.get('owner_uid') == getattr(os, 'getuid', lambda: None)()
                and not (path / 'releases').is_symlink()):
            return path
    return None


# LLM: current是安装器允许的版本链接，必须仍落在本安装releases内；这是磁盘选择版本，不宣称所有进程已切换。
# 函数用途: 只读汇总安装前缀、当前版本及已安装版本。
def _release_info(prefix):
    if prefix is None:
        return {'prefix': None, 'current_version': None, 'installed_versions': [], 'private_release': False}
    releases = prefix / 'releases'
    current = (prefix / 'current').resolve()
    data = _read(current, 'release.json') if current.is_relative_to(releases) else None
    versions = sorted(p.name for p in releases.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith('.')) if releases.is_dir() else []
    return {'prefix': str(prefix), 'current_version': data.get('version') if isinstance(data, dict) else None,
            'installed_versions': versions, 'private_release': True}


# LLM: PID只用于保守排除服务，不能授予终止权；兼容数字文件及结构化进程记录，不接受布尔。
# 函数用途: 从服务记录提取正整数PID。
def _pid(value):
    value = value.get('pid') if isinstance(value, dict) else value
    return value if type(value) is int and value > 0 else None


# LLM: 系统服务状态只认有界systemctl返回，不启用/重启/安装服务；缺工具或错误必须显式未知。
# 函数用途: 查询当前用户服务活动状态。
def _os_service(service):
    result = {'service': service, 'systemctl_status': 'not_observed'}
    if platform.system() != 'Linux' or not service:
        return result
    if not isinstance(service, dict) or service.get('read_error'):
        return {**result, 'systemctl_status': 'unknown', 'observation_error': 'invalid_service_record'}
    try:
        unit = _unit_name(service.get('unit_name', DEFAULT_UNIT))
        observed = subprocess.run(['systemctl', '--user', 'is-active', unit], capture_output=True, text=True, timeout=2, check=False)
        result.update(systemctl_status=observed.stdout.strip() or 'unknown', return_code=observed.returncode)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result.update(systemctl_status='unknown', observation_error=type(exc).__name__)
    return result


# LLM: 命令行仅在已匹配发行进程及其直接父进程读取，限制长度，最后由统一回执递归脱敏。
# 函数用途: 读取proc命令行作管理员观察材料。
def _cmdline(pid):
    try:
        with (PROC_ROOT / str(pid) / 'cmdline').open('rb') as stream:
            return stream.read(65536).replace(b'\0', b' ').decode('utf-8', errors='replace').strip()
    except OSError:
        return ''


# LLM: 身份取内核exe/starttime/UID/TTY；缺记录、退出或未知身份均不列为可控制对象，不按进程名字扩大范围。
# 函数用途: 读取一个本账户、本安装发行进程的快照。
def _process(prefix, pid):
    try:
        folder = PROC_ROOT / str(pid)
        if folder.stat().st_uid != getattr(os, 'geteuid', lambda: folder.stat().st_uid)():
            return None
        executable = Path(os.readlink(folder / 'exe').removesuffix(' (deleted)')).resolve()
        if (prefix / 'releases').is_symlink() or not executable.is_relative_to(prefix / 'releases'):
            return None
        fields = (folder / 'stat').read_text().rsplit(') ', 1)[1].split()
        if fields[0] == 'Z':
            return None
        parent, tty, started = int(fields[1]), int(fields[4]), int(fields[19])
        command = _cmdline(pid)
        try:
            stdin = os.readlink(folder / 'fd/0')
        except FileNotFoundError:
            stdin = ''
        interactive = (tty != 0 or stdin.startswith(('/dev/pts/', '/dev/tty', '/dev/console'))
                       or any(arg in {'tui', '--tui'} for arg in command.split()))
        return dict(pid=pid, ppid=parent, interactive=interactive, cmdline=command, parent_cmdline=_cmdline(parent),
                    _identity=(str(executable), started, parent, tty))
    except (OSError, ValueError, IndexError):
        return None


# LLM: 扫描只在Linux/proc存在且安装已核验时进行；进程瞬时消失可跳过，未覆盖平台不推断没有孤儿。
# 函数用途: 列举本安装中未被服务PID记录管理的进程。
def _unmanaged(prefix, protected):
    if platform.system() != 'Linux' or prefix is None or not PROC_ROOT.is_dir():
        return []
    try:
        entries = list(PROC_ROOT.iterdir())
    except OSError:
        return []
    rows = [_process(prefix, int(p.name)) for p in entries if p.name.isdigit() and int(p.name) not in protected]
    return [{key: value for key, value in row.items() if not key.startswith('_')}
            for row in sorted((row for row in rows if row), key=lambda row: row['pid'])]


# LLM: 非交互且本发行的目标才允许打开pidfd；打开后再核出生及服务排除，PID复用不回退os.kill数字信号。
# 函数用途: 用准确进程句柄进行TERM及必要的KILL，并观察真正退出。
def _terminate(tool, pid):
    prefix = _installation(tool.prefix)
    if (type(pid) is not int or pid <= 1 or platform.system() != 'Linux' or prefix is None
            or pid in tool._protected()):
        raise ValueError('目标不是可终止的未受管发行进程')
    before = _process(prefix, pid)
    if not before or before['interactive'] or not before['cmdline']:
        raise ValueError('拒绝受管、交互、外部或身份未知的进程')
    if not callable(getattr(os, 'pidfd_open', None)) or not callable(getattr(signal, 'pidfd_send_signal', None)):
        raise OSError('当前内核或Python缺少安全pidfd支持，未发送信号')
    fd = os.pidfd_open(pid, 0)
    try:
        after = _process(prefix, pid)
        if (not after or after['_identity'] != before['_identity'] or after['interactive'] or pid in tool._protected()):
            raise ValueError('目标进程身份或服务归属已变化，未发送信号')
        signal.pidfd_send_signal(fd, signal.SIGTERM)
        if not select.select([fd], [], [], 0.3)[0]:
            if pid in tool._protected():
                raise RuntimeError('TERM后目标被登记为服务，未升级KILL，请核对当前状态')
            signal.pidfd_send_signal(fd, signal.SIGKILL)
            if not select.select([fd], [], [], 0.3)[0]:
                raise RuntimeError('已发信号但目标退出未确认')
        return {'target_pid': pid, 'terminated': True}
    finally:
        os.close(fd)


# LLM: systemd不经shell；fallback工作目录绑定本模块可信导入根，Home/绝对配置另传；转义展开、拒绝控制字符，不改变原CLI语义。
# 函数用途: 为用户级服务构造准确启动命令和unit正文。
def _unit_text(tool, prefix):
    launcher = prefix / 'bin/my-agent' if prefix else None
    stable = launcher is not None and launcher.is_file()
    command = [str(launcher)] if stable else [sys.executable, '-m', 'agent_py_agent']
    config = getattr(getattr(tool.agent, 'config', None), 'config_path', None)
    if config:
        config = Path(config).expanduser()
        config = config if config.is_absolute() else Path(tool.agent.root) / config
        command += ['--config', str(config.absolute())]
    command += ['gateway', 'supervisor']
    home_paths = getattr(tool.agent, 'home_paths', None)
    home = getattr(home_paths, 'root', None) or (prefix / 'data' if prefix else None)
    working = (getattr(home_paths, 'owner_home_dir', None) or home) if stable else Path(__file__).resolve().parents[3]
    if any(any(char in str(arg) for char in '\n\r\0') for arg in [*command, home or '', working or '']):
        raise ValueError('服务启动参数不能含控制字符')
    quoted = ' '.join(json.dumps(arg.replace('%', '%%').replace('$', '$$'), ensure_ascii=False) for arg in command)
    extra = ('Environment=' + json.dumps('MY_AGENT_HOME=' + str(home).replace('%', '%%'), ensure_ascii=False) + '\n') if home else ''
    if working:
        extra += 'WorkingDirectory=' + json.dumps(str(working).replace('%', '%%'), ensure_ascii=False) + '\n'
    return command, '[Unit]\nDescription=my-agent Gateway supervisor\n[Service]\nType=simple\n' + extra + 'ExecStart=' + quoted + '\nRestart=always\nRestartSec=5\n[Install]\nWantedBy=default.target\n'


# LLM: 经原resolver得到目标owner规范home，不创建目录；只读视图只给库路径，收口视图才打开可写运行库；会话库不初始化。
# 函数用途: 为管理员跨owner对账或唤醒治理组装目标owner的最小宿主视图。
def _owner_view(agent, owner_id, *, writable):
    from types import SimpleNamespace

    from ..gateway_parts.owner_conversation_store import owner_conversation_store
    from ..runtime_db.schema import runtime_db_path
    from ..user_space.owner_resolver import (
        home_paths_with_owner,
        owner_identity_from_owner_id,
        resolve_owner_home,
    )

    scoped = home_paths_with_owner(agent.home_paths, resolve_owner_home(agent.home_paths.root, owner_identity_from_owner_id(owner_id)))
    home = Path(scoped.owner_home_dir)
    if str(scoped.owner_id) != owner_id or home.is_symlink() or not home.is_dir():
        raise ValueError('目标 owner 不存在；请核对 owner_id')
    database = runtime_db_path(home)
    if writable:
        from ..runtime_db.repository import RuntimeRepository

        if database.is_symlink() or not database.is_file():
            raise ValueError('目标 owner 没有运行库；不在其目录下新建')
        repo = RuntimeRepository(database)
    else:
        repo = SimpleNamespace(db_path=database)
    return SimpleNamespace(config=agent.config, home_paths=scoped, root=home, subagents=SimpleNamespace(runtime_db=repo),
                           conversation_store=owner_conversation_store(agent, scoped, initialize=False))


# LLM: 身份/路径只由宿主构造，所有写动作须管理员与显式True双门且保留原审批；不注册为脚本执行拦截器。
# 类用途: 提供运行状态、维护锁、准确孤儿清理与用户级看门狗配置。
class RuntimeGovernanceTool(BaseTool):
    model_spec = ToolModelSpec(
        TOOL_NAME,
        '管理员运行治理：只读查看三层看门狗、发行版本和未受管进程；写动作须管理员及admin_authorized=True，不限制日常脚本。',
        {
            'type': 'object',
            'properties': {
                'action': {
                    'type': 'string',
                    'enum': ['status', 'reconcile_preview', 'quarantined_wakes_preview', *WRITES],
                    'description': 'status 只读查看三层看门狗、发行版本和未受管进程；其余写动作须管理员身份且显式传 admin_authorized=true。',
                },
                'cursor': {'type': 'string', 'maxLength': 8192, 'description': '上一页 next_cursor；首次使用空字符串，绑定当前 owner 与过滤。'},
                'limit': {'type': 'integer', 'minimum': 1, 'description': '本次最多处理的条目数；不传使用 retention_scan_slice_entries。'},
                'run_ids': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}, 'description': '可选准确子代理编号；不接受其他 owner 或路径。'},
                'owner_id': {'type': 'string', 'minLength': 1, 'maxLength': 300,
                             'description': '可选目标 owner（如 local/main、providers/feishu/users/<id>、providers/feishu/groups/<id>）；'
                                            '仅本机管理员前台可用，跨 owner 对账只处理执行账。'},
                'wake_signal_ids': {'type': 'array', 'minItems': 1, 'maxItems': 20, 'items': {'type': 'string', 'minLength': 1},
                                    'description': 'quarantined_wakes_dismiss / quarantined_wakes_replay 的准确唤醒编号。'},
                'admin_authorized': {
                    'type': 'boolean',
                    'description': '管理员明确授权本次控制动作，不替代宿主审批',
                },
                'reason': {
                    'type': 'string',
                    'minLength': 1,
                    'maxLength': 1000,
                    'description': '设置维护锁等治理动作的原因说明（1–1000 字符）。',
                },
                'ttl_seconds': {
                    'type': 'integer',
                    'minimum': 1,
                    'maximum': 604800,
                    'default': 1800,
                    'description': 'action=set_maintenance_lock 时维护锁的有效秒数（1–604800，默认 1800）。',
                },
                'target_pid': {
                    'type': 'integer',
                    'minimum': 2,
                    'description': 'action=terminate_orphan 时要终止的本发行非交互未受管进程 PID（>=2）。',
                },
                'unit_name': {
                    'type': 'string',
                    'default': DEFAULT_UNIT,
                    'description': 'action=configure_watchdog_service 时的单段安全 .service 单元名，默认 my-agent-gw-supervisor.service。',
                },
            },
            'required': ['action'],
            'additionalProperties': False,
        },
        hints=ToolModelHints(
            category='system',
            use_cases=(
                '管理员查询三层看门狗状态、私有发行版本、维护锁与未受管进程快照（action=status）',
                '管理员在升级或维护窗口显式授权开启或清除维护锁（set_maintenance_lock / clear_maintenance_lock）',
                '管理员显式授权清理已核验的非交互孤儿发行进程或生成用户级看门狗 unit 配置',
            ),
            avoid_when=(
                '日常执行普通脚本、工作流或监控任务时（不要用本工具代替常规执行工具）',
                '未获管理员明确指令时不要发起写动作或自行设置 admin_authorized=true',
            ),
            keywords=('runtime_governance', 'watchdog', '看门狗', '维护锁', '孤儿进程', 'supervisor'),
            examples=(
                '{"action":"status"}',
                '{"action":"set_maintenance_lock","admin_authorized":true,"reason":"rolling upgrade","ttl_seconds":1800}',
            ),
            default_deferred=True,
            deferred_summary='管理员运行治理：三层看门狗、维护锁与准确孤儿清理，不限制普通脚本',
        ),
    )
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy('dangerous', by_parameter=(('action', (('status', 'read_only'), ('reconcile_preview', 'read_only'), ('reconcile_apply', 'dangerous'),
                                                                                     ('quarantined_wakes_preview', 'read_only'))),)),
        approval_policy=ApprovalPolicy('always', always_effects=('dangerous',)), sandbox_policy=SandboxPolicy('none'),
        concurrency_policy=ConcurrencyPolicy('serial'), idempotency_policy=IdempotencyPolicy('operation'),
        resource_scopes=ResourceScopePolicy(mode='declared', static_scopes=('runtime_governance:installation',)))

    # LLM: 构造无读写副作用，Gateway路径复用唯一owner resolver；可选参数仅供宿主注入，工具Schema不接受它们。
    # 函数用途: 绑定当前管理员上下文、服务工作区及可选准确安装前缀。
    def __init__(self, agent=None, *, gateway_workspace=None, prefix=None, is_admin=False, owner_id='', owner_type='main_agent'):
        self.agent, self.prefix, self._is_admin = agent, prefix, is_admin is True
        tools = getattr(agent, 'tools', None)
        self.owner_id = str(owner_id or getattr(tools, 'owner_id', '') or '')
        self.owner_type = str(getattr(tools, 'owner_type', owner_type))
        if gateway_workspace is None and getattr(getattr(agent, 'home_paths', None), 'owner_home_dir', None):
            gateway_workspace = runtime_paths_for_agent(agent.config, Path(agent.root), agent.home_paths)['gateway_workspace']
        self.workspace = Path(gateway_workspace).absolute() if gateway_workspace is not None else None

    # LLM: 继承到子代理的工具对象也须按当前宿主身份复核；外部home不能凭同名local/admin或构造标志提权。
    # 函数用途: 判断当前调用者是否确实为管理员主代理。
    def _admin(self):
        role = getattr(getattr(self.agent, 'tools', None), 'owner_type', self.owner_type)
        if self.owner_type not in {'main_agent', 'user'} or role not in {'main_agent', 'user'}:
            return False
        if self.agent is not None and current_subagent_run_id(self.agent):
            return False
        home = getattr(self.agent, 'home_paths', None)
        if home is not None and not is_permission_admin(home):
            return False
        return self._is_admin or self.owner_id in {'local', 'admin'}

    # LLM: 请求快照只可收紧构造时身份；子代理快照不得借父工具对象发起任何写动作。
    # 函数用途: 保留原Registry上下文边界再执行治理动作。
    def execute_scoped(self, params, context):
        role = getattr(getattr(context, 'runtime_snapshot', None), 'owner_type', '')
        if params.get('action') in WRITES and role not in {'main_agent', 'user'}:
            return _reply({'error': DENIED}, 'PERMISSION_DENIED')
        return self.execute(params)

    # LLM: 身份双门在任何IO前；精确action路由，错误不冒充成功，可能已写unit或已发信号时保守unknown。
    # 函数用途: 执行只读状态或管理员本次明确授权的控制。
    def execute(self, params):
        action = params.get('action') if isinstance(params, dict) else None
        if action in WRITES and (not self._admin() or params.get('admin_authorized') is not True):
            return _reply({'error': DENIED}, 'PERMISSION_DENIED')
        if not validate_tool_input(params, self.model_spec.input_schema).ok:
            return _reply({'error': '参数不符合治理工具Schema'}, 'TOOL_INVALID_ARGUMENTS')
        try:
            if action in {'reconcile_preview', 'reconcile_apply', 'quarantined_wakes_preview', 'quarantined_wakes_dismiss',
                          'quarantined_wakes_replay'}:
                if self.agent is None:
                    return _reply({'error': '缺少当前 owner 宿主上下文；请从管理员前台重试。', 'effect_outcome': 'not_started'}, 'RECONCILIATION_CONTEXT_REQUIRED')
                from ..user_space.owner_access import is_foreground_admin_interactive
                own = str(getattr(getattr(self.agent, 'home_paths', None), 'owner_id', '') or '')
                cross = bool(params.get('owner_id')) and params.get('owner_id') != own
                if (action in WRITES or cross) and not is_foreground_admin_interactive(self.agent):
                    return _reply({'error': '当前调用没有本机管理员前台受理证明；请从本机前台或当前绑定管理员私聊发起。',
                                   'effect_outcome': 'not_started'}, 'ADMIN_CONTROL_DENIED')
                if cross and not self._admin():
                    return _reply({'error': DENIED}, 'PERMISSION_DENIED')
                target = _owner_view(self.agent, params['owner_id'], writable=action == 'reconcile_apply') if cross else self.agent
                if action.startswith('quarantined_wakes_'):
                    return _reply(_wake_governance(target, action, params, actor=self.owner_id or own))
                from ..conversation.runtime_reconciliation import reconcile_owner_runtime
                report = reconcile_owner_runtime(target, apply=action == 'reconcile_apply', cursor=params.get('cursor', ''),
                    limit=params.get('limit'), run_ids=tuple(params.get('run_ids', ())), task_runs_only=cross)
                return _reply(report, 'RECONCILIATION_PARTIAL' if report['errors'] else '', effect=report['effect_outcome'])
            if action == 'status':
                return _reply(self._status())
            if self.workspace is None:
                raise ValueError('缺少宿主Gateway工作区')
            if action in {'set_maintenance_lock', 'clear_maintenance_lock'}:
                return _reply(self._maintenance(params))
            if action == 'terminate_orphan':
                return _reply(_terminate(self, params.get('target_pid')))
            return _reply(self._configure(params))
        except ValueError as exc:
            return _reply({'error': str(exc)}, 'TOOL_INVALID_ARGUMENTS')
        except (OSError, RuntimeError) as exc:
            return _reply({'error': '治理动作未完成', 'error_type': type(exc).__name__}, 'TOOL_EXECUTION_FAILED',
                          effect='not_started' if action == 'status' else 'unknown')

    # LLM: PID/state/heartbeat任何记录中的角色PID都排除；控制时损坏记录拒绝，不把未读到角色当作孤儿。
    # 函数用途: 构造当前受保护的进程编号集合。
    def _protected(self, *, strict=True):
        rows = [_read(self.workspace, name) for name in SERVICE_FILES]
        if strict and any(isinstance(row, dict) and row.get('read_error') for row in rows):
            raise ValueError('服务身份记录不可读，拒绝终止进程')
        return {os.getpid(), *[pid for row in rows if (pid := _pid(row)) is not None]}

    # LLM: 状态不签发服务ready；异常时间戳局部标未知，不影响其他层；维护锁消费仍由原监护模块负责。
    # 函数用途: 汇总三层看门狗、维护期、发行信息及未受管进程。
    def _status(self):
        rows = {name: _read(self.workspace, name) for name in (*SERVICE_FILES, 'maintenance_lock.json', 'supervisor_service.json')}
        lock = rows['maintenance_lock.json']
        expiry = lock.get('expires_at') if isinstance(lock, dict) else None
        valid_expiry = type(expiry) in {int, float} and 0 <= expiry <= 253402300799 and math.isfinite(expiry)
        active = expiry > time.time() if valid_expiry else False if lock is None else None
        services = {role: dict(pid=_pid(rows[f'{role}.pid']), state=rows[f'{role}_state.json']) for role in ('gateway', 'adapter')}
        services['gateway']['heartbeat'] = rows['gateway_heartbeat.json']
        prefix = _installation(self.prefix)
        return dict(release_info=_release_info(prefix), layer1_os_service=_os_service(rows['supervisor_service.json']),
            layer2_supervisor=dict(pid=_pid(rows['supervisor.pid']), state=rows['supervisor_state.json'],
                                  maintenance_lock=lock, maintenance_lock_active=active),
            layer3_services=services, unmanaged_processes=_unmanaged(prefix, self._protected(strict=False)),
            process_observation=dict(platform=platform.system(), proc_available=PROC_ROOT.is_dir(), service_read_errors=[name for name in SERVICE_FILES if isinstance(rows[name], dict) and rows[name].get('read_error')]),
            normal_script_policy=dict(ad_hoc_scripts_restricted=False, first_class_script_scope=['workflow', 'monitor', 'skill']))

    # LLM: 只写或删固定maintenance_lock.json，TTL有界且actor来自宿主；nofollow拒绝链接，不改监护和调度策略。
    # 函数用途: 设置明确期限的维护标记或清除标记。
    def _maintenance(self, params):
        if params['action'] == 'clear_maintenance_lock':
            unlink_file_beneath(self.workspace, ('maintenance_lock.json',))
            return {'maintenance_lock': None, 'maintenance_lock_active': False}
        now = time.time()
        row = dict(reason=params.get('reason', 'administrator maintenance'), ttl_seconds=params.get('ttl_seconds', 1800),
                   actor=self.owner_id or 'admin', created_at=now, expires_at=now + params.get('ttl_seconds', 1800))
        _write(self.workspace, 'maintenance_lock.json', row)
        return {'maintenance_lock': row, 'maintenance_lock_active': True}

    # LLM: 只生成用户级unit及原服务登记，不执行daemon-reload/enable/start；跨文件部分写入失败由execute报告unknown。
    # 函数用途: 保存稳定启动入口的看门狗服务配置，显式标注尚未启用。
    def _configure(self, params):
        if platform.system() != 'Linux':
            raise ValueError('用户级systemd服务仅支持Linux')
        name = _unit_name(params.get('unit_name', DEFAULT_UNIT))
        command, text = _unit_text(self, _installation(self.prefix))
        directory = Path.home() / '.config/systemd/user'
        _write(directory, name, text)
        row = dict(unit_name=name, unit_path=str(directory / name), command=command, actor=self.owner_id or 'admin')
        _write(self.workspace, 'supervisor_service.json', row)
        return {**row, 'configured': True, 'activated': False}

    # LLM: 原执行器按真实动作锁定写根；参数不能注入任意根，status不声明写入，不放宽主/子路径或审批。
    # 函数用途: 向宿主声明准确服务状态与用户unit写入目录。
    def effective_write_roots(self, arguments, write_boundary, workspace_root):
        if arguments.get('action') not in WRITES:
            return ()
        if arguments.get('action') in {'reconcile_apply', 'quarantined_wakes_dismiss', 'quarantined_wakes_replay'}:
            root = getattr(getattr(self.agent, 'home_paths', None), 'owner_home_dir', None)
            owner_id = arguments.get('owner_id')
            if owner_id and owner_id != getattr(getattr(self.agent, 'home_paths', None), 'owner_id', None):
                from ..user_space.owner_resolver import (
                    home_paths_with_owner,
                    owner_identity_from_owner_id,
                    resolve_owner_home,
                )

                home = self.agent.home_paths
                root = home_paths_with_owner(home, resolve_owner_home(home.root, owner_identity_from_owner_id(owner_id))).owner_home_dir
            return (str(root),) if root is not None else ()
        roots = [str(self.workspace)] if self.workspace is not None else []
        if arguments.get('action') == 'configure_watchdog_service':
            roots.append(str(Path.home() / '.config/systemd/user'))
        return tuple(roots)


# LLM: 预览只读分页并附最近错误事实；撤销只归档不执行；重放沿 /wakes 同一入口（来源、pending 冲突、领域终态在结案锁内复核）。
#   每个编号独立处理，一个失败不影响其它；结果只含结构化码与编号。
# 函数用途: 执行已结案后台唤醒的预览、撤销或重放。
def _wake_governance(target, action, params, *, actor):
    from functools import partial

    from ..conversation.wake_domain_closeout import wake_domain_terminal

    store = target.conversation_store
    attempts = store.wakes.attempts
    owner_id = str(getattr(target.home_paths, 'owner_id', '') or '')
    if action == 'quarantined_wakes_preview':
        limit = params.get('limit') or 20
        page = attempts.quarantined_page(limit=min(int(limit), 100), cursor=params.get('cursor', ''))
        for row in page.get('items', []):
            context = attempts.quarantined_error_context(row.get('wake_signal_id') or '')
            if context:
                row['last_error'] = context
        return {'owner_id': owner_id, 'quarantined_count': attempts.quarantined_count(), **page}
    ids = list(params.get('wake_signal_ids') or [])
    if not ids:
        raise ValueError('wake_signal_ids 必须给出至少一个准确唤醒编号')
    now = time.time()
    results = []
    for wake_signal_id in ids:
        if action == 'quarantined_wakes_dismiss':
            results.append(attempts.dismiss(wake_signal_id, now=now, actor=actor, reason=str(params.get('reason') or '')))
            continue
        try:
            outcome = attempts.replay(wake_signal_id, now=now, domain_terminal=partial(wake_domain_terminal, store))
        except ValueError:
            results.append({'wake_signal_id': '', 'replayed': False, 'error_code': 'WAKE_SIGNAL_ID_INVALID'})
            continue
        results.append({'wake_signal_id': wake_signal_id, 'replayed': bool(outcome.ok), 'error_code': outcome.error_code or ''})
    done = sum(1 for row in results if row.get('dismissed') or row.get('replayed'))
    return {'owner_id': owner_id, 'action': action, 'results': results, 'applied': done,
            'quarantined_count': attempts.quarantined_count(), 'effect_outcome': 'succeeded' if done else 'not_started'}
