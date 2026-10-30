# LLM: status只读本owner原registry/候选和当前快照，不要求开关或采用授权；adopted仍核当前claim/owner/hash，沿原幂等资源写账，不能确认内容。
# 模块用途: 查询完整学习状态并登记当前任务采用；模型采用不证明质量，也不能代替用户确认。
from __future__ import annotations

import json
import logging
from functools import partial
from importlib import import_module
from inspect import signature

from ..tooling.models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolModelSpec,
    ToolRuntimePolicy,
)
from ..user_space.owner_quota import OwnerQuotaExceeded, OwnerQuotaUnavailable
from .skill_learning_maintenance import LearningObservationSource, observe_learning
from .skill_learning_proposals import list_learning_proposals
from .skill_learning_publish import SkillLearningGateError
from .skill_learning_report import learned_skill_row
from .skill_learning_store import SkillLearningStore, SkillLearningStoreError
from .skill_search_tool import _snapshot_for


# LLM: 只表达纯参数问题；错误说明由宿主固定生成，不回显模型输入或磁盘异常。
# 类用途: 将可纠正的参数缺失与权限、存储失败分开。
class _FeedbackArgumentsError(ValueError):
    pass


# LLM: 规范handler只认action；旧operation在宿主版本化边界转换，采用仍需要准确技能编号。
# 函数用途: 在claim和业务存储前校验唯一反馈动作。
def _feedback_operation(params):
    if not isinstance(params, dict) or set(params) - {'action', 'skill_id'}:
        raise _FeedbackArgumentsError('参数须为对象，仅支持action、skill_id。')
    operation = params.get('action', 'status')
    if operation not in ('status', 'adopted'):
        raise _FeedbackArgumentsError('动作必须为 status / adopted。')
    if not isinstance(params.get('skill_id', ''), str):
        raise _FeedbackArgumentsError('skill_id 必须为字符串。')
    if operation == 'adopted' and not params.get('skill_id', '').strip():
        raise _FeedbackArgumentsError('action=adopted 时必须提供 skill_id')
    return operation


# LLM: 只投影候选元数据和原人工控制命令，不读取正文或确认候选；修订取原CAS字段。
# 函数用途: 为状态查询生成一条待确认候选摘要。
def _proposal_status(row):
    identifier, revision = row['proposal_id'], row['revision']
    return {'proposal_id': identifier, 'revision': revision, 'status': row['status'],
        'skill_name': row['decision']['name'], 'description': row['decision']['description'],
        'confirm_command': f'/skills learned confirm {identifier} {revision}',
        'preview_command': f'/skills learned proposal-show {identifier}'}


# LLM: 原登记仍是自学所有权权威；磁盘状态复用原报告推导，返回值不得暴露本机路径。
# 函数用途: 返回一个已发布技能的公开版本与维护状态。
def _registered_status(store, record):
    row = learned_skill_row(store, record)
    skill = {key: value for key, value in row.items() if key != 'path'}
    return {'ok': True, 'skill_id': f'owner:{record.name}', 'name': record.name, 'sha256': record.sha256,
        'status': row['state'], 'skill': skill, 'maintenance': record.maintenance}


# LLM: 可选Store方法优先，否则只借c6公开模块接口；模块缺失保持旧版兼容，不新建第二使用账。
# 函数用途: 取得附带统计的同owner读写函数，不在此执行记账。
def _usage_interface(store, method, function):
    callback = getattr(store, method, None)
    if callback is not None:
        return callback
    module_name = f'{__package__}.skill_usage_ledger'
    try:
        module = import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            return None
        raise
    return partial(getattr(module, function), store)


# LLM: 原报告可选c6摘要失败时明确未知；只读公共读口，不自动修复或确认内容。
# 函数用途: 给反馈状态附加原使用台账摘要，兼容Store和模块级接口。
def _with_usage_summary(store, result):
    try:
        reader = _usage_interface(store, 'usage_summary', 'load_skill_usage_summary')
        if reader is not None:
            result['skill_usage'] = reader() if callable(reader) else reader
    except Exception:  # noqa: BLE001 - 辅助统计失败不影响原状态查询。
        result['skill_usage_warning'] = 'SKILL_USAGE_UNAVAILABLE'
    return result


