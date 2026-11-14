# LLM: Owner根只由宿主构造器注入；复用资产Store与受审批的ScriptRunner，不从模型参数取得执行权限。
# 模块用途: 暴露固化脚本管理、源评估与单次运行；schema与完整描述符构造复用所属纯模块。
from __future__ import annotations

import json
import os
from contextlib import nullcontext
from dataclasses import asdict, fields, is_dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from ..assets import AssetPostCommitReadbackError, valid_skill_reference
from ..assets.asset_dependents import (
    AssetActionError,
    asset_projection,
    model_metadata,
    require_revision,
)
from ..assets.descriptor import (
    REQUIRED_DEFINITION_KEYS,
    AssetDescriptor,
    asset_descriptor_from_definition,
    clean_json,
)
from ..assets.revision_history import get_version, list_versions, materialize_script
from ..assets.script_store import ScriptStore
from ..assets.version_diff import diff_versions, load_snapshot
from ..assets.version_hook import version_operation
from ..common.json_io import locked_json_path
from ..common.nofollow_fs import open_directory_beneath
from .input_schema import validate_tool_input
from .models import (
    ApprovalPolicy,
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolRuntimePolicy,
)
from .tool_spec import _spec


# LLM: 独立动作锁仅包管理动作；源检查与其它只读入口不建资产动作目录，绑定另归Store集合锁。
# 函数用途: 串行化同Owner同类资产的管理动作，防止增量更新或Skill绑定丢失。
def _dispatch(tool, params):
    from ..workflows.datasource_runtime import _ASSET_IM_NOTIFIER, bind_asset_im_notifier
    notifier = getattr(tool, 'im_notifier', None)
    ctx = (bind_asset_im_notifier(tool.owner_home, notifier)
           if callable(notifier) and _ASSET_IM_NOTIFIER.get() is None else nullcontext())
    with ctx:
        if params['action'] in ('list', 'get', 'preview', 'run', 'resume', 'inspect_run', 'check_sources',
                                'versions', 'diff', 'impact', 'status'):
            return tool._dispatch(params)
        tool.owner_home.mkdir(parents=True, exist_ok=True)
        collection = tool.model_spec.name
        from ..assets import _verify_directory_beneath
        _verify_directory_beneath(tool.owner_home, (collection,), create=True)
        with locked_json_path(tool.owner_home / collection / '.tool-actions'):
            summary = params.get('summary_line', '')
            if params['action'] == 'promote_draft':
                summary = 'promoted_from_draft=' + params['draft_id']
            with version_operation(summary_line=summary,
                    restored_from=params.get('to_version') if params['action'] == 'rollback' else None) as state:
                data = tool._dispatch(params)
                return {**data, 'version_recording': state['recording']} if isinstance(data, dict) and state['recording'] else data


# LLM: 注入对象来自宿主，显式不同Owner仍拒绝，不能借依赖注入跨Owner执行。
# 函数用途: 校验Store或Engine的公开Owner绑定，不读取资产。
def _owner(home, backend):
    if backend is not None and Path(getattr(backend, 'owner_home', home)).absolute() != home:
        raise ValueError('注入后端Owner与工具Owner不一致')


