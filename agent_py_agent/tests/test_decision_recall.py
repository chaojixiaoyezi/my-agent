"""召回决策只排列原预算后候选，覆盖关闭、非选择、版本撤销和原主链组合。"""
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RuntimeContextRequest
from agent_py_agent.agent.agent_core.runtime.loop_support import (
    _formal_memories_for_request,
    _refresh_recall_candidates,
)
from agent_py_agent.agent.backends.decision_protocol import (
    DecisionAnswer,
    DecisionBinding,
    DecisionResponse,
)
from agent_py_agent.agent.memory_store import MemoryRecord
from agent_py_agent.agent.memory_store import decision_recall as module
from agent_py_agent.agent.memory_store.recall import MemoryRecallScope
from agent_py_agent.agent.tooling.cancellation import ToolCancelled


def test_supplemental_queries_are_finite_parts_of_the_original_prompt():
    prompt = "请分析这份报告的主要结论。对比去年同季度的数据变化。指出需要核对的异常指标。"
    candidates = module.supplemental_query_candidates(prompt)

    assert [query for _, query in candidates] == [
        "请分析这份报告的主要结论", "对比去年同季度的数据变化", "指出需要核对的异常指标",
    ]
    assert len({key for key, _ in candidates}) == len(candidates)


def test_supplemental_queries_do_not_use_partial_oversized_or_simple_prompt():
    assert module.supplemental_query_candidates("请帮我总结今天的情况") == ()
    assert module.supplemental_query_candidates("甲" * 4097 + "。查找历史记录") == ()
    assert len(module.supplemental_query_candidates("。".join(f"核对第{i}项材料的来源" for i in range(12)))) <= 4


@pytest.fixture
def prepared():
    records = [MemoryRecord("user", f"记忆正文{i}", entry_id=f"m{i}", kind="fact", version=1,
                           attributes={"scope_type": "personal", "scope_key": "personal", "origin": "user_explicit"})
               for i in range(3)]
    request = RuntimeContextRequest("当前问题", None, False, request_id="request-1", run_id="run-1", task_id="task-1",
                                    task_attributes={"agent_thread_id": "thread-1"})
    host = SimpleNamespace(backend=SimpleNamespace(name="echo", model_name="main-model"),
                           config=SimpleNamespace(model_context_window_tokens=8000))
    scope = MemoryRecallScope.from_runtime(task_id=request.task_id, task_attributes=request.task_attributes)
    return host, request, records, scope


# LLM: fake 只替换公共策略边界，答案仍使用原合同；当前参数对象必须原样进入服务，不从记忆或文本补身份。
# 函数用途: 观察准备/调用并返回可审查的优先级建议。
def install(monkeypatch, *, mode="apply", status="success", choice=None, mutate=None, error=None):
    calls = []
    def begin(agent, request, **kwargs):
        assert kwargs["operation_id"].startswith("recall:")
        return SimpleNamespace(error_code="", deadline=time.monotonic() + 5, enabled_points=() if mode == "off" else ("recall",))
    def decide(agent, request, stage, **kwargs):
        calls.append((request, kwargs))
        if error:
            raise error
        answers = tuple(DecisionAnswer(key, "choice", choice or ("later" if index == 0 else "first"))
                        for index, key in enumerate(kwargs["questions"]))
        response = DecisionResponse(DecisionBinding("recall", "owner", "operation", "policy", kwargs["candidates_revision"]),
                                    "digest", "decision-config", "decision-actual", answers, b"{}")
        if mutate:
            response = mutate(response)
        return SimpleNamespace(mode=mode, status=status, may_apply=mode == "apply" and status == "success", response=response)
    monkeypatch.setattr(module, "begin_decision_stage", begin)
    monkeypatch.setattr(module, "decide", decide)
    monkeypatch.setattr(module, "decision_outcome_is_current", lambda *_args: True, raising=False)
    return calls


def run(prepared, *, refresh=None):
    host, request, records, scope = prepared
    return module.rerank_recalled_memories(host, request, records, recall_scope=scope,
                                          refresh=refresh or (lambda: list(records)))


