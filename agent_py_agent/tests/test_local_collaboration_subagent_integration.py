from __future__ import annotations

import json
import re

from agent_py_agent.agent.agent_core.orchestration.dispatch.params import DispatchParams
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    FakeDeliveryService,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


class _CreateChildBackend:
    name = "create-child"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    + json.dumps(
                        {
                            "tool": "create_subagents",
                            "goal": "本地子代理观察一条线索，并在需要主代理处理时上报。",
                            "defer_start": True,
                            "allowed_tools": ["raise_event", "submit_collaboration_result", "inspect_collaboration"],
                        },
                        ensure_ascii=False,
                    )
                    + "\n[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        return ModelResponse(text="已创建本地子代理，等待调度。", backend=self.name)


class _ChildRaisesMainEventBackend:
    name = "child-raises-main-event"

    def __init__(self) -> None:
        self.calls = 0
        self.seen_prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        self.seen_prompts.append(prompt)
        run_id = _run_id_from_prompt(prompt)
        if self.calls == 1:
            return _tool_call_response(self.name, _raise_event_call(run_id))
        return _subagent_result_response(self.name, _raise_event_result())


class _BackgroundWakeBackend:
    name = "background-wake"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.prompts.append(prompt)
        if "[natural-user-reply]" in prompt:
            assert '"open_count":' in prompt
            return ModelResponse(
                text="后台主代理已看到子代理事件，并准备继续调度。",
                backend=self.name,
            )
        assert "local_child_signal" in prompt
        assert "本地子代理发现需要主代理马上处理" in prompt
        return ModelResponse(text="后台主代理已看到子代理事件，并准备继续调度。", backend=self.name)


class _CreateCollaborationChildrenBackend:
    name = "create-collaboration-children"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return _tool_call_response(self.name, _create_collaboration_children_call())
        return ModelResponse(text="已创建两个本地协作子代理，等待调度。", backend=self.name)


class _ChildOpensCollaborationCaseBackend:
    name = "child-opens-collaboration-case"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        run_id = _run_id_from_prompt(prompt)
        if self.calls == 1:
            return _tool_call_response(self.name, _open_collaboration_call(run_id))
        if self.calls == 2:
            case_id = _json_field_from_prompt(prompt, "case_id")
            return _tool_call_response(self.name, _request_collaboration_call(case_id, run_id))
        case_id = _json_field_from_prompt(prompt, "case_id")
        request_id = _json_field_from_prompt(prompt, "request_id")
        return _subagent_result_response(self.name, _raise_collaboration_result(case_id, request_id))


class _ChildSubmitsCollaborationEvidenceBackend:
    name = "child-submits-collaboration-evidence"

    def __init__(self, *, case_id: str, request_id: str) -> None:
        self.case_id = case_id
        self.request_id = request_id
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        run_id = _run_id_from_prompt(prompt)
        if self.calls == 1:
            return _tool_call_response(self.name, _submit_collaboration_result_call(self.case_id, self.request_id, run_id))
        if self.calls == 2:
            return _tool_call_response(self.name, _update_request_call(self.case_id, self.request_id, run_id))
        return _subagent_result_response(self.name, _evidence_result())


class _BackgroundCollaborationWakeBackend:
    name = "background-collaboration-wake"

    def __init__(self, *, case_id: str) -> None:
        self.case_id = case_id
        self.calls = 0
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            assert "collaboration_case_closed" in prompt
            assert self.case_id in prompt
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    + json.dumps({"tool": "inspect_collaboration", "case_id": self.case_id}, ensure_ascii=False)
                    + "\n[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        if self.calls == 2:
            assert "artifact://local-source-b/evidence-1" in prompt
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"inspect_agent_tree"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        assert '"does_not_dispatch": true' in prompt
        return ModelResponse(text="后台主代理已读取协作 case 和代理树，准备继续调度。", backend=self.name)


def _tool_call_response(backend: str, payload: dict[str, object]) -> ModelResponse:
    text = "[TOOL_CALL]\n" + json.dumps(payload, ensure_ascii=False) + "\n[/TOOL_CALL]"
    return ModelResponse(text=text, backend=backend)


