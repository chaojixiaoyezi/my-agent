# LLM: 自身发现只投影宿主owner、actor与冻结边界；插件cwd读根与内置读门分别标来源，不读配置正文或产生授权。
# 模块用途: 为 list_tools 和 /status 提供同源、无秘密的本人身份及执行边界说明。
from __future__ import annotations

from ..conversation.host_actor import HostActorError, resolve_host_actor
from ..conversation.tool_discovery import thread_loaded_tools
from ..settings.reasoning_effort import run_thread_id
from ..user_space.approval_mode import read_permission_state
from ..user_space.owner_policy import resolve_effective_owner_policy
from .models import ToolInvocationContext, ToolRuntimeSnapshot


# LLM: 裸 registry 只能说明实际读到的执行模式，不将 owner_type 误当 ask/auto；可见名称严格限制在本轮允许集合。
# 函数用途: 为工具合同探针和宿主回调生成共同的目录事实，不读取全局配置或运行工具。
def registry_self_context(registry, snapshot: ToolRuntimeSnapshot, context: ToolInvocationContext | None = None) -> dict[str, object]:
    available = set(snapshot.available_tool_names)
    visible = {spec.name for spec in registry.model_visible_specs(runtime_snapshot=snapshot)}
    unavailable = {name: (code, reason) for name, code, reason in snapshot.unavailable_tools}
    scope = snapshot.allowed_tools
    states = []
    for name, tool in registry.tools.items():
        if (scope is not None and name not in scope) or (scope is None and name in registry.default_hidden_tool_names):
            continue
        if name in registry.disabled_tool_names:
            state, code, reason = 'disabled', 'TOOL_DISABLED', '当前 owner 或部署策略禁用。'
        elif name in unavailable:
            state, (code, reason) = 'unavailable', unavailable[name]
        elif name in available:
            state, code, reason = ('ready' if name in visible else 'deferred'), '', ''
        else:
            continue
        item = {'name': name, 'state': state, 'error_code': code, 'reason': reason}
        if state in {'disabled', 'unavailable'}:
            item['description'] = tool.model_spec.description[:220]
        states.append(item)
    try:
        execution = registry.approval_mode_reader() if registry.approval_mode_reader else 'ask'
    except (OSError, ValueError):
        execution = 'unavailable'
    return {'schema_version': 'tool-self-context.v1', 'run_id': snapshot.run_id,
            'owner_type': snapshot.owner_type, 'scope': 'current_owner_and_run',
            'permissions': {'mode': 'unknown', 'approval_mode': execution, 'source': 'unbound_owner'},
            'tool_states': states,
            'usage': {'query': 'list_tools', 'tool_details': 'tool_search',
                      'status': '/status', 'help': '/help',
                      'permissions': '由本人在正规权限面板或管理入口修改；查询不改变授权。',
                      'resource_authorization': '工具就绪仍须逐次核对具体文件、网络、凭据和通道资源授权。'}}


# LLM: composition root绑定agent；文件根来自本调用上下文，read_roots明确为插件cwd范围，builtin读门只列冻结模式/数量。
#   不新增披露exact/墙外路径，不热读currentrun重建读权；工具可见与路径裁决仍由原执行器掌握。
# 函数用途: 为当前本人生成可公开的身份、真实审批来源、文件与网络范围及线程加载事实。
def tool_self_context(agent, snapshot: ToolRuntimeSnapshot, context: ToolInvocationContext | None = None) -> dict[str, object]:
    home = getattr(agent, 'home_paths', None)
    current = getattr(agent, '_current_run_params', None)
    thread_id = run_thread_id(current) if current is not None else ''
    boundary = getattr(current, 'write_boundary', None)
    boundary = boundary if isinstance(boundary, dict) else {}
    if context is not None and context.execution_permission_context is not None:
        boundary = dict(context.execution_permission_context)
    permissions = permission_self_context(agent)
    actor = None
    actor_error = ''
    if thread_id:
        try:
            resolved = resolve_host_actor(agent, thread_id)
            actor = resolved.to_record() if resolved is not None else None
        except HostActorError as exc:
            actor_error = exc.error_code
    registry = agent.tools
    read = context.workspace_read_context if context is not None else None
    write = context.workspace_write_context if context is not None else None
    roots = [str(root) for root in registry.workspace_roots]
    frozen = context.execution_permission_context if context is not None else None
    builtin_mode = (frozen or {}).get("builtin_read_scope_mode", "not_observed")
    builtin_roots = (frozen or {}).get("builtin_read_roots")
    builtin_external = (frozen or {}).get("builtin_read_external_roots")
    loaded = thread_loaded_tools(agent, current) if thread_id else set()
    loaded.intersection_update(snapshot.available_tool_names)
    states = registry_self_context(registry, snapshot, context)['tool_states']
    model_names = {spec.name for spec in registry.model_visible_specs(runtime_snapshot=snapshot, loaded_tool_names=loaded)}
    for item in states:
        if item['name'] in snapshot.available_tool_names:
            item['state'] = 'ready' if item['name'] in model_names else 'deferred'
    return {'tool_states': states, 'identity': {'owner_id': str(getattr(home, 'owner_id', '') or ''),
                         'owner_provider': str(getattr(home, 'owner_provider', '') or ''),
                         'owner_kind': str(getattr(home, 'owner_kind', '') or ''),
                         'run_id': snapshot.run_id, 'thread_id': thread_id,
                         'actor': actor, 'actor_error_code': actor_error},
            'permissions': permissions,
            'filesystem': {'path_access_mode': read.path_policy.mode if read else registry.path_access_mode,
                           'shell_access_mode': boundary.get('shell_access_mode') or getattr(getattr(agent, 'config', None), 'access_mode', 'unknown'),
                           'owner_scoped': bool(read.path_policy.owner_scope_root) if read else bool(registry.owner_scope_root),
                           'read_roots': [str(root) for root in read.read_roots] if read else _read_roots(boundary, roots),
                           'read_roots_scope': 'plugin_workspace_context' if read else 'unbound_registry_projection',
                           'builtin_read_scope': builtin_mode,
                           'builtin_read_scope_source': 'frozen_executor_context' if builtin_mode in {'exact', 'policy'} else 'not_observed',
                           'builtin_exact_read_root_count': len(builtin_roots) if isinstance(builtin_roots, tuple) else None,
                           'builtin_external_read_root_count': len(builtin_external) if isinstance(builtin_external, tuple) else None,
                           'write_roots': [str(root) for root in write.write_roots] if write else _boundary_roots(boundary, roots),
                           'source': 'invocation_context' if context is not None else 'current_runtime'},
            'network': network_self_context(agent, boundary),
            'discovery': {'loading_scope': 'current_thread', 'persists_across_compact': True,
                          'loaded_tools': sorted(loaded), 'model_schema_tools': sorted(model_names),
                          'deferred_tools': sorted(snapshot.available_tool_names - model_names),
                          'authorization_rechecked_each_run': True}}


