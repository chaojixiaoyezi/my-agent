import json
import logging
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.skill_learning_maintenance import (
    LearningObservationSource,
    control_learning,
    maintain_learning,
    observe_learning,
    recommended_learning,
)
from agent_py_agent.agent.capability.skill_learning_publish import (
    SkillLearningGateError,
    remove_learned_skill,
)
from agent_py_agent.agent.tooling.versioned_input_decoder import decode_tool_input
from agent_py_agent.tests.test_background_tool_approval import background
from agent_py_agent.tests.test_skill_learning import NAME, _output, _publish_first, _runtime

_FIXTURES = (background,)


def _learned(tmp_path):
    ctx = _runtime(tmp_path,_output())
    _publish_first(ctx)
    return ctx


def test_model_adoption_does_not_prove_quality_and_periodic_job_cannot_inflate(tmp_path):
    ctx = _learned(tmp_path)
    row = ctx.store.load_registry().skills[NAME]
    for number in range(5):
        observe_learning(ctx.store,NAME,sha256=row.sha256,task_id=f'task-{number}',outcome='adopted',source=LearningObservationSource(job_id='same-job'))
        observe_learning(ctx.store,NAME,sha256=row.sha256,task_id=f'task-{number}',outcome='effective',source=LearningObservationSource(authority='human',job_id='same-job',actor_id='human-test',receipt_id=f'control-{number}'))
    data = ctx.store.load_registry().skills[NAME].maintenance
    assert data['quality']=='trial' and len(data['observations'])==6
    observe_learning(ctx.store,NAME,sha256=row.sha256,task_id='independent-task',outcome='adopted')
    observe_learning(ctx.store,NAME,sha256=row.sha256,task_id='independent-task',outcome='effective',source=LearningObservationSource(authority='human',actor_id='human-test',receipt_id='control-new'))
    data = ctx.store.load_registry().skills[NAME].maintenance
    assert data['quality']=='trial' and data['helped_count']==2 and len(data['observations'])==8
    for number in range(3):
        observe_learning(ctx.store,NAME,sha256=row.sha256,task_id=f'network-{number}',outcome='environment',source=LearningObservationSource(authority='human',actor_id='human-test',receipt_id=f'control-net-{number}'))
    assert ctx.store.load_registry().skills[NAME].maintenance['quality']=='trial'
    with pytest.raises(ValueError):
        observe_learning(ctx.store,NAME,sha256=row.sha256,task_id='forged',outcome='effective',source=LearningObservationSource())


def test_idle_baseline_protection_cold_archive_restore_and_user_remove(tmp_path):
    ctx = _learned(tmp_path)
    now = time.time()
    maintain_learning(ctx.store,mode='autonomous',cool_seconds=10,archive_seconds=20,now=now,protection_complete=True)
    assert ctx.store.load_registry().skills[NAME].maintenance['state']=='trial'
    control_learning(ctx.store,NAME,operation='pin')
    maintain_learning(ctx.store,mode='autonomous',cool_seconds=10,archive_seconds=20,now=now+30,protection_complete=True)
    assert ctx.store.skill_path(NAME).exists()
    control_learning(ctx.store,NAME,operation='unpin')
    maintain_learning(ctx.store,mode='autonomous',cool_seconds=10,archive_seconds=20,now=now+30,protected_ids=[f'owner:{NAME}'],protection_complete=True)
    assert ctx.store.skill_path(NAME).exists()
    maintain_learning(ctx.store,mode='autonomous',cool_seconds=10,archive_seconds=20,now=now+30,protection_complete=False)
    assert ctx.store.load_registry().skills[NAME].maintenance['state']=='trial' and ctx.store.skill_path(NAME).exists()
    maintain_learning(ctx.store,mode='autonomous',cool_seconds=10,archive_seconds=20,now=now+30,protection_complete=True)
    assert ctx.store.load_registry().skills[NAME].maintenance['state']=='archived' and not ctx.store.skill_path(NAME).exists()
    control_learning(ctx.store,NAME,operation='restore')
    assert ctx.store.skill_path(NAME).exists()
    remove_learned_skill(ctx.store,NAME)
    with pytest.raises(SkillLearningGateError):
        control_learning(ctx.store,NAME,operation='restore')


def test_archive_registry_failure_restores_original_directory(tmp_path,monkeypatch):
    ctx = _learned(tmp_path)
    now = time.time()
    maintain_learning(ctx.store,now=now)
    maintain_learning(ctx.store,mode='autonomous',cool_seconds=10,now=now+15,protection_complete=True)
    before = ctx.store.skill_path(NAME).read_bytes()
    def fail(*args):
        raise OSError('synthetic registry unavailable')
    monkeypatch.setattr(ctx.store,'save_registry',fail)
    with pytest.raises(OSError):
        maintain_learning(ctx.store,mode='autonomous',archive_seconds=20,now=now+30,protection_complete=True)
    assert ctx.store.skill_path(NAME).read_bytes()==before


def test_recommendation_limit_is_not_library_capacity(tmp_path):
    ctx = _learned(tmp_path)
    registry = ctx.store.load_registry()
    record = registry.skills[NAME]
    registry = replace(registry,skills={f'skill-{number}':replace(record,name=f'skill-{number}') for number in range(52)})
    assert len(recommended_learning(registry,50))==50 and len(registry.skills)==52
    pinned = replace(registry.skills['skill-51'],maintenance={'pinned':True})
    registry = replace(registry,skills={**registry.skills,'skill-51':pinned})
    assert 'skill-51' in recommended_learning(registry,50)


def test_real_library_can_save_more_than_recommendation_limit(tmp_path):
    from agent_py_agent.agent.capability.skill_learning_prompt import parse_skill_learning_decision
    from agent_py_agent.agent.capability.skill_learning_publish import (
        PublishRequest,
        publish_learned_skill,
    )
    ctx = _runtime(tmp_path)
    for number in range(52):
        decision = parse_skill_learning_decision(_output(name=f'method-{number:03d}'),frozenset())
        publish_learned_skill(ctx.store,PublishRequest(decision,max_skills=0))
    registry = ctx.store.load_registry()
    assert len(registry.skills)==52
    snapshot = ctx.skills.snapshot_for(force_reload=True)
    owners = [entry for entry in snapshot.enabled_entries() if entry.source == "owner"]
    assert len(owners)==52 and sum(entry.recommended for entry in owners)==50
    hidden = next(entry for entry in snapshot.entries if not entry.recommended)
    assert snapshot.read_body(hidden.stable_id)


def test_disabled_and_pending_import_do_not_reappear_from_cached_snapshot(tmp_path):
    ctx = _learned(tmp_path)
    old = ctx.skills.snapshot_for()
    control_learning(ctx.store,NAME,operation='disable')
    disabled = ctx.skills.snapshot_for()
    assert disabled is not old and disabled.resolve(f'owner:{NAME}') is None
    control_learning(ctx.store,NAME,operation='enable')
    with ctx.store.state_lock():
        registry = ctx.store.load_registry()
        ctx.store.save_registry(replace(registry,import_operations={'op':{'status':'preparing','target_names':[NAME]}}))
    assert ctx.skills.snapshot_for().resolve(f'owner:{NAME}') is None
    with ctx.store.state_lock():
        registry = ctx.store.load_registry()
        ctx.store.save_registry(replace(registry,import_operations={'op':{'status':'committed','target_names':[NAME]}}))
    assert ctx.skills.snapshot_for().resolve(f'owner:{NAME}') is not None


def test_deployment_budget_shared_and_blocks_before_extra_model_or_owner_charge(tmp_path):
    from agent_py_agent.tests.test_skill_learning import _ctx
    ctx = _runtime(tmp_path,_output())
    ctx.service.runtime = replace(ctx.service.runtime,deployment_root=tmp_path/'deployment')
    ctx.service.settings = replace(ctx.service.settings,global_daily_limit=1)
    _publish_first(ctx)
    before = ctx.store.load_registry().daily_calls
    ctx.service.enqueue_from_finalize(_ctx('run-2'))
    assert ctx.service.run_pending().status=='global_daily_limit'
    assert len(ctx.backend.prompts)==1 and ctx.store.load_registry().daily_calls==before