# LLM: c6新增队列回填参数时必须显式refill=False；旧无参数接口本身只读，按签名选择，不靠异常重做。
# 函数用途: 计算热队列请求数且不触发任何恢复写入。
def _pending_request_count(store):
    reader = store.pending_requests
    options = {'refill': False} if 'refill' in signature(reader).parameters else {}
    return len(reader(**options))


# LLM: 可选溢出候选沿原读口返回，不搬动文件；旧版无include_overflow参数时仍使用同一接口。
# 函数用途: 读取本owner完整待确认候选列表。
def _pending_learning_proposals(store):
    options = {'include_overflow': True} if 'include_overflow' in signature(list_learning_proposals).parameters else {}
    return [row for row in list_learning_proposals(store, **options) if row['status'] == 'pending']


# LLM: 不取执行scope或开启自学习；只读同owner原登记、候选和队列，计数明确是后台总结而非检索次数。
# 函数用途: 汇总已发布方法、待审候选、积压请求和每日总结调用口径。
def _feedback_overview(agent, store, registry, pending):
    skills = [_registered_status(store, registry.skills[name]) for name in sorted(registry.skills)]
    result = {'ok': True, 'self_learning_enabled': bool(getattr(agent.config, 'enable_self_learning', False)),
        'learned_skills_count': len(skills), 'learned_skills': [
            {'name': row['name'], 'version': row['skill']['version'], 'status': row['status'], 'sha256': row['sha256']} for row in skills],
        'pending_learning_proposals_count': len(pending), 'pending_learning_proposals': [_proposal_status(row) for row in pending],
        'pending_requests_count': _pending_request_count(store), 'daily_summarization_calls': {
            'owner_date': registry.daily_date, 'owner_calls': registry.daily_calls,
            'owner_daily_limit': int(getattr(agent.config, 'self_learning_daily_limit', 0) or 0),
            'note': '本身份后台自动总结 LLM 调用次数（非 skill_search 工具调用次数）'}}
    history = getattr(registry, 'daily_history', None)
    if history is not None:
        result['daily_history'] = history
    overflow = getattr(store, 'overflow_requests', None)
    if callable(overflow):
        result['overflow_requests_count'] = len(overflow())
    return _with_usage_summary(store, result)


# LLM: 精确匹配本owner登记/待审候选后才查固定快照；读状态不是采用，不扩大写入授权。
# 函数用途: 查询全部学习状态或一条具名技能、候选及非自学快照条目。
def _feedback_status(agent, store, reference):
    registry = store.load_registry()
    name = reference.removeprefix('owner:')
    if name in registry.skills:
        return _registered_status(store, registry.skills[name])
    pending = _pending_learning_proposals(store)
    if not reference:
        return _feedback_overview(agent, store, registry, pending)
    matches = [row for row in pending if reference == row['proposal_id'] or name == row['decision']['name']]
    if len(matches) == 1:
        return {'ok': True, **_proposal_status(matches[0])}
    if len(matches) > 1:
        raise _FeedbackArgumentsError('存在多个同名待确认候选，请使用完整 proposal_id 查询。')
    snapshot = _snapshot_for(agent)
    entry = snapshot.resolve(reference) if snapshot else None
    if entry is None:
        raise _FeedbackArgumentsError('未找到该 skill_id 对应的技能或待确认候选。')
    return {'ok': True, 'skill_id': entry.stable_id, 'name': entry.name, 'source': entry.source,
        'status': 'active_builtin_or_shared', 'note': '该技能为内置或共享技能，非本身份自学习生成'}


# LLM: 可选Store或c6公开使用台账都不提升质量或确认内容；只在原采用成功后记一次，统计异常仅警告。
# 函数用途: 为成功采用附加同owner使用事件，不要求c6已安装。
def _record_adopted_usage(store, entry, scope):
    try:
        recorder = _usage_interface(store, 'record_skill_usage', 'record_skill_usage_event')
        if callable(recorder):
            recorder(event='adopted', skill_id=entry.stable_id, source=entry.source, task_id=scope.root_task_id)
    except Exception:  # noqa: BLE001 - 已成功采用不能因辅助统计失败而反转。
        logging.getLogger(__name__).warning('Skill adopted 使用台账暂不可写，原采用结果保持。')


