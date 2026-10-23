# LLM: 工作流只由可信Owner的领域Store和Engine执行；工具不自建运行账本，不从正文猜断点或授权。
# 模块用途: 提供工作流CRUD、动态Skill绑定、运行、断点续跑及快照检查的Agent工具。
from __future__ import annotations

import inspect
from functools import partial
from pathlib import Path
from types import SimpleNamespace

from ..common.nofollow_fs import read_text_beneath
from ..workflows.models import redact_bearer_values
from .models import BaseTool
from .scripts_tool import (
    _descriptor,
    _dispatch,
    _optional,
    _outcome,
    _owner,
    _plain,
    _policy,
    _spec,
    _validate,
)


# LLM: steps是脚本/Skill引用的唯一权威；保存前校验，再由领域定义恢复完整严格类型。
# 函数用途: 校验工作流定义并从步骤重算refs，支持无领域模块时的同形JSON协议。
def _definition(value, workflow_id):
    value = _plain(value)
    descriptor = _plain(_descriptor(value['descriptor'], workflow_id, 'workflow'))
    steps = value['steps']
    if not isinstance(steps, list) or len({step['step_id'] for step in steps}) != len(steps):
        raise ValueError('steps必须为无重复step_id的列表')
    scripts, skills = [], []
    for step in steps:
        from ..workflows.models import WORKFLOW_ON_ERROR_POLICIES
        if step.get('on_error', 'escalate_to_agent') not in WORKFLOW_ON_ERROR_POLICIES:
            raise ValueError(f"Unsupported on_error policy: {step['on_error']}")
        if step['mode'] not in ('deterministic_script', 'llm_skill_analysis', 'hybrid'):
            raise ValueError('未知步骤mode')
        if step.get('script_id'):
            scripts.append(step['script_id'])
        skills.extend(step.get('skill_ids', []))
    descriptor['refs'] = {**descriptor.get('refs', {}), 'scripts': list(dict.fromkeys(scripts)), 'skills': list(dict.fromkeys(skills))}
    value['descriptor'] = descriptor
    module = _optional('assets.workflow_store')
    return module.WorkflowDefinition.from_dict(value) if module else value


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


# LLM: 只匹配当前Owner已发现描述符，不执行Skill正文；无匹配时保留显式编号。
# 函数用途: 根据步骤的编号、名称和标签解析规范技能ID。
def _resolve_owner_skills(owner_home, step, context):
    from ..assets.skill_adapter import list_owner_skill_descriptors
    ids, tags = getattr(step, 'skill_ids', ()), getattr(step, 'skill_tags', ())
    found = []
    for descriptor in list_owner_skill_descriptors(owner_home):
        text = ' '.join(text for key in ('name', 'summary', 'refs', 'tags') for text in _skill_text(descriptor.get(key)))
        if any(item in (descriptor['id'], descriptor['name']) for item in ids) or any(tag in text for tag in tags):
            found.append(descriptor['id'])
    return list(dict.fromkeys(found or ids))


# LLM: 旧资产API缺include_body时复用同一安全发现顺序，正文仍从Owner根nofollow读取；不拼请求ID路径。
# 函数用途: 兼容当前只返回元数据的技能getter，并优先使用新增的安全正文接口。
def _skill_descriptor(owner_home, skill_id):
    from ..assets.skill_adapter import (
        _owner_skill_paths,
        extract_skill_descriptor,
        get_owner_skill_descriptor,
    )
    if 'include_body' in inspect.signature(get_owner_skill_descriptor).parameters:
        return get_owner_skill_descriptor(owner_home, skill_id, include_body=True)
    descriptor = get_owner_skill_descriptor(owner_home, skill_id)
    for path in _owner_skill_paths(Path(owner_home), ()):
        if extract_skill_descriptor(path)['id'] == descriptor['id']:
            body = read_text_beneath(owner_home, path.relative_to(owner_home).parts)
            return {**descriptor, 'body_markdown': body or ''}
    raise FileNotFoundError('Owner技能正文不存在')


