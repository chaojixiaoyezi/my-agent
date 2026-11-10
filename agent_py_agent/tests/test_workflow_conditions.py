# 模块用途: WF-04 类型化条件回归：旧 equals 形状逐字兼容（含 REQ-002 评审 F1 反例）、超大整数与坏重复计数（F2/F3）、
#   缺值/类型稳定、正则安全子集，以及真实 WorkflowStore 保存与 WorkflowEngine 单节点条件分支。
from __future__ import annotations

import time

import pytest

from agent_py_agent.agent.workflows.conditions import (
    WorkflowConditionError,
    evaluate_condition,
    validate_condition,
)


def leaf(op, value=None, *, key='v', step='s'):
    node = {'op': op, 'ref': {'step_id': step, 'key': key}}
    if op not in ('exists', 'not_exists'):
        node['value'] = value
    return node


@pytest.mark.parametrize('condition,outputs,expected', [
    ({'step_id': 's', 'key': '', 'equals': 7}, {'s': {'': 7}}, True),
    ({'step_id': 's', 'key': 0, 'equals': 7}, {'s': [7]}, True),
    ({'step_id': 's', 'equals': 7, 'fallback': 'unused'}, {'s': 7}, True),
    # 旧形状保留 Python 相等（1 == True）；类型化 eq 才区分 bool 与数字。
    ({'step_id': '$inputs', 'key': 'flag', 'equals': True}, {}, True),
])
def test_legacy_equals_shape_is_byte_compatible(condition, outputs, expected):
    assert evaluate_condition(condition, outputs, {'flag': 1}) is expected
    validate_condition(condition)


def test_legacy_shape_keeps_original_exceptions():
    with pytest.raises(TypeError):
        evaluate_condition({'step_id': [], 'equals': 7}, {}, {})
    with pytest.raises(KeyError):
        evaluate_condition({'step_id': 'missing', 'equals': 7}, {}, {})


@pytest.mark.parametrize('node,outputs,expected', [
    (leaf('gt', 3), {'s': {'v': 4}}, True), (leaf('gte', 4), {'s': {'v': 4}}, True),
    (leaf('lt', 3), {'s': {'v': 4}}, False), (leaf('lte', 4.5), {'s': {'v': 4}}, True),
    (leaf('eq', 10 ** 400), {'s': {'v': 10 ** 400}}, True), (leaf('gt', 1.5), {'s': {'v': 10 ** 400}}, True),
    (leaf('eq', 10 ** 400), {'s': {'v': 'x'}}, False), (leaf('gt', 'b'), {'s': {'v': 'c'}}, True),
    (leaf('gt', 3), {'s': {'v': '4'}}, False), (leaf('eq', 1), {'s': {'v': True}}, False),
    (leaf('neq', 1), {'s': {'v': True}}, True), (leaf('in', ['a', 'b']), {'s': {'v': 'b'}}, True),
    (leaf('not_in', ['a', 'b']), {'s': {'v': 'c'}}, True), (leaf('regex', r'^ERR-\d+$'), {'s': {'v': 'ERR-42'}}, True),
    (leaf('regex', r'^ERR-\d+$'), {'s': {'v': 42}}, False),
    (leaf('exists'), {'s': {'v': 0}}, True), (leaf('exists'), {'s': {'v': None}}, False),
    (leaf('not_exists'), {'s': {}}, True),
    (leaf('neq', 1), {'s': {}}, False), (leaf('not_in', [1]), {}, False), (leaf('gt', 0), {'s': [1]}, False),
    ({'op': 'and', 'conditions': [leaf('gt', 1), leaf('lt', 9)]}, {'s': {'v': 5}}, True),
    ({'op': 'or', 'conditions': [leaf('eq', 1), leaf('eq', 5)]}, {'s': {'v': 5}}, True),
    ({'op': 'and', 'conditions': [leaf('gt', 1), leaf('exists', key='other')]}, {'s': {'v': 5}}, False),
])
def test_typed_operators_with_stable_missing_and_type_behavior(node, outputs, expected):
    validate_condition(node)
    assert evaluate_condition(node, outputs, {}) is expected


