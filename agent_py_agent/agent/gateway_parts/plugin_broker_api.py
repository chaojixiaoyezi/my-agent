# LLM: 宿主内 Broker；owner 参数须来自已认证 Gateway scope 或 from_grant，绝不可解包请求体作为宿主参数。
# 模块用途: 在固定用户范围包装资产、遥测和受控执行；不安装 HTTP 路由，不自行授予插件执行权。
import json
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from uuid import uuid4

from ..workflows.models import redact_bearer_values

READ_ACTIONS = frozenset(('assets.list', 'assets.get', 'assets.topology',
    'telemetry.tasks.list', 'telemetry.tasks.get', 'telemetry.workflows.runs.list',
    'telemetry.workflows.runs.get', 'telemetry.tool_calls.list',
    'telemetry.tool_calls.get', 'telemetry.tool_stats'))
WRITE_ACTIONS = frozenset(('assets.save', 'assets.update_sources', 'assets.bind'))
EXEC_ACTIONS = frozenset(('execute.tool', 'execute.script', 'execute.workflow', 'execute.workflow_resume'))


# LLM: 公共出口递归转成 JSON 类型并沿宿主规则脱敏；不修改原始数据，不将异常字符串公开。
# 函数用途: 隐藏结果中密钥字段、正文赋值及凭据 URL，保留 token 用量和引用。
def public_payload(value):
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        value = {key: public_payload(item) for key, item in value.items()}
    elif isinstance(value, (tuple, list)):
        value = [public_payload(item) for item in value]
    elif isinstance(value, Path):
        value = str(value)
    return redact_bearer_values(value)


# LLM: 仅平台管理员可在只读动作中选择规范 owner；路径必须与原身份后缀一致，禁止目录猜测或穿越。
# 函数用途: 从可信用户目录解析管理员的显式审计目标，不创建目录。
def _request_scope(home, owner_id, is_admin, action, payload):
    if not isinstance(owner_id, str) or not owner_id or any(
            key in payload for key in ('owner_home', 'is_admin', 'admin', 'target_owner_home')):
        raise PermissionError
    target = payload.pop('owner_id', owner_id)
    if target == owner_id:
        return Path(home), owner_id
    if is_admin is not True or action not in READ_ACTIONS or not isinstance(target, str):
        raise PermissionError
    from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home
    parts = target.split('/')
    if target == 'local/main':
        identity = OwnerIdentity.local_main()
    elif (len(parts) == 4 and parts[0] == 'providers' and parts[2] in ('users', 'groups')
          and all(part and part not in ('.', '..') and '\\' not in part for part in parts)):
        identity = OwnerIdentity(parts[1], 'user' if parts[2] == 'users' else 'group', parts[3])
    else:
        raise PermissionError
    suffix = ('owners', *owner_id.split('/'))
    path = Path(home).absolute()
    if path.parts[-len(suffix):] != suffix:
        raise PermissionError
    owner = resolve_owner_home(path.parents[len(suffix) - 1], identity)
    return owner.home_dir, owner.owner_id