def _create_collaboration_children_call() -> dict[str, object]:
    return {
        "tool": "create_subagents",
        "goal": "并行创建两个本地子代理，完成一次带证据的协作闭环。",
        "defer_start": True,
        "items": [
            {
                "goal": "观察一条线索，打开协作 case，并请求另一个代理补证据。",
                "agent_name": "local-source-a",
                "allowed_tools": ["raise_collaboration", "raise_collaboration", "inspect_collaboration"],
            },
            {
                "goal": "收到协作请求后提交 refs-first 证据，并更新请求状态。",
                "agent_name": "local-source-b",
                "allowed_tools": ["submit_collaboration_result", "update_collaboration", "inspect_collaboration"],
            },
        ],
    }


def _raise_event_call(run_id: str) -> dict[str, object]:
    return {
        "tool": "raise_event",
        "task_id": run_id,
        "event_type": "local_child_signal",
        "summary": "本地子代理发现需要主代理马上处理的协作事件。",
        "urgency": "urgent",
        "source_agent_id": run_id,
        "requires_llm_report": True,
    }


def _raise_event_result() -> dict[str, object]:
    return {
        "status": "DONE",
        "summary": "已通过 raise_event 上报主代理。",
        "used_tools": ["raise_event"],
        "evidence_packets": [_raise_event_packet()],
        "artifacts": [],
        "tests": [],
        "next_actions": ["等待后台主代理处理 wake signal"],
    }


def _raise_event_packet() -> dict[str, object]:
    return {
        "id": "evpkt-child-signal",
        "claim": "子代理已经写入主代理唤醒事件。",
        "checked_scope": "conversation wake ledger",
        "evidence_refs": ["conversation://wake/local-child-signal"],
        "artifact_refs": [],
        "confidence": 0.9,
    }


def _subagent_result_response(backend: str, payload: dict[str, object]) -> ModelResponse:
    text = "[SUBAGENT_RESULT]\n" + json.dumps(payload, ensure_ascii=False) + "\n[/SUBAGENT_RESULT]"
    return ModelResponse(text=text, backend=backend)


def _open_collaboration_call(run_id: str) -> dict[str, object]:
    return {
        "tool": "raise_collaboration",
        "task_id": run_id,
        "title": "本地多代理协作 case",
        "summary": "一个子代理发现线索，需要另一个子代理补充证据。",
        "priority": "urgent",
        "created_by": run_id,
        "entities": {"signal": "local-open-world"},
        "required_capabilities": ["query"],
    }


def _request_collaboration_call(case_id: str, run_id: str) -> dict[str, object]:
    return {
        "tool": "raise_collaboration",
        "case_id": case_id,
        "requester_agent_id": run_id,
        "required_capabilities": ["query"],
        "question": "请围绕同一线索提交证据引用。",
        "target_agent_ids": ["local-source-b"],
    }


def _raise_collaboration_result(case_id: str, request_id: str) -> dict[str, object]:
    return {
        "status": "DONE",
        "summary": "已打开协作 case 并发起补证据请求。",
        "used_tools": ["raise_collaboration", "raise_collaboration"],
        "evidence_packets": [_case_request_packet(case_id, request_id)],
        "artifacts": [],
        "tests": [],
    }


def _case_request_packet(case_id: str, request_id: str) -> dict[str, object]:
    return {
        "id": "local-source-a-case-request",
        "claim": "第一个子代理已打开协作 case 并发起请求。",
        "checked_scope": "collaboration case",
        "evidence_refs": [f"collaboration://case/{case_id}", f"collaboration://request/{request_id}"],
        "artifact_refs": [],
        "confidence": 0.8,
    }


def _submit_collaboration_result_call(case_id: str, request_id: str, run_id: str) -> dict[str, object]:
    return {
        "tool": "submit_collaboration_result",
        "case_id": case_id,
        "request_id": request_id,
        "source_agent_id": run_id,
        "matched": True,
        "summary": "第二个本地子代理提交了一条结构化证据引用。",
        "evidence_refs": ["artifact://local-source-b/evidence-1"],
        "confidence": 0.8,
    }


