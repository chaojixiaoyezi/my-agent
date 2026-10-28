"""R4 审计反例使用原发布、采用和队列入口；临时 home，不调用模型。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.skill_learning_host_feedback import _observe
from agent_py_agent.agent.capability.skill_learning_maintenance import (
    LearningObservationSource,
    observe_learning,
)
from agent_py_agent.agent.capability.skill_learning_publish import (
    PublishRequest,
    publish_learned_skill,
)
from agent_py_agent.agent.capability.skill_learning_request import build_learning_request
from agent_py_agent.agent.capability.skill_learning_validation import (
    default_machine_promotion_verifier,
)
from agent_py_agent.tests.test_skill_learning import BODY, _ctx, _runtime
from agent_py_agent.tests.test_wave7_autonomous_learning_closure import decision


# LLM: 独审给出的无步骤孤立围栏和空触发节必须在真实核验入口被拒，不以推导代替实跑。
# 函数用途: 防止兼容分支或跨节正则将空壳候选当作完整Skill。
@pytest.mark.parametrize('body', ['# 草稿\n```', '# When to use\n# Procedure', '# When to use\n\n# Procedure'])
def test_r4_review_incomplete_fence_and_empty_trigger_section_are_rejected(body):
    draft = {**decision().__dict__, 'body': body, 'when_to_use': '', 'tags': []}
    result = default_machine_promotion_verifier(draft)
    assert result['status'] == 'failed'


# LLM: 通过真实默认核验器，不替换检查函数；每种输入必须命中结构拒绝码。
# 函数用途: 防止无结构填充被当作可复用方法。
@pytest.mark.parametrize('body', ['just do it', 'read inputs\nwrite outputs', '# only a title', '- only one step'])
def test_r4_f07_rejects_unstructured_procedure(body):
    result = default_machine_promotion_verifier(replace(decision(), body=body).__dict__)
    assert result['status'] == 'failed'
    assert result['error_code'] == 'skill_procedure_unstructured'


# LLM: 缺描述由独立码报告，不用其它闸门失败冒充该检查。
# 函数用途: 确保Skill必有非空描述。
@pytest.mark.parametrize('description', ['', '   ', None])
def test_r4_f07_requires_description(description):
    result = default_machine_promotion_verifier({**decision().__dict__, 'description': description})
    assert result['status'] == 'failed' and result['error_code'] == 'description_missing'


# LLM: 正文有步骤但没有适用信号或完整旧分节时，应准确拒绝。
# 函数用途: 守住触发信息缺失的反例。
def test_r4_f07_rejects_steps_without_trigger_or_legacy_sections():
    result = default_machine_promotion_verifier({**decision().__dict__, 'when_to_use': '', 'tags': [],
                                               'body': '1. Read inputs\n2. Check output'})
    assert result['status'] == 'failed' and result['error_code'] == 'skill_trigger_metadata_missing'


# LLM: 覆盖真实when/tags、触发节与旧完整分节；静态通过不能标为执行成功。
# 函数用途: 保证合法方法及旧Skill夹具兼容。
@pytest.mark.parametrize('body,metadata', [
    ('- Read inputs\n- Check output', {'when_to_use': 'tabular data'}),
    ('1. Read inputs\n2. Check output', {'tags': ['csv']}),
    ('```sh\nprintf ok\n```', {'tags': ['command']}),
    ('## When to use\nFor CSV inputs\n1. Inspect headers', {}),
    ('## 适用场景\nCSV 输入', {}),
    ('# Command\n```sh\nprintf ok\n```', {}),
    (BODY, {}),
])
def test_r4_f07_structured_skills_and_legacy_description_are_valid(body, metadata):
    draft = {**decision().__dict__, 'when_to_use': '', 'tags': [], 'body': body, **metadata}
    result = default_machine_promotion_verifier(draft)
    assert result['status'] == 'passed' and result['executed'] is False


# LLM: 经原enqueue和refill恢复路径检查，共根、同轨迹、显式谱系均不能吞掉不同子任务。
# 函数用途: 重现同根子任务误合并。
@pytest.mark.parametrize('lineage', [False, True])
def test_r4_f08_distinct_subtasks_survive_recovery_with_same_root_and_trajectory(tmp_path, lineage):
    store = _runtime(tmp_path).store
    first = build_learning_request(_ctx('same-run'), 1)
    first.update(task_id='task-a', root_task_id='root-1', request_key='a'*16)
    second = {**first, 'task_id': 'task-b', 'request_key': 'b'*16}
    if lineage:
        second['merge_from_request_ids'] = [first['request_key']]
    store.enqueue(first)
    store.enqueue(second)
    queued = [store.read_request(path) for path in store.pending_requests(refill=True)]
    assert {row['task_id'] for row in queued} == {'task-a', 'task-b'}


# LLM: 仅原学习采用入口写 model 回执；hash 从当前登记表取，不构造成功证明。
# 函数用途: 为指定技能版本准备真实采用记录。
def _adopt(store, name, run):
    row = store.load_registry().skills[name]
    observe_learning(store, name, sha256=row.sha256, task_id=f'{run}-{name}-{row.version}',
                     outcome='adopted', source=LearningObservationSource(run_id=run))
    return row.sha256


# LLM: 走真实发布/更新/采用入口；更新不能抹除run内旧版身份，观察不伪造host质量。
# 函数用途: 核对多技能、同技能两版与连续更新的归因歧义。
@pytest.mark.parametrize('new_adopted,other_adopted,updates', [(False, True, 1), (True, False, 1), (False, True, 2)])
def test_r4_f11_updated_adoption_history_keeps_untargeted_receipt_ambiguous(tmp_path, new_adopted, other_adopted, updates):
    ctx = _runtime(tmp_path)
    store = ctx.store
    publish_learned_skill(store, PublishRequest(decision('skill-a')))
    old_hash = _adopt(store, 'skill-a', 'run-1')
    for number in range(updates):
        current = store.load_registry().skills['skill-a']
        changed = replace(decision('skill-a'), decision='update', update_target='skill-a', body=BODY+f'\nchanged {number}')
        publish_learned_skill(store, PublishRequest(changed, expected_revision=current.revision, expected_sha256=current.sha256))
    if new_adopted:
        _adopt(store, 'skill-a', 'run-1')
    if other_adopted:
        publish_learned_skill(store, PublishRequest(decision('skill-b')))
        _adopt(store, 'skill-b', 'run-1')
    params = SimpleNamespace(run_id='run-1', attempt_id='attempt')
    agent = SimpleNamespace(config=SimpleNamespace(enable_self_learning=True), home_paths=ctx.home, _current_run_params=params)
    _observe(agent, params, 'effective', 'receipt-1')
    registry = store.load_registry()
    assert not any(fact['authority'] == 'host' for row in registry.skills.values()
                   for fact in row.maintenance.get('observations', []))
    assert any(fact['sha256'] == old_hash and fact['run_id'] == 'run-1'
               for fact in registry.skills['skill-a'].maintenance['historical_adoptions'])


# LLM: 只有旧版采用时，不能给没有当前采用记录的新版本记分。
# 函数用途: 防止唯一历史采用被升级成新版采用事实。
def test_r4_f11_old_adoption_cannot_credit_unadopted_new_version(tmp_path):
    ctx = _runtime(tmp_path)
    publish_learned_skill(ctx.store, PublishRequest(decision('skill-a')))
    _adopt(ctx.store, 'skill-a', 'run-1')
    old = ctx.store.load_registry().skills['skill-a']
    changed = replace(decision('skill-a'), decision='update', update_target='skill-a', body=BODY+'\nupdated')
    publish_learned_skill(ctx.store, PublishRequest(changed, expected_revision=old.revision, expected_sha256=old.sha256))
    params = SimpleNamespace(run_id='run-1', attempt_id='attempt')
    agent = SimpleNamespace(config=SimpleNamespace(enable_self_learning=True), home_paths=ctx.home, _current_run_params=params)
    _observe(agent, params, 'effective', 'receipt-old')
    assert ctx.store.load_registry().skills['skill-a'].maintenance.get('helped_count', 0) == 0


# LLM: 自动回滚清空当前observations前必须将采用记录沉入historical_adoptions，防止同run后续多技能无目标回执误归因。
# 函数用途: 覆盖A@v2自动回滚至v1后同run采用B时的无目标回执歧义拒绝。
def test_r5_f11_rollback_preserves_adoption_history_against_ambiguous_receipt(tmp_path):
    ctx = _runtime(tmp_path)
    store = ctx.store
    publish_learned_skill(store, PublishRequest(decision('skill-a')))
    v1_row = store.load_registry().skills['skill-a']
    changed = replace(decision('skill-a'), decision='update', update_target='skill-a', body=BODY + '\nv2-body')
    publish_learned_skill(store, PublishRequest(changed, expected_revision=v1_row.revision, expected_sha256=v1_row.sha256))
    v2_hash = store.load_registry().skills['skill-a'].sha256
    for idx in range(2):
        observe_learning(store, 'skill-a', sha256=v2_hash, task_id=f'fail-{idx}',
                         outcome='adopted', source=LearningObservationSource(run_id='run-rb'))
        observe_learning(store, 'skill-a', sha256=v2_hash, task_id=f'fail-{idx}',
                         outcome='ineffective', source=LearningObservationSource(
                             run_id='run-rb', authority='host', receipt_id=f'neg-{idx}'))
    assert store.load_registry().skills['skill-a'].sha256 == v1_row.sha256
    publish_learned_skill(store, PublishRequest(decision('skill-b')))
    _adopt(store, 'skill-b', 'run-rb')
    params = SimpleNamespace(run_id='run-rb', attempt_id='attempt')
    agent = SimpleNamespace(config=SimpleNamespace(enable_self_learning=True), home_paths=ctx.home, _current_run_params=params)
    _observe(agent, params, 'effective', 'receipt-rb')
    registry = store.load_registry()
    assert registry.skills['skill-b'].maintenance.get('helped_count', 0) == 0
    assert any(fact['sha256'] == v2_hash and fact['run_id'] == 'run-rb'
               for fact in registry.skills['skill-a'].maintenance.get('historical_adoptions', []))