# LLM: 仅宿主构造服务；普通构造参数必须是已认证 scope，from_grant 每次调用重验激活，invoker 必须绑定同一 owner 权限。
# 类用途: 固定一次插件会话的身份与受控执行入口。
class PluginBrokerService:
    # LLM: 所有参数均来自宿主认证上下文，不得解包外部payload；空run/task也绑定为空，不从请求补全。
    # 函数用途: 初始化单个 owner 的 Broker。
    def __init__(self, owner_home, *, owner_id, is_admin=False, tool_invoker=None,
                 include_asset_source: bool = True, trusted_run_id: str = '', trusted_task_id: str = ''):
        self.home, self.owner_id = Path(owner_home), owner_id
        self.is_admin, self.invoker, self.grant = is_admin, tool_invoker, None
        if not isinstance(trusted_run_id, str) or not isinstance(trusted_task_id, str):
            raise ValueError('可信运行和任务身份必须是字符串')
        self.include_asset_source = include_asset_source is True
        self.trusted_run_id, self.trusted_task_id = trusted_run_id, trusted_task_id
        self.trusted_parent_run_id, self.trusted_root_run_id = '', ''

    # LLM: 插件 grant 不授平台管理员身份，activation require 每次请求复验；调用方仍须验证令牌。
    # 函数用途: 从有效插件授权提取用户范围。
    @classmethod
    def from_grant(cls, grant, *, tool_invoker=None):
        from ..plugin_host_broker_topics import grant_asset_source
        grant.activation_ref.require()
        owner = grant.activation_ref.owner()
        activation = getattr(grant, 'activation', {})
        if not isinstance(activation, dict):
            raise ValueError('激活上下文必须是对象')
        service = cls(owner.home_dir, owner_id=owner.owner_id, tool_invoker=tool_invoker,
                      include_asset_source=grant_asset_source(grant),
                      trusted_run_id=activation.get('run_id', ''), trusted_task_id=activation.get('task_id', ''))
        service.trusted_parent_run_id = str(activation.get('parent_run_id', '') or '')
        service.trusted_root_run_id = str(activation.get('root_run_id', '') or '')
        service.grant = grant
        return service

    # LLM: 只覆盖身份字段，不向通用工具凭空添加未声明参数；可信invoker仍必须绑定同一上下文。
    # 函数用途: 裁决执行请求顶层与工具参数身份，并返回结构化冲突事实。
    def _execution_scope(self, payload):
        scope = {'owner_id': self.owner_id, 'run_id': self.trusted_run_id, 'task_id': self.trusted_task_id}
        tree = {**scope, 'parent_run_id': self.trusted_parent_run_id, 'root_run_id': self.trusted_root_run_id}
        warnings = []
        arguments = payload.get('arguments')
        if isinstance(arguments, dict):
            payload['arguments'] = arguments = dict(arguments)
        for label, values in (('', payload), ('arguments.', arguments)):
            if not isinstance(values, dict):
                continue
            for key, trusted in tree.items():
                if key in values and values[key] != trusted:
                    warnings.append(f'{label}{key} mismatch: caller passed {values[key]!r} but bound to {trusted!r}')
                if values is payload or key in values:
                    values[key] = trusted
        return {'scope_resolution': scope, 'scope_warnings': warnings}

    # LLM: 先校验身份再加载依赖；异常只给固定代码，避免数据库路径或秘密进入公开结果。
    # 函数用途: 分发插件请求，返回统一成功或失败信封。
    def dispatch(self, action, payload=None):
        try:
            if payload is None:
                payload = {}
            if not isinstance(payload, dict) or not isinstance(action, str):
                raise ValueError
            payload = dict(payload)
            scope = self._execution_scope(payload) if action in EXEC_ACTIONS else {}
            home, owner_id = _request_scope(self.home, self.owner_id, self.is_admin, action, payload)
            if self.grant is not None:
                try:
                    self.grant.activation_ref.require()
                except (OSError, ValueError, RuntimeError) as exc:
                    raise PermissionError from exc
                owner = self.grant.activation_ref.owner()
                if owner.home_dir != self.home or owner.owner_id != self.owner_id:
                    raise PermissionError
                if action in WRITE_ACTIONS:
                    raise PermissionError
            if action not in READ_ACTIONS | WRITE_ACTIONS | EXEC_ACTIONS:
                raise ValueError
            if action in EXEC_ACTIONS and not callable(self.invoker):
                return {'ok': False, 'error_code': 'BROKER_EXECUTION_DENIED'}
            data = self._route(home, owner_id, action, payload)
            return {'ok': True, 'data': public_payload(data | scope if scope else data)}
        except PermissionError:
            return {'ok': False, 'error_code': 'BROKER_SCOPE_DENIED'}
        except (ValueError, TypeError, KeyError):
            return {'ok': False, 'error_code': 'BROKER_INVALID_REQUEST'}
        except (ImportError, OSError, RuntimeError):
            return {'ok': False, 'error_code': 'BROKER_UNAVAILABLE'}

    # LLM: 路由保持显式白名单，不按请求字符串反射调用任意方法。
    # 函数用途: 将验证后的动作交给对应服务。
    def _route(self, home, owner_id, action, payload):
        from ..plugin_host_broker_topics import mutate_assets, project_asset, query_assets
        if action in EXEC_ACTIONS:
            return self._execute(home, owner_id, action, payload)
        if action in WRITE_ACTIONS:
            return project_asset(mutate_assets(home, owner_id, action, payload), home, self.include_asset_source)
        if action.startswith('assets.'):
            return query_assets(home, owner_id, action, payload, grant=self.grant,
                                include_asset_source=self.include_asset_source)
        return _query_telemetry(home, owner_id, action, payload)

    # LLM: invoker 必须已绑定当前 owner 的授权/审批链；先创建账本，再执行一次，使用单调时钟测时。
    # 函数用途: 执行工具并记录成功、拒绝、超时或失败；记账失败仍保留执行事实，不能自动重试。
    def _execute(self, home, owner_id, action, payload):
        from ..observability.telemetry_ledger import ToolTelemetryLedger
        name, arguments = _execution_arguments(action, payload)
        ledger = ToolTelemetryLedger(home, owner_id=owner_id)
        record = {key: payload[key] for key in ('task_id', 'run_id', 'parent_run_id',
                  'root_run_id', 'workflow_run_id', 'step_id') if key in payload}
        record.update(call_id=uuid4().hex, tool_name=name, owner_id=owner_id,
                      args_summary=public_payload(arguments), started_at=time.time(), status='ok', error_code='')
        start = time.monotonic()
        try:
            result = _execution_result(self.invoker(name, arguments))
            if isinstance(result, dict) and (result.get('ok') is False or result.get('status') in (
                    'failed', 'error', 'paused_for_resume', 'cancelled', 'denied', 'timeout')):
                status = result.get('status')
                record.update(status=status if status in ('denied', 'timeout') else 'error',
                              error_code=result.get('error_code') or 'TOOL_EXECUTION_' + (
                                  'INCOMPLETE' if status == 'paused_for_resume' else 'FAILED'))
        except Exception as exc:
            status = 'denied' if isinstance(exc, PermissionError) else 'timeout' if isinstance(exc, TimeoutError) else 'error'
            record.update(status=status, error_code='TOOL_EXECUTION_' + status.upper())
            result = {'ok': False, 'error_code': record['error_code']}
        record.update(ended_at=time.time(), duration_ms=round((time.monotonic() - start) * 1000, 3))
        if isinstance(result, dict):
            record['artifact_ref'] = result.get('artifact_ref', '')
            if name == 'workflows' and isinstance(result.get('workflow_run_id'), str):
                record['workflow_run_id'] = result['workflow_run_id']
        persisted = True
        try:
            ledger.record_tool_call(record)
        except (OSError, ValueError, TypeError, RuntimeError):
            persisted = False
        return {key: record[key] for key in ('call_id', 'started_at', 'ended_at', 'duration_ms', 'status', 'error_code')} | {
            'result': result, 'telemetry_persisted': persisted}


