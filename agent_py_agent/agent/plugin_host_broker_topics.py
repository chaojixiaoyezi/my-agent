# LLM: 公开只读主题适配；owner 由调用方从认证 grant 提取，不接受 body 中的 owner 或控制参数。
# 模块用途: 为插件批量返回资产摘要、拓扑和遥测投影，并在同一 owner 中适配资产管理。
from dataclasses import fields, replace
from os.path import abspath
from pathlib import Path

TOPIC_ACTIONS = {'assets_summary': 'assets.list', 'topology': 'assets.topology',
    'recent_tasks': 'telemetry.tasks.list', 'recent_tool_calls': 'telemetry.tool_calls.list',
    'tool_stats': 'telemetry.tool_stats', 'workflow_runs': 'telemetry.workflows.runs.list'}


# LLM: 在任何查询之前校验全部主题；错误信封保留，不将失败当作空数据。
# 函数用途: 查询当前可信 owner 的公开主题，不提供写入或执行能力。
def query_broker_topics(owner_home, owner_id, body, *, grant=None):
    from .gateway_parts.plugin_broker_api import PluginBrokerService
    topics = body.get('topics') if isinstance(body, dict) else None
    if (not isinstance(topics, list) or not topics
            or any(not isinstance(topic, str) or topic not in TOPIC_ACTIONS for topic in topics)):
        raise ValueError('topics 必须是非空的公开主题数组')
    if any(key in body for key in ('owner_id', 'owner_home', 'is_admin', 'admin')):
        raise ValueError('主题查询不接受身份字段')
    service = (PluginBrokerService.from_grant(grant) if grant is not None
               else PluginBrokerService(owner_home, owner_id=owner_id))
    return {topic: service.dispatch(TOPIC_ACTIONS[topic]) for topic in dict.fromkeys(topics)}


# LLM: 权限只取宿主传入Grant，不从请求体或资产记录推导；缺失或畸形声明一律无源码权限。
# 函数用途: 判断外部插件是否显式声明源码读取能力。
def grant_asset_source(grant):
    activation = getattr(grant, 'activation', {})
    permissions = activation.get('host_api', ()) if isinstance(activation, dict) else ()
    return (isinstance(permissions, (tuple, list, set, frozenset))
            and any(item in permissions for item in ('assets.source', 'write', 'execute')))


# LLM: 仅对资产投影应用，绝不修改权威记录；refs中的绝对路径按词法规范化，域外路径不回显。
# 函数用途: 移除宿主入口路径，隐藏未获授权的源码，递归输出逻辑引用。
def project_asset(value, home, include_source=True, in_refs=False):
    from .gateway_parts.plugin_broker_api import public_payload
    value = public_payload(value)
    if isinstance(value, dict):
        result = {key: project_asset(item, home, include_source, in_refs or key == 'refs')
                  for key, item in value.items() if key != 'entrypoint_path'}
        if not include_source:
            for key in ('code', 'body_markdown'):
                if key in result:
                    result[key], result['source_redacted'] = '', True
        return result
    if isinstance(value, list):
        return [project_asset(item, home, include_source, in_refs) for item in value]
    if in_refs and isinstance(value, str) and value.startswith('/'):
        try:
            return Path(abspath(value)).relative_to(Path(abspath(home))).as_posix()
        except ValueError:
            return '[external-path]'
    return value


# LLM: 仅过滤存储附加字段，不删标准提取 key；实际字段校验由 AssetDescriptor 负责。
# 函数用途: 将多个权威存储的输出统一成标准资产描述符。
def _descriptor(value):
    from .assets.descriptor import AssetDescriptor
    if hasattr(value, 'descriptor'):
        return value.descriptor
    if isinstance(value, AssetDescriptor):
        return value
    names = {field.name for field in fields(AssetDescriptor)}
    return AssetDescriptor.from_dict({key: item for key, item in value.items() if key in names})


