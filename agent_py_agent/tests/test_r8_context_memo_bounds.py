"""临时memo键有界，不裁剪真实历史与估算输入。"""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model import context_pressure as pressure
from agent_py_agent.agent.agent_core.model.context_estimate_cache import ContextEstimateMemo
from agent_py_agent.agent.backends.tool_ir import UserTurn
from agent_py_agent.tests.test_model_latency_q2 import _context_case


def test_large_leaf_and_wide_identity_tree_decline_cache_with_bounded_visits(monkeypatch):
    assert pressure._context_value_fingerprint('x' * 2_000_000) is None
    calls = []
    original = pressure._context_value_fingerprint

    def observed(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(pressure, '_context_value_fingerprint', observed)
    assert observed({str(i): i for i in range(100_000)}) is None
    assert len(calls) == 1


@pytest.mark.parametrize('memo_available', [False, True])
def test_uncacheable_projection_does_not_construct_output_key_and_keeps_full_history(monkeypatch, memo_available):
    from agent_py_agent.agent.agent_core.model import context_estimate_cache

    _agent, params = _context_case()
    params.tool_ir_history = [UserTurn('x' * 300_000)]
    memo = ContextEstimateMemo()
    memo.remember_prompt_projection(('old', 'old-output', 'old-prompt', ()), revision=memo.context_revision())
    monkeypatch.setattr(context_estimate_cache, 'estimate_memo_for', lambda params: memo if memo_available else None)
    calls = []
    original = pressure._prompt_history_projection_key

    def tracked(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(pressure, '_prompt_history_projection_key', tracked)
    prompt, history = pressure.project_request_prompt_history(params, 'complete prompt')
    assert prompt == 'complete prompt' and history == params.tool_ir_history
    assert len(history[0].text) == 300_000
    assert len(calls) == int(memo_available)
    if memo_available:
        assert memo.prompt_projection() is None


def test_forget_releases_old_values_and_does_not_republish_inflight_result():
    memo = ContextEstimateMemo()
    entered, release = Event(), Event()

    def compute():
        entered.set()
        assert release.wait(3)
        return 'native', 12, {'complete': 12}, 'same'

    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(memo.evaluate, ('old-key',), compute)
        assert entered.wait(3)
        memo.forget_context()
        release.set()
        assert future.result(3)[1] == 12
    assert memo._last is None
    assert memo.evaluate(('new-key',), lambda: ('native', 15, {}, 'new'))[1] == 15


def test_uncacheable_input_releases_previous_projection_leaf_without_gc():
    import weakref

    class Leaf(str):
        pass

    _agent, params = _context_case()
    leaf = Leaf('previous complete projection')
    reference = weakref.ref(leaf)
    memo = ContextEstimateMemo()
    params._context_estimate_memo = memo
    memo.remember_prompt_projection(('old-key', 'old-output-key', leaf, ()), revision=memo.context_revision())
    del leaf
    assert reference() is not None
    params.tool_ir_history = [UserTurn('x' * 300_000)]
    pressure.project_request_prompt_history(params, 'next complete prompt')
    assert reference() is None
    assert memo.prompt_projection() is None


def test_large_history_estimate_still_counts_complete_content():
    agent, params = _context_case()
    small = pressure._model_visible_context_components(agent, params, 'prompt')[1]
    params.tool_ir_history = [UserTurn('x' * 300_000 + 'END-OF-COMPLETE-HISTORY')]
    large = pressure._model_visible_context_components(agent, params, 'prompt')[1]
    assert large > small + 50_000
    assert params._context_estimate_memo._last is None
    assert params._context_estimate_memo.prompt_projection() is None


def test_large_projection_forgets_once_and_late_small_projection_cannot_republish(monkeypatch):
    from copy import copy

    from agent_py_agent.agent.agent_core import tool_ir_history

    _agent, params = _context_case()
    memo = ContextEstimateMemo()
    params._context_estimate_memo = memo
    large = copy(params)
    large.tool_ir_history = [UserTurn('x' * 300_000)]
    entered, release = Event(), Event()
    original = tool_ir_history.project_native_prompt_history
    original_forget = memo.forget_context
    forgets = []

    def tracked_forget():
        forgets.append(1)
        original_forget()

    def projected(value, prompt, **kwargs):
        if prompt == 'short complete prompt':
            entered.set()
            assert release.wait(3)
        return original(value, prompt, **kwargs)

    monkeypatch.setattr(memo, 'forget_context', tracked_forget)
    monkeypatch.setattr(tool_ir_history, 'project_native_prompt_history', projected)
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(pressure.project_request_prompt_history, params, 'short complete prompt')
        assert entered.wait(3)
        try:
            big_prompt, big_history = pressure.project_request_prompt_history(large, 'large complete prompt')
            assert big_prompt == 'large complete prompt' and len(big_history[0].text) == 300_000
        finally:
            release.set()
        small_prompt, small_history = pending.result(3)
    assert small_prompt == 'short complete prompt' and small_history == params.tool_ir_history
    assert memo.prompt_projection() is None
    assert forgets == [1]
    # 同请求完整key重新核验后，新一代仍可正常发表，未关闭缓存能力。
    pressure.project_request_prompt_history(params, 'short complete prompt')
    assert memo.prompt_projection() is not None


def test_key_accounting_does_not_allocate_temporary_size_tuples(monkeypatch):
    import sys

    source = {'history': [{'role': 'user', 'text': 'complete'}], 'number': float('nan')}
    expected = pressure._context_value_fingerprint(source)
    tuple_queries = []

    def measured(value):
        if type(value) is tuple:
            tuple_queries.append(value)
        return sys.getsizeof(value)

    monkeypatch.setattr(pressure, 'sys', SimpleNamespace(getsizeof=measured))
    assert pressure._context_value_fingerprint(source) == expected
    assert tuple_queries == []


def test_large_estimate_preserves_cacheable_prompt_projection(monkeypatch):
    from agent_py_agent.agent.agent_core import tool_ir_history

    agent, params = _context_case()
    params.provider_history_messages = [{'role': 'user', 'content': 'x' * 300_000}]
    original = tool_ir_history.project_native_prompt_history
    calls = []

    def projected(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(tool_ir_history, 'project_native_prompt_history', projected)
    results = [pressure._model_visible_context_components(agent, params, 'small prompt') for _ in range(3)]
    assert results[0] == results[1] == results[2]
    assert results[0][1] > 50_000
    assert calls == [1]
    assert params._context_estimate_memo.prompt_projection() is not None
    assert params._context_estimate_memo._last is None


def test_estimate_invalidation_fences_only_estimate_producer():
    memo = ContextEstimateMemo()
    revision = memo.context_revision()
    entered, release = Event(), Event()

    def compute():
        entered.set()
        assert release.wait(3)
        return 'native', 12, {}, 'exact'

    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(memo.evaluate, ('estimate',), compute)
        assert entered.wait(3)
        try:
            memo.forget_estimate()
            memo.remember_prompt_projection(('key', 'output-key', 'complete', ()), revision=revision)
            assert memo.prompt_projection() is not None
        finally:
            release.set()
        assert pending.result(3)[1] == 12
    assert memo._last is None
