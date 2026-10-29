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


def context(run='a', calls=()):
    return SimpleNamespace(do_save=True, context_scope='default', source='tui', tool_rounds=8,
                           run_id=run, task_id='task-'+run, request_id='request-'+run,
                           task_attributes={'conversation_thread_id': 'same-thread'},
                           user_prompt='合并CSV', final_response=SimpleNamespace(text='完成'),
                           archive_tool_calls=list(calls))


def decision(name='csv-cleaner'):
    return SkillLearningDecision('create', '可复用', name=name, description='合并CSV并核对结果',
                                 when_to_use='合并CSV时', tags=('csv',), body=BODY)


@pytest.fixture
def learned(tmp_path):
    home = ensure_my_agent_home(tmp_path/'home')
    store = SkillLearningStore.for_home(home)
    publish_learned_skill(store, PublishRequest(decision()))
    return home, store


@pytest.mark.parametrize('source', ['def run(value, value):\n    return value\n', 'return 1\n'])
def test_r3_review_static_script_rejects_compile_errors(source):
    from agent_py_agent.agent.capability.asset_promotion import default_machine_promotion_verifier

    result = default_machine_promotion_verifier({'kind':'script', 'name':'compile-probe',
                                               'description':'检查脚本编译错误', 'code':source})
    assert result['status'] == 'failed' and result['executed'] is False


def test_r3_review_script_compilation_does_not_execute():
    from agent_py_agent.agent.capability.asset_promotion import default_machine_promotion_verifier

    result = default_machine_promotion_verifier({'kind':'script', 'name':'no-exec-probe',
                                               'description':'编译不能执行候选',
                                               'code':'raise AssertionError("candidate was executed")'})
    assert result['status'] == 'passed' and result['executed'] is False


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


@pytest.mark.parametrize('kind', ['unhashable', 'falsey'])
def test_r3_review_custom_callable_is_not_implicitly_defaulted(learned, kind):
    from agent_py_agent.agent.capability.asset_promotion import promote_verified_skill

    class Verifier:
        __hash__ = None
        calls = 0

        def __bool__(self):
            return kind != 'falsey'

        def __call__(self, candidate, sha256):
            self.calls += 1
            return {'status':'failed', 'sha256':sha256, 'receipt_id':'rejected'}

    _, store = learned
    verifier = Verifier()
    publish = PublishRequest(decision('callable-probe'), request=build_learning_request(context(), 1))
    assert promote_verified_skill(store, publish, verifier) is None
    assert verifier.calls == 1 and 'callable-probe' not in store.load_registry().skills


def test_f08_distinct_runs_in_same_thread_survive_queue_recovery(learned):
    _, store = learned
    for run in ('csv', 'ssh'):
        assert store.enqueue(build_learning_request(context(run), 1)) == 'queued'
    assert {store.read_request(path)['run_id'] for path in store.pending_requests(refill=True)} == {'csv', 'ssh'}


def test_f08_same_task_different_trajectory_is_not_overwritten(learned):
    _, store = learned
    for run in ('first', 'repaired'):
        request = build_learning_request(context(run), 1)
        request.update(task_id='same-task', trajectory_fingerprint=run)
        store.enqueue(request)
    assert len(store.pending_requests(refill=True)) == 2


@pytest.mark.parametrize('root,task', [('root-a', 'same-task'), ('', '')])
def test_f08_distinct_runs_without_merge_evidence_survive(learned, root, task):
    _, store = learned
    for number, run in enumerate(('first', 'second')):
        request = build_learning_request(context(run), 1)
        request.update(root_task_id=root+str(number) if root else '', task_id=task,
                       trajectory_fingerprint='')
        store.enqueue(request)
    assert len(store.pending_requests(refill=True)) == 2


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


def test_f11_conflicting_explicit_targets_do_not_credit(learned):
    from agent_py_agent.agent.capability.skill_learning_host_feedback import _receipt_targets

    _,store = learned
    row = store.load_registry().skills['csv-cleaner']
    assert not _receipt_targets({'skill_id':'owner:csv-cleaner','asset_ref':'owner:unrelated','sha256':row.sha256},row)


@pytest.mark.parametrize('conflict', [{'asset_id':'owner:unrelated'}, {'content_sha256':'a'*64}])
def test_review_f11_nested_target_aliases_must_all_agree(learned, conflict):
    from agent_py_agent.agent.capability.skill_learning_host_feedback import _receipt_targets

    _, store = learned
    row = store.load_registry().skills['csv-cleaner']
    target = {'stable_id':'owner:csv-cleaner', 'sha256':row.sha256, **conflict}
    assert not _receipt_targets({'target_skill_ids':[target]}, row)


