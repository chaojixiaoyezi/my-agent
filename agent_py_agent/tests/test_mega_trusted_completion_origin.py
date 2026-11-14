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
    candidate = SimpleNamespace(tool_rounds=6, archive_tool_calls=[{"tool": "read", "ok": True}], source="background_main_agent",
        run_id=params.run_id, task_id=record["task_id"], task_attributes={"conversation_thread_id": target.thread_id},
        user_prompt="核对资料", final_response=SimpleNamespace(text="已核对"), do_save=False)
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


def _native_learning_threads(agent):
    """仿真原本地入口身份，执行核权不能只靠owner_id或业务正文补造来源。"""
    def create(name):
        return agent.conversation_store.threads.get_or_create({
            "channel": "chat", "channel_conversation_id": name, "channel_user_id": "local-agent",
            "canonical_user_id": "local-agent", "owner_id": agent.home_paths.owner_id,
            "owner_home": str(agent.home_paths.owner_home_dir),
        })
    return create("learning-source"), create("learning-target")


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

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), enable_tools=True,
        business_spaces_enabled=True), tmp_path)
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
                    "name": "read_file", "input": {"path": str(home / f"input-{self.calls - 1}.txt")}}])
            return ModelResponse(text="六份资料已逐一核对。", backend=self.name)

    agent.backend = Backend()
    store = agent.conversation_store
    owner = agent.home_paths.owner_id
    source, target = _native_learning_threads(agent)
    from agent_py_agent.agent.conversation.business_access import BusinessActor
    from agent_py_agent.agent.conversation.business_spaces import BusinessSpaceService
    spaces = BusinessSpaceService(home, owner_id=owner, enabled=True)
    actor = BusinessActor(owner, "local-agent", spaces, lambda action, facts: True)
    sid = spaces.create({"name": "原任务方法空间"}, actor=actor,
                        idempotency_key="six-tools-original-space")["space_id"]
    outcome = dispatch_session_task(agent, SessionTaskDispatchPlan(source.thread_id, target.thread_id, "核对六份资料"),
        target, CapabilityConfig(), business_context_intent=BusinessContextIntent(owner, sid, None, None),
        business_context_factory=lambda identifier: BusinessExecutionContext(owner, sid, None, identifier, 1, None, (), (), "business-context.v1"))
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


def _real_learning_background(tmp_path, *, rounds=6, enabled=True, include=True):
    """保留真实生产组合、空间、dispatch、SQLite、claim 和学习 FIFO；只控制任务模型。"""
    import json

    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.agent.backends.base import ProviderToolCapability, _utc_now_iso
    from agent_py_agent.agent.capability.config import CapabilityConfig
    from agent_py_agent.agent.conversation import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
    )
    from agent_py_agent.agent.conversation.business_access import BusinessActor
    from agent_py_agent.agent.conversation.business_context import resolve_business_context
    from agent_py_agent.agent.conversation.business_spaces import BusinessSpaceService
    from agent_py_agent.agent.conversation.session_task_dispatch import (
        SessionTaskDispatchPlan,
        dispatch_session_task,
    )
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    config = AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), enable_tools=True,
        business_spaces_enabled=True, enable_self_learning=enabled, self_learning_include_background=include,
        self_learning_min_tool_rounds=6, prompt_files=[])
    agent = SimpleAgent(config, tmp_path / "workspace")
    home, owner = agent.home_paths.owner_home_dir, str(agent.home_paths.owner_id)
    for n in range(rounds):
        (home / f"input-{n}.txt").write_text(f"verified input {n}; RAW_TOOL_OUTPUT_NOT_PERSISTED", encoding="utf-8")

    class Backend:
        name = "r3-controlled-background-model"
        calls = 0

        def probe_tool_capability(self):
            return ProviderToolCapability(provider=self.name, endpoint="local://test", model="", stream=False,
                native_supported=True, evidence="contract_test", observed_at=_utc_now_iso())

        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls += 1
            if self.calls <= rounds:
                return ModelResponse(text="", backend=self.name, tool_use_blocks=[{"id": f"step-{self.calls}",
                    "name": "read_file", "input": {"path": str(home / f"input-{self.calls - 1}.txt")}}])
            return ModelResponse(text="六份资料已逐一核对。" + "核验结论" * 800, backend=self.name)

    agent.backend = Backend()
    spaces = BusinessSpaceService(home, owner_id=owner, enabled=config.business_spaces_enabled)
    actor = BusinessActor(owner, "local-agent", spaces, lambda action, facts: True)
    sid = spaces.create({"name": "原任务方法空间"}, actor=actor, idempotency_key="r3-original-space")["space_id"]
    source, target = _native_learning_threads(agent)
    outcome = dispatch_session_task(agent, SessionTaskDispatchPlan(source.thread_id, target.thread_id,
        "核对六份资料。" + "任务说明" * 800), target, CapabilityConfig(),
        business_context_intent=BusinessContextIntent(owner, sid, None, None),
        business_context_factory=lambda identifier: resolve_business_context(actor=actor, task_id=identifier,
            requested_space_id=sid, project_ref=None, binding_id=None))
    assert outcome.ok, outcome
    task_id = json.loads(outcome.output)["task_id"]
    runtime = BackgroundMainAgentRuntime(agent=agent, store=agent.conversation_store)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": agent.conversation_store})
    return agent, config, task_id, runtime, scheduler