# LLM: 只读当前 owner 的四个权威来源，不扫描别的用户；标准身份不能取自资产正文。
# 函数用途: 生成当前用户资产的完整拓扑并计算反向引用。
def asset_topology(home, owner_id, root=None):
    from .assets.monitor_adapter import list_monitor_descriptors
    from .assets.script_store import ScriptStore
    from .assets.skill_adapter import list_owner_skill_descriptors
    from .assets.topology import build_asset_topology
    from .assets.workflow_store import WorkflowStore
    rows = [*ScriptStore(home).list_scripts(), *WorkflowStore(home).list_workflows(),
            *list_monitor_descriptors(home), *list_owner_skill_descriptors(home)]
    descriptors = [replace(_descriptor(row), owner_id=owner_id) for row in rows]
    return build_asset_topology(descriptors, root_key=root)


# LLM: Skill 路径沿适配器同序发现并核对元数据ID；不把ID当目录，不接受额外根。
# 函数用途: 安全读取本人任意嵌套技能正文，兼容自定义ID。
def _skill_body(home, asset_id):
    from .assets.skill_adapter import _skill_paths, extract_skill_descriptor
    from .common.nofollow_fs import read_text_beneath
    home = Path(home).absolute()
    for root in (home / 'skills', home / 'learned'):
        for path in _skill_paths(root):
            if extract_skill_descriptor(path)['id'] == asset_id:
                return read_text_beneath(home, path.relative_to(home).parts)
    raise FileNotFoundError


# LLM: 同一投影用于直接查询与Broker；外部Grant优先裁决源码权限，缺省仅供内部可信调用。
# 函数用途: 返回不带宿主路径且符合源码权限的资产数据。
def query_assets(home, owner_id, action, payload, *, grant=None, include_asset_source=True):
    allowed = include_asset_source and (grant is None or grant_asset_source(grant))
    return project_asset(_query_assets(home, owner_id, action, payload, allowed), home, allowed)


# LLM: kind 白名单仅表示本协议四种资产；找不到明示 None，未授权时不加载脚本和Skill正文。
# 函数用途: 将真实存储详情与拓扑反向引用合并。
def _query_assets(home, owner_id, action, payload, include_source):
    if action == 'assets.topology':
        return asset_topology(home, owner_id, payload.get('root'))
    kind = payload.get('kind', 'all')
    if kind not in ('all', 'monitor', 'workflow', 'script', 'skill'):
        raise ValueError('非法资产类型')
    nodes = asset_topology(home, owner_id)['nodes']
    rows = [row for row in nodes if not row.get('missing') and (kind == 'all' or row['kind'] == kind)]
    if action == 'assets.list':
        return rows
    if kind == 'all':
        raise ValueError('读取资产需要明确 kind')
    row = next((item for item in rows if item['id'] == payload['id']), None)
    if row is None:
        return None
    if kind == 'script':
        from .assets.script_store import ScriptStore
        if not include_source:
            return {**row, 'code': '', 'source_redacted': True}
        return {**ScriptStore(home).get_script(row['id']), **row}
    if kind == 'workflow':
        from .assets.workflow_store import WorkflowStore
        from .gateway_parts.plugin_broker_api import public_payload
        detail = public_payload(WorkflowStore(home).get_workflow(row['id']))
        return {**detail, **row}
    if kind == 'skill':
        return {**row, 'body_markdown': _skill_body(home, row['id']) if include_source else ''}
    from .assets.monitor_adapter import get_monitor_descriptor
    return {**get_monitor_descriptor(home, row['id']), **row}


# LLM: 资产元数据不能伪造归属，写入标准描述符固定 authenticated owner；所有路径由存储校验。
# 函数用途: 从插件请求构造本人的资产描述符。
def _owned_descriptor(payload, owner_id):
    data = dict(payload['descriptor'])
    if data.get('owner_id', owner_id) != owner_id:
        raise PermissionError
    if data.get('kind') != payload['kind']:
        raise ValueError('类型不匹配')
    data['owner_id'] = owner_id
    from .assets.descriptor import AssetDescriptor
    return AssetDescriptor.from_dict(data)


