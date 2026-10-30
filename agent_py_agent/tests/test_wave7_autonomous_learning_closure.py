# LLM: 原W2真实Store/Router/Runner合同与故障注入回归，verifier替身仅验证晋升语义，不代表生产执行链通过。
# 模块用途: 保留已观察失败的可执行断言，检查自主学习版本、身份与安全闭环，不删测或放宽门禁。
"""Wave7 W2: observed failures remain executable contracts, not report-only claims.

来源: 2026-10-27 wave7-w2-autonomous-learning.md / F01–F17审计及原始复现。
做法: 真Store/Router/Runner和故障注入；verifier替身只验证晋升合同，不宣称生产接线。
复现: python3 -m pytest agent_py_agent/tests/test_wave7_autonomous_learning_closure.py -q --tb=short
W1依赖(F06/F14/F15)原样单列test_wave7_w1_integration_contracts.py，仍正常收集且不跳过/xfail；此处验证W2自身合同。
"""
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.skill_learning import _material
from agent_py_agent.agent.capability.skill_learning_host_feedback import (
    observe_command_verification,
)
from agent_py_agent.agent.capability.skill_learning_maintenance import (
    LearningObservationSource,
    observe_learning,
)
from agent_py_agent.agent.capability.skill_learning_prompt import SkillLearningDecision
from agent_py_agent.agent.capability.skill_learning_publish import (
    PublishRequest,
    publish_learned_skill,
)
from agent_py_agent.agent.capability.skill_learning_request import build_learning_request
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

BODY = ('# 数据整理\n\n## 步骤\n先核对表头、编码和输入合同，再按日期稳定排序。'
        '输出前核对输入输出行数，保留原件，抽查首尾记录及字段类型。' * 3)


# LLM: 默认把不同 run 放在同一线程，工具轨迹由调用方传入，便于分离任务身份与线程归属。
# 函数用途: 构造带任务、请求标识及归档工具调用的学习请求测试上下文。
def context(run='a', calls=()):
    return SimpleNamespace(do_save=True, context_scope='default', source='tui', tool_rounds=8,
                           run_id=run, task_id='task-'+run, request_id='request-'+run,
                           task_attributes={'conversation_thread_id': 'same-thread'},
                           user_prompt='合并CSV', final_response=SimpleNamespace(text='完成'),
                           archive_tool_calls=list(calls))


# LLM: 名称可变但正文与其他元数据保持固定，避免身份测试被候选内容变化干扰。
# 函数用途: 生成具有有效说明、适用条件和复用步骤的创建型学习决策。
def decision(name='csv-cleaner'):
    return SkillLearningDecision('create', '可复用', name=name, description='合并CSV并核对结果',
                                 when_to_use='合并CSV时', tags=('csv',), body=BODY)


# LLM: 每个用例使用独立临时 home，并经真实发布入口写入初始技能，不伪造登记表状态。
# 函数用途: 为版本、反馈与队列回归提供已发布 csv-cleaner 的 home 和学习 Store。
@pytest.fixture
def learned(tmp_path):
    home = ensure_my_agent_home(tmp_path/'home')
    store = SkillLearningStore.for_home(home)
    publish_learned_skill(store, PublishRequest(decision()))
    return home, store


# LLM: 重复形参和函数外 return 必须在编译检查中失败，不能因语法树可解析而通过，也不得执行候选。
# 函数用途: 回归默认静态验证器对脚本编译错误的拒绝及未执行标记。
@pytest.mark.parametrize('source', ['def run(value, value):\n    return value\n', 'return 1\n'])
def test_r3_review_static_script_rejects_compile_errors(source):
    from agent_py_agent.agent.capability.asset_promotion import default_machine_promotion_verifier

    result = default_machine_promotion_verifier({'kind':'script', 'name':'compile-probe',
                                               'description':'检查脚本编译错误', 'code':source})
    assert result['status'] == 'failed' and result['executed'] is False


# LLM: 候选一旦执行就会抛异常；静态检查通过只能说明编译成功，executed 必须保持 False。
# 函数用途: 确认脚本编译验证不会运行候选顶层代码。
def test_r3_review_script_compilation_does_not_execute():
    from agent_py_agent.agent.capability.asset_promotion import default_machine_promotion_verifier

    result = default_machine_promotion_verifier({'kind':'script', 'name':'no-exec-probe',
                                               'description':'编译不能执行候选',
                                               'code':'raise AssertionError("candidate was executed")'})
    assert result['status'] == 'passed' and result['executed'] is False


# LLM: 缺失轨迹字段不构成合并证据，两个入队顺序都不能吞掉带完整轨迹的请求。
# 函数用途: 验证轨迹不完整的同 run 请求与已知轨迹请求分别保留。
@pytest.mark.parametrize('reverse', [False, True])
def test_r3_review_missing_trajectory_cannot_erase_known_material(learned, reverse):
    _, store = learned
    left = build_learning_request(context('same'), 1)
    left.update(request_key='a'*16, trajectory_digest='a'*64, trajectory_fingerprint='b'*64)
    right = {key:value for key, value in left.items() if key not in {'trajectory_digest','trajectory_fingerprint'}}
    right['request_key'] = 'b'*16
    for request in (right, left) if reverse else (left, right):
        store.enqueue(request)
    assert len(store.pending_requests(refill=True)) == 2


# LLM: 自定义 verifier 即使不可哈希或布尔值为假也不能被替换成默认验证器，其失败结论必须生效。
# 函数用途: 检查特殊可调用对象被实际调用一次且不会误发布候选。
@pytest.mark.parametrize('kind', ['unhashable', 'falsey'])
def test_r3_review_custom_callable_is_not_implicitly_defaulted(learned, kind):
    from agent_py_agent.agent.capability.asset_promotion import promote_verified_skill

    # LLM: 对象不可哈希，并可按参数呈现假值；调用次数用于辨别生产入口是否跳过了自定义验证器。
    # 类用途: 构造返回拒绝回执的特殊可调用验证器替身。
    class Verifier:
        __hash__ = None
        calls = 0

        # LLM: 仅 falsey 场景返回 False，但对象仍有有效的 __call__，不能据此当作未提供。
        # 函数用途: 为验证器缺省选择回归控制对象的真假值。
        def __bool__(self):
            return kind != 'falsey'

        # LLM: 回执绑定传入 hash 并明确失败；计数能揭示自定义 verifier 是否真的被调用。
        # 函数用途: 记录一次候选验证调用并返回失败回执。
        def __call__(self, candidate, sha256):
            self.calls += 1
            return {'status':'failed', 'sha256':sha256, 'receipt_id':'rejected'}

    _, store = learned
    verifier = Verifier()
    publish = PublishRequest(decision('callable-probe'), request=build_learning_request(context(), 1))
    assert promote_verified_skill(store, publish, verifier) is None
    assert verifier.calls == 1 and 'callable-probe' not in store.load_registry().skills


# LLM: 线程相同不代表同一次学习任务；恢复队列后必须仍能读到两个不同 run 的请求。
# 函数用途: 回归同线程不同运行的学习请求不会在队列恢复时互相覆盖。
def test_f08_distinct_runs_in_same_thread_survive_queue_recovery(learned):
    _, store = learned
    for run in ('csv', 'ssh'):
        assert store.enqueue(build_learning_request(context(run), 1)) == 'queued'
    assert {store.read_request(path)['run_id'] for path in store.pending_requests(refill=True)} == {'csv', 'ssh'}


# LLM: 即使 task_id 相同，旧轨迹与修复后轨迹也不能在没有显式合并证据时覆盖彼此。
# 函数用途: 验证相同任务的不同轨迹保留为两个待处理请求。
def test_f08_same_task_different_trajectory_is_not_overwritten(learned):
    _, store = learned
    for run in ('first', 'repaired'):
        request = build_learning_request(context(run), 1)
        request.update(task_id='same-task', trajectory_fingerprint=run)
        store.enqueue(request)
    assert len(store.pending_requests(refill=True)) == 2


# LLM: 分别覆盖不同根任务及缺失任务字段，不能借共同线程或空字段推定不同 run 应当合并。
# 函数用途: 检查缺少显式合并证据的不同运行均保留在队列中。
@pytest.mark.parametrize('root,task', [('root-a', 'same-task'), ('', '')])
def test_f08_distinct_runs_without_merge_evidence_survive(learned, root, task):
    _, store = learned
    for number, run in enumerate(('first', 'second')):
        request = build_learning_request(context(run), 1)
        request.update(root_task_id=root+str(number) if root else '', task_id=task,
                       trajectory_fingerprint='')
        store.enqueue(request)
    assert len(store.pending_requests(refill=True)) == 2


# LLM: 新请求明确引用旧 request_key 才允许收敛，即使轨迹已变化，也应留下新 run 的材料。
# 函数用途: 验证显式合并谱系能把旧请求更新为唯一的新请求。
def test_f08_explicit_merge_lineage_converges_changed_trajectory(learned):
    _, store = learned
    first = build_learning_request(context('first'), 1)
    second = build_learning_request(context('second'), 1)
    first.update(task_id='same', trajectory_fingerprint='old')
    second.update(task_id='same', trajectory_fingerprint='new', merge_from_request_ids=[first['request_key']])
    store.enqueue(first)
    store.enqueue(second)
    paths = store.pending_requests(refill=True)
    assert len(paths) == 1 and store.read_request(paths[0])['run_id'] == 'second'


# LLM: root_task_id 与 task_id 的等价身份可合并，不同身份则必须分开，不能退回共同线程作去重键。
# 函数用途: 回归根任务和任务字段别名在同轨迹请求去重中的一致性。
@pytest.mark.parametrize('other_task,count', [('task-A', 1), ('task-B', 2)])
def test_review_f08_root_and_task_aliases_do_not_fall_back_to_thread(learned, other_task, count):
    _, store = learned
    first = build_learning_request(context('a'), 1)
    second = build_learning_request(context('b'), 1)
    first.update(root_task_id='task-A', task_id='', trajectory_fingerprint='same')
    second.update(task_id=other_task, trajectory_fingerprint='same')
    store.enqueue(first)
    store.enqueue(second)
    assert len(store.pending_requests(refill=True)) == count


# LLM: 工具轨迹限长时仍须保留后段修复与验证回合及其证据引用，不能只留下早期失败。
# 函数用途: 检查长轨迹压缩后保留最后修复、验证及可追溯证据。
def test_f09_segmented_trace_keeps_fix_verifier_and_evidence():
    calls = [{'tool': 'run_command', 'ok': False, 'tool_round': n,
              'parameters': {'command': f'attempt-{n}'}} for n in range(1, 81)]
    calls.extend({'tool': 'run_command', 'ok': True, 'tool_round': n,
                  'parameters': {'command': label}, 'evidence_refs': [f'verification:{n}']}
                 for n, label in ((81, 'fix'), (82, 'verify')))
    request = build_learning_request(context(calls=calls), 1)
    assert len(request['tool_trace']) == 80
    assert {81, 82} <= {row['round'] for row in request['tool_trace']}
    assert 'verification:82' in json.dumps(request)


