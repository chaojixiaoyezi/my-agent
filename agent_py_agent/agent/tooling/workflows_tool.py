# LLM: 工作流只由可信Owner的领域Store和Engine执行；工具不自建运行账本，不从正文猜断点或授权。
# 模块用途: 提供工作流CRUD、动态Skill绑定、运行、断点续跑及快照检查的Agent工具。
from __future__ import annotations

from pathlib import Path

from ..assets import AssetPostCommitReadbackError
from ..assets.skill_adapter import get_owner_skill_descriptor, list_owner_skill_descriptors
from ..assets.workflow_store import WorkflowDefinition, WorkflowStore, with_workflow_step_refs
from ..capability.skills import split_skill_frontmatter
from ..workflows.models import redact_bearer_values
from .models import BaseTool
from .scripts_tool import (
    _descriptor,
    _dispatch,
    _outcome,
    _owner,
    _plain,
    _policy,
    _post_commit_failure,
    _spec,
    _validate,
)


# LLM: 工具只补目标id/kind与标准Key预检，定义校验和引用派生全部委托同一资产合同。
# 函数用途: 规范工具保存请求，不另维护模式或错误策略副本。
def _definition(value, workflow_id):
    value = _plain(value)
    value['descriptor'] = _plain(_descriptor(value['descriptor'], workflow_id, 'workflow'))
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