@pytest.mark.parametrize("mode,status", [("off", "off"), ("observe", "success"), ("apply", "deadline"), ("apply", "cooldown"), ("apply", "error")])
def test_off_observe_and_failure_preserve_original_order(prepared, monkeypatch, mode, status):
    install(monkeypatch, mode=mode, status=status)
    records, _ = run(prepared, refresh=lambda: pytest.fail("no adoption cannot refresh"))
    assert records is prepared[2]


def test_off_never_prepares_material(prepared, monkeypatch):
    install(monkeypatch, mode="off")
    monkeypatch.setattr(module, "_material", lambda *_args: pytest.fail("disabled cannot encode records"))
    assert run(prepared)[0] is prepared[2]


def test_apply_stable_sort_keeps_hot_lesson_slots_and_all_objects(prepared, monkeypatch):
    host, request, records, scope = prepared
    hot = replace(records[0], kind="hot", entry_id="hot")
    lesson = replace(records[0], kind="lesson", entry_id="lesson")
    records = [hot, records[0], lesson, *records[1:]]
    original = [vars(record).copy() for record in records]
    calls = install(monkeypatch)
    result, _ = run((host, request, records, scope))
    assert [record.entry_id for record in result] == ["hot", "m1", "lesson", "m2", "m0"]
    assert result[0] is hot and result[2] is lesson
    assert sorted(map(id, result)) == sorted(map(id, records))
    assert [vars(record) for record in records] == original
    assert calls[0][0] is request and len(calls[0][1]["questions"]) == 3


@pytest.mark.parametrize("choice", ["not_needed", "need_data", "no_match", "abstain"])
def test_non_selection_keeps_all_original_material(prepared, monkeypatch, choice):
    calls = install(monkeypatch, choice=choice)
    result, finding = run(prepared)
    assert result == prepared[2] and finding.endswith("retain_order:" + choice)
    question = calls[0][1]["questions"]["memory_0"]
    assert question["criteria"]["need_data"]["required_refs"] == [{"kind": "memory_source_ref", "ref": "memory:m0"}]


def test_one_bad_question_cannot_reorder_or_discard_any_record(prepared, monkeypatch):
    install(monkeypatch, mutate=lambda response: replace(response, answers=(replace(response.answers[0], error_code="missing_answer"), *response.answers[1:])))
    assert run(prepared)[0] == prepared[2]


@pytest.mark.parametrize("change", ["revision", "body", "scope", "model", "backend", "attrs"])
def test_current_candidate_and_model_binding_rejects_stale_suggestion(prepared, monkeypatch, change):
    host, request, records, _ = prepared
    def mutate(response):
        if change == "revision":
            return replace(response, binding=replace(response.binding, candidates_revision="old"))
        if change == "body":
            records[0].content = "更新后的正文"
        elif change == "scope":
            records[0].attributes["scope_key"] = "project:changed"
        elif change == "model":
            host.backend.model_name = "new-model"
        elif change == "backend":
            host.backend = SimpleNamespace(name="echo", model_name="main-model")
        elif change == "attrs":
            request.task_attributes["project_id"] = "changed"
        return response
    install(monkeypatch, mutate=mutate)
    result, finding = run(prepared)
    assert result == records and finding.endswith("stale")


def test_revoked_source_uses_refreshed_authorized_baseline_not_old_list(prepared, monkeypatch):
    install(monkeypatch)
    result, finding = run(prepared, refresh=lambda: prepared[2][1:])
    assert [record.entry_id for record in result] == ["m1", "m2"] and finding.endswith("stale")


def test_refresh_deadline_keeps_current_order(prepared, monkeypatch):
    install(monkeypatch)
    now = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(module, "begin_decision_stage", lambda *_args, **_kwargs: SimpleNamespace(error_code="", deadline=101, enabled_points=("recall",)))
    def refresh():
        now[0] = 102
        return list(prepared[2])
    result, finding = run(prepared, refresh=refresh)
    assert result == prepared[2] and finding.endswith("stale")


