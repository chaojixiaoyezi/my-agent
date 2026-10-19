"""普通本地委派：原Gateway/claim/core/child、只替供应商与IM运输，不启动服务或真实模型。"""

from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest

from agent_py_agent.agent.backends import http
from agent_py_agent.agent.capability.channel_message_tool import SendMessageTool
from agent_py_agent.agent.concurrency.interrupt import register_interruptible
from agent_py_agent.agent.conversation.control_commands import conversation_request_interrupt_name
from agent_py_agent.agent.conversation.im_delegation import prepare_delegated_execution
from agent_py_agent.agent.conversation.session_task_report import (
    TaskTurnOutcome,
    close_out_turn,
    report_task_result,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.delivery import ChannelCapabilities
from agent_py_agent.agent.gateway_parts import request_execution
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.im_dispatch_policy import (
    IM_DISPATCH_KEY,
    capture_im_dispatch_for_owner,
)
from agent_py_agent.agent.gateway_parts.recovery import terminalize_gateway_request_file
from agent_py_agent.agent.runtime_context import current_subagent_run_id
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.settings.im_task_preferences import set_delegation_preference
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.cli.gateway_loops import _build_background_scheduler
from agent_py_agent.tests.test_channel_message_tool import _RecordingAdapter
from agent_py_agent.tests.test_committed_model_messages import _commit_writer_fixture
from agent_py_agent.tests.test_im_delegation_runtime import runtime_fixture
from agent_py_agent.tests.test_session_task_real_chain import ScriptedWire, _ready_gateway_paths


# LLM: 所有Agent/Store/claim/core是真实产品；供应商fake只回普通正文/原probe，IM adapter只记录，不触网络。
# 函数用途: 建一个本人IM正常任务环境，分别记录真实执行会话与渠道运输。
def ordinary_chain(tmp_path, monkeypatch, *, owner_kind="user"):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="openai_compatible",
            api_base="http://127.0.0.1:9/v1",
            api_key="synthetic-not-credential",
            model_name="ordinary-offline",
            stream_enabled=False,
            model_context_window_tokens=200_000,
            enable_plugins=False,
            prompt_files=[],
            my_agent_home=str(tmp_path / "home"),
            my_agent_owner_provider="feishu",
            my_agent_owner_kind=owner_kind,
            my_agent_owner_id="oc_a" if owner_kind == "group" else "ou_a",
            gateway_per_user_owner_scoping=False,
        ),
        tmp_path / "workspace",
    )
    provider = _RecordingAdapter()
    agent.channel_registry.register_adapter(
        "feishu", provider, capabilities=ChannelCapabilities(text=True, reply=True, proactive=True)
    )
    calls = []
    physical = []
    blocked = []
    original_run = SimpleAgent.run

    def run(current, prompt, *, params):
        call = {
            "thread": (params.task_attributes or {}).get("conversation_thread_id"),
            "child": current_subagent_run_id(current),
            "actor": params.host_channel_actor,
        }
        first = len(physical)
        try:
            return original_run(current, prompt, params=params)
        finally:
            call["tools"] = physical[first:][-1] if len(physical) > first else []
            call["model_calls"] = len(physical) - first
            calls.append(call)

    monkeypatch.setattr(SimpleAgent, "run", run)

    def wire(request):
        names = [row.get("function", {}).get("name") for row in request.payload.get("tools", [])]
        if names == ["my_agent_capability_probe"]:
            return ScriptedWire()(request)
        physical.append(names)
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "合成资料已经核对。"},
                    "finish_reason": "stop",
                }
            ]
        }

    monkeypatch.setattr(http, "post_json", wire)
    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.im_task_routing._select_route",
        lambda *_: {"kind": "task", "package_refs": []},
    )
    from agent_py_agent.agent.agent_core.orchestration import lifecycle

    monkeypatch.setattr(lifecycle, "auto_start_tasks", lambda *_: {"status": "test-deferred"})
    return agent, _ready_gateway_paths(agent), provider, calls, blocked