# LLM: 成功按裸名读取的 owner 技能应进入可更新材料；有规范 stable_id 回执时还须保存准确已用身份。
# 函数用途: 验证裸名读取在有无规范结果两种情况下都能关联现有技能。
@pytest.mark.parametrize('canonical_result', [False, True])
def test_f13_bare_name_read_is_updatable(learned, canonical_result):
    _, store = learned
    call = {'tool': 'skill_search', 'ok': True, 'parameters': {'skill_id': 'csv-cleaner'}}
    if canonical_result:
        call['result'] = {'stable_id': 'owner:csv-cleaner'}
    request = build_learning_request(context(calls=[call]), 1)
    entry = SimpleNamespace(name='csv-cleaner', source='owner', stable_id='owner:csv-cleaner',
                            path=str(store.skill_path('csv-cleaner')), description='CSV')
    assert len(_material(store, request, (entry,)).updatable_skills) == 1
    if canonical_result:
        assert request['used_skill_ids'] == ['owner:csv-cleaner']


# LLM: 两个技能都被采用时，带准确目标和当前 hash 的宿主回执只能记到指定技能，不能广播记功。
# 函数用途: 检查各目标字段别名的验证回执只归属命中的当前版本。
@pytest.mark.parametrize('target_field', ['skill_id', 'asset_id', 'asset_ref', 'target_skill_ids'])
def test_f11_receipt_credits_only_named_current_version(learned, target_field):
    home, store = learned
    publish_learned_skill(store, PublishRequest(decision('ssh-triage')))
    params = SimpleNamespace(run_id='verified-run', attempt_id='a')
    agent = SimpleNamespace(config=SimpleNamespace(enable_self_learning=True), home_paths=home, _current_run_params=params)
    for row in store.load_registry().skills.values():
        observe_learning(store, row.name, sha256=row.sha256, task_id='task', outcome='adopted',
                         source=LearningObservationSource(run_id=params.run_id))
    target = ['owner:csv-cleaner'] if target_field == 'target_skill_ids' else 'owner:csv-cleaner'
    observe_command_verification(agent, params, {'id': 1, 'status': 'passed', 'exit_code': 0, target_field: target,
                                               'skill_content_hash':store.load_registry().skills['csv-cleaner'].sha256})
    registry = store.load_registry()
    assert any(row['authority'] == 'host' for row in registry.skills['csv-cleaner'].maintenance['observations'])
    assert not any(row['authority'] == 'host' for row in registry.skills['ssh-triage'].maintenance['observations'])


# LLM: 多个已采用技能使无目标回执含糊，显式目标配旧 hash 也无效，两者都不能生成宿主正反馈。
# 函数用途: 回归无归属或版本不匹配的命令验证不会给技能记功。
def test_f11_unattributed_or_stale_hash_receipt_does_not_credit(learned):
    home, store = learned
    publish_learned_skill(store, PublishRequest(decision('ssh-triage')))
    row = store.load_registry().skills['csv-cleaner']
    params = SimpleNamespace(run_id='r', attempt_id='a')
    agent = SimpleNamespace(config=SimpleNamespace(enable_self_learning=True), home_paths=home, _current_run_params=params)
    observe_learning(store, row.name, sha256=row.sha256, task_id='t', outcome='adopted', source=LearningObservationSource(run_id='r'))
    other = store.load_registry().skills['ssh-triage']
    observe_learning(store, other.name, sha256=other.sha256, task_id='t', outcome='adopted', source=LearningObservationSource(run_id='r'))
    for extra in ({}, {'skill_id': 'owner:csv-cleaner', 'sha256': 'a'*64}):
        observe_command_verification(agent, params, {'id': 2, 'status': 'passed', 'exit_code': 0, **extra})
    assert not any(f['authority'] == 'host' for f in store.load_registry().skills[row.name].maintenance['observations'])


# LLM: 单一采用可提供无歧义的默认归属，但错误 hash 或显式空目标必须阻止这种推断。
# 函数用途: 检查单技能采用时宿主回执推断归属的允许与拒绝边界。
@pytest.mark.parametrize('extra,credited', [({}, True), ({'sha256':'a'*64}, False),
                                          ({'content_hash':'a'*64}, False),
                                          ({'target_skill_ids':[]}, False)])
def test_f11_single_adoption_uses_unambiguous_host_receipt(learned, extra, credited):
    home, store = learned
    row = store.load_registry().skills['csv-cleaner']
    params = SimpleNamespace(run_id='single', attempt_id='a')
    agent = SimpleNamespace(config=SimpleNamespace(enable_self_learning=True), home_paths=home, _current_run_params=params)
    observe_learning(store, row.name, sha256=row.sha256, task_id='t', outcome='adopted', source=LearningObservationSource(run_id='single'))
    observe_command_verification(agent, params, {'id':3, 'status':'passed', 'exit_code':0, **extra})
    assert any(f['authority'] == 'host' for f in store.load_registry().skills[row.name].maintenance['observations']) is credited


# LLM: 同一回执里 skill_id 与 asset_ref 指向不同技能时，即使 hash 正确也不能只挑有利字段。
# 函数用途: 确认相互冲突的显式目标不能匹配当前技能。
def test_f11_conflicting_explicit_targets_do_not_credit(learned):
    from agent_py_agent.agent.capability.skill_learning_host_feedback import _receipt_targets

    _,store = learned
    row = store.load_registry().skills['csv-cleaner']
    assert not _receipt_targets({'skill_id':'owner:csv-cleaner','asset_ref':'owner:unrelated','sha256':row.sha256},row)


# LLM: 列表内目标的所有身份与 hash 别名也必须一致，不能忽略嵌套对象中冲突的别名。
# 函数用途: 检查嵌套目标携带错误身份或内容摘要时被拒绝匹配。
@pytest.mark.parametrize('conflict', [{'asset_id':'owner:unrelated'}, {'content_sha256':'a'*64}])
def test_review_f11_nested_target_aliases_must_all_agree(learned, conflict):
    from agent_py_agent.agent.capability.skill_learning_host_feedback import _receipt_targets

    _, store = learned
    row = store.load_registry().skills['csv-cleaner']
    target = {'stable_id':'owner:csv-cleaner', 'sha256':row.sha256, **conflict}
    assert not _receipt_targets({'target_skill_ids':[target]}, row)


# LLM: 损坏的快照引用索引不是合法空集合，必须显式报错，避免误判保护信息已完整收集。
# 函数用途: 验证引用收集器拒绝格式错误的 skill_snapshot_refs。
def test_f17_malformed_refs_are_not_a_complete_empty_set():
    from agent_py_agent.agent.capability.skill_learning_protection import _include_refs

    with pytest.raises(ValueError):
        _include_refs(set(), {'skill_snapshot_refs':'broken index'})


# LLM: 已稳定技能的正文 hash 变化后不能继承旧版质量和计数，但必须保存旧版版本、修订及 hash 供回退。
# 函数用途: 检查更新发布重置试用状态并记录准确的上一稳定版本。
def test_f12_new_hash_resets_trial_and_keeps_previous_stable(learned):
    _, store = learned
    current = store.load_registry().skills['csv-cleaner']
    with store.state_lock():
        registry = store.load_registry()
        store.save_registry(replace(registry, skills={current.name: replace(current, maintenance={'state': 'active', 'quality': 'established', 'helped_count': 8})}))
    update = replace(decision(), decision='update', update_target=current.name, body=BODY+'\n检查空值。')
    publish_learned_skill(store, PublishRequest(update, expected_revision=current.revision, expected_sha256=current.sha256))
    after = store.load_registry().skills[current.name]
    assert after.maintenance['state'] == after.maintenance['quality'] == 'trial'
    assert after.maintenance['helped_count'] == after.maintenance['hurt_count'] == 0
    assert after.maintenance['previous_stable'] == {'version': current.version, 'revision': current.revision, 'sha256': current.sha256}


# LLM: 说明中明确排除的源代码审计不能因关键词出现而加分，真正适用的审计卡片应获正分。
# 函数用途: 回归路由评分对否定适用条件与正向用途的区分。
def test_f02_negative_applicability_is_not_positive_keyword():
    from agent_py_agent.agent.capability.router import CapabilityCard, score_card

    excluded = CapabilityCard('excluded', 'skill', 'log-triage', '处理事件日志；不用于源代码审计')
    suited = CapabilityCard('suited', 'skill', 'code-audit', '源代码审计')
    assert score_card('源代码审计', excluded)[0] < 0
    assert score_card('源代码审计', suited)[0] > 0


# LLM: 漏洞发现与修复报告的改写表达应匹配证据整理包，明确排除证据整理的查询则不能强匹配。
# 函数用途: 检查能力包强匹配同时识别任务改写和查询中的否定要求。
def test_f03_package_paraphrase_and_query_negation():
    from agent_py_agent.agent.capability.router import CapabilityCard, has_strong_match

    card = CapabilityCard('capability:security-evidence', 'capability_package', 'security-evidence',
                          '整理漏洞发现、修复报告和证据，生成审计报告', keywords=['证据整理', '审计报告'])
    assert has_strong_match('把已有漏洞发现和修复报告整理成报告', card)
    assert not has_strong_match('别做证据整理，只扫描漏洞', card)


# LLM: 快照使用固定指纹且只包含传入条目，不触发磁盘发现，便于隔离路由和分页合同。
# 函数用途: 为搜索与推荐测试创建最小的冻结技能快照。
def snapshot(entries=(), workspace='/unused'):
    from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshot

    return SkillSnapshot(tuple(entries), (), 'frozen', 'owner', str(workspace))


# LLM: 大量无关内置技能不能在索引截断时挤掉精确适用的 Owner 技能，断言检查实际渲染结果。
# 函数用途: 验证相关私有技能在大目录元数据索引中仍然可见。
def test_f04_owner_exact_fit_is_not_truncated_by_builtin_catalog():
    from agent_py_agent.agent.capability.router import CapabilityRouter
    from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshotEntry

    entries = [SkillSnapshotEntry(f'builtin:base-{i}', f'base-{i}', '无关文档处理', '/unused', 'builtin', 'test', 'a'*64)
               for i in range(200)]
    entries.append(SkillSnapshotEntry('owner:alarm-summary', 'alarm-summary', '汇总今日告警', '/unused', 'owner', 'test', 'b'*64))
    router = CapabilityRouter(skill_snapshot=snapshot(entries))
    assert 'owner:alarm-summary' in router.render_skill_metadata_index(context_window_tokens=128000)