def test_r3_real_background_completion_restarts_with_original_fifo(tmp_path):
    """原缺陷回归：不替换 bridge/reader，六轮真实工具完成后必须入原 FIFO。"""
    from agent_py_agent.agent.capability.task_learning_bridge import enqueue_completed_task_learning
    from agent_py_agent.agent.conversation.business_contracts import TrustedTaskCompletion
    from agent_py_agent.agent.conversation.session_tasks import (
        load_task_learning_evidence,
        retry_completed_task_learning,
    )
    from agent_py_agent.agent.core import SimpleAgent

    agent, config, task_id, runtime, scheduler = _real_learning_background(tmp_path)
    reports = scheduler.tick()
    assert len(reports) == 1 and reports[0].tool_call_count == 6
    canonical = agent.conversation_store.session_tasks.require(task_id)
    assert canonical.status == "done"
    assert canonical.metadata["task_learning_enqueue"] == {"status": "enqueued", "reason": "queued"}
    completion = TrustedTaskCompletion.from_dict(canonical.metadata["trusted_task_completion"])
    [path] = agent.skill_learning.store.pending_requests()
    request = agent.skill_learning.store.read_request(path)
    material = canonical.metadata["task_learning_candidate"]["request_material"]
    assert request["user_prompt"] == material["user_prompt"]
    assert request["user_prompt"].startswith("The same Agent was resumed by a structured background event.")
    assert "Wake reason: session_task" in request["user_prompt"] and 0 < len(request["user_prompt"]) <= 3000
    assert request["final_response"].startswith("六份资料已逐一核对。") and len(request["final_response"]) == 3000
    assert len(request["tool_trace"]) == 6
    assert all(row["tool"] == "read_file" and row["ok"] is True for row in request["tool_trace"])
    import json
    assert [row["args"] for row in request["tool_trace"]] == [
        json.dumps({"path": str(agent.home_paths.owner_home_dir / f"input-{n}.txt")}) for n in range(6)]
    assert "RAW_TOOL_OUTPUT_NOT_PERSISTED" not in str(material)
    assert request["created_at"] == material["created_at"]
    assert request["trajectory_digest"] == completion.trajectory_digest
    assert request["completion_identity"] == [completion.owner_id, task_id, completion.run_id,
        completion.attempt_id, completion.completion_ref]
    assert request["task_id"] == request["root_task_id"] == task_id
    assert request["run_id"] == completion.run_id and request["attempt_id"] == completion.attempt_id
    assert request["claim_ref"] == completion.claim_ref and request["result_refs"] == list(completion.result_refs)
    assert request["business_context"] == canonical.metadata["business_context"]
    assert request["learning_scope"]["space_id"] == completion.space_id
    exact = agent.subagents.runtime_db.get_task_run(canonical.task_run_id)
    assert request["runtime_task_id"] == exact["task_id"]
    assert agent.backend.calls == 7

    # 新 SimpleAgent 只读同一原 home，不带原 finalize context、claim binding 或候选 Python 对象。
    reopened = SimpleAgent(config, tmp_path / "workspace")
    reopened.backend = SimpleNamespace(generate=lambda *a, **k: pytest.fail("replay must not run a model"))
    evidence = load_task_learning_evidence(reopened, completion)
    assert evidence.request_material == material and evidence.context.do_save is False
    assert enqueue_completed_task_learning(completion=completion, host=reopened)["status"] == "duplicate"
    assert retry_completed_task_learning(reopened, task_id)["status"] == "enqueued"
    assert reopened.skill_learning.store.pending_requests() == [path]
    assert reopened.conversation_store.session_tasks.require(task_id) == canonical
    assert agent.backend.calls == 7


