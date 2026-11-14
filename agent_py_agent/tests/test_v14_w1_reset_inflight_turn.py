# LLM: 使用真实运行时、worker和本地执行入口，Event控制交错；仅Agent为有限的内存替身。
# 模块用途: 守住重置期间的回合责任、终态、队列清理与旧显示隔离。
from __future__ import annotations

import queue
import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.cli.chat_parts import tui_worker
from agent_py_agent.cli.chat_parts.plain_state import ChatJob
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime, TuiTurnSummary


# LLM: 队列仍执行真实计数，只用Event观察已消费两条任务，不用sleep猜测时序。
# 类用途: 在最后一条任务确认后通知测试线程。
class _ObservedQueue(queue.Queue):
    def __init__(self):
        super().__init__()
        self.drained = threading.Event()

    def task_done(self):
        super().task_done()
        if not self.unfinished_tasks:
            self.drained.set()


# LLM: 不连接模型或Gateway，run仍由原_worker_local_path调用，输出走真实adapter。
# 类用途: 用两个Event冻结首轮，让主线程准确插入reset和stop。
class _Agent:
    def __init__(self):
        self.config = SimpleNamespace(chat_history_max_turns=20)
        self.started = threading.Event()
        self.release = threading.Event()
        self.second = threading.Event()
        self.stopped = False

    def run(self, prompt, **kwargs):
        if prompt == 'old-input':
            self.started.set()
            assert self.release.wait(5), 'first turn release missing'
            kwargs['on_chunk'].write_model('old-visible', public_stream=True)
            if self.stopped:
                raise InterruptedError('confirmed local stop')
            text = 'old-reply'
        else:
            self.second.set()
            text = 'new-reply'
        return SimpleNamespace(response=text, prompt=prompt, runtime_status='ok', tool_rounds=0)


# LLM: 仅构造原worker依赖，历史、控制句柄、runtime与队列均共享实际引用。
# 函数用途: 启动有限测试worker并捕获其真实异常。
def _worker_case(*, show_prompt=False):
    runtime = TuiRuntime('reset-worker')
    agent = _Agent()
    jobs = _ObservedQueue()
    cfg = tui_worker.TuiWorkerConfig(jobs=jobs, state_lock=threading.RLock(),
        is_running_ref=[False], pending_jobs_ref=[2], running_prompt_ref=[''],
        running_request_id_ref=[''], running_started_at_ref=[0.0], agent=agent,
        args=SimpleNamespace(), paths=None, use_gateway=False, conversation_history=[],
        history_lock=threading.RLock(), build_history_context=lambda: '', assistant_outputs=[],
        last_token_estimate_ref=[0], stop_event=threading.Event(), tui_runtime=runtime)
    failures = []

    def work():
        try:
            tui_worker._tui_worker_body(cfg)
        except BaseException as exc:
            failures.append(exc)

    runtime.enqueue_prompt('old', 'old-input', queued=False)
    jobs.put(ChatJob(user='old-input', show_prompt=show_prompt, inject=[], prompt_files=[], request_id='old'))
    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    assert agent.started.wait(5)
    return runtime, cfg, agent, thread, failures


# LLM: 顺序为开始A、reset、A结束、开始B；最终总是退出本测试线程，不遗留后台运行。
# 函数用途: 验证reset不杀worker、不污染新历史，同时保留A自己的完成记录。
@pytest.mark.parametrize('resets,stop_first', [(1, False), (2, False), (1, True)])
def test_reset_inflight_worker_survives_and_next_reply_visible(resets, stop_first):
    runtime, cfg, agent, thread, failures = _worker_case()
    try:
        if stop_first:
            runtime.enqueue_active_turn_input('steer', 'supplement')
            runtime.promote_active_turn_inputs(('steer',), request_id='old')
            assert runtime.request_interrupt()
            assert cfg.local_run_ref[0].request_interrupt()
            agent.stopped = True
        for index in range(resets):
            runtime.publish_conversation_reset(f'thread-new-{index}')
        runtime.enqueue_prompt('new', 'new-input', queued=True)
        cfg.jobs.put(ChatJob(user='new-input', show_prompt=False, inject=[], prompt_files=[], request_id='new'))
        agent.release.set()
        assert agent.second.wait(5), f'worker stopped before second turn: {failures!r}'
        assert cfg.jobs.drained.wait(5)
        assert thread.is_alive()
        assert not failures
        assert cfg.jobs.unfinished_tasks == 0
        snapshot = runtime.store.snapshot()
        texts = [block.text for block in (*snapshot.stable_blocks, *snapshot.active_blocks)]
        assert 'new-reply' in texts
        assert not any('old-' in text for text in texts)
        assert cfg.conversation_history == [('new-input', 'new-reply')]
        assert cfg.assistant_outputs == ['new-reply']
        assert not snapshot.pending_steers and not snapshot.queued_inputs
        assert not cfg.is_running_ref[0] and cfg.local_run_ref[0] is None
        completion = runtime.completions.clocks['old'].completion
        assert completion.status == ('interrupted' if stop_first else 'completed')
        assert not runtime._turns and not runtime._retired_turns
    finally:
        agent.release.set()
        cfg.stop_event.set()
        thread.join(5)
        assert not thread.is_alive()


