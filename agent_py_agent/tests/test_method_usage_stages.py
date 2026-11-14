"""V03-06：原工具与真实Store接受点写账，纯读不冒充选择，原分页不写。"""
import json
from types import SimpleNamespace

from agent_py_agent.agent.capability.method_usage import (
    method_row,
    method_usage_scope,
    record_workflow_receipts,
)
from agent_py_agent.agent.capability.router import CapabilityRouter
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshot
from agent_py_agent.agent.capability.skill_usage_ledger import (
    load_skill_usage_summary,
    record_skill_usage_events,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import home_paths_with_owner
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.tests.test_workflow_package_template_derivation import installed, save


def world(tmp_path):
    definition = {'descriptor': {'id': 'source', 'kind': 'workflow', 'name': '模板',
        'access_targets': [], 'data_sources': [], 'credential_refs': [], 'io_contract': {}},
        'steps': [{'step_id': 'analyze', 'name': '分析', 'mode': 'llm_skill_analysis', 'skill_ids': []}]}
    w = installed(tmp_path / 'home', definition)
    snapshot = SkillSnapshot((), (), 'test', w.owner.owner_id, '', (w.pack,))
    paths = home_paths_with_owner(home_paths(w.owner.root), w.owner)
    params = SimpleNamespace(task_id='task-one', request_id='request-one', run_id='run-one', task_attributes={})
    agent = SimpleNamespace(home_paths=paths, current_skill_snapshot=lambda: snapshot,
        capability_router=CapabilityRouter(skill_snapshot=snapshot), config=AgentConfig(), _current_run_params=params)
    w.tool.run_params_provider = lambda: params
    w.tool.skill_snapshot_provider = lambda: snapshot
    w.tool.engine = WorkflowEngine(w.owner.home_dir, analysis_handler=lambda *_: {'value': 1}, owner_id=w.owner.owner_id)
    return w, agent, SkillSearchTool(agent), params, SkillLearningStore.for_home(paths)


def rows(store):
    p = store.directory / 'usage_ledger.jsonl'
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_natural_search_read_explicit_accept_and_actual_run_have_five_stages(tmp_path):
    w, agent, search, params, store = world(tmp_path)
    assert search.execute({'action': 'search', 'query': '模板'}).ok
    assert {x['event'] for x in rows(store)} == {'visible', 'manual_search'}
    assert search.execute({'action': 'get', 'package_id': w.pack.package_id}).ok
    assert {x['event'] for x in rows(store)} == {'visible', 'manual_search', 'manual_get'}
    # 纯get不是选择。只有原source读取/验证及Owner Store接受后，才出现selected。
    assert save(w).ok
    assert sum(x['event'] == 'selected' for x in rows(store)) == 1
    result = w.tool.execute({'action': 'run', 'workflow_id': 'derived-check'})
    assert result.ok, result.output
    events = rows(store)
    assert {x['event'] for x in events} == {'visible', 'manual_search', 'selected', 'manual_get', 'executed'}
    executed = next(x for x in events if x['event'] == 'executed')
    assert executed['receipt_ref'].startswith('workflow_run:')
    assert all(x['task_id'] == params.task_id and x['version_ref'] == w.pack.package_sha256 for x in events)


def test_no_choice_or_read_control_stays_only_candidates_and_repeated_pages_do_not_write(tmp_path):
    w, agent, search, params, store = world(tmp_path)
    assert search.execute({'action': 'search', 'query': '模板'}).ok
    before = {p.name: p.read_bytes() for p in store.directory.glob('usage_*') if p.is_file() and not p.name.endswith('.lock')}
    for _ in range(3):
        assert search.execute({'action': 'search', 'query': '模板'}).ok
        load_skill_usage_summary(store)
    after = {p.name: p.read_bytes() for p in store.directory.glob('usage_*') if p.is_file() and not p.name.endswith('.lock')}
    assert before == after
    assert len(rows(store)) == 2
    assert not any(x['event'] in ('selected', 'executed', 'manual_get') for x in rows(store))


def test_version_and_task_keys_distinguish_phases_without_changing_feedback_uses(tmp_path):
    w, agent, search, params, store = world(tmp_path)
    def item(event, version='a' * 64, receipt='request:one'):
        return method_row(params, event=event, kind='skill', stable_id='owner:method',
            version_ref=version, source='unit_actual_return', receipt_ref=receipt)
    batch = [item('visible'), item('selected')]
    record_skill_usage_events(store, batch + batch)
    assert len(rows(store)) == 2
    assert load_skill_usage_summary(store)['totals']['effective_uses'] == 0
    record_skill_usage_events(store, [item('selected', 'b' * 64)])
    params.task_id = 'task-two'
    record_skill_usage_events(store, [item('selected')])
    assert len(rows(store)) == 4


def test_failed_or_unstarted_run_cannot_create_execution_usage(tmp_path):
    w, agent, search, params, store = world(tmp_path)
    assert save(w).ok
    result = w.tool.execute({'action': 'run', 'workflow_id': 'nonexistent'})
    assert not result.ok
    assert not any(x['event'] == 'executed' for x in rows(store))
    # inspect不是执行；即使给一个外观像run的结果也不能凭点选补写。
    record_workflow_receipts(w.tool, {'action': 'inspect_run'}, SimpleNamespace(result_envelope={'workflow_run_id':'invented'}))
    assert not any(x['event'] == 'executed' for x in rows(store))


def test_context_is_frozen_before_late_host_parameter_change(tmp_path):
    w, agent, search, params, store = world(tmp_path)
    with method_usage_scope(params):
        params.task_id = 'late-other-task'
        assert search.execute({'action': 'search', 'query': '模板'}).ok
    assert {row['task_id'] for row in rows(store)} == {'task-one'}


def test_model_attribute_business_context_cannot_reassign_actual_search_ledger(tmp_path):
    from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
    w, agent, search, params, store = world(tmp_path)
    params.task_id = 'task-A'
    params.business_context = BusinessExecutionContext(w.owner.owner_id, 'space-a', None,
        'task-A', 1, None, (), (), 'business-context.v1')
    params.task_attributes['business_context'] = {'task_id': 'forged-task-B'}
    with method_usage_scope(params):
        assert search.execute({'action': 'search', 'query': '模板'}).ok
    events = rows(store)
    assert {x['event'] for x in events} == {'visible', 'manual_search'}
    assert {x['task_id'] for x in events} == {'task-A'}


def test_attribute_or_untyped_business_context_never_supplies_task_authority(tmp_path):
    w, agent, search, params, store = world(tmp_path)
    params.task_attributes['business_context'] = {'task_id': 'forged-task-B'}
    params.business_context = {'task_id': 'forged-task-C'}
    with method_usage_scope(params):
        assert search.execute({'action': 'search', 'query': '模板'}).ok
    assert {x['task_id'] for x in rows(store)} == {'task-one'}
    params.task_id = ''
    with method_usage_scope(params):
        assert search.execute({'action': 'search', 'query': '模板'}).ok
    assert not any(x['task_id'].startswith('forged-') for x in rows(store))
    assert rows(store)[-1]['event'] == 'manual_search'
    assert rows(store)[-1]['task_id'] == ''
