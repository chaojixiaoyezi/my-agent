"""W7 剧本模板、空间草案、零执行预检与社区交换。

模型只生成未保存定义；预检只消费冻结 preview 与现有声明，不创建 Agent、
启动服务、调用工具或直连网络。社区外发沿宿主 provider，重放不重复外发。
"""
from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath

from ..assets.script_store import ScriptStore
from ..assets.workflow_store import WorkflowDefinition, WorkflowStore
from ..common.log_redaction import redact_sensitive_value
from ..plugin_host.business_action_specs import business_action_spec
from ..plugin_host.contracts import DomainResult
from ..plugin_host.errors import HostAPIError
from ..plugin_host.schemas import canonical_json, strict_json, thaw, validate_object
from ..tooling.input_schema import validate_tool_input
from .v13_store import _page_rows, v13_store_for

ROUTE_MAP = {
    'GET /api/v1/playbooks/gallery': 'workflows.templates.list',
    'POST /api/v1/playbooks/install-template': 'workflows.create',
    'POST /api/v1/playbooks/draft-from-prompt': 'workflows.draft_from_prompt',
    'POST /api/v1/playbooks/{id}/dry-run': 'workflows.dry_run',
    'POST /api/v1/business-context/preview': 'business_context.rules.preview (W11 #100)',
    'GET /api/v1/live-actions': 'live_actions.discover',
    'GET /api/v1/live-actions/by-capability/{capability}': 'live_actions.by_capability',
    'GET /api/v1/live-actions/by-vendor/{vendorId}': 'live_actions.by_vendor',
    'POST /api/v1/live-actions/dry-run': 'live_actions.dry_run',
    'GET /api/v1/community/playbooks': 'community_playbooks.list',
    'POST /api/v1/community/playbooks/submit': 'community_playbooks.submit',
    'POST /api/v1/community/playbooks/{id}/install': 'community_playbooks.install',
    'POST /graphql': 'unsupported: closed ActionSpec; use specific actions',
    'GET /api/v1/marketplace/installed': 'spaces.packages.catalog / spaces.packages.list',
    'POST /api/v1/marketplace/install': 'packages.install (W11 #74)',
}
FROZEN_NAME_MAPPING = {
    'workflows.delete': ('workflows.recycle',),
    'workflows.scope.preview': ('workflows.scope.get', 'workflows.preview'),
    'workflows.scope.apply': ('workflows.scope.set',),
}


# LLM: 摘要绑定 JSON 意图，不是权限或执行证据。
# 函数用途: 生成稳定的操作和快照标识。
def _digest(value):
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


# LLM: 冻结动作可能返回 DomainResult，不把领域数据误当信封。
# 函数用途: 取得领域返回的独立 JSON 数据。
def _data(value):
    return thaw(value.data if isinstance(value, DomainResult) else value)


# LLM: 空间只来自原领域，不从输入 ID 自授范围。
# 函数用途: 解析本次明确或默认空间。
def _space(call):
    from .v13_w7_simulation_sessions import resolve_space
    return _data(resolve_space(call))


# LLM: 宿主字段不能回灌冻结定义；保留普通脚本与 Skill 声明。
# 函数用途: 将原存储定义转换为可编辑的冻结输入投影。
def _editable_definition(value):
    value = thaw(value.to_dict() if isinstance(value, WorkflowDefinition) else value)
    descriptor = value['descriptor']
    for name in ('owner_id', 'revision', 'runtime_summary', 'used_by', 'scope_layer',
                 'source_scope', 'workspace_root', 'schema_version'):
        descriptor.pop(name, None)
    descriptor.get('metadata', {}).pop('business_workflow', None)
    return value


# LLM: 冻结输入、原值对象和 DAG 依赖同时验证，不静默修复非法定义。
# 函数用途: 验证画布可保存的定义及步骤依赖。
def _validate_definition(value):
    result = validate_object({'definition': value}, business_action_spec('workflows.create').params_schema)['definition']
    try:
        WorkflowDefinition.from_any(result)
        steps = {step['step_id']: step for step in result['steps']}
        if len(steps) != len(result['steps']):
            raise ValueError('重复步骤')
        graph = {key: {item['step_id'] for item in step.get('inputs_map', {}).values()
                       if isinstance(item, dict) and 'step_id' in item and item['step_id'] != '$inputs'}
                 for key, step in steps.items()}
        pending, done = set(graph), set()
        while pending:
            ready = {key for key in pending if graph[key] <= done}
            if not ready:
                raise ValueError('依赖缺失或循环')
            done.update(ready)
            pending.difference_update(ready)
    except (ValueError, TypeError, KeyError) as error:
        raise HostAPIError('INVALID_SCHEMA') from error
    return result