# LLM: 输入沿原宿主偏好快照和完整worker，不从客户端伪造委派策略；同一真实主session后续聊天用新request。
# 函数用途: 在临时Gateway路径执行一个普通IM请求，不开HTTP服务。
def ask(agent, paths, key, prompt, *, actor="ou_a"):
    owner = (
        OwnerIdentity.provider_group("feishu", "oc_a")
        if agent.home_paths.owner_kind == "group"
        else OwnerIdentity.provider_user("feishu", "ou_a")
    )
    scope = GatewayControlScope(actor, "feishu", "oc_a", resolved_owner=owner)
    request = {
        "id": key,
        "kind": "ask",
        "prompt": prompt,
        "user_id": actor,
        "status": "processing",
        "turn_phase": "open",
        "execution_attempt_id": "attempt-" + key,
        "metadata": {"channel": "feishu", "message_id": "message-" + key},
        "conversation": {
            "canonical_user_id": actor,
            "channel": "feishu",
            "channel_user_id": actor,
            "channel_conversation_id": "oc_a",
        },
    }
    request[IM_DISPATCH_KEY] = capture_im_dispatch_for_owner(
        home_paths=agent.home_paths, config=agent.config, scope=scope, capability_root=agent.root
    )
    path = paths.processing / (key + ".json")
    path.write_text(json.dumps(request))
    with register_interruptible(conversation_request_interrupt_name(key)):
        response = request_execution._handle_gateway_request(agent, path)
    status = "done" if response.get("ok") else "failed"
    terminalize_gateway_request_file(
        paths,
        path,
        paths.done if status == "done" else paths.failed,
        key,
        conversation_store=agent.conversation_store,
        terminal_response={"id": key, "status": status, "ok": status == "done"},
    )
    assert response.get("ok"), json.dumps(response, ensure_ascii=False)
    return response


@pytest.mark.parametrize("owner_kind", ["user", "group"])
def test_actual_worker_child_and_sender_model_are_only_public_speaker(
    tmp_path, monkeypatch, owner_kind
):
    agent, paths, provider, calls, blocked = ordinary_chain(
        tmp_path, monkeypatch, owner_kind=owner_kind
    )
    set_delegation_preference(agent, "feishu", "ou_a", True)
    ask(agent, paths, "first", "整理工作区里的资料")
    tasks, errors = agent.conversation_store.session_tasks.list_report(limit=0)
    assert not errors and len(tasks) == 1
    task = tasks[0]
    assert len(calls) == 1 and calls[0]["thread"] == task.sender_thread_id and not calls[0]["tools"]
    assert calls[0]["model_calls"] == 1
    scheduler = _build_background_scheduler(agent, agent.delivery_service)
    scheduler.tick(now=time.time())
    child = agent.subagents.list_runs()[0]
    assert child.attributes["conversation_request_id"] == task.task_id
    assert not provider.messages  # 后台主首片不发额外受理/进度。
    set_delegation_preference(agent, "feishu", "ou_a", False)
    ask(agent, paths, "chat", "你好，继续聊聊", actor="ou_b" if owner_kind == "group" else "ou_a")
    if owner_kind == "user":
        assert calls[-1]["thread"] == task.sender_thread_id
    result = agent.run_subagent(child.id, dry_run=False, probe=False)
    assert result.ok, result
    assert "send_message" not in child.allowed_tools
    for index in range(8):
        scheduler.tick(now=time.time() + 30 + index * 30)
        if not agent.conversation_store.wakes.pending(limit=0):
            break
    current = agent.conversation_store.session_tasks.load(task.task_id)
    assert current.status == "done", {
        "task_status": current.status,
        "child": agent.subagents.load(child.id).status,
        "calls": [(c["thread"], bool(c["child"]), c.get("model_calls")) for c in calls],
        "wakes": [
            (w.reason, w.thread_id, w.root_task_id)
            for w in agent.conversation_store.wakes.pending(limit=0)
        ],
    }
    key = f"session_task_result:{task.task_id}:done"
    assert agent.conversation_store.guidance.receipt(key).status == "consumed"
    assert len(provider.messages) == 1, provider.messages
    final_calls = [c for c in calls[2:] if not c["child"] and c["thread"] == task.sender_thread_id]
    assert len(final_calls) == (
        2 if owner_kind == "user" else 1
    )  # 中途聊天与最终结果均在原sender；后台汇总属于独立work thread。
    assert final_calls[-1]["tools"] == []
    assert final_calls[-1]["actor"] is None
    assert provider.messages[0][0] == ("oc_a" if owner_kind == "group" else "ou_a")
    before = len(calls)
    scheduler.tick(now=time.time())
    scheduler.tick(now=time.time())
    assert len(provider.messages) == 1 and len(calls) == before


