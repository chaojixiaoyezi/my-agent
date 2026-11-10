# 模块用途: V07-03 回归：workflows.get 经原生 /client/business 返回对象详情（用途、IO 合同、步骤摘要与依赖），
#   超出上限或旧记录不合规时标 coverage=partial 而不是让详情整体失败。只用 tmp 隔离 home，不是线上证据。
from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.workflows.business_scope import workflow_detail_view
from agent_py_agent.tests.test_business_v12_background_scheduling import (  # noqa: F401 - pytest fixture
    native,
    ok,
)
from agent_py_agent.tests.test_business_v12_workflow_foreach_schema import flow


def test_workflow_detail_has_purpose_io_and_step_summaries(native):  # noqa: F811
    definition = flow('detail-flow', None)
    definition['descriptor']['summary'] = '按金额汇总订单并在超阈值时通知'
    io_contract = {'inputs': ['orders'], 'outputs': ['total'], 'nullable_outputs': [], 'output_types': {'total': 'number'}}
    definition['descriptor']['io_contract'] = io_contract
    definition['steps'] = [
        {'step_id': 'load', 'name': 'load orders', 'mode': 'deterministic_script', 'script_id': 'load'},
        {'step_id': 'sum', 'name': 'sum orders', 'mode': 'deterministic_script', 'script_id': 'sum',
         'inputs_map': {'rows': {'step_id': 'load', 'key': 'rows'}, 'n': {'step_id': '$inputs', 'key': 'n'}},
         'condition': {'op': 'gt', 'ref': {'step_id': '$inputs', 'key': 'n'}, 'value': 0},
         'foreach': {'from': 'inputs.orders'}, 'on_error': 'skip'}]
    ok(native, 'workflows.create', {'definition': definition}, idempotency_key='detail-flow-create-001')
    detail = ok(native, 'workflows.get', {'workflow_ref': 'detail-flow'})['detail']
    assert detail == {'summary': '按金额汇总订单并在超阈值时通知', 'io_contract': io_contract, 'step_count': 2,
                      'coverage': 'complete', 'steps': [
        {'step_id': 'load', 'name': 'load orders', 'mode': 'deterministic_script', 'depends_on': [],
         'conditional': False, 'foreach': False, 'on_error': 'escalate_to_agent'},
        {'step_id': 'sum', 'name': 'sum orders', 'mode': 'deterministic_script', 'depends_on': ['load'],
         'conditional': True, 'foreach': True, 'on_error': 'skip'}]}


def test_detail_marks_partial_instead_of_failing_on_bounds_or_legacy_io():
    def step(index):
        return SimpleNamespace(step_id=f's{index}', name='n' * 300, mode='deterministic_script', inputs_map={},
                               condition=None, foreach=None, on_error='fail')

    legacy_io = SimpleNamespace(to_dict=lambda: {'inputs': 'not-a-list'})
    definition = SimpleNamespace(descriptor=SimpleNamespace(summary='p' * 5000, io_contract=legacy_io),
                                 steps=[step(index) for index in range(101)])
    detail = workflow_detail_view(definition)
    assert detail['coverage'] == 'partial' and detail['io_contract'] is None
    assert detail['step_count'] == 101 and len(detail['steps']) == 100
    assert len(detail['summary']) == 4096 and len(detail['steps'][0]['name']) == 256