# LLM: 本人模式的单一来源为 approval_mode；读取失败只投影固定原因，不能借配置错误放宽或泄露原正文。
# 函数用途: 返回 /status 与模型自查共用的审批选择、执行模式和来源。
def permission_self_context(agent) -> dict[str, object]:
    home = getattr(agent, 'home_paths', None)
    if home is None or not hasattr(home, 'owner_tool_policy_json'):
        return {'mode': 'unknown', 'approval_mode': 'unknown', 'source': 'unbound_owner'}
    try:
        return read_permission_state(home)
    except (OSError, ValueError, TypeError, AttributeError):
        return {'mode': 'unavailable', 'approval_mode': 'unavailable', 'source': 'invalid_policy',
                'reason': '本人审批配置无法读取或无效，未放宽权限。'}


# LLM: 网络有效事实沿原 owner policy 与本轮 boundary，只投影布尔值和范围，不输出原策略文件或凭据。
# 函数用途: 解释 owner 与任务对网络的当前收紧；缺身份或坏账明确标未知。
def network_self_context(agent, boundary: dict[str, object]) -> dict[str, object]:
    home = getattr(agent, 'home_paths', None)
    try:
        policy = resolve_effective_owner_policy(home)
        permissions = boundary.get('effective_permissions') or {}
        child_enabled = boundary.get('network_enabled', permissions.get('network_enabled', True) if isinstance(permissions, dict) else True)
        frozen_owner_enabled = bool(getattr(getattr(agent, 'owner_policy', None), 'network_enabled', True))
        enabled = policy.network_enabled and frozen_owner_enabled and child_enabled and boundary.get('network_access_mode') != 'disabled'
        return {'enabled': enabled, 'source': 'owner_policy_and_run_boundary',
                'scope': list(boundary.get('network_scope') or []), 'resource_authorization_required': True}
    except (OSError, ValueError, TypeError, AttributeError):
        return {'enabled': None, 'source': 'unavailable', 'resource_authorization_required': True}


# LLM: 这里只读宿主 write_boundary 的原结构，缺省沿 registry 工作区；不会按用户正文推断可写目录。
# 函数用途: 在无 scoped 文件上下文的状态查询中展示已冻结写根。
def _boundary_roots(boundary: dict[str, object], defaults: list[str]) -> list[str]:
    roots = boundary.get('allowed_write_roots')
    return [str(root) for root in roots] if isinstance(roots, (list, tuple)) else defaults


# LLM: exact 读取范围必须保留显式空集合，不能以默认根覆盖宿主收紧；与原 runtime_boundary 同源。
# 函数用途: 在无 scoped 上下文时展示精确读取根。
def _read_roots(boundary: dict[str, object], defaults: list[str]) -> list[str]:
    if boundary.get('read_scope_mode') != 'exact':
        return defaults
    roots = boundary.get('allowed_read_roots')
    return [str(root) for root in roots] if isinstance(roots, (list, tuple)) else []