def test_typed_ref_fallback_and_inputs():
    node = {'op': 'eq', 'ref': {'step_id': 's', 'key': 'v', 'fallback': {'step_id': '$inputs', 'key': 'n'}}, 'value': 3}
    assert evaluate_condition(node, {'s': {'v': None}}, {'n': 3}) is True


@pytest.mark.parametrize('node', [
    {'op': 'gt', 'ref': {'step_id': 's'}},
    {'op': 'between', 'ref': {'step_id': 's'}, 'value': 1},
    {'op': 'eq', 'ref': {'step_id': 's', 'path': 'x'}, 'value': 1},
    {'op': 'in', 'ref': {'step_id': 's'}, 'value': 'abc'},
    {'op': 'eq', 'ref': {'step_id': 's'}, 'value': {'nested': 1}},
    {'op': 'eq', 'ref': {'step_id': 's'}, 'value': float('nan')},
    {'op': 'and', 'conditions': []},
    {'op': 'or', 'conditions': [leaf('eq', 1)] * 33},
    leaf('regex', 'a{99999999999999999999}'), leaf('regex', 'a{2000}'), leaf('regex', '(a|aa)+b'),
    leaf('regex', '(a+)+$'), leaf('regex', r'(\w)\1'), leaf('regex', '(?=a)a'), leaf('regex', 'a*b*c*'),
    leaf('regex', '('), leaf('regex', ''), leaf('regex', 'a' * 257),
])
def test_invalid_typed_conditions_are_structured_rejections(node):
    with pytest.raises(WorkflowConditionError) as error:
        validate_condition(node)
    assert error.value.error_code == 'WORKFLOW_CONDITION_INVALID'


def test_depth_and_node_limits():
    deep = leaf('eq', 1)
    for _ in range(8):
        deep = {'op': 'and', 'conditions': [deep]}
    with pytest.raises(WorkflowConditionError):
        validate_condition(deep)
    wide = {'op': 'or', 'conditions': [{'op': 'and', 'conditions': [leaf('eq', i)] * 30} for i in range(3)]}
    with pytest.raises(WorkflowConditionError):
        validate_condition(wide)


def test_accepted_regex_is_fast_on_adversarial_subject():
    node = leaf('regex', r'^a*b$')
    started = time.monotonic()
    assert evaluate_condition(node, {'s': {'v': 'a' * 100000}}, {}) is False
    assert time.monotonic() - started < 1.0


def test_store_rejects_invalid_typed_condition_and_engine_branches_on_valid_one(tmp_path):
    from agent_py_agent.agent.workflows import WorkflowEngine
    from agent_py_agent.tests.test_workflow_engine import save_node

    with pytest.raises(WorkflowConditionError):
        save_node(tmp_path, 'script_transform', config={'mapping': {}}, condition=leaf('regex', '(a|aa)+b'))
    save_node(tmp_path, 'script_transform', config={'mapping': {'value': {'step_id': '$inputs', 'key': 'n'}}},
              condition={'op': 'and', 'conditions': [
                  {'op': 'gte', 'ref': {'step_id': '$inputs', 'key': 'n'}, 'value': 3},
                  {'op': 'regex', 'ref': {'step_id': '$inputs', 'key': 'tag'}, 'value': '^ok'}]})
    engine = WorkflowEngine(tmp_path)
    matched = engine.debug_step('debug-flow', 'target', mock_inputs={'n': 4, 'tag': 'ok-1'})
    assert matched['condition_branch'] is True and matched['output_data'] == {'value': 4}
    skipped = engine.debug_step('debug-flow', 'target', mock_inputs={'n': 2, 'tag': 'ok-1'})
    assert skipped['condition_branch'] is False and skipped['status'] == 'skipped'