# LLM: 身份由Service裁决；恢复补丁及数据源仅透传给受控工具验证，不能覆盖宿主身份或路径。
# 函数用途: 将脚本、工作流及其修复恢复动作转换为受控工具调用。
def _execution_arguments(action, payload):
    if action == 'execute.tool':
        name, arguments = payload['tool_name'], payload.get('arguments', {})
    else:
        mapping = {'execute.script': ('scripts', 'run', 'script_id', ('inputs', 'override_data_sources')),
                   'execute.workflow': ('workflows', 'run', 'workflow_id', ('inputs',)),
                   'execute.workflow_resume': ('workflows', 'resume', 'workflow_run_id',
                                               ('from_step_id', 'patched_step_output', 'updated_data_sources'))}
        name, operation, id_key, options = mapping[action]
        arguments = {'action': operation, id_key: payload[id_key]}
        arguments.update({key: payload[key] for key in options if key in payload})
        if operation == 'run':
            arguments.setdefault('inputs', {})
    if not isinstance(name, str) or not name or not isinstance(arguments, dict):
        raise ValueError('工具名或参数无效')
    if any(key in arguments for key in ('owner_home', 'is_admin', 'admin', 'target_owner_home')):
        raise PermissionError
    if name in ('scripts', 'workflows'):
        arguments = {key: value for key, value in arguments.items()
                     if key not in ('owner_id', 'run_id', 'task_id', 'parent_run_id', 'root_run_id')}
    return name, arguments


