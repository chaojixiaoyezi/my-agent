"""完成证明必须来自真实claim/SQLite/SessionTask；聊天JSON不是来源。"""
import sys
import time
from dataclasses import replace
from types import ModuleType, SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.business_contracts import (
    BusinessContextIntent,
    BusinessExecutionContext,
)
from agent_py_agent.agent.conversation.claim_execution_binding import (
    ClaimExecutionBinding,
    bind_claim_execution,
    publish_claim_runtime_authority,
)
from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft, SessionTaskUpdate
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository


@pytest.fixture
def completed_host(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    source = store.threads.create(owner_id="owner")
    target = store.threads.create(owner_id="owner")
    task = store.session_tasks.create(SessionTaskDraft(source.thread_id, target.thread_id, "work", dedupe_key="work"),
        business_context_intent=BusinessContextIntent("owner", "space-A", None, None),
        business_context_factory=lambda identifier: BusinessExecutionContext("owner", "space-A", None, identifier,
                                      1, None, (), (), "business-context.v1"))
    context = BusinessExecutionContext.from_dict(task.metadata["business_context"])
    repo = RuntimeRepository(tmp_path / "runtime.db")
    record = repo.record_run_creation(owner_id="owner", run_id="actual-run", attempt_status="running",
        task_run_metadata={"business_context": context.to_dict(), "session_task_ref": task.task_id})
    store.session_tasks.bind_execution(task.task_id, task_run_id=record["task_run_id"], target_thread_id=target.thread_id)
    claim = store.claims.acquire({"thread_id": target.thread_id, "lease_seconds": 300, "now": time.time(), "reason": "session_task"})
    binding = ClaimExecutionBinding(store.claims, target.thread_id, claim["claim_id"], target.thread_id)
    host = SimpleNamespace(conversation_store=store, home_paths=SimpleNamespace(owner_id="owner"), subagents=SimpleNamespace(runtime_db=repo))
    params = SimpleNamespace(source="background_main_agent", business_context=context, run_id="actual-run", attempt_id=record["attempt_id"])
    candidate = SimpleNamespace(tool_rounds=6, archive_tool_calls=[{"tool": "read", "ok": True}], source="background_main_agent")
    authority = {"invocation_run_id": "invocation", "task_id": record["task_id"], "run_id": "actual-run",
                 "agent_run_id": record["agent_run_id"], "attempt_id": record["attempt_id"]}
    calls = []
    bridge = ModuleType("agent_py_agent.agent.capability.task_learning_bridge")
    def enqueue_completed_task_learning(*, completion, host):
        from agent_py_agent.agent.conversation.session_tasks import verify_trusted_task_completion
        assert verify_trusted_task_completion(host, completion) is True
        calls.append(completion)
        return {"status": "enqueued", "reason": "accepted"}
    bridge.enqueue_completed_task_learning = enqueue_completed_task_learning
    monkeypatch.setitem(sys.modules, bridge.__name__, bridge)
    return host, task, params, candidate, binding, authority, calls


def _stage_and_finish(data):
    from agent_py_agent.agent.conversation.session_tasks import stage_background_learning_candidate
    host, task, params, candidate, binding, authority, calls = data
    host.conversation_store.session_tasks.bind_turn(task.task_id, turn_id="input-turn")
    with bind_claim_execution(binding):
        publish_claim_runtime_authority(authority)
        stage_background_learning_candidate(host, params=params, candidate=candidate)
    host.subagents.runtime_db.settle_agent_run(agent_run_id=authority["agent_run_id"], status="done", attempt_id=params.attempt_id)
    host.conversation_store.session_tasks.advance(task.task_id, SessionTaskUpdate(status="done", result_refs=("result",)))


def test_canonical_completion_then_enqueue_once_and_replay_without_live_memory(completed_host):
    from agent_py_agent.agent.conversation.session_tasks import (
        confirm_background_task_completion,
        retry_completed_task_learning,
    )
    host, task, params, candidate, binding, authority, calls = completed_host
    _stage_and_finish(completed_host)
    with bind_claim_execution(binding):
        completion = confirm_background_task_completion(host, run_id=params.run_id, attempt_id=params.attempt_id)
    assert completion.task_id == task.task_id and completion.task_id != authority["task_id"]
    assert completion.claim_ref == binding.claim_id and completion.tool_rounds == 6
    assert completion.source_kind == "background_main_agent"
    assert retry_completed_task_learning(host, task.task_id)["status"] == "enqueued"
    assert retry_completed_task_learning(host, task.task_id)["status"] == "enqueued"
    assert len(calls) == 1
    assert calls[0].space_id == "space-A"


def test_unfinished_or_cancelled_never_mints_trusted_completion(completed_host):
    from agent_py_agent.agent.conversation.session_tasks import confirm_background_task_completion
    host, task, params, candidate, binding, authority, calls = completed_host
    with bind_claim_execution(binding):
        publish_claim_runtime_authority(authority)
        assert confirm_background_task_completion(host, run_id=params.run_id, attempt_id=params.attempt_id) is None
        host.conversation_store.session_tasks.advance(task.task_id, SessionTaskUpdate(status="cancelled"))
        assert confirm_background_task_completion(host, run_id=params.run_id, attempt_id=params.attempt_id) is None
    assert calls == []


def test_fake_context_wrong_attempt_and_expired_claim_cannot_complete(completed_host):
    from agent_py_agent.agent.conversation.session_tasks import (
        confirm_background_task_completion,
        stage_background_learning_candidate,
        verify_trusted_task_completion,
    )
    host, task, params, candidate, binding, authority, calls = completed_host
    _stage_and_finish(completed_host)
    with pytest.raises((ValueError, PermissionError)):
        with bind_claim_execution(replace(binding, claim_id="forged")):
            confirm_background_task_completion(host, run_id=params.run_id, attempt_id=params.attempt_id)
    with pytest.raises((ValueError, PermissionError)):
        with bind_claim_execution(binding):
            confirm_background_task_completion(host, run_id=params.run_id, attempt_id="wrong")
    with bind_claim_execution(binding):
        completion = confirm_background_task_completion(host, run_id=params.run_id, attempt_id=params.attempt_id)
    assert verify_trusted_task_completion(host, replace(completion, owner_id="foreign")) is False
    assert verify_trusted_task_completion(host, {**completion.to_dict(), "trusted": True}) is False


def test_queue_failure_retains_original_pending_obligation(completed_host, monkeypatch):
    from agent_py_agent.agent.conversation.session_tasks import (
        confirm_background_task_completion,
        retry_completed_task_learning,
    )
    host, task, params, candidate, binding, authority, calls = completed_host
    _stage_and_finish(completed_host)
    with bind_claim_execution(binding):
        confirm_background_task_completion(host, run_id=params.run_id, attempt_id=params.attempt_id)
    bridge = sys.modules["agent_py_agent.agent.capability.task_learning_bridge"]
    original = bridge.enqueue_completed_task_learning
    def fail(**kwargs):
        raise OSError("queue write failed")
    monkeypatch.setattr(bridge, "enqueue_completed_task_learning", fail)
    with pytest.raises(OSError):
        retry_completed_task_learning(host, task.task_id)
    assert host.conversation_store.session_tasks.require(task.task_id).metadata["task_learning_enqueue"]["status"] == "pending"
    monkeypatch.setattr(bridge, "enqueue_completed_task_learning", original)
    assert retry_completed_task_learning(host, task.task_id)["status"] == "enqueued"


@pytest.mark.parametrize("include_background", [True, False])
def test_real_scheduler_six_native_tool_rounds_produce_one_frozen_completion(tmp_path, monkeypatch, include_background):
    """W2真实Scheduler/core/原工具/收口；未交付W6端口为contract fake，不代表跨片集成通过。"""
    import json

    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.agent.backends.base import ProviderToolCapability, _utc_now_iso
    from agent_py_agent.agent.capability.config import CapabilityConfig
    from agent_py_agent.agent.conversation import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
    )
    from agent_py_agent.agent.conversation.session_task_dispatch import (
        SessionTaskDispatchPlan,
        dispatch_session_task,
    )
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), enable_tools=True), tmp_path)
    home = agent.home_paths.owner_home_dir
    for n in range(6):
        (home / f"input-{n}.txt").write_text(f"verified input {n}")

    class Backend:
        name = "mega-background-six-tools"
        calls = 0
        def probe_tool_capability(self):
            return ProviderToolCapability(provider=self.name, endpoint="local://test", model="", stream=False,
                native_supported=True, evidence="contract_test", observed_at=_utc_now_iso())
        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls += 1
            if self.calls <= 6:
                return ModelResponse(text="", backend=self.name, tool_use_blocks=[{"id": f"step-{self.calls}",
                    "name": "read_file", "input": {"path": f"input-{self.calls - 1}.txt"}}])
            return ModelResponse(text="六份资料已逐一核对。", backend=self.name)

    agent.backend = Backend()
    store = agent.conversation_store
    owner = agent.home_paths.owner_id
    source = store.threads.create(owner_id=owner, canonical_user_id="local-agent")
    target = store.threads.create(owner_id=owner, canonical_user_id="local-agent")
    outcome = dispatch_session_task(agent, SessionTaskDispatchPlan(source.thread_id, target.thread_id, "核对六份资料"),
        target, CapabilityConfig(), business_context_intent=BusinessContextIntent(owner, "space-A", None, None),
        business_context_factory=lambda identifier: BusinessExecutionContext(owner, "space-A", None, identifier, 1, None, (), (), "business-context.v1"))
    assert outcome.ok
    task_id = json.loads(outcome.output)["task_id"]
    calls, queued = [], []
    bridge = ModuleType("agent_py_agent.agent.capability.task_learning_bridge")
    def enqueue_completed_task_learning(*, completion, host):
        from agent_py_agent.agent.conversation.session_tasks import verify_trusted_task_completion
        assert verify_trusted_task_completion(host, completion)
        calls.append(completion)
        if include_background and completion.tool_rounds >= 6:
            queued.append(completion.completion_ref)
            return {"status": "enqueued", "reason": "contract_fake_accepted"}
        return {"status": "ineligible", "reason": "contract_fake_background_disabled"}
    bridge.enqueue_completed_task_learning = enqueue_completed_task_learning
    monkeypatch.setitem(sys.modules, bridge.__name__, bridge)
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    reports = scheduler.tick()
    assert len(reports) == 1 and reports[0].tool_call_count == 6
    canonical = store.session_tasks.require(task_id)
    assert canonical.status == "done"
    assert len(calls) == 1 and calls[0].tool_rounds == 6
    assert len(queued) == int(include_background)
    assert calls[0].task_id == task_id
    exact = agent.subagents.runtime_db.agent_run_for_run_id(calls[0].run_id)
    assert canonical.task_run_id == exact["task_run_id"]
    assert calls[0].attempt_id == exact["current_attempt_id"]
    assert [m.content for m in store.messages.recent(target.thread_id, limit=0) if m.role == "assistant" and m.content] == ["六份资料已逐一核对。"]
    from agent_py_agent.agent.conversation.session_tasks import retry_completed_task_learning
    reopened = SimpleNamespace(conversation_store=ConversationStore(store.storage.root), subagents=agent.subagents,
                               home_paths=agent.home_paths)
    retry_completed_task_learning(reopened, task_id)
    assert len(calls) == 1 and agent.backend.calls == 7