# LLM: JSON及显式Skill引用在claim前纯校验，不接触存储；目录ID与Skill来源引用采用各自合同。
# 函数用途: 共用Schema及按动作必填项，防止错误参数先创建Owner或留下未知操作。
def _validate(tool, params):
    name = tool.model_spec.name
    validation = validate_tool_input(params, tool.model_spec.input_schema)
    if not validation.ok:
        advice = ('按issues[].path修正对应字段；object用真实对象如{}、array用真实数组如[]，'
                  '删除未声明字段，不要原样重试或传序列化JSON字符串。')
        return _outcome(name, {'error': '参数不符合工具Schema',
            'issues': [issue.to_dict() for issue in validation.issues[:8]],
            'issues_truncated': len(validation.issues) > 8, 'effect_outcome': 'not_started',
            'next_action': advice},
                        False, 'TOOL_INVALID_ARGUMENTS', effect_outcome='not_started', next_action=advice)
    if 'skill_id' in params:
        try:
            valid_skill_reference(params['skill_id'])
        except (TypeError, ValueError) as exc:
            return _outcome(name, {'error': str(exc)}, False, 'TOOL_INVALID_ARGUMENTS',
                            effect_outcome='not_started',
                            next_action='请传入合法的 Skill 稳定引用（先通过 skill_search 查询准确 skill_id）。')
    action = params['action']
    descriptor = params.get('descriptor')
    if (name == 'scripts' and isinstance(descriptor, dict) and 'kind' in descriptor
            and descriptor['kind'] != 'script'):
        return _outcome(name, {'error': '脚本kind冲突', 'expected_kind': 'script',
            'provided_kind': descriptor['kind']}, False, 'TOOL_INVALID_ARGUMENTS',
            effect_outcome='not_started',
            next_action='descriptor.kind 省略或固定填 "script"；工作流定义请改用 workflows 工具。')
    key = 'script_id' if name == 'scripts' else 'workflow_id'
    required = [] if action in ('list', 'status', 'promote_draft') else [key]
    if action == 'promote_draft':
        required = ['draft_id']
    if action in ('rollback',):
        required += ['to_version', 'expected_revision']
    if action == 'diff':
        required += ['from_version']
    if action == 'versions' and params.get('limit', 20) > 50:
        return _outcome(name, {'error': '版本列表limit必须为1至50'}, False, 'TOOL_INVALID_ARGUMENTS',
                        effect_outcome='not_started', next_action='将 limit 调整到 1 至 50 之间后再查询。')
    if action in ('resume', 'inspect_run'):
        required = ['workflow_run_id']
    if action == 'register':
        required += ['descriptor', 'code']
    if action in ('save', 'draft'):
        required += ['source_ref' if name == 'workflows' and action == 'save' and 'source_ref' in params else 'definition']
    if action in ('update_sources', 'record_candidate'):
        required += ['source_id'] + (['candidate'] if action == 'record_candidate' else [])
    if action in ('bind_skill', 'unbind_skill'):
        required += ['step_id', 'skill_id']
    if any(key not in params or params[key] is None or params[key] == '' for key in required):
        return _outcome(name, {'error': '缺少条件必填参数', 'required': required}, False, 'TOOL_INVALID_ARGUMENTS',
                        effect_outcome='not_started',
                        next_action=f'补齐动作 {action} 所需的必填参数 {required} 后再调用。')
    return None


# LLM: 仅供Store内部继承字段，不脱敏或序列化；展示必须另经_plain，避免把脱敏代码写回磁盘。
# 函数用途: 无损取得领域对象、dataclass或原字典的数据。
def _raw(value):
    data = value.to_dict() if hasattr(value, 'to_dict') else asdict(value) if is_dataclass(value) else value
    if (isinstance(data, dict) and not isinstance(value, dict)
            and ('workflow_run_id' in data or 'exit_code' in data and 'output_data' in data)):
        data.update({name: getattr(value, name) for name in ('execution_definition_ref', 'workflow_run_id',
            'phase', 'effect_outcome', 'confirmed_step_refs', 'preparation_timings', 'delivery_receipts') if hasattr(value, name)})
    return data


# LLM: 只用于模型可见投影，保留领域版本字段并共用有限JSON安全codec，不能作为更新的旧值。
# 函数用途: 把领域结果转换为脱敏的普通JSON值。
def _plain(value):
    return clean_json(_raw(value))


# LLM: 两工具共用错误出口；明确提交后读回事实优先于异常类型，进程清理未确认仍保留unknown。
# 函数用途: 将动作结果转换为宿主结果，运行失败也附完整业务回执与精确副作用状态。
def _outcome(name, data, ok=True, code='', *, effect_outcome='', failure_stage='', next_action=''):
    if isinstance(data, dict) and data.get('effect_outcome'):
        effect_outcome = data['effect_outcome']
    if not ok and not effect_outcome:
        cleanup_unverified = isinstance(data, dict) and (
            bool((data.get('escalation_context') or {}).get('cleanup_error'))
            or any(bool((s.get('escalation_result') or {}).get('cleanup_error')) for s in data.get('steps') or () if isinstance(s, dict))
        )
        effect_outcome = 'unknown' if cleanup_unverified else ('not_started' if code == 'TOOL_INVALID_ARGUMENTS' else 'failed')
    plain = _plain(data)
    outcome = ToolHandlerOutcome(name, ok, json.dumps(plain, ensure_ascii=False), error_code=code,
        result_envelope=plain if isinstance(plain, dict) else {},
        effect_outcome='' if ok else effect_outcome, failure_stage=failure_stage,
        next_action='' if ok else next_action)
    if isinstance(plain, dict):
        for field_name in ('workflow_run_id', 'phase', 'execution_definition_ref', 'confirmed_step_refs'):
            if field_name in plain:
                setattr(outcome, field_name, plain[field_name])
    return outcome


