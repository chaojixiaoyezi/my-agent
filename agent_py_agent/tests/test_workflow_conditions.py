# 模块用途: WF-04 类型化条件回归：旧 equals 形状逐字兼容（含 REQ-002 评审 F1 反例）、超大整数与坏重复计数（F2/F3）、
#   缺值/类型稳定、正则子集与 NFA 匹配的工作量上界（MSG-012 WF04-R1/R2 反例）、整数支持范围（WF04-R3），
#   以及真实 WorkflowStore 保存与 WorkflowEngine 单节点条件分支。
from __future__ import annotations

import random
import re
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
    leaf('regex', 'a{99999999999999999999}'), leaf('regex', 'a{2000}'), leaf('regex', r'(\w)\1'),
    leaf('regex', '(?=a)a'), leaf('regex', '('), leaf('regex', ''), leaf('regex', 'a' * 257),
    leaf('regex', 'a{300}'), leaf('regex', '(a{100}){100}'), leaf('regex', r'\bword'), leaf('regex', '(?i)err'),
    leaf('regex', '(?s:.)'), leaf('regex', 'a++'), leaf('regex', '(?>a)'), leaf('regex', '(?<=a)b'),
    leaf('eq', 10 ** 1000), leaf('in', [1, -10 ** 1000]),
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


# 评审 WF04-R1/R2 的接受反例与旧版曾拒绝的回溯形状：现在都被接受，并在 NFA 上界内给出与 re 相同的结论。
@pytest.mark.parametrize('pattern,subject,expected', [
    ('(a?){30}a{30}', 'a' * 30, True), ('(a|aa){40}b', 'a' * 80, False), ('a*a*b', 'a' * 4096, False),
    ('(a|aa)+b', 'a' * 4096, False), ('(a+)+$', 'a' * 4095 + '!', False), ('a*b*c*d', 'abc' * 1365, False),
    ('(.?){120}x', ''.join(chr(0x4e00 + index) for index in range(4096)), False), ('^ERR-\\d+$', 'ERR-42', True),
], ids=['optional-fixed', 'branch-fixed', 'adjacent-stars', 'branch-plus', 'nested-plus', 'three-stars',
        'wide-distinct-chars', 'anchored-digits'])
def test_backtracking_shapes_evaluate_within_the_nfa_bound(pattern, subject, expected):
    node = leaf('regex', pattern)
    validate_condition(node)
    started = time.monotonic()
    assert evaluate_condition(node, {'s': {'v': subject}}, {}) is expected
    assert time.monotonic() - started < 1.5


def _random_pattern(rng, depth=0):
    roll = rng.random()
    if depth > 3 or roll < 0.3:
        return rng.choice(['a', 'b', '.', '[ab]', '[^a]', r'\d', r'\w', r'\s', '1', r'\n'])
    if roll < 0.5:
        return _random_pattern(rng, depth + 1) + _random_pattern(rng, depth + 1)
    if roll < 0.6:
        return '(' + _random_pattern(rng, depth + 1) + '|' + _random_pattern(rng, depth + 1) + ')'
    if roll < 0.85:
        suffix = rng.choice(['*', '+', '?', '{2}', '{0,2}', '{1,3}', '{2,}', '*?', '+?'])
        return '(?:' + _random_pattern(rng, depth + 1) + ')' + suffix
    return rng.choice(['^', '$', r'\A', r'\Z']) + _random_pattern(rng, depth + 1)


def test_regex_matcher_agrees_with_re_search_on_the_supported_subset():
    rng = random.Random(20261110)
    checked = 0
    for _ in range(1500):
        pattern = _random_pattern(rng)
        node = leaf('regex', pattern)
        for _ in range(5):
            subject = ''.join(rng.choice('ab1 \n_') for _ in range(rng.randint(0, 9)))
            assert evaluate_condition(node, {'s': {'v': subject}}, {}) is (re.search(pattern, subject) is not None), (
                pattern, subject)
            checked += 1
    assert checked == 7500


def test_integer_range_is_explicit_and_resolution_is_typed(tmp_path):
    from agent_py_agent.agent.workflows import WorkflowEngine
    from agent_py_agent.tests.test_workflow_engine import save_node

    validate_condition(leaf('eq', 10 ** 1000 - 1))
    with pytest.raises(WorkflowConditionError) as error:
        validate_condition(leaf('gt', -10 ** 1000))
    assert error.value.details == {'reason': 'integer_out_of_range'}
    # 运行时读到的超范围整数按类型不符：存在，但不等于/不大于任何值。
    huge = {'s': {'v': 10 ** 2000}}
    assert [evaluate_condition(leaf(op, value), huge, {}) for op, value in (
        ('eq', 5), ('neq', 5), ('gt', 5), ('in', [5]))] == [False, True, False, False]
    assert evaluate_condition(leaf('exists'), huge, {}) is True
    # 超过解释器整数转换上限、无法 JSON 规范化的值按缺失处理（只在进程内直接求值时可达）。
    assert evaluate_condition(leaf('exists'), {'s': {'v': 10 ** 5000}}, {}) is False
    assert evaluate_condition(leaf('not_exists'), {'s': {'v': 10 ** 5000}}, {}) is True
    # 真实边界：类型化字面量在保存时结构化拒绝；旧形状保持原 ValueError 且不落盘；运行输入在求值前即被拒绝。
    with pytest.raises(WorkflowConditionError):
        save_node(tmp_path / 'typed', 'script_transform', config={'mapping': {}}, condition=leaf('eq', 10 ** 5000))
    with pytest.raises(ValueError):
        save_node(tmp_path / 'legacy', 'script_transform', config={'mapping': {}},
                  condition={'step_id': '$inputs', 'key': 'n', 'equals': 10 ** 5000})
    save_node(tmp_path / 'run', 'script_transform', config={'mapping': {'value': {'step_id': '$inputs', 'key': 'n'}}},
              condition={'op': 'exists', 'ref': {'step_id': '$inputs', 'key': 'n'}})
    engine = WorkflowEngine(tmp_path / 'run')
    assert engine.debug_step('debug-flow', 'target', mock_inputs={'n': 10 ** 2000})['condition_branch'] is True
    with pytest.raises(ValueError):
        engine.debug_step('debug-flow', 'target', mock_inputs={'n': 10 ** 5000})


def test_store_rejects_invalid_typed_condition_and_engine_branches_on_valid_one(tmp_path):
    from agent_py_agent.agent.workflows import WorkflowEngine
    from agent_py_agent.tests.test_workflow_engine import save_node

    with pytest.raises(WorkflowConditionError):
        save_node(tmp_path, 'script_transform', config={'mapping': {}}, condition=leaf('regex', '(?=a)a'))
    save_node(tmp_path, 'script_transform', config={'mapping': {'value': {'step_id': '$inputs', 'key': 'n'}}},
              condition={'op': 'and', 'conditions': [
                  {'op': 'gte', 'ref': {'step_id': '$inputs', 'key': 'n'}, 'value': 3},
                  {'op': 'regex', 'ref': {'step_id': '$inputs', 'key': 'tag'}, 'value': '^ok'}]})
    engine = WorkflowEngine(tmp_path)
    matched = engine.debug_step('debug-flow', 'target', mock_inputs={'n': 4, 'tag': 'ok-1'})
    assert matched['condition_branch'] is True and matched['output_data'] == {'value': 4}
    skipped = engine.debug_step('debug-flow', 'target', mock_inputs={'n': 2, 'tag': 'ok-1'})
    assert skipped['condition_branch'] is False and skipped['status'] == 'skipped'
