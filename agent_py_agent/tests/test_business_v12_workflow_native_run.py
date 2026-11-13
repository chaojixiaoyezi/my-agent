# 模块用途: G5（MSG-017）原生工作流运行端口回归：同一 Owner 复用已登记 WorkflowsTool 的 owner 绑定引擎；运行级上下文以准确
#   workflow_run_id 作逻辑 task_id，与运行记录及显式来源同次冻结。原生 create → run → 耐久结果 → workflow_runs.get 观察，
#   同键重放不再执行、异意图同键 409，新 Gateway/Agent 读回同一运行，范围撤销后新运行在写入前被拒。只用 tmp 隔离 home。
from __future__ import annotations

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.tests.test_business_v12_background_scheduling import (  # noqa: F401 - pytest fixture
    call,
    native,
    ok,
)
from agent_py_agent.tests.test_business_v12_workflow_receipts import refused


def _flow(name, *, scope=None):
    step = {'step_id': 'calc', 'name': 'calc', 'mode': 'deterministic_script', 'script_id': 'calc'}
    definition = {'descriptor': {'id': name, 'kind': 'workflow', 'name': name, 'access_targets': [],
                                 'data_sources': [], 'credential_refs': [], 'io_contract': {}}, 'steps': [step]}
    return {'definition': definition, **({'scope': scope} if scope else {})}


def _script(agent):
    ScriptStore(agent.home_paths.owner_home_dir).save_script(AssetDescriptor('calc', 'script', 'calc'),
                                                             'import json\nprint(json.dumps({"total": 3}))')


def _runs(agent):
    return WorkflowRunStore(agent.home_paths.owner_home_dir).list_runs(limit=100)


def test_owner_level_run_executes_natively_without_freezing_a_space(native):  # noqa: F811
    _, agent = native
    _script(agent)
    ok(native, 'workflows.create', _flow('native-owner'), idempotency_key='g5-owner-create-0001')
    receipt = ok(native, 'workflows.run', {'workflow_ref': 'native-owner'}, idempotency_key='g5-owner-run-000001')
    assert receipt['state'] == 'completed'
    run = ok(native, 'workflow_runs.get', {'workflow_run_id': receipt['workflow_run_id']})
    assert (run['run']['status'], run['run']['task_id'], run['run']['space_id']) == ('succeeded', None, None)
    [record] = _runs(agent)
    assert record.business_context == {} and record.steps[0].output_snapshot == {'total': 3}
    snapshot = WorkflowRunStore(agent.home_paths.owner_home_dir).load_workflow_snapshot(record.workflow_run_id)
    assert '_business_context_origin' not in snapshot