# LLM: 连续分页必须保持准确总数且无遗漏；快照条目变化后旧游标必须失败，不能继续混读新目录。
# 函数用途: 检查全局技能搜索的分页覆盖、结束标记及快照漂移拒绝。
def test_f04_global_search_paginates_without_gaps_or_snapshot_drift():
    from agent_py_agent.agent.capability.router import CapabilityRouter
    from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
    from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshotEntry

    entries = [SkillSnapshotEntry(f'owner:alert-{i}', f'alert-{i}', '告警汇总', '/unused', 'owner', 'test', 'a'*64)
               for i in range(30)]
    router = CapabilityRouter(skill_snapshot=snapshot(entries))
    tool = SkillSearchTool(SimpleNamespace(capability_router=router))
    first = json.loads(tool.execute({'query': '告警汇总', 'limit': 20}).output)
    assert first.get('total') == 30 and first.get('has_more') is True
    second = json.loads(tool.execute({'query': '告警汇总', 'cursor': first['next_cursor'], 'limit': 20}).output)
    assert second['total'] == 30 and second['has_more'] is False
    assert len({row['skill_id'] for row in first['matches']+second['matches']}) == 30
    router._skill_snapshot = snapshot(entries[:-1])
    assert not tool.execute({'query': '告警汇总', 'cursor': first['next_cursor']}).ok


# LLM: 通过真实 Store 保存的脚本与工作流都应进入搜索摘要；该入口不得泄露代码或步骤，也不在此执行。
# 函数用途: 验证已有脚本和工作流能够作为直接能力候选被发现。
def test_f01_saved_scripts_and_workflows_are_direct_candidates(tmp_path):
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore
    from agent_py_agent.agent.assets.workflow_store import (
        WorkflowDefinition,
        WorkflowStepSpec,
        WorkflowStore,
    )
    from agent_py_agent.agent.capability.router import CapabilityRouter
    from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool

    home, workspace = tmp_path/'owner', tmp_path/'workspace'
    workspace.mkdir()
    ScriptStore(home, workspace_root=workspace).save_script(
        AssetDescriptor(id='alarm', kind='script', name='汇总今日告警'), 'print("{}")', target_scope='workspace')
    WorkflowStore(home).save_workflow(WorkflowDefinition(AssetDescriptor(id='alarms', kind='workflow', name='汇总今日告警'),
        (WorkflowStepSpec('s', '汇总', 'deterministic_script', script_id='alarm'),)))
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home, owner_id='owner'),
                            tools=SimpleNamespace(workspace_root=workspace))
    agent.capability_router = CapabilityRouter(skill_snapshot=snapshot(workspace=workspace))
    result = SkillSearchTool(agent).execute({'query': '汇总今日告警'})
    matches = json.loads(result.output)['matches']
    assert {row['kind'] for row in matches} >= {'script', 'workflow'}
    assert all('code' not in row and 'steps' not in row for row in matches)


# LLM: 用工作区脚本的真实输出证明 Runner 执行了候选代码；显式关闭沙箱，不据此外推沙箱隔离能力。
# 函数用途: 回归工作区作用域脚本能经真实 Runner 执行并返回业务数据。
def test_f05_workspace_script_executes_actual_code(tmp_path):
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner

    home, project = tmp_path/'owner', tmp_path/'project'
    project.mkdir()
    ScriptStore(home, workspace_root=project).save_script(AssetDescriptor(id='project', kind='script', name='项目脚本'),
                                                        'print(\'{"executed": true}\')', target_scope='workspace')
    result = ScriptRunner(home, workspace_root=project, use_sandbox=False).run_script('project')
    assert result.ok, result.error_code
    assert result.output_data['executed'] is True


# LLM: 两个独立任务的宿主负反馈触发回退，旧正文与版本恢复但修订号仍须递增，不能倒退 CAS 身份。
# 函数用途: 检查试用版本连续退化后恢复稳定内容并保持版本存储一致。
def test_f12_trial_regressions_restore_previous_stable(learned):
    _, store = learned
    current = store.load_registry().skills['csv-cleaner']
    update = replace(decision(), decision='update', update_target=current.name, body=BODY+'\n错误改版。')
    publish_learned_skill(store, PublishRequest(update, expected_revision=current.revision, expected_sha256=current.sha256))
    trial = store.load_registry().skills[current.name]
    for task in ('regression-1', 'regression-2'):
        observe_learning(store, current.name, sha256=trial.sha256, task_id=task, outcome='adopted')
        observe_learning(store, current.name, sha256=trial.sha256, task_id=task, outcome='ineffective',
                         source=LearningObservationSource(authority='host', receipt_id=task))
    restored = store.load_registry().skills[current.name]
    assert restored.sha256 == current.sha256 and restored.version == current.version
    assert restored.revision > trial.revision
    assert store.read_version(current.name, current.version) == store.skill_path(current.name).read_text()


# LLM: 新采用反馈可以更新技能热度，却不能改写上次维护时钟并掩盖到期维护任务。
# 函数用途: 确认热反馈后维护仍被发现为待处理，且维护时间保持原值。
def test_f17_maintenance_clock_survives_hot_observation(learned):
    from agent_py_agent.agent.capability.skill_learning import (
        SkillLearningService,
        SkillLearningSettings,
    )
    from agent_py_agent.agent.capability.skill_learning_maintenance import maintain_learning

    _, store = learned
    maintain_learning(store, mode='autonomous', now=1, protection_complete=True)
    current = store.load_registry().skills['csv-cleaner']
    observe_learning(store, current.name, sha256=current.sha256, task_id='hot', outcome='adopted')
    service = SkillLearningService(store, SkillLearningSettings(maintenance_mode='autonomous'), SimpleNamespace())
    assert service.has_pending()
    assert store.load_registry().last_maintenance_at == 1


# LLM: 大量已完成任务不应耗尽在途保护扫描，合法空快照引用也不应被当作保护来源失败。
# 函数用途: 验证超过扫描限额的历史任务与空引用不阻止保护信息完整收集。
def test_f17_empty_refs_and_completed_history_do_not_block_protection(learned, tmp_path):
    from agent_py_agent.agent.capability.skill_learning_protection import (
        learning_protection_for_agent,
    )

    home, _ = learned
    directory = tmp_path/'tasks'
    directory.mkdir()
    for index in range(1002):
        (directory/f'{index:04}.json').write_text('{}')
    # LLM: 只有指定末尾任务为 running，其余均完成；序列化结果显式携带合法空引用列表。
    # 函数用途: 为保护扫描构造区分历史任务与在途任务的加载结果。
    def load(task):
        status = 'running' if task == '1001' else 'completed'
        return SimpleNamespace(status=status, skill_snapshot_refs=[], to_dict=lambda: {'skill_snapshot_refs':[]}), None
    agent = SimpleNamespace(home_paths=home, conversation_store=SimpleNamespace(
        threads=SimpleNamespace(list_report=lambda **kw: ([SimpleNamespace(thread_id='t')], [])),
        tasks=SimpleNamespace(storage=SimpleNamespace(tasks_dir=directory), load_report=load,
                              active_report=lambda thread: ([load('1001')[0]], []))),
        scheduler_repository=SimpleNamespace(list_jobs=lambda **kw: ([], [])),
        subagents=SimpleNamespace(list_runs_report=lambda: SimpleNamespace(load_errors=[], runs=[])))
    assert learning_protection_for_agent(agent, scan_limit=10)['protection_complete'] is True


# LLM: 对同一对话轮次重复提取须复用提案身份；真正提交后技能登记与统一资产登记必须同时存在。
# 函数用途: 回归轮次重放的幂等提案及提交后的统一登记。
def test_f10_f16_turn_replay_reuses_one_registered_family(tmp_path):
    from agent_py_agent.agent.assets import turn_extractor as extract
    from agent_py_agent.tests.test_turn_asset_extractor import history

    home, store, thread, _, first, *_ = history.__wrapped__(tmp_path)
    refs = [{'thread_id':thread.thread_id, 'message_id':first.message_id}]
    a = extract.propose_assets_from_turns(store, owner_home=home, turn_refs=refs, target_types=['skill'])
    b = extract.propose_assets_from_turns(store, owner_home=home, turn_refs=refs, target_types=['skill'])
    assert a['proposal_id'] == b['proposal_id']
    extract.commit_extracted_assets(home, proposal_id=a['proposal_id'])
    registry = SkillLearningStore(home/'data'/'skill_learning', home/'skills').load_registry()
    assert a['candidate_skills'][0]['name'] in registry.skills
    assert len(registry.assets) == 1


# LLM: 载体判断使用命令、步骤依赖和分支等结构特征，不靠候选名称猜测应保存成何种资产。
# 函数用途: 检查脚本、工作流和技能三类结构特征对应的载体选择。
@pytest.mark.parametrize('kind,features', [('script', {'commands':['sort input.csv']}),
                                         ('workflow', {'steps':[{'id':'a'}, {'id':'b', 'depends_on':['a']}]}),
                                         ('skill', {'branches':True})])
def test_f07_carrier_selection_uses_structural_features(kind, features):
    from agent_py_agent.agent.capability import asset_promotion

    assert asset_promotion.select_asset_carrier(features) == kind


# LLM: 只改技能名不应改变语义族指纹；指纹相同不等于授权合并不同规范 ID 的发布身份。
# 函数用途: 验证同一方法改名后仍生成相同的语义族摘要。
def test_f10_semantic_family_ignores_skill_rename():
    from agent_py_agent.agent.capability import asset_promotion

    a, b = decision(), decision('same-method-renamed')
    assert isinstance(asset_promotion.task_family_fingerprint('skill', a.__dict__), str)
    assert asset_promotion.task_family_fingerprint('skill', a.__dict__) == asset_promotion.task_family_fingerprint('skill', b.__dict__)


# LLM: 外层 succeeded 不能弥补 dry_run、未执行或缺失执行证明，静态成功不得冒充业务验证。
# 函数用途: 检查业务验证判定拒绝空输出及非真实执行回执。
@pytest.mark.parametrize('output', [{'dry_run':True}, {'executed':False}, {}])
def test_f10_dry_run_is_not_business_validation(output):
    from agent_py_agent.agent.capability import asset_promotion

    assert asset_promotion.business_validated({'status':'succeeded', 'output':output}) is False


