"""摘要等待可观察、覆盖数不虚增，退出后不再发表旧进度。"""
import threading
from contextvars import ContextVar

from agent_py_agent.agent.conversation import compact_progress_pulse as module


def test_pulse_repeats_real_coverage_and_preserves_context():
    value = ContextVar('progress_owner', default='missing')
    token = value.set('owner-a')
    received = threading.Event()
    rows = []

    def report(covered, total):
        rows.append((covered, total, value.get()))
        if len(rows) >= 2:
            received.set()

    try:
        with module.compact_progress_pulse(report, interval=.01) as progress:
            progress(3, 10)
            assert received.wait(2)
        after = list(rows)
        assert after and all(row == (3, 10, 'owner-a') for row in after)
        assert progress.__self__.closed
        assert progress.__self__.call.fn is None
        assert rows == after
    finally:
        value.reset(token)


def test_pulse_zero_and_missing_callback_do_not_start_worker():
    before = module._SCHEDULER._thread
    with module.compact_progress_pulse(None, interval=5) as progress:
        assert progress is None
    with module.compact_progress_pulse(lambda *_: None, interval=0):
        assert module._SCHEDULER._thread is before


def test_pulse_failure_unsubscribes_and_never_reports_terminal():
    import pytest

    seen = threading.Event()
    with pytest.raises(RuntimeError, match='model failure'):
        with module.compact_progress_pulse(lambda *_: seen.set(), interval=.01) as progress:
            assert seen.wait(2)
            raise RuntimeError('model failure')
    assert progress.__self__.closed and progress.__self__.call.fn is None


def test_model_wait_emits_progress_without_changing_call_thread(monkeypatch):
    from agent_py_agent.agent.backends.base import ModelResponse
    from agent_py_agent.agent.conversation import compact_request_budget as budget
    from agent_py_agent.tests.test_compact_request_budget import _request

    waiting = threading.Event()
    reports = []
    caller = threading.get_ident()
    request = _request('等待模型返回的原始材料')
    request.agent.config.memory_compact_progress_heartbeat_seconds = .01

    def generate(candidate):
        assert threading.get_ident() == caller
        assert waiting.wait(2), '模型调用等待期间必须继续更新展示'
        return ModelResponse(text='摘要事实', backend='offline')

    def progress(covered, total):
        reports.append((covered, total))
        waiting.set()

    monkeypatch.setattr(budget, 'generate_auxiliary_model_response', generate)
    result = budget.generate_bounded_compact_response(request, source_progress=progress)
    assert result.text == '摘要事实'
    assert reports and set(reports) == {(0, 1)}