def test_background_learning_is_explicit_and_global_reservations_are_atomic(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from agent_py_agent.agent.capability.skill_learning_budget import reserve_deployment_learning
    from agent_py_agent.agent.capability.skill_learning_request import build_learning_request
    from agent_py_agent.tests.test_skill_learning import _ctx
    assert build_learning_request(_ctx(source='background_main_agent'),3) is None
    # W6 #25：后台开关不是来源证明，后台主任务只经 task_learning_bridge 的可信完成入队。
    assert build_learning_request(_ctx(source='background_main_agent'),3,include_background=True) is None
    with ThreadPoolExecutor(max_workers=8) as pool:
        decisions = list(pool.map(lambda _:reserve_deployment_learning(tmp_path,3),range(20)))
    assert sum(decisions)==3


# LLM: 直接handler使用规范action；采用仍须真实当前claim，不能恢复旧operation业务分支。
# 函数用途: 验证模型只能登记采用且关闭自学习后不可写账。
def test_feedback_tool_only_records_model_adoption_for_current_claim(background,monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.capability import skill_feedback_tool
    from agent_py_agent.agent.capability.skill_learning_prompt import parse_skill_learning_decision
    from agent_py_agent.agent.capability.skill_learning_publish import (
        PublishRequest,
        publish_learned_skill,
    )
    from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
    agent = background.agent
    agent.config.enable_self_learning=True
    agent._current_run_params=SimpleNamespace(run_id=background.task_id,task_id=background.task_id,task_attributes={})
    store = SkillLearningStore.for_home(agent.home_paths)
    publish_learned_skill(store,PublishRequest(parse_skill_learning_decision(_output(),frozenset())))
    snapshot = agent.skills_service.snapshot_for(force_reload=True)
    monkeypatch.setattr(skill_feedback_tool,'_snapshot_for',lambda _agent:snapshot)
    tool = skill_feedback_tool.SkillFeedbackTool(agent)
    assert tool.execute({'action':'adopted','skill_id':f'owner:{NAME}'}).ok
    observation = store.load_registry().skills[NAME].maintenance['observations'][0]
    assert observation['authority']=='model' and observation['run_id']==background.task_id
    assert not tool.execute({'action':'effective','skill_id':f'owner:{NAME}'}).ok
    agent.config.enable_self_learning=False
    assert not tool.execute({'action':'adopted','skill_id':f'owner:{NAME}'}).ok


# LLM: 真实登记、候选与队列都只放tmp owner；模型没有run或发布权也应能只读查询。
# 函数用途: 准备已发布方法和未确认候选共存的反馈测试。
def _feedback_status_case(tmp_path):
    from agent_py_agent.agent.capability.skill_feedback_tool import SkillFeedbackTool
    from agent_py_agent.agent.capability.skill_learning_prompt import parse_skill_learning_decision
    from agent_py_agent.agent.capability.skill_learning_proposals import propose_learning
    from agent_py_agent.agent.capability.skill_learning_publish import PublishRequest

    ctx = _learned(tmp_path)
    proposal = propose_learning(ctx.store, PublishRequest(
        parse_skill_learning_decision(_output(name='pending-method'), frozenset()), {'request_key': 'pending-test'}))
    ctx.store.enqueue({'request_key': '1' * 16})
    agent = SimpleNamespace(home_paths=ctx.home, config=SimpleNamespace(enable_self_learning=False, self_learning_daily_limit=5),
                            _current_skill_snapshot=ctx.skills.snapshot_for())
    return ctx, SkillFeedbackTool(agent), proposal.reason


# LLM: 旧参数仅由显式v1解码器在effect前转换；不能把未解码的operation当规范业务输入。
# 函数用途: 验证唯一action声明及规范化后采用的写入级别。
@pytest.mark.parametrize('params,effect', [({}, 'read_only'), ({'operation': 'status'}, 'read_only'),
    ({'action': 'status'}, 'read_only'), ({'operation': 'adopted'}, 'mutating'), ({'action': 'adopted'}, 'mutating'),
    ({'action': 'remove'}, 'mutating'), ({'action': 'restore'}, 'mutating'), ({'action': 'ineffective'}, 'mutating')])
def test_feedback_status_schema_and_action_effects(params, effect):
    from agent_py_agent.agent.capability.skill_feedback_tool import SkillFeedbackTool
    from agent_py_agent.agent.tooling.models import (
        IdempotencyPolicy,
        tool_effect_for_runtime_policy,
    )

    assert SkillFeedbackTool.model_spec.input_schema['required'] == []
    assert set(SkillFeedbackTool.model_spec.input_schema['properties']) == {'action', 'skill_id'}
    assert set(SkillFeedbackTool.model_spec.input_schema['properties']['action']['enum']) == {
        'status', 'adopted', 'remove', 'restore', 'ineffective',
    }
    original = dict(params)
    name, canonical = decode_tool_input('skill_feedback', params, version=1)
    assert name == 'skill_feedback' and params == original and 'operation' not in canonical
    assert tool_effect_for_runtime_policy(SkillFeedbackTool.runtime_policy, canonical) == effect
    assert SkillFeedbackTool.runtime_policy.idempotency_policy == IdempotencyPolicy('operation')


# LLM: 查询不要求自学习开关或任务快照授权；比较实际文件字节确保不确认、消费或发布候选。
# 函数用途: 检查默认/规范及显式v1归一后的全景状态、计数口径并确认不再暴露已下线的pending_learning_proposals。
@pytest.mark.parametrize('params', [{}, {'operation': 'status'}, {'action': 'status'}])
@pytest.mark.parametrize('enabled', [False, True])
def test_feedback_status_panorama_is_readonly(tmp_path, params, enabled):
    ctx, tool, _proposal_id = _feedback_status_case(tmp_path)
    tool.agent.config.enable_self_learning = enabled
    before = {str(p): p.read_bytes() for p in ctx.home.owner_home_dir.rglob('*') if p.is_file()}
    _name, canonical = decode_tool_input('skill_feedback', params, version=1)
    outcome = tool.execute(canonical)
    assert outcome.ok, outcome.output
    result = json.loads(outcome.output)
    assert result['self_learning_enabled'] is enabled and result['learned_skills_count'] == 1
    assert result['learned_skills'][0]['name'] == NAME
    assert isinstance(result.get('pending_requests_count'), int) and result['pending_requests_count'] >= 1
    assert 'pending_learning_proposals' not in result
    registry = ctx.store.load_registry()
    assert result['daily_summarization_calls'] == {'owner_date': registry.daily_date, 'owner_calls': registry.daily_calls,
        'owner_daily_limit': 5, 'note': '本身份后台自动总结 LLM 调用次数（非 skill_search 工具调用次数）'}
    assert {str(p): p.read_bytes() for p in ctx.home.owner_home_dir.rglob('*') if p.is_file()} == before
    assert '拟保存内容' not in outcome.output and str(ctx.home.owner_home_dir) not in outcome.output


# LLM: 状态按registry精确查找，不把任务采用权限用于只读报告；V2-4已移除旧待确认提案状态查询。
# 函数用途: 查询已发布名字或owner编号，并确认旧提案查询不再作为已发布技能返回。
@pytest.mark.parametrize('reference', [NAME, f'owner:{NAME}'])
def test_feedback_status_resolves_registered_or_pending_skill(tmp_path, reference):
    ctx, tool, proposal_id = _feedback_status_case(tmp_path)
    outcome = tool.execute({'action': 'status', 'skill_id': reference})
    assert outcome.ok, outcome.output
    result = json.loads(outcome.output)
    assert result['skill']['sha256'] == ctx.store.load_registry().skills[NAME].sha256
    assert result['maintenance'] == ctx.store.load_registry().skills[NAME].maintenance
    assert not tool.execute({'action': 'status', 'skill_id': 'pending-method'}).ok
    assert not tool.execute({'action': 'status', 'skill_id': proposal_id}).ok


# LLM: 内置/工作区/共享快照只返回公开身份；不得误登记为自学方法或要求采用权限。
# 函数用途: 验证非自学习来源状态可读而adopted仍拒绝。
@pytest.mark.parametrize('source', ['builtin', 'workspace', 'shared'])
def test_feedback_status_accepts_other_snapshot_sources_without_adoption(tmp_path, source):
    _ctx, tool, _proposal_id = _feedback_status_case(tmp_path)
    entry = SimpleNamespace(stable_id=f'{source}:method', name='method', source=source)
    tool.agent._current_skill_snapshot = SimpleNamespace(resolve=lambda _ref: entry)
    result = tool.execute({'action': 'status', 'skill_id': entry.stable_id})
    assert result.ok, result.output
    assert json.loads(result.output) == {'ok': True, 'skill_id': entry.stable_id, 'name': 'method', 'source': source,
        'status': 'active_builtin_or_shared', 'note': '该技能为内置或共享技能，非本身份自学习生成'}
    tool.agent.config.enable_self_learning = True
    assert not tool.execute({'action': 'adopted', 'skill_id': entry.stable_id}).ok


# LLM: 规范handler只接受action，旧字段不能成为隐式别名；缺字段与非法动作都须在写入前拒绝。
# 函数用途: 验证当前参数名的可理解错误，不把未知操作退回status或采用。
@pytest.mark.parametrize('params,message', [({'action': 'adopted'}, 'action=adopted 时必须提供 skill_id'),
    ({'operation': 'status', 'action': 'adopted'}, '仅支持action、skill_id'), ({'action': 'effective'}, 'status / adopted')])
def test_feedback_invalid_actions_are_explicit(tmp_path, params, message):
    ctx, tool, _proposal_id = _feedback_status_case(tmp_path)
    before = {p: p.read_bytes() for p in ctx.home.owner_home_dir.rglob('*') if p.is_file()}
    result = tool.execute(params)
    assert not result.ok and message in result.output
    assert {p: p.read_bytes() for p in ctx.home.owner_home_dir.rglob('*') if p.is_file()} == before


# LLM: 即便两动作值相同，规范handler也拒绝旧字段；提前校验不得取得写claim或触碰业务存储。
# 函数用途: 钉住旧参数只可经宿主v1边界迁移，不能在业务层隐式兼容。
@pytest.mark.parametrize('params', [
    {'operation': 'status'}, {'operation': 'adopted', 'skill_id': f'owner:{NAME}'},
    {'operation': 'status', 'action': 'status'},
])
def test_feedback_canonical_handler_rejects_legacy_fields_before_storage(params):
    from agent_py_agent.agent.capability.skill_feedback_tool import SkillFeedbackTool

    tool = SkillFeedbackTool(SimpleNamespace())
    validation = tool.validate_invocation(params, None)
    assert not validation.ok and validation.effect_outcome == 'not_started'
    result = tool.execute(params)
    assert not result.ok and result.error_code == 'SKILL_FEEDBACK_UNAVAILABLE'
    assert '仅支持action、skill_id' in result.output


# LLM: 可选c6读口不改变状态报告或发布权；统计失败不把成功采用误报失败。
# 函数用途: 验证状态附带usage_summary兼容口。
def test_feedback_status_includes_optional_usage_summary(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
    _ctx, tool, _proposal_id = _feedback_status_case(tmp_path)
    monkeypatch.setattr(SkillLearningStore, 'usage_summary', lambda _store: {'totals': {'effective_uses': 2}}, raising=False)
    result = tool.execute({'action': 'status'})
    assert result.ok and json.loads(result.output)['skill_usage']['totals']['effective_uses'] == 2


# LLM: 旧采用由可信v1上下文在effect/claim前解码；规范v2与旧v1都沿真实background claim写同一账。
# 函数用途: 验证两协议采用的模型authority、可选使用记账和失效claim拒绝。
@pytest.mark.parametrize('input_version', [1, 2])
def test_feedback_adopted_alias_preserves_claim_and_optional_usage(background, monkeypatch, input_version):
    from agent_py_agent.agent.capability import skill_feedback_tool
    from agent_py_agent.agent.capability.skill_learning_prompt import parse_skill_learning_decision
    from agent_py_agent.agent.capability.skill_learning_publish import (
        PublishRequest,
        publish_learned_skill,
    )
    from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
    agent = background.agent
    agent.config.enable_self_learning = True
    agent._current_run_params = SimpleNamespace(run_id=background.task_id, task_id=background.task_id, task_attributes={})
    store = SkillLearningStore.for_home(agent.home_paths)
    publish_learned_skill(store, PublishRequest(parse_skill_learning_decision(_output(), frozenset())))
    snapshot = agent.skills_service.snapshot_for(force_reload=True)
    monkeypatch.setattr(skill_feedback_tool, '_snapshot_for', lambda _agent: snapshot)
    usages = []
    monkeypatch.setattr(SkillLearningStore, 'record_skill_usage', lambda _store, **kwargs: usages.append(kwargs), raising=False)
    tool = skill_feedback_tool.SkillFeedbackTool(agent)
    from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call

    key = 'operation' if input_version == 1 else 'action'
    result = execute_registry_test_call(agent.tools, 'skill_feedback', {key: 'adopted', 'skill_id': f'owner:{NAME}'},
        run_id=background.task_id, register_with=agent, trusted_run_context={'tool_input_version': input_version},
        call_id='feedback-adopted')
    assert result.ok, result.output
    if input_version == 1:
        assert result.metadata['input_migration']['action'] == 'adopted'
    assert len(usages) == 1 and usages[0]['event'] == 'adopted' and usages[0]['skill_id'] == f'owner:{NAME}'
    assert store.load_registry().skills[NAME].maintenance['observations'][0]['authority'] == 'model'
    agent._current_run_params = SimpleNamespace(run_id='stale', task_id='stale', task_attributes={})
    assert not tool.execute({'action': 'adopted', 'skill_id': f'owner:{NAME}'}).ok and len(usages) == 1


# LLM: 模拟c6新增refill参数的真实接口形状；只读查询不能触发队列恢复写入，旧无参数接口仍须兼容。
# 函数用途: 验证可选队列API不会把status变成写入。
def test_feedback_status_never_refills_optional_overflow_queue(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
    ctx, tool, _proposal_id = _feedback_status_case(tmp_path)
    calls = []
    # LLM: 参数值为唯一观测点，不真的移动请求，保持该测试可独立于c6合入顺序运行。
    # 函数用途: 暴露默认会回填的新式队列接口供状态查询测试。
    def pending(_store, *, refill=True):
        calls.append(refill)
        return []
    monkeypatch.setattr(SkillLearningStore, 'pending_requests', pending)
    before = ctx.store.registry_path.read_bytes()
    outcome = tool.execute({'action': 'status'})
    assert outcome.ok, outcome.output
    assert calls == [False] and json.loads(outcome.output)['pending_requests_count'] == 0
    assert ctx.store.registry_path.read_bytes() == before


# LLM: c6公开模块接口也可提供摘要，不要求在Store上另造方法或更改权威存储。
# 函数用途: 验证状态查询读取模块级可选usage接口。
def test_feedback_status_reads_optional_public_usage_ledger(tmp_path, monkeypatch):
    import sys
    _ctx, tool, _proposal_id = _feedback_status_case(tmp_path)
    module = SimpleNamespace(load_skill_usage_summary=lambda _store: {'totals': {'adopted': 3}})
    monkeypatch.setitem(sys.modules, 'agent_py_agent.agent.capability.skill_usage_ledger', module)
    result = tool.execute({'action': 'status'})
    assert result.ok and json.loads(result.output)['skill_usage']['totals']['adopted'] == 3


# LLM: c6模块采用记账必须在原claim通过后恰调用一次；台账异常只能软失败，不改变原采用回执。
# 函数用途: 验证模块级adopted接口和统计故障隔离。
def test_feedback_adopted_records_public_usage_hook_after_claim(background, monkeypatch):
    import sys

    from agent_py_agent.agent.capability import skill_feedback_tool
    from agent_py_agent.agent.capability.skill_learning_prompt import parse_skill_learning_decision
    from agent_py_agent.agent.capability.skill_learning_publish import (
        PublishRequest,
        publish_learned_skill,
    )
    from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
    agent = background.agent
    agent.config.enable_self_learning = True
    agent._current_run_params = SimpleNamespace(run_id=background.task_id, task_id=background.task_id, task_attributes={})
    store = SkillLearningStore.for_home(agent.home_paths)
    publish_learned_skill(store, PublishRequest(parse_skill_learning_decision(_output(), frozenset())))
    snapshot = agent.skills_service.snapshot_for(force_reload=True)
    monkeypatch.setattr(skill_feedback_tool, '_snapshot_for', lambda _agent: snapshot)
    calls = []
    # LLM: 仅捕获本次合法采用事件，随后注入辅助失败；不得覆盖原采用结果。
    # 函数用途: 验证公开使用台账调用形状与异常隔离。
    def record(_store, **kwargs):
        calls.append(kwargs)
        raise OSError('usage unavailable')
    monkeypatch.setitem(sys.modules, 'agent_py_agent.agent.capability.skill_usage_ledger', SimpleNamespace(record_skill_usage_event=record))
    result = skill_feedback_tool.SkillFeedbackTool(agent).execute({'action': 'adopted', 'skill_id': f'owner:{NAME}'})
    assert result.ok, result.output
    assert len(calls) == 1 and calls[0]['event'] == 'adopted' and calls[0]['skill_id'] == f'owner:{NAME}'
    assert store.load_registry().skills[NAME].maintenance['observations'][0]['authority'] == 'model'


# LLM: 使用真实工具执行器和原注册清单，验证修复覆盖schema、调用前检查与handler，不只直接调execute。
# 函数用途: 验证默认/规范状态与宿主v1旧状态经正式工具链执行，采用缺编号返回清晰错误。
@pytest.mark.parametrize('params', [{}, {'operation': 'status'}, {'action': 'status'}])
def test_feedback_status_through_canonical_executor(background, params):
    from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
    agent = background.agent
    input_version = 1 if 'operation' in params else 2
    result = execute_registry_test_call(agent.tools, 'skill_feedback', params, run_id=background.task_id,
        register_with=agent, call_id='feedback-status', trusted_run_context={'tool_input_version': input_version})
    assert result.ok, result.output
    if input_version == 1:
        assert result.metadata['input_migration']['action'] == 'status'
    assert 'learned_skills_count' in result.output and 'pending_requests_count' in result.output
    missing = execute_registry_test_call(agent.tools, 'skill_feedback', {'action': 'adopted'}, run_id=background.task_id,
                                         register_with=agent, call_id='feedback-missing-id')
    assert not missing.ok and 'action=adopted 时必须提供 skill_id' in missing.output


# LLM: 仅固定配额模块的单调时钟，真实枚举、state锁、准入与双文件写入不变；成功计数在保存返回后累加。
# 函数用途: 确定性验证一批搜索命中只做一次准入事务，并核对实际落盘与连续身份记录。
def test_c4r_search_matches_use_one_joint_quota_scan_and_state_lock(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from agent_py_agent.agent.capability import skill_usage_ledger as usage
    from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
    from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
    from agent_py_agent.agent.user_space import owner_quota
    from agent_py_agent.tests.test_conversation_method_carry import method_fixture

    fixture = method_fixture(tmp_path, count=3)
    store = SkillLearningStore.for_home(fixture.agent.home_paths)
    (store.skills_root.parent / 'quota.json').write_text('{"max_disk_mb": 1024}')
    monkeypatch.setattr(owner_quota, 'time', SimpleNamespace(**{**vars(time), 'monotonic': lambda: 100.0}))
    # 安装夹具已有超过128项；先由原后台对账建立完整基线，不能把未对账增长拒绝当使用账写入失败。
    store.directory.mkdir(parents=True, exist_ok=True)
    for _ in range(128):
        baseline = owner_quota.reconcile_owner_quota_slice(store.skills_root.parent, max_entries=128, max_ms=2500)
        if baseline["complete"]:
            break
    else:
        pytest.fail("使用账夹具的原预算配额对账未完成")
    observed = {'locks': 0, 'scans': 0, 'saves': 0}
    state_lock, save = SkillLearningStore.state_lock, usage._save_usage
    snapshot = owner_quota._bounded_quota_snapshot
    snapshots = []

    # LLM: 保留原owner配额→state锁顺序及真实admission，退出也走原上下文。
    # 函数用途: 统计这次搜索实际进入使用事务的次数。
    @contextmanager
    def counted_lock(self):
        observed['locks'] += 1
        with state_lock(self) as admission:
            yield admission

    # LLM: 包装原鲜活核对而不模拟扫描结果；128项/2500ms预算和未完成拒绝仍由产品判定。
    # 函数用途: 观察实际配额准入的有界扫描及完成状态。
    def counted_scan(root, *, max_entries, max_ms):
        observed['scans'] += 1
        result = snapshot(root, max_entries=max_entries, max_ms=max_ms)
        snapshots.append((root, max_entries, max_ms, result))
        return result

    # LLM: 双文件写入继续使用原函数；异常不得被记为已完成保存，原返回值和异常保持。
    # 函数用途: 只统计实际成功返回的批处理保存。
    def counted_save(*args, **kwargs):
        result = save(*args, **kwargs)
        observed['saves'] += 1
        return result

    monkeypatch.setattr(SkillLearningStore, 'state_lock', counted_lock)
    monkeypatch.setattr(owner_quota, '_bounded_quota_snapshot', counted_scan)
    monkeypatch.setattr(usage, '_save_usage', counted_save)
    result = SkillSearchTool(fixture.agent).execute({'action': 'search', 'query': 'entry', 'limit': 20})
    assert result.ok, result.output
    matches = json.loads(result.output)['matches']
    assert len(matches) >= 3
    # 完整基线已证明大于128项，原准入只核受影响目录，不再重做必然读不完的全新扫描。
    assert observed == {'locks': 1, 'scans': 0, 'saves': 1}
    assert snapshots == []
    summary = usage.load_skill_usage_summary(store)
    assert summary['totals']['visible'] == len(matches)
    assert summary['totals']['manual_search'] == len(matches)
    assert summary['totals']['effective_uses'] == 0
    saved_summary = json.loads((store.directory / 'usage_summary.json').read_text())
    assert saved_summary['totals'] == summary['totals']
    expected_events = 2 * len(matches)
    assert saved_summary['last_sequence'] == expected_events
    rows = [json.loads(line) for line in (store.directory / 'usage_ledger.jsonl').read_text().splitlines()]
    assert [row['sequence'] for row in rows] == list(range(1, expected_events + 1))
    assert [row['event'] for row in rows] == ['visible', 'manual_search'] * len(matches)
    assert [row['skill_id'] for row in rows] == [match['stable_id'] for match in matches for _ in range(2)]
    assert all(row['task_id'] == fixture.params.task_id for row in rows)
    assert all(row['request_id'] == fixture.params.request_id for row in rows)
    assert all(row['thread_id'] == fixture.link.thread_id for row in rows)
    assert all(row['producer'] == 'skill_search.search' for row in rows)


# LLM: 真实枚举首项后推进2501ms，耗尽128项/2500ms准入预算；辅助记账故障不改变搜索结果。
# 函数用途: 验证预算耗尽时返回完整搜索命中并告警，且不留下半批usage记录或汇总。
def test_c4r_search_matches_quota_scan_budget_exhaustion_skips_usage_write(tmp_path, monkeypatch, caplog):
    import logging

    from agent_py_agent.agent.capability import skill_usage_ledger as usage
    from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
    from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
    from agent_py_agent.agent.user_space import owner_quota
    from agent_py_agent.tests.test_conversation_method_carry import method_fixture

    fixture = method_fixture(tmp_path, count=3)
    store = SkillLearningStore.for_home(fixture.agent.home_paths)
    (store.skills_root.parent / 'quota.json').write_text('{"max_disk_mb": 1024}')
    paths = [store.directory / name for name in ('usage_ledger.jsonl', 'usage_summary.json')]
    before = {path: path.read_bytes() if path.exists() else None for path in paths}
    clock = [100.0]
    entries, snapshot, save = owner_quota._quota_entries, owner_quota._bounded_quota_snapshot, usage._save_usage
    pulled, closed, snapshots, saves = [], [], [], []

    # LLM: 每个目录项仍来自原nofollow迭代器，只在真实首项已读出后推进测试时钟；关闭行为也继续原实现。
    # 函数用途: 精确模拟首项I/O耗尽2500ms预算而不伪造扫描结果。
    def delayed_entries(root, parts=()):
        iterator = entries(root, parts)
        try:
            for row in iterator:
                pulled.append(row)
                clock[0] = 102.501
                yield row
        finally:
            iterator.close()
            closed.append(True)

    # LLM: 保留真实扫描和预算判定，仅记录调用参数及完成标志。
    # 函数用途: 确认确实命中原有界扫描的未完成分支。
    def counted_scan(root, *, max_entries, max_ms):
        result = snapshot(root, max_entries=max_entries, max_ms=max_ms)
        snapshots.append((root, max_entries, max_ms, result[2]))
        return result

    # LLM: 保存必须调用原函数，只有成功返回才标记完成；准入异常继续传播给原软失败边界。
    # 函数用途: 区分保存尝试与真正提交。
    def counted_save(*args):
        saves.append('attempted')
        result = save(*args)
        saves.append('completed')
        return result

    monkeypatch.setattr(owner_quota, 'time', SimpleNamespace(**{**vars(time), 'monotonic': lambda: clock[0]}))
    monkeypatch.setattr(owner_quota, '_quota_entries', delayed_entries)
    monkeypatch.setattr(owner_quota, '_bounded_quota_snapshot', counted_scan)
    monkeypatch.setattr(usage, '_save_usage', counted_save)
    with caplog.at_level(logging.WARNING, logger=usage.__name__):
        result = SkillSearchTool(fixture.agent).execute({'action': 'search', 'query': 'entry', 'limit': 20})
    assert result.ok, result.output
    matches = json.loads(result.output)['matches']
    assert len(matches) >= 3
    assert {f'entry-{index}' for index in range(3)} <= {match.get('package_id') for match in matches}
    assert snapshots == [(fixture.agent.home_paths.owner_home_dir, 128, 2500.0, False)]
    assert len(pulled) == 1 and closed == [True]
    assert (usage.__name__, logging.WARNING, 'SKILL_USAGE_RECORD_UNAVAILABLE') in caplog.record_tuples
    assert saves == ['attempted']
    assert {path: path.read_bytes() if path.exists() else None for path in paths} == before


# LLM: 固定配额时钟隔离128项预算；搜索、准入、分片对账与连续双文件写入均走真实链路。
# 函数用途: 验证条目预算耗尽只告警不落半账，对账完成后两批搜索可连续记账且配额保持干净。
def test_c4r_search_matches_quota_entry_budget_exhaustion_and_reconciled_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    from agent_py_agent.agent.capability import skill_usage_ledger as usage
    from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
    from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
    from agent_py_agent.agent.user_space import owner_quota
    from agent_py_agent.tests.test_conversation_method_carry import method_fixture

    fixture = method_fixture(tmp_path, count=3)
    store = SkillLearningStore.for_home(fixture.agent.home_paths)
    (store.skills_root.parent / 'quota.json').write_text('{"max_disk_mb": 1024}', encoding='utf-8')
    monkeypatch.setattr(owner_quota, 'time', SimpleNamespace(**{
        **owner_quota.time.__dict__, 'monotonic': lambda: 100.0,
    }))
    owner_home = fixture.agent.home_paths.owner_home_dir
    pad = owner_home / 'entry_budget_pad'
    pad.mkdir()
    for number in range(200):
        (pad / f'{number:04d}').write_bytes(b'x')
    ledger_path = store.directory / 'usage_ledger.jsonl'
    summary_path = store.directory / 'usage_summary.json'
    quota_path = owner_home / '.owner-quota-ledger.json'
    snapshot = owner_quota._bounded_quota_snapshot
    snapshots = []

    # LLM: 不替换枚举或扫描结果，仅检查128项/2500ms参数并记录真实返回值。
    # 函数用途: 证明本次拒绝来自未完成的有界配额扫描。
    def counted_scan(root, *, max_entries, max_ms):
        assert root == owner_home and (max_entries, max_ms) == (128, 2500.0)
        result = snapshot(root, max_entries=max_entries, max_ms=max_ms)
        snapshots.append(result)
        return result

    monkeypatch.setattr(owner_quota, '_bounded_quota_snapshot', counted_scan)
    tool = SkillSearchTool(fixture.agent)
    warning = (usage.__name__, logging.WARNING, 'SKILL_USAGE_RECORD_UNAVAILABLE')
    with caplog.at_level(logging.WARNING, logger=usage.__name__):
        result = tool.execute({'action': 'search', 'query': 'entry', 'limit': 20})
    assert result.ok, result.output
    assert [row['package_id'] for row in json.loads(result.output)['matches']] == ['entry-0', 'entry-1', 'entry-2']
    assert len(snapshots) == 1
    _total, count, complete = snapshots[0]
    assert complete is False and count <= 128
    assert warning in caplog.record_tuples
    assert not ledger_path.exists() and not summary_path.exists()
    assert json.loads(quota_path.read_text(encoding='utf-8'))['dirty_since'] is not None

    # 真实对账必须推进到完成；设上限让恢复停滞直接失败，而不是挂住测试。
    for _ in range(20):
        report = owner_quota.reconcile_owner_quota_slice(owner_home, max_entries=128, max_ms=1000)
        if report['complete'] is True:
            break
    else:
        pytest.fail('owner quota reconciliation did not complete within 20 slices')
    assert json.loads(quota_path.read_text(encoding='utf-8'))['dirty_since'] is None

    caplog.clear()
    expected_ids = ['capability:entry-0', 'capability:entry-0', 'capability:entry-1',
                    'capability:entry-1', 'capability:entry-2', 'capability:entry-2']
    with caplog.at_level(logging.WARNING, logger=usage.__name__):
        for _ in range(2):
            result = tool.execute({'action': 'search', 'query': 'entry', 'limit': 20})
            assert result.ok, result.output
            assert [row['package_id'] for row in json.loads(result.output)['matches']] == ['entry-0', 'entry-1', 'entry-2']
            assert warning not in caplog.record_tuples
            assert json.loads(quota_path.read_text(encoding='utf-8'))['dirty_since'] is None
    rows = [json.loads(line) for line in ledger_path.read_text(encoding='utf-8').splitlines()]
    assert len(rows) == 6
    assert [row['sequence'] for row in rows] == list(range(1, 7))
    assert [row['skill_id'] for row in rows] == expected_ids
    assert [row['event'] for row in rows] == ['visible', 'manual_search'] * 3
    summary = json.loads(summary_path.read_text(encoding='utf-8'))
    assert summary['last_sequence'] == 6
    assert summary['totals']['visible'] == summary['totals']['manual_search'] == 3

    # 切换新任务标识发起第二批搜索，确认对账后仍可持续追加记账且配额保持干净。
    fixture.params.task_id = f'{fixture.params.task_id}-batch2'
    with caplog.at_level(logging.WARNING, logger=usage.__name__):
        result = tool.execute({'action': 'search', 'query': 'entry', 'limit': 20})
        assert result.ok, result.output
        assert [row['package_id'] for row in json.loads(result.output)['matches']] == ['entry-0', 'entry-1', 'entry-2']
        assert warning not in caplog.record_tuples
        assert json.loads(quota_path.read_text(encoding='utf-8'))['dirty_since'] is None
    rows = [json.loads(line) for line in ledger_path.read_text(encoding='utf-8').splitlines()]
    assert len(rows) == 12
    assert [row['sequence'] for row in rows] == list(range(1, 13))
    assert [row['skill_id'] for row in rows] == expected_ids * 2
    assert [row['event'] for row in rows] == ['visible', 'manual_search'] * 6
    summary = json.loads(summary_path.read_text(encoding='utf-8'))
    assert summary['last_sequence'] == 12
    assert summary['totals']['visible'] == summary['totals']['manual_search'] == 6


# LLM: 公开批量接口须保留原四类事件和序号口径；空批次不能创建目录，坏批次不能写部分记录。
# 函数用途: 验证批量API、空输入与整批软失败边界。
def test_c4r_batch_usage_api_is_atomic_for_invalid_input_and_empty_is_readonly(tmp_path):
    from agent_py_agent.agent.capability import skill_usage_ledger as usage

    store = _runtime(tmp_path).store
    record_batch = getattr(usage, 'record_skill_usage_events', None)
    assert callable(record_batch)
    before = {str(p): p.read_bytes() for p in store.directory.rglob('*') if p.is_file()}
    record_batch(store, [])
    assert {str(p): p.read_bytes() for p in store.directory.rglob('*') if p.is_file()} == before
    good = {'event': 'manual_search', 'skill_id': 'builtin:batch', 'source': 'builtin'}
    record_batch(store, [good, dict(good, event='invalid')])
    assert not (store.directory / 'usage_ledger.jsonl').exists()
    record_batch(store, [good, dict(good, event='manual_get', chars=4)])
    summary = usage.load_skill_usage_summary(store)
    assert summary['totals']['manual_search'] == summary['totals']['manual_get'] == 1
    assert summary['last_sequence'] == 2


# LLM: 包入口和RuntimeFacts都走真实路径；观察线程update_atomic完整调用范围，而不是只观察回调内存。
# 函数用途: 验证carry写使用台账时线程JSON锁已经退出。
def test_c4r_carry_usage_runs_after_thread_atomic_update(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import method_carry
    from agent_py_agent.agent.capability.skill_usage_ledger import load_skill_usage_summary
    from agent_py_agent.tests.test_conversation_method_carry import (
        method_fixture,
        read_package,
        records,
    )
    from agent_py_agent.tests.test_conversation_method_restore import carry, compact

    fixture = method_fixture(tmp_path)
    assert read_package(fixture).ok
    compact(fixture)
    threads = fixture.agent.conversation_store.threads
    update_atomic, record_usage = threads.update_atomic, method_carry._record_carried_usage
    active, observations = [], []

    # LLM: 在整个真实线程事务期间设置标记，finally确保异常路径也退出观测范围。
    # 函数用途: 暴露调用者是否仍在线程锁范围内。
    def observed_update(*args, **kwargs):
        active.append(True)
        try:
            return update_atomic(*args, **kwargs)
        finally:
            active.pop()

    # LLM: 记录锁状态后继续真实使用台账写入，不能靠直接跳过记账让测试变绿。
    # 函数用途: 核对持久带回代次先提交，随后才写使用事实。
    def observed_usage(request, selected):
        observations.append((bool(active), records(fixture)[0]['carried_generation']))
        return record_usage(request, selected)

    monkeypatch.setattr(threads, 'update_atomic', observed_update)
    monkeypatch.setattr(method_carry, '_record_carried_usage', observed_usage)
    assert len(carry(fixture)) == 1
    assert observations == [(False, 1)]
    assert load_skill_usage_summary(fixture.agent.home_paths)['totals']['host_injected'] == 1


# LLM: 线程提交抛错不能把仅在回调中完成的内存投递算为持久成功，不触碰生产线程文件。
# 函数用途: 验证线程保存失败时使用记账仍为零。
def test_c4r_failed_thread_commit_does_not_record_carried_usage(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability.skill_usage_ledger import load_skill_usage_summary
    from agent_py_agent.tests.test_conversation_method_carry import (
        method_fixture,
        read_package,
        records,
    )
    from agent_py_agent.tests.test_conversation_method_restore import carry, compact

    fixture = method_fixture(tmp_path)
    assert read_package(fixture).ok
    compact(fixture)
    threads = fixture.agent.conversation_store.threads

    # LLM: 执行真实carry变换后模拟线程发表失败，不保存返回的新thread。
    # 函数用途: 命中回调成功但update_atomic整体未成功的边界。
    def failed_update(thread_id, update):
        update(threads.require(thread_id))
        raise OSError('injected thread commit failure')

    monkeypatch.setattr(threads, 'update_atomic', failed_update)
    carry(fixture)
    assert records(fixture)[0]['carried_generation'] == 0
    assert load_skill_usage_summary(fixture.agent.home_paths)['totals']['host_injected'] == 0


# LLM: 使用真实TaskStore claim与选包结果形状，模型响应不联网，不跳过原入口reader或pin。
# 函数用途: 为提交成功/拒绝测试准备一个已领取的合法包选择。
def _c4r_selection(fixture):
    tasks = fixture.agent.conversation_store.tasks
    claim = tasks.claim_capability_selection(
        task_id=fixture.link.task_id, thread_id=fixture.link.thread_id,
        request_id=fixture.params.request_id, run_id=fixture.params.run_id, attempt_id=fixture.params.attempt_id,
        candidate_digest='a' * 64, model_binding_digest='b' * 64,
        execution_is_current=fixture.authority.is_current)
    assert claim is not None
    result = SimpleNamespace(outcome='selected', selected_refs=tuple(p.to_ref() for p in fixture.scope.skills.packages),
                             error_code='', warning_codes=(), failure_facts={})
    return claim, result


# LLM: 拒绝finish、finish后取消与IR拒绝都不是实际投递；成功场景仍使用真实CAS和RuntimeFacts。
# 函数用途: 钉住包选择只有两个提交门都通过才恰记一次host_injected。
@pytest.mark.parametrize('outcome', ['rejected', 'cancelled', 'ir_rejected', 'committed'])
def test_c4r_selection_records_usage_only_after_committed_runtime_facts(tmp_path, monkeypatch, outcome):
    from agent_py_agent.agent.capability.package_selection_runtime import _commit_selection
    from agent_py_agent.agent.capability.skill_usage_ledger import load_skill_usage_summary
    from agent_py_agent.agent.common.cancellation import ToolCancelled
    from agent_py_agent.tests.test_capability_package_entry_context import _fixture

    fixture = _fixture(tmp_path, bodies=(b'SELECTION-BODY',))
    claim, result = _c4r_selection(fixture)
    tasks = fixture.agent.conversation_store.tasks
    finish = tasks.finish_capability_selection
    observed = []

    # LLM: 在finish边界读真实使用汇总，捕获在TaskStore提交前提前记账的问题。
    # 函数用途: 包装一次原CAS，必要时注入拒绝或提交后的取消。
    def observed_finish(**kwargs):
        observed.append(load_skill_usage_summary(fixture.agent.home_paths)['totals']['host_injected'])
        if outcome == 'rejected':
            return None
        value = finish(**kwargs)
        if outcome == 'cancelled':
            fixture.params.cancellation_token.cancel('cancel after finish')
        return value

    monkeypatch.setattr(tasks, 'finish_capability_selection', observed_finish)
    if outcome == 'ir_rejected':
        monkeypatch.setattr('agent_py_agent.agent.capability.package_selection_runtime.record_runtime_facts_turn_ir',
                            lambda *_a, **_kw: False)
    if outcome == 'cancelled':
        with pytest.raises(ToolCancelled):
            _commit_selection(fixture.agent, fixture.params, fixture.scope, fixture.authority, claim, result, 10000)
    else:
        _commit_selection(fixture.agent, fixture.params, fixture.scope, fixture.authority, claim, result, 10000)
    assert observed == [0]
    expected = int(outcome == 'committed')
    assert load_skill_usage_summary(fixture.agent.home_paths)['totals']['host_injected'] == expected
    turns = [turn for turn in fixture.params.tool_ir_history if getattr(turn, 'source', '') == 'capability_package_entries']
    assert len(turns) == expected
    if turns:
        assert 'SELECTION-BODY' in turns[0].text


# LLM: 读取、主选包与压缩带回走真实入口，身份规范化只作用于使用统计，不改授权稳定ID。
# 函数用途: 验证同一包的三种使用聚合到一个by_skill键且各事件不丢不重。
def test_c4r_package_read_selection_and_carry_share_canonical_usage_key(tmp_path):
    from agent_py_agent.agent.capability.package_selection_runtime import _commit_selection
    from agent_py_agent.agent.capability.skill_usage_ledger import load_skill_usage_summary
    from agent_py_agent.tests.test_conversation_method_carry import method_fixture, read_package
    from agent_py_agent.tests.test_conversation_method_restore import carry, compact

    fixture = method_fixture(tmp_path)
    assert read_package(fixture).ok
    claim, result = _c4r_selection(fixture)
    _commit_selection(fixture.agent, fixture.params, fixture.scope, fixture.authority, claim, result, 10000)
    compact(fixture)
    assert len(carry(fixture)) == 1
    summary = load_skill_usage_summary(fixture.agent.home_paths)
    assert set(summary['by_skill']) == {'entry-0'}
    counts = summary['by_skill']['entry-0']
    assert counts['manual_get'] == 1 and counts['host_injected'] == 2 and counts['effective_uses'] == 3
    assert summary['totals']['effective_uses'] == 3


# LLM: 构造旧双键摘要与水位后的旧前缀明细，迁移不得重放已汇总事件或修改生命周期/每日计数。
# 函数用途: 验证旧身份合并、最近元数据、只读加载和下一次写入后的持久规范化。
def test_c4r_legacy_usage_keys_merge_before_replay_without_inflating_totals(tmp_path):
    from agent_py_agent.agent.capability import skill_usage_ledger as usage

    store = _runtime(tmp_path).store
    usage.record_skill_usage_event(store, event='manual_get', kind='capability_package', skill_id='entry-0', source='read')
    usage.record_skill_usage_event(store, event='host_injected', kind='capability_package', skill_id='capability:entry-0', source='carry')
    path = store.directory / 'usage_summary.json'
    ledger = store.directory / 'usage_ledger.jsonl'
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    summary = json.loads(path.read_text())
    counts = dict.fromkeys((*usage.USAGE_EVENTS, 'effective_uses'), 0)
    summary['by_skill'] = {
        'entry-0': dict(counts, manual_get=1, effective_uses=1, last_used_at=rows[0]['at'], last_source='read', kind='capability_package'),
        'capability:entry-0': dict(counts, host_injected=1, effective_uses=1, last_used_at=rows[1]['at'], last_source='carry', kind='capability_package')}
    path.write_text(json.dumps(summary))
    pending = dict(rows[-1], skill_id='capability:entry-0', sequence=3)
    ledger.write_text(ledger.read_text() + json.dumps(pending) + '\n')
    before = path.read_bytes(), ledger.read_bytes()
    loaded = usage.load_skill_usage_summary(store)
    assert set(loaded['by_skill']) == {'entry-0'}
    assert loaded['by_skill']['entry-0']['effective_uses'] == 3
    assert loaded['by_skill']['entry-0']['last_source'] == 'carry'
    assert loaded['totals']['effective_uses'] == 3 and loaded['last_sequence'] == 3
    assert (path.read_bytes(), ledger.read_bytes()) == before
    usage.record_skill_usage_event(store, event='manual_search', skill_id='skill:builtin:method', source='builtin')
    usage.record_skill_usage_event(store, event='manual_get', skill_id='builtin:method', source='builtin')
    saved = json.loads(path.read_text())
    assert set(saved['by_skill']) == {'entry-0', 'builtin:method'}
    assert saved['by_skill']['entry-0']['effective_uses'] == 3
    assert saved['by_skill']['builtin:method']['manual_search'] == 1
    assert saved['by_skill']['builtin:method']['manual_get'] == 1
    assert usage.load_skill_usage_summary(store) == saved


def _legacy_auto_lesson(tmp_path):
    from agent_py_agent.agent.capability.skill_learning_store import (
        LearnedSkill,
        SkillLearningStore,
    )
    from agent_py_agent.tests.test_skill_proposals import _proposal_ctx

    ctx = _proposal_ctx(tmp_path)
    with ctx.service._locked():
        committed = ctx.service._commit_locked(
            ctx.service.directory / f'{ctx.proposal.proposal_id}.json', ctx.proposal, 'auto',
        ).proposal
    store = SkillLearningStore.for_home(ctx.home)
    name = committed.target.skill_name
    record = LearnedSkill(
        name, 1, committed.draft.sha256, committed.created_at, committed.updated_at,
        tuple(committed.source.run_ids), layout='owner',
        maintenance={'state': 'trial', 'baseline_at': 1.0, 'ownership': 'owner_layout',
                     'source_proposal_id': committed.proposal_id},
    )
    with store.state_lock():
        registry = store.load_registry()
        store.save_registry(replace(registry, skills={name: record}))
    return ctx, store, record


def test_existing_auto_confirmed_lesson_enters_lifecycle_preserving_path_and_identity(tmp_path):
    from agent_py_agent.agent.capability.skill_learning_migration import adopt_committed_lessons

    ctx, store, original = _legacy_auto_lesson(tmp_path)
    path = store.skill_path(original.name)
    before = path.read_bytes()
    assert adopt_committed_lessons(store, ctx.service) == [original.name]
    row = store.load_registry().skills[original.name]
    assert row.layout == 'owner' and row.sha256 == original.sha256
    assert store.skill_path(row.name) == path and path.read_bytes() == before
    assert row.maintenance['ownership'] == 'machine'
    assert row.maintenance['lifecycle_adoption']['proposal_id'] == ctx.proposal.proposal_id
    assert adopt_committed_lessons(store, ctx.service) == []
    assert row.name in store.managed_records(store.load_registry())
    maintain_learning(store, mode='autonomous', archive_seconds=1, now=time.time(),
                      protection_complete=True, batch_ms=5000)
    archived = store.load_registry().skills[row.name]
    assert archived.maintenance['state'] == 'archived'
    assert not path.exists() and store.canonical_skill_path(row.name, record=archived).read_bytes() == before
    control_learning(store, row.name, operation='restore')
    assert path.read_bytes() == before
    assert store.load_registry().skills[row.name].layout == 'owner'


@pytest.mark.parametrize('mutate', ['body', 'extra_file', 'manual', 'source_hash', 'registered_source'])
def test_legacy_adoption_never_takes_over_user_or_unverified_files(tmp_path, mutate):
    import json

    from agent_py_agent.agent.capability.skill_learning_migration import adopt_committed_lessons

    ctx, store, original = _legacy_auto_lesson(tmp_path)
    path = store.skill_path(original.name)
    proposal_path = ctx.service.directory / f'{ctx.proposal.proposal_id}.json'
    payload = json.loads(proposal_path.read_text())
    if mutate == 'body':
        path.write_text(path.read_text() + '\n用户补充\n')
    elif mutate == 'extra_file':
        (path.parent / 'notes.txt').write_text('用户附件')
    elif mutate == 'manual':
        payload['receipt']['confirmed_by'] = 'user'
    elif mutate == 'source_hash':
        payload['source']['content_hash'] = '0' * 64
    else:
        with store.state_lock():
            registry = store.load_registry()
            changed = replace(original, source_run_ids=('unrelated-run',))
            store.save_registry(replace(registry, skills={original.name: changed}))
    proposal_path.write_text(json.dumps(payload))
    before = path.read_bytes()
    if mutate == 'source_hash':
        from agent_py_agent.agent.capability.skill_learning import _adopt_committed_lessons_isolated
        _adopt_committed_lessons_isolated(store, ctx.service)
    else:
        assert adopt_committed_lessons(store, ctx.service) == []
    assert path.read_bytes() == before
    assert store.load_registry().skills[original.name].maintenance['ownership'] == 'owner_layout'


def test_cap_progresses_past_unknown_skill_and_keeps_archive_recoverable(tmp_path):
    from agent_py_agent.agent.capability.skill_usage_ledger import load_usage_evidence

    ctx, store, original = _legacy_auto_lesson(tmp_path)
    from agent_py_agent.agent.capability.skill_learning_migration import adopt_committed_lessons
    adopt_committed_lessons(store, ctx.service)
    # 同一机器来源的其它记录仅用于模拟混合所有权库，未知项不能按从未使用淘汰。
    with store.state_lock():
        registry = store.load_registry()
        row = registry.skills[original.name]
        for name in ('a-unknown', 'b-safe', 'c-safe'):
            record = replace(row, name=name, layout='learned')
            target = store.learned_root / name / 'SKILL.md'
            target.parent.mkdir(parents=True)
            target.write_bytes(store.skill_path(row.name).read_bytes())
            registry = replace(registry, skills={**registry.skills, name: record})
        store.save_registry(registry)
    unknown = store.learned_root / 'a-unknown' / 'SKILL.md'
    unknown.unlink()
    unknown.symlink_to(tmp_path / 'outside')
    result = maintain_learning(store, mode='autonomous', max_skills=1,
                               protection_complete=True, batch_ms=5000)
    registry = store.load_registry()
    assert registry.skills['a-unknown'].maintenance['ownership'] == 'unknown'
    assert registry.skills['a-unknown'].maintenance.get('ever_used') is False
    assert len([r for r in registry.skills.values()
                if r.maintenance.get('ownership') == 'machine' and r.maintenance['state'] != 'archived']) == 0
    assert any(r.maintenance['state'] == 'archived' for r in registry.skills.values())
    assert result['coverage']['usage_sufficient'] is True
    assert load_usage_evidence(store)['coverage']['lifetime_summary'] == 'complete'


def test_registered_legacy_receipt_survives_source_retention_and_restarts_usage_scan(tmp_path):
    from agent_py_agent.agent.capability.skill_learning_migration import adopt_committed_lessons

    ctx, store, original = _legacy_auto_lesson(tmp_path)
    # 指向明确不存在的来源库，模拟历史候选被保留策略清理，不改原提案来源。
    ctx.service.candidates_path = tmp_path / 'source-retained-missing.jsonl'
    with store.state_lock():
        registry = store.load_registry()
        store.save_registry(replace(registry, maintenance_cursor={
            'phase': 'usage', 'after_name': 'zz-last', 'started_at': time.time(),
        }))
    assert adopt_committed_lessons(store, ctx.service) == [original.name]
    registry = store.load_registry()
    assert registry.maintenance_cursor == {}
    assert registry.skills[original.name].maintenance['lifecycle_adoption']['source_verification'] == 'committed_receipt_source_missing'


def test_unregistered_missing_source_does_not_gain_machine_ownership_from_receipt(tmp_path):
    from agent_py_agent.agent.capability.skill_learning_migration import adopt_committed_lessons

    ctx, store, original = _legacy_auto_lesson(tmp_path)
    ctx.service.candidates_path = tmp_path / 'source-retained-missing.jsonl'
    with store.state_lock():
        registry = store.load_registry()
        store.save_registry(replace(registry, skills={}))
    before = store.skill_path(original.name, record=original).read_bytes()
    assert adopt_committed_lessons(store, ctx.service) == []
    assert store.load_registry().skills == {}
    assert store.skill_path(original.name, record=original).read_bytes() == before


def test_cap_rank_does_not_classify_unknown_lifetime_as_never_used(tmp_path):
    from agent_py_agent.agent.capability.skill_learning_publish import _victim_rank_key

    _, _, row = _legacy_auto_lesson(tmp_path)
    unknown = replace(row, maintenance={'ever_used': None, 'independent_uses_coverage': 'exact'})
    never_used = replace(row, maintenance={'ever_used': False, 'independent_uses_coverage': 'exact'})
    assert _victim_rank_key(row.name, unknown)[0] != 0
    assert _victim_rank_key(row.name, never_used)[0] == 0


def test_legacy_activity_upper_bound_delays_idle_archive_without_inventing_real_use(tmp_path):
    ctx = _learned(tmp_path)
    now = time.time()
    with ctx.store.state_lock():
        registry = ctx.store.load_registry()
        row = registry.skills[NAME]
        row = replace(row, maintenance={**row.maintenance, 'baseline_at': now - 100})
        ctx.store.save_registry(replace(registry, skills={NAME: row}))
    def evidence(_store):
        return {'coverage': {'lifetime_summary': 'complete'}, 'by_skill': {
            NAME: {'ever_used': True, 'last_activity_at_upper_bound': now - 5},
        }}
    maintain_learning(ctx.store, mode='autonomous', archive_seconds=20, now=now,
                      protection_complete=True, usage_loader=evidence, batch_ms=5000)
    maint = ctx.store.load_registry().skills[NAME].maintenance
    assert maint['state'] == 'trial'
    assert float(maint.get('last_used_at') or 0) == 0
    assert maint['legacy_last_activity_upper_bound'] == now - 5


def test_adopted_owner_layout_remains_managed_after_verified_revision(tmp_path):
    from agent_py_agent.agent.capability.skill_learning_migration import adopt_committed_lessons
    from agent_py_agent.agent.capability.skill_learning_prompt import parse_skill_learning_decision
    from agent_py_agent.agent.capability.skill_learning_publish import (
        PublishRequest,
        _update_locked,
    )

    ctx, store, original = _legacy_auto_lesson(tmp_path)
    adopt_committed_lessons(store, ctx.service)
    before = store.load_registry().skills[original.name]
    request = PublishRequest(
        parse_skill_learning_decision(_output(name=original.name), frozenset()),
        expected_revision=before.revision, expected_sha256=before.sha256,
        promotion_authority='autonomous_verified',
    )
    with store.state_lock():
        _update_locked(store, store.load_registry(), request)
    row = store.load_registry().skills[original.name]
    assert row.sha256 != before.sha256 and row.layout == 'owner'
    assert row.maintenance['lifecycle_adoption'] == before.maintenance['lifecycle_adoption']
    assert row.name in store.managed_records(store.load_registry())


def test_legacy_adoption_service_proxy_includes_existing_owner_and_similarity_reads_its_path(tmp_path):
    from agent_py_agent.agent.capability.skill_learning import (
        _adopt_committed_lessons_isolated,
        _similar_managed_skills,
    )
    from agent_py_agent.agent.capability.skill_learning_prompt import parse_skill_learning_decision

    ctx, store, original = _legacy_auto_lesson(tmp_path)
    _adopt_committed_lessons_isolated(store, ctx.service)
    assert store.load_registry().skills[original.name].maintenance['ownership'] == 'machine'
    decision = parse_skill_learning_decision(
        _output(name='same-procedure-new-name', body=ctx.proposal.draft.body), frozenset(),
    )
    matches = _similar_managed_skills(store, decision, threshold_percent=70)
    assert matches == [{
        'name': original.name, 'similarity_percent': 100,
        'description': ctx.proposal.draft.description,
        'when_to_use': ctx.proposal.draft.when_to_use,
    }]


def test_cap_one_entry_budget_isolates_failed_victim_and_continues_other_skills(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability.skill_learning_store import (
        CODE_EVIDENCE_INCOMPLETE,
        SkillLearningStoreError,
    )

    ctx = _learned(tmp_path)
    store = ctx.store
    with store.state_lock():
        registry = store.load_registry()
        source = registry.skills[NAME]
        records = {}
        for name in ('a-failing', 'b-safe', 'c-safe'):
            row = replace(source, name=name, maintenance={**source.maintenance,
                          'ownership': 'machine', 'ever_used': False})
            target = store.learned_root / name / 'SKILL.md'
            target.parent.mkdir(parents=True)
            target.write_bytes(store.skill_path(NAME).read_bytes())
            records[name] = row
        store.save_registry(replace(registry, skills=records, maintenance_cursor={
            'phase': 'lifecycle', 'after_name': 'zz-last', 'started_at': time.time(),
        }))
    archive = store._transition_archive_locked
    attempts = []

    def fail_one(registry, name, **kwargs):
        attempts.append(name)
        if name == 'a-failing':
            raise SkillLearningStoreError(CODE_EVIDENCE_INCOMPLETE, 'synthetic transient read failure')
        return archive(registry, name, **kwargs)

    monkeypatch.setattr(store, '_transition_archive_locked', fail_one)
    for _ in range(20):
        result = maintain_learning(store, mode='autonomous', max_skills=1,
                                   protection_complete=True, batch_count=1, batch_ms=5000)
        if not result['backlog']:
            break
    rows = store.load_registry().skills
    assert attempts.count('a-failing') == 1
    assert rows['a-failing'].maintenance['ownership'] == 'unknown'
    assert rows['b-safe'].maintenance['state'] == rows['c-safe'].maintenance['state'] == 'archived'


def _protection_runtime(tmp_path, *, task_link='missing-link'):
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

    repo = RuntimeRepository(tmp_path / 'runtime.db')
    task = repo.create_task(owner_id='owner', thread_id='missing-thread', conversation_task_id=task_link)
    run = repo.create_task_run(task_id=task['task_id'], status='created', metadata={'allowed_skills': ['owner:held']})
    agent_run = repo.create_agent_run(task_run_id=run['task_run_id'], run_id='protection-run')
    attempt = repo.create_attempt(agent_run['agent_run_id'])
    repo.settle_agent_attempt(agent_run_id=agent_run['agent_run_id'], attempt_id=attempt['attempt_id'])
    with repo.transaction() as conn:
        conn.execute('UPDATE agent_attempts SET metadata_json=? WHERE attempt_id=?', (
            '{"executor":{"schema_version":"attempt-executor.v1","status":"exited","ended_at":1}}',
            attempt['attempt_id'],
        ))
    agent = SimpleNamespace(runtime_db=repo, conversation_store=ConversationStore(tmp_path/'conversations', initialize=False))
    return agent, agent_run, attempt


def _collect_runtime_protection(agent, *, entries=128):
    from agent_py_agent.agent.capability.skill_learning_protection import _collect_unsettled_tasks
    from agent_py_agent.agent.memory_store.retention_scan_budget import RetentionScanBudget

    protected = set()
    complete = _collect_unsettled_tasks(agent, protected, RetentionScanBudget(entries, 8_388_608, time.monotonic()+2))
    scan = getattr(agent, '_skill_task_protection_scan', {})
    if not complete and scan.get('complete') and scan.get('cursor'):
        # 收尾片只登记完整事实，下一片才对当前来源做完整复核。
        complete = _collect_unsettled_tasks(agent, protected, RetentionScanBudget(entries, 8_388_608, time.monotonic()+2))
    return complete, protected


def test_protection_exited_attempt_does_not_require_missing_thread_or_mutate_taskrun(tmp_path):
    agent, row, _ = _protection_runtime(tmp_path)
    assert _collect_runtime_protection(agent) == (True, {'owner:held'})
    run = agent.runtime_db.get_task_run(row['task_run_id'])
    assert run['status'] == 'created' and run['closed_at'] == 0


@pytest.mark.parametrize('state', ['alive', 'unknown', 'replaced'])
def test_protection_missing_projection_rejects_live_or_unknown_or_replaced_runner(tmp_path, monkeypatch, state):
    from agent_py_agent.agent.runtime_db import executor_liveness

    agent, _, attempt = _protection_runtime(tmp_path)
    with agent.runtime_db.transaction() as conn:
        conn.execute('UPDATE agent_attempts SET metadata_json=? WHERE attempt_id=?', (
            '{"runner_pid":123456,"runner_start_time":"old-birth"}', attempt['attempt_id'],
        ))
    monkeypatch.setattr(executor_liveness, '_process_state', lambda _pid: 'alive' if state != 'unknown' else 'unknown')
    monkeypatch.setattr(executor_liveness, 'process_start_time', lambda _pid: 'new-birth' if state == 'replaced' else 'old-birth')
    assert _collect_runtime_protection(agent) == (False, {'owner:held'})


def test_protection_cache_invalidates_current_attempt_and_scope_lock_generation(tmp_path):
    agent, row, _ = _protection_runtime(tmp_path)
    assert _collect_runtime_protection(agent)[0]
    agent.runtime_db.create_attempt(row['agent_run_id'])
    assert _collect_runtime_protection(agent) == (False, {'owner:held'})


def test_protection_temporary_read_failure_retries_without_source_byte_change(tmp_path, monkeypatch):
    agent, _, _ = _protection_runtime(tmp_path)
    reader = agent.conversation_store.tasks.load_report
    monkeypatch.setattr(agent.conversation_store.tasks, 'load_report', lambda _task: (None, {'error_type': 'OSError'}))
    assert _collect_runtime_protection(agent)[0] is False
    monkeypatch.setattr(agent.conversation_store.tasks, 'load_report', reader)
    assert _collect_runtime_protection(agent) == (True, {'owner:held'})


def test_protection_unknown_operation_prevents_missing_projection_exemption(tmp_path):
    agent, row, attempt = _protection_runtime(tmp_path)
    with agent.runtime_db.transaction() as conn:
        conn.execute("INSERT INTO tool_operations(operation_id,operation_type,status,attempt_id,agent_run_id,"
                     "attempt_generation,tool_operation_generation,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     ('protection-op', 'tool', 'UNKNOWN', attempt['attempt_id'], row['agent_run_id'], 1, 1, 1, 1))
    assert _collect_runtime_protection(agent) == (False, {'owner:held'})


def test_protection_cached_active_task_file_change_invalidates_refs_without_thread_change(tmp_path, monkeypatch):
    import json

    agent, _, _ = _protection_runtime(tmp_path, task_link='')
    tasks = agent.conversation_store.tasks
    path = tasks.storage.task_path('active-link')
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'allowed_skills': ['owner:first']}))
    link = SimpleNamespace(task_id='active-link', status='running', to_dict=lambda: json.loads(path.read_text()))
    monkeypatch.setattr(tasks, '_require_thread', lambda _thread: SimpleNamespace(active_task_ids=('active-link',)))
    monkeypatch.setattr(tasks, 'active_report', lambda _thread: ([link], []))
    assert _collect_runtime_protection(agent) == (True, {'owner:held', 'owner:first'})
    path.write_text(json.dumps({'allowed_skills': ['owner:second']}))
    assert _collect_runtime_protection(agent) == (True, {'owner:held', 'owner:second'})


def test_protection_bounded_scan_resumes_and_new_task_invalidates_prior_complete(tmp_path):
    agent, _, _ = _protection_runtime(tmp_path)
    repo = agent.runtime_db
    for _ in range(3):
        task = repo.create_task(owner_id='owner')
        run = repo.create_task_run(task_id=task['task_id'])
        child = repo.create_agent_run(task_run_id=run['task_run_id'])
        attempt = repo.create_attempt(child['agent_run_id'])
        repo.settle_agent_attempt(agent_run_id=child['agent_run_id'], attempt_id=attempt['attempt_id'])
        with repo.transaction() as conn:
            conn.execute('UPDATE agent_attempts SET metadata_json=? WHERE attempt_id=?', (
                '{"executor":{"schema_version":"attempt-executor.v1","status":"exited","ended_at":1}}',
                attempt['attempt_id'],
            ))
    for _ in range(5):
        complete, protected = _collect_runtime_protection(agent, entries=1)
        if complete:
            break
    assert complete and protected == {'owner:held'}
    task = repo.create_task(owner_id='owner', conversation_task_id='not-created')
    run = repo.create_task_run(task_id=task['task_id'], metadata={'allowed_skills': ['owner:new']})
    child = repo.create_agent_run(task_run_id=run['task_run_id'])
    repo.create_attempt(child['agent_run_id'])
    assert _collect_runtime_protection(agent) == (False, {'owner:held', 'owner:new'})


def test_protection_active_link_write_between_read_and_stamp_is_partial_then_retried(tmp_path, monkeypatch):
    import json

    agent, _, _ = _protection_runtime(tmp_path, task_link='')
    tasks = agent.conversation_store.tasks
    path = tasks.storage.task_path('active-link')
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'allowed_skills': ['owner:before']}))
    monkeypatch.setattr(tasks, '_require_thread', lambda _thread: SimpleNamespace(active_task_ids=('active-link',)))
    changed = False

    def read_and_change(_thread):
        nonlocal changed
        data = json.loads(path.read_text())
        link = SimpleNamespace(task_id='active-link', status='running', to_dict=lambda: data)
        if not changed:
            changed = True
            path.write_text(json.dumps({'allowed_skills': ['owner:after']}))
        return [link], []

    monkeypatch.setattr(tasks, 'active_report', read_and_change)
    assert _collect_runtime_protection(agent) == (False, {'owner:held', 'owner:before'})
    assert _collect_runtime_protection(agent) == (True, {'owner:held', 'owner:after'})


def test_protection_fixed_snapshot_cpu_jitter_resumes_multiple_jobs_without_budget_cliff(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import skill_learning_proposals
    from agent_py_agent.agent.capability import skill_learning_protection as protection

    agent, _, _ = _protection_runtime(tmp_path)
    agent.home_paths = SimpleNamespace(owner_home_dir=tmp_path, owner_skill_learning_dir=tmp_path/'data/skill_learning')
    jobs = [{'allowed_skills': [f'owner:job-{index}']} for index in range(3)]
    agent.scheduler_repository = SimpleNamespace(list_jobs=lambda **_kw: (jobs, []))
    agent.subagents = SimpleNamespace(list_runs_report=lambda: SimpleNamespace(runs=[], load_errors=[]))
    monkeypatch.setattr(skill_learning_proposals, 'list_learning_proposals', lambda *_a, **_kw: [])
    snapshot = protection._runtime_task_ref_snapshot

    def delayed(*args, **kwargs):
        time.sleep(0.04)
        return snapshot(*args, **kwargs)

    monkeypatch.setattr(protection, '_runtime_task_ref_snapshot', delayed)
    results = [protection.learning_protection_for_agent(agent) for _ in range(4)]
    assert any(result['protection_complete'] for result in results)
    assert results[-1]['protection_complete']
    assert set(results[-1]['protected_ids']) == {'owner:held', 'owner:job-0', 'owner:job-1', 'owner:job-2'}
    # 新监控内容不能沿用已完整消费的旧保护证明。
    jobs[0] = {'allowed_skills': ['owner:replacement-job']}
    changed = [protection.learning_protection_for_agent(agent) for _ in range(3)]
    assert changed[-1]['protection_complete']
    assert 'owner:replacement-job' in changed[-1]['protected_ids']
    assert 'owner:job-0' not in changed[-1]['protected_ids']