def _extract_post_commit_target(name: str, error: Exception, params: dict | None) -> tuple[str, str, str]:
    id_key = 'script_id' if name == 'scripts' else 'workflow_id'
    target_id = ''
    action = ''
    if isinstance(params, dict):
        action = str(params.get('action') or '').strip()
        for candidate_key in (id_key, 'workflow_ref', 'draft_id'):
            raw_val = params.get(candidate_key)
            if isinstance(raw_val, str) and raw_val.strip():
                target_id = raw_val.strip()
                break
    if not target_id:
        text = str(error)
        if text.startswith('资产') and text.endswith('提交后读回失败'):
            target_id = text[len('资产'):-len('提交后读回失败')].strip()
    return id_key, target_id, action


# LLM: 只接Store提交后typed异常；业务stage精确保留，宿主阶段沿既有execution枚举，禁止重放写动作修复读回。
# 函数用途: 给两个资产工具统一返回已提交但读回失败的unknown非重试回执，并保留目标引用与只读核实指引。
def _post_commit_failure(name, error, *, params=None):
    id_key, target_id, action = _extract_post_commit_target(name, error, params)
    cause = getattr(error, '__cause__', None) or error
    cause_type = type(cause).__name__
    ref_arg = f'{id_key}="{target_id}"' if target_id else f'目标 {id_key}'
    advice = (
        f'资产主写入已提交落盘，仅提交后读回失败（committed=true, effect_outcome=unknown）：'
        f'严禁重新执行 {action or "本次写操作"} 以免重复变更或版本漂移；'
        f'请改用只读 {name}(action="get", {ref_arg}) 或 {name}(action="versions", {ref_arg}) 核实已提交版本与原操作回执。'
    )
    refs = {
        **({id_key: target_id, 'target_asset_id': target_id} if target_id else {}),
        **({'operation_action': action} if action else {}),
    }
    envelope = {
        'error': str(error),
        'error_code': 'TOOL_POST_COMMIT_READBACK_FAILED',
        'committed': True,
        'failure_stage': 'post_commit_readback',
        'effect_outcome': 'unknown',
        'retryable': False,
        **refs,
        'error_details': {
            'cause': 'post_commit_readback_failed',
            'committed': True,
            'readback_exception_type': cause_type,
            **refs,
        },
    }
    return _outcome(name, envelope,
                    False, 'TOOL_POST_COMMIT_READBACK_FAILED', effect_outcome='unknown',
                    failure_stage='execution', next_action=advice)


# LLM: 危险动作在ask模式走原审批、在auto/full-access模式沿自主决策放行；资源按细化动态身份锁定 (WF-06)。
# 函数用途: 给两工具统一声明只读、写入与危险动作的真实效果，由工具实例提供精准动态资源锁。
def _policy(name, reads, dangerous):
    return ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy('mutating', by_parameter=(('action', tuple(
            [(action, 'read_only') for action in reads] + [(action, 'dangerous') for action in dangerous])),)),
        approval_policy=ApprovalPolicy('dangerous'),
        idempotency_policy=IdempotencyPolicy('operation'),
        resource_scopes=ResourceScopePolicy(mode='none'))


_SHARED_ASSET_DEF_DESCRIPTIONS: dict[str, dict[str, str]] = {
    'AccessTarget': {
        'target_id': '访问目标稳定标识。',
        'type': '访问目标类别（如 http、database、filesystem、channel）。',
        'endpoint': '目标逻辑端点或地址标识（不含明文凭据）。',
        'method': '访问方式或请求方法（如 GET、POST、SELECT）。',
        'extra': '目标扩展结构化元数据字典。',
    },
    'CredentialBindingRef': {
        'credential_ref': '隔离凭据库中的凭据引用 ID（不含明文密钥）。',
        'alias': '资产内部引用的凭据逻辑别名。',
        'authentication': '凭据注入认证方式（如 bearer、header、basic、query）。',
        'status': '凭据绑定当前核验状态。',
    },
    'EvaluatedCandidate': {
        'candidate_id': '候选数据源标识。',
        'decision': '候选源评估决定（active、ignored 或 pending）。',
        'evaluated_at': '候选源评估时间戳（ISO-8601）。',
        'reason': '采纳或忽略该候选源的原因说明。',
        'sample_count': '评估时核验的样本条数。',
    },
    'DataSourceSpec': {
        'source_id': '资产绑定的数据源稳定代号。',
        'system': '数据源所属外部或本地系统标识。',
        'query_scope': '绑定的查询范围过滤参数字典。',
        'evaluated_candidates': '已评估的候选数据源记录列表。',
    },
    'IOContract': {
        'inputs': '声明的输入字段名数组或输入规格描述。',
        'outputs': '声明的必选输出 JSON 字段名非空字符串数组。',
        'nullable_outputs': '允许值为 null 的输出字段名数组。',
        'output_types': '输出字段名到 JSON 类型说明的映射字典。',
    },
    'AssetRefs': {
        'workflows': '关联或引用的工作流 ID 数组。',
        'scripts': '关联或引用的脚本 ID 数组。',
        'skills': '关联或引用的 Skill 稳定引用数组。',
        'monitors': '关联或引用的监控任务 ID 数组。',
        'skill_dir': '关联的 Skill 目录引用标识。',
    },
    'RuntimeSummary': {
        'run_count': '累计运行总次数。',
        'consecutive_failures': '最近连续失败次数。',
        'last_status': '最近一次运行终态标识。',
        'last_run_at': '最近一次运行时间戳（ISO-8601）。',
        'last_duration_ms': '最近一次运行耗时毫秒数。',
        'last_error': '最近一次失败的脱敏错误摘要。',
    },
}