def test_terminal_report_partial_wake_retries_same_key_without_business_or_quota_duplicate(
    tmp_path, monkeypatch
):
    from agent_py_agent.agent.conversation import session_task_report as reports
    from agent_py_agent.agent.conversation.session_task_report import recover_delegated_result

    agent, params, task, _, _ = runtime_fixture(tmp_path, monkeypatch)
    prepare_delegated_execution(agent, params)
    store = agent.conversation_store
    original = store.wakes.raise_signal
    quota = []
    old_pair = reports._record_pair

    def record(*args):
        quota.append(args[1:])
        return old_pair(*args)

    monkeypatch.setattr(reports, "_record_pair", record)
    monkeypatch.setattr(
        store.wakes,
        "raise_signal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(BlockingIOError("ordinary wake busy")),
    )
    closed = close_out_turn(
        agent,
        task.task_id,
        TaskTurnOutcome(True, summary="原工作结果", result_refs=("output/report.md",)),
    )
    assert closed.status == "done"
    key = f"session_task_result:{task.task_id}:done"
    first = store.guidance.receipt(key)
    assert first and not store.wakes.delivery_receipt(task.sender_thread_id, key)
    monkeypatch.setattr(store.wakes, "raise_signal", original)
    recovered = recover_delegated_result(agent, task.target_thread_id)
    assert recovered.response == "原工作结果" and len(quota) == 1
    assert store.guidance.receipt(key).entry.guidance_id == first.entry.guidance_id
    published = store.wakes.delivery_receipt(task.sender_thread_id, key)
    assert published == "pending"
    signals = [s for s in store.wakes.pending(limit=0) if s.thread_id == task.sender_thread_id]
    assert len(signals) == 1
    store.wakes.mark_handled(signals[0].wake_signal_id)
    for _ in range(3):
        assert report_task_result(agent, store.session_tasks.load(task.task_id))
    assert not [s for s in store.wakes.pending(limit=0) if s.thread_id == task.sender_thread_id]
    assert store.wakes.delivery_receipt(task.sender_thread_id, key) == "handled"
    assert len(quota) == 1 and len(agent.subagents.list_runs()) == 1


def test_accepted_advance_busy_and_report_read_busy_never_ack_or_hide_failure(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.background_execution import BackgroundCapacityDeferred
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundRunRequest,
        _close_out_session_task_turn,
    )

    agent, params, task, _, _ = runtime_fixture(tmp_path, monkeypatch)
    prepare_delegated_execution(agent, params)
    request = BackgroundRunRequest(
        thread_id=task.target_thread_id, host_session_task_id=task.task_id
    )
    execution = SimpleNamespace(
        response="工作已结束", runtime_status="ok", error=None, delivery_artifacts=()
    )
    advance = agent.conversation_store.session_tasks.advance
    with monkeypatch.context() as patch:
        patch.setattr(
            agent.conversation_store.session_tasks,
            "advance",
            lambda *_a, **_k: (_ for _ in ()).throw(BlockingIOError("ordinary task busy")),
        )
        with pytest.raises(BackgroundCapacityDeferred):
            _close_out_session_task_turn(agent, request, execution, turn_id=task.task_id)
    assert agent.conversation_store.session_tasks.load(task.task_id).status == "accepted"
    assert agent.conversation_store.session_tasks.advance == advance
    _close_out_session_task_turn(agent, request, execution, turn_id=task.task_id)
    assert agent.conversation_store.session_tasks.load(task.task_id).status == "done"
    with monkeypatch.context() as patch:
        patch.setattr(
            agent.conversation_store.wakes,
            "delivery_receipt",
            lambda *_a: (_ for _ in ()).throw(OSError("ordinary read unavailable")),
        )
        with pytest.raises(BackgroundCapacityDeferred):
            _close_out_session_task_turn(agent, request, execution, turn_id=task.task_id)


@pytest.mark.parametrize("summary", ["", "只有原模型摘要"])
def test_terminal_recovery_never_calls_model_and_empty_summary_can_close(
    tmp_path, monkeypatch, summary
):
    from agent_py_agent.agent.conversation.runtime import BackgroundMainAgentRuntime

    agent, params, task, _, _ = runtime_fixture(tmp_path, monkeypatch)
    prepare_delegated_execution(agent, params)
    close_out_turn(
        agent,
        task.task_id,
        TaskTurnOutcome(True, summary=summary, result_refs=("output/original.md",)),
    )
    monkeypatch.setattr(
        agent, "run", lambda *_a, **_k: pytest.fail("terminal metadata recovery reexecuted model")
    )
    runtime = BackgroundMainAgentRuntime(
        agent=agent, store=agent.conversation_store, channels=agent.delivery_service
    )
    report = runtime.run_once(
        {
            "thread_id": task.target_thread_id,
            "reason": "session_task",
            "host_session_task_id": task.task_id,
            "now": time.time(),
        }
    )
    assert report.wake_handled and report.response == summary
    assert report.delivery_status != "sent"
    assert not agent.conversation_store.messages.recent(task.sender_thread_id, limit=0)