# LLM: 参数化 verifier 替身只验证晋升合同；错误 hash 或试跑不能发布，默认静态通过也不能伪造执行或用户确认。
# 函数用途: 核对自主晋升的试用状态、回执等级与人格文件不变边界。
@pytest.mark.parametrize('receipt_kind', ['verified', 'dry_run', 'wrong_hash', 'missing'])
def test_f07_machine_verified_promotion_never_forges_user_confirmation(tmp_path, receipt_kind):
    from dataclasses import asdict

    from agent_py_agent.agent.capability.skill_learning import (
        SkillLearningService,
        SkillLearningSettings,
    )
    from agent_py_agent.agent.capability.skill_learning_prompt import (
        OUTPUT_SCHEMA_VERSION,
        SkillLearningMaterial,
    )

    home = ensure_my_agent_home(tmp_path/'home')
    store = SkillLearningStore.for_home(home)
    personality = {name:(home.owner_home_dir/name).read_bytes() for name in ('AGENTS.md','SOUL.md')}
    # LLM: 根据用例参数返回正确摘要、错误摘要或试跑输出，这些合成回执不代表实际执行候选。
    # 函数用途: 提供控制晋升路径所需的宿主验证回执替身。
    def verifier(publish, sha):
        return {'status':'passed', 'executed':True, 'contract_valid':True,
                'sha256':sha if receipt_kind != 'wrong_hash' else 'a'*64, 'receipt_id':'host-verifier-1',
                'output':{'executed':receipt_kind != 'dry_run'}}
    runtime = SimpleNamespace(guard_config=None, promotion_verifier=verifier if receipt_kind != 'missing' else None)
    settings = SkillLearningSettings.from_config(SimpleNamespace(enable_self_learning=True))
    service = SkillLearningService(store, settings, runtime)
    payload = {'schema_version':OUTPUT_SCHEMA_VERSION, **asdict(decision())}
    result = service._decide(SkillLearningMaterial({'run_id':'source-run'}), json.dumps(payload))
    if receipt_kind in {'dry_run', 'wrong_hash'}:
        assert result.event == 'proposed'
        assert not store.load_registry().skills
        return
    assert result.event == 'published'
    row = store.load_registry().skills['csv-cleaner']
    assert row.maintenance['promotion_authority'] == 'autonomous_verified'
    assert row.maintenance['promotion_history'] == ['observed','candidate','validated','trial']
    assert row.maintenance['quality'] == 'trial'
    if receipt_kind == 'missing':
        assert row.maintenance['verification_receipt']['verification_kind'] == 'deterministic_static'
        assert row.maintenance['verification_receipt']['executed'] is False
    assert 'user_confirmed' not in json.dumps(store.load_registry().to_record())
    assert personality == {name:(home.owner_home_dir/name).read_bytes() for name in personality}


# LLM: e905 DEL-MR-11改为精确规范ID合同；相似正文只能推荐，不吞掉另一个候选的身份。
# 函数用途: 保留不同名字的独立提案，不使用旧family-only去重。
def test_f10_service_keeps_renamed_pending_identity(tmp_path):
    from dataclasses import asdict

    from agent_py_agent.agent.capability.skill_learning import (
        SkillLearningService,
        SkillLearningSettings,
    )
    from agent_py_agent.agent.capability.skill_learning_prompt import (
        OUTPUT_SCHEMA_VERSION,
        SkillLearningMaterial,
    )
    from agent_py_agent.agent.capability.skill_learning_proposals import list_learning_proposals

    store = SkillLearningStore.for_home(ensure_my_agent_home(tmp_path/'home'))
    service = SkillLearningService(store, SkillLearningSettings(), SimpleNamespace(guard_config=None))
    for name in ('one-method', 'same-renamed'):
        output = json.dumps({'schema_version':OUTPUT_SCHEMA_VERSION, **asdict(decision(name))})
        service._decide(SkillLearningMaterial({'run_id':name}), output)
    assert {row['decision']['name'] for row in list_learning_proposals(store)} == {'one-method', 'same-renamed'}


# LLM: 从 record_lesson 候选生成的提案应在统一资产表中保留同一个 proposal_id，不能丢失入口出处。
# 函数用途: 检查 S1 候选提案与统一资产登记之间的来源关联。
def test_f16_s1_candidate_has_unified_provenance(tmp_path):
    from agent_py_agent.tests.test_skill_proposals import _record_lesson, _runtime

    ctx = _runtime(tmp_path)
    [proposal] = ctx.service.propose_from_candidates(_record_lesson(ctx))
    rows = SkillLearningStore.for_home(ctx.home).load_registry().assets.values()
    assert any(ref.get('proposal_id') == proposal.proposal_id for row in rows for ref in row['provenance'])


# LLM: 明显危险的候选必须先被安全门拒绝，验证器调用记录和技能登记都应保持为空。
# 函数用途: 验证发布晋升在调用执行验证器之前先落实内容安全检查。
def test_f07_guard_rejects_before_execution_verifier(tmp_path):
    from agent_py_agent.agent.capability.asset_promotion import promote_verified_skill
    from agent_py_agent.agent.capability.skill_learning_publish import SkillLearningGateError

    store = SkillLearningStore.for_home(ensure_my_agent_home(tmp_path/'home'))
    calls = []
    unsafe = replace(decision(), body=BODY+'\n执行 rm -rf / 清理全部文件。')
    with pytest.raises(SkillLearningGateError):
        promote_verified_skill(store, PublishRequest(unsafe), lambda *args: calls.append(args))
    assert not calls
    assert not store.load_registry().skills


# LLM: 已待审的同一候选在获得匹配 hash 的验证回执后可转入发布，不能被待审身份永久挡住。
# 函数用途: 回归待审学习候选向机器验证发布的状态转换。
def test_f10_pending_candidate_can_become_machine_verified(tmp_path):
    from agent_py_agent.agent.capability.asset_promotion import propose_or_promote_skill

    store = SkillLearningStore.for_home(ensure_my_agent_home(tmp_path/'home'))
    request = PublishRequest(decision())
    propose_or_promote_skill(store, request, autonomous_verified=False)
    result = propose_or_promote_skill(store, request, verifier=lambda _,sha: {
        'sha256':sha,'status':'passed','executed':True,'contract_valid':True,'output':{},'receipt_id':'r'})
    assert result.event == 'published'


# LLM: 步骤 ID 规范化不能误改 inputs_map 内同名的字面量，否则不同业务输入会得到同一语义指纹。
# 函数用途: 检查工作流族指纹保留字面输入值的差异。
def test_f10_workflow_literal_inputs_are_not_rewritten_as_step_ids():
    from agent_py_agent.agent.capability.asset_promotion import task_family_fingerprint

    a = {'steps':[{'step_id':'a','inputs_map':{'literal':'a'}}]}
    b = {'steps':[{'step_id':'b','inputs_map':{'literal':'b'}}]}
    assert task_family_fingerprint('workflow', a) != task_family_fingerprint('workflow', b)


# LLM: 机器验证通过也不能绕过修订和 hash 的 CAS；同一旧请求第二次提交必须拒绝且不改当前正文。
# 函数用途: 回归自主更新发布的成功路径与过期版本冲突保护。
def test_f06_machine_verified_skill_update_honors_cas(learned):
    from agent_py_agent.agent.capability.asset_promotion import promote_verified_skill
    from agent_py_agent.agent.capability.skill_learning_publish import SkillLearningGateError

    _, store = learned
    row = store.load_registry().skills['csv-cleaner']
    update = PublishRequest(replace(decision(), decision='update', body=BODY+'\n复核列类型。'),
                            expected_revision=row.revision, expected_sha256=row.sha256)
    # LLM: 合成成功回执只绑定本次候选 hash，使测试聚焦后续 CAS 门禁，而非候选执行链。
    # 函数用途: 为版本冲突回归提供摘要匹配的验证器替身。
    def verifier(_, sha):
        return {'status':'passed', 'executed':True, 'contract_valid':True,
                'sha256':sha, 'receipt_id':'cas-check', 'output':{}}
    assert promote_verified_skill(store, update, verifier).event == 'updated'
    current = store.skill_path(row.name).read_bytes()
    with pytest.raises(SkillLearningGateError, match='REVISION_CONFLICT'):
        promote_verified_skill(store, update, verifier)
    assert store.skill_path(row.name).read_bytes() == current


# LLM: 在冻结后、执行前把 Store 改成新版，Runner 输出仍必须来自已冻结的旧代码，不能二次读库。
# 函数用途: 检查脚本执行使用冻结正文而不受随后存储修改影响。
def test_f14_runner_executes_frozen_code_not_later_store_edit(tmp_path, monkeypatch):
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore
    from agent_py_agent.agent.workflows import script_runner

    home = tmp_path/'owner'
    store = ScriptStore(home)
    descriptor = AssetDescriptor(id='tail', kind='script', name='tail')
    store.save_script(descriptor, 'print(\'{"version":1}\')')
    execute = script_runner._execute
    # LLM: 只在进入真实执行函数前改写 Store，原调用参数原样传递，确保故障点位于冻结之后。
    # 函数用途: 注入冻结代码与当前存储版本不同的执行时序。
    def change_after_freeze(*args):
        store.save_script(descriptor, 'print(\'{"version":2}\')')
        return execute(*args)
    monkeypatch.setattr(script_runner, '_execute', change_after_freeze)
    result = script_runner.ScriptRunner(home, use_sandbox=False).run_script('tail')
    assert result.ok and result.output_data == {'version':1}


# LLM: 脚本即使正常输出 JSON，缺少声明的 report_uri 也必须按输出合同失败，不能只看进程成功。
# 函数用途: 验证 Runner 最终返回值必须满足必填输出字段合同。
def test_f15_runner_final_output_must_pass_contract(tmp_path):
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor, IOContract
    from agent_py_agent.agent.assets.script_store import ScriptStore
    from agent_py_agent.agent.workflows import ScriptRunner

    home = tmp_path/'owner'
    ScriptStore(home).save_script(AssetDescriptor(id='contract', kind='script', name='contract',
        io_contract=IOContract(outputs=('report_uri',),output_types={'report_uri':'string'})), 'print("{}")')
    result = ScriptRunner(home, use_sandbox=False).run_script('contract')
    assert not result.ok and result.error_code == 'SCRIPT_MISSING_OUTPUT_KEYS'


# LLM: e905收敛family-only复用，不把B的代码/证明混进A；精确ID各自保留原hash和状态。
# 函数用途: 检查同族不同规范ID的出处和版本不会串账。
def test_review_same_family_provenance_keeps_exact_identity(tmp_path):
    from agent_py_agent.agent.capability.asset_promotion import register_asset

    store = SkillLearningStore.for_home(ensure_my_agent_home(tmp_path/'home'))
    register_asset(store,kind='script',asset_id='a',asset={'code':'print(1)'},source={'origin':'a'},sha256='a'*64,state='active')
    row = register_asset(store,kind='script',asset_id='b',asset={'code':'print( 1 )'},source={'origin':'b'},sha256='b'*64)
    assert (row['asset_id'],row['sha256'],row['state']) == ('b','b'*64,'candidate')
    rows = {item['asset_id']: item for item in store.load_registry().assets.values()}
    assert (rows['a']['sha256'], rows['a']['state']) == ('a'*64, 'active')
    assert rows['a']['provenance'] == [{'origin': 'a'}]
    assert row['provenance'] == [{'origin': 'b'}]


