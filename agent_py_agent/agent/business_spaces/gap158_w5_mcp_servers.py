"""部署 MCP 配置走原参数中心；目录读取只投影已存在连接，不启动服务。"""
from __future__ import annotations

import hashlib
import json
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from ..conversation.business_access import BusinessError
from ..gateway_parts.business_api_v13 import v13_require_access, v13_require_admin
from ..plugin_host.errors import HostAPIError
from ..plugin_runtime import PluginMCPClient
from ..settings import parameter_changes
from ..settings.config import load_config
from ..settings.model_scope import deployment_model_config
from ..settings.user_config_capability import masked_structure, user_config_path
from ..tooling.mcp_client import MCPError, MCPServerConfig
from ..tooling.mcp_registration import MCPProxyTool, mcp_server_facts, refresh_registered_mcp_client
from .v13_store import _page_rows, v13_store_for


class _ReceiptReplay(Exception):
    """点查原 Store 的已封回执，不把永久历史塞入在途元数据。"""
    def __init__(self, fact):
        self.fact = fact


def receipted(call, work, *, collection='admin_ops', scope='main'):
    """原 Store 先耐久占位，锁外执行一次，最后按 claim 封回执；未结绝不重放副作用。"""
    v13_require_access(call)
    if not call.idempotency_key:
        raise HostAPIError('INVALID_SCHEMA')
    store = v13_store_for(call, scope)
    identity = hashlib.sha256(json.dumps([call.owner_id, call.subject_id, call.action,
        call.idempotency_key], separators=(',', ':')).encode()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps([call.action, call.params, call.expected_revision],
        sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    claim = uuid4().hex

    def reserve(metadata, _result):
        fact = metadata.fact(identity)
        if fact is not None:
            if fact.get('fingerprint') != fingerprint:
                raise HostAPIError('IDEMPOTENCY_CONFLICT', 409)
            raise _ReceiptReplay(fact)
        active = metadata.setdefault('w5_operations', {})
        previous = active.get(identity)
        if previous is not None:
            if previous.get('fingerprint') != fingerprint:
                raise HostAPIError('IDEMPOTENCY_CONFLICT', 409)
            raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown')
        if len(active) >= 64:
            raise HostAPIError('HOST_NOT_READY', 503)
        active[identity] = {'claim': claim, 'fingerprint': fingerprint}

    try:
        store.transaction(collection, lambda _records: {'reserved': True},
            idempotency_key='w5-reserve-' + claim, params_digest=fingerprint, metadata_update=reserve)
    except _ReceiptReplay as replay:
        fact = replay.fact
        if fact['state'] == 'completed':
            return fact['result']
        error = fact['error']
        if error['kind'] == 'business':
            raise BusinessError(error['code'], details=error.get('details')) from None
        raise HostAPIError(error['code'], error['status'], error['outcome']) from None

    def finish(value):
        def seal(metadata, _result):
            active = metadata.get('w5_operations', {})
            if active.get(identity) != {'claim': claim, 'fingerprint': fingerprint}:
                raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown')
            metadata.seal_fact(identity, {'fingerprint': fingerprint, **value})
            del active[identity]
        try:
            store.transaction(collection, lambda _records: {'sealed': True},
                idempotency_key='w5-seal-' + claim, params_digest=fingerprint, metadata_update=seal)
        except Exception:
            raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown') from None

    try:
        v13_require_access(call)
        result = work()
    except HostAPIError as error:
        finish({'state': 'failed', 'error': {'kind': 'host', 'code': error.code,
            'status': error.status, 'outcome': error.outcome}})
        raise
    except BusinessError as error:
        if error.code == 'OUTCOME_UNKNOWN':
            # 不封成可重试拒绝，保留同一占位；后续请求只报告未知。
            raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown') from None
        finish({'state': 'failed', 'error': {'kind': 'business', 'code': error.code,
            'details': error.details}})
        raise
    except Exception:
        raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown') from None
    finish({'state': 'completed', 'result': result})
    return result


def _admin_action(handler):
    """重放也须当前管理员；回执只承接原领域结果，不新增配置或连接权威。"""
    @wraps(handler)
    def invoke(call):
        v13_require_admin(call)
        return receipted(call, lambda: handler(call))
    return invoke


@contextmanager
def live_host(call):
    """仅借已加载的准确 owner 宿主；被动普通 owner 不因此构造 Agent。"""
    v13_require_access(call)
    host = call.context.host
    with ExitStack() as stack:
        if getattr(host, 'tools', None) is None:
            paths = host.home_paths
            provider = getattr(call.context, 'host_provider', None)
            if (getattr(paths, 'owner_provider', None) != 'local'
                    or getattr(paths, 'owner_kind', None) != 'main' or not callable(provider)):
                raise HostAPIError('HOST_NOT_READY', 503)
            host = provider(stack)
        paths = host.home_paths
        if (paths.owner_id != call.owner_id
                or Path(paths.owner_home_dir).resolve() != Path(call.owner_home).resolve()):
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        v13_require_access(call)
        yield host


@contextmanager
def live_registry(call):
    """持有原宿主借用至投影或刷新结束，既不建注册表也不准备全部 MCP。"""
    with live_host(call) as host:
        registry = getattr(host, 'tools', None)
        if registry is None or not hasattr(registry, 'tools'):
            raise HostAPIError('HOST_NOT_READY', 503)
        yield registry


def config_snapshot(call):
    """路径来自已加载部署配置；字节摘要与正式 reader 前后复核，防止丢失并发写。"""
    v13_require_access(call)
    config = deployment_model_config(call.services.host)
    path = user_config_path(config)
    if path is None or path.is_symlink() or not path.is_file():
        raise HostAPIError('HOST_NOT_READY', 503)
    try:
        original = path.read_bytes()
        loaded = load_config(path)
        if path.is_symlink() or path.read_bytes() != original:
            raise HostAPIError('REVISION_CONFLICT', 409)
    except (OSError, ValueError) as error:
        if isinstance(error, HostAPIError):
            raise
        raise HostAPIError('HOST_NOT_READY', 503) from None
    return path, hashlib.sha256(original).hexdigest(), loaded


def config_result(report):
    """参数事务结果只按稳定错误码归类，不把底层文案或凭据带到业务层。"""
    if report.get('ok'):
        return report['revision']
    code = report.get('code')
    if code == 'CONFIG_REVISION_CONFLICT':
        raise HostAPIError('REVISION_CONFLICT', 409)
    if code in {'PARAMETER_INVALID', 'CONFIG_INVALID', 'PARAMETER_NOT_EFFECTIVE'}:
        raise HostAPIError('INVALID_SCHEMA')
    if code == 'CONFIG_ROLLBACK_INCOMPLETE':
        raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown')
    raise HostAPIError('HOST_NOT_READY', 503)


def _expected(call, revision):
    """客户端 CAS 是额外约束；未传时仍用本次读出的摘要做内部 CAS。"""
    expected = call.params.get('expected_config_revision')
    if expected is not None and expected != revision:
        raise HostAPIError('REVISION_CONFLICT', 409)
    return revision


def _ordinary_clients(registry):
    owner = getattr(registry, '_mcp_catalog_owner', registry)
    return tuple(client for client in getattr(owner, '_mcp_clients', ())
                 if not isinstance(client, PluginMCPClient)
                 and not getattr(client, '_registry_policy_retiring', False))


def _facts(registry):
    """使用原发布事实，不向远端请求工具目录。"""
    source = SimpleNamespace(_mcp_clients=_ordinary_clients(registry))
    return {row['server']: row for row in mcp_server_facts(source)}


def _tool_rows(registry, name):
    """只选该服务的已发布代理，名称和 effect 均来自原规格而非服务自报权限。"""
    result = []
    owner = getattr(registry, '_mcp_catalog_owner', registry)
    clients = {id(client) for client in _ordinary_clients(registry)}
    for tool in tuple(owner.tools.values()):
        if (isinstance(tool, MCPProxyTool) and not isinstance(tool.client, PluginMCPClient)
                and id(tool.client) in clients and tool.client.config.name == name):
            result.append({'name': tool.remote_tool, 'model_name': tool.model_spec.name,
                           'description': tool.model_spec.description[:1000],
                           'effect': tool.client.config.effect_for_tool(tool.remote_tool)})
    return sorted(result, key=lambda row: row['model_name'])


def _server(name, entry, fact, count):
    """回显沿原结构脱敏，环境容器全部遮值；保存的部署原值不变。"""
    return {'name': name, 'enabled': True, 'connected': bool(fact.get('running', False)),
            'tool_count': count, 'entry': masked_structure('mcp_servers', {name: entry})[name]}


def list_servers(call):
    """读取部署声明和已加载目录；冷入口无运行来源时明确未就绪。"""
    _, revision, config = config_snapshot(call)
    entries = config.mcp_servers
    if len(entries) > 64:
        raise HostAPIError('HOST_NOT_READY', 503)
    with live_registry(call) as registry:
        facts = _facts(registry)
        rows = [_server(name, entry, facts.get(name, {}), len(_tool_rows(registry, name)))
                for name, entry in sorted(entries.items())]
    return {'items': rows, 'config_revision': revision}


@_admin_action
def create(call):
    """管理员修改部署映射，严格校验单项后复用原参数中心审计与整文件 CAS。"""
    v13_require_admin(call)
    path, revision, config = config_snapshot(call)
    revision = _expected(call, revision)
    entries = deepcopy(config.mcp_servers)
    name, entry = call.params['name'], call.params['entry']
    try:
        MCPServerConfig.from_mapping(name, entry)
    except MCPError:
        raise HostAPIError('INVALID_SCHEMA') from None
    if name in entries or len(entries) >= 64:
        raise HostAPIError('STATE_CONFLICT', 409)
    entries[name] = entry
    v13_require_admin(call)
    with parameter_changes.mcp_servers_write_scope():
        report = parameter_changes.set_parameter('mcp_servers', entries,
            paths=parameter_changes.WritePaths(path, expected_revision=revision),
            origin=parameter_changes.ChangeOrigin('business', 'mcp_servers.create'))
    current = config_result(report)
    # 保存不等于已经连接，新配置按原配置生效时机加载；此处不替用户启动进程。
    return {'server': _server(name, entry, {}, 0), 'config_revision': current}


@_admin_action
def delete(call):
    """删除只改部署映射，不把配置删除误报为现有进程已停止。"""
    v13_require_admin(call)
    path, revision, config = config_snapshot(call)
    revision = _expected(call, revision)
    entries = deepcopy(config.mcp_servers)
    name = call.params['name']
    if name not in entries:
        return {'deleted': False, 'config_revision': revision}
    del entries[name]
    v13_require_admin(call)
    with parameter_changes.mcp_servers_write_scope():
        report = parameter_changes.set_parameter('mcp_servers', entries,
            paths=parameter_changes.WritePaths(path, expected_revision=revision),
            origin=parameter_changes.ChangeOrigin('business', 'mcp_servers.delete'))
    return {'deleted': True, 'config_revision': config_result(report)}


def tools(call):
    """在原目录中分页，不连接、重连或刷新其他服务。"""
    _, _, config = config_snapshot(call)
    name = call.params['name']
    if name not in config.mcp_servers:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    with live_registry(call) as registry:
        facts = _facts(registry).get(name, {})
        rows = _tool_rows(registry, name)
        page = _page_rows(rows, [call.owner_id, call.action, name, facts, rows],
                          cursor=call.params.get('cursor'), limit=call.params.get('limit', 50))
    if page['next_cursor'] is None:
        del page['next_cursor']
    return {'connected': bool(facts.get('running', False)), **page}


@_admin_action
def refresh(call):
    """管理员只刷新命名客户端的固定连接，失败保持原连接事实，不扫描或重启全部服务。"""
    v13_require_admin(call)
    _, _, config = config_snapshot(call)
    name = call.params['name']
    if name not in config.mcp_servers:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    with live_registry(call) as registry:
        from ..tooling.registry import _project_shared_mcp_tools

        owner = getattr(registry, '_mcp_catalog_owner', registry)
        lock = getattr(owner, '_mcp_prepare_lock', None)
        if lock is None:
            raise HostAPIError('HOST_NOT_READY', 503)
        # 与原连接准备/关闭共用一把锁；全表发布必须针对原目录 owner，不能由权限视图覆盖它。
        with lock:
            closed = getattr(owner, '_mcp_closed', None)
            if closed is not None and closed.is_set():
                raise HostAPIError('HOST_NOT_READY', 503)
            client = next((client for client in _ordinary_clients(owner) if client.config.name == name), None)
            if client is None:
                return {'name': name, 'connected': False, 'tool_count': 0}
            v13_require_admin(call)
            try:
                count = refresh_registered_mcp_client(owner, client, transport=client.connection())
            except MCPError as error:
                return {'name': name, 'connected': bool(_facts(owner).get(name, {}).get('running', False)),
                        'tool_count': len(_tool_rows(owner, name)), 'error_code': error.code}
            _project_shared_mcp_tools(registry)
            return {'name': name, 'connected': bool(_facts(owner).get(name, {}).get('running', False)),
                    'tool_count': count}


HANDLERS = {'mcp_servers.list': list_servers, 'mcp_servers.create': create,
            'mcp_servers.delete': delete, 'mcp_servers.tools': tools, 'mcp_servers.refresh': refresh}