def _update_request_call(case_id: str, request_id: str, run_id: str) -> dict[str, object]:
    return {
        "tool": "update_collaboration",
        "case_id": case_id,
        "request_id": request_id,
        "status": "completed",
        "actor_agent_id": run_id,
        "summary": "证据引用已提交。",
    }


def _evidence_result() -> dict[str, object]:
    return {
        "status": "DONE",
        "summary": "已提交协作证据并更新请求状态。",
        "used_tools": ["submit_collaboration_result", "update_collaboration"],
        "evidence_packets": [_local_source_b_packet()],
        "artifacts": [],
        "tests": [],
    }


def _local_source_b_packet() -> dict[str, object]:
    return {
        "id": "local-source-b-evidence",
        "claim": "第二个子代理已提交证据引用。",
        "checked_scope": "collaboration case",
        "evidence_refs": ["artifact://local-source-b/evidence-1"],
        "artifact_refs": [],
        "confidence": 0.8,
    }


def test_main_run_created_child_is_bound_to_conversation_thread(tmp_path) -> None:
    agent = _agent(tmp_path)
    thread = _bind_thread(agent)
    agent.backend = _CreateChildBackend()

    agent.run(
        "请派一个本地子代理观察任务进展，有事叫醒你。",
        params=RunParams(
            task_id="root-task-1",
            allowed_tools=["create_subagents"],
            save=False,
            source="test",
        ),
    )

    child = agent.subagents.list_runs()[0]
    linked = agent.conversation_store.thread_for_task(child.id)

    assert linked is not None
    assert linked.thread_id == thread.thread_id
    assert child.attributes["conversation_thread_id"] == thread.thread_id
    assert child.attributes["conversation_task_id"] == "root-task-1"


def test_plain_local_run_materializes_thread_for_child_completion_wake(tmp_path) -> None:
    agent = _agent(tmp_path)
    agent.backend = _CreateChildBackend()

    agent.run(
        "请派一个本地子代理观察任务进展，有事叫醒你。",
        params=RunParams(
            task_id="root-task-local",
            allowed_tools=["create_subagents"],
            save=False,
            source="test",
        ),
    )
    child = agent.subagents.list_runs()[0]

    linked_parent = agent.conversation_store.thread_for_task("root-task-local")
    linked_child = agent.conversation_store.thread_for_task(child.id)

    assert linked_parent is not None
    assert linked_child is not None
    assert linked_child.thread_id == linked_parent.thread_id
    assert child.attributes["conversation_thread_id"] == linked_parent.thread_id
    assert child.attributes["conversation_task_id"] == "root-task-local"


def test_real_local_child_runner_event_wakes_background_main_agent_after_restart(tmp_path) -> None:
    agent = _agent(tmp_path)
    _bind_thread(agent)
    agent.backend = _CreateChildBackend()
    agent.run(
        "请派一个本地子代理观察任务进展，有事叫醒你。",
        params=RunParams(
            task_id="root-task-1",
            allowed_tools=["create_subagents"],
            save=False,
            source="test",
        ),
    )
    child = agent.subagents.list_runs()[0]
    child_backend = _ChildRaisesMainEventBackend()
    agent.backend = child_backend
    agent._subagent_worker_backend_override = child_backend

    report = agent.dispatch_subagents(
        None,
        params=DispatchParams(
            apply=True,
            start_runners=True,
            include_run_ids=[child.id],
            max_runners=1,
            probe=False,
        ),
    )

    assert report.records[0].ok is True
    assert agent.conversation_store.pending_wake_signals()
    restarted = _agent(tmp_path)
    restarted.backend = _BackgroundWakeBackend()
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(
        agent=restarted,
        store=restarted.conversation_store,
        channels=channels,
    )
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': restarted.conversation_store})

    reports = scheduler.tick(now=100.0)

    assert len(reports) == 1
    assert reports[0].response == "后台主代理已看到子代理事件，并准备继续调度。"
    assert restarted.conversation_store.pending_wake_signals() == []
    assert channels.adapter("internal").sent_messages[0].content == reports[0].response