# LLM: 分页绑定主体、空间、筛选与完整投影，不写集合或回执。
# 函数用途: 为只读目录生成防篡改快照游标。
def _page(call, rows, *, partial=False):
    filters = {key: value for key, value in call.params.items() if key not in ('cursor', 'limit')}
    result = _page_rows(rows, [str(call.owner_home), call.owner_id, call.subject_id, call.action, filters, rows],
                        cursor=call.params.get('cursor'), limit=call.params.get('limit', 50))
    if partial:
        result['coverage'] = 'partial'
    return result


# LLM: 先经冻结目录，只读当前安装代次的已声明成员，不扫描或解包。
# 函数用途: 取得当前可用内容包快照。
def _packages(call):
    from ..capability.package_provider import (
        _owner_from_home,
        enabled_capability_packages,
        enabled_public_capability_packages,
    )
    from ..gateway_parts.business_api_v13 import v13_call_frozen
    visible, cursor = set(), None
    for index in range(100):
        params = {'limit': 100}
        if cursor is not None:
            params['cursor'] = cursor
        page = _data(v13_call_frozen(call, 'spaces.packages.catalog', params))
        visible.update(row['package_ref'] for row in page['items']
                       if row['availability']['state'] == 'available')
        cursor = page['next_cursor']
        if cursor is None:
            break
    if cursor is not None:
        raise HostAPIError('LIMIT_EXCEEDED')
    owner = _owner_from_home(call.owner_home)
    return tuple(package for package in (*enabled_capability_packages(owner),
                                        *enabled_public_capability_packages(owner))
                 if package.stable_id in visible)


# LLM: 引用从结构字段汇总，不解析说明文案猜执行关系。
# 函数用途: 汇总定义中的脚本、Skill 与监控器引用。
def _definition_refs(definition):
    refs = {'skill_ids': set(), 'script_ids': set(), 'monitor_rule_ids': set()}
    pending = [definition]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                target = {'skill_id': 'skill_ids', 'script_id': 'script_ids',
                          'monitor_rule_id': 'monitor_rule_ids'}.get(key, key)
                if target in refs:
                    values = value if isinstance(value, list) else [value]
                    refs[target].update(item for item in values if isinstance(item, str) and item)
                pending.append(value)
        elif isinstance(node, list):
            pending.extend(node)
    declared = definition['descriptor'].get('refs', {})
    for key, target in (('skills', 'skill_ids'), ('scripts', 'script_ids')):
        refs[target].update(declared.get(key, []))
    return {key: sorted(value) for key, value in refs.items()}


# LLM: 坏包条目跳过并标覆盖不足，不伪造完整模板库。
# 函数用途: 从已安装包和原全局工作流层投影模板。
def list_templates(call):
    from ..capability.package_resources import package_resource_reference
    from ..gateway_parts.business_api_v13 import v13_require_access, v13_schema
    v13_require_access(call)
    if call.params.get('space_id'):
        _space(call)
    from ..workflows.native_governance import builtin_workflow_templates
    candidates = [(item, 'builtin', None, item['descriptor']['id']) for item in builtin_workflow_templates()]
    partial = False
    for package in _packages(call):
        for member in package.members:
            path = PurePosixPath(member.path)
            if 'workflows' not in path.parts[:-1] or path.suffix != '.json':
                continue
            try:
                definition = _validate_definition(strict_json(package.read(member.path), maximum=262144))
                # 来源随可编辑定义一起交付；只记录原成员身份，不给派生副本授予包写权。
                definition['descriptor'].setdefault('metadata', {})['derived_from'] = package_resource_reference(package, member.path)
                candidates.append((definition, 'package', package.package_id, package.stable_id + ':' + member.path))
            except (HostAPIError, ValueError, TypeError, KeyError):
                partial = True
    for item in WorkflowStore(call.owner_home).list_workflows():
        if item.descriptor.scope_layer in ('global', 'builtin'):
            try:
                candidates.append((_validate_definition(_editable_definition(item)), 'builtin', None, item.descriptor.id))
            except (HostAPIError, ValueError, TypeError, KeyError):
                partial = True
    rows = []
    for definition, source, package_id, identity in candidates:
        descriptor = definition['descriptor']
        if call.params.get('tag') and call.params['tag'] not in descriptor.get('tags', []):
            continue
        refs = _definition_refs(definition)
        row = {'template_id': 'template-' + _digest(identity)[:48], 'name': descriptor['name'],
                     'summary': descriptor.get('summary', ''), 'tags': descriptor.get('tags', []),
                     'source': source, 'package_id': package_id, 'definition': definition,
                     'required_script_ids': refs['script_ids'], 'required_skill_ids': refs['skill_ids']}
        try:
            rows.append(validate_object(row, v13_schema({'$ref': '#/$defs/V13WorkflowTemplate'})))
        except HostAPIError:
            partial = True
    return _page(call, sorted(rows, key=lambda row: row['template_id']), partial=partial)