def test_space_run_freezes_run_scoped_context_and_survives_restart_duplicate_and_revocation(native, tmp_path):  # noqa: F811
    server, agent = native
    _script(agent)
    space = ok(native, 'spaces.create', {'name': 'orders'}, idempotency_key='g5-space-create-0001')['space_id']
    other = ok(native, 'spaces.create', {'name': 'archive'}, idempotency_key='g5-space-create-0002')['space_id']
    scope = {'scope_mode': 'spaces', 'space_ids': [space]}
    ok(native, 'workflows.create', _flow('native-space', scope=scope), idempotency_key='g5-space-flow-00001')
    request = {'workflow_ref': 'native-space', 'space_id': space}
    receipt = ok(native, 'workflows.run', request, idempotency_key='g5-space-run-000001')
    run_id = receipt['workflow_run_id']
    assert receipt['state'] == 'completed'
    [record] = _runs(agent)
    store = WorkflowRunStore(agent.home_paths.owner_home_dir)
    assert record.workflow_run_id == run_id and record.status == 'succeeded'
    assert record.steps[0].output_snapshot == {'total': 3}
    assert (record.business_context['task_id'], record.business_context['space_id']) == (run_id, space)
    assert record.business_context['owner_id'] == agent.home_paths.owner_id
    assert store.load_workflow_snapshot(run_id)['_business_context_origin'] == 'workflow_run'
    view = ok(native, 'workflow_runs.get', {'workflow_run_id': run_id})['run']
    assert (view['task_id'], view['space_id'], view['status']) == (None, space, 'succeeded')
    # 同键同意图重放：同一运行、不再执行；同键异意图：写入前 409。
    again = ok(native, 'workflows.run', request, idempotency_key='g5-space-run-000001')
    assert again['workflow_run_id'] == run_id and len(_runs(agent)) == 1
    assert store.load_run(run_id).revision == record.revision
    refused(native, 'workflows.run', {**request, 'inputs': {'changed': True}}, 'IDEMPOTENCY_CONFLICT',
            idempotency_key='g5-space-run-000001')
    # 范围外空间在写入前拒绝，不新建运行。
    refused(native, 'workflows.run', {'workflow_ref': 'native-space', 'space_id': other}, 'WORKFLOW_SPACE_MISMATCH',
            idempotency_key='g5-space-run-000002')
    assert len(_runs(agent)) == 1
    # 新 Gateway/Agent 进程视角：同一 home 读回原运行与冻结上下文。
    server.stop()
    agent2 = SimpleAgent(AgentConfig(model_backend='echo', enable_tools=True, business_spaces_enabled=True,
                                     memory_path='memory.jsonl', my_agent_home=str(tmp_path / 'home')),
                         tmp_path / 'workspace')
    server2 = GatewayHTTPServer(0, gateway_paths_from_root(agent2.home_paths.root / 'gateway'),
                                params=GatewayHTTPServerParams(agent=agent2))
    server2.start()
    try:
        restarted = (server2, agent2)
        view = ok(restarted, 'workflow_runs.get', {'workflow_run_id': run_id})['run']
        assert (view['status'], view['space_id'], view['revision']) == ('succeeded', space, record.revision)
        # 撤销：范围改到另一空间后，同空间的新运行在写入前被拒；原运行不变。
        current = ok(restarted, 'workflows.get', {'workflow_ref': 'native-space'})['workflow']
        ok(restarted, 'workflows.scope.set', {'workflow_ref': 'native-space',
           'scope': {'scope_mode': 'spaces', 'space_ids': [other]}},
           idempotency_key='g5-space-scope-00001', expected_revision=current['scope_revision'])
        refused(restarted, 'workflows.run', request, 'WORKFLOW_SPACE_MISMATCH', idempotency_key='g5-space-run-000003')
        ok(restarted, 'workflows.scope.set', {'workflow_ref': 'native-space',
           'scope': {'scope_mode': 'unassigned', 'space_ids': []}},
           idempotency_key='g5-space-scope-00002', expected_revision=current['scope_revision'] + 1)
        refused(restarted, 'workflows.run', request, 'WORKFLOW_UNASSIGNED', idempotency_key='g5-space-run-000004')
        assert [row.workflow_run_id for row in _runs(agent2)] == [run_id]
        assert store.load_run(run_id).revision == record.revision
    finally:
        server2.stop()


def test_non_primary_owner_executes_with_its_own_workflow_engine(native):  # noqa: F811
    from agent_py_agent.tests.test_mega_business_api_actions import background_call

    _, agent = native
    owner_home = agent.home_paths.owner_home_dir.parents[1] / 'providers' / 'feishu' / 'users' / 'workflow-owner'
    ScriptStore(owner_home).save_script(AssetDescriptor('calc', 'script', 'calc'),
        'import json\nprint(json.dumps({"total": 3}))')
    request = _flow('foreign-owner-condition')
    status, created = background_call(native, 'workflows.create', request,
        user='workflow-owner', idempotency_key='owner-create-00001')
    assert status == 200, created
    status, run = background_call(native, 'workflows.run', {'workflow_ref': 'foreign-owner-condition'},
        user='workflow-owner', idempotency_key='owner-run-00000001')
    assert status == 200, run
    status, view = background_call(native, 'workflow_runs.get',
        {'workflow_run_id': run['data']['workflow_run_id']}, user='workflow-owner')
    assert status == 200 and view['data']['run']['status'] == 'succeeded', view
    assert _runs(agent) == []
    [record] = WorkflowRunStore(owner_home).list_runs(limit=10)
    assert record.owner_id != agent.home_paths.owner_id
    assert record.steps[0].output_snapshot == {'total': 3}
    status, scope = background_call(native, 'workflows.scope.get',
        {'workflow_ref': 'foreign-owner-condition'}, user='workflow-owner')
    assert status == 200, scope
    status, scoped = background_call(native, 'workflows.scope.set',
        {'workflow_ref': 'foreign-owner-condition', 'scope': {'scope_mode': 'unassigned', 'space_ids': []}},
        user='workflow-owner', idempotency_key='owner-unassign-0001', expected_revision=scope['data']['scope_revision'])
    assert status == 200, scoped
    status, denied = background_call(native, 'workflows.run', {'workflow_ref': 'foreign-owner-condition'},
        user='workflow-owner', idempotency_key='owner-run-denied-0001')
    assert status == 409 and denied['error']['code'] == 'WORKFLOW_UNASSIGNED', denied
    assert len(WorkflowRunStore(owner_home).list_runs(limit=10)) == 1
