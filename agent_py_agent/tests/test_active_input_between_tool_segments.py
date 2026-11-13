"""新输入在已执行段后优先进入原模型，剩余未启动调用保留配对而不执行。"""
import json
from threading import Event, Thread

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.base import EchoBackend
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling import BaseTool, ToolHandlerOutcome
from agent_py_agent.tests._tool_runtime_harness import (
    make_test_model_spec,
    make_test_runtime_policy,
)


@pytest.mark.parametrize("remaining", [1, 4])
def test_active_input_defers_only_unstarted_serial_tool_calls(tmp_path, remaining, monkeypatch):
    entered, release, second_started = Event(), Event(), Event()
    agent = SimpleAgent(AgentConfig(model_backend='echo'), tmp_path)

    first_path = agent.home_paths.owner_home_dir / 'first-effect.txt'
    second_path = agent.home_paths.owner_home_dir / 'second-effect.txt'
    real_write = agent.tools.tools['write_file'].execute
    def controlled_write(params):
        if params.get('path') == str(first_path):
            entered.set()
            assert release.wait(10)
        else:
            second_started.set()
        return real_write(params)
    monkeypatch.setattr(agent.tools.tools['write_file'], 'execute', controlled_write)
    requests = []
    class Backend(EchoBackend):
        def generate(self, prompt, on_chunk=None, **kwargs):
            requests.append(kwargs.get('messages', []))
            if len(requests) == 1:
                return ModelResponse(text='', backend=self.name, tool_use_blocks=[
                    {'id': 'step-one', 'name': 'write_file', 'input': {'path': str(first_path), 'content': 'once'}},
                    *({'id': 'step-two-' + str(index), 'name': 'write_file', 'input': {'path': str(second_path), 'content': 'deferred'}} for index in range(remaining))])
            return ModelResponse(text='已收到进度问题，第一步已经完成。', backend=self.name)
    agent.backend = Backend()
    outcome = {}
    def run():
        try:
            outcome['result'] = agent.run('执行两个步骤。', params=RunParams(
                request_id='input-boundary', run_id='input-boundary', task_id='input-boundary',
                allowed_tools=['write_file'], save=False,
                task_attributes={'repeated_failure_halt_threshold': 2}))
        except BaseException as exc:
            outcome['error'] = exc
    thread = Thread(target=run)
    thread.start()
    try:
        assert entered.wait(10)
        entry = agent.conversation_store.guidance.append_once({'target_type': 'task',
            'target_id': 'input-boundary', 'message': '请先回答当前进度。',
            'metadata': {'kind': 'active_turn_user_input', 'expected_turn_id': 'input-boundary'}},
            dedupe_key='input-between-segments')
        receipt = agent.conversation_store.guidance.receipt('input-between-segments')
        assert receipt.status == 'pending'
        assert len(requests) == 1  # 正在运行的工具不会被第二模型抢占。
        release.set()
        thread.join(15)
        assert not thread.is_alive() and 'error' not in outcome, outcome.get('error')
        assert not second_started.is_set()
        assert first_path.read_text() == 'once'
        assert not second_path.exists()
        assert outcome['result'].runtime_status == 'ok'
        archive = outcome['result'].archive_tool_calls
        assert archive[0]['ok'] is True
        deferred = [row for row in archive if row.get('error_code') == 'TURN_INPUT_PENDING']
        assert len(deferred) == remaining
        assert all(row['handler_executed'] is False for row in deferred)
        assert agent.conversation_store.guidance.receipt('input-between-segments').status == 'consumed'
        wire = json.dumps(requests[-1], ensure_ascii=False)
        assert '请先回答当前进度。' in wire
        assert 'TURN_INPUT_PENDING' in wire
        assert 'step-one' in wire and 'step-two' in wire
        assert entry.guidance_id
    finally:
        release.set()
        thread.join(15)


def test_input_after_parallel_segment_preserves_started_calls_and_defers_next_segment(tmp_path):
    started_one, started_two, release, marker_started = Event(), Event(), Event(), Event()
    agent = SimpleAgent(AgentConfig(model_backend='echo'), tmp_path)
    completed = []
    class ParallelWork(BaseTool):
        model_spec = make_test_model_spec('parallel_work', input_schema={'type': 'object',
            'properties': {'step': {'type': 'integer'}}, 'required': ['step'], 'additionalProperties': False})
        runtime_policy = make_test_runtime_policy('read_only', concurrency_mode='parallel_safe')
        def execute(self, params):
            (started_one if params['step'] == 1 else started_two).set()
            assert release.wait(10)
            completed.append(params['step'])
            return ToolHandlerOutcome('parallel_work', True, 'finished')
    class Marker(BaseTool):
        model_spec = make_test_model_spec('serial_marker', input_schema={'type': 'object',
            'properties': {}, 'additionalProperties': False})
        runtime_policy = make_test_runtime_policy('read_only', concurrency_mode='serial')
        def execute(self, params):
            marker_started.set()
            return ToolHandlerOutcome('serial_marker', True, 'finished')
    agent.tools.register(ParallelWork())
    agent.tools.register(Marker())
    requests = []
    class Backend(EchoBackend):
        def generate(self, prompt, on_chunk=None, **kwargs):
            requests.append(kwargs.get('messages', []))
            if len(requests) == 1:
                return ModelResponse(text='', backend=self.name, tool_use_blocks=[
                    {'id': 'parallel-one', 'name': 'parallel_work', 'input': {'step': 1}},
                    {'id': 'parallel-two', 'name': 'parallel_work', 'input': {'step': 2}},
                    {'id': 'serial-next', 'name': 'serial_marker', 'input': {}}])
            return ModelResponse(text='并行段已结束，新输入已收到。', backend=self.name)
    agent.backend = Backend()
    outcome = {}
    def run():
        try:
            outcome['result'] = agent.run('执行并行后串行步骤。', params=RunParams(
                request_id='parallel-input', run_id='parallel-input', task_id='parallel-input',
                allowed_tools=['parallel_work', 'serial_marker'], save=False))
        except BaseException as exc:
            outcome['error'] = exc
    thread = Thread(target=run)
    thread.start()
    try:
        assert started_one.wait(10) and started_two.wait(10)
        agent.conversation_store.guidance.append_once({'target_type': 'task', 'target_id': 'parallel-input',
            'message': '先处理新输入。', 'metadata': {'kind': 'active_turn_user_input',
                                                  'expected_turn_id': 'parallel-input'}},
            dedupe_key='parallel-segment-input')
        release.set()
        thread.join(15)
        assert not thread.is_alive() and 'error' not in outcome, outcome.get('error')
        assert sorted(completed) == [1, 2]
        assert not marker_started.is_set()
        wire = json.dumps(requests[-1], ensure_ascii=False)
        assert 'TURN_INPUT_PENDING' in wire
        assert all(identity in wire for identity in ['parallel-one', 'parallel-two', 'serial-next'])
        assert len(requests) == 2
    finally:
        release.set()
        thread.join(15)
