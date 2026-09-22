"""Curator 前置标注只改变临时提取上下文，原材料、证据和游标仍归原 Curator。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.decision_protocol import (
    DecisionAnswer,
    DecisionBinding,
    DecisionResponse,
)
from agent_py_agent.agent.memory_store import decision_curator as module
from agent_py_agent.agent.memory_store.curator_backend import (
    curator_prompt,
    shrink_batch_for_timeout,
)
from agent_py_agent.agent.memory_store.curator_inputs import CuratorInputBatch, CuratorMessageInput
from agent_py_agent.agent.tooling.cancellation import ToolCancelled


@pytest.fixture
def batch():
    return CuratorInputBatch(messages=tuple(
        CuratorMessageInput(f"m{i}", f"thread-{i}", "user", "internal", 1.0, "原始材料", "原始材料", f"hash-{i}", {})
        for i in range(4)
    ), audit_events=())


# LLM: fake 只替换外层策略调用，仍使用正式绑定与逐题响应对象，不提供任何记忆写入权。
# 函数用途: 观察消费者构造的 scope/材料并返回固定可审查的建议。
def install_decision(monkeypatch, *, mode="apply", status="success", mutate=None, error=None):
    observed = []
    def begin(agent, params, **kwargs):
        assert kwargs["scope"] == "owner_background"
        assert params.run_id == kwargs["operation_id"] == "curator-run"
        assert params.thread_id == params.task_id == params.request_id == "" and not params.task_attributes
        return SimpleNamespace(error_code="", enabled_points=() if mode == "off" else ("curator",))
    def decide(agent, params, stage, **kwargs):
        observed.append(kwargs)
        if error is not None:
            raise error
        answers = []
        for key in kwargs["questions"]:
            answers.append(DecisionAnswer(key, "choice", "fact" if key.endswith("tag") else "high"))
        response = DecisionResponse(DecisionBinding("curator", "owner", "curator-run", "revision", kwargs["candidates_revision"]),
                                    "digest", "configured", "actual-model", tuple(answers), b"{}")
        if mutate:
            response = mutate(response)
        return SimpleNamespace(mode=mode, status=status, may_apply=mode == "apply" and status == "success", response=response)
    monkeypatch.setattr(module, "begin_decision_stage", begin)
    monkeypatch.setattr(module, "decide", decide)
    monkeypatch.setattr(module, "decision_outcome_is_current", lambda *_: True)
    return observed


def annotate(batch):
    return module.annotate_curator_batch(object(), batch, "curator-run", max_input_chars=40_000)


@pytest.mark.parametrize("mode,status", [("off", "off"), ("observe", "success"), ("apply", "deadline"), ("apply", "cooldown"), ("apply", "configuration_required")])
def test_inactive_observe_or_failure_keeps_exact_original(batch, monkeypatch, mode, status):
    install_decision(monkeypatch, mode=mode, status=status)
    result, warnings = annotate(batch)
    assert result is batch and "decision_annotations" not in result.to_model_payload()
    assert (not warnings) == (status == "off")


def test_apply_only_adds_temporary_hints_and_preserves_every_source(batch, monkeypatch):
    observed = install_decision(monkeypatch)
    original = batch.to_model_payload()
    result, warnings = annotate(batch)
    assert result.messages is batch.messages and result.audit_events is batch.audit_events
    assert result.formal_memories is batch.formal_memories
    assert len(result.decision_annotations) == 4 and len(observed) == 1
    payload = result.to_model_payload()
    assert {key: payload[key] for key in original} == original
    assert payload["decision_annotations"][0]["model"] == "actual-model"
    assert "不得当作证据" in curator_prompt(result) and "memory_curator_decision:apply:success" in warnings


def test_bad_question_does_not_discard_successful_sibling(batch, monkeypatch):
    def partial(response):
        return replace(response, answers=tuple(replace(answer, error_code="invalid_answer") if answer.question_id.endswith("tag") else answer for answer in response.answers))
    install_decision(monkeypatch, mutate=partial)
    result, _ = annotate(batch)
    assert all(item.tag == "" and item.priority == "high" for item in result.decision_annotations)


def test_wrong_candidate_revision_is_rejected(batch, monkeypatch):
    install_decision(monkeypatch, mutate=lambda response: replace(response, binding=replace(response.binding, candidates_revision="old")))
    result, warnings = annotate(batch)
    assert result is batch and warnings == ("memory_curator_decision:apply:stale",)


def test_configuration_revocation_before_annotation_consumption_keeps_original(batch, monkeypatch):
    install_decision(monkeypatch)
    monkeypatch.setattr(module, "decision_outcome_is_current", lambda *_: False)
    result, warnings = annotate(batch)
    assert result is batch and warnings == ("memory_curator_decision:apply:stale",)


def test_material_mutation_during_decision_invalidates_annotation(batch, monkeypatch):
    def mutate(response):
        batch.messages[0].metadata["task_id"] = "changed-task"
        return response
    install_decision(monkeypatch, mutate=mutate)
    result, warnings = annotate(batch)
    assert result is batch and warnings == ("memory_curator_decision:apply:stale",)


def test_annotation_budget_never_makes_original_batch_invalid(batch, monkeypatch):
    install_decision(monkeypatch)
    result, warnings = module.annotate_curator_batch(object(), batch, "curator-run", max_input_chars=len(curator_prompt(batch)))
    assert result is batch and warnings == ("memory_curator_decision:apply:annotation_budget",)


def test_shrink_removes_only_hints_for_absent_sources(batch, monkeypatch):
    install_decision(monkeypatch)
    result, _ = annotate(batch)
    smaller = shrink_batch_for_timeout(result)
    assert len(smaller.messages) == 2
    hints = smaller.to_model_payload()["decision_annotations"]
    assert [item["source_id"] for item in hints] == ["m0", "m1"]
    assert smaller.messages == batch.messages[:2]


def test_changed_source_hash_drops_annotation_from_projection(batch, monkeypatch):
    install_decision(monkeypatch)
    result, _ = annotate(batch)
    result = replace(result, messages=(replace(result.messages[0], content_hash="different"), *result.messages[1:]))
    assert len(result.to_model_payload()["decision_annotations"]) == 3


def test_question_cap_never_discards_unannotated_material(batch, monkeypatch):
    many = replace(batch, messages=tuple(replace(batch.messages[0], message_id=f"m{i}") for i in range(100)))
    calls = install_decision(monkeypatch)
    result, _ = module.annotate_curator_batch(object(), many, "curator-run", max_input_chars=100_000)
    assert len(calls[0]["questions"]) == 64 and len(calls[0]["state"]["batch"]["messages"]) == 100
    assert len(result.messages) == 100 and len(result.decision_annotations) == 32


@pytest.mark.parametrize("error", [RuntimeError("private provider text"), ValueError("invalid config"), TimeoutError("late")])
def test_optional_failure_is_fixed_warning_without_private_prose(batch, monkeypatch, error):
    install_decision(monkeypatch, error=error)
    result, warnings = annotate(batch)
    assert result is batch and warnings == ("memory_curator_decision:off:enhancement_failed",)


@pytest.mark.parametrize("error", [InterruptedError(), ToolCancelled(), KeyboardInterrupt()])
def test_user_interruption_is_never_converted_to_enhancement_failure(batch, monkeypatch, error):
    install_decision(monkeypatch, error=error)
    with pytest.raises(type(error)):
        annotate(batch)


@pytest.mark.parametrize("mode,status", [("off", "off"), ("observe", "success"), ("apply", "success"), ("apply", "deadline")])
def test_original_curator_commit_chain_keeps_exact_evidence_and_cursor(tmp_path, monkeypatch, mode, status):
    from agent_py_agent.tests.test_memory_curator_v2 import (
        _conversation,
        _service,
        _StaticStructuredBackend,
        _valid_output,
    )
    store, thread, message = _conversation(tmp_path)
    backend = _StaticStructuredBackend(_valid_output(thread.thread_id, message.message_id, message.content))
    service = _service(tmp_path, backend, store)
    install_decision(monkeypatch, mode=mode, status=status)
    service.dependencies = replace(service.dependencies, annotate_batch=lambda batch, _run, _deadline: annotate(batch))
    result = service.run(reason="admin", force=True)
    assert result.status == "succeeded" and backend.calls == 1
    assert ("decision_annotations" in backend.prompts[0]) == (mode == "apply" and status == "success")
    state = service.state_store.load()
    assert state.per_thread_cursors[thread.thread_id] == message.message_id
    assert message.message_id in backend.prompts[0]


def test_extraction_failure_after_annotation_never_advances_cursor(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_memory_curator_v2 import _conversation, _FailingBackend, _service
    store, thread, _message = _conversation(tmp_path)
    service = _service(tmp_path, _FailingBackend(), store)
    install_decision(monkeypatch)
    service.dependencies = replace(service.dependencies, annotate_batch=lambda batch, _run, _deadline: annotate(batch))
    result = service.run(reason="admin", force=True)
    assert result.status == "failed"
    assert thread.thread_id not in service.state_store.load().per_thread_cursors


def test_all_invalid_answers_keep_original(batch, monkeypatch):
    def invalid(response):
        return replace(response, answers=tuple(replace(answer, error_code="invalid_answer") for answer in response.answers))
    install_decision(monkeypatch, mutate=invalid)
    result, _ = annotate(batch)
    assert result is batch


@pytest.mark.parametrize("remaining,headroom", [(190, 45), (120, 10), (100, 0), (-5, 0)])
def test_annotation_deadline_reserves_original_extraction_and_half_commit_buffer(monkeypatch, remaining, headroom):
    from datetime import datetime, timedelta, timezone

    from agent_py_agent.agent.memory_store import curator
    now = datetime.now(timezone.utc)
    class FrozenDatetime:
        @staticmethod
        def now(_zone):
            return now
        fromisoformat = datetime.fromisoformat
    monkeypatch.setattr(curator, "datetime", FrozenDatetime)
    monkeypatch.setattr(curator, "time", SimpleNamespace(monotonic=lambda: 1000.0))
    monkeypatch.setattr(curator, "extraction_budget_seconds", lambda _config: 100)
    context = SimpleNamespace(started_at=(now - timedelta(seconds=20)).isoformat(), expires_at=(now + timedelta(seconds=remaining)).isoformat())
    assert curator._annotation_deadline(context, object()) == 1000.0 + headroom


def test_invalid_lease_timestamp_does_not_grant_enhancement_budget(monkeypatch):
    from agent_py_agent.agent.memory_store import curator
    monkeypatch.setattr(curator, "time", SimpleNamespace(monotonic=lambda: 1000.0))
    assert curator._annotation_deadline(SimpleNamespace(started_at="broken", expires_at=""), object()) == 1000.0


def test_original_lease_budget_and_extraction_share_single_helper():
    from agent_py_agent.agent.memory_store.curator import _lease_seconds
    from agent_py_agent.agent.memory_store.curator_backend import extraction_budget_seconds
    from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorConfig
    config = MemoryCuratorConfig(timeout_seconds=17, max_retries=2, max_input_chars=40_000)
    assert extraction_budget_seconds(config) == 17 * 8 * 3
    assert _lease_seconds(config) == extraction_budget_seconds(config) + 90


@pytest.mark.parametrize("mode", ["off", "observe", "apply"])
def test_owner_background_runs_original_service_worker_and_ledger(tmp_path, batch, monkeypatch, mode):
    from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger
    from agent_py_agent.agent.backends.typesafe_decision import TypesafeDecisionBackend
    from agent_py_agent.agent.backends.typesafe_decision_wire import parse_typesafe_response
    from agent_py_agent.tests.test_decision_model_profiles import decision
    from agent_py_agent.tests.test_decision_settings import host_at, patch
    host = host_at(tmp_path)
    key, _ = decision(host)
    patch(host, {"enabled": True, "profile_id": key, "points.curator.mode": mode})
    calls = []
    def backend_decide(backend, request, *, deadline):
        calls.append(request)
        assert request.binding.thread_id == request.binding.task_id == ""
        assert request.binding.run_id == "curator-run"
        raw = {"model": "curator-decision-actual", "answers": {
            question: {"type": "choice", "choice": "fact" if question.endswith("tag") else "normal", "confidence": 1.0,
                       "probabilities": {key: float(key == ("fact" if question.endswith("tag") else "normal")) for key in spec["criteria"]}}
            for question, spec in request.payload(backend.model_name)["questions"].items()
        }, "usage": {"input_tokens": 17, "output_tokens": 3}}
        return parse_typesafe_response(request, backend.model_name, raw)
    monkeypatch.setattr(TypesafeDecisionBackend, "decide", backend_decide)
    result, warnings = module.annotate_curator_batch(host, batch, "curator-run", max_input_chars=40_000)
    assert len(calls) == (0 if mode == "off" else 1), warnings
    assert bool(result.decision_annotations) == (mode == "apply"), warnings
    records = model_call_ledger(host).records()
    assert len(records) == len(calls)
    if calls:
        record = records[0]
        assert record.run_id == "curator-run" and record.metadata["thread_id"] == ""
        assert record.metadata["purpose"] == "decision" and record.metadata["auxiliary"] is True


def test_expired_caller_budget_with_huge_setting_keeps_original_extraction(tmp_path, monkeypatch):
    import time

    from agent_py_agent.agent.backends.typesafe_decision import TypesafeDecisionBackend
    from agent_py_agent.tests.test_decision_model_profiles import decision
    from agent_py_agent.tests.test_decision_settings import host_at, patch
    from agent_py_agent.tests.test_memory_curator_v2 import (
        _conversation,
        _service,
        _StaticStructuredBackend,
        _valid_output,
    )
    host = host_at(tmp_path / "owner")
    key, _ = decision(host)
    patch(host, {"enabled": True, "profile_id": key, "points.curator.mode": "apply", "background_timeout_seconds": 1e9})
    monkeypatch.setattr(TypesafeDecisionBackend, "decide", lambda *_args, **_kwargs: pytest.fail("expired lease cannot start decision"))
    store, thread, message = _conversation(tmp_path)
    backend = _StaticStructuredBackend(_valid_output(thread.thread_id, message.message_id, message.content))
    service = _service(tmp_path, backend, store)
    service.dependencies = replace(service.dependencies, annotate_batch=lambda batch, run, _deadline:
        module.annotate_curator_batch(host, batch, run, max_input_chars=40_000, caller_deadline=time.monotonic() - 1))
    result = service.run(reason="admin", force=True)
    assert result.status == "succeeded" and backend.calls == 1
    assert "decision_annotations" not in backend.prompts[0]
    assert service.state_store.load().per_thread_cursors[thread.thread_id] == message.message_id


@pytest.mark.parametrize("outcome", ["not_needed", "need_data", "no_match", "abstain"])
@pytest.mark.parametrize("field", ["tag", "priority"])
def test_non_selection_outcomes_are_distinct_hints_not_fact_labels(batch, monkeypatch, outcome, field):
    batch = replace(batch, messages=(replace(batch.messages[0], full_content="完整原始材料", content_preview="完整"),))
    def answer(response):
        return replace(response, answers=tuple(replace(item, value=outcome) if item.question_id.endswith(field) else item for item in response.answers))
    calls = install_decision(monkeypatch, mutate=answer)
    result, _ = annotate(batch)
    hint = result.decision_annotations[0]
    assert getattr(hint, field) == "" and getattr(hint, field + "_outcome") == outcome
    assert hint.required_refs == ((("full_source_ref", "message:m0"),) if outcome == "need_data" else ())
    question = calls[0]["questions"][f"item_0_{field}"]
    assert {"not_needed", "need_data", "no_match", "abstain"} <= question["criteria"].keys()
    assert question["criteria"]["need_data"]["required_refs"] == [{"kind": "full_source_ref", "ref": "message:m0"}]
    assert result.messages is batch.messages and result.messages[0].full_content == "完整原始材料"
    payload = result.to_model_payload()["decision_annotations"][0]
    assert payload[field + "_outcome"] == outcome


def test_off_stage_does_not_prepare_or_encode_any_material(batch, monkeypatch):
    install_decision(monkeypatch, mode="off", status="off")
    monkeypatch.setattr(module, "_decision_material", lambda *_args: pytest.fail("off must not prepare batch"))
    result, warnings = annotate(batch)
    assert result is batch and not warnings


def test_unavailable_stage_does_not_prepare_material_or_break_original(batch, monkeypatch):
    monkeypatch.setattr(module, "begin_decision_stage", lambda *_args, **_kwargs: SimpleNamespace(error_code="configuration_unavailable", enabled_points=()))
    monkeypatch.setattr(module, "_decision_material", lambda *_args: pytest.fail("unavailable stage cannot prepare batch"))
    result, warnings = annotate(batch)
    assert result is batch and warnings == ("memory_curator_decision:unknown:stage_unavailable",)