# LLM: 工具回执传输成功不代表业务成功；仅解包真实 ToolHandlerOutcome 类型，普通文本不作控制字段。
# 函数用途: 提取宿主工具结果中的业务 JSON，并保留宿主拒绝或执行失败。
def _execution_result(value):
    from ..tooling.models import ToolHandlerOutcome
    if isinstance(value, ToolHandlerOutcome):
        try:
            result = json.loads(value.output)
        except (ValueError, TypeError):
            result = {'output': value.output}
        if not isinstance(result, dict):
            result = {'output': result}
        if not value.ok:
            result.update(ok=False, error_code=value.error_code)
        value = result
    return public_payload(value)


# LLM: 查询条件和分页先校验再加载账本；未知字段拒绝，不扩大用户原查询。
# 函数用途: 统一限制遥测查询的过滤字段与分页参数。
def _validate_query(action, payload):
    filters = {'telemetry.tasks.list': {'status', 'trigger_kind', 'since', 'until', 'limit', 'cursor'},
        'telemetry.tool_calls.list': {'task_id', 'run_id', 'workflow_run_id', 'tool_name', 'status',
                                     'min_duration_ms', 'since', 'until', 'limit', 'cursor'},
        'telemetry.workflows.runs.list': {'workflow_id', 'status', 'limit'},
        'telemetry.tasks.get': {'task_id'}, 'telemetry.tool_calls.get': {'call_id'},
        'telemetry.workflows.runs.get': {'workflow_run_id'}, 'telemetry.tool_stats': {'since', 'until'}}
    if payload.keys() - filters[action]:
        raise ValueError('未知查询条件')
    for key in ('limit', 'cursor'):
        if key in payload and (type(payload[key]) is not int or payload[key] < 0
                               or key == 'limit' and payload[key] > 1000):
            raise ValueError('分页参数无效')


# LLM: 所有遥测对象只使用可信 home；查询不接受任意方法名或额外过滤键，避免误拼条件静默扩大范围。
# 函数用途: 显式分派任务、工作流运行及工具调用的查询与统计。
def _query_telemetry(home, owner_id, action, payload):
    _validate_query(action, payload)
    from ..observability.telemetry_ledger import ToolTelemetryLedger
    if action.startswith('telemetry.workflows.'):
        from ..workflows.run_store import WorkflowRunStore
        store = WorkflowRunStore(home)
        if action.endswith('.get'):
            return store.load_run(payload['workflow_run_id'])
        return store.list_runs(**payload)
    ledger = ToolTelemetryLedger(home, owner_id=owner_id)
    if action.startswith('telemetry.tasks.'):
        from ..observability.task_call_inspector import TaskCallInspector
        inspector = TaskCallInspector(home, owner_id=owner_id)
        if action.endswith('.get'):
            return inspector.get_task_call_detail(payload['task_id'], ledger=ledger)
        return inspector.list_task_calls(**payload)
    if action == 'telemetry.tool_calls.get':
        return ledger.get_tool_call(payload['call_id'])
    if action == 'telemetry.tool_stats':
        from ..plugin_host_broker_topics import tool_and_script_stats
        return tool_and_script_stats(ledger, payload)
    return ledger.query_tool_calls(**payload)


# LLM: 这是内部可信 Gateway 入口，不是请求体直接反序列化入口；保留任务指定六参数签名。
# 函数用途: 为单次请求创建固定 owner 的 Broker 并返回脱敏信封。
def dispatch_plugin_broker_request(owner_home, *, owner_id, is_admin=False,
                                   action, payload=None, tool_Invoker=None):
    return PluginBrokerService(owner_home, owner_id=owner_id, is_admin=is_admin,
                               tool_invoker=tool_Invoker).dispatch(action, payload)
