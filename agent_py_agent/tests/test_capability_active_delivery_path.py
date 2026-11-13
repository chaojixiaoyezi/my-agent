"""能力申请从真实工具、原wake到活动父模型上下文及ACK，不自动裁决。"""
import json

from agent_py_agent.agent import runtime_context
from agent_py_agent.agent.agent_core.capability_request_tool import CapabilityRequestTool
from agent_py_agent.agent.agent_core.runtime.guidance import (
    acknowledge_injected_turn_input,
    inject_pending_turn_input,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_runtime_guidance import _tool_loop_params


def test_capability_request_proactively_reaches_active_parent_before_ack(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend='echo', enable_tools=True), tmp_path)
    store = agent.conversation_store
    thread = store.threads.create()
    child = agent.subagents.create_run(goal='测试申请', thought='验证投递', plan=['请求能力'],
        root_id='parent-task', parent_id='parent-task', allowed_tools=['capability_request'])
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': child.id, 'goal': 'child'})
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'parent-task', 'goal': 'parent'})
    context = runtime_context.set_current_subagent_context(agent, run_id=child.id)
    try:
        result = CapabilityRequestTool(agent).execute({'problem': '测试角色尚未提供读取工具',
            'needed_capability': 'read_file', 'capability_type': 'mcp', 'requested_mcp_tools': ['read_file']})
    finally:
        runtime_context.restore_current_subagent_context(agent, context)
    assert result.ok, result.output
    request = agent.subagents.load(child.id).capability_requests[0]
    wakes = store.wakes.pending()
    wake = next(w for w in wakes if w.reason == 'subagent_capability_request_open')
    assert wake.source_agent_id == child.id and wake.root_task_id == 'parent-task'
    assert wake.urgency == 'urgent'
    params = _tool_loop_params(task_id='parent-task', request_id='parent-turn',
                               task_attributes={'conversation_task_id': 'parent-task'})
    assert inject_pending_turn_input(agent, params)
    payload = json.loads(next(text for text in params.tool_context if text.startswith('[RUNTIME_TASK_EVENTS]')).splitlines()[-1])
    event = next(e for e in payload['events'] if e['wake_signal_id'] == wake.wake_signal_id)
    assert event['metadata']['request_id'] == request.id
    assert event['metadata']['capability_request']['requested_mcp_tools'] == ['read_file']
    assert 'parent_tool_authority' in event['metadata']
    assert any(w.wake_signal_id == wake.wake_signal_id for w in store.wakes.pending())
    assert acknowledge_injected_turn_input(agent, params) == 1
    assert not any(w.wake_signal_id == wake.wake_signal_id for w in store.wakes.pending())
    assert agent.subagents.load(child.id).capability_requests[0].status == 'OPEN'
