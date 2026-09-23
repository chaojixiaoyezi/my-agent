"""召回前补充只选择有限查询，原结果、scope、预算与访问确认保持宿主权威。"""

import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime import loop_support
from agent_py_agent.agent.agent_core.runtime.loop_models import RuntimeContextRequest
from agent_py_agent.agent.conversation import decision_service
from agent_py_agent.agent.memory_store import decision_recall
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory, MemoryRecord
from agent_py_agent.agent.memory_store.recall import (
    MemoryRecallScope,
    long_term_record_matches_scope,
)


@pytest.fixture
def prepared():
    original = MemoryRecord("user", "Orion release details", entry_id="original", kind="fact", version=1,
                            attributes={"scope_type": "personal", "scope_key": "personal"})
    extra = MemoryRecord("user", "Lyra deployment checklist", entry_id="extra", kind="fact", version=1,
                         attributes={"scope_type": "personal", "scope_key": "personal"})

    class Memory:
        calls = []
        touches = []

        def search_scoped_candidates(self, query, top_k, predicate):
            self.calls.append((query, top_k))
            return [row for row in (original, extra) if predicate(row)]

        def confirm_scoped_access(self, rows, predicate):
            self.touches.extend(row.entry_id for row in rows)
            return [row for row in rows if predicate(row)]

    memory = Memory()
    agent = SimpleNamespace(memory=memory, backend=SimpleNamespace(name="echo", model_name="main"),
                            config=SimpleNamespace(model_context_window_tokens=8000))
    request = RuntimeContextRequest("请核对 Orion 发布记录。查找 Lyra 部署清单。", None, False,
                                    request_id="request", run_id="run", task_id="task",
                                    task_attributes={"agent_thread_id": "thread"})
    stage = SimpleNamespace(error_code="", enabled_points=("pre_recall",), deadline=time.monotonic() + 5)
    scope = MemoryRecallScope.from_runtime()
    return agent, request, stage, scope, original, extra


def _run(prepared, monkeypatch, *, choice="query_1", mode="apply", budget=100, refresh=None):
    agent, request, stage, scope, original, extra = prepared
    response = SimpleNamespace(answers=(SimpleNamespace(question_id="supplemental_query", value=choice, error_code=""),))
    outcome = SimpleNamespace(mode=mode, status="success", may_apply=mode == "apply", response=response)
    monkeypatch.setattr(decision_recall, "decide", lambda *_args, **_kwargs: outcome)
    monkeypatch.setattr(decision_recall, "decision_outcome_is_current", lambda *_args: True)
    return decision_recall.supplement_recalled_memories(
        agent, request, [original], recall_scope=scope, stage=stage,
        queries=(("query_1", "Lyra deployment checklist"),), slots=1, search_top_k=5,
        remaining_chars=budget, refresh=refresh or (lambda: [original]),
    )


def test_apply_appends_only_new_in_scope_record_and_confirms_access(prepared, monkeypatch):
    records, finding = _run(prepared, monkeypatch)
    agent, _, _, _, original, extra = prepared
    assert records == [original, extra]
    assert finding == "memory_pre_recall_decision:apply:success"
    assert agent.memory.calls == [("Lyra deployment checklist", 5)]
    assert agent.memory.touches == ["extra"]


@pytest.mark.parametrize("choice,mode", [("not_needed", "apply"), ("need_data", "apply"),
                                          ("no_match", "apply"), ("abstain", "apply"),
                                          ("query_1", "observe")])
def test_non_selection_and_observe_keep_original_without_search(prepared, monkeypatch, choice, mode):
    records, _ = _run(prepared, monkeypatch, choice=choice, mode=mode)
    assert records == [prepared[4]]
    assert prepared[0].memory.calls == []
    assert prepared[0].memory.touches == []