_DESCRIPTOR_FIELD_DESCRIPTIONS: dict[str, str] = {
    'id': '资产稳定唯一 ID（原生注册/保存时可省略并由顶层 script_id 或 workflow_id 固定）。',
    'kind': '资产固定类型标识（script 或 workflow）。',
    'name': '资产显示名称（必填非空字符串）。',
    'summary': '资产职责与用途简短摘要。',
    'version': '资产语义或快照版本号。',
    'revision': '存储层乐观并发控制整数 revision。',
    'owner_id': '资产归属 owner 标识（由宿主固定）。',
    'scope_layer': '资产归属作用域层级（如 owner 或 workspace）。',
    'source_scope': '资产来源范围标识。',
    'workspace_root': '关联工作区根路径投影（由宿主维护）。',
    'access_targets': '声明的访问目标数组（无目标传 []）。',
    'data_sources': '声明的数据源规格数组（无数据源传 []）。',
    'credential_refs': '声明的隔离凭据引用数组（无凭据传 []）。',
    'io_contract': '输入输出字段契约对象（无约束可传 {}）。',
    'refs': '正向引用的脚本、Skill、工作流与监控集合。',
    'used_by': '反向引用当前资产的上游资产集合（只读投影）。',
    'runtime_summary': '最近受控运行统计摘要（只读投影）。',
    'tags': '资产分类与检索标签字符串数组。',
    'metadata': '开放扩展 JSON 元数据对象（保存结构化扩展键值）。',
    'enabled': '资产是否处于启用状态。',
    'schema_version': '资产描述符结构协议版本号。',
}


def _annotate_schema_properties(node: dict, descriptions: dict[str, str]) -> None:
    props = node.get('properties')
    if not isinstance(props, dict):
        return
    for key, text in descriptions.items():
        field = props.get(key)
        if isinstance(field, dict) and not field.get('description'):
            field['description'] = text


def _annotate_shared_asset_defs(definitions: dict) -> None:
    for def_name, mapping in _SHARED_ASSET_DEF_DESCRIPTIONS.items():
        target = definitions.get(def_name)
        if isinstance(target, dict):
            _annotate_schema_properties(target, mapping)
    for desc_name in ('AssetDescriptor', 'WorkflowDescriptorInput'):
        descriptor = definitions.get(desc_name)
        if isinstance(descriptor, dict):
            _annotate_schema_properties(descriptor, _DESCRIPTOR_FIELD_DESCRIPTIONS)
            for ref_field in ('refs', 'used_by'):
                sub = descriptor.get('properties', {}).get(ref_field)
                if isinstance(sub, dict):
                    _annotate_schema_properties(sub, _SHARED_ASSET_DEF_DESCRIPTIONS['AssetRefs'])