# LLM: 唯一领域调用是冻结 preview，不执行脚本、工具、通知或模型。
# 函数用途: 将原 preview 计划和错误映射为零副作用视图。
def dry_run(call):
    from ..gateway_parts.business_api_v13 import v13_call_frozen
    params = {'workflow_ref': call.params['workflow_id']}
    for key in ('space_id', 'inputs'):
        if key in call.params:
            params[key] = call.params[key]
    try:
        preview = _data(v13_call_frozen(call, 'workflows.preview', params))
    except KeyError as error:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404) from error
    steps = []
    for step in preview['plan']:
        mode = step['mode']
        effects = ('external' if mode in ('http_request', 'tool_call', 'notification', 'script', 'agent_task')
                   else 'none' if mode.startswith('condition') else 'unknown')
        steps.append({'step_id': step['step_id'], 'mode': mode, 'would_run': step['would_execute'],
                      'side_effects': effects, 'blocked_reason': None if step['would_execute'] else 'preview_blocked'})
    return {'workflow_id': call.params['workflow_id'], 'steps': steps,
            'required_capabilities': preview['required_capabilities'], 'errors': preview['errors'],
            'external_side_effects': 'none'}


# LLM: 作业终结是纯值验证，不保存或执行模型生成的 DAG。
# 函数用途: 解析 workflow_draft 严格 JSON 并核对实际引用完整性。
def finalize_workflow_draft(text: str) -> dict:
    from ..gateway_parts.business_api_v13 import v13_schema
    result = strict_json(text.encode('utf-8'), maximum=262144)
    shape = {'type': 'object', 'properties': {'definition': {'$ref': '#/$defs/WorkflowDefinition'},
             'referenced': {'$ref': '#/$defs/V13DraftRefs'}, 'warnings': {'type': 'array',
             'items': {'type': 'string', 'minLength': 0, 'maxLength': 512}, 'maxItems': 50}},
             'required': ['definition', 'referenced', 'warnings'], 'additionalProperties': False}
    result = validate_object(result, v13_schema(shape))
    _validate_definition(result['definition'])
    for key, actual in _definition_refs(result['definition']).items():
        if not set(actual) <= set(result['referenced'][key]):
            raise ValueError('模型遗漏定义中的引用')
    return result


# LLM: 白名单必须由宿主的作业快照提供，模型不能自报授权目录。
# 函数用途: 供通用作业终结口核对引用均属于该空间目录。
def validate_workflow_draft_references(result, context_facts):
    actual = _definition_refs(result['definition'])
    for key in ('skill_ids', 'script_ids', 'monitor_rule_ids'):
        allowed = set(context_facts[key])
        if not set(result['referenced'][key]) <= allowed or not set(actual[key]) <= allowed:
            raise ValueError('草案引用不在空间挂载目录')
    return result


# LLM: 没有输入 schema 的老脚本只接受原 io_contract 所声明的输入名。
# 函数用途: 读取或合成固定脚本的闭合输入 schema。
def _script_schema(row):
    schema = row.get('metadata', {}).get('input_schema')
    if isinstance(schema, dict) and schema.get('type') == 'object':
        return schema
    names = row.get('io_contract', {}).get('inputs', [])
    return {'type': 'object', 'properties': {name: {} for name in names},
            'required': list(names), 'additionalProperties': False}


