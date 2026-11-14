# LLM: 工作流只由可信Owner的领域Store和Engine执行；工具不自建运行账本，不从正文猜断点或授权。
# 模块用途: 提供工作流CRUD、绑定与运行；直接使用纯schema及资产完整定义构造，不接管执行协议。
from __future__ import annotations

import copy
import json
import time
from contextlib import nullcontext
from dataclasses import fields, replace
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from ..assets import AssetPostCommitReadbackError
from ..assets.asset_dependents import (
    AssetActionError,
    bound_monitors,
    dependents,
    model_metadata,
    require_revision,
    workflow_projection,
)
from ..assets.descriptor import AssetDescriptor, asset_descriptor_from_definition
from ..assets.revision_history import (
    blob_get,
    delete_draft,
    get_draft,
    latest_version,
    list_versions,
    save_draft,
)
from ..assets.skill_adapter import get_owner_skill_descriptor, list_owner_skill_descriptors
from ..assets.version_diff import diff_versions, field_changes, load_snapshot
from ..assets.version_hook import _VERSION_OPERATION
from ..assets.workflow_package_template import (
    WorkflowTemplateError,
    decode_package_workflow,
    issue_package_workflow_source,
)
from ..assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStore,
    validate_definition_eligibility,
    with_workflow_step_refs,
)
from ..capability.skills import split_skill_frontmatter
from ..plugin_host.errors import HostAPIError
from ..workflows.business_scope import (
    WORKFLOW_ACTION_FIELDS,
    WORKFLOW_READ_ACTIONS,
    WorkflowPolicyRefusal,
    validate_business_request,
    workflow_list_matches,
)
from ..workflows.models import redact_bearer_values
from ..workflows.review_receipts import pending_review_error
from ..workflows.run_store import WorkflowExecutionError
from .models import BaseTool
from .scripts_tool import (
    _annotate_schema_properties,
    _annotate_shared_asset_defs,
    _dispatch,
    _outcome,
    _owner,
    _plain,
    _policy,
    _post_commit_failure,
    _validate,
)
from .tool_spec import _spec


# LLM: 兼容当前组合标记与历史快照保留的基础 pin；只判定身份，不读取或修改 Store。
# 函数用途: 识别不能通过公开工作流回滚写入的宿主组合。
def _host_managed_composition(metadata):
    return isinstance(metadata, dict) and (isinstance(metadata.get('composition'), dict) or 'base_workflow_pin' in metadata)


# LLM: 完整定义的目标id/kind与标准Key交资产领域统一校验，引用派生沿原合同，不改变_plain顺序。
# 函数用途: 规范工具保存请求，不另维护模式或错误策略副本。
def _definition(value, workflow_id):
    value = _plain(value)
    value['descriptor'] = _plain(asset_descriptor_from_definition(value['descriptor'], workflow_id, 'workflow'))
    return with_workflow_step_refs(WorkflowDefinition.from_any(value))