# LLM: 当前回执须保留真实执行类型，不能把已执行和纯静态trial混为同一质量事实。
# 函数用途: 核对更新发布的准确hash、receipt和执行级别。
def test_review_verified_update_persists_current_receipt(learned):
    from agent_py_agent.agent.capability.asset_promotion import promote_verified_skill

    _,store = learned
    current = store.load_registry().skills['csv-cleaner']
    request = PublishRequest(replace(decision(),decision='update',body=BODY+'\n核对缺失字段。'),
                             expected_revision=current.revision,expected_sha256=current.sha256)
    promote_verified_skill(store,request,lambda _,sha: {'sha256':sha,'status':'passed','executed':True,
        'contract_valid':True,'output':{},'receipt_id':'new-receipt'})
    row = store.load_registry().skills[current.name]
    assert row.maintenance['verification_receipt'] == {'sha256':row.sha256,'receipt_id':'new-receipt',
        'carrier': 'skill', 'verification_kind': 'runtime_execution', 'checks_executed': True, 'executed': True}
    assert row.maintenance['promotion_authority'] == 'autonomous_verified'


# LLM: 在真实登记写入后注入异常，提交必须恢复提案 DRAFT、清空本次资产登记并撤销技能及版本文件。
# 函数用途: 回归对话提取提交在登记阶段失败时的跨存储回滚。
def test_review_extraction_registry_write_failure_restores_all(tmp_path,monkeypatch):
    from agent_py_agent.agent.assets import turn_extractor as extract
    from agent_py_agent.tests.test_turn_asset_extractor import history

    home,store,thread,_,first,*_ = history.__wrapped__(tmp_path)
    proposal = extract.propose_assets_from_turns(store,owner_home=home,
        turn_refs=[{'thread_id':thread.thread_id,'message_id':first.message_id}],target_types=['skill'])
    original = SkillLearningStore.save_registry
    # LLM: 先执行原始 save_registry 再报错，确保覆盖已替换登记文件后仍需恢复的事务窗口。
    # 函数用途: 注入提取提交的写后登记失败。
    def fail(self, registry):
        original(self,registry)
        raise OSError('injected after registry replace')
    monkeypatch.setattr(SkillLearningStore,'save_registry',fail)
    with pytest.raises(OSError):
        extract.commit_extracted_assets(home,proposal_id=proposal['proposal_id'])
    assert extract.get_extraction_proposal(home,proposal['proposal_id'])['status'] == 'DRAFT'
    registry = SkillLearningStore(home/'data'/'skill_learning',home/'skills').load_registry()
    assert not registry.skills and not registry.assets
    assert not list((home/'data'/'skill_learning'/'versions').rglob('*.md'))
    assert not list((home/'skills').rglob('SKILL.md'))


# LLM: 新规范ID必须走自身静态/沙箱/安全/配额门，不借旧名字的正式版本证明。
# 函数用途: 回归改名不会拿到旧Skill身份，两个名字的发布事实分开保存。
def test_review_formal_method_rename_requires_own_publication(learned):
    from agent_py_agent.agent.capability.asset_promotion import propose_or_promote_skill

    _,store = learned
    result = propose_or_promote_skill(store,PublishRequest(decision('renamed')),
        verifier=lambda _,sha: {'sha256':sha,'status':'passed','executed':True,'contract_valid':True,'output':{},'receipt_id':'r'})
    assert result.event == 'published' and result.skill_name == 'renamed'
    assert set(store.load_registry().skills) == {'csv-cleaner', 'renamed'}


# LLM: 两个包的查询匹配内容相同，当前 project_id 适用的包必须在实际索引中排到无关包之前。
# 函数用途: 验证能力包应用范围参与元数据推荐排序。
def test_f04_application_scope_prioritizes_relevant_package():
    from agent_py_agent.agent.capability.router import CapabilityCard, CapabilityRouter

    cards = [CapabilityCard('capability:a-other','capability_package','a-other','查询',metadata={'package_id':'a-other','stable_id':'capability:a-other'}),
             CapabilityCard('capability:z-current','capability_package','z-current','查询',
                            metadata={'package_id':'z-current','stable_id':'capability:z-current','application_scope':{'project_id':'project-a'}})]
    router = CapabilityRouter(extra_cards=cards)
    agent = SimpleNamespace(_current_run_params=SimpleNamespace(user_prompt='查询',task_attributes={'project_id':'project-a'}))
    # LLM: 绑定方法的所属对象提供当前任务参数，回调不制造额外技能快照，排序只依赖显式卡片和任务范围。
    # 类用途: 为路由范围排序测试承载当前运行参数及快照提供方法。
    class Provider:
        _current_run_params = agent._current_run_params
        # LLM: 返回 None 让路由保留额外卡片；任务上下文通过绑定对象属性传递，不从快照结果猜测。
        # 函数用途: 提供不含技能快照的绑定回调以测试应用范围排序。
        def current(self):
            return None
    router._skill_snapshot_provider = Provider().current
    result = router.render_skill_metadata_index(context_window_tokens=128000)
    assert result.index('z-current') < result.index('a-other')


# LLM: workbench、domain 和标签各维度都须满足交集，别名可以补充同维度取值，不能只匹配其中一维。
# 函数用途: 检查能力包应用范围的多维交集和任务属性别名。
@pytest.mark.parametrize('attrs,applies', [
    ({'workbench_id':'ops', 'domain':'alerts', 'tags':['prod']}, True),
    ({'workbench':'ops', 'domains':['alerts'], 'workspace_tags':['prod']}, True),
    ({'workbenches':['ops'], 'domain':'other', 'tags':['prod']}, False),
    ({'workbench':'ops', 'domain':'alerts'}, False),
    ({'workbench':'other', 'workbenches':['ops'], 'domains':['alerts'], 'tags':['prod']}, True),
])
def test_r2_application_scope_intersects_each_dimension(attrs, applies):
    from agent_py_agent.agent.capability.router import CapabilityCard, _package_priority

    card = CapabilityCard('capability:scoped', 'capability_package', 'scoped', '查询', metadata={
        'application_scope':{'workbenches':['ops'], 'domains':['alerts'], 'workspace_tags':['prod']}})
    assert _package_priority(card, '', attrs)[1] is not applies


# LLM: 任一保护来源异常都应标记采集不完整，但不能丢掉对话、定时任务、提案和子代理中其他来源的引用。
# 函数用途: 回归保护引用收集的来源级故障隔离及不完整标记。
@pytest.mark.parametrize('broken', ['conversations', 'scheduler', 'proposals', 'subagents'])
def test_r2_protection_source_failure_preserves_other_references(learned, monkeypatch, broken):
    from agent_py_agent.agent.capability import skill_learning_proposals as proposals
    from agent_py_agent.agent.capability.skill_learning_protection import (
        learning_protection_for_agent,
    )

    home, _ = learned
    # LLM: 同一异常替身只替换参数指定的一个来源，其他来源保持返回有效引用，便于检验故障隔离。
    # 函数用途: 模拟一个保护信息来源不可用。
    def fail(*args, **kwargs):
        raise RuntimeError('one source unavailable')
    task = SimpleNamespace(status='running', to_dict=lambda: {'allowed_skills':['owner:task']})
    agent = SimpleNamespace(home_paths=home, conversation_store=SimpleNamespace(
        threads=SimpleNamespace(list_report=fail if broken == 'conversations' else lambda **kw: ([SimpleNamespace(thread_id='t')], [])),
        tasks=SimpleNamespace(active_report=lambda thread: ([task], []))),
        scheduler_repository=SimpleNamespace(list_jobs=fail if broken == 'scheduler' else lambda **kw: ([{'allowed_skills':['owner:job']}], [])),
        subagents=SimpleNamespace(list_runs_report=fail if broken == 'subagents' else lambda: SimpleNamespace(
            load_errors=[], runs=[SimpleNamespace(status='running', attributes={'allowed_skills':['owner:child']})])))
    monkeypatch.setattr(proposals, 'list_learning_proposals', fail if broken == 'proposals' else lambda *a, **kw: [
        {'status':'pending', 'expected_sha256':'a'*64, 'decision':{'name':'proposal'}}])
    result = learning_protection_for_agent(agent)
    assert not result['protection_complete']
    expected = {'conversations':'owner:task', 'scheduler':'owner:job', 'proposals':'owner:proposal', 'subagents':'owner:child'}
    assert set(expected.values()) - {expected[broken]} <= set(result['protected_ids'])


# LLM: 三个独立任务的采用及宿主有效反馈使试用转为 active，晋升历史应追加一次而非重写或重复追加。
# 函数用途: 检查试用技能激活后的状态和完整晋升轨迹。
def test_r2_trial_to_active_records_one_promotion_transition(learned):
    _, store = learned
    current = store.load_registry().skills['csv-cleaner']
    for task in ('one', 'two', 'three'):
        observe_learning(store, current.name, sha256=current.sha256, task_id=task, outcome='adopted')
        observe_learning(store, current.name, sha256=current.sha256, task_id=task, outcome='effective',
                         source=LearningObservationSource(authority='host', receipt_id=task))
    row = store.load_registry().skills[current.name]
    assert row.maintenance['state'] == 'active'
    assert row.maintenance['promotion_history'] == ['observed','candidate','validated','trial','active']


# LLM: 默认 Runtime 必须有宿主验证器；请求内自报的执行成功回执不可信，发布后应记录独立静态验证事实。
# 函数用途: 验证默认机器验证接线及模型自报回执不能替代宿主证据。
def test_r2_default_runtime_has_machine_verifier_and_ignores_request_claims(tmp_path):
    from dataclasses import asdict

    from agent_py_agent.agent.capability.skill_learning import (
        SkillLearningRuntime,
        SkillLearningService,
        SkillLearningSettings,
    )
    from agent_py_agent.agent.capability.skill_learning_prompt import (
        OUTPUT_SCHEMA_VERSION,
        SkillLearningMaterial,
    )

    runtime = SkillLearningRuntime(backend=None, snapshot_provider=lambda: snapshot())
    assert callable(runtime.promotion_verifier)
    store = SkillLearningStore.for_home(ensure_my_agent_home(tmp_path/'home'))
    service = SkillLearningService(store, SkillLearningSettings(autonomous_verified=True), runtime)
    material = SkillLearningMaterial({'run_id':'r', 'request_key':'r', 'verification_receipt':{
        'status':'passed', 'executed':True, 'contract_valid':True, 'receipt_id':'model-claim', 'output':{}}})
    event = service._decide(material, json.dumps({'schema_version':OUTPUT_SCHEMA_VERSION, **asdict(decision())}))
    assert event.event == 'published'
    receipt = store.load_registry().skills['csv-cleaner'].maintenance['verification_receipt']
    assert receipt['receipt_id'] != 'model-claim'
    assert receipt['verification_kind'] == 'deterministic_static' and receipt['executed'] is False