# LLM: 同 owner 不等于已挂载；只交付原 resolver 的空间挂载事实与显式关联资源。
# 函数用途: 读取草案可引用的 Skill、固定脚本和监控器最小目录。
def workflow_draft_context(call):
    from ..gateway_parts.business_api_v13 import v13_call_frozen, v13_require_access
    space = _space(call)
    space_id = space['space_id']
    mounted = {row['package_ref'] for row in space.get('mounts', [])
               if row['enabled'] and row['availability']['state'] == 'available'}
    try:
        resolved = _data(v13_call_frozen(call, 'spaces.capabilities.resolve', {'space_id': space_id}))
    except HostAPIError as error:
        # 空挂载是空间已确认事实；冷入口没有能力解析宿主时仍可生成不引用 Skill 的草案。
        # 非空挂载无法核验就失败，不能把来源不可用谎报成挂载资源为空。
        if error.code != 'HOST_NOT_READY' or mounted:
            raise
        resolved = {'candidates': []}
    skills = [row['method_ref'] for row in resolved['candidates'] if row['kind'] == 'skill'
              and row['availability']['state'] == 'available'
              and ('mounted_first' in row['reason_codes'] or row['origin']['source_ref'] == space_id)]
    monitors = [row for row in v13_store_for(call).list('monitor_rules')
                if row.get('owner_id') == call.owner_id and row.get('space_id') == space_id]
    for row in monitors:
        v13_require_access(call, row)
    linked = {row.get('script_id') for row in monitors}
    scripts = []
    for row in ScriptStore(call.owner_home).list_scripts():
        meta = row.get('metadata', {})
        if row.get('owner_id') not in ('', call.owner_id, None) or not row.get('enabled', True):
            continue
        if (meta.get('space_id') == space_id or space_id in meta.get('space_ids', [])
                or meta.get('package_ref') in mounted or row['id'] in linked):
            scripts.append({'script_id': row['id'], 'name': row['name'], 'input_schema': _script_schema(row)})
    return {'space_id': space_id, 'skill_ids': sorted(set(skills)),
            'script_ids': sorted(row['script_id'] for row in scripts),
            'monitor_rule_ids': sorted(row['rule_id'] for row in monitors), 'scripts': scripts,
            'monitors': [{'rule_id': row['rule_id'], 'name': row['name'], 'script_id': row.get('script_id')}
                         for row in monitors]}


# LLM: 任务目录来自服务端，只作为模型事实；保存和运行仍分别走冻结动作。
# 函数用途: 向原空间助手派发结构化草案作业。
def draft_from_prompt(call):
    from ..gateway_parts.business_api_v13 import v13_start_model_job
    facts = workflow_draft_context(call)
    instruction = ('只输出 JSON 对象 {definition, referenced:{skill_ids,script_ids,monitor_rule_ids},warnings}。'
                   'definition 必须符合 WorkflowDefinition；只引用下列目录，不执行、不保存、不得扩展目录。\n'
                   + canonical_json({'prompt': call.params['prompt'], 'name': call.params.get('name'),
                                     'context_facts': facts}))
    return v13_start_model_job(call, 'workflow_draft', instruction, space_id=facts['space_id'],
                               context_facts=facts)


# LLM: 能力名必须是原声明的结构标识，不从描述文本或固定厂商词表推断。
# 函数用途: 将一条来源声明投影为可发现的实时动作。
def _live_row(source, source_ref, capability, name, schema, *, vendor_id=None,
              vendor_name=None, risk='high', approval=True, available=True, reason=None):
    from ..gateway_parts.business_api_v13 import v13_schema
    if not isinstance(capability, str) or re.fullmatch(r'[a-z][a-z0-9_]{0,31}', capability) is None:
        return None
    if risk not in ('low', 'medium', 'high', 'critical'):
        risk = 'high'
    row = {'action_ref': source + ':' + source_ref, 'capability': capability,
           'vendor_id': vendor_id, 'vendor_name': vendor_name, 'display_name': name,
           'source': source, 'source_ref': source_ref, 'input_schema': schema, 'risk_level': risk,
           'requires_approval': approval, 'available': available, 'unavailable_reason': reason}
    try:
        return validate_object(row, v13_schema({'$ref': '#/$defs/V13LiveAction'}))
    except HostAPIError:
        return None


