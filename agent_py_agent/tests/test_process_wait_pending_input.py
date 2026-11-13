"""等待后台进程时让出给新输入，保留原进程与同一终态回执。"""
import json
import time
from threading import Event, Thread

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.base import EchoBackend
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.background_process_launch import start_background_process
from agent_py_agent.agent.tooling.process_registry import process_registry
from agent_py_agent.agent.tooling.process_scope import ProcessAccessScope, ProcessExecutionScope
from agent_py_agent.agent.tooling.process_session_store import process_session_store_root
from agent_py_agent.tests._managed_process_harness import managed_request


def test_input_yields_wait_and_same_process_later_finishes(tmp_path, monkeypatch, record_testsuite_property):
    agent = SimpleAgent(AgentConfig(model_backend='echo'), tmp_path)
    owner = agent.home_paths.owner_home_dir
    thread_id = agent.conversation_store.threads.create().thread_id
    release = owner / 'release'
    marker = owner / 'started-once'
    code = ('from pathlib import Path\nimport time\n'
            f'p=Path({str(marker)!r}); p.write_text(p.read_text()+"x" if p.exists() else "x")\n'
            f'while not Path({str(release)!r}).exists(): time.sleep(0.02)\n'
            'print("same-process-finished", flush=True)\n')
    scope = ProcessAccessScope(agent.home_paths.owner_id, thread_id, str(owner))
    hosted = start_background_process(managed_request(owner / 'process-fixture', code,
        access_scope=scope, store_root=process_session_store_root(agent.root, owner), execution_scope=ProcessExecutionScope(str(owner), thread_id, 'f10-wait', 'f10-wait', '')))
    record = process_registry.attach(hosted.record, hosted.process, hosted.store_root)
    entered = Event()
    original_wait = process_registry.wait
    receipt_at_return = []

    def observed_wait(*args, **kwargs):
        entered.set()
        result = original_wait(*args, **kwargs)
        receipt = agent.conversation_store.guidance.receipt('wait-input')
        receipt_at_return.append(receipt.status if receipt else None)
        return result

    monkeypatch.setattr(process_registry, 'wait', observed_wait)

    class Backend(EchoBackend):
        requests = []
        def generate(self, prompt, on_chunk=None, **kwargs):
            self.requests.append(kwargs.get('messages', []))
            if len(self.requests) == 1:
                return ModelResponse(text='', backend=self.name, tool_use_blocks=[{
                    'id': 'wait-original', 'name': 'process_session', 'input': {
                        'action': 'wait', 'session_id': record.session_id, 'timeout_seconds': 30}}])
            return ModelResponse(text='已收到进度问题，原进程仍运行。', backend=self.name)

    agent.backend = Backend()
    outcome = {}
    def run():
        try:
            outcome['result'] = agent.run('等待当前命令', params=RunParams(run_id='f10-wait', task_id='f10-wait',
                request_id='f10-wait', save=False, allowed_tools=['process_session'],
                task_attributes={'conversation_thread_id': thread_id}))
        except BaseException as error:
            outcome['error'] = error

    thread = Thread(target=run)
    thread.start()
    try:
        assert entered.wait(10), (outcome.get('error'), [(r.get('error_code'), r.get('output_preview')) for r in getattr(outcome.get('result'), 'archive_tool_calls', [])])
        submitted = time.monotonic()
        agent.conversation_store.guidance.append_once({'target_type': 'task', 'target_id': 'f10-wait',
            'message': '请先回复当前进度', 'metadata': {'kind': 'active_turn_user_input', 'expected_turn_id': 'f10-wait'}},
            dedupe_key='wait-input')
        thread.join(5)
        assert not thread.is_alive() and 'error' not in outcome, outcome.get('error')
        elapsed = time.monotonic() - submitted
        assert elapsed < 5
        record_testsuite_property('input_to_return_seconds', elapsed)
        assert receipt_at_return == ['pending']
        assert hosted.process.poll() is None
        assert marker.read_text() == 'x'
        wire = json.dumps(agent.backend.requests[-1], ensure_ascii=False)
        assert 'turn_input_pending' in wire and '请先回复当前进度' in wire
        assert agent.conversation_store.guidance.receipt('wait-input').status == 'consumed'
        release.touch()
        finished = original_wait(record.session_id, 10, scope, hosted.store_root)
        assert finished['status'] == 'exited' and finished['exit_code'] == 0
        assert 'same-process-finished' in finished['output_tail']
        assert finished['session_id'] == record.session_id
        assert marker.read_text() == 'x'
    finally:
        release.touch()
        thread.join(10)
        process_registry.kill(record.session_id, scope, hosted.store_root)


def test_wait_cancellation_precedes_input_yield_and_keeps_process(tmp_path):
    import pytest

    from agent_py_agent.agent.common.cancellation import (
        CancellationToken,
        ToolCancelled,
        bind_cancellation_token,
    )

    hosted = start_background_process(managed_request(tmp_path))
    record = process_registry.attach(hosted.record, hosted.process, hosted.store_root)
    token = CancellationToken()
    token.cancel()
    try:
        with bind_cancellation_token(token), pytest.raises(ToolCancelled):
            process_registry.wait(record.session_id, 30, pending_input_probe=lambda: True)
        assert hosted.process.poll() is None
    finally:
        process_registry.kill(record.session_id)