def test_ack_final_only_does_not_leak_into_next_request_or_rich_transcript(tmp_path):
    from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter

    one = BufferedChunkStreamWriter(tmp_path / "ack.jsonl")
    one.set_verbose_level("stream")
    one.set_final_only()
    one.write_model("已受理")
    _commit_writer_fixture(one, "已受理", call_id="test_ack_final_only_does_not_leak_into_next_request_or_rich_transcript:0")
    one.write("模型等待过程")
    one.flush()
    assert not one.chunk_path.exists()
    following = BufferedChunkStreamWriter(tmp_path / "chat.jsonl")
    following.set_verbose_level("stream")
    following.write_model("普通后续聊天")
    _commit_writer_fixture(following, "普通后续聊天", call_id="test_ack_final_only_does_not_leak_into_next_request_or_rich_transcript:1")
    following.flush()
    rows = [json.loads(line) for line in following.chunk_path.read_text().splitlines()]
    assert any(row["kind"] == "assistant_commentary" for row in rows)
    assert not any(row["kind"] in {"model_delta", "model_draft"} for row in rows)
    rich = BufferedChunkStreamWriter(tmp_path / "rich.jsonl", rich_transcript=True)
    rich.set_final_only()
    rich.write_model("富客户端内部过程\n")
    _commit_writer_fixture(rich, "富客户端内部过程\n", call_id="test_ack_final_only_does_not_leak_into_next_request_or_rich_transcript:2")
    assert rich.chunk_path.exists()


def test_failed_actual_handoff_never_samples_success_ack(tmp_path, monkeypatch):
    agent, paths, provider, calls, _ = ordinary_chain(tmp_path, monkeypatch)
    set_delegation_preference(agent, "feishu", "ou_a", True)
    monkeypatch.setattr(
        agent.conversation_store.session_tasks,
        "create",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("ordinary task queue unavailable")),
    )
    with pytest.raises(AssertionError):
        ask(agent, paths, "queue-failed", "整理资料")
    assert not calls and not provider.messages
    assert not agent.conversation_store.session_tasks.list_report(limit=0)[0]


def test_actual_child_missing_root_is_unknown_but_ordinary_child_stays_ordinary(
    tmp_path, monkeypatch
):
    from agent_py_agent.agent.conversation.im_delegation_delivery import (
        current_delegated_delivery_task,
    )
    from agent_py_agent.agent.runtime_errors import DataCorruptionError
    from agent_py_agent.tests.test_host_message_context import select_child

    agent, params, _, _, source = runtime_fixture(tmp_path, monkeypatch)
    prepare_delegated_execution(agent, params)
    child = agent.subagents.list_runs()[0]
    select_child(agent, child)
    with monkeypatch.context() as patch:
        patch.setattr(agent.conversation_store.tasks, "load", lambda *_: None)
        with pytest.raises(DataCorruptionError):
            current_delegated_delivery_task(agent)
    root = agent.conversation_store.tasks.bind(
        {"task_id": "ordinary-root", "thread_id": source.thread_id, "status": "active"}
    )
    ordinary = agent.subagents.create_run(
        goal="普通工作",
        root_id=root.task_id,
        parent_id=root.task_id,
        owner=agent.home_paths.owner_id,
        attributes={"conversation_thread_id": source.thread_id},
    )
    from agent_py_agent.agent.runtime_context import (
        restore_current_subagent_context,
        set_current_subagent_context,
    )

    previous = set_current_subagent_context(agent, run_id=ordinary.id)
    try:
        assert current_delegated_delivery_task(agent) is None
    finally:
        restore_current_subagent_context(agent, previous)