# LLM: 插件目录只冷读本人和原公共安装，不初始化插件进程或取得执行权。
# 函数用途: 用原 PluginManagement 读取公开动作和对应工具声明。
def _plugin_rows(call):
    from ..capability.package_provider import _owner_from_home
    from ..path_access_policy import PathAccessPolicy
    from ..plugin_install_store import PluginInstallStore
    from ..plugin_management import PluginManagement, PluginManagementContext
    from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home
    owner = _owner_from_home(call.owner_home)
    context = PluginManagementContext(owner, call.subject_id, 'business', '', None, owner.home_dir,
        PathAccessPolicy.from_values(mode='normal', owner_scope_root=owner.home_dir),
        False, bool(getattr(call.context.host.config, 'enable_plugins', False)))
    manager = PluginManagement(context)
    catalog = manager.catalog()
    own = manager.installations.snapshot()
    public = (() if owner.identity == OwnerIdentity.local_main() else
              PluginInstallStore(resolve_owner_home(owner.root, OwnerIdentity.local_main())).snapshot())
    entries = {(row.manifest.plugin_id, row.installation_ref): row
               for row in (*own, *(row for row in public if row.installation_scope == 'public'))}
    rows, partial = [], False
    for plugin in catalog.plugins:
        entry = entries.get((plugin.plugin_id, plugin.installation_ref))
        if entry is None:
            continue
        tools = {tool.name: tool for tool in entry.manifest.tools}
        for action in plugin.actions:
            tool = tools.get(action.target) if action.kind == 'tool' else None
            if tool is None or tool.requested_effect == 'read_only':
                continue
            available = plugin.enabled and context.enabled and action.available
            row = _live_row('plugin', plugin.plugin_id + ':' + tool.name, action.name, action.summary,
                            tool.input_schema, vendor_id=plugin.plugin_id, vendor_name=plugin.plugin_id,
                            available=available, reason=None if available else 'plugin_unavailable')
            if row is not None:
                rows.append(row)
            else:
                partial = True
    return rows, partial


# LLM: 只读取现存授权快照和原代理，不连服务、不从名称前缀猜来源。
# 函数用途: 投影已发布 MCP 工具的原声明与可用性。
def _mcp_rows(call):
    from ..tooling.mcp_registration import MCPProxyTool
    registry = getattr(call.context.host, 'tools', None)
    if registry is None or registry.operation_owner_id != call.owner_id:
        return [], True
    snapshot = registry.runtime_snapshot()
    rows, partial = [], False
    for runtime in snapshot.runtimes:
        spec = runtime.model_spec
        tool = registry.tools.get(spec.name)
        if (not isinstance(tool, MCPProxyTool) or spec.hints.provider_id.startswith(('plugin:', 'package:'))
                or not runtime.exposure.model_visible):
            continue
        effect = tool.client.config.effect_for_tool(tool.remote_tool)
        if effect == 'read_only':
            continue
        available = runtime.availability.available
        row = _live_row('mcp', spec.name, tool.remote_tool, tool.remote_tool, spec.input_schema,
                        vendor_id=tool.client.config.name, vendor_name=tool.client.config.name,
                        available=available, reason=None if available else 'mcp_unavailable')
        if row is not None:
            rows.append(row)
        else:
            partial = True
    return rows, partial


# LLM: 来源失败不能变成空目录的完整成功；错误由运输层按只读失败处理。
# 函数用途: 汇总固定脚本、插件和既有 MCP 真实声明。
def _live_actions(call):
    from ..gateway_parts.business_api_v13 import v13_require_access
    v13_require_access(call)
    if call.params.get('space_id'):
        _space(call)
    rows, partial = [], False
    for script in ScriptStore(call.owner_home).list_scripts():
        if script.get('owner_id') not in ('', call.owner_id, None):
            continue
        meta = script.get('metadata', {})
        capabilities = meta.get('capabilities', [])
        if not isinstance(capabilities, list):
            partial = True
            continue
        risk = meta.get('risk_level', 'high' if script.get('access_targets') or script.get('credential_refs') else 'low')
        approval = meta.get('requires_approval')
        approval = approval if type(approval) is bool else risk in ('high', 'critical')
        for capability in capabilities:
            enabled = script.get('enabled', True)
            row = _live_row('script', script['id'], capability, script['name'], _script_schema(script),
                            vendor_id=meta.get('vendor_id'), vendor_name=meta.get('vendor_name'),
                            risk=risk, approval=approval, available=enabled,
                            reason=None if enabled else 'script_disabled')
            if row is not None:
                rows.append(row)
            else:
                partial = True
    plugins, plugins_partial = _plugin_rows(call)
    rows.extend(plugins)
    mcp, mcp_partial = _mcp_rows(call)
    rows.extend(mcp)
    unique = {(row['action_ref'], row['capability']): row for row in rows}
    return [unique[key] for key in sorted(unique)], partial or plugins_partial or mcp_partial