def test_budget_rejects_candidate_without_touch(prepared, monkeypatch):
    records, finding = _run(prepared, monkeypatch, budget=3)
    assert records == [prepared[4]]
    assert finding == "memory_pre_recall_decision:apply:success:no_addition"
    assert prepared[0].memory.calls
    assert prepared[0].memory.touches == []


def test_disabled_point_keeps_original_without_decision_or_search(prepared, monkeypatch):
    agent, request, stage, scope, original, _extra = prepared
    stage.enabled_points = ()
    monkeypatch.setattr(decision_recall, "decide", lambda *_args, **_kwargs: pytest.fail("不应调用 Jev"))
    records, finding = decision_recall.supplement_recalled_memories(
        agent, request, [original], recall_scope=scope, stage=stage,
        queries=(("query_1", "Lyra deployment checklist"),), slots=1, search_top_k=5,
        remaining_chars=100, refresh=lambda: pytest.fail("不应刷新正式源"),
    )
    assert records == [original]
    assert finding == ""
    assert agent.memory.calls == []


def test_revoked_original_source_is_not_resurrected(prepared, monkeypatch):
    records, finding = _run(prepared, monkeypatch, refresh=lambda: [])
    assert records == []
    assert finding == "memory_pre_recall_decision:apply:stale"
    assert prepared[0].memory.calls == []
    assert prepared[0].memory.touches == []


def test_optional_search_failure_returns_refreshed_authority(prepared, monkeypatch):
    agent, _request, _stage, _scope, original, _extra = prepared
    current = MemoryRecord("user", original.content, entry_id=original.entry_id, kind=original.kind,
                           version=original.version, attributes=original.attributes)

    def fail_search(_query, _top_k, _predicate):
        raise OSError("derived search unavailable")

    monkeypatch.setattr(agent.memory, "search_scoped_candidates", fail_search)
    records, finding = _run(prepared, monkeypatch, refresh=lambda: [current])
    assert records == [current]
    assert records[0] is current
    assert finding == "memory_pre_recall_decision:enhancement_failed"
    assert agent.memory.touches == []


def test_cancel_after_optional_search_propagates_without_confirming_access(prepared, monkeypatch):
    checks = iter((None, InterruptedError("停止")))

    def interrupt():
        result = next(checks)
        if result:
            raise result

    monkeypatch.setattr(decision_recall, "_check_interrupted", interrupt)
    with pytest.raises(InterruptedError, match="停止"):
        _run(prepared, monkeypatch)
    assert prepared[0].memory.calls
    assert prepared[0].memory.touches == []


def test_real_scoped_lexical_baseline_does_not_double_touch_or_claim_addition(prepared, monkeypatch, tmp_path):
    agent, request, stage, scope, _original, _extra = prepared
    memory = JsonlMemory(tmp_path / "memory.jsonl", ops_path=tmp_path / "ops.jsonl")
    for entry_id, content in (("orion", "Orion release details for Friday"),
                              ("lyra", "Lyra deployment checklist before rollout")):
        memory.add_record(MemoryRecord("user", content, entry_id=entry_id, kind="fact",
                                       attributes={"scope_type": "personal", "scope_key": "personal"}))
    agent.memory = memory
    def predicate(row):
        return long_term_record_matches_scope(row, scope)
    base = memory.search_scoped(request.user_prompt, 3, predicate)
    assert {row.entry_id for row in base} == {"orion", "lyra"}
    assert memory.flush_access_events() == 2
    answer = SimpleNamespace(question_id="supplemental_query", value="query_2", error_code="")
    outcome = SimpleNamespace(mode="apply", status="success", may_apply=True,
                              response=SimpleNamespace(answers=(answer,)))
    monkeypatch.setattr(decision_recall, "decide", lambda *_args, **_kwargs: outcome)
    monkeypatch.setattr(decision_recall, "decision_outcome_is_current", lambda *_args: True)
    ids = {row.entry_id for row in base}
    records, finding = decision_recall.supplement_recalled_memories(
        agent, request, base, recall_scope=scope, stage=stage,
        queries=decision_recall.supplemental_query_candidates(request.user_prompt),
        slots=1, search_top_k=3, remaining_chars=1000,
        refresh=lambda: [row for row in memory.all() if row.entry_id in ids],
    )
    assert [row.entry_id for row in records] == [row.entry_id for row in base]
    assert finding == "memory_pre_recall_decision:apply:success:no_addition"
    assert memory.flush_access_events() == 0


