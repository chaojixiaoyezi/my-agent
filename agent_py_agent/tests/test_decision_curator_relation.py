"""关系建议复用原 Curator、配置和账本；不发送收费请求，不获得记忆写权限。"""
import hashlib
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.decision_protocol import (
    DecisionAnswer,
    DecisionBinding,
    DecisionResponse,
)
from agent_py_agent.agent.memory_store import decision_curator as curator
from agent_py_agent.agent.memory_store import decision_curator_relation as relation
from agent_py_agent.agent.memory_store.curator_backend import (
    curator_prompt,
    shrink_batch_for_timeout,
)
from agent_py_agent.agent.memory_store.curator_formal import FormalMemorySource
from agent_py_agent.agent.memory_store.curator_inputs import CuratorInputBatch, CuratorMessageInput
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.decision_settings import (
    execute_decision_settings_operation as settings,
)
from agent_py_agent.agent.settings.memory import normalize_memory_settings
from agent_py_agent.agent.settings.model_provider_schema import ModelProfileError
from agent_py_agent.cli.chat_parts.tui_decision_menu import _field_label, _fields
from agent_py_agent.tests.test_decision_settings import host_at, patch


# LLM: 只写 pytest 临时目录的真实原仓库，投影完全复用 FormalMemorySource；不伪造版本或完整性事实。
# 函数用途: 构造一条正式记忆和带精确哈希的新消息，供关系请求正负验证。
@pytest.fixture
def prepared(tmp_path):
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    record = memory.add("user", "用户喜欢简短答复。", kind="fact",
                        attributes={"subject_key": "reply.length", "scope_type": "personal", "scope_key": "personal"})
    empty = SimpleNamespace(list=lambda: [])
    formal_source = FormalMemorySource(memory, empty, empty)
    text = "请记住，我现在希望答复包含完整解释。"
    message = CuratorMessageInput("message-1", "thread-1", "user", "internal", 1.0, text, text,
                                  "sha256:" + hashlib.sha256(text.encode()).hexdigest(), {})
    return SimpleNamespace(memory=memory), CuratorInputBatch((message,), (), formal_source.read()), record


# LLM: fake 只替换供应商策略调用；保留精确 response binding、原仓库复读与 Curator prompt 投影。
# 函数用途: 提供可独立控制的标签/关系答案，并记录同一阶段与期限供断言。
def install(monkeypatch, *, points=("curator_relation",), mode="apply", status="success", value="possible_update", mutate=None, error=None):
    calls, stages = [], []

    def begin(_agent, params, **kwargs):
        assert kwargs["scope"] == "owner_background" and params.thread_id == ""
        stage = SimpleNamespace(error_code="", enabled_points=points,
                                deadline=kwargs["caller_deadline"] or time.monotonic() + 60)
        stages.append(stage)
        return stage

    def decide(_agent, _params, stage, **kwargs):
        calls.append((stage, kwargs))
        if error is not None:
            raise error
        answers = tuple(DecisionAnswer(key, "choice", value if key.startswith("pair_") else "fact" if key.endswith("tag") else "normal")
                        for key in kwargs["questions"])
        response = DecisionResponse(DecisionBinding(kwargs["point"], "owner", "curator-run", "policy", kwargs["candidates_revision"]),
                                    "input-digest", "configured", "actual-decision", answers, b"{}")
        if mutate is not None:
            response = mutate(response)
        return SimpleNamespace(mode=mode, status=status, may_apply=mode == "apply" and status == "success", response=response)

    monkeypatch.setattr(curator, "begin_decision_stage", begin)
    monkeypatch.setattr(curator, "decide", decide)
    monkeypatch.setattr(relation, "decide", decide)
    monkeypatch.setattr(curator, "decision_outcome_is_current", lambda *_: True)
    monkeypatch.setattr(relation, "decision_outcome_is_current", lambda *_: True)
    return calls, stages


def annotate(agent, batch, **kwargs):
    return curator.annotate_curator_batch(agent, batch, "curator-run", max_input_chars=40_000, **kwargs)