def test_f17_malformed_refs_are_not_a_complete_empty_set():
    from agent_py_agent.agent.capability.skill_learning_protection import _include_refs

    with pytest.raises(ValueError):
        _include_refs(set(), {'skill_snapshot_refs':'broken index'})


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


def test_f02_negative_applicability_is_not_positive_keyword():
    from agent_py_agent.agent.capability.router import CapabilityCard, score_card

    excluded = CapabilityCard('excluded', 'skill', 'log-triage', '处理事件日志；不用于源代码审计')
    suited = CapabilityCard('suited', 'skill', 'code-audit', '源代码审计')
    assert score_card('源代码审计', excluded)[0] < 0
    assert score_card('源代码审计', suited)[0] > 0


def test_f03_package_paraphrase_and_query_negation():
    from agent_py_agent.agent.capability.router import CapabilityCard, has_strong_match

    card = CapabilityCard('capability:security-evidence', 'capability_package', 'security-evidence',
                          '整理漏洞发现、修复报告和证据，生成审计报告', keywords=['证据整理', '审计报告'])
    assert has_strong_match('把已有漏洞发现和修复报告整理成报告', card)
    assert not has_strong_match('别做证据整理，只扫描漏洞', card)


def snapshot(entries=(), workspace='/unused'):
    from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshot

    return SkillSnapshot(tuple(entries), (), 'frozen', 'owner', str(workspace))


def test_f04_owner_exact_fit_is_not_truncated_by_builtin_catalog():
    from agent_py_agent.agent.capability.router import CapabilityRouter
    from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshotEntry

    entries = [SkillSnapshotEntry(f'builtin:base-{i}', f'base-{i}', '无关文档处理', '/unused', 'builtin', 'test', 'a'*64)
               for i in range(200)]
    entries.append(SkillSnapshotEntry('owner:alarm-summary', 'alarm-summary', '汇总今日告警', '/unused', 'owner', 'test', 'b'*64))
    router = CapabilityRouter(skill_snapshot=snapshot(entries))
    assert 'owner:alarm-summary' in router.render_skill_metadata_index(context_window_tokens=128000)


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


def test_f17_empty_refs_and_completed_history_do_not_block_protection(learned, tmp_path):
    from agent_py_agent.agent.capability.skill_learning_protection import (
        learning_protection_for_agent,
    )

    home, _ = learned
    directory = tmp_path/'tasks'
    directory.mkdir()
    for index in range(1002):
        (directory/f'{index:04}.json').write_text('{}')
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


@pytest.mark.parametrize('kind,features', [('script', {'commands':['sort input.csv']}),
                                         ('workflow', {'steps':[{'id':'a'}, {'id':'b', 'depends_on':['a']}]}),
                                         ('skill', {'branches':True})])
def test_f07_carrier_selection_uses_structural_features(kind, features):
    from agent_py_agent.agent.capability import asset_promotion

    assert asset_promotion.select_asset_carrier(features) == kind


def test_f10_semantic_family_ignores_skill_rename():
    from agent_py_agent.agent.capability import asset_promotion

    a, b = decision(), decision('same-method-renamed')
    assert isinstance(asset_promotion.task_family_fingerprint('skill', a.__dict__), str)
    assert asset_promotion.task_family_fingerprint('skill', a.__dict__) == asset_promotion.task_family_fingerprint('skill', b.__dict__)


@pytest.mark.parametrize('output', [{'dry_run':True}, {'executed':False}, {}])
def test_f10_dry_run_is_not_business_validation(output):
    from agent_py_agent.agent.capability import asset_promotion

    assert asset_promotion.business_validated({'status':'succeeded', 'output':output}) is False


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


def test_f16_s1_candidate_has_unified_provenance(tmp_path):
    from agent_py_agent.tests.test_skill_proposals import _record_lesson, _runtime

    ctx = _runtime(tmp_path)
    [proposal] = ctx.service.propose_from_candidates(_record_lesson(ctx))
    rows = SkillLearningStore.for_home(ctx.home).load_registry().assets.values()
    assert any(ref.get('proposal_id') == proposal.proposal_id for row in rows for ref in row['provenance'])


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