# LLM: 合成宿主回执在发布中断后可由默认验证器复用，但候选 hash、请求身份字段或 Owner 改变都必须失效。
# 函数用途: 检查晋升重试复用已保存回执的准确绑定边界。
def test_r2_default_verifier_reuses_host_receipt_after_publish_interruption(tmp_path, monkeypatch):
    from dataclasses import asdict

    from agent_py_agent.agent.capability import skill_learning_publish as publisher
    from agent_py_agent.agent.capability.asset_promotion import promote_verified_skill
    from agent_py_agent.agent.capability.skill_learning import (
        SkillLearningRuntime,
        SkillLearningService,
        SkillLearningSettings,
    )
    from agent_py_agent.agent.capability.skill_learning_prompt import (
        OUTPUT_SCHEMA_VERSION,
        SkillLearningMaterial,
    )

    store = SkillLearningStore.for_home(ensure_my_agent_home(tmp_path/'home'))
    request = {'run_id':'verified-run', 'request_key':'original-request', 'trajectory_fingerprint':'original-input'}
    publish = PublishRequest(decision(), request)
    original = publisher.publish_learned_skill
    # LLM: 只替换发布调用，保留前面的验证与回执保存流程，以构造已验证但尚未发布的重试场景。
    # 函数用途: 在取得验证回执后注入发布中断。
    def interrupted(*args):
        raise OSError('publish interrupted after host verification')
    monkeypatch.setattr(publisher, 'publish_learned_skill', interrupted)
    with pytest.raises(OSError):
        promote_verified_skill(store, publish, lambda _, sha: {
            'status':'passed', 'executed':True, 'contract_valid':True, 'sha256':sha,
            'receipt_id':'host-execution-1', 'output':{}})
    monkeypatch.setattr(publisher, 'publish_learned_skill', original)
    from agent_py_agent.agent.capability.skill_learning_host_feedback import (
        builtin_machine_verifier,
    )
    from agent_py_agent.agent.capability.skill_learning_publish import render_learned_skill
    from agent_py_agent.agent.capability.skill_snapshot import skill_content_sha256

    sha = skill_content_sha256(render_learned_skill(publish.decision))
    for field in ('run_id', 'request_key', 'trajectory_fingerprint'):
        assert builtin_machine_verifier(replace(publish, request={**request, field:'changed'}), sha, store=store) is None
    assert builtin_machine_verifier(publish, 'a'*64, store=store) is None
    other = SkillLearningStore.for_home(ensure_my_agent_home(tmp_path/'other'))
    with other.state_lock():
        other.save_registry(store.load_registry())
    assert builtin_machine_verifier(publish, sha, store=other) is None
    runtime = SkillLearningRuntime(backend=None, snapshot_provider=lambda: snapshot())
    service = SkillLearningService(store, SkillLearningSettings(autonomous_verified=True), runtime)
    result = service._decide(SkillLearningMaterial(request), json.dumps({'schema_version':OUTPUT_SCHEMA_VERSION, **asdict(decision())}))
    assert result.event == 'published'
    row = store.load_registry().skills['csv-cleaner']
    assert row.maintenance['verification_receipt']['receipt_id'] == 'host-execution-1'
    assert row.maintenance['promotion_authority'] == 'autonomous_verified'


# LLM: 对同一候选重新验证时，返回失败或抛异常都必须撤销旧成功回执，不能继续借旧证明晋升。
# 函数用途: 回归再次验证失败后已缓存机器回执的失效处理。
@pytest.mark.parametrize('throws', [False, True])
def test_r2_failed_reverification_invalidates_previous_machine_receipt(tmp_path, monkeypatch, throws):
    from agent_py_agent.agent.capability import skill_learning_publish as publisher
    from agent_py_agent.agent.capability.asset_promotion import promote_verified_skill
    from agent_py_agent.agent.capability.skill_learning_host_feedback import (
        builtin_machine_verifier,
    )
    from agent_py_agent.agent.capability.skill_learning_publish import render_learned_skill
    from agent_py_agent.agent.capability.skill_snapshot import skill_content_sha256

    store = SkillLearningStore.for_home(ensure_my_agent_home(tmp_path/'home'))
    publish = PublishRequest(decision(), {'run_id':'r', 'request_key':'q', 'trajectory_fingerprint':'f'})
    # LLM: 发布失败前的合成成功回执应真实保存，后续用它确认再次验证确实清除了已有成功证据。
    # 函数用途: 阻止初次发布完成以留下可供失效测试的旧回执。
    def interrupted(*args):
        raise OSError('interrupted publication')
    monkeypatch.setattr(publisher, 'publish_learned_skill', interrupted)
    with pytest.raises(OSError):
        promote_verified_skill(store, publish, lambda _, sha: {
            'status':'passed', 'executed':True, 'contract_valid':True, 'sha256':sha,
            'receipt_id':'old-pass', 'output':{}})
    sha = skill_content_sha256(render_learned_skill(publish.decision))
    assert builtin_machine_verifier(publish, sha, store=store) is not None
    # LLM: 分别覆盖验证器抛异常和正常返回 failed，两种失败都不能保留先前的成功回执。
    # 函数用途: 为再次验证回归生成参数化失败结果。
    def failed(*args):
        if throws:
            raise RuntimeError('verifier failed')
        return {'status':'failed'}
    if throws:
        with pytest.raises(RuntimeError, match='verifier failed'):
            promote_verified_skill(store, publish, failed)
    else:
        assert promote_verified_skill(store, publish, failed) is None
    assert builtin_machine_verifier(publish, sha, store=store) is None


# LLM: 只给明确目标还不够，所有目标别名都必须配当前内容 hash；缺失或错误摘要不能记入当前版本。
# 函数用途: 验证显式目标回执对准确版本摘要的强制要求。
@pytest.mark.parametrize('field', ['skill_id', 'skill_name', 'target_skill_id'])
@pytest.mark.parametrize('hash_value,expected', [(None, False), ('wrong', False), ('current', True)])
def test_r3_f11_explicit_target_requires_exact_hash(learned, field, hash_value, expected):
    from agent_py_agent.agent.capability.skill_learning_host_feedback import _receipt_targets

    _, store = learned
    row = store.load_registry().skills['csv-cleaner']
    evidence = {field:'owner:csv-cleaner'}
    if hash_value is not None:
        evidence['skill_content_hash'] = row.sha256 if hash_value == 'current' else 'a'*64
    assert _receipt_targets(evidence, row) is expected


# LLM: 同一 run 对同一技能采用过多个 hash 时，无目标回执也存在版本歧义，不能推定验证了当前版本。
# 函数用途: 检查跨版本采用历史阻止无归属命令回执记功。
def test_r3_f11_multiple_adopted_versions_are_ambiguous(learned):
    home, store = learned
    row = store.load_registry().skills['csv-cleaner']
    observe_learning(store, row.name, sha256=row.sha256, task_id='t', outcome='adopted',
                     source=LearningObservationSource(run_id='r'))
    with store.state_lock():
        registry = store.load_registry()
        current = registry.skills[row.name]
        observations = current.maintenance['observations']
        data = {**current.maintenance, 'observations':[{**observations[0], 'sha256':'a'*64}, *observations]}
        store.save_registry(replace(registry, skills={row.name:replace(current, maintenance=data)}))
    params = SimpleNamespace(run_id='r', attempt_id='a')
    agent = SimpleNamespace(config=SimpleNamespace(enable_self_learning=True), home_paths=home, _current_run_params=params)
    observe_command_verification(agent, params, {'id':42, 'status':'passed', 'exit_code':0})
    assert not any(f['authority']=='host' for f in store.load_registry().skills[row.name].maintenance['observations'])


# LLM: 连续试用更新必须一直保存最初稳定基线，最后试用版退化时不能回退到中间试用版。
# 函数用途: 验证多次试用更新后的负反馈回退恢复原稳定正文和版本。
def test_r3_f12_repeated_trial_updates_restore_original_stable(learned):
    _, store = learned
    stable = store.load_registry().skills['csv-cleaner']
    with store.state_lock():
        registry = store.load_registry()
        store.save_registry(replace(registry, skills={stable.name:replace(stable, maintenance={**stable.maintenance, 'state':'active'})}))
    for suffix in ('second', 'third'):
        current = store.load_registry().skills[stable.name]
        draft = replace(decision(), decision='update', body=BODY+'\n'+suffix)
        publish_learned_skill(store, PublishRequest(draft, expected_revision=current.revision, expected_sha256=current.sha256))
    trial = store.load_registry().skills[stable.name]
    assert trial.maintenance['previous_stable']['sha256'] == stable.sha256
    for task in ('regression-a', 'regression-b'):
        observe_learning(store, stable.name, sha256=trial.sha256, task_id=task, outcome='adopted')
        observe_learning(store, stable.name, sha256=trial.sha256, task_id=task, outcome='ineffective',
                         source=LearningObservationSource(authority='host', receipt_id=task))
    restored = store.load_registry().skills[stable.name]
    assert restored.sha256 == stable.sha256 and restored.version == stable.version
    assert restored.revision > trial.revision


# LLM: 任务、轨迹摘要或工具轨迹改变都应改变请求身份；无 run 的旧记录也须保留任务差异，相同重放则去重。
# 函数用途: 检查请求主身份包含任务与轨迹信息且重复入队保持幂等。
@pytest.mark.parametrize('difference', ['task', 'digest', 'trace', 'legacy'])
def test_r3_f08_primary_identity_retains_task_and_trajectory(learned, difference):
    _, store = learned
    left, right = context('same'), context('same')
    if difference == 'task':
        right.task_id = 'another-task'
    elif difference == 'digest':
        left.trajectory_digest, right.trajectory_digest = 'first', 'second'
    elif difference == 'trace':
        right.archive_tool_calls = [{'tool':'read_file', 'ok':True, 'parameters':{'path':'other'}}]
    else:
        left.run_id = right.run_id = ''
        right.task_id = 'another-legacy-task'
    first, second = build_learning_request(left, 1), build_learning_request(right, 1)
    assert first['request_key'] != second['request_key']
    store.enqueue(first)
    store.enqueue(second)
    assert len(store.pending_requests(refill=True)) == 2
    store.enqueue(build_learning_request(right, 1))
    assert len(store.pending_requests(refill=True)) == 2