# LLM: 所有路径由构造器Owner固定，model参数只含安全逻辑ID；运行/恢复/删除保持危险动作分类。
# 类用途: 对Agent暴露可管理和恢复的工作流工具。
class WorkflowsTool(BaseTool):
    ACTIONS = ('list', 'get', 'save', 'delete', 'bind_skill', 'unbind_skill', 'run', 'resume', 'inspect_run', 'preview')
    READ_ONLY_ACTIONS = ('list', 'get', 'inspect_run', 'preview')
    model_spec = _spec('workflows', '管理Owner工作流、自动脚本/Skill引用、动态技能绑定、运行与断点续跑；步骤输出含_pause_for_resume=true可正常暂停等待确认，再用resume+patched_step_output续跑。',
        list(ACTIONS), {
            'expected_revision': {'type': ['integer', 'null'], 'minimum': 0},
            'allow_script_drift': {'type': 'boolean', 'description': '显式确认恢复采用当前脚本版本'},
            **{key: {'type': 'string'} for key in ('workflow_id', 'step_id', 'skill_id', 'workflow_run_id', 'from_step_id')},
            'definition': {'type': 'object', 'description': (
                '工作流定义对象：{descriptor: {name, access_targets:[], data_sources:[], credential_refs:[], io_contract:{}}, '
                'body_markdown: 说明文本, steps: [{step_id, name, mode(deterministic_script|llm_skill_analysis|hybrid), '
                'script_id(可选), skill_ids(可选列表), condition(可选:{"step_id":"前步ID或$inputs","key":"字段","equals":值}), '
                'inputs_map(可选:{"入参":{"step_id":"前步ID或$inputs","key":"字段"}}), on_error(escalate_to_agent|fail|skip)}]}')},
            'patched_step_output': {'type': 'object', 'description': '断点续跑时注入并经脚本IO契约校验的步骤输出（用于人工确认或故障修复）'},
            'resolution_origin': {'type': 'string', 'enum': ['user', 'agent', 'system']},
            'updated_data_sources': {'type': ['object', 'array'], 'description': '断点续跑时更新的数据源配置'},
            'inputs': {'type': 'object', 'description': '工作流运行输入参数字典（可在step的condition或inputs_map中用step_id="$inputs"引用）'}})
    runtime_policy = _policy('workflows', READ_ONLY_ACTIONS, ('run', 'resume', 'delete'))

    # LLM: Owner与项目/快照/模型提供者仅由可信宿主注入，模型实参不能替换；无分析器不注入假实现。
    # 函数用途: 固定当前Owner及运行时依赖，构造阶段不执行资产。
    def __init__(self, owner_home: Path, *, store=None, engine=None, analysis_handler=None,
                 workspace_root_provider=None, skill_snapshot_provider=None, workspace_root=None, im_notifier=None):
        self.owner_home = Path(owner_home).absolute()
        _owner(self.owner_home, store)
        _owner(self.owner_home, engine)
        self.store, self.engine, self.im_notifier = store, engine, im_notifier
        self.analysis_handler = analysis_handler
        self.workspace_root_provider = workspace_root_provider
        self.skill_snapshot_provider = skill_snapshot_provider
        self.workspace_root = workspace_root

    # LLM: None表示无快照，空受限快照仍原样传递；项目只来自宿主且每次读取，不缓存其它Owner权限。
    # 函数用途: 取得可信宿主当前的技能发现范围。
    def _skill_options(self):
        return {
            'workspace_root': self.workspace_root_provider() if self.workspace_root_provider else self.workspace_root,
            'skill_snapshot': self.skill_snapshot_provider() if self.skill_snapshot_provider else None,
        }

    # LLM: 与直接execute共用无副作用预检，避免缺参动作先进入写操作账。
    # 函数用途: 按动作验证条件必填和参数Schema。
    def validate_invocation(self, params, context):
        return _validate(self, params)

    # LLM: 正式Store优先，默认每次绑定当前项目，模块损坏不回退；写入仍由Store显式scope裁决。
    # 函数用途: 延迟取得当前Owner工作流存储。
    def _store(self):
        return self.store if self.store is not None else WorkflowStore(self.owner_home, workspace_root=self._skill_options()['workspace_root'])

    # LLM: 默认引擎绑定本次项目并复用同一实例；项目路径变化时重建，保持外部注入analysis_handler跨调用生效。
    # 函数用途: 延迟取得Owner绑定的正式工作流执行器。
    def _engine(self):
        if self.engine is not None:
            return self.engine
        ws = self._skill_options()['workspace_root']
        cached = getattr(self, '_lazy_engine', None)
        if cached is None or getattr(self, '_lazy_engine_ws', None) != ws:
            from ..workflows.engine import WorkflowEngine
            cached = WorkflowEngine(self.owner_home, workspace_root=ws,
                skill_resolver=lambda step, context: _resolve_owner_skills(
                    self.owner_home, step, context, **self._skill_options()),
                skill_details_resolver=lambda references: [
                    _skill_descriptor(self.owner_home, reference, **self._skill_options())
                    for reference in references],
                analysis_handler=self.analysis_handler)
            self._lazy_engine = cached
            self._lazy_engine_ws = ws
        return cached

    # LLM: inspect失败是成功读取；运行失败保留断点机器码，CAS在Store原锁拒绝，不冒称写入已发生。
    # 函数用途: 统一工具结果与结构化错误，不吞中断。
    def execute(self, params):
        invalid = self.validate_invocation(params, None)
        if invalid is not None:
            return invalid
        try:
            data = _dispatch(self, params)
            ok = (params['action'] not in ('run', 'resume')
                  or data.get('status') == 'succeeded'
                  or (data.get('status') == 'paused_for_resume' and not data.get('needs_repair')))
            return _outcome('workflows', data, ok, 'TOOL_EXECUTION_FAILED' if not ok else '')
        except AssetPostCommitReadbackError as exc:
            return _post_commit_failure('workflows', exc)
        except (ValueError, KeyError, TypeError) as exc:
            if str(exc).startswith('ASSET_REVISION_CONFLICT:'):
                return _outcome('workflows', {'error_code': 'ASSET_REVISION_CONFLICT', 'error': str(exc)},
                                False, 'TOOL_INVALID_ARGUMENTS', effect_outcome='not_started')
            return _outcome('workflows', {'error': str(exc)}, False, 'TOOL_INVALID_ARGUMENTS')
        except (OSError, RuntimeError, ImportError) as exc:
            return _outcome('workflows', {'error': str(exc)}, False, 'TOOL_EXECUTION_FAILED')

    # LLM: 恢复默认拒绝依赖漂移，显式允许才透传；保存CAS只由原Store核验，输入与前缀沿冻结快照。
    # 函数用途: 分派CRUD、绑定、运行、补丁恢复或快照检查。
    def _dispatch(self, params):
        action = params['action']
        if action == 'resume':
            options = {'allow_script_drift': params['allow_script_drift']} if 'allow_script_drift' in params else {}
            return _plain(self._engine().resume_workflow_run(params['workflow_run_id'], params.get('from_step_id'),
                patched_step_output=params.get('patched_step_output'), updated_data_sources=params.get('updated_data_sources'),
                resolution_origin=params.get('resolution_origin'), **options))
        if action == 'inspect_run':
            return _plain(self._engine().store.load_run(params['workflow_run_id']))
        store = self._store()
        if action == 'list':
            return {'workflows': [_plain(item) for item in store.list_workflows()]}
        workflow_id = params['workflow_id']
        if action == 'save':
            options = {'expected_revision': params['expected_revision']} if 'expected_revision' in params else {}
            return _plain(store.save_workflow(_definition(params['definition'], workflow_id), **options))
        definition = store.get_workflow(workflow_id)
        if definition is None:
            raise ValueError('工作流不存在')
        if action == 'get':
            return _plain(definition)
        if action == 'delete':
            return {'deleted': store.delete_workflow(workflow_id)}
        if action in ('bind_skill', 'unbind_skill'):
            method = store.attach_skill_to_step if action == 'bind_skill' else store.detach_skill_from_step
            return _plain(method(workflow_id, params['step_id'], params['skill_id']))
        if action == 'preview':
            return self._preview(definition, params.get('inputs', {}))
        if action == 'run':
            return _plain(self._engine().run_workflow(_plain(definition), params.get('inputs', {})))
        raise ValueError('未知工作流action')

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