def test_off_preserves_prompt_bytes_and_does_not_prepare_or_read_formal(prepared, monkeypatch):
    agent, batch, _record = prepared
    original = curator_prompt(batch)
    calls, _ = install(monkeypatch, points=())
    monkeypatch.setattr(relation, "_relation_material", lambda *_: pytest.fail("off must not prepare"))
    monkeypatch.setattr(agent.memory, "all", lambda: pytest.fail("off must not reread"))
    result, warnings = annotate(agent, batch)
    assert result is batch and not warnings and not calls
    assert curator_prompt(result) == original and "relation_annotations" not in batch.to_model_payload()


@pytest.mark.parametrize("value", tuple(relation._RELATIONS))
def test_relation_is_temporary_bound_to_exact_pair_and_has_no_write_authority(prepared, monkeypatch, value):
    agent, batch, record = prepared
    original = Path(agent.memory.path).read_bytes()
    calls, _ = install(monkeypatch, value=value)
    result, warnings = annotate(agent, batch)
    assert result.messages is batch.messages and result.formal_memories is batch.formal_memories
    assert not result.decision_annotations and len(result.relation_annotations) == 1
    hint = result.to_model_payload()["relation_annotations"][0]
    assert hint["relation"] == value and hint["coverage"] == "presented_pair_only"
    assert hint["authority_version"] == record.version and hint["authority_id"] == record.entry_id
    assert hint["content_hash"] == batch.messages[0].content_hash and hint["formal_content_hash"] == batch.formal_memories[0].content_hash
    assert not {"candidate_id", "promotion_target", "proposed_action", "status"} & hint.keys()
    assert "不得当作证据" in curator_prompt(result) and "不证明全库没有冲突" in curator_prompt(result)
    assert warnings == ("memory_curator_relation:apply:success",)
    assert calls[0][1]["point"] == "curator_relation"
    assert set(calls[0][1]["source_refs"]) == {"message:message-1", batch.formal_memories[0].authority_ref}
    assert Path(agent.memory.path).read_bytes() == original


@pytest.mark.parametrize("mode,status", [("observe", "success"), ("apply", "deadline"), ("apply", "error"), ("off", "off")])
def test_non_applying_outcome_preserves_original(prepared, monkeypatch, mode, status):
    agent, batch, _ = prepared
    install(monkeypatch, mode=mode, status=status)
    result, _warnings = annotate(agent, batch)
    assert result is batch


@pytest.mark.parametrize("change", ["source_truncated", "source_hash", "formal_truncated", "formal_hash", "version_missing", "version_bool", "lesson", "no_formal", "audit_only"])
def test_missing_complete_material_never_sends_relation_request(prepared, monkeypatch, change):
    agent, batch, _ = prepared
    if change.startswith("source_"):
        message = replace(batch.messages[0], **({"content_preview": "请记住"} if change == "source_truncated" else {"content_hash": "wrong"}))
        batch = replace(batch, messages=(message,))
    elif change == "no_formal":
        batch = replace(batch, formal_memories=())
    elif change == "audit_only":
        from agent_py_agent.agent.memory_store.curator_inputs import _audit_input
        batch = replace(batch, messages=(), audit_events=(_audit_input({"event_id": "a", "preview": "预览"}),))
    else:
        changes = {"formal_truncated": {"content_preview": "用户"}, "formal_hash": {"content_hash": "wrong"},
                   "version_missing": {"authority_version": None}, "version_bool": {"authority_version": True},
                   "lesson": {"authority_type": "lesson"}}[change]
        batch = replace(batch, formal_memories=(replace(batch.formal_memories[0], **changes),))
    calls, _ = install(monkeypatch)
    result, warnings = annotate(agent, batch)
    assert result is batch and not calls and warnings == ("memory_curator_relation:unknown:need_data",)


