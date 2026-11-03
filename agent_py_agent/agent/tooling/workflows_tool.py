# LLM: 工作流只由可信Owner的领域Store和Engine执行；工具不自建运行账本，不从正文猜断点或授权。
# 模块用途: 提供工作流CRUD、绑定与运行；直接使用纯schema及资产完整定义构造，不接管执行协议。
from __future__ import annotations

import copy
from contextlib import nullcontext
from pathlib import Path

from ..assets import AssetPostCommitReadbackError
from ..assets.descriptor import asset_descriptor_from_definition
from ..assets.skill_adapter import get_owner_skill_descriptor, list_owner_skill_descriptors
from ..assets.workflow_store import WorkflowDefinition, WorkflowStore, with_workflow_step_refs
from ..capability.skills import split_skill_frontmatter
from ..workflows.business_scope import (
    WORKFLOW_ACTION_FIELDS,
    WORKFLOW_READ_ACTIONS,
    validate_business_request,
)
from ..workflows.models import redact_bearer_values
from ..workflows.run_store import WorkflowExecutionError
from .models import BaseTool
from .scripts_tool import (
    _dispatch,
    _outcome,
    _owner,
    _plain,
    _policy,
    _post_commit_failure,
    _validate,
)
from .tool_spec import _spec


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


# LLM: 路径由构造器Owner固定；纯schema沿tool_spec，运行/恢复/删除仍保持原危险动作分类。
# 类用途: 对Agent暴露可管理和恢复的工作流工具。
class WorkflowsTool(BaseTool):
    ACTIONS = ('list', 'get', 'save', 'delete', 'bind_skill', 'unbind_skill', 'run', 'resume', 'inspect_run', 'preview',
               *WORKFLOW_ACTION_FIELDS)
    READ_ONLY_ACTIONS = ('list', 'get', 'inspect_run', 'preview', *WORKFLOW_READ_ACTIONS)
    model_spec = _spec('workflows', '管理Owner工作流、自动脚本/Skill引用、动态技能绑定、运行与断点续跑；步骤输出含_pause_for_resume=true可正常暂停等待确认，再用resume+patched_step_output续跑。',
        list(ACTIONS), {
            'expected_revision': {'type': ['integer', 'null'], 'minimum': 0},
            'idempotency_key': {'type': 'string', 'minLength': 1, 'maxLength': 256},
            **{key: {'type': 'string', 'minLength': 1, 'maxLength': 128}
               for key in ('workflow_ref', 'space_id', 'project_ref', 'resolution_ref')},
            'cursor': {'type': 'string', 'minLength': 1, 'maxLength': 2048},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
            'query': {'type': 'string', 'maxLength': 256},
            'scope_mode': {'type': 'string', 'enum': ['owner_global', 'spaces', 'unassigned', 'all']},
            'lifecycle': {'type': 'string', 'enum': ['active', 'recycled', 'all']},
            'protected': {'type': 'boolean'},
            'policy': {'type': 'object'},
            'concurrency_policy': {'type': 'string', 'enum': ['allow', 'skip_if_running', 'queue'],
                'description': 'run/resume并发策略，默认allow；skip_if_running跳过重叠，queue有界FIFO等待当前执行释放'},
            'scope': {'type': 'object', 'properties': {
                'scope_mode': {'type': 'string', 'enum': ['owner_global', 'spaces', 'unassigned']},
                'space_ids': {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': 128}}},
                'required': ['scope_mode', 'space_ids'], 'additionalProperties': False},
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
    runtime_policy = _policy('workflows', READ_ONLY_ACTIONS, ('run', 'resume', 'delete', 'workflows.run',
        'workflow_runs.resume', 'workflow_runs.cancel', 'workflows.recycle', 'workflows.cleanup.run'))

    # LLM: Owner与项目/快照/模型提供者仅由可信宿主注入，模型实参不能替换；无分析器不注入假实现。
    # 函数用途: 固定当前Owner及运行时依赖，构造阶段不执行资产。
    def __init__(self, owner_home: Path, *, store=None, engine=None, analysis_handler=None,
                 workspace_root_provider=None, skill_snapshot_provider=None, workspace_root=None, im_notifier=None,
                 runner_options_provider=None, business_service=None, business_actor_provider=None,
                 run_params_provider=None, schedule_ids_provider=None):
        self.owner_home = Path(owner_home).absolute()
        _owner(self.owner_home, store)
        _owner(self.owner_home, engine)
        self.store, self.engine, self.im_notifier = store, engine, im_notifier
        self.analysis_handler = analysis_handler
        self.workspace_root_provider = workspace_root_provider
        self.skill_snapshot_provider = skill_snapshot_provider
        self.workspace_root = workspace_root
        self.runner_options_provider = runner_options_provider
        self.business_service, self.business_actor_provider = business_service, business_actor_provider
        self.run_params_provider, self.schedule_ids_provider = run_params_provider, schedule_ids_provider
        _owner(self.owner_home, getattr(business_service, 'store', None))

    # LLM: 每次run/resume冻结当前宿主上下文到本次浅拷贝engine，整次调用只用它；缓存engine仅共享RunStore/回调/解析器，
    #   不承载任一调用的runner，等待中的运行不会改用后启动运行的身份或路径范围；显式注入执行器保持原样。
    # 函数用途: 给本次默认执行器配置可信脚本范围和宿主来源屏蔽。
    def _execution_engine(self):
        engine = copy.copy(self._engine())
        if self.engine is None:
            options = self._skill_options()
            engine.skill_resolver = lambda step, context: _resolve_owner_skills(self.owner_home, step, context, **options)
            engine.skill_details_resolver = lambda ids: [_skill_descriptor(self.owner_home, reference, **options) for reference in ids]
        if self.run_params_provider is not None:
            engine = copy.copy(engine)
            params = self.run_params_provider()
            on_chunk = getattr(params, 'effective_on_chunk', None)
            sink = getattr(on_chunk, 'write_workflow_step_progress', None)
            engine.step_observer = sink if callable(sink) else None
            bind = getattr(engine.analysis_handler, 'bind_run_params', None)
            if callable(bind):
                engine.analysis_handler = bind(params)
        if self.engine is None and self.runner_options_provider is not None:
            from ..workflows.script_runner import ScriptRunner
            engine = copy.copy(engine)
            engine.runner = ScriptRunner(self.owner_home, workspace_root=self._skill_options()['workspace_root'],
                                         **self.runner_options_provider())
        return engine

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
        if isinstance(params, dict) and 'concurrency_policy' in params and params['concurrency_policy'] not in (
                'allow', 'skip_if_running', 'queue'):
            return _outcome('workflows', {'error_code': 'INVALID_CONCURRENCY_POLICY'}, False, 'TOOL_INVALID_ARGUMENTS')
        if isinstance(params, dict) and params.get('action') in WORKFLOW_ACTION_FIELDS:
            from .input_schema import validate_tool_input
            if not validate_tool_input(params, self.model_spec.input_schema).ok:
                return _outcome('workflows', {'error': '参数不符合工具Schema'}, False, 'TOOL_INVALID_ARGUMENTS')
            try:
                validate_business_request(params['action'], self._business_request(params),
                    expected_revision=params.get('expected_revision'), idempotency_key=params.get('idempotency_key', ''))
            except (ValueError, TypeError) as exc:
                return _outcome('workflows', {'error': str(exc)}, False, 'TOOL_INVALID_ARGUMENTS')
            return None
        return _validate(self, params)

    # LLM: 并发策略属于宿主执行装配，不把它混入业务身份或越过业务请求校验。
    # 函数用途: 从工具参数中提取原业务服务可接受的请求正文。
    @staticmethod
    def _business_request(params):
        return {key: value for key, value in params.items()
                if key not in ('action', 'expected_revision', 'idempotency_key', 'concurrency_policy')}

    def _business_engine(self, owner_home, *, run_authorizer, agent_steps):
        if Path(owner_home).absolute() != self.owner_home:
            raise PermissionError('WORKFLOW_OWNER_MISMATCH')
        engine = copy.copy(self._execution_engine())
        original_authorizer = engine.run_authorizer

        def authorize(record, phase):
            previous = original_authorizer(record, phase) if callable(original_authorizer) else None
            current = run_authorizer(record, phase)
            return previous if current is None else current

        engine.run_authorizer = authorize
        if agent_steps is not None:
            engine.agent_steps = agent_steps
        return engine

    # LLM: 原业务服务负责身份/幂等校验，本次engine只绑定宿主端口；持久准入回执不替代执行事实。
    # 函数用途: 调用原业务入口并保持并发准入在首次调用和同键重试中的返回语义。
    def _dispatch_business(self, params):
        if self.business_service is None or not callable(self.business_actor_provider):
            raise PermissionError('WORKFLOW_BUSINESS_HOST_UNAVAILABLE')
        _owner(self.owner_home, self.business_service.store)
        backend = copy.copy(self.business_service)
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
            result = backend.execute(params['action'], self._business_request(params), actor=self.business_actor_provider(),
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
            business = params['action'] in WORKFLOW_ACTION_FIELDS
            data = self._dispatch_business(params) if business else _dispatch(self, params)
            ok = (params['action'] not in ('run', 'resume')
                  or data.get('status') in ('succeeded', 'queued')
                  or data.get('status') == 'skipped' and data.get('error_code') == 'WORKFLOW_RUN_SKIPPED_ALREADY_RUNNING'
                  or (data.get('status') == 'paused_for_resume' and not data.get('needs_repair')))
            if business:
                ok = data.get('state') not in ('failed', 'blocked')
            return _outcome('workflows', data, ok, 'TOOL_EXECUTION_FAILED' if not ok else '')
        except AssetPostCommitReadbackError as exc:
            return _post_commit_failure('workflows', exc)
        except WorkflowExecutionError as exc:
            return _outcome('workflows', exc.receipt, False, 'TOOL_EXECUTION_FAILED',
                            effect_outcome=exc.receipt['effect_outcome'], failure_stage='execution')
        except PermissionError as exc:
            return _outcome('workflows', {'error': str(exc)}, False, 'TOOL_PERMISSION_DENIED', effect_outcome='not_started')
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
            options = {'concurrency_policy': params.get('concurrency_policy', 'allow')}
            if 'allow_script_drift' in params:
                options['allow_script_drift'] = params['allow_script_drift']
            return _plain(self._execution_engine().resume_workflow_run(params['workflow_run_id'], params.get('from_step_id'),
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
            return _plain(store.save_workflow(self._topology_refs(_definition(params['definition'], workflow_id)), **options))
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
            return _plain(self._execution_engine().run_workflow(_plain(definition), params.get('inputs', {}),
                          concurrency_policy=params.get('concurrency_policy', 'allow')))
        raise ValueError('未知工作流action')

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