# LLM: 两个专用筛选动作共用原声明投影，筛选只读取结构字段。
# 函数用途: 按 capability 与不区分大小写的 vendor_id 分页发现动作。
def discover_live_actions(call):
    rows, partial = _live_actions(call)
    rows = [row for row in rows
            if (not call.params.get('capability') or row['capability'] == call.params['capability'])
            and (not call.params.get('vendor_id') or (row['vendor_id'] or '').casefold() == call.params['vendor_id'].casefold())]
    return _page(call, rows, partial=partial)


# LLM: 仅精确筛选声明能力，不改变就绪性或授权。
# 函数用途: 提供按能力发现的冻结命名处理器。
def live_actions_by_capability(call):
    return discover_live_actions(call)


# LLM: 厂商比较仅规范大小写，不扩展别名或猜厂商。
# 函数用途: 提供按厂商发现的冻结命名处理器。
def live_actions_by_vendor(call):
    return discover_live_actions(call)


# LLM: SpaceView 没有自创 datasource_bindings 字段；只沿原冻结工作流范围取访问声明。
# 函数用途: 收集该空间原生工作流和关联固定脚本的范围声明。
def _scoped_targets(call, space_id):
    from ..gateway_parts.business_api_v13 import v13_call_frozen
    result, cursor = [], None
    for index in range(100):
        params = {'space_id': space_id, 'limit': 100}
        if cursor is not None:
            params['cursor'] = cursor
        page = _data(v13_call_frozen(call, 'workflows.list', params))
        for row in page['items']:
            workflow = WorkflowStore(call.owner_home).get_workflow(row['workflow_ref'])
            if workflow is not None:
                descriptor = workflow.descriptor.to_dict()
                result.extend(descriptor['access_targets'])
                result.extend(descriptor['data_sources'])
        cursor = page['next_cursor']
        if cursor is None:
            break
    if cursor is not None:
        raise HostAPIError('LIMIT_EXCEEDED')
    linked = {row.get('script_id') for row in v13_store_for(call).list('monitor_rules')
              if row.get('owner_id') == call.owner_id and row.get('space_id') == space_id}
    for script in ScriptStore(call.owner_home).list_scripts():
        meta = script.get('metadata', {})
        if script.get('owner_id') not in ('', call.owner_id, None) or not script.get('enabled', True):
            continue
        if (meta.get('space_id') == space_id or space_id in meta.get('space_ids', [])
                or script['id'] in linked):
            result.extend(script.get('access_targets', []))
            result.extend(script.get('data_sources', []))
    return result


# LLM: 目标必须精确命中原空间绑定；任意 endpoint 或空 ID 不能扩大范围。
# 函数用途: 在不联系目标系统的前提下检查实时动作目标范围。
def _scope_check(call, selected):
    target = call.params.get('target')
    if target is None:
        return {'in_scope': True, 'reasons': []}
    if not target:
        return {'in_scope': False, 'reasons': ['目标缺少可核验的范围标识']}
    if not call.params.get('space_id'):
        return {'in_scope': False, 'reasons': ['目标检查需要明确业务空间']}
    space = _space(call)
    allowed = _scoped_targets(call, space['space_id'])
    keys = ('target_id', 'datasource_id', 'source_id', 'endpoint', 'host_id', 'host', 'ip', 'url')
    matches = []
    for row in allowed:
        fields = {**row.get('extra', {}), **row.get('query_scope', {}), **row}
        if 'source_id' in fields:
            fields['datasource_id'] = fields['source_id']
        if (any(key in target and target[key] == fields.get(key) for key in keys)
                and all(key in fields and value == fields[key] for key, value in target.items())):
            matches.append(row)
    return {'in_scope': bool(matches), 'reasons': [] if matches else ['目标不在该空间已声明的数据源或访问范围']}