@pytest.mark.parametrize("phase", ["before", "during"])
@pytest.mark.parametrize("change", ["replace", "same_body_version", "remove"])
def test_exact_formal_mutation_invalidates_advice(prepared, monkeypatch, phase, change):
    agent, batch, record = prepared

    def mutate(response):
        if change == "remove":
            agent.memory.remove(record.entry_id, expected_version=record.version)
        else:
            body = record.content if change == "same_body_version" else "另一条正式内容。"
            agent.memory.replace(record.entry_id, body, expected_version=record.version)
        return response

    if phase == "before":
        mutate(None)
    calls, _ = install(monkeypatch, mutate=mutate if phase == "during" else None)
    result, warnings = annotate(agent, batch)
    assert result is batch and warnings[-1].endswith(":stale")
    assert len(calls) == int(phase == "during")


def test_other_owner_formal_is_not_sent_even_when_frozen_batch_has_valid_hash(prepared, monkeypatch, tmp_path):
    agent, batch, _ = prepared
    agent.memory = JsonlMemory(tmp_path / "other-owner.jsonl")
    calls, _ = install(monkeypatch)
    result, warnings = annotate(agent, batch)
    assert result is batch and not calls and warnings == ("memory_curator_relation:unknown:stale",)


def test_mutated_source_or_wrong_response_revision_rejects_advice(prepared, monkeypatch):
    agent, batch, _ = prepared
    def mutate(response):
        batch.messages[0].metadata["run_id"] = "changed-source"
        return response
    install(monkeypatch, mutate=mutate)
    result, warnings = annotate(agent, batch)
    assert result is batch and warnings[-1] == "memory_curator_relation:apply:stale"
    install(monkeypatch, mutate=lambda response: replace(response, binding=replace(response.binding, candidates_revision="other")))
    assert annotate(agent, batch)[0] is batch


def test_config_revocation_and_annotation_budget_keep_original(prepared, monkeypatch):
    agent, batch, _ = prepared
    install(monkeypatch)
    monkeypatch.setattr(relation, "decision_outcome_is_current", lambda *_: False)
    result, warnings = annotate(agent, batch)
    assert result is batch and warnings[-1].endswith(":stale")
    install(monkeypatch)
    result, warnings = curator.annotate_curator_batch(agent, batch, "curator-run", max_input_chars=len(curator_prompt(batch)))
    assert result is batch and warnings[-1].endswith(":annotation_budget")


def test_same_stage_deadline_and_independent_failure_preserve_prior_tags(prepared, monkeypatch):
    agent, batch, _ = prepared
    calls, stages = install(monkeypatch, points=("curator", "curator_relation"))
    deadline = time.monotonic() + 10
    result, _warnings = annotate(agent, batch, caller_deadline=deadline)
    assert len(stages) == 1 and all(stage is stages[0] for stage, _ in calls) and stages[0].deadline == deadline
    assert result.decision_annotations and result.relation_annotations
    monkeypatch.setattr(relation, "decide", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("private provider text")))
    result, warnings = annotate(agent, batch)
    assert result.decision_annotations and not result.relation_annotations
    assert warnings[-1] == "memory_curator_relation:off:enhancement_failed" and "private" not in str(warnings)


def test_prior_tag_revocation_while_relation_waits_removes_only_stale_tags(prepared, monkeypatch):
    agent, batch, _ = prepared
    install(monkeypatch, points=("curator", "curator_relation"))
    checks = iter((True, False))
    monkeypatch.setattr(curator, "decision_outcome_is_current", lambda *_: next(checks))
    result, warnings = annotate(agent, batch)
    assert not result.decision_annotations and result.relation_annotations
    assert warnings[-1] == "memory_curator_decision:apply:stale"


@pytest.mark.parametrize("error", [InterruptedError(), KeyboardInterrupt()])
def test_cancel_is_not_optional_failure(prepared, monkeypatch, error):
    agent, batch, _ = prepared
    install(monkeypatch, error=error)
    with pytest.raises(type(error)):
        annotate(agent, batch)


def test_expired_caller_does_not_read_or_send(prepared, monkeypatch):
    agent, batch, _ = prepared
    calls, _ = install(monkeypatch)
    monkeypatch.setattr(agent.memory, "all", lambda: pytest.fail("deadline must be checked before local I/O"))
    result, _warnings = annotate(agent, batch, caller_deadline=time.monotonic() - 1)
    assert result is batch and not calls