def test_child_final_send_gate_uses_original_attempt_and_ordinary_send_unchanged(
    tmp_path, monkeypatch
):
    from agent_py_agent.agent.runtime_context import (
        restore_current_subagent_context,
        set_current_subagent_context,
    )
    from agent_py_agent.tests import test_im_delegation_runtime as fixtures
    from agent_py_agent.tests.test_host_message_context import select_child

    owned, _, provider, _, _ = ordinary_chain(tmp_path, monkeypatch)
    monkeypatch.setitem(
        fixtures.runtime_fixture.__globals__, "SimpleAgent", lambda *_a, **_k: owned
    )
    agent, params, _, _, _ = fixtures.runtime_fixture(tmp_path, monkeypatch)
    prepare_delegated_execution(agent, params)
    child = agent.subagents.list_runs()[0]

    active = agent.subagents.lifecycle.prepare_runner_attempt(child.id)
    set_current_subagent_context(agent, run_id=child.id, attempt_id=active.runner_active_attempt_id)
    denied = SendMessageTool(agent).execute({"message": "孩子尝试报告普通结果"})
    assert not denied.ok and denied.error_code == "CHANNEL_TARGET_OUT_OF_SCOPE"
    assert not provider.messages
    previous = set_current_subagent_context(agent, run_id="")
    try:
        agent._current_run_params = None
        sent = SendMessageTool(agent).execute({"message": "普通本人消息"})
        assert sent.ok, sent.output
        assert len(provider.messages) == 1
    finally:
        restore_current_subagent_context(agent, previous)


