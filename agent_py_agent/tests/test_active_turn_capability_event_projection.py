"""活动父代理的能力事件保留原申请身份及父级权限事实，不签发权限。"""
import json
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.runtime.guidance import inject_pending_turn_input
from agent_py_agent.agent.conversation.models import WakeSignal


def test_active_parent_receives_original_capability_context_without_private_metadata(monkeypatch):
    from agent_py_agent.agent.agent_core.runtime import guidance

    snapshot = {'id': 'capreq', 'requested_tools': ['read_file'], 'status': 'OPEN'}
    ceiling = {'all_requested_tools_grantable': False, 'unavailable_tools': ['read_file']}
    event = WakeSignal(wake_signal_id='wake', thread_id='thread', root_task_id='task',
        source_agent_id='child', parent_agent_id='parent', reason='subagent_capability_request_open',
        urgency='urgent', metadata={'request_id': 'capreq', 'capability_request': snapshot,
                                   'parent_tool_authority': ceiling, 'private_marker': 'do not project'})
    monkeypatch.setattr(guidance, 'inject_pending_guidance', lambda *_a, **_k: False)
    monkeypatch.setattr(guidance, '_has_unseen_direct_child_result', lambda *_a: False)
    monkeypatch.setattr(guidance, '_pending_task_events', lambda *_a: [event])
    params = SimpleNamespace(tool_context=[], runtime_injections=[], live_archive_state={})
    assert inject_pending_turn_input(SimpleNamespace(), params)
    payload = json.loads(params.tool_context[0].splitlines()[-1])['events'][0]
    assert payload['metadata'] == {'request_id': 'capreq', 'capability_request': snapshot,
                                   'parent_tool_authority': ceiling}
    assert payload['parent_agent_id'] == 'parent'
    assert payload['urgency'] == 'urgent'
    assert 'private_marker' not in params.tool_context[0]
    assert 'grant' not in payload
    assert event.metadata['capability_request']['status'] == 'OPEN'


def test_non_capability_event_keeps_original_priority_without_synthesized_authority():
    from agent_py_agent.agent.agent_core.runtime.guidance import _task_event_payload

    event = WakeSignal(wake_signal_id='wake', thread_id='thread', root_task_id='task',
                       reason='subagent_runner_finished', urgency='normal',
                       metadata={'request_id': 'unrelated', 'parent_tool_authority': {}})
    payload = _task_event_payload(event)
    assert payload['urgency'] == 'normal'
    assert 'metadata' not in payload
