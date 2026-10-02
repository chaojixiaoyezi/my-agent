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
from agent_py_agent.tests.test_decision_reach_counts import reach_counter

# 夹具把片段材料设置换成固定值；读取失败用例要用真实读取入口。
_REAL_FRAGMENT_MATERIAL = decision_recall._fragment_material


@pytest.fixture
def prepared(monkeypatch):
    # 片段材料默认只给文字（与仓库默认一致）；真实设置读取见下面的 _fragment_material 用例。
    monkeypatch.setattr(decision_recall, "_fragment_material", lambda *_args: "query_text")
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



def test_pre_recall_reach_reasons_are_counted(prepared, monkeypatch, tmp_path):
    agent, request, stage, scope, original, _extra = prepared
    reasons = reach_counter(agent, monkeypatch, "pre_recall", tmp_path)

    def supplement(**changes):
        kwargs = {"recall_scope": scope, "stage": stage, "queries": (("query_1", "Lyra deployment checklist"),),
                  "slots": 1, "search_top_k": 5, "remaining_chars": 100, "refresh": lambda: [original], **changes}
        return decision_recall.supplement_recalled_memories(agent, request, [original], **kwargs)

    supplement(queries=())
    supplement(slots=0)
    supplement(remaining_chars=0)
    closed = SimpleNamespace(error_code="admin_disabled", enabled_points=(), deadline=stage.deadline)
    assert supplement(stage=closed)[1] == "memory_pre_recall_decision:unavailable"
    supplement(stage=SimpleNamespace(error_code="", enabled_points=(), deadline=stage.deadline))
    _run(prepared, monkeypatch)

    def broken(_value):
        raise decision_recall.DecisionInputError("材料超出上限")

    monkeypatch.setattr(decision_recall, "_digest", broken)
    assert supplement()[1] == "memory_pre_recall_decision:enhancement_failed"
    assert reasons() == ({"no_query_fragments": 1, "no_free_slots": 1, "no_room": 1, "admin_disabled": 1,
                          "point_off": 1, "bad_material": 1}, 1)

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


