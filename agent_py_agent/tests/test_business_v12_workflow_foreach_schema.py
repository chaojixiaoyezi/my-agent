# 模块用途: WF-07 回归：已有 foreach 批处理配置经原生 /client/business 的冻结 schema 保存、读回与校验，
#   非法配置在执行前被结构化拒绝；不创建新的批处理引擎。
from __future__ import annotations

from agent_py_agent.agent.assets.workflow_store import WorkflowStore
from agent_py_agent.tests.test_business_v12_background_scheduling import (  # noqa: F401 - pytest fixture
    call,
    native,
    ok,
)


def flow(name, foreach):
    step = {'step_id': 'each', 'name': 'each', 'mode': 'deterministic_script', 'script_id': 'per-item',
            'foreach': foreach}
    return {'descriptor': {'id': name, 'kind': 'workflow', 'name': name, 'access_targets': [], 'data_sources': [],
                           'credential_refs': [], 'io_contract': {}}, 'steps': [step]}


def test_foreach_roundtrips_through_native_workflow_schema_and_bad_config_is_rejected(native):  # noqa: F811
    foreach = {'from': 'inputs.orders', 'batch_size': 2, 'max_parallel': 2, 'on_batch_error': 'continue'}
    created = ok(native, 'workflows.create', {'definition': flow('orders-each', foreach)},
                 idempotency_key='foreach-create-orders-01')
    assert ok(native, 'workflows.get', {'workflow_ref': created['workflow_ref']})['workflow']['definition_revision'] == 1
    _, host = native
    saved = WorkflowStore(host.home_paths.owner_home_dir).get_workflow(created['workflow_ref'])
    assert saved.steps[0].foreach == foreach
    for bad in ({'from': 'inputs.orders', 'batch_size': 0}, {'from': 'inputs.orders', 'surprise': 1},
                {'batch_size': 2}, {'from': 'inputs.orders', 'max_parallel': 9}):
        status, payload = call(native, 'workflows.create', {'definition': flow('orders-bad', bad)},
                               idempotency_key='foreach-create-bad-0001')
        assert status == 400 and payload['effect_outcome'] == 'not_started', (bad, payload)
    status, payload = call(native, 'workflows.get', {'workflow_ref': 'orders-bad'})
    assert status == 404, payload