def test_result_wake_busy_sender_consumes_exact_guidance_without_extra_claim(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.runtime.guidance import (
        acknowledge_injected_turn_input,
        inject_pending_guidance,
    )
    from agent_py_agent.agent.conversation.runtime import _session_message_consumed_admission
    from agent_py_agent.tests.test_runtime_guidance import _tool_loop_params

    agent, params, task, _, source = runtime_fixture(tmp_path, monkeypatch)
    prepare_delegated_execution(agent, params)
    close_out_turn(agent, task.task_id, TaskTurnOutcome(True, summary="原工作结果"))
    key = f"session_task_result:{task.task_id}:done"
    wake = next(
        s
        for s in agent.conversation_store.wakes.pending(limit=0)
        if s.thread_id == source.thread_id
    )
    assert (
        _session_message_consumed_admission(wake, guidance_store=agent.conversation_store.guidance)
        == ""
    )
    current = _tool_loop_params(
        task_attributes={
            "conversation_thread_id": source.thread_id,
            "conversation_request_id": "ongoing-main",
            "conversation_task_turn_active": True,
        }
    )
    inject_pending_guidance(agent, current)
    entry = agent.conversation_store.guidance.receipt(key)
    assert entry.status == "reserved"
    assert (
        _session_message_consumed_admission(wake, guidance_store=agent.conversation_store.guidance)
        == ""
    )
    from agent_py_agent.agent.agent_core.runtime.guidance import mark_injected_turn_input_submitted

    mark_injected_turn_input_submitted(agent, current, provider_call_id="ordinary-main-call")
    acknowledge_injected_turn_input(agent, current)
    assert agent.conversation_store.guidance.receipt(key).status == "consumed"
    assert (
        _session_message_consumed_admission(wake, guidance_store=agent.conversation_store.guidance)
        == "session_message_consumed"
    )
    assert agent.conversation_store.claims.load(source.thread_id) == {}


# 原fixture只贡献真实W快照/租约/P3；测试业务对象另按同typed owner构造真实SimpleAgent。
from agent_py_agent.tests.test_gateway_multiprocess_worker_agent import worker as worker_case

_FIXTURES = (worker_case,)


def test_positive_count_report_runs_inside_original_worker_and_live_claim(
    worker_case, tmp_path, monkeypatch
):
    from agent_py_agent.agent.capability.config import CapabilityConfig
    from agent_py_agent.agent.conversation.background_claim import (
        BackgroundClaimDependencies,
        run_with_heartbeat,
    )
    from agent_py_agent.agent.conversation.claim_execution_binding import current_claim_execution
    from agent_py_agent.agent.conversation.execution_threads import (
        IM_TASK_EXECUTION_KEY,
        ensure_execution_thread,
    )
    from agent_py_agent.agent.conversation.session_task_dispatch import (
        dispatch_owned_background_task,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
    from agent_py_agent.agent.gateway_parts.multiprocess_background_execution import (
        background_request_scope,
        current_background_request,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_dispatch_binding import (
        bind_claimed_request,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_dispatch_claim import (
        ClaimCandidate,
        freeze_claim_snapshot,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_execution import (
        current_worker_execution,
        worker_execution_scope,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_route import (
        RouteStamp,
        freeze_request_route,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_worker_agent import create_worker_pool
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.queue_service import (
        claim_request,
        ensure_gateway_folders,
    )
    from agent_py_agent.agent.user_space.owner_resolver import ensure_owner_home
    from agent_py_agent.tests.support.multiprocess_dispatch_cases import (
        reservation_for,
        stamped_request,
    )

    frozen, config, root, owner, _other = worker_case
    ensure_owner_home(frozen.launch.owner_root, owner)
    agent = create_worker_pool(frozen, config, root).get(owner)
    home = agent.home_paths
    payload = stamped_request(owner)
    payload = {k: v for k, v in payload.items() if k != "route"}
    payload = freeze_request_route(
        payload, owner, RouteStamp(2, frozen.launch.partition.c_run_id, 100.0, "binding-v1")
    )
    paths = gateway_paths_from_root(frozen.launch.gateway_root)
    ensure_gateway_folders(paths)
    path = paths.inbox / (payload["id"] + ".json")
    path.write_text(json.dumps(payload))
    snapshot = freeze_claim_snapshot(
        frozen, ClaimCandidate(path, payload, reservation_for(payload))
    )
    reserved = reservation_file(paths, payload["id"])
    reserved.parent.mkdir(parents=True, exist_ok=True)
    reserved.write_bytes(snapshot.reservation_bytes)
    claim = claim_request(paths, path, worker_snapshot=snapshot)
    binding = bind_claimed_request(snapshot, claim, json.loads(claim.path.read_bytes()))
    store = agent.conversation_store
    source = store.threads.get_or_create(
        {
            "canonical_user_id": "alice",
            "channel": "internal",
            "channel_user_id": "alice",
            "channel_conversation_id": "local-session",
            "owner_id": home.owner_id,
            "owner_home": str(home.owner_home_dir),
        }
    )
    target = ensure_execution_thread(agent, source.thread_id, "ordinary-positive", kind="delegated")
    target = store.threads.update_atomic(
        target.thread_id,
        lambda row: replace(
            row,
            metadata={
                **row.metadata,
                IM_TASK_EXECUTION_KEY: {"mode": "delegated", "package_refs": []},
            },
        ),
    )
    dispatched = dispatch_owned_background_task(
        agent,
        source_thread_id=source.thread_id,
        target_thread=target,
        goal="合成普通工作",
        config=CapabilityConfig(),
    )
    assert dispatched.ok
    task = store.session_tasks.load(json.loads(dispatched.output)["task_id"])
    store.session_tasks.bind_turn(task.task_id, turn_id=task.task_id)
    args = {"thread_id": target.thread_id, "task_id": "", "reason": "session_task"}
    row = store.claims.acquire({**args, "lease_seconds": 30})
    before = binding.claim.path.read_bytes()
    observed = []
    append = store.guidance.append_once
    publish = store.wakes.raise_signal

    def check():
        assert current_worker_execution().binding is binding
        assert current_background_request() is not None
        current = current_claim_execution()
        assert current.claims is store.claims and current.claim_id == row["claim_id"]
        assert store.claims.load(target.thread_id)["status"] == "running"
        observed.append(True)

    def append_result(*a, **kw):
        check()
        return append(*a, **kw)

    def publish_result(*a, **kw):
        check()
        return publish(*a, **kw)

    monkeypatch.setattr(store.guidance, "append_once", append_result)
    monkeypatch.setattr(store.wakes, "raise_signal", publish_result)

    def run(_):
        return close_out_turn(
            agent, task.task_id, TaskTurnOutcome(True, summary="原W内结束的普通结果")
        )

    dependencies = BackgroundClaimDependencies(
        claims=store.claims,
        claim_scope_id=lambda *_: target.thread_id,
        child_owns_task=lambda _: False,
        terminal_task=lambda _: False,
        recovery_block=lambda _: None,
        source_admission=lambda _: "",
        retire_source=lambda _: None,
        run_once=run,
        runtime_facts=lambda: {},
        record_policy_failure=lambda _: pytest.fail("unexpected policy failure"),
        lease_seconds=30,
        heartbeat_interval_seconds=1,
    )
    context = {
        "agent": agent,
        "request": json.loads(binding.claim.path.read_bytes()),
        "request_path": binding.claim.path,
        "request_id": binding.claim.request_id,
    }
    with worker_execution_scope(binding, agent), background_request_scope(context):
        result = run_with_heartbeat(
            dependencies, row["claim_id"], args, claim_scope_id=row["claim_scope_id"]
        )
        assert result.status == "done"
    assert observed == [True, True]
    assert binding.claim.path.read_bytes() == before
    key = f"session_task_result:{task.task_id}:done"
    assert (
        store.guidance.receipt(key)
        and store.wakes.delivery_receipt(source.thread_id, key) == "pending"
    )


def test_delegate_quota_notice_keeps_original_wake_and_normal_notice_still_sends(
    tmp_path, monkeypatch
):
    from agent_py_agent.agent.conversation.runtime import _deliver_quota_fallback
    from agent_py_agent.tests import test_im_delegation_runtime as fixtures

    owned, _, provider, _, _ = ordinary_chain(tmp_path, monkeypatch)
    monkeypatch.setitem(
        fixtures.runtime_fixture.__globals__, "SimpleAgent", lambda *_a, **_k: owned
    )
    agent, params, task, _, source = fixtures.runtime_fixture(tmp_path, monkeypatch)
    prepare_delegated_execution(agent, params)
    scheduler = _build_background_scheduler(agent, agent.delivery_service)
    wake = next(
        s
        for s in agent.conversation_store.wakes.pending(limit=0)
        if s.thread_id == task.target_thread_id
    )
    status, handled = _deliver_quota_fallback(
        scheduler,
        wake,
        wake.metadata,
        content="合成额度事实",
        channel="feishu",
        target="ou_a",
        supports_proactive=True,
    )
    assert status == "suppressed" and not handled and not provider.messages
    assert agent.conversation_store.wakes.pending_one(wake.wake_signal_id) is not None
    ordinary = agent.conversation_store.wakes.raise_signal(
        {
            "thread_id": source.thread_id,
            "reason": "scheduled_progress_report",
            "summary": "普通通知",
        }
    )
    status, handled = _deliver_quota_fallback(
        scheduler,
        ordinary,
        ordinary.metadata,
        content="普通原通道通知",
        channel="feishu",
        target="ou_a",
        supports_proactive=True,
    )
    assert status == "sent" and handled and len(provider.messages) == 1


@pytest.mark.parametrize("primary_consumed", [True, False])
def test_two_result_messages_keep_exact_wake_admission_and_all_injected_facts(
    tmp_path, monkeypatch, primary_consumed
):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.runtime.guidance import (
        acknowledge_injected_turn_input,
        inject_pending_guidance,
        mark_injected_turn_input_submitted,
    )
    from agent_py_agent.agent.agent_core.tool_loop.natural_user_reply import (
        pending_natural_user_reply,
    )
    from agent_py_agent.agent.conversation.runtime import (
        _same_reason_wake_batch,
        _session_message_consumed_admission,
    )
    from agent_py_agent.agent.gateway_parts.im_task_routing import prepare_im_task_dispatch
    from agent_py_agent.tests.test_runtime_guidance import _tool_loop_params

    agent, params, first, context, source = runtime_fixture(tmp_path, monkeypatch)
    prepare_delegated_execution(agent, params)
    close_out_turn(agent, first.task_id, TaskTurnOutcome(True, summary="第一份实际结果"))
    first_key = f"session_task_result:{first.task_id}:done"
    if primary_consumed:
        busy = _tool_loop_params(task_attributes={"conversation_thread_id": source.thread_id,
            "conversation_request_id": "busy-main", "conversation_task_turn_active": True})
        inject_pending_guidance(agent, busy)
        mark_injected_turn_input_submitted(agent, busy, provider_call_id="busy-provider")
        acknowledge_injected_turn_input(agent, busy)
    request = {IM_DISPATCH_KEY: dict(context.request[IM_DISPATCH_KEY])}
    path = tmp_path / "second-request.json"
    path.write_text(json.dumps(request))
    second = prepare_im_task_dispatch(SimpleNamespace(agent=agent, request=request, request_path=path,
        request_id="second-input"), source, "第二份独立资料整理")
    agent.conversation_store.session_tasks.bind_turn(second.task_id, turn_id=second.task_id)
    close_out_turn(agent, second.task_id, TaskTurnOutcome(True, summary="第二份实际结果"))
    second_key = f"session_task_result:{second.task_id}:done"
    wakes = {w.dedupe_key: w for w in agent.conversation_store.wakes.pending(limit=0)
        if w.thread_id == source.thread_id}
    assert not _same_reason_wake_batch(wakes[first_key], wakes[second_key])
    assert _session_message_consumed_admission(wakes[second_key], guidance_store=agent.conversation_store.guidance) == ""
    if primary_consumed:
        assert _session_message_consumed_admission(wakes[first_key], guidance_store=agent.conversation_store.guidance) == "session_message_consumed"
    current = _tool_loop_params(task_attributes={"conversation_thread_id": source.thread_id,
        "conversation_request_id": "main-result", "conversation_task_turn_active": True,
        "host_session_message_key": second_key if primary_consumed else first_key})
    prepare_delegated_execution(agent, current)
    phase = pending_natural_user_reply(current)
    results = phase["facts"]["delegated_results"]
    assert {row["session_task_id"] for row in results} == ({second.task_id} if primary_consumed else {first.task_id, second.task_id})
    assert {row["result"] for row in results} == ({"第二份实际结果"} if primary_consumed else {"第一份实际结果", "第二份实际结果"})
    mark_injected_turn_input_submitted(agent, current, provider_call_id="main-result-provider")
    acknowledge_injected_turn_input(agent, current)
    assert all(agent.conversation_store.guidance.receipt(key).status == "consumed" for key in (first_key, second_key))
    assert _session_message_consumed_admission(wakes[second_key], guidance_store=agent.conversation_store.guidance) == "session_message_consumed"


@pytest.mark.parametrize("scope", ["default", "isolated"])
def test_real_local_agent_typed_nonconversation_keeps_original_child_defaults(tmp_path, monkeypatch, scope):
    from agent_py_agent.agent.agent_core.orchestration import lifecycle
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.conversation.im_delegation_delivery import (
        current_delegated_delivery_task,
    )

    agent = SimpleAgent(AgentConfig(prompt_files=[], enable_plugins=False,
        my_agent_home=str(tmp_path / "home")), tmp_path / "workspace")
    agent._current_run_params = RunParams(context_scope=scope, run_id="local-run", task_id="local-task",
        task_attributes={}, host_channel_actor=None)
    monkeypatch.setattr(lifecycle, "auto_start_tasks", lambda *_: {"status": "test-deferred"})
    assert agent.conversation_store.tasks.load("local-task") is None
    assert current_delegated_delivery_task(agent) is None
    result = CreateSubagentsTool(agent).execute({"goal": "本地普通资料处理"})
    assert result.ok, result.output
    child = agent.subagents.list_runs()[0]
    assert "schedule" not in child.allowed_tools
    assert agent.conversation_store.tasks.load("local-task").host_channel_actor is None


def test_conversation_params_missing_attrs_still_checks_canonical_delegate_scope(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.im_delegation_delivery import (
        current_delegated_delivery_task,
    )
    from agent_py_agent.agent.runtime_errors import DataCorruptionError

    agent, params, _, _, _ = runtime_fixture(tmp_path, monkeypatch)
    prepare_delegated_execution(agent, params)
    agent._current_run_params = replace(agent._current_run_params, context_scope="conversation", task_attributes={})
    with pytest.raises(DataCorruptionError):
        current_delegated_delivery_task(agent)


@pytest.mark.parametrize("claim_present", [True, False])
def test_missing_run_params_does_not_downgrade_actual_background_delivery(
    worker_case, tmp_path, monkeypatch, claim_present
):
    import sys

    from agent_py_agent.agent.conversation.claim_execution_binding import (
        bind_claim_execution,
        current_claim_execution,
    )
    from agent_py_agent.agent.conversation.im_delegation_delivery import (
        current_delegated_delivery_task,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_background_execution import (
        current_background_request,
    )
    from agent_py_agent.agent.runtime_errors import DataCorruptionError

    original = close_out_turn
    observed = []

    # LLM: 复用同文件真实W/P3/后台运输/claim组件，只在原业务栈检查缺params的负例，不制造执行权。
    # 函数用途: 原claim存在与暂时清空两种情况下，真实后台运输都不能降级成普通可发话身份。
    def check_missing_params(agent, *args, **kwargs):
        assert current_background_request() is not None
        assert current_claim_execution() is not None
        previous = getattr(agent, "_current_run_params", None)
        agent._current_run_params = None
        try:
            with bind_claim_execution(current_claim_execution() if claim_present else None):
                with pytest.raises(DataCorruptionError):
                    current_delegated_delivery_task(agent)
                observed.append(claim_present)
        finally:
            agent._current_run_params = previous
        return original(agent, *args, **kwargs)

    monkeypatch.setattr(sys.modules[__name__], "close_out_turn", check_missing_params)
    test_positive_count_report_runs_inside_original_worker_and_live_claim(
        worker_case, tmp_path, monkeypatch
    )
    assert observed == [claim_present]