def test_completion_wake_publish_failure_repairs_same_original_proof_after_restart(completed_host, monkeypatch):
    from agent_py_agent.agent.conversation import BackgroundMainAgentRuntime
    from agent_py_agent.agent.conversation.session_tasks import (
        confirm_background_task_completion,
        repair_completed_task_learning_wake,
    )
    host, task, params, candidate, binding, authority, calls = completed_host
    _stage_and_finish(completed_host)
    original = host.conversation_store.wakes.raise_signal
    def fail(*args, **kwargs):
        raise OSError("wake publication EIO")
    monkeypatch.setattr(host.conversation_store.wakes, "raise_signal", fail)
    with bind_claim_execution(binding), pytest.raises(OSError):
        confirm_background_task_completion(host, run_id=params.run_id, attempt_id=params.attempt_id)
    saved = host.conversation_store.session_tasks.require(task.task_id)
    assert saved.metadata["task_learning_enqueue"]["status"] == "pending"
    monkeypatch.setattr(host.conversation_store.wakes, "raise_signal", original)
    reopened = SimpleNamespace(conversation_store=ConversationStore(host.conversation_store.storage.root),
        subagents=host.subagents, home_paths=host.home_paths,
        run=lambda *a, **k: pytest.fail("queue repair must not call model"))
    first = repair_completed_task_learning_wake(reopened, task.task_id)
    assert repair_completed_task_learning_wake(reopened, task.task_id).wake_signal_id == first.wake_signal_id
    report = BackgroundMainAgentRuntime(agent=reopened, store=reopened.conversation_store).run_once({
        "thread_id": task.target_thread_id, "reason": "task_learning_completion", "wake_signal": first.to_dict()})
    assert report.wake_handled is True and report.delivery_reason == "task_learning_queue_only"
    assert len(calls) == 1