@pytest.mark.parametrize("error", [RuntimeError("private provider detail"), ValueError("invalid")])
def test_ordinary_enhancement_error_keeps_original(prepared, monkeypatch, error):
    install(monkeypatch, error=error)
    assert run(prepared) == (prepared[2], "memory_recall_decision:enhancement_failed")


@pytest.mark.parametrize("error", [InterruptedError(), ToolCancelled(), KeyboardInterrupt()])
def test_user_interrupt_propagates(prepared, monkeypatch, error):
    install(monkeypatch, error=error)
    with pytest.raises(type(error)):
        run(prepared)


def test_more_than_64_candidates_use_original_protocol_budget_without_dropping_records(prepared, monkeypatch):
    host, request, records, scope = prepared
    many = [replace(records[0], entry_id=f"m{index}") for index in range(65)]
    calls = install(monkeypatch)
    result, _ = run((host, request, many, scope))
    assert len(calls) == 1 and len(calls[0][1]["questions"]) == 65
    assert sorted(map(id, result)) == sorted(map(id, many))


def test_task_local_context_never_reaches_recall_decision(prepared, monkeypatch):
    host, request, records, scope = prepared
    monkeypatch.setattr(module, "rerank_recalled_memories", lambda *_args, **_kwargs: pytest.fail("task_local cannot recall"))
    routed = SimpleNamespace(injected_sections=["old"], findings=[])
    assert _formal_memories_for_request(host, request, routed, recall_scope=scope, long_term_memories=records, skip_formal_recall=True) == []
    assert routed.injected_sections == []


def test_refresh_uses_current_active_scoped_sources_and_original_budget(prepared):
    host, _request, records, scope = prepared
    host.memory_hot = SimpleNamespace(list=lambda: [])
    host.memory_lessons = SimpleNamespace(list=lambda: [])
    other_scope = replace(records[1], attributes={"scope_type": "company", "scope_key": "company:other"})
    updated = replace(records[2], content="当前有效正文", kind="note", version=2)
    unrelated = replace(records[0], entry_id="new-not-selected")
    host.memory = SimpleNamespace(all=lambda: [other_scope, updated, unrelated])
    result = _refresh_recall_candidates(host, records, [], scope)
    assert [record.entry_id for record in result] == ["m2"] and result[0].content == "当前有效正文"


def test_consumption_rechecks_policy_after_memory_refresh(prepared, monkeypatch):
    install(monkeypatch)
    checked = []
    monkeypatch.setattr(module, "decision_outcome_is_current", lambda *_args: checked.append(True) or False)
    result, finding = run(prepared)
    assert result == prepared[2] and checked == [True] and finding.endswith("stale")


def test_off_and_same_priority_leave_existing_prompt_identical(prepared, monkeypatch):
    from agent_py_agent.agent.prompting_parts.memory_context import memory_context_text
    baseline = memory_context_text(prepared[2])
    install(monkeypatch, choice="normal")
    result, _ = run(prepared)
    assert memory_context_text(result) == baseline


# LLM: 原文件仓库与 owner/thread 配置均真实创建于临时目录，仅原生供应商 decide 用自有fixture代替，不收费联网。
# 函数用途: 构造可经过正式配置、worker、账本和召回接线的宿主。
def real_host(tmp_path, records, mode):
    from agent_py_agent.agent.memory_store import JsonlMemory
    from agent_py_agent.tests.test_decision_model_profiles import decision
    from agent_py_agent.tests.test_decision_settings import host_at, patch
    host = host_at(tmp_path)
    key, _ = decision(host)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    patch(host, {"enabled": True, "profile_id": key, "points.recall.mode": mode})
    host.backend = SimpleNamespace(name="echo", model_name="original-main")
    host.memory = JsonlMemory(tmp_path / "memory.jsonl")
    for record in records:
        host.memory.add_record(record)
    host.memory_hot = SimpleNamespace(list=lambda: [])
    host.memory_lessons = SimpleNamespace(list=lambda: [])
    request = RuntimeContextRequest("相关问题", None, False, request_id="request", run_id="run", task_id="task",
                                    task_attributes={"agent_thread_id": thread.thread_id})
    return host, request