def test_partial_answer_and_pair_limit_keep_original_coverage_and_shrink_filters(prepared, monkeypatch):
    agent, batch, _ = prepared
    batch = replace(batch, messages=tuple(replace(batch.messages[0], message_id=f"m{i}") for i in range(40)))
    def partial(response):
        return replace(response, answers=tuple(replace(answer, error_code="invalid_answer") if answer.question_id == "pair_0" else answer for answer in response.answers))
    calls, _ = install(monkeypatch, mutate=partial)
    result, _warnings = curator.annotate_curator_batch(agent, batch, "curator-run", max_input_chars=100_000)
    assert len(calls[0][1]["questions"]) == 32 and len(result.messages) == 40
    assert len(result.relation_annotations) == 31
    smaller = shrink_batch_for_timeout(result)
    assert len(smaller.messages) == 20 and len(smaller.to_model_payload()["relation_annotations"]) == 19
    changed = replace(smaller, formal_memories=(replace(batch.formal_memories[0], authority_version=99),))
    assert "relation_annotations" not in changed.to_model_payload()


def test_formal_host_facts_do_not_change_original_model_projection(prepared):
    _agent, batch, record = prepared
    formal = batch.formal_memories[0]
    assert formal.authority_version == record.version and formal.content_chars == len(formal.content_preview)
    assert formal.to_model() == replace(formal, authority_version=None, content_chars=None).to_model()
    assert "authority_version" not in formal.to_model() and "content_chars" not in formal.to_model()


def test_real_long_formal_body_is_incomplete_not_a_short_matching_preview(prepared, monkeypatch):
    agent, batch, record = prepared
    agent.memory.replace(record.entry_id, "完整正式材料" * 300, expected_version=record.version)
    empty = SimpleNamespace(list=lambda: [])
    formal = FormalMemorySource(agent.memory, empty, empty).read()
    assert formal[0].content_chars > len(formal[0].content_preview)
    batch = replace(batch, formal_memories=formal)
    calls, _ = install(monkeypatch)
    result, warnings = annotate(agent, batch)
    assert result is batch and not calls and warnings == ("memory_curator_relation:unknown:need_data",)