# LLM: 正文仅由Owner安全发现接口加载，缺失技能不伪造正文；先脱敏再截断，不执行技能或模型。
# 函数用途: 为未注入分析器的工具提供真实技能步骤预览和结构化分析摘要。
def _analysis_summary(step, resolved_skills, context, *, owner_home):
    summaries = []
    for skill_id in resolved_skills:
        try:
            descriptor = _skill_descriptor(owner_home, skill_id)
        except FileNotFoundError:
            continue
        safe = redact_bearer_values(descriptor)
        lines = safe.get('body_markdown', '').splitlines()
        if lines and lines[0].strip() == '---':
            lines = lines[next(i for i in range(1, len(lines)) if lines[i].strip() == '---') + 1:]
        summaries.append({key: safe.get(key, '') for key in ('id', 'name', 'summary')} | {
            'procedure_preview': '\n'.join(lines).strip()[:200]})
    return {'step_id': step.step_id, 'mode': step.mode, 'resolved_skills': list(resolved_skills),
            'analyzed_keys': sorted((context or {}).keys()), 'status': 'analyzed', 'skill_summaries': summaries}


# LLM: 当前回调只读宿主绑定Owner的技能正文，替换为模型处理器后不继承零模型标记。
# 函数用途: 适配引擎回调顺序并传入可信Owner，不从步骤参数获取路径。
def _default_analysis_handler(step, context, resolved, *, owner_home):
    return _analysis_summary(SimpleNamespace(**step), resolved, context, owner_home=owner_home)


_default_analysis_handler.llm_involved = False


