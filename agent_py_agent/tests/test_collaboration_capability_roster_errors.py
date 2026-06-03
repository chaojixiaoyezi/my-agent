from __future__ import annotations

import json

from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent


def test_auto_target_matching_reports_bad_capability_roster(tmp_path) -> None:
    agent = _agent_with_thread(tmp_path)
    agent.collaboration_store.capabilities_path.write_text("{not-json", encoding="utf-8")
    case_id = json.loads(
        agent.tools.tools["raise_collaboration"].execute(
            {
                "task_id": "task-1",
                "title": "能力名单损坏",
                "summary": "需要找合适的响应代理。",
                "created_by": "agent-a",
                "required_capabilities": ["query"],
            }
        ).output
    )["case_id"]

    result = agent.tools.tools["raise_collaboration"].execute(
        {
            "case_id": case_id,
            "requester_agent_id": "agent-a",
            "required_capabilities": ["query"],
            "question": "请找具备 query 能力的代理补充证据。",
        }
    )
    payload = json.loads(result.output)
    request = agent.collaboration_store.case_status(case_id)["requests"][-1]

    assert result.ok is True
    assert payload["target_agent_ids"] == []
    assert payload["capability_roster_load_error"]["context"] == "collaboration.agent_capabilities.read"
    assert request["metadata"]["capability_roster_load_error"]["path"] == str(
        agent.collaboration_store.capabilities_path
    )


def _agent_with_thread(tmp_path) -> SimpleAgent:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    agent.conversation_store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-1", "goal": "协作任务", "now": 2.0}
    )
    return agent