# LLM: reset在finalize之后不得使既有终态倒退；未知id仍是调用合同错误。
# 函数用途: 检查另一交错顺序、重复begin和未知完成。
def test_finalize_then_reset_keeps_terminal_and_unknown_id_raises():
    runtime = TuiRuntime('completed-reset')
    adapter = runtime.begin_turn('known')
    assert runtime.begin_turn('known') is adapter
    runtime.complete_turn('known', TuiTurnSummary(response_text='before'))
    done = runtime.completions.clocks['known'].completion
    runtime.publish_conversation_reset('next-thread')
    adapter.start()
    adapter.write_model('late', public_stream=True)
    adapter.finalize(TuiTurnSummary(ok=False, error='late'))
    assert runtime.completions.clocks['known'].completion is done
    assert runtime.store.snapshot().active_blocks == ()
    assert not runtime.store.snapshot().stable_blocks
    with pytest.raises(KeyError, match='not started'):
        runtime.complete_turn('never-started', TuiTurnSummary())


# LLM: 所有公开adapter输出入口都实际调用，不以一个context检查外推其它正文/工具路径。
# 函数用途: 验证退休adapter晚到事件与终态不进入新视图。
@pytest.mark.parametrize('late', ['model', 'thinking', 'tool', 'metrics', 'notice', 'unknown', 'consumed', 'submitted'])
def test_retired_adapter_never_publishes_after_two_resets(late):
    runtime = TuiRuntime('retired-events')
    turn = runtime.begin_turn('old')
    runtime.publish_conversation_reset('middle')
    assert runtime.begin_turn('old') is turn
    runtime.publish_conversation_reset('last')
    if late in {'consumed', 'submitted'}:
        runtime.enqueue_active_turn_input('late-id', 'new-thread-input')
    before = runtime.store.snapshot()
    if late == 'model':
        turn.write_model('old-text', public_stream=True)
    elif late == 'thinking':
        turn.write_thinking_delta('old-thinking')
    elif late == 'tool':
        turn.write_progress({'round': 1, 'call_index': 1, 'tool': 'read_file', 'phase': 'started'})
    elif late == 'metrics':
        turn.write_model_metrics({'schema': 'model_runtime_metrics.v1', 'input_tokens': 7000000})
    elif late == 'notice':
        turn.on_gateway_event({'kind': 'host_notice', 'notice': {'notice_id': 'old', 'text': 'old-notice'}})
    elif late in {'consumed', 'submitted'}:
        turn.on_gateway_event({'kind': f'active_turn_input_{late}', 'client_message_ids': ['late-id']})
    runtime.complete_turn('old', TuiTurnSummary(response_text='old-final', outcome_unknown=late == 'unknown'))
    assert turn._finished
    assert runtime.store.snapshot() == before
    assert not runtime._turns and not runtime._retired_turns