# LLM: 仅字符串叶子是匹配值，映射的键和序列化标点不构成技能标签。
# 函数用途: 展开技能描述字段的实际文本值。
def _skill_text(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        value = value.values()
    elif not isinstance(value, (list, tuple)):
        return []
    return [text for item in value for text in _skill_text(item)]


# LLM: 必需引用逐项经同一目录精确读取，快照存在时不能补扫；标签仅用于附加发现，缺引用绝不退回原ID伪成功。
# 函数用途: 解析当前Owner与受限快照中真实存在的Skill引用。
def _resolve_owner_skills(owner_home, step, context, *, workspace_root=None, skill_snapshot=None):
    ids, tags = getattr(step, 'skill_ids', ()), getattr(step, 'skill_tags', ())
    options = {'workspace_root': workspace_root, 'skill_snapshot': skill_snapshot}
    found = []
    for reference in ids:
        get_owner_skill_descriptor(owner_home, reference, **options)
        found.append(reference)
    if not tags:
        return list(dict.fromkeys(found))
    for descriptor in list_owner_skill_descriptors(owner_home, **options):
        text = ' '.join(text for key in ('name', 'summary', 'refs', 'tags') for text in _skill_text(descriptor.get(key)))
        if any(tag in text for tag in tags):
            reference = descriptor.get('metadata', {}).get('stable_id') or descriptor['id']
            if reference not in found:
                found.append(reference)
    return list(dict.fromkeys(found))


# LLM: 固定getter协议，正文/来源/hash均由同一个授权目录核验，不猜未来参数或重新拼路径读取。
# 函数用途: 读取一项受权Skill的完整详情。
def _skill_descriptor(owner_home, skill_id, *, workspace_root=None, skill_snapshot=None):
    return get_owner_skill_descriptor(owner_home, skill_id, include_body=True,
                                     workspace_root=workspace_root, skill_snapshot=skill_snapshot)


# LLM: 只用于只读preview，显式传递workspace_root与skill_snapshot范围；正文先脱敏再截断，缺Skill仍抛错。
# 函数用途: 为一个分析步骤生成200字以内的静态技能预览。
def _analysis_summary(step, resolved_skills, context, *, owner_home, workspace_root=None, skill_snapshot=None):
    summaries = []
    for skill_id in resolved_skills:
        descriptor = _skill_descriptor(owner_home, skill_id,
                                       workspace_root=workspace_root, skill_snapshot=skill_snapshot)
        safe = redact_bearer_values(descriptor)
        _, body = split_skill_frontmatter(safe.get('body_markdown', ''))
        summaries.append({key: safe.get(key, '') for key in ('id', 'name', 'summary')} | {
            'procedure_preview': body.strip()[:200]})
    return {'step_id': step.step_id, 'mode': step.mode, 'resolved_skills': list(resolved_skills),
            'analyzed_keys': sorted((context or {}).keys()), 'status': 'previewed',
            'llm_involved': False, 'skill_summaries': summaries}


_WORKFLOW_DEFINITION_DESCRIPTIONS: dict[str, str] = {
    'descriptor': '工作流标准资产描述符对象（含 name、access_targets、data_sources、credential_refs、io_contract 等）。',
    'steps': '工作流步骤定义数组。',
    'body_markdown': '工作流业务背景与操作说明 Markdown 正文。',
    'enabled': '工作流定义是否启用。',
    'edges': '可选显式步骤有向边数组。',
    'schedule_ids': '关联的定时计划 ID 列表。',
    'datasource_ids': '工作流级关联的数据源代号列表。',
}

_WORKFLOW_EDGE_DESCRIPTIONS: dict[str, str] = {
    'from_step_id': '有向边起点步骤 ID。',
    'to_step_id': '有向边终点步骤 ID。',
    'label': '有向边分支条件或展示说明标签。',
}

_WORKFLOW_STEP_DESCRIPTIONS: dict[str, str] = {
    'step_id': '工作流内唯一步骤标识。',
    'name': '步骤显示名称。',
    'description': '步骤用途与执行要求说明。',
    'script_id': '脚本模式绑定的固化脚本资产 ID。',
    'skill_ids': '分析或混合模式精确绑定的 Skill 稳定引用数组。',
    'skill_tags': '按标签匹配当前 owner 可用 Skill 的补充标签数组。',
    'on_error': '步骤失败策略（如 fail、skip、escalate_to_agent）。',
    'inputs_map': '步骤入参映射字典（引用前序 step_id 或 $inputs 的输出字段）。',
    'timeout_seconds': '单步运行超时秒数。',
    'next_step_ids': '显式下游步骤 ID 数组。',
    'depends_on': '前置依赖步骤 ID 数组。',
    'ui_position': '可视化画布节点位置坐标对象。',
    'foreach': '数组分批迭代执行配置对象。',
    'retry_policy': '步骤瞬态错误重试策略对象。',
    'tool_spec': 'tool_call 模式调用的原生工具名称与参数规格对象。',
    'notification_spec': '步骤结构化通知投递规格对象。',
}

_WORKFLOW_STEP_NESTED_DESCRIPTIONS: dict[str, dict[str, str]] = {
    'ui_position': {
        'x': '画布节点横坐标数值。',
        'y': '画布节点纵坐标数值。',
    },
    'foreach': {
        'from': '待遍历集合来源引用或字段路径。',
        'batch_size': '每批处理的记录数量。',
        'max_parallel': '最大并行批次上限。',
        'as': '当前元素或批次注入步骤输入的键名。',
        'max_batch_tokens': '单批估算最大 Token 预算上限。',
        'on_batch_error': '单批执行失败时的处理策略。',
        'mode': '集合分批执行模式。',
    },
    'retry_policy': {
        'max_attempts': '最大尝试总次数。',
        'delay_seconds': '首次重试间隔秒数。',
        'backoff_factor': '连续重试退避乘数。',
        'retryable_codes': '允许触发自动重试的错误码数组。',
    },
    'tool_spec': {
        'tool_name': '要调用的目标原生工具名。',
        'arguments': '传给目标工具的静态 JSON 参数对象。',
        'arguments_map': '从上游步骤或 $inputs 映射的工具参数字典。',
        'idempotent': '该工具调用是否声明为幂等操作。',
    },
    'notification_spec': {
        'channel': '通知目标通道标识。',
        'target': '已授权通知目标引用标识。',
        'template': '通知消息模板标识或格式说明。',
    },
}


def _resolve_local_def(definitions: dict, node: object) -> dict | None:
    if not isinstance(node, dict):
        return None
    ref = node.get('$ref')
    if isinstance(ref, str) and ref.startswith('#/$defs/'):
        target = definitions.get(ref[len('#/$defs/'):])
        if isinstance(target, dict):
            return target
    return node


def _typed_definition_spec(spec):
    """复用公开定义字段向模型说明真实 JSON 类型；Owner 旧字段仍由原构造器裁决。"""
    from ..plugin_host.business_action_specs import standalone_schema
    from .business_spaces_tool import _model_projection

    schema = copy.deepcopy(spec.input_schema)
    schema['properties']['action']['description'] = (
        '工作流动作：list/get/inspect_run/preview/versions/diff/impact/status 及对应业务读动作为只读查询；'
        'save/draft/publish/rollback/bind_skill/unbind_skill 维护工作流定义与版本，run/resume/delete 执行、恢复或删除。'
    )
    public = _model_projection(standalone_schema({'$ref': '#/$defs/WorkflowDefinition'}))
    definitions = public.pop('$defs')
    _annotate_shared_asset_defs(definitions)
    definition = definitions['WorkflowDefinition']
    descriptor = definitions['WorkflowDescriptorInput']
    # 原生 save 的 id/kind 来自 workflow_id；不能把业务创建入口的必填条件扩大到旧入口。
    descriptor['required'] = [key for key in descriptor['required'] if key not in ('id', 'kind')]
    descriptor['additionalProperties'] = True
    definition['additionalProperties'] = True
    _annotate_schema_properties(definition, _WORKFLOW_DEFINITION_DESCRIPTIONS)
    edges_prop = definition.get('properties', {}).get('edges')
    if isinstance(edges_prop, dict):
        edge_item = _resolve_local_def(definitions, edges_prop.get('items'))
        if edge_item is not None:
            _annotate_schema_properties(edge_item, _WORKFLOW_EDGE_DESCRIPTIONS)
    _annotate_schema_properties(definitions['WorkflowStepDefinition'], _WORKFLOW_STEP_DESCRIPTIONS)
    step_fields = definitions['WorkflowStepDefinition']['properties']
    for nested_key, nested_map in _WORKFLOW_STEP_NESTED_DESCRIPTIONS.items():
        nested_prop = step_fields.get(nested_key)
        target_node = _resolve_local_def(definitions, nested_prop)
        if target_node is not None:
            _annotate_schema_properties(target_node, nested_map)
    definitions['WorkflowStepDefinition']['properties']['condition']['description'] = (
        '结构化条件优先：{"op":"gte","ref":{"step_id":"$inputs","key":"count"},"value":2}；'
        '支持eq/neq/gt/gte/lt/lte/in/not_in/exists/not_exists/regex，数值须传JSON数值。'
        '组合使用{"op":"and","conditions":[条件对象,...]}或op=or；'
        'regex的value是正则文本。兼容旧{step_id,key,equals}对象；不要猜字符串表达式语法。')
    step_fields['mode']['description'] = ('save成功只证明定义可保存，不保证宿主有该mode的正式执行器。'
        'condition/condition_branch/script_transform/script/approval_wait默认仅供debug_step，'
        '除非宿主明确安装对应正式执行器。条件分支正式运行时，把结构化condition直接放到已有正式步骤'
        '（如tool_call、deterministic_script、llm_skill_analysis）上；不要创建独立condition节点再run。'
        '无文件写入的正式例：{"step_id":"check","name":"条件读取当前代理状态","mode":"tool_call",'
        '"condition":{"op":"gt","ref":{"step_id":"$inputs","key":"n"},"value":3},'
        '"tool_spec":{"tool_name":"list_agents","arguments":{}}}；须先核当前工具权限。'
        '纯节点演练用debug_step并明确allow_side_effects=false。subworkflow调用已保存子流程。')
    step_fields['subworkflow_ref']['description'] = ('子流程稳定ID，如item-amount或workflow:item-amount；'
        '不要把@版本拼入ID。固定历史版本用config.definition_version。')
    step_fields['config']['description'] = (step_fields['config'].get('description', '') +
        ' mode=subworkflow时，可用definition_version指定workflows(action=versions)返回的整数version，'
        '如{"definition_version":2}；也可用pinned_definition_ref核对完整64位摘要，'
        '摘要必须取实际版本回执，格式示意如{"pinned_definition_ref":"' + 'a' * 64 + '"}。'
        '两者同时给出时，摘要须匹配该历史版本；仅给摘要时核对当前子流程。'
        '这些键放在当前步骤config内，不放步骤根或ID内；其它模式的config仍按原执行器扩展。')
    schema['properties']['definition'] = {'$ref': '#/$defs/WorkflowDefinition',
        'description': '真实JSON对象；steps/access_targets/data_sources/credential_refs必须是数组，'
                       'io_contract/inputs_map/config/tool_spec必须是对象，不能填字符串或序列化JSON。'
                       '例如 {"descriptor":{"name":"巡检","access_targets":[],"data_sources":[], '
                       '"credential_refs":[],"io_contract":{}},"steps":[]}。'
                       '步骤字段和类型沿公开工作流定义；扩展配置由原运行时校验。'}
    schema['$defs'] = definitions
    from .input_schema import inline_local_schema_refs

    schema['properties']['definition'] = inline_local_schema_refs(schema, schema['properties']['definition'])
    hints = replace(
        spec.hints,
        use_cases=(
            '定义、保存、预览与执行包含确定性脚本、原生工具调用或 Skill 分析的多步骤可恢复工作流',
            '按 workflow_run_id 只读检查运行轨迹与产物（inspect_run），并在暂停或人工修复后用 resume 断点续跑',
            '管理工作流草稿发布（draft/publish）、历史版本对比与影响评估（versions/diff/impact）及安全回滚（rollback）',
        ),
        avoid_when=(
            '仅注册或单次运行单个确定性提取脚本、无需多步编排与断点恢复时用 scripts',
            '仅在当前回合把几个常规工具按固定依赖串联成一次性短流水线、无需持久化工作流资产时用 tool_program',
            '需要按未来时间点、固定间隔、cron 或飞书群事件建立持久周期触发计划时用 schedule',
        ),
        examples=(
            '{"action":"list"}',
            '{"action":"inspect_run","workflow_run_id":"run_001"}',
            '{"action":"save","workflow_id":"daily_check","definition":{"descriptor":{"name":"日常巡检","access_targets":[],"data_sources":[],"credential_refs":[],"io_contract":{}},"steps":[]}}',
        ),
    )
    return replace(spec, input_schema=schema, hints=hints, schema_hash='')


def _schema_save_source(spec):
    from ..capability.package_resources import package_resource_reference_schema
    from ..contracts.tool_input_schema import EXCLUSIVE_ARGUMENT_GROUPS_KEY

    schema = copy.deepcopy(spec.input_schema)
    schema['properties']['source_ref'] = package_resource_reference_schema()
    schema[EXCLUSIVE_ARGUMENT_GROUPS_KEY] = [['definition', 'source_ref']]
    schema['oneOf'] = [
        {'properties': {'action': {'const': 'save'}}, 'required': ['workflow_id'],
         'oneOf': [{'required': ['definition']}, {'required': ['source_ref']}]},
        {'properties': {'action': {'enum': [action for action in schema['properties']['action']['enum']
                                          if action != 'save']}}}]
    schema['properties']['source_ref']['description'] = (
        '仅action=save：从已授权包的严格JSON工作流模板派生到独立新Owner workflow_id；'
        '与definition互斥。原样使用skill_search的完整九字段source_ref。'
        '不覆盖现有工作流、不安装、不执行包脚本；来源及成员摘要由宿主固定，不能手填metadata伪造。')
    return replace(spec, input_schema=schema, schema_hash='')


# LLM: 路径由构造器Owner固定；纯schema沿tool_spec，运行/恢复/删除仍保持原危险动作分类。
# 类用途: 对Agent暴露可管理和恢复的工作流工具。
class WorkflowsTool(BaseTool):
    ACTIONS = ('list', 'get', 'save', 'delete', 'bind_skill', 'unbind_skill', 'run', 'resume', 'inspect_run', 'preview',
               'versions', 'diff', 'impact', 'rollback', 'draft', 'publish', 'status', *WORKFLOW_ACTION_FIELDS)
    READ_ONLY_ACTIONS = ('list', 'get', 'inspect_run', 'preview', 'versions', 'diff', 'impact', 'status', *WORKFLOW_READ_ACTIONS)
    model_spec = _spec('workflows', '管理Owner工作流、脚本/Skill引用、运行与断点续跑。步骤输出_pause_for_resume=true可暂停，再用resume+patched_step_output续跑。'
        '业务任务的脚本将交付文件写入环境变量MY_AGENT_RUN_OUT指向的目录；从inspect_run的steps[].output_artifacts取得artifact_id和sha256，按原版本交付。',
        list(ACTIONS), {
            'expected_revision': {'type': ['integer', 'null'], 'minimum': 0,
                'description': '乐观锁期望整数 revision（更新、发布、回滚或业务写动作传精确整数版本防覆盖）。'},
            'version': {'type': ['integer', 'null'], 'minimum': 1,
                'description': '目标历史整数版本号（>= 1）。'},
            'from_version': {'type': ['integer', 'null'], 'minimum': 1,
                'description': 'diff 对比的起始整数版本号（>= 1，diff 必填）。'},
            'to_version': {'type': ['integer', 'null'], 'minimum': 1,
                'description': 'diff 对比的目标整数版本号或 rollback 要恢复到的历史版本号（>= 1）。'},
            'summary_line': {'type': 'string', 'maxLength': 120,
                'description': '版本变更简短摘要说明（最长 120 字符）。'},
            'include_monitors': {'type': 'boolean',
                'description': 'impact/status 查询时是否一并包含关联监控引用。'},
            'idempotency_key': {'type': 'string', 'minLength': 1, 'maxLength': 256,
                'description': '业务写动作或触发调用的客户端幂等键（1 至 256 字符）。'},
            'workflow_ref': {'type': 'string', 'minLength': 1, 'maxLength': 128,
                'description': '业务工作流动作指定的工作流稳定引用（1 至 128 字符）。'},
            'space_id': {'type': 'string', 'minLength': 1, 'maxLength': 128,
                'description': '关联或筛选的业务空间稳定 ID（1 至 128 字符）。'},
            'project_ref': {'type': 'string', 'minLength': 1, 'maxLength': 128,
                'description': '关联或筛选的业务项目引用标识（1 至 128 字符）。'},
            'resolution_ref': {'type': 'string', 'minLength': 1, 'maxLength': 128,
                'description': '人工裁决或异常恢复关联引用 ID（1 至 128 字符）。'},
            'cursor': {'type': 'string', 'minLength': 1, 'maxLength': 2048,
                'description': '列表查询续读的不透明分页游标字符串。'},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100,
                'description': '列表或历史版本单次最多返回条数（1 至 100）。'},
            'query': {'type': 'string', 'maxLength': 256, 'description': '列表按工作流显示名或ID做区分大小写的包含匹配；空串不过滤'},
            'scope_mode': {'type': 'string', 'enum': ['owner_global', 'spaces', 'unassigned', 'all'], 'description': '列表范围过滤；省略或all不过滤'},
            'lifecycle': {'type': 'string', 'enum': ['active', 'recycled', 'all'], 'description': '显式过滤工作流生命周期；原生list省略时全部，业务workflows.list省略时active'},
            'protected': {'type': 'boolean',
                'description': 'workflows.protection.set 设置是否保护工作流免于自动回收清理。'},
            'policy': {'type': 'object',
                'description': 'workflows.cleanup.policy.set 更新的工作流清理治理策略对象。'},
            'concurrency_policy': {'type': 'string', 'enum': ['allow', 'skip_if_running', 'queue'],
                'description': 'run/resume并发策略，默认allow；skip_if_running跳过重叠，queue有界FIFO等待当前执行释放'},
            'scope': {
                'type': 'object',
                'description': 'workflows.scope.set 设置的工作流可见与归属空间范围对象。',
                'properties': {
                    'scope_mode': {
                        'type': 'string',
                        'enum': ['owner_global', 'spaces', 'unassigned'],
                        'description': '工作流范围模式：owner_global（全局）、spaces（指定业务空间）或 unassigned（未分配）。',
                    },
                    'space_ids': {
                        'type': 'array',
                        'items': {'type': 'string', 'minLength': 1, 'maxLength': 128},
                        'description': '当 scope_mode="spaces" 时绑定的业务空间 ID 列表（其它模式传 []）。',
                    },
                },
                'required': ['scope_mode', 'space_ids'],
                'additionalProperties': False,
            },
            'allow_script_drift': {'type': 'boolean', 'description': '显式确认恢复采用当前脚本版本'},
            'workflow_id': {'type': ['string', 'null'],
                'description': '原生工作流资产唯一稳定 ID（除 list/status/resume/inspect_run 外必填）。'},
            'step_id': {'type': 'string',
                'description': '工作流内目标步骤唯一 ID（bind_skill/unbind_skill 等动作使用）。'},
            'skill_id': {'type': 'string',
                'description': 'bind_skill/unbind_skill 针对的 Skill 稳定引用 ID。'},
            'workflow_run_id': {'type': 'string',
                'description': '工作流运行实例唯一 ID（resume/inspect_run 必填）。'},
            'from_step_id': {'type': 'string',
                'description': 'resume 断点续跑时可选指定的起始重跑步骤 ID。'},
            'definition': {'type': 'object', 'description': (
                '工作流定义对象：{descriptor: {name, access_targets:[], data_sources:[], credential_refs:[], io_contract:{}}, '
                'body_markdown: 说明文本, steps: [{step_id, name, mode(deterministic_script|llm_skill_analysis|hybrid), '
                'script_id(可选), skill_ids(可选列表), condition(可选:{"step_id":"前步ID或$inputs","key":"字段","equals":值}), '
                'inputs_map(可选:{"入参":{"step_id":"前步ID或$inputs","key":"字段"}}), on_error(escalate_to_agent|fail|skip)}]}')},
            'patched_step_output': {'type': 'object', 'description': '断点续跑时注入并经脚本IO契约校验的步骤输出（用于人工确认或故障修复）'},
            'resolution_origin': {'type': 'string', 'enum': ['user', 'agent', 'system'],
                'description': '断点恢复或人工修复裁决来源标识（user、agent 或 system）。'},
            'updated_data_sources': {'type': ['object', 'array'], 'description': '断点续跑时更新的数据源配置'},
            'inputs': {'type': 'object', 'description': '工作流运行输入参数字典（可在step的condition或inputs_map中用step_id="$inputs"引用）'}})
    model_spec = _schema_save_source(_typed_definition_spec(model_spec))
    runtime_policy = _policy('workflows', READ_ONLY_ACTIONS, ('run', 'resume', 'delete', 'workflows.run',
        'workflow_runs.resume', 'workflow_runs.cancel', 'workflows.recycle', 'workflows.cleanup.run'))

    # LLM: Owner与项目/快照/模型提供者仅由可信宿主注入，模型实参不能替换；无分析器不注入假实现。
    # 函数用途: 固定当前Owner及运行时依赖，构造阶段不执行资产。
    def __init__(self, owner_home: Path, *, store=None, engine=None, analysis_handler=None,
                 workspace_root_provider=None, skill_snapshot_provider=None, workspace_root=None, im_notifier=None,
                 runner_options_provider=None, business_service=None, business_actor_provider=None,
                 business_binding_provider=None,
                 run_params_provider=None, schedule_ids_provider=None, runtime_ports_provider=None,
                 execution_scope_provider=None, review_dispatch=None, review_governance_provider=None,
                 package_resource_resolver=None, owner_id=''):
        self.owner_home = Path(owner_home).absolute()
        _owner(self.owner_home, store)
        _owner(self.owner_home, engine)
        self.store, self.engine, self.im_notifier = store, engine, im_notifier
        self.analysis_handler = analysis_handler
        self.owner_id = owner_id
        self.workspace_root_provider = workspace_root_provider
        self.skill_snapshot_provider = skill_snapshot_provider
        self.workspace_root = workspace_root
        self.runner_options_provider = runner_options_provider
        self.business_service, self.business_actor_provider = business_service, business_actor_provider
        self.business_binding_provider = business_binding_provider
        self.run_params_provider, self.schedule_ids_provider = run_params_provider, schedule_ids_provider
        self.runtime_ports_provider = runtime_ports_provider
        self.execution_scope_provider = execution_scope_provider
        self.review_dispatch = review_dispatch
        self.review_governance_provider = review_governance_provider
        self.package_resource_resolver = package_resource_resolver
        _owner(self.owner_home, getattr(business_service, 'store', None))

    # LLM: 每次run/resume冻结当前宿主上下文到本次浅拷贝engine，整次调用只用它；缓存engine仅共享RunStore/回调/解析器，
    #   不承载任一调用的runner，等待中的运行不会改用后启动运行的身份或路径范围；显式注入执行器保持原样。
    # 函数用途: 给本次默认执行器配置可信脚本范围和宿主来源屏蔽。
    def _execution_engine(self):
        if self.engine is not None:
            return self.engine
        options = self._skill_options()
        engine = copy.copy(self._engine(_options=options))
        params = self.run_params_provider() if self.run_params_provider is not None else None
        if options != self._lazy_engine_options:
            # 同范围复用已冻结的解析器；快照变化只影响本次副本，不能改写等待中的执行器。
            engine.skill_resolver = lambda step, context: _resolve_owner_skills(self.owner_home, step, context, **options)
            engine.skill_details_resolver = lambda ids: [_skill_descriptor(self.owner_home, reference, **options) for reference in ids]
        if self.run_params_provider is not None:
            on_chunk = getattr(params, 'on_chunk', None)
            sink = getattr(on_chunk, 'write_workflow_step_progress', None)
            engine.step_observer = sink if callable(sink) else None
            bind = getattr(engine.analysis_handler, 'bind_run_params', None)
            if callable(bind):
                engine.analysis_handler = bind(params)
        if self.runner_options_provider is not None:
            from ..workflows.script_runner import ScriptRunner
            engine.runner = ScriptRunner(self.owner_home, workspace_root=options['workspace_root'],
                                         **self.runner_options_provider())
        if self.runtime_ports_provider is not None:
            dispatcher, invoke = self.runtime_ports_provider()
            engine.tool_dispatcher = dispatcher
            engine.invoke_host_context = invoke
            handler = engine.analysis_handler
            if callable(handler) and callable(getattr(handler, 'bind_run_params', None)) and params is not None:
                engine.analysis_handler = wraps(handler)(lambda *args: invoke(lambda: handler(*args)))
        if self.execution_scope_provider is not None:
            engine.execution_scope = self.execution_scope_provider(params)
        return engine

    # LLM: None表示无快照，空受限快照仍原样传递；项目只来自宿主且每次读取，不缓存其它Owner权限。
    # 函数用途: 取得可信宿主当前的技能发现范围。
    def _skill_options(self):
        return {
            'workspace_root': self.workspace_root_provider() if self.workspace_root_provider else self.workspace_root,
            'skill_snapshot': self.skill_snapshot_provider() if self.skill_snapshot_provider else None,
        }

    # LLM: 细化工作流资源锁粒度 (WF-06)；只读动作与run/resume等执行不持定义写锁，定义变更动作仅锁当前工作流身份 workflow:<workflow_id>。
    # 函数用途: 动态声明 claim 前要锁定的资源范围，避免 owner 级静态锁阻塞无关工作流的保存与编辑。
    def effective_resource_scopes(
        self,
        arguments: dict[str, object],
        write_boundary: dict | None,
        workspace_root: object,
    ) -> tuple[str, ...]:
        action = str(arguments.get("action") or "").strip().lower()
        if not action or action in self.READ_ONLY_ACTIONS:
            return ()
        if action in (
            "run",
            "resume",
            "cancel",
            "debug_step",
            "workflows.run",
            "workflow_runs.resume",
            "workflow_runs.cancel",
        ):
            return ()

        target = ""
        for key in ("workflow_id", "workflow_ref"):
            val = arguments.get(key)
            if isinstance(val, str) and val.strip():
                target = val.strip()
                break
        if not target:
            definition = arguments.get("definition")
            if isinstance(definition, dict):
                target = str(
                    definition.get("id")
                    or (definition.get("descriptor") or {}).get("id")
                    or ""
                ).strip()
        if target:
            clean_id = target.removeprefix("workflow:").strip()
            return (f"workflow:{clean_id}",)
        if "cleanup" in action:
            return ("workflow:cleanup",)
        return ("workflow:create",)

    # LLM: 与直接execute共用无副作用预检，避免缺参动作先进入写操作账。
    # 函数用途: 按动作验证条件必填和参数Schema。
    def validate_invocation(self, params, context):
        if isinstance(params, dict) and 'source_ref' in params and params.get('action') != 'save':
            return _outcome('workflows', {'error_code': 'WORKFLOW_TEMPLATE_SAVE_ONLY',
                'effect_outcome': 'not_started', 'next_action': 'source_ref仅用于save；其它动作删除该字段。'},
                False, 'TOOL_INVALID_ARGUMENTS', effect_outcome='not_started')
        if isinstance(params, dict) and 'concurrency_policy' in params and params['concurrency_policy'] not in (
                'allow', 'skip_if_running', 'queue'):
            return _outcome('workflows', {'error_code': 'INVALID_CONCURRENCY_POLICY'}, False, 'TOOL_INVALID_ARGUMENTS')
        if isinstance(params, dict) and params.get('action') in WORKFLOW_ACTION_FIELDS:
            from ..conversation.business_access import BusinessError
            from .input_schema import validate_tool_input
            validation = validate_tool_input(params, self.model_spec.input_schema)
            if not validation.ok:
                return _outcome('workflows', {'error_code': 'WORKFLOW_SCHEMA_INVALID',
                    'error': '参数不符合当前action的Schema；工具尚未执行。按issues中的字段路径与类型修正，不要把JSON对象或数组发送为字符串。',
                    'issues': [issue.to_dict() for issue in validation.issues[:8]],
                    'effect_outcome': 'not_started'}, False, 'TOOL_INVALID_ARGUMENTS', effect_outcome='not_started')
            try:
                validate_business_request(params['action'], self._business_request(params),
                    expected_revision=params.get('expected_revision'), idempotency_key=params.get('idempotency_key', ''))
            except BusinessError as exc:
                return _outcome('workflows', {'error_code': exc.code, 'details': exc.details,
                    'effect_outcome': 'not_started'}, False, 'TOOL_INVALID_ARGUMENTS', effect_outcome='not_started')
            except (ValueError, TypeError) as exc:
                return _outcome('workflows', {'error_code': str(exc), 'error': str(exc),
                    'details': getattr(exc, 'details', {}), 'effect_outcome': 'not_started'},
                    False, 'TOOL_INVALID_ARGUMENTS', effect_outcome='not_started')
            return None
        return _validate(self, params)

    # LLM: 并发策略属于宿主执行装配，不把它混入业务身份或越过业务请求校验。
    # 函数用途: 从工具参数中提取原业务服务可接受的请求正文。
    @staticmethod
    def _business_request(params):
        return {key: value for key, value in params.items()
                if key not in ('action', 'expected_revision', 'idempotency_key', 'concurrency_policy')}

    def _execution_business_binding(self):
        if callable(self.business_binding_provider):
            return self.business_binding_provider()
        if self.business_service is not None and callable(self.business_actor_provider):
            return self.business_service, self.business_actor_provider()
        raise PermissionError('WORKFLOW_BUSINESS_AUTHORIZATION_UNAVAILABLE')

    def _authorized_execution_engine(self, *, definition=None, record=None, business_binding=None):
        """固定定义仍用原执行器，只从当前业务服务复用授权闭包，不重新挑选版本。"""
        from ..assets.workflow_store import BUSINESS_WORKFLOW_METADATA
        from ..workflows.native_governance import validated_workflow_review_policy
        business_required = business_binding is not None or (bool(record.scope_snapshot or record.business_context)
            if record is not None else bool(definition.descriptor.metadata.get(BUSINESS_WORKFLOW_METADATA)))
        metadata = (self._engine().store.load_workflow_snapshot(record.workflow_run_id)['descriptor'].get('metadata', {})
                    if record is not None else definition.descriptor.metadata)
        # 审核策略属于冻结工作流，与是否选择业务空间无关；复用原宿主治理，不走裸执行器。
        policy = validated_workflow_review_policy(metadata)
        review_required = bool(policy and policy['steps'])
        engine = self._execution_engine()
        if not business_required and not review_required and record is None:
            return engine
        recovery_without_store = False
        if business_required:
            service, actor = business_binding if business_binding is not None else self._execution_business_binding()
            _owner(self.owner_home, service.store)
            authorize = service.execution_authorizer(actor)
        else:
            # 原无空间入口只需要原宿主审核器，不借业务功能开关签出或剥夺Owner执行身份。
            governance = self.review_governance_provider() if callable(self.review_governance_provider) else None
            authorize = getattr(governance, 'require_effect', None)
            if not callable(authorize):
                if record is not None:
                    from ..workflows.governance import WorkflowOperationRecovery
                    # 缺审核依赖不授予重做未知写入的权利；原恢复服务只读核验、缺证明明确拒绝。
                    governance = WorkflowOperationRecovery(None)
                    authorize = governance.require_effect
                    recovery_without_store = True
                if review_required:
                    raise WorkflowPolicyRefusal('WORKFLOW_REVIEW_SERVICE_UNAVAILABLE', details={
                        'effect_outcome': 'not_started', 'reason': 'declared_review_policy_without_host_governance',
                        'recommended_action': 'change_strategy',
                        'recovery_hint': '未执行；请在原Owner和任务中恢复原审核服务，不要启用无关空间功能、移除策略或重做副作用。'})
            _owner(self.owner_home, governance)
        bound = copy.copy(engine)
        previous = engine.run_authorizer

        def check(current, phase):
            prior = previous(current, phase) if callable(previous) else None
            if recovery_without_store:
                from ..workflows.governance import WorkflowEffectPhase
                from ..workflows.release_gate import WorkflowEffectAdmission
                if (not isinstance(phase, WorkflowEffectPhase) and phase == 'resume'
                        and isinstance(prior, WorkflowEffectAdmission) and prior.reconciled_output is not None):
                    return prior
            result = authorize(current, phase)
            return prior if result is None else result

        invoke = getattr(engine, 'invoke_host_context', None)
        bound.run_authorizer = (lambda current, phase: invoke(lambda: check(current, phase))) if callable(invoke) else check
        return bound

    def _raw_run_business_options(self, params, definition):
        """已有任务只沿宿主冻结DTO；无DTO的显式选择复用业务服务原解析器。"""
        from ..workflows.business_scope import workflow_state

        current = self.run_params_provider() if callable(self.run_params_provider) else None
        context = getattr(current, 'business_context', None)
        requested = {key: params[key] for key in ('space_id', 'project_ref') if key in params}
        if context is None and not requested:
            return ({'owner_id': self.owner_id} if self.owner_id else {}), None
        binding = self._execution_business_binding()
        service, actor = binding
        import uuid
        identity = uuid.uuid4().hex
        context, origin = service.resolve_run_context(actor, requested, workflow_run_id=identity,
            scope=workflow_state(definition)['scope'], inherited_context=context)
        options = {'workflow_run_id': identity, 'business_context': context, 'owner_id': service.owner_id}
        if origin is not None:
            options['business_context_origin'] = origin
        return options, binding

    def _business_engine(self, owner_home, *, run_authorizer, agent_steps):
        if Path(owner_home).absolute() != self.owner_home:
            raise PermissionError('WORKFLOW_OWNER_MISMATCH')
        engine = copy.copy(self._execution_engine())
        original_authorizer = engine.run_authorizer

        def authorize(record, phase):
            previous = original_authorizer(record, phase) if callable(original_authorizer) else None
            current = run_authorizer(record, phase)
            return previous if current is None else current

        invoke = getattr(engine, 'invoke_host_context', None)
        engine.run_authorizer = (lambda record, phase: invoke(lambda: authorize(record, phase))) if callable(invoke) else authorize
        if agent_steps is not None:
            engine.agent_steps = agent_steps
        return engine

    # LLM: 原业务服务负责身份/幂等校验，本次engine只绑定宿主端口；持久准入回执不替代执行事实。
    # 函数用途: 调用原业务入口并保持并发准入在首次调用和同键重试中的返回语义。
    def _dispatch_business(self, params):
        if callable(self.business_binding_provider):
            service, actor = self.business_binding_provider()
        else:
            if self.business_service is None or not callable(self.business_actor_provider):
                raise PermissionError('WORKFLOW_BUSINESS_HOST_UNAVAILABLE')
            service, actor = self.business_service, self.business_actor_provider()
        _owner(self.owner_home, service.store)
        backend = copy.copy(service)
        current = self.run_params_provider() if callable(self.run_params_provider) else None
        frozen_context = getattr(current, 'business_context', None)
        if params['action'] == 'workflows.run' and frozen_context is not None:
            # qualified模型入口也继承原任务；仅本次服务副本绑定DTO，原HTTP调用方/provider不受影响。
            backend._inherited_run_context = frozen_context
        factory = backend.engine_factory or self._business_engine
        admissions = []

        # LLM: 运行策略只冻结到本次可信engine，不增加身份字段，不更改调度器payload协议。
        # 函数用途: 为业务执行器装配本次并发策略，避免第二次准入。
        def make_engine(owner_home, *, run_authorizer, agent_steps):
            engine = copy.copy(factory(owner_home, run_authorizer=run_authorizer, agent_steps=agent_steps))
            engine.default_concurrency_policy = params.get('concurrency_policy', 'allow')
            engine.concurrency_admission_observer = admissions.append
            return engine
        backend.engine_factory = make_engine
        from ..workflows.datasource_runtime import bind_asset_im_notifier
        with bind_asset_im_notifier(self.owner_home, self.im_notifier) if callable(self.im_notifier) else nullcontext():
            result = backend.execute(params['action'], self._business_request(params), actor=actor,
                expected_revision=params.get('expected_revision'), idempotency_key=params.get('idempotency_key', ''))
        if not admissions and params['action'] == 'workflows.run' and result.get('state') == 'blocked':
            from ..workflows.run_store import WorkflowRunStore
            runs = WorkflowRunStore(self.owner_home)
            current = runs.load_run(result['workflow_run_id'])
            if current.status in ('queued', 'skipped'):
                cached = runs.load_admission_receipt(current.workflow_run_id)
                if cached is not None:
                    admissions.append(cached)
                elif current.status == 'queued':
                    # 创建原run到准入回执发表之间仍属pending，不能伪造queue_position或报blocked。
                    return {**result, 'state': 'pending'}
        if admissions:
            admission = admissions[-1]
            state = ('pending' if admission['status'] == 'queued' else 'completed'
                     if admission.get('error_code') == 'WORKFLOW_RUN_SKIPPED_ALREADY_RUNNING' else 'blocked')
            return {**result, **admission, 'state': state}
        return result

    # LLM: 正式Store优先，默认每次绑定当前项目，模块损坏不回退；写入仍由Store显式scope裁决。
    # 函数用途: 延迟取得当前Owner工作流存储。
    def _store(self):
        return self.store if self.store is not None else WorkflowStore(self.owner_home, workspace_root=self._skill_options()['workspace_root'])

    # LLM: 默认缓存只绑定已冻结范围，不承载本次runner；_options仅由执行入口传入可信宿主快照，项目变化时重建。
    # 函数用途: 延迟取得Owner绑定的正式工作流执行器，避免等待中的解析器读取后来范围。
    def _engine(self, *, _options=None):
        if self.engine is not None:
            return self.engine
        options = self._skill_options() if _options is None else _options
        ws = options['workspace_root']
        cached = getattr(self, '_lazy_engine', None)
        if cached is None or getattr(self, '_lazy_engine_ws', None) != ws:
            from ..workflows.engine import WorkflowEngine
            cached = WorkflowEngine(self.owner_home, workspace_root=ws, owner_id=self.owner_id,
                skill_resolver=lambda step, context: _resolve_owner_skills(
                    self.owner_home, step, context, **options),
                skill_details_resolver=lambda references: [
                    _skill_descriptor(self.owner_home, reference, **options)
                    for reference in references],
                analysis_handler=self.analysis_handler)
            self._lazy_engine = cached
            self._lazy_engine_ws = ws
            self._lazy_engine_options = options
        return cached

    # LLM: inspect失败是成功读取；运行失败保留断点机器码，CAS在Store原锁拒绝，不冒称写入已发生。
    # 函数用途: 统一工具结果与结构化错误，不吞中断。
    def execute(self, params):
        return self.execute_scoped(params, None)

    def execute_scoped(self, params, context):
        from ..capability.method_usage import method_usage_scope, record_workflow_receipts
        from .result_display import workflow_result_display
        usage_params = self.run_params_provider() if callable(self.run_params_provider) else None
        with method_usage_scope(usage_params):
            outcome = self._execute(params)
            record_workflow_receipts(self, params, outcome)
        data = outcome.result_envelope
        steps = data.get('steps', [])
        # 消费原执行器的结构化效果事实；未知副作用不能被通用错误回执诱导重放。
        unknown_step = any(
            isinstance(step, dict) and isinstance(step.get('escalation_result'), dict)
            and step['escalation_result'].get('effect_outcome') == 'unknown'
            for step in steps) if isinstance(steps, list) else False
        if not outcome.ok and (data.get('effect_outcome') == 'unknown' or unknown_step):
            reported_code = outcome.reported_error_code or outcome.error_code
            prior_next = outcome.next_action
            data = {**data, 'effect_outcome': 'unknown',
                'reported_error_code': reported_code}
            original_run_id = data.get('workflow_run_id')
            if isinstance(original_run_id, str) and original_run_id:
                data['recovery_query'] = {
                    'requires_new_user_message': True, 'read_only': True, 'tool': 'workflows',
                    'parameters': {'action': 'inspect_run', 'workflow_run_id': original_run_id},
                    'resume_permitted': False,
                    'instruction': '本轮已因未知副作用停止。请另发一条消息，仅只读查询此原运行；'
                                   '使用parameters中的完整参数，不要resume、重跑或更换幂等键。'}
                if not prior_next:
                    prior_next = data['recovery_query']['instruction']
            outcome = _outcome('workflows', data, False,
                'WORKFLOW_RECONCILIATION_REQUIRED', effect_outcome='unknown',
                failure_stage='effect_reconciliation', next_action=prior_next)
            outcome.reported_error_code = reported_code
        if (outcome.error_code != 'WORKFLOW_RECONCILIATION_REQUIRED' and self.review_dispatch and context is not None and not outcome.ok and isinstance(params, dict)
                and params.get('action') in ('run', 'resume', 'workflows.run', 'workflow_runs.resume')
                and outcome.result_envelope.get('workflow_run_id')
                and (outcome.error_code == 'WORKFLOW_REVIEW_REQUIRED'
                     or outcome.result_envelope.get('state') == 'failed')):
            receipt = self.review_dispatch(outcome.result_envelope['workflow_run_id'], context)
            if receipt is not None:
                data = {**outcome.result_envelope, 'review_dispatch': receipt}
                outcome = _outcome('workflows', data, False, 'WORKFLOW_REVIEW_REQUIRED',
                                   effect_outcome=outcome.effect_outcome)
        outcome.result_envelope = {**outcome.result_envelope, 'display': workflow_result_display(
            params.get('action', '') if isinstance(params, dict) else '',
            outcome.result_envelope, ok=outcome.ok, error_code=outcome.error_code)}
        return outcome

    def _execute(self, params):
        invalid = self.validate_invocation(params, None)
        if invalid is not None:
            return invalid
        try:
            business = params['action'] in WORKFLOW_ACTION_FIELDS
            data = self._dispatch_business(params) if business else _dispatch(self, params)
            ok = (params['action'] not in ('run', 'resume')
                  or data.get('status') in ('succeeded', 'queued')
                  or data.get('status') == 'skipped' and data.get('error_code') == 'WORKFLOW_RUN_SKIPPED_ALREADY_RUNNING'
                  or (data.get('status') == 'paused_for_resume' and not data.get('needs_repair')))
            if business:
                ok = data.get('state') not in ('failed', 'blocked')
            if params['action'] == 'publish' and data.get('publish_complete') is False:
                ok = False
            code = ''
            if not ok:
                if data.get('status') == 'paused_for_resume':
                    code = 'WORKFLOW_PAUSED'
                    data = {**data, 'next_action': 'inspect_run'}
                else:
                    code = pending_review_error(data) or 'TOOL_EXECUTION_FAILED'
            return _outcome('workflows', data, ok, code)
        except WorkflowTemplateError as exc:
            return _outcome('workflows', exc.details, False, 'TOOL_INVALID_ARGUMENTS', effect_outcome='not_started')
        except AssetPostCommitReadbackError as exc:
            return _post_commit_failure('workflows', exc, params=params)
        except AssetActionError as exc:
            return _outcome('workflows', exc.data, False, exc.code, effect_outcome='not_started')
        except WorkflowExecutionError as exc:
            outcome = _outcome('workflows', exc.receipt, False, 'TOOL_EXECUTION_FAILED',
                            effect_outcome=exc.receipt['effect_outcome'], failure_stage=(
                                'effect_reconciliation' if exc.failure_stage == 'completion_clock' else 'execution'))
            # 提交后异常的精确宿主码只作原因展示，不能把原unknown控制改成未执行或重新开始。
            outcome.reported_error_code = exc.error_code
            return outcome
        except HostAPIError as exc:
            from ..contracts.error_taxonomy import error_contract

            # 只认宿主类型的结构化码和未执行证明；未知/已提交结果仍沿原保守控制，不解析正文猜状态。
            code = (exc.code if exc.outcome == 'not_started' and error_contract(exc.code).code == exc.code
                    else 'TOOL_PERMISSION_DENIED' if exc.status == 403 else 'TOOL_EXECUTION_FAILED')
            outcome = _outcome('workflows', {'error_code': exc.code, 'error': exc.message,
                            'host_error': exc.to_payload()}, False,
                            code,
                            effect_outcome='not_started' if exc.outcome == 'not_started' else 'unknown')
            outcome.reported_error_code = exc.code
            return outcome
        except WorkflowPolicyRefusal as exc:
            return _outcome('workflows', {'error_code': exc.code, 'error': str(exc),
                'details': exc.details}, False, exc.code, effect_outcome='not_started')
        except PermissionError as exc:
            return _outcome('workflows', {'error': str(exc)}, False, 'TOOL_PERMISSION_DENIED', effect_outcome='not_started')
        except (ValueError, KeyError, TypeError) as exc:
            if str(exc).startswith('ASSET_REVISION_CONFLICT:'):
                return _outcome('workflows', {'error_code': 'ASSET_REVISION_CONFLICT', 'error': str(exc)},
                                False, 'TOOL_INVALID_ARGUMENTS', effect_outcome='not_started')
            return _outcome('workflows', {'error': str(exc)}, False, 'TOOL_INVALID_ARGUMENTS')
        except (OSError, RuntimeError, ImportError) as exc:
            return _outcome('workflows', {'error': str(exc)}, False, 'TOOL_EXECUTION_FAILED')

    # LLM: 恢复默认拒绝依赖漂移；宿主组合在恢复上下文及写回前拒绝，普通保存CAS仍由原Store核验。
    # 函数用途: 分派CRUD、绑定、运行、补丁恢复或快照检查。
    def _dispatch(self, params):
        action = params['action']
        if action == 'status':
            return self._status(params)
        if action == 'resume':
            options = {'concurrency_policy': params.get('concurrency_policy', 'allow')}
            if 'allow_script_drift' in params:
                options['allow_script_drift'] = params['allow_script_drift']
            record = self._engine().store.load_run(params['workflow_run_id'])
            return _plain(self._authorized_execution_engine(record=record).resume_workflow_run(params['workflow_run_id'], params.get('from_step_id'),
                patched_step_output=params.get('patched_step_output'), updated_data_sources=params.get('updated_data_sources'),
                resolution_origin=params.get('resolution_origin'), **options))
        if action == 'inspect_run':
            return _plain(self._engine().store.load_run(params['workflow_run_id']))
        store = self._store()
        if action == 'list':
            return {'workflows': [_plain(item) for item in store.list_workflows()
                                  if item.descriptor.metadata.get('hidden') is not True
                                  and workflow_list_matches(item, params, default_lifecycle='all')]}
        workflow_id = params['workflow_id']
        if action == 'versions':
            return {'versions': list_versions(self.owner_home, 'workflow', workflow_id, limit=params.get('limit', 20))}
        if action == 'diff':
            return diff_versions(self.owner_home, 'workflow', workflow_id, params['from_version'], params.get('to_version'))
        if action == 'impact':
            return self._impact(params)
        if action == 'draft':
            definition, stripped = self._clean_definition(params['definition'], workflow_id)
            latest = latest_version(self.owner_home, 'workflow', workflow_id)
            draft = save_draft(self.owner_home, 'workflow', workflow_id, payload=self._flat_definition(definition),
                side_files={'WORKFLOW.md': definition.body_markdown or None}, base_version=latest['version'] if latest else None)
            return {'draft': draft, 'stripped_metadata_keys': stripped}
        if action == 'publish':
            draft = get_draft(self.owner_home, 'workflow', workflow_id)
            if draft is None:
                raise AssetActionError('ASSET_DRAFT_NOT_FOUND')
            raw = blob_get(self.owner_home, draft['payload_blob'])
            if raw is None:
                raise AssetActionError('ASSET_HISTORY_BLOB_MISSING')
            definition = self._from_flat(json.loads(raw))
            data = self._save_definition(definition.to_dict(), workflow_id, params.get('expected_revision'))
            recording = (_VERSION_OPERATION.get() or {}).get('recording', {})
            if recording.get('recorded') is not True:
                return {**data, 'publish_complete': False, 'committed': True, 'effect_outcome': 'unknown'}
            delete_draft(self.owner_home, 'workflow', workflow_id)
            return {**data, 'publish_complete': True}
        if action == 'rollback':
            from ..assets.version_hook import restoring_snapshot
            record, payload, _, _ = load_snapshot(self.owner_home, 'workflow', workflow_id, params['to_version'])
            selected = self._from_flat(payload).descriptor.metadata
            # 历史身份已命中时直接拒绝，不必读取当前资产或进入其存储恢复流程。
            if _host_managed_composition(selected):
                raise AssetActionError('ASSET_STORE_DIRECT_WRITE_DENIED',
                    {'workflow_id': workflow_id, 'reason': 'monitor_composition_host_managed'})
            current = self._store().get_workflow(workflow_id)
            if current is not None and _host_managed_composition(current.descriptor.metadata):
                raise AssetActionError('ASSET_STORE_DIRECT_WRITE_DENIED',
                    {'workflow_id': workflow_id, 'reason': 'monitor_composition_host_managed'})
            with restoring_snapshot(self.owner_home, record):
                return self._save_definition(self._from_flat(payload).to_dict(), workflow_id, params['expected_revision'])
        if action == 'save':
            if 'source_ref' in params:
                return self._save_package_template(params['source_ref'], workflow_id, params.get('expected_revision'))
            return self._save_definition(params['definition'], workflow_id, params.get('expected_revision'))
        definition = store.get_workflow(workflow_id)
        if definition is None:
            raise ValueError('工作流不存在')
        if action == 'get':
            return _plain(definition) | workflow_projection(self.owner_home, definition, **self._skill_options())
        if action == 'delete':
            return {'deleted': store.delete_workflow(workflow_id)}
        if action in ('bind_skill', 'unbind_skill'):
            method = store.attach_skill_to_step if action == 'bind_skill' else store.detach_skill_from_step
            return _plain(method(workflow_id, params['step_id'], params['skill_id']))
        if action == 'preview':
            return self._preview(definition, params.get('inputs', {}))
        if action == 'run':
            version = getattr(self, '_pinned_definition_version', params.get('version'))
            options, binding = self._raw_run_business_options(params, definition)
            if version is not None:
                options['definition_version'] = version
            return _plain(self._authorized_execution_engine(definition=definition, business_binding=binding).run_workflow(_plain(definition), params.get('inputs', {}),
                          concurrency_policy=params.get('concurrency_policy', 'allow'), **options))
        raise ValueError('未知工作流action')

    # LLM: 仅清理模型传入metadata；保留原定义codec、步骤refs和数据源绑定规则。
    # 函数用途: 规范工作流保存请求并记录剥离键。
    def _clean_definition(self, value, workflow_id):
        definition = _definition(value, workflow_id)
        previous = self._store().get_workflow(workflow_id)
        if (previous is not None and previous.descriptor.owner_id and self.owner_id
                and previous.descriptor.owner_id != self.owner_id):
            raise PermissionError('WORKFLOW_OWNER_MISMATCH')
        source = definition.descriptor.metadata.get('derived_from')
        if isinstance(source, dict):
            if previous is None or previous.descriptor.metadata.get('derived_from') != source:
                raise WorkflowTemplateError('WORKFLOW_TEMPLATE_SOURCE_HOST_ONLY')
        metadata, stripped = model_metadata(definition.descriptor.metadata)
        if definition.descriptor.owner_id:
            stripped = [*stripped, 'descriptor.owner_id']
        # owner由注册工具的宿主提供，不信模型即使填写同值；旧记录仍由Store锁内保留原凭据。
        return replace(definition, descriptor=replace(definition.descriptor, metadata=metadata,
            owner_id=self.owner_id)), stripped

    # LLM: G1只预检是否显式提供CAS，具体revision匹配仍在原Store的同一锁内判断。
    # 函数用途: 统一save、publish和rollback的正式保存路径。
    def _save_definition(self, value, workflow_id, expected_revision):
        store = self._store()
        previous = store.get_workflow(workflow_id)
        if previous is not None:
            require_revision(self.owner_home, 'workflow', workflow_id, previous.descriptor.revision,
                             expected_revision, **self._skill_options())
        definition, stripped = self._clean_definition(value, workflow_id)
        saved = store.save_workflow(self._topology_refs(definition), expected_revision=expected_revision)
        return _plain(saved) | {'stripped_metadata_keys': stripped}

    def _save_package_template(self, source_ref, workflow_id, expected_revision):
        store = self._store()
        if store.get_workflow(workflow_id) is not None:
            raise WorkflowTemplateError('WORKFLOW_TEMPLATE_TARGET_EXISTS',
                next_action='目标工作流已存在，未覆盖；请使用独立的新workflow_id。')
        if not callable(self.package_resource_resolver):
            raise WorkflowTemplateError('WORKFLOW_TEMPLATE_SOURCE_UNAVAILABLE',
                next_action='当前宿主未绑定原包资源读取器；请在已授权Owner会话使用此入口，不要手抄来源冒充验证。')
        try:
            content = self.package_resource_resolver(source_ref)
        except (OSError, ValueError, TypeError) as error:
            from ..runtime_errors import is_structured_error_code

            reason = getattr(error, 'error_code', '') or getattr(error.__cause__, 'error_code', '')
            raise WorkflowTemplateError('WORKFLOW_TEMPLATE_SOURCE_UNAVAILABLE', field_path='$.source_ref',
                details={'source_exception_type': type(error).__name__,
                         'source_error_code': reason if is_structured_error_code(reason) else 'SOURCE_REFERENCE_INVALID'},
                next_action='原包来源已过期、不可用或摘要不符；用skill_search从当前已授权快照重取完整source_ref，核对版本后再派生，勿盲重试。') from error
        value = decode_package_workflow(content, source_ref, workflow_id)
        try:
            definition, stripped = self._clean_definition(value, workflow_id)
            definition = self._topology_refs(definition)
            validate_definition_eligibility(definition)
        except (ValueError, TypeError, KeyError) as error:
            from ..common.log_redaction import redact_sensitive_text

            raise WorkflowTemplateError('WORKFLOW_TEMPLATE_DEFINITION_INVALID',
                field_path=getattr(error, 'field', '$.steps'), details={
                    'validation_exception_type': type(error).__name__,
                    'validation_reason': redact_sensitive_text(str(error))[:256]}) from error
        proof = issue_package_workflow_source(self.owner_home, definition, dict(content.source_ref))
        saved = store.save_workflow(definition, expected_revision=expected_revision, package_template_source=proof)
        from ..capability.method_usage import record_template_selection
        record_template_selection(self, saved, dict(content.source_ref), chars=len(content.data.decode("utf-8")))
        return _plain(saved) | {'stripped_metadata_keys': stripped}

    # LLM: W1历史payload使用与原Store相同的平铺格式，不保存工具展示字段。
    # 函数用途: 把工作流定义转为历史快照格式。
    @staticmethod
    def _flat_definition(definition):
        value = definition.to_dict()
        return {**value['descriptor'], **{key: item for key, item in value.items() if key != 'descriptor'}}

    # LLM: 历史字段通过同一严格codec恢复，描述符与步骤字段不相互混入。
    # 函数用途: 从W1快照恢复完整工作流。
    @staticmethod
    def _from_flat(payload):
        names = {item.name for item in fields(AssetDescriptor)} | {'schema_version'}
        return WorkflowDefinition.from_any({'descriptor': {key: value for key, value in payload.items() if key in names},
            **{key: value for key, value in payload.items() if key not in names}})

    # LLM: 影响报告只读描述符与历史，不执行或升级监控；缺失W8字段不会报错。
    # 函数用途: 报告目标版本的依赖、监控、组合及IO变更。
    def _impact(self, params):
        workflow_id = params['workflow_id']
        current = self._store().get_workflow(workflow_id)
        if current is None:
            raise AssetActionError('ASSET_VERSION_NOT_FOUND')
        _, target, _, _ = load_snapshot(self.owner_home, 'workflow', workflow_id, params.get('to_version'))
        compositions = []
        for definition in self._store().list_workflows():
            metadata = definition.descriptor.metadata
            composition = metadata.get('composition')
            if metadata.get('hidden') is not True or not isinstance(composition, dict):
                continue
            if composition.get('base_workflow_id') != workflow_id:
                continue
            ids = composition.get('appended_step_ids')
            compositions.append({'workflow_id': definition.descriptor.id,
                'appended_step_ids': [item for item in ids if isinstance(item, str)] if isinstance(ids, list) else []})
        return {'dependents': dependents(self.owner_home, 'workflow', workflow_id, **self._skill_options()),
                'monitors': bound_monitors(self.owner_home, workflow_id), 'compositions': compositions,
                'io_contract_changes': field_changes(current.descriptor.to_dict()['io_contract'], target.get('io_contract', {}))}

    # LLM: 查询固定活动状态的有界页，不因最新历史挤占页面而漏掉旧运行；不实例化Engine或Agent。
    # 函数用途: 返回当前步骤、版本、监控来源和可选Batch进度。
    def _status(self, params):
        from ..workflows.run_store import WorkflowRunStore

        runs = self.engine.store if self.engine is not None else WorkflowRunStore(self.owner_home)
        limit = params.get('limit', 50)
        workflow_id = params.get('workflow_id')
        active = [*runs.list_runs(workflow_id, status='running', limit=limit),
                  *runs.list_runs(workflow_id, status='queued', limit=limit),
                  *runs.list_runs(workflow_id, status='paused_for_resume', limit=limit)]
        result = []
        for value in sorted(active, key=lambda item: (_plain(item).get('started_at', 0)), reverse=True):
            data = _plain(value)
            context = data.get('trigger_context')
            context = context if isinstance(context, dict) else {}
            trigger = data.get('trigger_kind', context.get('trigger_kind', 'manual'))
            if params.get('include_monitors', True) is False and trigger == 'monitor':
                continue
            steps = data.get('steps')
            step = next((item for item in steps if isinstance(item, dict) and item.get('status') == 'running'), None) if isinstance(steps, list) else None
            if step is None and isinstance(steps, list):
                step = next((item for item in steps if isinstance(item, dict) and item.get('status') in ('pending', 'paused_for_resume')), None)
            started = data.get('started_at')
            epoch = started if type(started) in (int, float) else None
            if isinstance(started, str):
                try:
                    instant = datetime.fromisoformat(started.replace('Z', '+00:00'))
                    epoch = instant.replace(tzinfo=timezone.utc).timestamp() if instant.tzinfo is None else instant.timestamp()
                except ValueError:
                    pass
            row = {'workflow_run_id': data['workflow_run_id'], 'workflow_id': data['workflow_id'],
                   'trigger_kind': trigger, 'current_step': step.get('step_id') if step else None,
                   'started_at': started, 'duration': max(0, time.time() - epoch) if epoch is not None else None}
            if type(data.get('definition_version')) is int:
                row['definition_version'] = data['definition_version']
            summary = step.get('batches_summary') if step else None
            if isinstance(summary, dict) and type(summary.get('total')) is int and summary['total'] > 0:
                completed = summary.get('completed') if type(summary.get('completed')) is int else 0
                row['batch_progress'] = f"Batch {min(completed + 1, summary['total'])}/{summary['total']}"
            result.append(row)
        return {'runs': result[:limit], 'limit': limit, 'truncated': len(result) > limit}

    # LLM: 数据源只读当前授权脚本；监控引用来自只读列表，不启动调度或改共享AssetRefs。
    # 函数用途: 保存时补齐画布关联的数据源和监控编号。
    def _topology_refs(self, definition):
        from ..assets.script_store import ScriptStore
        scripts = ScriptStore(self.owner_home, workspace_root=self._skill_options()['workspace_root'])
        bindings = {}
        for step in definition.steps:
            if step.script_id and step.script_id not in bindings:
                record = scripts.get_script(step.script_id)
                if record is not None:
                    bindings[step.script_id] = tuple(source['source_id'] for source in record.get('data_sources', ()))
        if callable(self.schedule_ids_provider):
            schedules = self.schedule_ids_provider(definition.descriptor.id)
        else:
            from ..assets.monitor_adapter import _jobs
            schedules = tuple(job['job_id'] for job in _jobs(self.owner_home)
                if job.get('status') != 'deleted' and (job.get('execution_target') or {}).get('kind') == 'workflow'
                and job['execution_target'].get('asset_id') == definition.descriptor.id)
        return with_workflow_step_refs(definition, script_datasources=bindings or None, schedule_ids=schedules)

    # LLM: 只读定义与已授权Skill，不执行脚本/模型、不创建run账本；每项正文经脱敏后最多200字。
    # 函数用途: 显式预览分析步骤，区别于需要真实分析器的run/resume。
    def _preview(self, definition, inputs):
        definition = WorkflowDefinition.from_any(definition)
        options = self._skill_options()
        steps = []
        for step in definition.steps:
            if step.mode in ('llm_skill_analysis', 'hybrid'):
                resolved = _resolve_owner_skills(self.owner_home, step, inputs, **options)
                steps.append(_analysis_summary(step, resolved, inputs, owner_home=self.owner_home, **options))
        return {'status': 'previewed', 'llm_involved': False, 'steps': steps,
                'skill_summaries': [summary for step in steps for summary in step['skill_summaries']]}