def test_deadline_expiring_during_formal_read_does_not_apply_or_send(prepared, monkeypatch):
    from agent_py_agent.agent.backends import gateway_request_limits
    agent, batch, _ = prepared
    clock = [100.0]
    monkeypatch.setattr(gateway_request_limits, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    original = agent.memory.all
    def slow_read():
        current = original()
        clock[0] = 200.0
        return current
    monkeypatch.setattr(agent.memory, "all", slow_read)
    calls, _ = install(monkeypatch)
    result, _warnings = annotate(agent, batch, caller_deadline=150.0)
    assert result is batch and not calls


def test_owner_only_independent_settings_and_yaml_defaults(tmp_path):
    host = host_at(tmp_path)
    field = "points.curator_relation.mode"
    view = settings(host, "read", {})
    assert view["effective"]["points"]["curator_relation"]["effective_mode"] == "off"
    assert view["field_scopes"][field] == ["owner"] and field in _fields(view)
    assert "正式记忆关系建议" in _field_label(view, field)
    patch(host, {"enabled": True, field: "apply", "background_timeout_seconds": 8, "stage_timeout_seconds": 1})
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    view = patch(host, {"enabled": False, "profile_id": ""}, scope="thread", thread_id=thread.thread_id)
    assert field not in _fields(view)
    assert view["effective"]["points"]["curator_relation"]["effective_mode"] == "apply"
    assert view["effective"]["points"]["curator_relation"]["max_request_seconds"] == 8
    assert view["effective"]["points"]["curator"]["mode"] == "off"
    with pytest.raises(ModelProfileError):
        patch(host, {field: "apply"}, scope="thread", thread_id=thread.thread_id)
    config = Path(__file__).parents[1] / "config" / "agent_config.yaml"
    for value in (load_config(config), AgentConfig(), normalize_memory_settings()[0]):
        assert value.memory_decision_curator_relation_mode == "off"
        assert value.memory_decision_curator_relation_timeout_seconds is None
        assert value.memory_decision_curator_relation_profile_id is None
    override = tmp_path / "settings.yaml"
    override.write_text("memory_decision_curator_relation_mode: observe\nmemory_decision_curator_relation_timeout_seconds: 0.75\n")
    assert load_config(override).memory_decision_curator_relation_mode == "observe"
    assert load_config(override).memory_decision_curator_relation_timeout_seconds == 0.75


@pytest.mark.parametrize("mode", ["observe", "apply"])
def test_real_service_worker_and_original_ledger(prepared, monkeypatch, tmp_path, mode):
    from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger
    from agent_py_agent.agent.backends.typesafe_decision import TypesafeDecisionBackend
    from agent_py_agent.agent.backends.typesafe_decision_wire import parse_typesafe_response
    from agent_py_agent.tests.test_decision_model_profiles import decision
    agent, batch, _ = prepared
    host = host_at(tmp_path / "host")
    host.memory = agent.memory
    key, _ = decision(host)
    patch(host, {"enabled": True, "profile_id": key, "points.curator_relation.mode": mode})
    calls = []
    def backend_decide(backend, request, *, deadline):
        calls.append(request)
        assert deadline <= caller_deadline
        raw = {"model": "actual-relation", "answers": {qid: {"type": "choice", "choice": "possible_update", "confidence": 1.0,
               "probabilities": {key: float(key == "possible_update") for key in spec["criteria"]}}
               for qid, spec in request.payload(backend.model_name)["questions"].items()}, "usage": {"input_tokens": 23}}
        return parse_typesafe_response(request, backend.model_name, raw)
    monkeypatch.setattr(TypesafeDecisionBackend, "decide", backend_decide)
    caller_deadline = time.monotonic() + 10
    result, warnings = annotate(host, batch, caller_deadline=caller_deadline)
    assert len(calls) == 1 and bool(result.relation_annotations) == (mode == "apply"), warnings
    record, = model_call_ledger(host).records()
    assert record.run_id == "curator-run" and record.metadata["thread_id"] == ""
    assert record.metadata["purpose"] == "decision" and record.metadata["auxiliary"] is True


@pytest.mark.parametrize("failure", [False, True])
def test_original_extraction_and_commit_remain_authority(prepared, monkeypatch, tmp_path, failure):
    from agent_py_agent.tests.test_memory_curator_v2 import (
        _conversation,
        _FailingBackend,
        _service,
        _StaticStructuredBackend,
        _valid_output,
    )
    agent, batch, _ = prepared
    store, thread, message = _conversation(tmp_path)
    backend = _FailingBackend() if failure else _StaticStructuredBackend(_valid_output(thread.thread_id, message.message_id, message.content))
    empty = SimpleNamespace(list=lambda: [])
    service = _service(tmp_path, backend, store, formal_memory_source=FormalMemorySource(agent.memory, empty, empty))
    install(monkeypatch, value="possible_conflict")
    service.dependencies = replace(service.dependencies, annotate_batch=lambda batch, run, deadline:
                                   curator.annotate_curator_batch(agent, batch, run, max_input_chars=40_000, caller_deadline=deadline))
    original_memory = Path(agent.memory.path).read_bytes()
    result = service.run(reason="admin", force=True)
    assert (result.status == "succeeded") is not failure
    assert Path(agent.memory.path).read_bytes() == original_memory
    if failure:
        assert not service.candidate_service.list()
        assert not service.state_store.load().per_thread_cursors
    else:
        assert "relation_annotations" in backend.prompts[0]
        candidate, = service.candidate_service.list()
        assert candidate.proposed_action == "add" and candidate.candidate_type == "long_term_fact"
        assert candidate.promotion_mode == "auto_eligible"
        assert service.state_store.load().per_thread_cursors[thread.thread_id] == message.message_id