# LLM: reset保留的真实审批只能按原permission收口，不能恢复旧工具、正文或状态。
# 函数用途: 检查旧审批在reset后仍可决定或随原回合终结关闭。
@pytest.mark.parametrize('finish', ['decision', 'terminal', 'gateway'])
def test_review_reset_preserved_permission_can_resolve(finish):
    from agent_py_agent.agent.contracts.tool_approval import build_tool_approval_request
    from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
    runtime = TuiRuntime('permission-reset')
    turn = runtime.begin_turn('old-permission')
    request = build_tool_approval_request(ToolCall(call_id='call', tool_name='run_command',
        arguments={'command': 'printf fixture'}, source_protocol='native', schema_hash='sha256:fixture',
        run_id='run', turn_id='turn', attempt_id='attempt'), request_id='old-permission',
        round_number=1, call_index=1, description='fixture')
    decisions = []
    turn.configure_gateway_permission_sink(lambda approval, decision: decisions.append((approval, decision)))
    assert turn.on_gateway_event({'kind': 'permission_requested', 'permission': request.to_dict()})
    runtime.publish_conversation_reset('fresh-permission-thread')
    assert runtime.store.snapshot().permission.permission_id == request.permission_id
    if finish == 'decision':
        assert runtime.move_permission_selection(1)
        assert runtime.store.snapshot().permission.selected_index == 1
        assert runtime.toggle_permission_feedback()
        assert runtime.update_permission_feedback(request.permission_id, 'inspection only')
        assert runtime.store.snapshot().permission.feedback == 'inspection only'
        assert runtime.resolve_permission(request.permission_id, 'denied')
        assert len(decisions) == 1
    elif finish == 'gateway':
        assert turn.on_gateway_event({'kind': 'permission_resolved',
            'permission_id': request.permission_id, 'decision': 'denied'})
        assert runtime.store.snapshot().permission is None
    runtime.complete_turn('old-permission', TuiTurnSummary())
    assert runtime.store.snapshot().permission is None
    assert not runtime.store.snapshot().active_blocks
    assert not runtime.store.snapshot().stable_blocks


# LLM: 使用原local path与worker，prompt同正文一起受reset围栏。
# 函数用途: 复现show_prompt直接写共享runtime导致旧内容重现。
def test_review_local_show_prompt_is_discarded_after_reset():
    runtime, cfg, agent, thread, failures = _worker_case(show_prompt=True)
    try:
        runtime.publish_conversation_reset('fresh-prompt-thread')
        agent.release.set()
        assert cfg.jobs.drained.wait(5)
        assert not failures and thread.is_alive()
        assert not runtime.store.snapshot().stable_blocks
    finally:
        agent.release.set()
        cfg.stop_event.set()
        thread.join(5)
        assert not thread.is_alive()


# LLM: 独立Gateway结果同样必须拒绝旧回合的FINAL PROMPT。
# 函数用途: 检查Gateway诊断输出在reset后不进入新会话。
def test_review_gateway_show_prompt_is_discarded_after_reset():
    from agent_py_agent.cli.chat_parts.tui_worker_paths import _gateway_outcome
    runtime = TuiRuntime('gateway-prompt-reset')
    turn = runtime.begin_turn('old')
    runtime.publish_conversation_reset('fresh')
    ctx = SimpleNamespace(turn_adapter=turn, job=SimpleNamespace(show_prompt=True),
        cfg=SimpleNamespace(tui_runtime=runtime, state_lock=threading.RLock(), last_token_estimate_ref=[0]))
    _gateway_outcome(ctx, {'ok': True, 'prompt': 'old internal prompt', 'response': 'old response'})
    assert not runtime.store.snapshot().stable_blocks


# LLM: 执行中产生非reset编程错误仍传播，但cleanup必须真实清空refs和确认队列。
# 函数用途: 检查complete_turn抛错时finally不会跳过task_done。
def test_completion_error_still_cleans_worker_refs(monkeypatch):
    runtime, cfg, agent, thread, failures = _worker_case()
    try:
        def fail(*args):
            raise KeyError('simulated completion error')
        monkeypatch.setattr(runtime, 'complete_turn', fail)
        agent.release.set()
        thread.join(5)
        assert not thread.is_alive()
        assert len(failures) == 1 and isinstance(failures[0], KeyError)
        assert cfg.jobs.unfinished_tasks == 0
        assert not cfg.is_running_ref[0] and cfg.local_run_ref[0] is None
    finally:
        agent.release.set()
        cfg.stop_event.set()
        thread.join(5)