def test_f10_pending_candidate_can_become_machine_verified(tmp_path):
    from agent_py_agent.agent.capability.asset_promotion import propose_or_promote_skill

    store = SkillLearningStore.for_home(ensure_my_agent_home(tmp_path/'home'))
    request = PublishRequest(decision())
    propose_or_promote_skill(store, request, autonomous_verified=False)
    result = propose_or_promote_skill(store, request, verifier=lambda _,sha: {
        'sha256':sha,'status':'passed','executed':True,'contract_valid':True,'output':{},'receipt_id':'r'})
    assert result.event == 'published'


def test_f10_workflow_literal_inputs_are_not_rewritten_as_step_ids():
    from agent_py_agent.agent.capability.asset_promotion import task_family_fingerprint

    a = {'steps':[{'step_id':'a','inputs_map':{'literal':'a'}}]}
    b = {'steps':[{'step_id':'b','inputs_map':{'literal':'b'}}]}
    assert task_family_fingerprint('workflow', a) != task_family_fingerprint('workflow', b)


def test_f06_machine_verified_skill_update_honors_cas(learned):
    from agent_py_agent.agent.capability.asset_promotion import promote_verified_skill
    from agent_py_agent.agent.capability.skill_learning_publish import SkillLearningGateError

    _, store = learned
    row = store.load_registry().skills['csv-cleaner']
    update = PublishRequest(replace(decision(), decision='update', body=BODY+'\n复核列类型。'),
                            expected_revision=row.revision, expected_sha256=row.sha256)
    def verifier(_, sha):
        return {'status':'passed', 'executed':True, 'contract_valid':True,
                'sha256':sha, 'receipt_id':'cas-check', 'output':{}}
    assert promote_verified_skill(store, update, verifier).event == 'updated'
    current = store.skill_path(row.name).read_bytes()
    with pytest.raises(SkillLearningGateError, match='REVISION_CONFLICT'):
        promote_verified_skill(store, update, verifier)
    assert store.skill_path(row.name).read_bytes() == current


def test_f14_runner_executes_frozen_code_not_later_store_edit(tmp_path, monkeypatch):
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore
    from agent_py_agent.agent.workflows import script_runner

    home = tmp_path/'owner'
    store = ScriptStore(home)
    descriptor = AssetDescriptor(id='tail', kind='script', name='tail')
    store.save_script(descriptor, 'print(\'{"version":1}\')')
    execute = script_runner._execute
    def change_after_freeze(*args):
        store.save_script(descriptor, 'print(\'{"version":2}\')')
        return execute(*args)
    monkeypatch.setattr(script_runner, '_execute', change_after_freeze)
    result = script_runner.ScriptRunner(home, use_sandbox=False).run_script('tail')
    assert result.ok and result.output_data == {'version':1}


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


def test_review_extraction_registry_write_failure_restores_all(tmp_path,monkeypatch):
    from agent_py_agent.agent.assets import turn_extractor as extract
    from agent_py_agent.tests.test_turn_asset_extractor import history

    home,store,thread,_,first,*_ = history.__wrapped__(tmp_path)
    proposal = extract.propose_assets_from_turns(store,owner_home=home,
        turn_refs=[{'thread_id':thread.thread_id,'message_id':first.message_id}],target_types=['skill'])
    original = SkillLearningStore.save_registry
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


def test_f04_application_scope_prioritizes_relevant_package():
    from agent_py_agent.agent.capability.router import CapabilityCard, CapabilityRouter

    cards = [CapabilityCard('capability:a-other','capability_package','a-other','查询',metadata={'package_id':'a-other','stable_id':'capability:a-other'}),
             CapabilityCard('capability:z-current','capability_package','z-current','查询',
                            metadata={'package_id':'z-current','stable_id':'capability:z-current','application_scope':{'project_id':'project-a'}})]
    router = CapabilityRouter(extra_cards=cards)
    agent = SimpleNamespace(_current_run_params=SimpleNamespace(user_prompt='查询',task_attributes={'project_id':'project-a'}))
    class Provider:
        _current_run_params = agent._current_run_params
        def current(self):
            return None
    router._skill_snapshot_provider = Provider().current
    result = router.render_skill_metadata_index(context_window_tokens=128000)
    assert result.index('z-current') < result.index('a-other')


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