# LLM: 作用域取值应统一别名、大小写及集合表示，通配符可免该维约束，但缺少非通配属性时仍须不匹配。
# 函数用途: 回归包应用范围对别名、大小写、集合和通配符的匹配语义。
@pytest.mark.parametrize('scope,attrs,expected', [
    ({'workbenches':['PenTest']}, {'workbench_id':'pentest'}, True),
    ({'workspace_tags':{'Prod'}, 'channels':['IM']}, {'workspace_tag':'prod', 'channel':['im']}, True),
    ({'domains':['*'], 'channels':['all']}, {}, True),
    ({'channels':['im']}, {'channel':'tui'}, False),
    ({'workbenches':['pentest']}, {}, False),
])
def test_r3_b3_scope_aliases_case_sets_and_wildcards(scope, attrs, expected):
    from agent_py_agent.agent.capability.router import _scope_matches

    assert _scope_matches(scope, attrs) is expected


# LLM: application_scope 位于包 metadata 内，即使 to_ref 只给包 ID，卡片投影也不能丢掉渠道范围。
# 函数用途: 检查能力包应用范围在路由卡片投影中完整保留。
def test_r3_b3_package_metadata_scope_survives_projection():
    from agent_py_agent.agent.capability.router import from_capability_package

    package = SimpleNamespace(stable_id='capability:p', package_id='p', description='summary', keywords=[],
        summary='summary', source='test', version='1', metadata={'application_scope':{'channels':['im']}},
        to_ref=lambda: {'package_id':'p'})
    assert from_capability_package(package).metadata['application_scope'] == package.metadata['application_scope']


# LLM: 刚写入登记文件的热 mtime 不能遮蔽陈旧维护时钟；最近维护过的来源才不应再次被发现。
# 函数用途: 回归学习来源发现依据持久化维护时间判断是否到期。
@pytest.mark.parametrize('clock,expected', [(0, 1), (1, 1), ('recent', 0)])
def test_r3_f17_discovery_uses_maintenance_clock_not_hot_mtime(learned, clock, expected):
    import time

    from agent_py_agent.tests.test_gateway_learning_discovery import learning_sources

    home, store = learned
    with store.state_lock():
        store.save_registry(replace(store.load_registry(), last_maintenance_at=time.time() if clock == 'recent' else clock))
    refs = learning_sources(SimpleNamespace(home=home, store=store))
    assert len(refs) == expected


# LLM: 分页要同时给出准确 total 和 total_count；内容 hash 不变但激活代次变化时旧游标仍必须失效。
# 函数用途: 检查搜索总数别名及能力重新激活后的游标拒绝。
def test_r3_f04_reactivation_rejects_old_cursor_and_counts_total():
    from agent_py_agent.agent.capability.skill_search_tool import _search_page

    hits = [SimpleNamespace(card=SimpleNamespace(id=str(i), metadata={'content_sha256':'a'*64, 'activation_id':'old'}), score=1) for i in range(2)]
    _, page = _search_page(hits, {}, 'query', '', 1)
    assert page.get('total_count') == page['total'] == 2
    hits[0].card.metadata['activation_id'] = 'new'
    with pytest.raises(ValueError, match='CURSOR_STALE'):
        _search_page(hits, {'cursor':page['next_cursor']}, 'query', '', 1)


# LLM: 选中脚本或工作流须保留资产引用，并将对应执行工具放入当前可见短名单，不能仍把执行入口延迟隐藏。
# 函数用途: 验证能力决策投影同时保留所选资产身份与必要执行工具。
@pytest.mark.parametrize('kind,tool', [('script','scripts'), ('workflow','workflows')])
def test_r3_f01_selected_asset_keeps_execution_tool_and_reference(kind, tool):
    from agent_py_agent.agent.capability.decision_recommendation import _project
    from agent_py_agent.agent.tooling.models import (
        ToolModelHints,
        ToolModelSpec,
        ToolRuntimeSnapshot,
    )

    runtimes = tuple(SimpleNamespace(model_spec=ToolModelSpec(name, name, {'type':'object'}, hints=ToolModelHints(category='capability')),
        exposure=SimpleNamespace(model_visible=True), availability=SimpleNamespace(available=True),
        handler=SimpleNamespace(), runtime_policy=None) for name in ('scripts','workflows','tool_search'))
    tools = ToolRuntimeSnapshot('r', runtimes, frozenset(('scripts','workflows','tool_search')), (), None)
    params = SimpleNamespace(allowed_tools=None, task_attributes={}, context_scope='default')
    row = {'kind':kind, 'ref':kind+':owner:asset', 'tools_required':[tool]}
    result = _project(params, tools, None, snapshot(), [row], {'optional_categories':['capability'], 'context_policy':'progressive'}, True)
    assert tool in result.tool_snapshot.presentation_shortlist_names
    assert tool not in result.tool_snapshot.presentation_deferred_names
    assert row['ref'] in result.selected_skill_ids


# LLM: 默认晋升可完成确定性静态检查并发布试用技能，但 checks_executed 不等于资产业务执行成功。
# 函数用途: 核对默认发布回执记录静态验证级别且不被判为业务验证。
def test_r3_f07_default_production_promotion_is_static_not_business_execution(tmp_path):
    from agent_py_agent.agent.capability import asset_promotion

    store = SkillLearningStore.for_home(ensure_my_agent_home(tmp_path/'home'))
    event = asset_promotion.promote_verified_skill(store, PublishRequest(decision()), None)
    assert event is not None and event.event == 'published'
    assert event.carrier == 'skill'
    receipt = store.load_registry().skills['csv-cleaner'].maintenance['verification_receipt']
    assert receipt['verification_kind'] == 'deterministic_static'
    assert receipt['checks_executed'] is True
    assert not asset_promotion.business_validated(receipt)


# LLM: 默认验证器应区分合法脚本、语法错误和空工作流等草稿结构；这些静态结果不证明业务执行。
# 函数用途: 检查脚本与工作流候选的默认语法及结构验证结果。
@pytest.mark.parametrize('kind,body,passed', [('script','print("ok")',True), ('script','def broken(',False),
    ('workflow',[{'step_id':'a','mode':'deterministic_script','script_id':'s'}],True), ('workflow',[],False)])
def test_r3_f07_default_machine_verifier_checks_draft_syntax(kind, body, passed):
    from agent_py_agent.agent.capability import asset_promotion

    verifier = getattr(asset_promotion, 'default_machine_promotion_verifier', None)
    assert callable(verifier)
    draft = {'kind':kind, 'name':'valid-name', 'description':'reusable procedure', 'code':body if kind=='script' else '',
             'steps':body if kind=='workflow' else []}
    assert (verifier(draft)['status'] == 'passed') is passed


# LLM: 人格文件目标、路径逃逸、空正文及疑似凭据内容都不得通过默认草稿门，其他有效字段不能抵消拒绝原因。
# 函数用途: 回归默认技能验证器对越界目标和不安全或空内容的拒绝。
@pytest.mark.parametrize('extra', [{'target':'agents'}, {'target':'SOUL.md'}, {'name':'../escape'}, {'body':''},
                                  {'body':'Authorization: Bearer '+'x'*50}])
def test_r3_f07_default_verifier_rejects_unsafe_or_empty_drafts(extra):
    from agent_py_agent.agent.capability import asset_promotion

    verifier = getattr(asset_promotion, 'default_machine_promotion_verifier', None)
    assert callable(verifier)
    assert verifier({'kind':'skill', 'name':'valid-name', 'description':'procedure', 'body':BODY, **extra})['status'] != 'passed'


# LLM: 客户端只提交旧 cursor 而不额外提交激活字段时，游标自身仍须绑定旧代次并拒绝新激活目录。
# 函数用途: 验证仅凭分页游标也能检测能力激活代次变化。
def test_r3_f04_cursor_alone_binds_activation_generation():
    from agent_py_agent.agent.capability.skill_search_tool import _search_page

    hits = [SimpleNamespace(card=SimpleNamespace(id=str(i), metadata={'content_sha256':'a'*64, 'activation_id':'old'}), score=1) for i in range(2)]
    _, page = _search_page(hits, {}, 'query', '', 1)
    hits[0].card.metadata['activation_id'] = 'new'
    with pytest.raises(ValueError, match='CURSOR_STALE'):
        _search_page(hits, {'cursor':page['next_cursor']}, 'query', '', 1)


# LLM: 此处只断言显式 capability_package 载体可表示，不据此外推包构建、安装或发布链已经通过。
# 函数用途: 检查载体选择器保留显式能力包类型。
def test_r3_f07_package_carrier_is_representable():
    from agent_py_agent.agent.capability.asset_promotion import select_asset_carrier

    assert select_asset_carrier({'kind':'capability_package'}) == 'capability_package'


# LLM: 在真实版本写入完成后、登记表写入前注入IO失败；验证回滚及不同正文重试，不模拟版本Store。
# 函数用途: 新建与更新失败不得留下阻塞后续发布的未提交版本。
@pytest.mark.parametrize('operation', ['create', 'update', 'legacy_update'])
def test_r2_uncommitted_version_removed_and_different_retry_succeeds(tmp_path, monkeypatch, operation):
    from agent_py_agent.agent.capability.skill_learning_publish import SkillLearningGateError

    store = SkillLearningStore.for_home(ensure_my_agent_home(tmp_path / 'home'))
    request = PublishRequest(decision())
    previous = None
    revision = 1
    if operation != 'create':
        publish_learned_skill(store, request)
        previous = store.skill_path(request.decision.name).read_text()
        current = store.load_registry().skills[request.decision.name]
        revision = current.revision + 1
        request = PublishRequest(replace(decision(), decision='update', body=BODY + '\n失败的新内容。'),
                                 expected_revision=current.revision, expected_sha256=current.sha256)
        if operation == 'legacy_update':
            raw = json.loads(store.registry_path.read_text())
            raw['skills'][current.name].pop('revision')
            store.registry_path.write_text(json.dumps(raw))
    before = store.load_registry()
    original = store.save_registry
    visited = []

    # LLM: 只有目标版本真实落盘后才抛错，避免在save_version之前失败而漏测残留。
    # 函数用途: 精确命中版本已写、登记未提交的事务窗口。
    def fail_registry(registry):
        assert store.read_version(request.decision.name, revision) is not None
        visited.append(True)
        raise OSError('registry unavailable after version write')

    monkeypatch.setattr(store, 'save_registry', fail_registry)
    with pytest.raises(SkillLearningGateError, match='INSTALL_FAILED'):
        publish_learned_skill(store, request)
    assert visited == [True]
    assert store.load_registry() == before
    if previous is None:
        assert not store.skill_path(request.decision.name).exists()
    else:
        assert store.skill_path(request.decision.name).read_text() == previous
    assert store.read_version(request.decision.name, revision) is None
    monkeypatch.setattr(store, 'save_registry', original)
    retry = replace(request, decision=replace(request.decision, body=BODY + '\n重试时再次核对排序。'))
    event = publish_learned_skill(store, retry)
    assert event.event == ('published' if operation == 'create' else 'updated')
    assert event.version == revision
    assert store.read_version(event.skill_name, revision) == store.skill_path(event.skill_name).read_text()