def test_real_local_children_collaborate_and_wake_background_main_agent(tmp_path) -> None:
    agent = _agent(tmp_path)
    _bind_thread(agent)
    opener, responder = _create_collaboration_children(agent)
    open_report, evidence_report, case_id = _run_local_collaboration_children(agent, opener, responder)

    assert open_report.records[0].ok is True
    assert evidence_report.records[0].ok is True
    assert agent.conversation_store.thread_for_task(opener.id) is not None
    assert agent.conversation_store.thread_for_task(responder.id) is not None
    status = agent.collaboration_store.case_status(case_id)
    assert status["completed_request_count"] == 1
    assert status["evidence_count"] == 1
    assert status["ready_for_main_agent"] is True

    background, channels, reports = _run_background_collaboration_wake(tmp_path, case_id)

    assert len(reports) == 1
    assert background.calls == 3
    assert all("task-progress-completion-check" not in prompt for prompt in background.prompts)
    assert reports[0].response == "后台主代理已读取协作 case 和代理树，准备继续调度。"
    assert reports[0].delivery_status == "suppressed"
    assert reports[0].delivery_reason == "root_task_still_active"
    assert channels.adapter("internal").sent_messages == []


def _create_collaboration_children(agent: SimpleAgent):
    agent.backend = _CreateCollaborationChildrenBackend()
    agent.run(
        "请派两个本地子代理协作，有证据后叫醒你看 case 和代理树。",
        params=RunParams(task_id="root-task-1", allowed_tools=["create_subagents"], save=False, source="test"),
    )
    return sorted(agent.subagents.list_runs(), key=lambda item: item.agent_name)


def _run_local_collaboration_children(agent: SimpleAgent, opener, responder):
    open_report = _dispatch_one_child(agent, opener.id, _ChildOpensCollaborationCaseBackend())
    case_id = agent.collaboration_store.list_cases()[0].case_id
    request_id = agent.collaboration_store.case_status(case_id)["requests"][0]["request_id"]
    evidence_backend = _ChildSubmitsCollaborationEvidenceBackend(case_id=case_id, request_id=request_id)
    evidence_report = _dispatch_one_child(agent, responder.id, evidence_backend)
    return open_report, evidence_report, case_id


def _run_background_collaboration_wake(tmp_path, case_id: str):
    restarted = _agent(tmp_path)
    background = _BackgroundCollaborationWakeBackend(case_id=case_id)
    restarted.backend = background
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=restarted, store=restarted.conversation_store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': restarted.conversation_store, 'collaboration_store': restarted.collaboration_store})
    return background, channels, scheduler.tick(now=200.0)


def _dispatch_one_child(agent: SimpleAgent, run_id: str, backend):
    agent.backend = backend
    agent._subagent_worker_backend_override = backend
    return agent.dispatch_subagents(
        None,
        params=DispatchParams(
            apply=True,
            start_runners=True,
            include_run_ids=[run_id],
            max_runners=1,
            probe=False,
        ),
    )


def _agent(tmp_path) -> SimpleAgent:
    return SimpleAgent(AgentConfig(enable_tools=True, memory_path="memory.jsonl"), tmp_path)


def _bind_thread(agent: SimpleAgent):
    thread = agent.conversation_store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    agent.conversation_store.bind_task({'thread_id': thread.thread_id, 'task_id': "root-task-1", 'goal': "本地多代理协作任务", 'now': 2.0})
    return thread


def _run_id_from_prompt(prompt: str) -> str:
    match = re.search(r'"run_id":\s*"([^"]+)"', prompt)
    assert match is not None
    return match.group(1)


def _json_field_from_prompt(prompt: str, field: str) -> str:
    matches = re.findall(rf'"{re.escape(field)}":\s*"([^"]+)"', prompt)
    assert matches
    for value in reversed(matches):
        if value.startswith(f"{field.split('_')[0]}-"):
            return value
    return matches[-1]