# LLM: adopted仍核自学习开关、当前固定owner技能/hash及真实执行claim；只登记authority=model，不赋予human确认。
# 函数用途: 沿原维护账登记当前任务采用并附加可选使用统计。
def _adopt_feedback(agent, store, reference):
    from ..conversation.tool_approval_scope import (
        tool_approval_scope,
        tool_approval_scope_is_current,
    )

    if not getattr(agent.config, 'enable_self_learning', False):
        raise ValueError('自学习未启用')
    snapshot = _snapshot_for(agent)
    entry = snapshot.resolve(reference) if snapshot else None
    if entry is None or entry.source != 'owner':
        raise ValueError('当前任务没有该自学方法授权')
    record = store.load_registry().skills.get(entry.name)
    if record is None or record.sha256 != entry.content_sha256:
        raise ValueError('当前方法版本不可登记')
    current = agent._current_run_params
    scope = tool_approval_scope(agent, str(current.run_id or current.task_id))
    if not tool_approval_scope_is_current(agent, scope):
        raise ValueError('当前执行已失效')
    attributes = getattr(current, 'task_attributes', {}) or {}
    result = observe_learning(store, entry.name, sha256=entry.content_sha256, task_id=scope.root_task_id,
        outcome='adopted', source=LearningObservationSource(run_id=scope.run_id, job_id=str(attributes.get('scheduler_job_id') or '')))
    _record_adopted_usage(store, entry, scope)
    return result


# LLM: actor/run只读宿主执行；status不要求采用授权，adopted仍使用同一固定快照和当前claim。
# 类用途: 提供只读学习状态与受约束的模型采用登记，不把读取当成功。
class SkillFeedbackTool(BaseTool):
    model_spec = ToolModelSpec(name='skill_feedback',description='确实采用本轮已授权的自学Skill时登记adopted；此声明不会增加有效性评分。status查看本owner状态。用户结果反馈通过/skills learned feedback，正式内容确认不能由此工具执行。',
        input_schema={'type':'object','properties':{'action':{'type':'string','enum':['adopted','status']},
            'skill_id':{'type':'string'}},'required':[],'additionalProperties':False})
    # LLM: 旧输入已在effect计算前归一为action；adopted仍是写操作，原幂等及技能资源域保持。
    # 常量用途: 查询只读，采用按原操作账去重和锁定技能资源。
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy('read_only', by_parameter=tuple(
            (key, (('status', 'read_only'), ('adopted', 'mutating'))) for key in ('action',))),
        idempotency_policy=IdempotencyPolicy('operation'),
        resource_scopes=ResourceScopePolicy(mode='from_arguments', parameter_names=('skill_id',),
            parameter_kinds={'skill_id': 'logical'}, resource_domains={'skill_id': 'skill_learning'}),
    )

    # LLM: 绑定owner Agent，不构造第二份存储或授权。
    # 函数用途: 保存当前宿主供每次调用核验。
    def __init__(self,agent):
        self.agent = agent

    # LLM: 纯参数检查发生在操作claim前，缺少采用编号或冲突别名不能先产生写账。
    # 函数用途: 提前反馈可纠正的参数错误。
    def validate_invocation(self, params, context):
        try:
            _feedback_operation(params)
        except _FeedbackArgumentsError as exc:
            return ToolHandlerOutcome('skill_feedback', False, str(exc), error_code='SKILL_FEEDBACK_UNAVAILABLE', effect_outcome='not_started')
        return None

    # LLM: status读取本owner状态，adopted沿原claim与固定hash写账；仅固定参数错误可公开，其余异常不携正文或路径。
    # 函数用途: 查询全景/具名状态或登记本任务采用，不代替人类确认。
    def execute(self,params):
        try:
            operation = _feedback_operation(params)
            store = SkillLearningStore.for_home(self.agent.home_paths)
            reference = params.get('skill_id', '').strip()
            result = _feedback_status(self.agent, store, reference) if operation == 'status' else _adopt_feedback(self.agent, store, reference)
            return ToolHandlerOutcome('skill_feedback',True,json.dumps(result,ensure_ascii=False))
        except _FeedbackArgumentsError as exc:
            return ToolHandlerOutcome('skill_feedback',False,str(exc),error_code='SKILL_FEEDBACK_UNAVAILABLE')
        except (AttributeError,ValueError,OSError,RuntimeError,SkillLearningStoreError,SkillLearningGateError,OwnerQuotaExceeded,OwnerQuotaUnavailable):
            return ToolHandlerOutcome('skill_feedback',False,'无法核验当前方法、任务或存储，未登记反馈。',error_code='SKILL_FEEDBACK_UNAVAILABLE')
