"""Memory Wave-1 S4：remember 热路径有界读取、所有压缩提交登记 pre_compact、未整理旧会话保留保护与死字段清理。"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability import memory_tool
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.learning_source import user_dialogue_projection
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_store import _jsonl_indexing, retention_apply
from agent_py_agent.agent.memory_store import jsonl as jsonl_module
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.read_index import MemoryReadIndex
from agent_py_agent.agent.memory_store.recall import MemoryRecallScope
from agent_py_agent.agent.memory_store.retention import MemoryRetentionService
from agent_py_agent.agent.memory_store.retention_scan_budget import RetentionScanBudget
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.tests.memory_confirmation_fixtures import ConfirmedRememberWorkflow

_PERSONAL = {"scope_type": "personal", "scope_key": "personal"}


# 函数用途: 建一个只用临时 home 的本地代理和一条已绑定学习来源的会话。
def _agent_and_thread(tmp_path):
    agent = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / "home"), prompt_files=[]), tmp_path / "workspace")
    thread = agent.conversation_store.threads.get_or_create({
        "canonical_user_id": "user-1", "channel": "internal", "channel_conversation_id": "chat-1",
        "channel_user_id": "user-1", "learning_source": user_dialogue_projection(),
    })
    return agent, thread


# 函数用途: 以指定 Gateway 请求身份追加一条会话行。
def _append(agent, thread_id, role, content, request_id):
    return agent.conversation_store.messages.append({
        "thread_id": thread_id, "role": role, "content": content, "metadata": {"gateway_request_id": request_id},
    })


# 函数用途: 把当前运行绑定到给定会话与 Gateway 请求。
def _bind(agent, thread_id, request_id):
    agent._current_run_params = SimpleNamespace(
        request_id=request_id, run_id="run-" + request_id, task_id="task-s4",
        task_attributes={"conversation_thread_id": thread_id},
    )


# ---------------------------------------------------------------- OL-10 remember 有界读取


def test_user_evidence_is_found_by_one_backward_page_without_reading_the_whole_transcript(tmp_path, monkeypatch):
    agent, thread = _agent_and_thread(tmp_path)
    for index in range(150):
        _append(agent, thread.thread_id, "user" if index % 2 == 0 else "assistant", f"旧消息 {index}", f"req-old-{index // 2}")
    current = _append(agent, thread.thread_id, "user", "请记住 moneywise 项目使用 UTC 保存时间", "req-now")
    for index in range(5):
        _append(agent, thread.thread_id, "assistant", f"本轮过程 {index}", "req-now")
    _bind(agent, thread.thread_id, "req-now")
    messages = agent.conversation_store.messages
    monkeypatch.setattr(messages, "recent_report", lambda *_a, **_k: pytest.fail("evidence must not read the whole transcript"))
    pages = []
    original = messages.history_page_report

    def counted(*args, **kwargs):
        pages.append(kwargs.get("before"))
        return original(*args, **kwargs)

    monkeypatch.setattr(messages, "history_page_report", counted)

    ref = memory_tool._current_user_message_ref(agent, "moneywise 项目使用 UTC 保存时间")

    assert ref is not None and ref["message_id"] == current.message_id and ref["thread_id"] == thread.thread_id
    assert ref["quote"] == "moneywise 项目使用 UTC 保存时间" and ref["content_hash"].startswith("sha256:")
    # 本轮整组加更早的行一页就装下，读到比命中行更早的别的请求即停。
    assert pages == [None]


def test_user_evidence_beyond_the_page_bound_or_duplicated_in_the_window_is_missing(tmp_path, monkeypatch):
    agent, thread = _agent_and_thread(tmp_path)
    current = _append(agent, thread.thread_id, "user", "请记住 A 方案", "req-now")
    for index in range(200):
        _append(agent, thread.thread_id, "user", f"后来的消息 {index}", f"req-later-{index}")
    _bind(agent, thread.thread_id, "req-now")

    monkeypatch.setattr(memory_tool, "_USER_EVIDENCE_MAX_PAGES_COUNT", 1)
    assert memory_tool._current_user_message_ref(agent, "A 方案") is None
    monkeypatch.setattr(memory_tool, "_USER_EVIDENCE_MAX_PAGES_COUNT", 8)
    assert memory_tool._current_user_message_ref(agent, "A 方案")["message_id"] == current.message_id

    agent_dup, other = _agent_and_thread(tmp_path / "dup")
    _append(agent_dup, other.thread_id, "user", "请记住 B 方案", "req-dup")
    _append(agent_dup, other.thread_id, "user", "请记住 B 方案", "req-dup")
    _bind(agent_dup, other.thread_id, "req-dup")
    assert memory_tool._current_user_message_ref(agent_dup, "B 方案") is None


def test_target_lookup_reads_the_one_entry_by_id_and_never_lists_the_store(tmp_path, monkeypatch):
    agent, _thread = _agent_and_thread(tmp_path)
    kept = agent.memory.add("user", "部署端口是 8080", kind="fact", attributes=dict(_PERSONAL))
    monkeypatch.setattr(agent.memory, "all", lambda: pytest.fail("target lookup must not read the whole store"))
    seen = []
    original = agent.memory.read_ids
    monkeypatch.setattr(agent.memory, "read_ids", lambda ids: seen.append(list(ids)) or original(ids))

    entry_id, target = memory_tool._resolved_target(agent.memory, {"entry_id": kept.entry_id}, action="remove", index=1)
    missing = memory_tool._resolved_target(agent.memory, {"entry_id": "missing-id"}, action="replace", index=2)

    assert entry_id == kept.entry_id and target.content == "部署端口是 8080"
    assert (missing.error_code, missing.effect_outcome) == ("MEMORY_ENTRY_NOT_FOUND", "not_started")
    assert seen == [[kept.entry_id], ["missing-id"]]


# 函数用途: 写 7 条本人事实和 1 条别的项目事实，返回本人事实的 entry_id（升序）。
def _seed_list_store(agent):
    personal = [agent.memory.add("user", f"本人事实 {index}", kind="fact", attributes=dict(_PERSONAL)).entry_id
                for index in range(7)]
    agent.memory.add("user", "别的项目事实", kind="fact",
                     attributes={"scope_type": "project", "scope_key": "project:beta"})
    return sorted(personal)


# 函数用途: 按 next_cursor 把列表翻到底，返回每页条目编号与每页回执。
def _list_all_pages(memory, scope):
    pages, payloads, cursor = [], [], ""
    for _ in range(10):
        outcome = memory_tool._memory_list_result(memory, scope, cursor=cursor)
        assert outcome.ok, outcome.output
        payload = json.loads(outcome.output)
        payloads.append(payload)
        pages.append([entry["entry_id"] for entry in payload["entries"]])
        cursor = payload.get("next_cursor", "")
        if not cursor:
            return pages, payloads
    pytest.fail("list paging did not terminate")


@pytest.mark.parametrize("layout", ["indexed_generation", "generation_index_deleted", "unmigrated"])
def test_list_pages_only_the_visible_scope_with_a_cursor_and_never_reads_the_whole_store(tmp_path, monkeypatch, layout):
    agent, _thread = _agent_and_thread(tmp_path)
    personal = _seed_list_store(agent)
    index = MemoryReadIndex(agent.memory.path)
    if layout != "unmigrated":
        agent.memory.migrate_tiers()
        agent.memory.rebuild_read_index()
        assert index.coverage()["status"] == "complete"
    if layout == "indexed_generation":
        monkeypatch.setattr(_jsonl_indexing, "bounded_authority_records",
                            lambda *_a, **_k: pytest.fail("a complete read index pages without the authority scan"))
    elif layout == "generation_index_deleted":
        index.path.unlink()
    monkeypatch.setattr(agent.memory, "all", lambda: pytest.fail("list must not read the whole store"))
    monkeypatch.setattr(memory_tool, "_MEMORY_LIST_PAGE_ENTRIES", 3)
    monkeypatch.setattr(jsonl_module, "_LIST_PAGE_RESOLVE_WINDOW_RECORDS", 2)
    scope = MemoryRecallScope(keys=(("global", "global"), ("personal", "personal")))

    pages, payloads = _list_all_pages(agent.memory, scope)

    assert pages == [personal[0:3], personal[3:6], personal[6:7]]
    assert [payload.get("next_cursor") for payload in payloads] == [personal[2], personal[5], None]
    assert "继续翻页" in payloads[0]["hint"] and "hint" not in payloads[-1]


def test_small_list_keeps_the_legacy_single_page_envelope(tmp_path):
    agent, _thread = _agent_and_thread(tmp_path)
    personal = _seed_list_store(agent)
    payload = json.loads(memory_tool._memory_list_result(agent.memory, None).output)

    assert set(payload) == {"ok", "action", "entries"}
    assert len(payload["entries"]) == len(personal) + 1


def test_list_without_index_over_the_scan_budget_fails_closed_without_a_full_read(tmp_path, monkeypatch):
    agent, _thread = _agent_and_thread(tmp_path)
    _seed_list_store(agent)
    MemoryReadIndex(agent.memory.path).path.unlink(missing_ok=True)
    monkeypatch.setattr(jsonl_module, "_AUTHORITY_SCAN_MAX_RECORDS", 2)
    monkeypatch.setattr(agent.memory, "all", lambda: pytest.fail("an incomplete bounded read must not fall back to all()"))

    outcome = memory_tool._memory_list_result(agent.memory, None)

    assert (outcome.error_code, outcome.effect_outcome) == ("MEMORY_AUTHORITY_UNREADABLE", "not_started")
    assert "读索引尚未重建完成" in json.loads(outcome.output)["error"]


def test_remember_schema_exposes_cursor_only_on_the_top_level_list_call():
    spec = memory_tool.build_remember_model_spec()
    properties = spec.input_schema["properties"]

    assert properties["cursor"]["type"] == "string" and "next_cursor" in properties["cursor"]["description"]
    assert "cursor" not in properties["operations"]["items"]["properties"]


# ---------------------------------------------------------------- OL-11 压缩提交登记 pre_compact


def test_thread_compaction_registers_pre_compact_once_after_the_commit_returns(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core import _finalization_service
    from agent_py_agent.agent.conversation.compact import (
        ConversationCompactOptions,
        prepare_conversation_context,
    )
    from agent_py_agent.agent.gateway_parts.request_history import (
        append_gateway_conversation_message,
    )
    from agent_py_agent.tests.test_gateway_conversation_compact import (
        _agent,
        _context,
        _request,
        _SummaryBackend,
    )

    agent = _agent(tmp_path, context_tokens=1_000_000)
    agent.backend = _SummaryBackend()
    context = _context(agent, _request(), "create", "开始")
    for role in ("user", "assistant"):
        assert append_gateway_conversation_message(agent, {}, context, request_id=f"old-{role}", role=role,
                                                   content="原始资料" * 6_000)
    events = []
    threads = agent.conversation_store.threads
    original_commit = threads.update_compact_state

    def commit(*args, **kwargs):
        result = original_commit(*args, **kwargs)
        events.append("commit_returned")
        return result

    original_request = _finalization_service._request_memory_curator

    def request(target, reason):
        events.append(reason)
        return original_request(target, reason)

    monkeypatch.setattr(threads, "update_compact_state", commit)
    monkeypatch.setattr(_finalization_service, "_request_memory_curator", request)

    result = prepare_conversation_context(agent, agent.conversation_store, threads.require(context.thread_id),
                                          options=ConversationCompactOptions(force=True))

    assert result.compacted and events == ["commit_returned", "pre_compact"]
    state = agent.memory_curator.state_store.load()
    assert "pre_compact" in state.pending_reasons and state.pending_reason_generations["pre_compact"] == 1


def test_active_turn_compaction_registers_pre_compact_after_its_commit(monkeypatch):
    from agent_py_agent.agent.agent_core import _finalization_service
    from agent_py_agent.agent.conversation import active_turn_compact

    events = []
    monkeypatch.setattr(active_turn_compact, "commit_live_tool_compact",
                        lambda _agent, _binding, _request: events.append("commit_returned") or "updated-thread")
    monkeypatch.setattr(_finalization_service, "_request_memory_curator", lambda _agent, reason: events.append(reason))
    plan = SimpleNamespace(binding="binding", source_call_ids=("call-1",), source_tool_refs=(), retained_tool_refs=(),
                           retained_call_ids=(), projected_tokens_before=10, policy=None, progress={})
    request = SimpleNamespace(request_id="req", attempt_id="att", compact_context=None, interrupt_check=None,
                              progress_callback=None)

    assert active_turn_compact._commit_active_turn_compact(object(), plan, request, "summary", 5) == "updated-thread"
    assert events == ["commit_returned", "pre_compact"]


@pytest.mark.parametrize(("apply_id", "expected"), [("apply-1", ["pre_compact"]), ("", [])])
def test_finalization_auto_compact_registers_pre_compact_only_for_a_real_apply(tmp_path, monkeypatch, apply_id, expected):
    from agent_py_agent.agent.agent_core import _finalization_service, finalization_compact_auto
    from agent_py_agent.tests.test_memory_runtime_compact_auto_continuation import (
        _finalize_context_for_continuation,
    )

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), prompt_files=[]), tmp_path)
    ctx = replace(_finalize_context_for_continuation(tool_rounds=0, executed_tools=[]), compact_auto_continue_depth=0)
    events = []
    cycle = {
        "suggestion": {"should_prompt": False, "status": "ok", "token_budget": {"ratio": 0.5}, "message": "",
                       "recommended_commands": []},
        "trigger": {}, "status": "applied", "next_action": "return_result", "automatic_tool_execution": "none",
        "apply_id": apply_id, "continue_packet": {}, "allowed_to_continue": False,
    }
    monkeypatch.setattr(_finalization_service, "_request_memory_curator", lambda _agent, reason: events.append(reason))
    monkeypatch.setattr(finalization_compact_auto, "run_memory_compact_auto_cycle", lambda _root, _options: cycle)

    fields = finalization_compact_auto.compact_auto_cycle_fields(agent, ctx, {"turn": 10, "active": 10})

    assert fields["memory_compact_auto_apply_id"] == apply_id and events == expected


# ---------------------------------------------------------------- OL-12 未整理旧会话保留保护

_NOW = datetime(2026, 8, 4, tzinfo=timezone.utc)
_OLD = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()


# 函数用途: 建一个会话保留 365 天的临时 owner 与留存服务；profile 为空表示后台提取关闭。
def _retention_owner(tmp_path, *, profile):
    home = ensure_my_agent_home(tmp_path / "home")
    policy = json.loads(home.owner_retention_json.read_text(encoding="utf-8"))
    policy.update(conversation_days=365, trash_days=30)
    home.owner_retention_json.write_text(json.dumps(policy, sort_keys=True), encoding="utf-8")
    config = SimpleNamespace(memory_curator_enabled=True, memory_curator_model_profile=profile)
    service = MemoryRetentionService(home_paths=home, candidates=CandidateService(home.owner_memory_candidates_jsonl),
                                     config=config)
    return home, service


# 函数用途: 写一个早已不活跃、已从路由索引解绑的旧会话，返回会话与各行。
def _old_thread(home, rows):
    store = ConversationStore(Path(home.owner_home_dir) / "conversations")
    thread = store.threads.get_or_create({
        "canonical_user_id": "retention-user", "channel": "internal", "channel_conversation_id": "retention-chat",
        "channel_user_id": "retention-user", "now": _OLD,
    })
    written = [store.messages.append({"thread_id": thread.thread_id, "role": role, "content": content,
                                      "now": _OLD + number, "metadata": dict(metadata)})
               for number, (role, content, metadata) in enumerate(rows)]
    for name in ("channel_bindings.json", "user_latest_threads.json"):
        index = store.storage.root / name
        payload = json.loads(index.read_text(encoding="utf-8"))
        index.write_text(json.dumps({key: value for key, value in payload.items() if value != thread.thread_id}),
                         encoding="utf-8")
    return thread, written


# 函数用途: 写后台整理状态里的逐会话游标；raw 给定时原样写入（模拟损坏）。
def _write_curator_state(home, cursors=None, *, raw=None):
    path = Path(home.owner_memory_curator_state_json)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw if raw is not None else json.dumps({"per_thread_cursors": cursors or {}}), encoding="utf-8")


# 函数用途: 按真实预算把只读计划续扫完，累计动作与每片的未整理会话数。
def _plan_all(service):
    cursor, actions, uncurated = None, {}, 0
    for _ in range(256):
        report = service.plan(now=_NOW, scan_budget=RetentionScanBudget.default(), scan_cursor=cursor)
        actions.update((action.action_id, action) for action in report.actions)
        uncurated += report.uncurated_threads
        if not report.scan_partial or report.errors or report.legal_hold:
            return report, [action for action in actions.values() if action.category == "conversation"], uncurated
        cursor = report.scan_cursor
    pytest.fail("retention plan did not converge")


_ROWS = (("user", "旧会话里提过的偏好", {}), ("assistant", "好的", {}))


def test_extraction_on_protects_an_old_thread_with_messages_after_the_curator_cursor(tmp_path):
    home, service = _retention_owner(tmp_path, profile="aux-profile")
    thread, rows = _old_thread(home, _ROWS)
    _write_curator_state(home, {thread.thread_id: rows[0].message_id})

    report, conversations, uncurated = _plan_all(service)

    assert report.ok and conversations == [] and uncurated == 1


def test_extraction_on_trashes_a_fully_curated_thread_and_binds_the_cursor(tmp_path):
    home, service = _retention_owner(tmp_path, profile="aux-profile")
    thread, rows = _old_thread(home, (*_ROWS, ("user", "宿主标记不学的行", {"learning_excluded": True})))
    _write_curator_state(home, {thread.thread_id: rows[1].message_id})

    report, conversations, uncurated = _plan_all(service)

    assert report.ok and uncurated == 0
    assert [(action.record_id, action.curator_cursor) for action in conversations] == [(thread.thread_id, rows[1].message_id)]


@pytest.mark.parametrize("raw", [json.dumps({"per_thread_cursors": {"thread-other": "message-other"}}),
                                 json.dumps({"per_thread_cursors": {"thread-other": 5}})])
def test_extraction_on_protects_threads_whose_curation_cannot_be_confirmed(tmp_path, raw):
    home, service = _retention_owner(tmp_path, profile="aux-profile")
    _old_thread(home, _ROWS)
    _write_curator_state(home, raw=raw)

    report, conversations, uncurated = _plan_all(service)

    assert report.ok and conversations == [] and uncurated == 1


def test_unparseable_curator_state_already_fails_closed_through_the_reference_pins(tmp_path):
    home, service = _retention_owner(tmp_path, profile="aux-profile")
    _old_thread(home, _ROWS)
    _write_curator_state(home, raw="{not json")

    report, conversations, _uncurated = _plan_all(service)

    assert conversations == [] and [error.error_code for error in report.errors] == ["MEMORY_RETENTION_PIN_SOURCE_UNREADABLE"]


def test_extraction_off_still_trashes_and_reports_the_uncurated_count(tmp_path):
    home, service = _retention_owner(tmp_path, profile="")
    thread, _rows = _old_thread(home, _ROWS)

    report, conversations, uncurated = _plan_all(service)

    assert [(action.record_id, action.curator_cursor) for action in conversations] == [(thread.thread_id, None)]
    assert uncurated == 1
    assert report.to_dict().get("uncurated_threads", 0) == report.uncurated_threads
    assert "uncurated_threads" not in replace(report, uncurated_threads=0).to_dict()


def test_apply_skips_the_trash_when_the_curator_cursor_changed_after_planning(tmp_path):
    home, service = _retention_owner(tmp_path, profile="aux-profile")
    thread, rows = _old_thread(home, _ROWS)
    _write_curator_state(home, {thread.thread_id: rows[1].message_id})
    _report, [action], _uncurated = _plan_all(service)

    _write_curator_state(home, {thread.thread_id: rows[0].message_id})
    changed = retention_apply._trash_conversation(home=home, action=action, now=_NOW)
    _write_curator_state(home, {thread.thread_id: rows[1].message_id})
    trashed = retention_apply._trash_conversation(home=home, action=action, now=_NOW)

    assert (changed.status, trashed.status) == ("state_changed", "trashed")


# ---------------------------------------------------------------- OL-14 死字段


def test_promoted_fact_keeps_english_keywords_without_the_unused_chinese_field(tmp_path):
    from agent_py_agent.tests.test_memory_tool import _agent_with_current_user

    agent = _agent_with_current_user(tmp_path)
    result = ConfirmedRememberWorkflow(agent).execute({
        "content": "moneywise 项目使用 UTC 保存时间", "kind": "project", "origin": "user_explicit",
        "subject_key": "project.moneywise.timezone",
    })

    assert json.loads(result.output)["active_memory_changed"] is True
    [record] = agent.memory.all()
    assert "moneywise" in record.attributes["keywords_en"] and "keywords_cn" not in record.attributes