# LLM: 只替换实际 provider 的 decide，保留正式服务、有界 worker、准入和 ledger，响应经正式 wire 校验。
# 函数用途: 用固定优先级模拟原生供应商，不伪造调用或用量路径。
def install_backend(monkeypatch):
    from agent_py_agent.agent.backends.typesafe_decision import TypesafeDecisionBackend
    from agent_py_agent.agent.backends.typesafe_decision_wire import parse_typesafe_response
    calls = []
    def decide(backend, request, *, deadline):
        calls.append(request)
        questions = request.payload(backend.model_name)["questions"]
        raw = {"model": "actual-decision", "answers": {
            question: {"type": "choice", "choice": "later" if question == "memory_0" else "first", "confidence": 1.0,
                       "probabilities": {key: float(key == ("later" if question == "memory_0" else "first")) for key in spec["criteria"]}}
            for question, spec in questions.items()}, "usage": {"input_tokens": 29, "output_tokens": 4}}
        return parse_typesafe_response(request, backend.model_name, raw)
    monkeypatch.setattr(TypesafeDecisionBackend, "decide", decide)
    return calls


@pytest.mark.parametrize("mode", ["off", "observe", "apply"])
def test_formal_budgeted_recall_enters_real_service_worker_and_usage(tmp_path, prepared, monkeypatch, mode):
    from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger
    host, request = real_host(tmp_path, prepared[2], mode)
    calls = install_backend(monkeypatch)
    records = host.memory.all()
    routed = SimpleNamespace(receipts=[], findings=[], injected_sections=[])
    result = _formal_memories_for_request(host, request, routed, recall_scope=prepared[3], long_term_memories=records, skip_formal_recall=False)
    assert [record.entry_id for record in result] == (["m1", "m2", "m0"] if mode == "apply" else ["m0", "m1", "m2"]), routed.findings
    assert len(calls) == (0 if mode == "off" else 1)
    if calls:
        binding = calls[0].binding
        assert binding.run_id == request.run_id and binding.task_id == request.task_id
        assert binding.thread_id == request.task_attributes["agent_thread_id"]
        ledger = model_call_ledger(host).records()
        assert len(ledger) == 1 and ledger[0].metadata["purpose"] == "decision"
        assert ledger[0].metadata["thread_id"] == binding.thread_id


def test_existing_budget_selects_candidates_before_optional_ranking(tmp_path, prepared, monkeypatch):
    records = [replace(record, content="材料" * 1999 + record.entry_id) for record in prepared[2]]
    host, request = real_host(tmp_path, records, "apply")
    calls = install_backend(monkeypatch)
    routed = SimpleNamespace(receipts=[], findings=[], injected_sections=[])
    result = _formal_memories_for_request(host, request, routed, recall_scope=prepared[3], long_term_memories=host.memory.all(), skip_formal_recall=False)
    assert len(calls[0].payload("model")["state"]["memories"]) == 2
    assert [record.entry_id for record in result] == ["m1", "m0"]
    assert sum(len(record.content) for record in result) == 8000


def test_configuration_disabled_during_source_refresh_rejects_old_order(tmp_path, prepared, monkeypatch):
    from agent_py_agent.tests.test_decision_settings import patch
    host, request = real_host(tmp_path, prepared[2], "apply")
    install_backend(monkeypatch)
    records = host.memory.all()
    def refresh():
        patch(host, {"enabled": False})
        return list(records)
    result, finding = module.rerank_recalled_memories(host, request, records, recall_scope=prepared[3], refresh=refresh)
    assert result == records and finding.endswith("stale")