# LLM: 所有路径由构造器Owner固定，model参数只含安全逻辑ID；运行/恢复/删除保持危险动作分类。
# 类用途: 对Agent暴露可管理和恢复的工作流工具。
class WorkflowsTool(BaseTool):
    model_spec = _spec('workflows', '管理Owner工作流、自动脚本/Skill引用、动态技能绑定、运行与断点续跑；步骤输出含_pause_for_resume=true可正常暂停等待确认，再用resume+patched_step_output续跑。',
        ['list', 'get', 'save', 'delete', 'bind_skill', 'unbind_skill', 'run', 'resume', 'inspect_run'], {
            **{key: {'type': 'string'} for key in ('workflow_id', 'step_id', 'skill_id', 'workflow_run_id', 'from_step_id')},
            'definition': {'type': 'object', 'description': (
                '工作流定义对象：{descriptor: {name, access_targets:[], data_sources:[], credential_refs:[], io_contract:{}}, '
                'body_markdown: 说明文本, steps: [{step_id, name, mode(deterministic_script|llm_skill_analysis|hybrid), '
                'script_id(可选), skill_ids(可选列表), condition(可选:{"step_id":"前步ID或$inputs","key":"字段","equals":值}), '
                'inputs_map(可选:{"入参":{"step_id":"前步ID或$inputs","key":"字段"}}), on_error(escalate_to_agent|fail|skip)}]}')},
            'patched_step_output': {'type': 'object', 'description': '断点续跑时注入并经脚本IO契约校验的步骤输出（用于人工确认或故障修复）'},
            'updated_data_sources': {'type': ['object', 'array'], 'description': '断点续跑时更新的数据源配置'},
            'inputs': {'type': 'object', 'description': '工作流运行输入参数字典（可在step的condition或inputs_map中用step_id="$inputs"引用）'}})
    runtime_policy = _policy('workflows', ('list', 'get', 'inspect_run'), ('run', 'resume', 'delete'))

    # LLM: Store和Engine可用同协议替换；构造无资产执行，Owner根不接受工具实参覆盖。
    # 函数用途: 保存可信Owner与可选领域接口，允许并行模块未就绪时注入。
    def __init__(self, owner_home: Path, *, store=None, engine=None):
        self.owner_home = Path(owner_home).absolute()
        _owner(self.owner_home, store)
        _owner(self.owner_home, engine)
        self.store, self.engine = store, engine

    # LLM: 与直接execute共用无副作用预检，避免缺参动作先进入写操作账。
    # 函数用途: 按动作验证条件必填和参数Schema。
    def validate_invocation(self, params, context):
        return _validate(self, params)

    # LLM: 正式Store优先，模块损坏不静默回退；所有CRUD与另一工具共享c4权威路径。
    # 函数用途: 延迟取得当前Owner工作流存储。
    def _store(self):
        if self.store is None:
            module = _optional('assets.workflow_store')
            if module is None:
                raise RuntimeError('资产模块未就绪，请宿主注入WorkflowStore协议')
            self.store = module.WorkflowStore(self.owner_home)
        return self.store

    # LLM: 默认回调绑定可信Owner且标明不调模型；显式注入的Engine和后续模型回调替换仍优先。
    # 函数用途: 延迟取得同Owner执行器，安全加载技能正文摘要。
    def _engine(self):
        if self.engine is None:
            module = _optional('workflows')
            if module is None:
                raise RuntimeError('工作流模块未就绪，请宿主注入WorkflowEngine协议')
            handler = partial(_default_analysis_handler, owner_home=self.owner_home)
            handler.llm_involved = False
            self.engine = module.WorkflowEngine(self.owner_home,
                skill_resolver=lambda step, context: _resolve_owner_skills(self.owner_home, step, context),
                analysis_handler=handler)
        return self.engine

    # LLM: inspect历史失败是成功读取；正常完成或needs_repair=False的主动确认断点均返回ok=True，仅失败或需修复断点返回执行失败。
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
        except (ValueError, KeyError, TypeError) as exc:
            return _outcome('workflows', {'error': str(exc)}, False, 'TOOL_INVALID_ARGUMENTS')
        except (OSError, RuntimeError, ImportError) as exc:
            return _outcome('workflows', {'error': str(exc)}, False, 'TOOL_EXECUTION_FAILED')

    # LLM: 恢复只允许显式修复输出与数据源变更；定义、输入与前缀仍由冻结快照裁决。
    # 函数用途: 分派CRUD、绑定、运行、补丁恢复或快照检查。
    def _dispatch(self, params):
        action = params['action']
        if action == 'resume':
            return _plain(self._engine().resume_workflow_run(params['workflow_run_id'], params.get('from_step_id'),
                patched_step_output=params.get('patched_step_output'), updated_data_sources=params.get('updated_data_sources')))
        if action == 'inspect_run':
            return _plain(self._engine().store.load_run(params['workflow_run_id']))
        store = self._store()
        if action == 'list':
            return {'workflows': [_plain(item) for item in store.list_workflows()]}
        workflow_id = params['workflow_id']
        if action == 'save':
            return _plain(store.save_workflow(_definition(params['definition'], workflow_id)))
        definition = store.get_workflow(workflow_id)
        if definition is None:
            raise ValueError('工作流不存在')
        if action == 'get':
            return _plain(definition)
        if action == 'delete':
            return {'deleted': store.delete_workflow(workflow_id)}
        if action in ('bind_skill', 'unbind_skill'):
            return self._bind(params, _plain(definition))
        if action == 'run':
            return _plain(self._engine().run_workflow(_plain(definition), params.get('inputs', {})))
        raise ValueError('未知工作流action')

    # LLM: 仅分析/混合步骤可挂Skill；按ID去重、解绑后重算refs，不修改其他步骤或资产。
    # 函数用途: 为指定分析步挂载或解绑技能，并保存正式定义。
    def _bind(self, params, definition):
        step = next((item for item in definition['steps'] if item['step_id'] == params['step_id']), None)
        if step is None or step['mode'] not in ('llm_skill_analysis', 'hybrid'):
            raise ValueError('目标必须是现有分析或混合步骤')
        skill = params['skill_id']
        skills = step.get('skill_ids', [])
        step['skill_ids'] = list(dict.fromkeys([*skills, skill])) if params['action'] == 'bind_skill' else [item for item in skills if item != skill]
        return _plain(self._store().save_workflow(_definition(definition, params['workflow_id'])))