# LLM: 两次真实负反馈触发自动回退，在保存登记时失败；随后不同内容更新须可使用同一未提交revision。
# 函数用途: 负反馈回退的版本与正文一起撤回，不留下不可变版本冲突。
@pytest.mark.parametrize('legacy_registry', [False, True])
def test_r2_failed_trial_rollback_cleans_uncommitted_version(learned, monkeypatch, legacy_registry):
    _, store = learned
    stable = store.load_registry().skills['csv-cleaner']
    request = PublishRequest(replace(decision(), decision='update', body=BODY + '\n试用新版。'),
                             expected_revision=stable.revision, expected_sha256=stable.sha256)
    publish_learned_skill(store, request)
    trial = store.load_registry().skills[stable.name]
    for task in ('first', 'second'):
        observe_learning(store, trial.name, sha256=trial.sha256, task_id=task, outcome='adopted')
    observe_learning(store, trial.name, sha256=trial.sha256, task_id='first', outcome='ineffective',
                     source=LearningObservationSource(authority='host', receipt_id='first'))
    if legacy_registry:
        raw = json.loads(store.registry_path.read_text())
        raw['skills'][trial.name].pop('revision')
        store.registry_path.write_text(json.dumps(raw))
    before = store.load_registry()
    previous = store.skill_path(trial.name).read_text()
    original = store.save_registry

    # LLM: 验证确实已写回退版本后抛异常，不跳过真实反馈汇总和回退逻辑。
    # 函数用途: 命中自动回退的登记失败窗口。
    def fail_registry(registry):
        assert store.read_version(trial.name, trial.revision + 1) == store.read_version(stable.name, stable.version)
        raise OSError('trial registry unavailable')

    monkeypatch.setattr(store, 'save_registry', fail_registry)
    with pytest.raises(OSError, match='trial registry unavailable'):
        observe_learning(store, trial.name, sha256=trial.sha256, task_id='second', outcome='ineffective',
                         source=LearningObservationSource(authority='host', receipt_id='second'))
    assert store.load_registry() == before
    assert store.skill_path(trial.name).read_text() == previous
    assert store.read_version(trial.name, trial.revision + 1) is None
    monkeypatch.setattr(store, 'save_registry', original)
    retry = PublishRequest(replace(decision(), decision='update', body=BODY + '\n改进后重新核对。'),
                           expected_revision=trial.revision, expected_sha256=trial.sha256)
    assert publish_learned_skill(store, retry).version == trial.revision + 1


# LLM: 直接调用发布入口绕过上游必填过滤，以真实frontmatter读回证明发布器自己也拒绝空说明。
# 函数用途: 防止发布成功却无法被正式Skill加载器解析的空description。
def test_r2_publish_rejects_empty_description(tmp_path):
    from agent_py_agent.agent.capability.skill_learning_publish import SkillLearningGateError

    store = SkillLearningStore.for_home(ensure_my_agent_home(tmp_path / 'home'))
    with pytest.raises(SkillLearningGateError, match='DRAFT_INVALID'):
        publish_learned_skill(store, PublishRequest(replace(decision(), description='')))
    assert not store.load_registry().skills
    assert not store.skill_path('csv-cleaner').exists()


# LLM: 实际chmod为000，使用真实FD打开与清理；不是模拟权限成功，不跟随外部链接。
# 函数用途: 确认无读执行权限的子目录可回收，根外目录权限与文件保持不变。
def test_r2_stage_zero_mode_directory_is_cleaned(tmp_path):
    import os
    import stat

    from agent_py_agent.agent.capability.asset_promotion import _stage_dir

    external = tmp_path / 'external'
    external.mkdir(mode=0o700)
    (external / 'keep').write_text('keep')
    external.chmod(0o500)
    child = None
    try:
        with _stage_dir(tmp_path) as stage:
            child = stage / 'closed'
            child.mkdir()
            (child / 'payload').write_text('temporary')
            (stage / 'link').symlink_to(external, target_is_directory=True)
            child.chmod(0)
        assert not stage.exists()
        assert not list(stage.parent.glob('.purge_*'))
        assert not list((stage.parent / 'cleanup_pending').glob('*.json'))
        assert (external / 'keep').read_text() == 'keep'
        assert stat.S_IMODE(os.stat(external).st_mode) == 0o500
    finally:
        external.chmod(0o700)
        if child is not None and child.exists():
            child.chmod(0o700)
        # 旧版红测会把000目录隔离到墓碑，测试也要把该准确残留恢复权限供pytest回收。
        for pending in (tmp_path / 'data' / 'skill_learning').glob('.purge_*/closed'):
            pending.chmod(0o700)


# LLM: 只向清理函数注入非OS异常；真实上下文负责登记pending、保留首因并关闭所有自己取得的目录FD。
# 函数用途: 非OS清理失败也不能漏掉欠账或覆盖业务异常。
@pytest.mark.parametrize('business_failure', [False, True])
def test_r2_stage_unexpected_cleanup_error_preserves_fds_and_pending(tmp_path, monkeypatch, business_failure):
    import errno
    import os

    from agent_py_agent.agent.capability import asset_promotion as module
    from agent_py_agent.agent.common import nofollow_fs

    opened = []
    original = nofollow_fs.open_directory_beneath
    primary = ValueError('primary business failure')
    cleanup = RuntimeError('unexpected cleanup failure')

    # LLM: 记录真实打开的父目录FD，不改变打开、权限或清理行为。
    # 函数用途: 在退出后用fstat核对FD确实关闭。
    def track_open(*args, **kwargs):
        descriptor = original(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    # LLM: 注入清理期非OS首因，覆盖原窄except没有登记的分支。
    # 函数用途: 模拟普通运行时清理异常。
    def fail_cleanup(*args):
        raise cleanup

    monkeypatch.setattr(nofollow_fs, 'open_directory_beneath', track_open)
    monkeypatch.setattr(module, '_purge_directory', fail_cleanup)
    expected = primary if business_failure else cleanup
    with pytest.raises(type(expected)) as caught:
        with module._stage_dir(tmp_path, with_fd=True) as (stage, fd):
            opened.append(fd)
            if business_failure:
                raise primary
    assert caught.value is expected
    for descriptor in set(opened):
        with pytest.raises(OSError) as closed:
            os.fstat(descriptor)
        assert closed.value.errno == errno.EBADF
    receipts = list((stage.parent / 'cleanup_pending').glob('*.json'))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text())['stage'] == stage.name


# LLM: 构造真实深目录，只在修权限期间临时收紧Python调用栈；finally恢复限制并自底向上清理测试树。
# 函数用途: 验证修权限不再按目录深度递归；FD峰值与关闭另有真实FD观测测试。
def test_r2_stage_permission_repair_is_iterative(tmp_path):
    import os
    import sys

    from agent_py_agent.agent.capability.asset_promotion import _repair_stage_permissions

    paths = []
    child = tmp_path
    for _ in range(150):
        child = child / 'd'
        child.mkdir()
        paths.append(child)
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    previous_limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(120)
        _repair_stage_permissions(descriptor)
    finally:
        sys.setrecursionlimit(previous_limit)
        os.close(descriptor)
        for path in reversed(paths):
            path.rmdir()


# LLM: 通过真实发布入口检查已有历史与写后被改内容的保留；仅注入失败时点，不绕过版本冲突或nofollow门。
# 函数用途: 回滚只能删本次新写且未变的版本，不能按编号盲删。
@pytest.mark.parametrize('case', ['existing_same', 'existing_other', 'changed_after_write', 'version_write_raises'])
def test_r2_version_cleanup_preserves_foreign_or_existing_bytes(tmp_path, monkeypatch, case):
    from agent_py_agent.agent.capability.skill_learning_publish import (
        SkillLearningGateError,
        render_learned_skill,
    )

    store = SkillLearningStore.for_home(ensure_my_agent_home(tmp_path / 'home'))
    request = PublishRequest(decision())
    text = render_learned_skill(request.decision)
    foreign = text + '\n不是本次事务的内容。'
    if case.startswith('existing_'):
        with store.state_lock():
            store.save_version(request.decision.name, 1, text if case == 'existing_same' else foreign)
    original = store.save_version

    # LLM: 先调用真实save_version，只有目标落盘后才失败。
    # 函数用途: 覆盖版本写入返回前的异常也会撤销本次新版本。
    def fail_version(*args):
        original(*args)
        raise OSError('version write returned late failure')

    # LLM: 不提交registry；可选改动已写版本内容，回滚不得将这份变动后的内容当成本次原文。
    # 函数用途: 覆盖登记失败与未提交叶被改变两个窗口。
    def fail_registry(registry):
        if case == 'changed_after_write':
            (store.versions_dir / request.decision.name / 'v1.md').write_text(foreign)
        raise OSError('registry rejected')

    monkeypatch.setattr(store, 'save_registry', fail_registry)
    if case == 'version_write_raises':
        monkeypatch.setattr(store, 'save_version', fail_version)
    with pytest.raises(SkillLearningGateError, match='INSTALL_FAILED'):
        publish_learned_skill(store, request)
    expected = None if case == 'version_write_raises' else text if case == 'existing_same' else foreign
    assert store.read_version(request.decision.name, 1) == expected
    assert not store.load_registry().skills
    assert not store.skill_path(request.decision.name).exists()


# LLM: 跟踪真实子目录FD同时存活数量，不模拟打开结果；多个平级目录不应全部保留至遍历结束。
# 函数用途: 防止迭代修权限把递归栈问题换成宽目录的FD耗尽问题。
def test_r2_stage_permission_repair_releases_completed_directories(tmp_path, monkeypatch):
    import os

    from agent_py_agent.agent.capability.asset_promotion import _repair_stage_permissions
    from agent_py_agent.agent.common import nofollow_fs

    for index in range(40):
        (tmp_path / f'child-{index}').mkdir()
    original = nofollow_fs._openat_directory
    descriptors = set()
    peak = 0

    # LLM: 每次打开前核验旧FD是否仍有效，避免把系统复用的编号当成泄漏。
    # 函数用途: 观测真实遍历同时持有的子目录数量。
    def track_open(*args):
        nonlocal peak
        for fd in tuple(descriptors):
            try:
                os.fstat(fd)
            except OSError:
                descriptors.remove(fd)
        result = original(*args)
        descriptors.add(result)
        peak = max(peak, len(descriptors))
        return result

    monkeypatch.setattr(nofollow_fs, '_openat_directory', track_open)
    root = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        _repair_stage_permissions(root)
        assert peak <= 2
        for fd in descriptors:
            with pytest.raises(OSError):
                os.fstat(fd)
    finally:
        os.close(root)