# LLM: Schema 和范围只做纯预检；目录可见不签发执行或审批许可。
# 函数用途: 返回逐字段输入错误、范围结论及原审批要求。
def dry_run_live_action(call):
    rows, partial = _live_actions(call)
    selected = next((row for row in rows if row['action_ref'] == call.params['action_ref']), None)
    if selected is None:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    validation = validate_tool_input(call.params['inputs'], selected['input_schema'])
    errors = [{'path': issue.path[:256], 'code': 'INVALID_SCHEMA',
               'message': ('输入不符合 ' + issue.keyword)[:512]} for issue in validation.issues[:100]]
    if not selected['available']:
        errors.append({'path': '$', 'code': 'HOST_NOT_READY', 'message': '原动作当前不可用'})
    scope = _scope_check(call, selected)
    return {'action_ref': selected['action_ref'], 'valid': not errors and scope['in_scope'],
            'errors': errors[:100], 'scope_check': scope,
            'would_require_approval': selected['requires_approval'], 'side_effects': 'none'}


# LLM: 社区网络访问归已绑定 provider；缺方法或错误返回不能假装空列表。
# 函数用途: 取得一个明确可调用的社区目录端口。
def _community(call, method):
    from ..gateway_parts.business_api_v13 import v13_provider, v13_require_access
    v13_require_access(call)
    provider = v13_provider(call, 'community_catalog')
    if not callable(getattr(provider, method, None)):
        raise HostAPIError('HOST_NOT_READY', 503)
    return provider


# LLM: provider 的分页游标只原样透传，返回仍经本地闭合 schema 核验。
# 函数用途: 浏览社区剧本的公开元数据。
def list_community_playbooks(call):
    from ..gateway_parts.business_api_v13 import v13_schema
    provider = _community(call, 'list')
    page = provider.list('playbook', query=call.params.get('query', ''), tag=call.params.get('tag', ''),
                         cursor=call.params.get('cursor'), limit=call.params.get('limit', 50))
    result = {'items': page['items'], 'next_cursor': page['next_cursor'],
              'coverage': 'partial' if page['next_cursor'] is not None else 'complete'}
    for item in result['items']:
        if item.get('kind') != 'playbook' or item.get('format') != 'workflow':
            raise HostAPIError('HOST_NOT_READY', 503)
    return validate_object(result, v13_schema({'type': 'object', 'properties': {
        'items': {'type': 'array', 'items': {'$ref': '#/$defs/V13CommunityItem'}, 'maxItems': 100},
        'next_cursor': {'anyOf': [{'$ref': '#/$defs/Cursor'}, {'type': 'null'}]},
        'coverage': {'$ref': '#/$defs/Coverage'}},
        'required': ['items', 'next_cursor', 'coverage'], 'additionalProperties': False}))


# LLM: 冻结 get 是授权入口；完整正文仍读原 WorkflowStore，不另建正文权威。
# 函数用途: 取得可提交的现有工作流定义并核实可选空间范围。
def _authorized_definition(call):
    from ..gateway_parts.business_api_v13 import v13_call_frozen
    ref = call.params['workflow_id']
    try:
        view = _data(v13_call_frozen(call, 'workflows.get', {'workflow_ref': ref}))
    except KeyError as error:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404) from error
    if call.params.get('space_id'):
        space = _space(call)
        if view['scope']['scope_mode'] == 'spaces' and space['space_id'] not in view['scope']['space_ids']:
            raise HostAPIError('SPACE_SCOPE_MISMATCH', 404)
    definition = WorkflowStore(call.owner_home).get_workflow(ref)
    if definition is None or definition.descriptor.owner_id not in ('', call.owner_id):
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    return _validate_definition(_editable_definition(definition))


# LLM: 共享文档不能包含凭据引用或私有访问目标；原存储正文保持不变。
# 函数用途: 递归移除凭据引用和访问范围，并执行原秘密脱敏。
def _shareable(value):
    value = redact_sensitive_value(thaw(value))
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in ('credential_ref', 'access_target'):
                continue
            result[key] = [] if key in ('credential_refs', 'access_targets') else _shareable(item)
        return result
    if isinstance(value, list):
        return [_shareable(item) for item in value]
    return value