def test_formal_recall_records_only_the_entries_the_supplement_added(prepared, monkeypatch):
    agent, request, stage, scope, original, extra = prepared
    agent.config.memory_top_k = 3
    agent.config.home_lesson_stale_caveat_days = 7
    agent.memory_hot = None
    agent.memory_lessons = None
    routed = SimpleNamespace(receipts=[], findings=[], injected_sections=[], supplement_entry_ids=[])
    monkeypatch.setattr(loop_support, "hot_memory_records", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(loop_support, "routed_lesson_records", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(decision_service, "begin_decision_stage", lambda *_args, **_kwargs: stage)
    monkeypatch.setattr(decision_recall, "rerank_recalled_memories", lambda *_args, **_kwargs: ([original], ""))
    monkeypatch.setattr(decision_recall, "supplement_recalled_memories",
                        lambda *_args, **_kwargs: ([original, extra], "memory_pre_recall_decision:apply:success"))
    records = loop_support._formal_memories_for_request(
        agent, request, routed, recall_scope=scope, long_term_memories=[original], skip_formal_recall=False,
    )
    assert records == [original, extra]
    assert routed.supplement_entry_ids == ["extra"], "只记补充查询真正追加的记录"
    refs = loop_support._recalled_refs(records, routed.supplement_entry_ids)
    assert [(ref["entry_id"], ref["via"]) for ref in refs] == [("original", "baseline"), ("extra", "supplement")]
    assert all(set(ref) == {"entry_id", "version", "kind", "via"} for ref in refs), "来源清单不含正文"


def test_context_bundle_writes_recall_evidence_without_changing_the_prompt_section(tmp_path):
    from agent_py_agent.agent.user_space.context_bundle import (
        MainContextBundleRequest,
        build_main_context_bundle,
    )

    base = MainContextBundleRequest(root=tmp_path, home_paths=None, user_prompt="核对", save=False, memory_count=2,
                                    created_at="2026-09-24T00:00:00Z")
    plain = build_main_context_bundle(base)
    from dataclasses import replace

    evidence = build_main_context_bundle(replace(base, recalled_refs=(
        {"entry_id": "original", "version": 1, "kind": "fact", "via": "baseline"},
        {"entry_id": "extra", "version": 1, "kind": "fact", "via": "supplement"},
    ), recall_findings=("memory_pre_recall_decision:apply:success",)))
    refs = evidence.bundle["memory_refs"]
    assert [ref["via"] for ref in refs["recalled_refs"]] == ["baseline", "supplement"]
    assert refs["recall_findings"] == ["memory_pre_recall_decision:apply:success"]
    assert plain.bundle["memory_refs"]["recalled_refs"] == [] and plain.bundle["memory_refs"]["recall_findings"] == []
    assert evidence.prompt_section == plain.prompt_section, "来源清单只写文件，模型可见的提示段字节不变"


# ---- J8：片段材料 with_new_facts（先预检每个片段能新增的正式事实）----

QUERIES = (("query_1", "Orion release details"), ("query_2", "Lyra deployment checklist"))


# 函数用途: 把片段材料设成预检，并让候选检索按查询文本返回不同结果（Orion 片段只命中基线，Lyra 片段多一条新事实）。
def _previewing(prepared, monkeypatch, *, search=None):
    agent, _request, _stage, _scope, original, extra = prepared
    monkeypatch.setattr(decision_recall, "_fragment_material", lambda *_args: "with_new_facts")

    def by_query(query, top_k, predicate):
        agent.memory.calls.append((query, top_k))
        rows = (original,) if query.startswith("Orion") else (original, extra)
        return [row for row in rows if predicate(row)]

    monkeypatch.setattr(agent.memory, "search_scoped_candidates", search or by_query)
    agent.memory.calls.clear()
    agent.memory.touches.clear()


# 函数用途: 记下送给决策模型的 state/questions，并按给定选择返回一次成功结果。
def _deciding(monkeypatch, *, choice="query_2", mode="apply"):
    sent = []
    response = SimpleNamespace(answers=(SimpleNamespace(question_id="supplemental_query", value=choice, error_code=""),))
    outcome = SimpleNamespace(mode=mode, status="success", may_apply=mode == "apply", response=response)
    monkeypatch.setattr(decision_recall, "decide", lambda *_args, **kwargs: sent.append(kwargs) or outcome)
    monkeypatch.setattr(decision_recall, "decision_outcome_is_current", lambda *_args: True)
    return sent


# 函数用途: 用两个片段跑一次补充查询（空余 1 个名额）。
def _supplement(prepared, queries=QUERIES):
    agent, request, stage, scope, original, _extra = prepared
    return decision_recall.supplement_recalled_memories(
        agent, request, [original], recall_scope=scope, stage=stage, queries=queries, slots=1, search_top_k=5,
        remaining_chars=100, refresh=lambda: [original])


def test_query_text_default_keeps_the_original_request_material(prepared, monkeypatch):
    sent = _deciding(monkeypatch, choice="query_2")
    records, finding = _supplement(prepared)
    assert "fragment_additions" not in sent[0]["state"] and "fragment_additions_note" not in sent[0]["state"]
    assert list(sent[0]["questions"]["supplemental_query"]["criteria"])[:2] == ["query_1", "query_2"]
    assert prepared[0].memory.calls == [("Lyra deployment checklist", 5)], "默认只在采用时检索一次所选片段"
    assert (records, finding) == ([prepared[4], prepared[5]], "memory_pre_recall_decision:apply:success")


def test_with_new_facts_offers_only_fragments_that_add_and_applies_what_jev_saw(prepared, monkeypatch):
    _previewing(prepared, monkeypatch)
    sent = _deciding(monkeypatch, choice="query_2")
    records, finding = _supplement(prepared)
    state, criteria = sent[0]["state"], sent[0]["questions"]["supplemental_query"]["criteria"]
    assert state["fragment_additions"] == {"query_2": {"new_count": 1, "new_facts": [
        {"entry_id": "extra", "summary": "Lyra deployment checklist"}]}}
    assert "query_1" not in criteria and criteria["query_2"] == "Lyra deployment checklist", "主题已被原召回覆盖的片段不给选"
    assert {"not_needed", "no_match", "abstain", "need_data"} <= set(criteria)
    assert [query for query, _ in prepared[0].memory.calls] == ["Orion release details", "Lyra deployment checklist"]
    assert (records, finding) == ([prepared[4], prepared[5]], "memory_pre_recall_decision:apply:success")
    assert prepared[0].memory.touches == ["extra"], "预检不记访问；只有最终注入的经正式源确认"


def test_with_new_facts_skips_the_call_when_no_fragment_adds(prepared, monkeypatch, tmp_path):
    agent, _request, _stage, _scope, original, _extra = prepared
    reasons = reach_counter(agent, monkeypatch, "pre_recall", tmp_path)
    _previewing(prepared, monkeypatch, search=lambda query, top_k, predicate: [original])
    monkeypatch.setattr(decision_recall, "decide", lambda *_args, **_kwargs: pytest.fail("补不出新事实时不应调用决策模型"))
    assert _supplement(prepared) == ([original], "")
    assert agent.memory.touches == []
    assert reasons() == ({"no_new_facts": 1}, 0)


def test_with_new_facts_preview_failure_is_counted_and_keeps_the_original(prepared, monkeypatch, tmp_path):
    agent, _request, _stage, _scope, original, _extra = prepared
    reasons = reach_counter(agent, monkeypatch, "pre_recall", tmp_path)

    def broken(_query, _top_k, _predicate):
        raise OSError("派生检索不可用")

    _previewing(prepared, monkeypatch, search=broken)
    monkeypatch.setattr(decision_recall, "decide", lambda *_args, **_kwargs: pytest.fail("预检出错时不应调用决策模型"))
    assert _supplement(prepared) == ([original], "memory_pre_recall_decision:enhancement_failed")
    assert reasons() == ({"preview_failed": 1}, 0)


def test_with_new_facts_observe_previews_but_never_touches(prepared, monkeypatch):
    _previewing(prepared, monkeypatch)
    sent = _deciding(monkeypatch, choice="query_2", mode="observe")
    assert _supplement(prepared) == ([prepared[4]], "memory_pre_recall_decision:observe:success")
    assert sent[0]["state"]["fragment_additions"]["query_2"]["new_count"] == 1
    assert len(prepared[0].memory.calls) == 2 and prepared[0].memory.touches == []


def test_with_new_facts_preview_cancel_propagates(prepared, monkeypatch):
    _previewing(prepared, monkeypatch)
    checks = iter((None, InterruptedError("停止")))

    def interrupt():
        result = next(checks)
        if result:
            raise result

    monkeypatch.setattr(decision_recall, "_check_interrupted", interrupt)
    monkeypatch.setattr(decision_recall, "decide", lambda *_args, **_kwargs: pytest.fail("取消后不应调用决策模型"))
    with pytest.raises(InterruptedError, match="停止"):
        _supplement(prepared)
    assert len(prepared[0].memory.calls) == 1, "第二个片段预检前就停下"


def test_with_new_facts_on_a_lexical_store_asks_nothing_because_fragments_cannot_add(prepared, monkeypatch, tmp_path):
    """词面检索下片段词一定在整句里，原召回已拿到全部正分事实；预检后直接不问，也不留访问。"""
    agent, request, stage, scope, _original, _extra = prepared
    monkeypatch.setattr(decision_recall, "_fragment_material", lambda *_args: "with_new_facts")
    memory = JsonlMemory(tmp_path / "memory.jsonl", ops_path=tmp_path / "ops.jsonl")
    for entry_id, content in (("orion", "Orion release details for Friday"),
                              ("lyra", "Lyra deployment checklist before rollout")):
        memory.add_record(MemoryRecord("user", content, entry_id=entry_id, kind="fact",
                                       attributes={"scope_type": "personal", "scope_key": "personal"}))
    agent.memory = memory

    def predicate(row):
        return long_term_record_matches_scope(row, scope)

    base = memory.search_scoped(request.user_prompt, 3, predicate)
    assert memory.flush_access_events() == 2
    monkeypatch.setattr(decision_recall, "decide", lambda *_args, **_kwargs: pytest.fail("不应调用决策模型"))
    ids = {row.entry_id for row in base}
    records, finding = decision_recall.supplement_recalled_memories(
        agent, request, base, recall_scope=scope, stage=stage,
        queries=decision_recall.supplemental_query_candidates(request.user_prompt),
        slots=1, search_top_k=3, remaining_chars=1000,
        refresh=lambda: [row for row in memory.all() if row.entry_id in ids])
    assert (records, finding) == (base, "")
    assert memory.flush_access_events() == 0


def test_with_new_facts_on_a_real_store_offers_the_missing_fact_only(prepared, monkeypatch, tmp_path):
    agent, _request, stage, scope, _original, _extra = prepared
    monkeypatch.setattr(decision_recall, "_fragment_material", lambda *_args: "with_new_facts")
    memory = JsonlMemory(tmp_path / "memory.jsonl", ops_path=tmp_path / "ops.jsonl")
    for entry_id, content in (("orion", "Orion release details and Orion timetable"),
                              ("lyra", "Lyra deployment checklist before rollout")):
        memory.add_record(MemoryRecord("user", content, entry_id=entry_id, kind="fact",
                                       attributes={"scope_type": "personal", "scope_key": "personal"}))
    agent.memory = memory
    request = RuntimeContextRequest(
        "请核对 Orion release details 与 Orion timetable。再找 Lyra deployment checklist。", None, False,
        request_id="request", run_id="run", task_id="task", task_attributes={"agent_thread_id": "thread"})

    def predicate(row):
        return long_term_record_matches_scope(row, scope)

    base = memory.search_scoped(request.user_prompt, 1, predicate)
    assert [row.entry_id for row in base] == ["orion"] and memory.flush_access_events() == 1
    sent = _deciding(monkeypatch, choice="query_2")
    records, finding = decision_recall.supplement_recalled_memories(
        agent, request, base, recall_scope=scope, stage=stage,
        queries=decision_recall.supplemental_query_candidates(request.user_prompt),
        slots=1, search_top_k=1, remaining_chars=1000,
        refresh=lambda: [row for row in memory.all() if row.entry_id == "orion"])
    criteria = sent[0]["questions"]["supplemental_query"]["criteria"]
    assert "query_1" not in criteria and "query_2" in criteria
    assert sent[0]["state"]["fragment_additions"]["query_2"]["new_facts"][0]["entry_id"] == "lyra"
    assert ([row.entry_id for row in records], finding) == (["orion", "lyra"], "memory_pre_recall_decision:apply:success")
    assert memory.flush_access_events() == 1, "预检两次检索都不记访问，只有新增事实确认一次"


def test_fragment_material_is_registered_off_by_default_and_read_through_the_settings_service(tmp_path):
    from agent_py_agent.agent.settings.config import AgentConfig, load_config
    from agent_py_agent.agent.settings.decision_settings_defaults import decision_config_fields
    from agent_py_agent.agent.settings.decision_settings_schema import validate_decision_field
    from agent_py_agent.agent.settings.model_provider_schema import ModelProfileError
    from agent_py_agent.agent.settings.user_config_capability import packaged_config_path
    from agent_py_agent.tests.test_decision_settings import host_at, patch

    path = "points.pre_recall.fragment_material"
    assert decision_config_fields()[path] == ("memory", "memory_decision_pre_recall_fragment_material")
    assert AgentConfig().memory_decision_pre_recall_fragment_material == "query_text"
    assert load_config(packaged_config_path()).memory_decision_pre_recall_fragment_material == "query_text"
    for bad in ("always", "", True, None):
        with pytest.raises(ModelProfileError):
            validate_decision_field(path, bad)
    host = host_at(tmp_path)
    stage = SimpleNamespace(thread_id="")
    assert decision_recall._fragment_material(host, stage) == "query_text"
    patch(host, {path: "with_new_facts"})
    assert decision_recall._fragment_material(host, stage) == "with_new_facts"
    (tmp_path / "agent.yaml").write_text("memory_decision_pre_recall_fragment_material: maybe\n", encoding="utf-8")
    with pytest.raises(ModelProfileError):
        load_config(tmp_path / "agent.yaml")


# 函数用途: 让片段材料设置走真实读取入口，并返回“读取时抛给定错误再跑一次补充查询”的函数。
@pytest.fixture
def unreadable_setting(prepared, monkeypatch, tmp_path):
    from agent_py_agent.agent.settings import decision_settings

    agent, _request, stage, _scope, original, _extra = prepared
    reasons = reach_counter(agent, monkeypatch, "pre_recall", tmp_path)
    monkeypatch.setattr(decision_recall, "_fragment_material", _REAL_FRAGMENT_MATERIAL)
    monkeypatch.setattr(decision_recall, "decide", lambda *_args, **_kwargs: pytest.fail("设置读不出时不应调用决策模型"))
    stage.thread_id = "thread"

    def run(error):
        def unreadable(*_args, **_kwargs):
            raise error

        monkeypatch.setattr(decision_settings, "execute_decision_settings_operation", unreadable)
        records, finding = _supplement(prepared)
        return records == [original], finding, reasons()

    return run


@pytest.mark.parametrize("error,reason", [(BlockingIOError("设置正在保存"), "settings_busy"),
                                          (OSError("读不出"), "configuration_unavailable")])
def test_unreadable_fragment_setting_is_counted_and_keeps_the_original(unreadable_setting, error, reason):
    assert unreadable_setting(error) == (True, "memory_pre_recall_decision:enhancement_failed", ({reason: 1}, 0))