def test_real_scoped_search_can_fill_a_baseline_miss_without_retouching_it(prepared, monkeypatch, tmp_path):
    agent, _request, stage, scope, _original, _extra = prepared
    memory = JsonlMemory(tmp_path / "memory.jsonl", ops_path=tmp_path / "ops.jsonl")
    for entry_id, content in (("orion", "Orion release details and Orion timetable"),
                              ("lyra", "Lyra deployment checklist before rollout")):
        memory.add_record(MemoryRecord("user", content, entry_id=entry_id, kind="fact",
                                       attributes={"scope_type": "personal", "scope_key": "personal"}))
    agent.memory = memory
    request = RuntimeContextRequest(
        "请核对 Orion release details 与 Orion timetable。再找 Lyra deployment checklist。", None, False,
        request_id="request", run_id="run", task_id="task",
        task_attributes={"agent_thread_id": "thread"},
    )
    queries = decision_recall.supplemental_query_candidates(request.user_prompt)

    def predicate(row):
        return long_term_record_matches_scope(row, scope)

    base = memory.search_scoped(request.user_prompt, 1, predicate)
    assert [row.entry_id for row in base] == ["orion"]
    assert memory.flush_access_events() == 1
    answer = SimpleNamespace(question_id="supplemental_query", value="query_2", error_code="")
    outcome = SimpleNamespace(mode="apply", status="success", may_apply=True,
                              response=SimpleNamespace(answers=(answer,)))
    monkeypatch.setattr(decision_recall, "decide", lambda *_args, **_kwargs: outcome)
    monkeypatch.setattr(decision_recall, "decision_outcome_is_current", lambda *_args: True)
    records, finding = decision_recall.supplement_recalled_memories(
        agent, request, base, recall_scope=scope, stage=stage, queries=queries,
        slots=1, search_top_k=1, remaining_chars=1000,
        refresh=lambda: [row for row in memory.all() if row.entry_id == "orion"],
    )
    assert [row.entry_id for row in records] == ["orion", "lyra"]
    assert finding == "memory_pre_recall_decision:apply:success"
    assert memory.flush_access_events() == 1  # 只有新增事实确认访问。


def test_formal_recall_shares_one_stage_for_before_and_after_points(prepared, monkeypatch):
    agent, request, stage, scope, original, extra = prepared
    agent.config.memory_top_k = 3
    agent.config.home_lesson_stale_caveat_days = 7
    agent.memory_hot = None
    agent.memory_lessons = None
    routed = SimpleNamespace(receipts=[], findings=[], injected_sections=[])
    observed = []
    monkeypatch.setattr(loop_support, "hot_memory_records", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(loop_support, "routed_lesson_records", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(decision_service, "begin_decision_stage", lambda *_args, **_kwargs: observed.append("begin") or stage)
    monkeypatch.setattr(decision_recall, "rerank_recalled_memories",
                        lambda *_args, **kwargs: (observed.append(kwargs["stage"]) or [original, extra], ""))
    monkeypatch.setattr(decision_recall, "supplement_recalled_memories",
                        lambda *_args, **kwargs: (observed.append(kwargs["stage"]) or [original, extra], ""))

    records = loop_support._formal_memories_for_request(
        agent, request, routed, recall_scope=scope, long_term_memories=[original, extra],
        skip_formal_recall=False,
    )

    assert records == [original, extra]
    assert observed == ["begin", stage, stage]
