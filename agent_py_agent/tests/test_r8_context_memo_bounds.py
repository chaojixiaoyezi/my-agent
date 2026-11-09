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
    assert len(calls) <= pressure._CONTEXT_MEMO_KEY_MAX_NODES


@pytest.mark.parametrize('memo_available', [False, True])
def test_uncacheable_projection_does_not_construct_output_key_and_keeps_full_history(monkeypatch, memo_available):
    from agent_py_agent.agent.agent_core.model import context_estimate_cache

    _agent, params = _context_case()
    params.tool_ir_history = [UserTurn('x' * 300_000)]
    memo = ContextEstimateMemo()
    memo.remember_prompt_projection(('old', 'old-output', 'old-prompt', ()))
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
    memo.remember_prompt_projection(('old-key', 'old-output-key', leaf, ()))
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
