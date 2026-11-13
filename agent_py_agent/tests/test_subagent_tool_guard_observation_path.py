"""真实task-local工具循环共用规范观测，并将软提示送入下一原生模型请求。"""
import json

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.agent_core.tool_guard.call_guardrail import tool_guardrail_records
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.base import EchoBackend
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


def test_task_local_native_loop_observes_same_read_and_forwards_soft_hint(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend='echo'), tmp_path)
    run = agent.subagents.create_run(goal='读取测试文件', thought='核对', plan=['读取'], role='researcher')
    source = agent.home_paths.owner_home_dir / 'source.txt'
    source.write_text('stable test data', encoding='utf-8')
    prompts = []

    class Backend(EchoBackend):
        def generate(self, prompt, on_chunk=None, **kwargs):
            prompts.append(kwargs.get('messages', []))
            if len(prompts) <= 3:
                return ModelResponse(text='', backend=self.name, tool_use_blocks=[{
                    'id': 'read-' + str(len(prompts)), 'name': 'read_file', 'input': {'path': str(source)}}])
            return ModelResponse(text='已读取。', backend=self.name)

    agent.backend = Backend()
    params = RunParams(run_id=run.id, task_id=run.id, context_scope='task_local',
        allowed_tools=['read_file'], save=False,
        task_attributes={'readonly_no_progress_threshold': 0, 'repeated_success_hint_threshold': 3})
    result = agent.run('读取已提供文件。', params=params)
    records = [row for row in tool_guardrail_records(agent) if row['tool_name'] == 'read_file']
    assert len(records) == 3
    assert all(row['observed_success'] and row['run_id'] == run.id for row in records), records
    assert len({row['args_hash'] for row in records}) == len({row['result_hash'] for row in records}) == 1
    assert '[tool-loop-guardrail-hint]' in json.dumps(prompts[-1], ensure_ascii=False)
    assert result.response == '已读取。'
