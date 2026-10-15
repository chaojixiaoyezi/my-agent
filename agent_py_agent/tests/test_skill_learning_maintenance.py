import time
from dataclasses import replace

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
    assert ctx.store.load_registry().skills[NAME].maintenance['quality']=='established'
    for number in range(3):
        observe_learning(ctx.store,NAME,sha256=row.sha256,task_id=f'network-{number}',outcome='environment',source=LearningObservationSource(authority='human',actor_id='human-test',receipt_id=f'control-net-{number}'))
    assert ctx.store.load_registry().skills[NAME].maintenance['quality']=='established'
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
    assert ctx.store.load_registry().skills[NAME].maintenance['state']=='cooled' and ctx.store.skill_path(NAME).exists()
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
    assert build_learning_request(_ctx(source='background_main_agent'),3,include_background=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        decisions = list(pool.map(lambda _:reserve_deployment_learning(tmp_path,3),range(20)))
    assert sum(decisions)==3


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
    assert tool.execute({'operation':'adopted','skill_id':f'owner:{NAME}'}).ok
    observation = store.load_registry().skills[NAME].maintenance['observations'][0]
    assert observation['authority']=='model' and observation['run_id']==background.task_id
    assert not tool.execute({'operation':'effective','skill_id':f'owner:{NAME}'}).ok
    agent.config.enable_self_learning=False
    assert not tool.execute({'operation':'adopted','skill_id':f'owner:{NAME}'}).ok