def test_thread_identity_conflict_never_calls_provider(tmp_path, prepared, monkeypatch):
    from agent_py_agent.agent.runtime_context import (
        restore_current_subagent_context,
        set_current_subagent_context,
    )
    host, request = real_host(tmp_path, prepared[2], "apply")
    calls = install_backend(monkeypatch)
    previous = set_current_subagent_context(host, run_id="other-run", task_attributes={"agent_thread_id": "other-thread"})
    try:
        records = host.memory.all()
        result, _ = module.rerank_recalled_memories(host, request, records, recall_scope=prepared[3], refresh=lambda: records)
    finally:
        restore_current_subagent_context(host, previous)
    assert result is records and not calls


def test_prepare_context_reuses_sorted_records_in_original_loop_without_second_call(tmp_path, prepared, monkeypatch):
    from agent_py_agent.agent.agent_core.runtime import loop_support
    host, request = real_host(tmp_path, prepared[2], "apply")
    request = replace(request, user_prompt="记忆正文")
    host.config.memory_top_k = 5
    calls = install_backend(monkeypatch)
    routed = SimpleNamespace(receipts=[], findings=[], injected_sections=[])
    monkeypatch.setattr(loop_support, "_tool_snapshots_for_run", lambda *_args: (None, None))
    monkeypatch.setattr(loop_support, "_routed_memory_context_for_request", lambda *_args, **_kwargs: routed)
    monkeypatch.setattr(loop_support, "_resume_context_for_request", lambda *_args, **_kwargs: (SimpleNamespace(injected=False), ""))
    seen = []
    def bundle(_agent, _request, **kwargs):
        seen.append(kwargs["memories"])
        return None
    monkeypatch.setattr(loop_support, "build_runtime_main_context_bundle", bundle)
    context = loop_support._prepare_runtime_context(host, request)
    assert len(calls) == 1
    original_ids = [row["entry_id"] for row in calls[0].payload("model")["state"]["memories"]]
    assert [record.entry_id for record in context.memories] == [*original_ids[1:], original_ids[0]]
    params = loop_support._runtime_loop_params(request.user_prompt, context, loop_support.RunParams(request_id=request.request_id, run_id=request.run_id))
    assert params.memories is context.memories and seen == [context.memories] and len(calls) == 1


def test_child_uses_exact_child_thread_not_parent_conversation(tmp_path, prepared, monkeypatch):
    from agent_py_agent.agent.runtime_context import (
        restore_current_subagent_context,
        set_current_subagent_context,
    )
    host, request = real_host(tmp_path, prepared[2], "apply")
    child_thread = request.task_attributes["agent_thread_id"]
    attrs = {"agent_thread_id": child_thread, "conversation_thread_id": "parent-thread"}
    request = replace(request, run_id="child-run", task_attributes=attrs)
    calls = install_backend(monkeypatch)
    previous = set_current_subagent_context(host, run_id="child-run", task_attributes=attrs)
    try:
        records = host.memory.all()
        result, _ = module.rerank_recalled_memories(host, request, records, recall_scope=prepared[3], refresh=lambda: list(records))
    finally:
        restore_current_subagent_context(host, previous)
    assert [record.entry_id for record in result] == ["m1", "m2", "m0"]
    assert calls[0].binding.thread_id == child_thread and calls[0].binding.run_id == "child-run"


def test_main_model_switch_during_final_policy_read_cannot_apply_old_ranking(prepared, monkeypatch):
    install(monkeypatch)
    def changed(*_args):
        prepared[0].backend.model_name = "changed-during-policy-read"
        return True
    monkeypatch.setattr(module, "decision_outcome_is_current", changed)
    result, finding = run(prepared)
    assert result == prepared[2] and finding.endswith("stale")


def test_non_selection_and_question_failure_reasons_remain_distinct(prepared, monkeypatch):
    def partial(response):
        return replace(response, answers=(replace(response.answers[0], value="need_data"),
                                          replace(response.answers[1], value="abstain"),
                                          replace(response.answers[2], error_code="invalid_answer")))
    install(monkeypatch, mutate=partial)
    result, finding = run(prepared)
    assert result == prepared[2] and finding.endswith("retain_order:abstain,invalid_answer,need_data")