def _typed_descriptor_spec(spec):
    """脚本描述符复用公开资产字段；这里只展示类型，不接管运行权限或生成默认内容。"""
    from copy import deepcopy

    from ..plugin_host.business_action_specs import standalone_schema
    from .business_spaces_tool import _model_projection

    schema = deepcopy(spec.input_schema)
    schema['properties']['action']['description'] = (
        '脚本动作：list/get/check_sources/versions/diff 只读查询；'
        'register 新建固化脚本，update/update_sources/record_candidate/rollback/promote_draft 更新或回滚，run 受控单次运行，delete 删除。'
    )
    public = _model_projection(standalone_schema({'$ref': '#/$defs/AssetDescriptor'}))
    definitions = public.pop('$defs')
    descriptor = definitions['AssetDescriptor']
    # register以script_id固定id/kind；其余标准Key与原构造器必填项保持一致。
    descriptor['required'] = sorted(REQUIRED_DEFINITION_KEYS)
    descriptor['properties']['kind'] = {'const': 'script', 'description': '脚本资产固定种类常量 "script"。'}
    definitions['IOContract']['properties']['outputs']['items'] = {'type': 'string', 'minLength': 1}
    _annotate_shared_asset_defs(definitions)
    schema['properties']['descriptor'] = {'$ref': '#/$defs/AssetDescriptor',
        'description': '真实JSON描述符对象。access_targets/data_sources/credential_refs必须是数组（无项目传[]），'
                       'io_contract必须是对象（无输入输出要求可传{}）；不可传空串或序列化JSON文本。'
                       'io_contract.inputs是输入声明数组，outputs是非空字符串字段名数组（无输出要求可省略outputs）；'
                       '例如 {"name":"汇总","access_targets":[],"data_sources":[],"credential_refs":[], '
                       '"io_contract":{"inputs":["value"],"outputs":["result"]}}。'
                       '可选id/kind由script_id与script推导；仅接受已声明字段，metadata内可保存扩展JSON。'}
    schema['$defs'] = definitions
    from .input_schema import inline_local_schema_refs

    schema['properties']['descriptor'] = inline_local_schema_refs(schema, schema['properties']['descriptor'])
    hints = replace(
        spec.hints,
        use_cases=(
            '注册、版本化维护与单次受控运行可复用的确定性数据处理脚本（stdout 输出单行 JSON）',
            '维护脚本绑定的数据源查询范围（update_sources/check_sources）与候选源评估账本（record_candidate）',
            '查看脚本历史版本差异（versions/diff）、提升草稿（promote_draft）或按 expected_revision 回滚（rollback）',
        ),
        avoid_when=(
            '需要多步骤编排、条件分支、绑定 Skill 分析或断点续跑（resume）时用 workflows',
            '仅在当前回合把若干只读/常规工具调用按固定 DAG 一次性串联、无需注册长期脚本资产时用 tool_program',
            '需要按未来时刻、固定间隔、cron 或飞书群事件持久周期触发时用 schedule',
        ),
        examples=(
            '{"action":"list"}',
            '{"action":"get","script_id":"daily_summary"}',
            '{"action":"register","script_id":"daily_summary","code":"import json\\nprint(json.dumps({\\"result\\": 1}))\\n","descriptor":{"name":"每日汇总","access_targets":[],"data_sources":[],"credential_refs":[],"io_contract":{"inputs":[],"outputs":["result"]}}}',
        ),
    )
    return replace(spec, input_schema=schema, hints=hints, schema_hash='')