# LLM: 先完成本地预检再耐久占单外发；外发后未知不重试，回放仍复核当前读取权限。
# 函数用途: 脱敏提交社区剧本并记录真实 provider 回执。
def submit_community_playbook(call):
    from ..gateway_parts.business_api_v13 import v13_require_access
    provider = _community(call, 'submit')
    store = v13_store_for(call)
    fingerprint = _digest([call.action, call.params])
    submission_id = 'submission-' + _digest([call.owner_id, call.subject_id, call.idempotency_key])[:48]
    attempt = uuid.uuid4().hex
    definition = _authorized_definition(call)
    title = call.params.get('title', definition['descriptor']['name'])
    document = {'definition': _shareable(definition), 'title': title,
                'description': call.params.get('description', definition['descriptor'].get('summary', '')),
                'tags': call.params.get('tags', definition['descriptor'].get('tags', []))}
    v13_require_access(call)

    # LLM: Store 锁使同键并发者只有一个占单；回执校验先于任何外发或结果重放。
    # 函数用途: 固定一次且仅一次的外发尝试标识。
    def claim(records):
        return {'attempt': attempt}

    claimed = store.transaction('community_playbook_submissions', claim,
        idempotency_key=call.idempotency_key, params_digest=fingerprint)
    saved = store.get('community_playbook_submissions', submission_id)
    if saved is not None:
        v13_require_access(call, saved)
        return {'submission': _submission_view(saved)}
    if claimed['attempt'] != attempt:
        raise HostAPIError('OUTCOME_UNKNOWN', 503)
    v13_require_access(call)
    response = provider.submit('playbook', document)
    if (not isinstance(response, dict) or response.get('state') not in ('submitted', 'rejected')
            or not isinstance(response.get('submission_ref'), str) or not 1 <= len(response['submission_ref']) <= 256):
        raise HostAPIError('OUTCOME_UNKNOWN', 503)
    at = datetime.fromtimestamp(call.clock(), timezone.utc).isoformat()
    record = {'submission_id': submission_id, 'owner_id': call.owner_id, 'workflow_id': call.params['workflow_id'],
              'title': title, 'state': response['state'], 'submission_ref': response['submission_ref'],
              'submitted_at': at, 'revision': 1, 'created_at': at, 'updated_at': at}

    # LLM: 完成回执只保存公开提交事实，不保存外发正文或秘密。
    # 函数用途: 原子记录 provider 的准确结果。
    def complete(records):
        records[submission_id] = record
        return {'submission': _submission_view(record)}

    return store.transaction('community_playbook_submissions', complete,
        idempotency_key='complete-' + _digest([call.subject_id, call.idempotency_key])[:48],
        params_digest=fingerprint)


# LLM: 存储身份和修订留在服务端；公开响应严格按冻结投影。
# 函数用途: 裁剪提交回执的公共字段。
def _submission_view(row):
    return {key: row[key] for key in ('submission_id', 'workflow_id', 'title', 'state', 'submission_ref', 'submitted_at')}


# LLM: provider 只供定义；实际安装、幂等和范围仍走冻结 create。
# 函数用途: 校验社区剧本后保存为原生工作流，不运行它。
def install_community_playbook(call):
    from ..gateway_parts.business_api_v13 import v13_call_frozen
    provider = _community(call, 'get')
    store = v13_store_for(call)

    # LLM: 回执固定第一次看到的定义和空间，provider 改版不能使同键重放变更输入。
    # 函数用途: 在不写第二份工作流权威的前提下固定本次安装意图。
    def remember(records):
        definition = provider.get('playbook', call.params['playbook_id'])
        if definition is None:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        definition = _validate_definition(definition)
        space = _space(call)
        return {'definition': definition, 'space_id': space['space_id']}

    pinned = store.transaction('community_playbook_submissions', remember,
        idempotency_key='install-' + _digest([call.subject_id, call.idempotency_key])[:48],
        params_digest=_digest([call.action, call.params]))
    result = _data(v13_call_frozen(call, 'workflows.create', {'definition': pinned['definition'],
                   'scope': {'scope_mode': 'spaces', 'space_ids': [pinned['space_id']]}},
                   idempotency_key=call.idempotency_key))
    return {key: result[key] for key in ('workflow_ref', 'definition_revision')}