@pytest.mark.parametrize('broken', ['conversations', 'scheduler', 'proposals', 'subagents'])
def test_r2_protection_source_failure_preserves_other_references(learned, monkeypatch, broken):
    from agent_py_agent.agent.capability import skill_learning_proposals as proposals
    from agent_py_agent.agent.capability.skill_learning_protection import (
        learning_protection_for_agent,
    )

    home, _ = learned
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
    def interrupted(*args):
        raise OSError('interrupted publication')
    monkeypatch.setattr(publisher, 'publish_learned_skill', interrupted)
    with pytest.raises(OSError):
        promote_verified_skill(store, publish, lambda _, sha: {
            'status':'passed', 'executed':True, 'contract_valid':True, 'sha256':sha,
            'receipt_id':'old-pass', 'output':{}})
    sha = skill_content_sha256(render_learned_skill(publish.decision))
    assert builtin_machine_verifier(publish, sha, store=store) is not None
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


def test_r3_b3_package_metadata_scope_survives_projection():
    from agent_py_agent.agent.capability.router import from_capability_package

    package = SimpleNamespace(stable_id='capability:p', package_id='p', description='summary', keywords=[],
        summary='summary', source='test', version='1', metadata={'application_scope':{'channels':['im']}},
        to_ref=lambda: {'package_id':'p'})
    assert from_capability_package(package).metadata['application_scope'] == package.metadata['application_scope']


@pytest.mark.parametrize('clock,expected', [(0, 1), (1, 1), ('recent', 0)])
def test_r3_f17_discovery_uses_maintenance_clock_not_hot_mtime(learned, clock, expected):
    import time

    from agent_py_agent.tests.test_gateway_learning_discovery import learning_sources

    home, store = learned
    with store.state_lock():
        store.save_registry(replace(store.load_registry(), last_maintenance_at=time.time() if clock == 'recent' else clock))
    refs = learning_sources(SimpleNamespace(home=home, store=store))
    assert len(refs) == expected


def test_r3_f04_reactivation_rejects_old_cursor_and_counts_total():
    from agent_py_agent.agent.capability.skill_search_tool import _search_page

    hits = [SimpleNamespace(card=SimpleNamespace(id=str(i), metadata={'content_sha256':'a'*64, 'activation_id':'old'}), score=1) for i in range(2)]
    _, page = _search_page(hits, {}, 'query', '', 1)
    assert page.get('total_count') == page['total'] == 2
    hits[0].card.metadata['activation_id'] = 'new'
    with pytest.raises(ValueError, match='CURSOR_STALE'):
        _search_page(hits, {'cursor':page['next_cursor']}, 'query', '', 1)


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


@pytest.mark.parametrize('kind,body,passed', [('script','print("ok")',True), ('script','def broken(',False),
    ('workflow',[{'step_id':'a','mode':'deterministic_script','script_id':'s'}],True), ('workflow',[],False)])
def test_r3_f07_default_machine_verifier_checks_draft_syntax(kind, body, passed):
    from agent_py_agent.agent.capability import asset_promotion

    verifier = getattr(asset_promotion, 'default_machine_promotion_verifier', None)
    assert callable(verifier)
    draft = {'kind':kind, 'name':'valid-name', 'description':'reusable procedure', 'code':body if kind=='script' else '',
             'steps':body if kind=='workflow' else []}
    assert (verifier(draft)['status'] == 'passed') is passed


@pytest.mark.parametrize('extra', [{'target':'agents'}, {'target':'SOUL.md'}, {'name':'../escape'}, {'body':''},
                                  {'body':'Authorization: Bearer '+'x'*50}])
def test_r3_f07_default_verifier_rejects_unsafe_or_empty_drafts(extra):
    from agent_py_agent.agent.capability import asset_promotion

    verifier = getattr(asset_promotion, 'default_machine_promotion_verifier', None)
    assert callable(verifier)
    assert verifier({'kind':'skill', 'name':'valid-name', 'description':'procedure', 'body':BODY, **extra})['status'] != 'passed'


def test_r3_f04_cursor_alone_binds_activation_generation():
    from agent_py_agent.agent.capability.skill_search_tool import _search_page

    hits = [SimpleNamespace(card=SimpleNamespace(id=str(i), metadata={'content_sha256':'a'*64, 'activation_id':'old'}), score=1) for i in range(2)]
    _, page = _search_page(hits, {}, 'query', '', 1)
    hits[0].card.metadata['activation_id'] = 'new'
    with pytest.raises(ValueError, match='CURSOR_STALE'):
        _search_page(hits, {'cursor':page['next_cursor']}, 'query', '', 1)


def test_r3_f07_package_carrier_is_representable():
    from agent_py_agent.agent.capability.asset_promotion import select_asset_carrier

    assert select_asset_carrier({'kind':'capability_package'}) == 'capability_package'