def test_r3_staging_reuses_first_bounded_redacted_material(completed_host, monkeypatch):
    import hashlib
    import json

    from agent_py_agent.agent.capability import skill_learning_request as material_builder
    from agent_py_agent.agent.conversation.session_tasks import stage_background_learning_candidate

    host, task, params, candidate, binding, authority, _calls = completed_host
    candidate.user_prompt = "password=not-a-real-test-password\n" + "p" * 4000
    candidate.final_response = SimpleNamespace(text="token=not-a-real-test-token\n" + "r" * 4000)
    candidate.archive_tool_calls = [{"tool": "read_file", "ok": True, "tool_round": n,
        "model_parameters": {"path": str(n) + "x" * 400, "password": "not-a-real-test-password"},
        "output": "RAW_OUTPUT_MUST_NOT_PERSIST", "evidence_refs": ["evidence"]} for n in range(100)]
    expected_digest = hashlib.sha256(json.dumps(candidate.archive_tool_calls, ensure_ascii=False,
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with bind_claim_execution(binding):
        publish_claim_runtime_authority(authority)
        monkeypatch.setattr(material_builder, "utc_now", lambda: "first-created-at")
        stage_background_learning_candidate(host, params=params, candidate=candidate)
        first = host.conversation_store.session_tasks.require(task.task_id).metadata["task_learning_candidate"]
        monkeypatch.setattr(material_builder, "utc_now", lambda: "later-created-at")
        candidate.user_prompt = "A repeated callback must not replace the first material."
        stage_background_learning_candidate(host, params=params, candidate=candidate)
        assert host.conversation_store.session_tasks.require(task.task_id).metadata["task_learning_candidate"] == first
        candidate.archive_tool_calls.append({"tool": "different"})
        with pytest.raises(ValueError, match="COMPLETION_CANDIDATE_CONFLICT"):
            stage_background_learning_candidate(host, params=params, candidate=candidate)
    material = first["request_material"]
    assert first["trajectory_digest"] == material["trajectory_digest"] == expected_digest
    assert material["created_at"] == "first-created-at"
    assert len(material["user_prompt"]) == len(material["final_response"]) == 3000
    assert material["tool_calls_total"] == 100 and len(material["tool_trace"]) == 80
    assert all(len(row["args"]) <= 300 for row in material["tool_trace"])
    assert not any(value in json.dumps(first) for value in (
        "not-a-real-test-password", "not-a-real-test-token", "RAW_OUTPUT_MUST_NOT_PERSIST"))


@pytest.mark.parametrize("after_queue_commit", [False, True])
def test_r3_real_fifo_io_failure_survives_restart_and_replays_wake_once(tmp_path, monkeypatch, after_queue_commit):
    from agent_py_agent.agent.conversation import BackgroundMainAgentRuntime
    from agent_py_agent.agent.conversation.session_tasks import retry_completed_task_learning
    from agent_py_agent.agent.core import SimpleAgent

    agent, config, task_id, _runtime, scheduler = _real_learning_background(tmp_path)
    original = agent.skill_learning.store.enqueue

    def fail_once(request):
        if after_queue_commit:
            assert original(request) == "queued"
        raise OSError("original FIFO EIO")

    monkeypatch.setattr(agent.skill_learning.store, "enqueue", fail_once)
    reports = scheduler.tick()
    assert len(reports) == 1 and reports[0].tool_call_count == 6
    canonical = agent.conversation_store.session_tasks.require(task_id)
    assert canonical.status == "done" and canonical.metadata["task_learning_enqueue"]["status"] == "pending"
    wake_id = canonical.metadata["task_learning_wake_id"]
    assert agent.conversation_store.wakes.pending_one(wake_id) is not None
    assert len(agent.skill_learning.store.pending_requests()) == int(after_queue_commit)
    monkeypatch.setattr(agent.skill_learning.store, "enqueue", original)
    reopened = SimpleAgent(config, tmp_path / "workspace")
    monkeypatch.setattr(reopened, "run", lambda *a, **k: pytest.fail("learning repair must never rerun the task"))
    wake = reopened.conversation_store.wakes.pending_one(wake_id)
    replay = {"thread_id": canonical.target_thread_id, "reason": "task_learning_completion", "wake_signal": wake.to_dict()}
    runtime = BackgroundMainAgentRuntime(agent=reopened, store=reopened.conversation_store)
    report = runtime.run_once(replay)
    assert report.wake_handled and report.delivery_reason == "task_learning_queue_only"
    saved = reopened.conversation_store.session_tasks.require(task_id)
    assert saved.status == "done" and saved.task_run_id == canonical.task_run_id
    assert saved.metadata["trusted_task_completion"] == canonical.metadata["trusted_task_completion"]
    assert saved.metadata["task_learning_enqueue"]["status"] == ("duplicate" if after_queue_commit else "enqueued")
    assert reopened.conversation_store.wakes.pending_one(wake_id) is None
    [path] = reopened.skill_learning.store.pending_requests()
    original_request = reopened.skill_learning.store.read_request(path)
    assert retry_completed_task_learning(reopened, task_id) == saved.metadata["task_learning_enqueue"]
    assert runtime.run_once(replay).wake_handled
    assert reopened.skill_learning.store.pending_requests() == [path]
    assert reopened.skill_learning.store.read_request(path) == original_request
    assert agent.backend.calls == 7


@pytest.mark.parametrize("enabled,include,rounds,reason", [
    (False, True, 6, "background_learning_disabled"),
    (True, False, 6, "background_learning_disabled"),
    (True, True, 5, "background_source_or_rounds_ineligible"),
])
def test_r3_real_background_policy_ineligibility_is_terminal(tmp_path, enabled, include, rounds, reason):
    from agent_py_agent.agent.conversation.session_tasks import retry_completed_task_learning

    agent, _config, task_id, _runtime, scheduler = _real_learning_background(
        tmp_path, rounds=rounds, enabled=enabled, include=include)
    assert scheduler.tick()[0].tool_call_count == rounds
    task = agent.conversation_store.session_tasks.require(task_id)
    assert task.status == "done"
    result = {"status": "ineligible", "reason": reason}
    assert task.metadata["task_learning_enqueue"] == result
    assert retry_completed_task_learning(agent, task_id) == result
    assert agent.conversation_store.wakes.pending_one(task.metadata["task_learning_wake_id"]) is None
    assert agent.skill_learning is None or agent.skill_learning.store.pending_requests() == []
    assert agent.backend.calls == rounds + 1


def test_staged_tool_activity_cannot_replace_terminal_runtime_proof(completed_host):
    """六轮候选与活跃claim只证明执行中，不可签发可信完成或进入学习FIFO。"""
    from agent_py_agent.agent.conversation.session_tasks import (
        confirm_background_task_completion,
        stage_background_learning_candidate,
    )
    host, task, params, candidate, binding, authority, calls = completed_host
    host.conversation_store.session_tasks.bind_turn(task.task_id, turn_id="input-turn")
    with bind_claim_execution(binding):
        publish_claim_runtime_authority(authority)
        stage_background_learning_candidate(host, params=params, candidate=candidate)
        assert confirm_background_task_completion(host, run_id=params.run_id, attempt_id=params.attempt_id) is None
    saved = host.conversation_store.session_tasks.require(task.task_id)
    assert saved.status == "accepted"
    assert "trusted_task_completion" not in saved.metadata
    assert "task_learning_enqueue" not in saved.metadata
    assert calls == []