# LLM: 工作流绑定按 step_id 合并，保留旧步骤；Skill 绑定走权威 store，不直接改 JSON。
# 函数用途: 更新工作流步骤或给指定步骤附加 Skill。
def _bind_workflow(store, payload):
    from .assets.workflow_store import WorkflowStepSpec
    if 'skill_id' in payload and 'steps' in payload:
        raise ValueError('不能混合两种绑定形式')
    if 'skill_id' in payload:
        return store.attach_skill_to_step(payload['id'], payload['step_id'], payload['skill_id'])
    workflow = store.get_workflow(payload['id'])
    if workflow is None:
        raise ValueError('工作流不存在')
    steps = {step.step_id: step for step in workflow.steps}
    for value in payload['steps']:
        step = WorkflowStepSpec(**value)
        steps[step.step_id] = step
    return store.save_workflow(replace(workflow, steps=tuple(steps.values())))


# LLM: 所有变更只委托权威存储；不执行保存的脚本，不维护第二套资产账本。
# 函数用途: 保存资产、热更新脚本数据源、绑定工作流与监控。
def mutate_assets(home, owner_id, action, payload):
    from .assets.script_store import ScriptStore
    from .assets.workflow_store import WorkflowDefinition, WorkflowStepSpec, WorkflowStore
    kind = payload['kind']
    if action == 'assets.update_sources' and kind == 'script':
        return ScriptStore(home).update_data_sources(payload['id'], payload['source_id'],
            query_scope=payload.get('query_scope'), candidate=payload.get('candidate'))
    if kind == 'monitor' and action in ('assets.save', 'assets.bind'):
        return _save_monitor(home, owner_id, action, payload)
    if action == 'assets.bind' and kind == 'workflow':
        return _bind_workflow(WorkflowStore(home), payload)
    if action != 'assets.save' or kind not in ('script', 'workflow'):
        raise ValueError('不支持该资产变更')
    descriptor = _owned_descriptor(payload, owner_id)
    if kind == 'script':
        options = {key: payload[key] for key in ('entrypoint', 'interpreter', 'timeout_seconds') if key in payload}
        return ScriptStore(home).save_script(descriptor, payload['code'], **options)
    steps = tuple(WorkflowStepSpec(**step) for step in payload.get('steps', ()))
    return WorkflowStore(home).save_workflow(WorkflowDefinition(descriptor, steps, payload.get('body_markdown', '')))


# LLM: bind保留未指定字段，显式空列表用于解除绑定；save仍是完整覆盖，不修改Scheduler。
# 函数用途: 为监控动态绑定保留名称、计划与其他类型引用。
def _save_monitor(home, owner_id, action, payload):
    from .assets.monitor_adapter import get_monitor_descriptor, save_monitor_binding
    values = {}
    if action == 'assets.bind':
        try:
            old = get_monitor_descriptor(home, payload['id'])
        except FileNotFoundError:
            old = None
        if old is not None:
            values = {key: old[key] for key in ('name', 'summary', 'schedule_spec',
                      'access_targets', 'data_sources', 'credential_refs')}
            values.update({kind + '_ids': old['refs'][kind + 's'] for kind in ('workflow', 'script', 'skill')})
    values.update({key: value for key, value in payload.items() if key not in ('kind', 'id')})
    return {**save_monitor_binding(home, payload['id'], **values), 'owner_id': owner_id}


# LLM: 复用同一账本与nearest-rank算法；脚本分组只统计scripts/run，分页读全而不截断历史。
# 函数用途: 在原工具统计旁补充每个固化脚本的调用统计。
def tool_and_script_stats(ledger, payload):
    from .observability.telemetry_ledger import _stats
    result, groups, cursor = ledger.aggregate_tool_stats(**payload), {}, 0
    while True:
        rows = ledger.query_tool_calls(tool_name='scripts', limit=1000, cursor=cursor, **payload)
        for row in rows:
            args = row['args_summary']
            identifier = args.get('script_id')
            if args.get('action') == 'run' and isinstance(identifier, str) and identifier:
                groups.setdefault(identifier, []).append(row)
        cursor += len(rows)
        if len(rows) < 1000:
            break
    return {**result, 'scripts': {key: _stats(rows) for key, rows in sorted(groups.items())}}