# LLM: Store与runner均绑定同一个可信Owner；纯schema/descriptor沿共享构造器，execute不接收执行身份或任意命令。
# 类用途: 为Agent提供脚本目录、管理、源评估和受控运行工具。
class ScriptsTool(BaseTool):
    model_spec = _spec('scripts', '管理Owner固化脚本、标准提取Key与源评估；脚本应输出单行JSON对象到stdout；run仅单次覆盖数据源。'
        '正式工作流步骤要交付文件时，写入宿主提供的MY_AGENT_RUN_OUT目录。',
        ['list', 'get', 'register', 'update', 'update_sources', 'record_candidate', 'run', 'delete', 'check_sources',
         'versions', 'diff', 'rollback', 'promote_draft'], {
            'expected_revision': {'type': ['integer', 'null'], 'minimum': 0,
                'description': '乐观锁期望整数 revision（update/rollback 等写动作传入当前精确整数版本防并发覆盖）。'},
            'version': {'type': ['integer', 'null'], 'minimum': 1,
                'description': '指定查询的目标历史整数版本号（>= 1）。'},
            'from_version': {'type': ['integer', 'null'], 'minimum': 1,
                'description': 'diff 对比的起始整数版本号（>= 1，diff 必填）。'},
            'to_version': {'type': ['integer', 'null'], 'minimum': 1,
                'description': 'diff 对比的目标整数版本号或 rollback 要恢复到的历史版本号（>= 1）。'},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50,
                'description': 'versions 返回的最大历史版本条数（1 至 50，默认 20）。'},
            'draft_id': {'type': 'string', 'minLength': 1, 'maxLength': 128,
                'description': 'promote_draft 要提升为正式脚本的草稿 ID。'},
            'script_id': {'type': 'string',
                'description': '脚本资产唯一稳定 ID（除 list/promote_draft 外必填）。'},
            'code': {'type': 'string',
                'description': 'register 必填或 update 可选的脚本源码正文；运行时应向 stdout 输出单行 JSON 对象。'},
            'entrypoint': {'type': 'string',
                'description': '可选脚本入口标识（默认使用标准脚本主入口）。'},
            'interpreter': {'type': 'string',
                'description': '可选受控脚本解释器标识（如 python3）。'},
            'source_id': {'type': 'string',
                'description': 'update_sources 或 record_candidate 针对的数据源 source_id。'},
            'scope_key': {'type': 'string',
                'description': '可选查询范围分区键，限定 update_sources/record_candidate 作用的子范围。'},
            'descriptor': {'type': 'object', 'description': (
                '必填标准提取Key对象：{name: 非空字符串, access_targets: 列表(如[]), '
                'data_sources: 列表(如[{"source_id":"s1","system":"web","query_scope":{}}]), '
                'credential_refs: 列表(如[]), io_contract: {"inputs":[字段名],"outputs":[必选输出字段名]}，可选id/kind/summary/tags}')},
            'inputs': {'type': 'object', 'description': '传给脚本stdin及MY_AGENT_SCRIPT_INPUT_JSON的JSON对象'},
            'query_scope': {'type': 'object', 'description': 'update_sources时合并到指定source_id的查询范围字典'},
            'candidate': {'type': 'object', 'description': '候选源评估：{candidate_id, decision(active|ignored|pending), reason, sample_count}'},
            'add': {'type': 'array', 'items': {'type': 'string'},
                'description': '增量添加的标签或关联引用字符串数组。'},
            'remove': {'type': 'array', 'items': {'type': 'string'},
                'description': '增量移除的标签或关联引用字符串数组。'},
            'override_data_sources': {'type': ['object', 'array'], 'description': '仅本次传给runner，绝不写回资产'},
            'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 3600,
                'description': 'run 单次受控运行超时秒数（0 到 3600 秒之间）。'}})
    model_spec = _typed_descriptor_spec(model_spec)
    runtime_policy = _policy('scripts', ('list', 'get', 'check_sources', 'versions', 'diff'), ('run', 'delete'))

    # LLM: Store/runner和项目范围只由宿主注入；动态provider每次读取，模型参数不授予路径权限。
    # 函数用途: 固定Owner并保存显式领域依赖。
    def __init__(self, owner_home: Path, *, store=None, runner=None, workspace_root=None,
                 workspace_root_provider=None, im_notifier=None, runner_options_provider=None):
        self.owner_home = Path(owner_home).absolute()
        _owner(self.owner_home, store)
        _owner(self.owner_home, runner)
        self.store, self.runner, self.im_notifier = store, runner, im_notifier
        self.workspace_root, self.workspace_root_provider = workspace_root, workspace_root_provider
        self.runner_options_provider = runner_options_provider

    # LLM: 只使用宿主提供的项目；无显式项目时兼容已绑定runner范围，不从脚本元数据恢复权限。
    # 函数用途: 获取当前操作使用的项目根。
    def _workspace_root(self):
        return (self.workspace_root_provider() if callable(self.workspace_root_provider)
                else self.workspace_root or getattr(self.runner, 'workspace_root', None))

    # LLM: 细化脚本资源锁粒度 (WF-06)；只读动作与run执行不持定义写锁，定义变更动作仅锁当前脚本身份 script:<script_id>。
    # 函数用途: 动态声明 claim 前要锁定的资源范围，避免 owner 级静态锁阻塞无关操作。
    def effective_resource_scopes(
        self,
        arguments: dict[str, object],
        write_boundary: dict | None,
        workspace_root: object,
    ) -> tuple[str, ...]:
        action = str(arguments.get("action") or "").strip().lower()
        if not action or action in ("list", "get", "check_sources", "versions", "diff"):
            return ()
        if action == "run":
            return ()

        target = ""
        val = arguments.get("script_id")
        if isinstance(val, str) and val.strip():
            target = val.strip()
        if not target:
            descriptor = arguments.get("descriptor")
            if isinstance(descriptor, dict):
                target = str(descriptor.get("id") or "").strip()
        if target:
            clean_id = target.removeprefix("script:").strip()
            return (f"script:{clean_id}",)
        return ("script:create",)

    # LLM: 纯参数预检供原执行器claim前使用，不创建Owner或取得领域Store。
    # 函数用途: 返回条件必填及Schema错误，合法调用返回None。
    def validate_invocation(self, params, context):
        return _validate(self, params)

    # LLM: 显式Store优先；默认Store每次绑定当前workspace，避免缓存旧项目目录。
    # 函数用途: 按需取得当前Owner唯一脚本存储。
    def _store(self):
        return self.store if self.store is not None else ScriptStore(self.owner_home, workspace_root=self._workspace_root())

    # LLM: 提交后错误优先；CAS冲突单独结构化，运行保留领域源错误码，不从普通文本推导执行权。
    # 函数用途: 路由结构化脚本动作并返回JSON或原失败事实。
    def execute(self, params):
        invalid = self.validate_invocation(params, None)
        if invalid is not None:
            return invalid
        try:
            data = _dispatch(self, params)
            ok = params['action'] != 'run' or data.get('ok') is True
            return _outcome('scripts', data, ok, 'TOOL_EXECUTION_FAILED' if not ok else '')
        except AssetPostCommitReadbackError as exc:
            return _post_commit_failure('scripts', exc, params=params)
        except AssetActionError as exc:
            return _outcome('scripts', exc.data, False, exc.code, effect_outcome='not_started')
        except (ValueError, TypeError, KeyError) as exc:
            if str(exc).startswith('ASSET_REVISION_CONFLICT:'):
                return _outcome('scripts', {'error_code': 'ASSET_REVISION_CONFLICT', 'error': str(exc)},
                                False, 'TOOL_INVALID_ARGUMENTS', effect_outcome='not_started')
            return _outcome('scripts', {'error': str(exc)}, False, 'TOOL_INVALID_ARGUMENTS')
        except (OSError, RuntimeError, ImportError) as exc:
            return _outcome('scripts', {'error': str(exc)}, False, 'TOOL_EXECUTION_FAILED')

    # LLM: action精确枚举；check_sources刷新而get稳定，运行仍经源硬门；workspace/runner仅取原宿主绑定。
    # 函数用途: 将公开动作交给原领域存储、实时检查或执行器。
    def _dispatch(self, params):
        action, store = params['action'], self._store()
        if action == 'list':
            return {'scripts': _plain(store.list_scripts())}
        if action == 'promote_draft':
            return self._promote_draft(params)
        script_id = params['script_id']
        if action == 'versions':
            return {'versions': list_versions(self.owner_home, 'script', script_id, limit=params.get('limit', 20))}
        if action == 'diff':
            return diff_versions(self.owner_home, 'script', script_id, params['from_version'], params.get('to_version'))
        if action == 'rollback':
            return self._rollback(params)
        old = store.get_script(script_id)
        if action == 'register' and old is not None:
            raise ValueError('脚本已存在，请使用update')
        if action != 'register' and old is None:
            raise ValueError('脚本不存在')
        if action in ('register', 'update'):
            return self._save(params, old)
        if action == 'get':
            return _plain(old) | asset_projection(self.owner_home, 'script', script_id, old.get('metadata', {}),
                                                  workspace_root=self._workspace_root())
        if action == 'check_sources':
            from ..workflows.datasource_runtime import project_script_asset_fields
            return project_script_asset_fields(self.owner_home, old, workspace_root=self._workspace_root(), refresh_timestamp=True)
        if action == 'delete':
            return {'deleted': store.delete_script(script_id)}
        if action in ('update_sources', 'record_candidate'):
            return self._sources(params, old)
        if action == 'run':
            from ..workflows.datasource_runtime import run_script_with_datasources
            runner = self.runner
            if runner is None and self.runner_options_provider is not None:
                from ..workflows.script_runner import ScriptRunner
                runner = ScriptRunner(self.owner_home, workspace_root=self._workspace_root(), **self.runner_options_provider())
            options = {}
            if params.get('version') is not None:
                record = get_version(self.owner_home, 'script', script_id, params['version'])
                if record is None:
                    raise AssetActionError('ASSET_VERSION_NOT_FOUND')
                options['expected_script_pin'] = {'script_id': script_id, 'revision': record['storage_revision'],
                    'code_sha256': record['code_sha256'], 'source_scope': 'owner',
                    'execution_definition_ref': record['execution_definition_ref']}
            return _plain(run_script_with_datasources(self.owner_home, script_id, params.get('inputs', {}),
                runner=runner, workspace_root=self._workspace_root(),
                timeout_seconds=params.get('timeout_seconds'), override_data_sources=params.get('override_data_sources'), **options))
        raise ValueError('未知脚本action')

    # LLM: update只继承原正式字段和代码；完整定义沿资产领域校验，不写回实时投影，CAS仍由Store原锁检查。
    # 函数用途: 创建或更新脚本，保留其代码与执行参数。
    def _save(self, params, old):
        prior = _raw(old) if old else {}
        names = {item.name for item in fields(AssetDescriptor)}
        defaults = {key: value for key, value in prior.items() if key in names}
        descriptor = asset_descriptor_from_definition(params.get('descriptor', defaults), params['script_id'], 'script')
        metadata, stripped = model_metadata(descriptor.metadata)
        descriptor = replace(descriptor, metadata=metadata)
        if old is not None:
            require_revision(self.owner_home, 'script', params['script_id'], old.get('revision', 0),
                             params.get('expected_revision'), workspace_root=self._workspace_root())
        options = {key: params.get(key, prior.get(key, value)) for key, value in
                   (('entrypoint', 'main.py'), ('interpreter', 'python3'), ('timeout_seconds', 120))}
        if options['interpreter'] not in ('python3', 'bash', 'sh'):
            raise ValueError('只支持python3/bash/sh解释器')
        code = params.get('code', prior.get('code'))
        if not isinstance(code, str):
            raise ValueError('代码必须为文本')
        if 'expected_revision' in params:
            options['expected_revision'] = params['expected_revision']
        return _plain(self._store().save_script(descriptor, code, **options)) | {'stripped_metadata_keys': stripped}

    # LLM: 旧快照经W1完整校验并落入临时目录，仍调用原Store保存，不修改旧版本。
    # 函数用途: 将历史脚本恢复为一个新的正式版本。
    def _rollback(self, params):
        from tempfile import TemporaryDirectory

        script_id = params['script_id']
        record, payload, _, _ = load_snapshot(self.owner_home, 'script', script_id, params['to_version'])
        with TemporaryDirectory(prefix='asset-rollback-') as staging:
            root = materialize_script(self.owner_home, record, staging)
            from ..common.nofollow_fs import read_bytes_beneath
            code = read_bytes_beneath(root, (payload['entrypoint'],)).decode('utf-8')
            names = {item.name for item in fields(AssetDescriptor)} | {'schema_version'}
            descriptor = {key: value for key, value in payload.items() if key in names}
            request = {**params, 'action': 'update', 'descriptor': descriptor, 'code': code,
                       **{key: payload[key] for key in ('entrypoint', 'interpreter', 'timeout_seconds')}}
            return self._save(request, self._store().get_script(script_id))

    # LLM: 提炼读取沿原API；不存在独立mark接口时不改提炼原件，摘要保留准确来源。
    # 函数用途: 将一份提炼候选经正式Store保存为脚本。
    def _promote_draft(self, params):
        from ..assets.turn_extractor import _CANDIDATES, get_extraction_proposal

        try:
            proposal = get_extraction_proposal(self.owner_home, params['draft_id'])
        except FileNotFoundError:
            raise AssetActionError('ASSET_DRAFT_NOT_FOUND') from None
        candidates = proposal.get(_CANDIDATES['script'], [])
        if params.get('script_id'):
            selected = next((item for item in candidates if item.get('script_id') == params['script_id']), None)
        else:
            selected = candidates[0] if len(candidates) == 1 else None
        if selected is None:
            raise AssetActionError('ASSET_DRAFT_NOT_FOUND')
        script_id = params.get('script_id') or selected['script_id']
        descriptor = selected.get('key', selected.get('descriptor'))
        request = {'action': 'update', 'script_id': script_id, 'descriptor': descriptor,
                   'code': selected['entry_source'], 'entrypoint': selected.get('entrypoint', 'main.py'),
                   'interpreter': selected.get('interpreter', 'python3'),
                   'timeout_seconds': selected.get('timeout_seconds', 120), 'expected_revision': params.get('expected_revision')}
        return self._save(request, self._store().get_script(script_id)) | {'promoted_from_draft': params['draft_id']}

    # LLM: 读改写沿Store原值，不把展示脱敏字段回写；active候选仅加入显式scope_key，评估时间由宿主生成。
    # 函数用途: 增删源范围列表并登记候选评估，不启动探测或执行代码。
    def _sources(self, params, old):
        sources = _raw(old)['data_sources']
        source = next((row for row in sources if row['source_id'] == params['source_id']), None)
        if source is None:
            raise ValueError('数据源不存在')
        scope, patch = source['query_scope'].copy(), params.get('query_scope', {}).copy()
        scope.update(patch)
        candidate = params.get('candidate')
        if candidate:
            candidate = {**candidate, 'evaluated_at': candidate.get('evaluated_at', datetime.now(timezone.utc).isoformat())}
            if candidate.get('decision') == 'active' and candidate['candidate_id'] in params.get('remove', []):
                raise ValueError('不能同时激活和移除同一候选')
        key = params.get('scope_key')
        if params.get('add') or params.get('remove') or candidate and candidate.get('decision') == 'active':
            if not key or not isinstance(scope.get(key, []), list):
                raise ValueError('增删或激活必须指定列表scope_key')
            added = params.get('add', []) + ([candidate['candidate_id']] if candidate and candidate.get('decision') == 'active' else [])
            patch[key] = list(dict.fromkeys(value for value in scope.get(key, []) + added if value not in params.get('remove', [])))
        return _plain(self._store().update_data_sources(params['script_id'], params['source_id'], query_scope=patch, candidate=candidate))
