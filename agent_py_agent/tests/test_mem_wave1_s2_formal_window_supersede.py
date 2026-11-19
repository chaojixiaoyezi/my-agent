from __future__ import annotations

"""记忆第一波 S2：后台整理按相关性读正式窗口、冲突目标置顶、conservative_v2 同主题自动替换。"""

import hashlib
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import learning_source as ls
from agent_py_agent.agent.memory_store import jsonl as jsonl_module
from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
from agent_py_agent.agent.memory_store.candidates import CandidateIdentityConflictError
from agent_py_agent.agent.memory_store.curator import MemoryCuratorService, _formal_query
from agent_py_agent.agent.memory_store.curator_formal import (
    FORMAL_QUERY_MAX_CHARS,
    FormalMemorySource,
    FormalMemoryWindow,
)
from agent_py_agent.agent.memory_store.curator_models import (
    CONFLICT_PIN_MAX_ENTRIES,
    MemoryCuratorConfig,
    MemoryCuratorState,
    merge_conflict_pins,
    validate_curator_state,
)
from agent_py_agent.agent.memory_store.curator_state import (
    CuratorLeaseLostError,
    MemoryCuratorStateStore,
)
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.agent.memory_store.lessons import HotRuleRepository, LessonRepository
from agent_py_agent.agent.memory_store.operations import memory_content_hash
from agent_py_agent.agent.memory_store.promotion_supersede import (
    AUTO_SUPERSEDED_REASON,
    automatic_promotion_policy,
)
from agent_py_agent.agent.settings.memory import normalize_memory_settings
from agent_py_agent.tests.test_memory_curator_v2 import (
    _conversation,
    _service,
    _StaticStructuredBackend,
    _valid_output,
)
from agent_py_agent.tests.test_memory_promotion_v2 import _runtime

_NEW_FACT = "个人电脑现在使用 Linux。"


# 函数用途: 返回一个一旦被调用就让测试失败的替身，证明某条整库读取路径没有被走到。
def _forbidden(name: str):
    def call(*_args, **_kwargs):
        raise AssertionError(f"{name} must not be called")

    return call


# 函数用途: 建立隔离的正式长期记忆仓库。
def _memory(tmp_path) -> JsonlMemory:
    return JsonlMemory(tmp_path / "memory" / "long_term" / "memory.jsonl", ops_path=tmp_path / "memory" / "ops.jsonl")


# 函数用途: 写入一条带结构化主题与范围的正式事实。
def _fact(memory, content: str, subject_key: str, *, origin: str = "user_explicit"):
    return memory.add("user", content, kind="fact", attributes={
        "subject_key": subject_key, "scope_type": "personal", "scope_key": "personal", "origin": origin})


# 函数用途: 用真实 lesson/HOT 仓库组装后台整理的正式窗口来源。
def _source(tmp_path, memory) -> FormalMemorySource:
    lessons = LessonRepository(tmp_path / "memory" / "lessons", tmp_path / "memory" / "routing" / "INDEX.md")
    return FormalMemorySource(long_term=memory, lessons=lessons, hot=HotRuleRepository(tmp_path / "memory" / "memory-hot.md"))


# 函数用途: 生成只推进游标、不产出事件和候选的合法提炼结果，让晋升只来自积压候选。
def _empty_extraction(thread_id: str, message_id: str) -> dict[str, object]:
    payload = _valid_output(thread_id, message_id, "")
    payload.update(candidates=[], daily_events=[])
    return payload


# LLM: 与 promotion 夹具同一套宿主核验来源（消息 hash/quote/线程来源），只多一个可控的消息时间，
#   用来构造“用户后来又明确说了一次”的证据；核验结果仍由宿主核验器给出。
# 函数用途: 写一条指定时间的用户消息并据此观察一条 user_explicit 长期事实候选。
def _explicit_at(service, conversations, thread, *, content: str, now: float,
                 scope_type: str = "personal", scope_key: str = "personal"):
    message = conversations.messages.append(
        {"thread_id": thread.thread_id, "role": "user", "content": content, "now": now})
    ref = {"message_id": message.message_id, "thread_id": thread.thread_id, "role": "user",
           "content_hash": "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest(), "quote": content}
    return service.candidates.observe(CandidateObservation(
        candidate_type="long_term_fact", content=content, subject_key="device.os",
        scope=MemoryScope(scope_type, scope_key), origin="user_explicit", source_message_refs=(ref,),
        learning_source=ls.merge_message_sources([ls.facts_for_message(
            message.metadata, owner="user-1", thread_id=thread.thread_id, thread_source=thread.learning_source)]),
        proposed_action="add", promotion_target="long_term", promotion_mode="auto_eligible", confidence=0.99))


# 函数用途: 直接写入一条同主题旧条目，来源、范围和有效期都可控。
def _seed_prior(service, *, origin: str = "user_explicit", scope_type: str = "personal",
                scope_key: str = "personal", expires_at: float = 0.0):
    (record,) = service.long_term.apply_batch([{
        "action": "add", "entry_id": "memory-prior", "role": "user", "content": "个人电脑使用 macOS。",
        "kind": "fact", "tags": ["long_term_fact", origin], "source": "fixture", "expires_at": expires_at,
        "attributes": {"subject_key": "device.os", "scope_type": scope_type, "scope_key": scope_key, "origin": origin},
    }])
    return record


# ---------- OL-2：正式窗口按相关性有界读取 ----------


def test_formal_window_selects_relevant_long_term_without_full_scan(tmp_path, monkeypatch):
    memory = _memory(tmp_path)
    device = _fact(memory, "个人电脑使用 macOS 系统。", "device.os")
    drink = _fact(memory, "下午习惯喝绿茶。", "drink.afternoon")
    source = _source(tmp_path, memory)
    monkeypatch.setattr(memory, "all", _forbidden("long_term.all"))
    before = memory.path.read_bytes()

    window = source.read(query="我的电脑装的是 macOS", max_items=32, max_chars=6_000)

    ids = [item.authority_id for item in window]
    assert device.entry_id in ids and drink.entry_id not in ids
    assert (window.errors, window.missing_pins) == ((), ())
    # 后台整理的检索是只读的：不追加访问记录。
    assert memory.path.read_bytes() == before


def test_formal_window_empty_query_uses_readonly_lexical_subset(tmp_path, monkeypatch):
    memory = _memory(tmp_path)
    for index in range(5):
        _fact(memory, f"项目约定第 {index} 条。", f"project.rule.{index}")
    source = _source(tmp_path, memory)
    monkeypatch.setattr(memory, "all", _forbidden("long_term.all"))
    monkeypatch.setattr(memory, "search_scoped_candidates_report", _forbidden("search_scoped_candidates_report"))
    before = memory.path.read_bytes()

    window = source.read(query="   ", max_items=3)

    assert len(window) == 3 and {item.authority_type for item in window} == {"long_term"}
    assert source.read(query="", max_items=3) == window
    assert memory.path.read_bytes() == before


def test_formal_window_pins_point_read_first_and_report_confirmed_missing(tmp_path):
    memory = _memory(tmp_path)
    pinned = _fact(memory, "公司服务器统一使用 HTTPS 部署。", "ops.deploy")
    tea = _fact(memory, "下午习惯喝绿茶。", "drink.afternoon")
    source = _source(tmp_path, memory)

    window = source.read(query="绿茶", pinned_ids=(pinned.entry_id, pinned.entry_id, "memory-gone"))
    ids = [item.authority_id for item in window]
    assert ids.count(pinned.entry_id) == 1 and tea.entry_id in ids
    assert window.missing_pins == ("memory-gone",)
    # 置顶优先占预算：只放得下一条时留下的是置顶目标。
    tight = source.read(query="绿茶", max_items=1, pinned_ids=(pinned.entry_id,))
    assert [item.authority_id for item in tight] == [pinned.entry_id]


def test_formal_window_incomplete_authority_keeps_pins(tmp_path, monkeypatch):
    memory = _memory(tmp_path)
    pinned = _fact(memory, "公司服务器统一使用 HTTPS 部署。", "ops.deploy")

    def incomplete(_ids):
        raise ValueError("MEMORY_AUTHORITY_INCOMPLETE")

    monkeypatch.setattr(memory, "read_ids", incomplete)
    window = _source(tmp_path, memory).read(query="绿茶", pinned_ids=(pinned.entry_id,))

    # 覆盖不全时不能判定目标已消失：missing_pins 为空，置顶留到下一轮。
    assert window.missing_pins == ()
    assert {"code": "MEMORY_AUTHORITY_INCOMPLETE", "detail": "pinned_ids=1"} in window.errors


@pytest.mark.parametrize("query", ["", "个人电脑"])
def test_formal_window_reports_partial_authority_coverage(tmp_path, monkeypatch, query):
    memory = JsonlMemory(tmp_path / "memory" / "long_term" / "memory.jsonl")
    for index in range(3):
        _fact(memory, f"个人电脑使用 Linux {index}。", f"device.os.{index}")
    monkeypatch.setattr(jsonl_module, "_AUTHORITY_SCAN_MAX_RECORDS", 2)

    window = _source(tmp_path, memory).read(query=query)

    assert [row["code"] for row in window.errors] == ["FORMAL_MEMORY_COVERAGE_PARTIAL"]
    assert window.errors[0]["detail"].startswith("scanned_records=")


def test_formal_window_lessons_follow_routing_and_hot_reads_exact_refs(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_consolidated_c4_g1 import _hot_candidate, _lesson_candidate

    source = _source(tmp_path, _memory(tmp_path))
    backup = source.lessons.promote(_lesson_candidate(
        "c-backup", subject_key="deploy.backup", content="部署数据库之前先做全量备份。"))
    weekly = source.lessons.promote(_lesson_candidate(
        "c-weekly", subject_key="report.weekly", content="周报用表格列出本周进展。"))
    rule = source.hot.promote(_hot_candidate(
        "h-weekly", target_lesson_id=weekly.lesson_id, subject_key="report.weekly", rule="周报先列风险。"), lesson=weekly)
    monkeypatch.setattr(source.lessons, "list", _forbidden("lessons.list"))

    window = source.read(query="部署 数据库 备份")

    by_type: dict[str, list[str]] = {}
    for item in window:
        by_type.setdefault(item.authority_type, []).append(item.authority_id)
    assert by_type.get("lesson") == [backup.lesson_id]
    assert by_type.get("hot") == [rule.hot_id]
    assert window.errors == ()
    # 没有用户原话时不做相关性挑选，按路由顺序给出有界的 lesson 子集（与旧整库读取同样可见）。
    unranked = source.read(query="")
    assert {item.authority_id for item in unranked if item.authority_type == "lesson"} == {
        backup.lesson_id, weekly.lesson_id}


def test_formal_window_unreadable_lesson_routing_is_reported(tmp_path):
    from agent_py_agent.tests.test_consolidated_c4_g1 import _lesson_candidate

    source = _source(tmp_path, _memory(tmp_path))
    source.lessons.promote(_lesson_candidate("c-backup", subject_key="deploy.backup", content="部署数据库之前先做全量备份。"))
    source.lessons._heat_path().write_text("{broken", encoding="utf-8")

    window = source.read(query="部署 数据库 备份")

    assert {"code": "FORMAL_LESSON_ROUTING_UNREADABLE", "detail": "corrupt"} in window.errors
    assert [item for item in window if item.authority_type == "lesson"] == []


def test_curator_formal_query_uses_only_bounded_user_previews():
    messages = [
        SimpleNamespace(role="assistant", content_preview="助手回复不进查询"),
        SimpleNamespace(role="user", content_preview="  我的电脑\n用 macOS  "),
        SimpleNamespace(role="tool", content_preview="工具输出不进查询"),
        SimpleNamespace(role="user", content_preview="长" * 2_000),
    ]

    query = _formal_query(messages)

    assert query.startswith("我的电脑 用 macOS 长")
    assert "助手" not in query and "工具" not in query
    assert len(query) == FORMAL_QUERY_MAX_CHARS
    assert _formal_query([]) == ""


# 类用途: 记录 Curator 对正式窗口的每次读取参数，返回空窗口。
class _RecordingFormalSource:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def read(self, **kwargs):
        self.calls.append(kwargs)
        return FormalMemoryWindow()


def test_curator_reads_the_formal_window_once_with_the_user_query(tmp_path):
    store, thread, message = _conversation(tmp_path)
    backend = _StaticStructuredBackend(_valid_output(thread.thread_id, message.message_id, message.content))
    source = _RecordingFormalSource()
    service = _service(tmp_path, backend, store, formal_memory_source=source)
    service.request("task_complete")

    assert service.run_if_due().status == "succeeded"

    assert len(source.calls) == 1
    call = source.calls[0]
    assert "macOS" in call["query"] and call["pinned_ids"] == ()
    assert call["max_items"] == 32
    assert call["max_chars"] == min(6_000, max(1_000, service.config.max_input_chars // 4))


# ---------- OL-3a：冲突目标置顶 ----------


def test_conflict_pins_are_a_bounded_sanitized_lru():
    ids = [f"memory-{index}" for index in range(CONFLICT_PIN_MAX_ENTRIES + 4)]
    assert merge_conflict_pins([], add=ids) == ids[-CONFLICT_PIN_MAX_ENTRIES:]
    assert merge_conflict_pins(["a", "b", "c"], add=["a"], remove=["b"]) == ["c", "a"]
    assert merge_conflict_pins(["a"], add=["", " a ", 7, "x" * 201, None]) == ["a"]
    assert merge_conflict_pins("not-a-list", add="a") == []


def test_curator_state_cleans_and_validates_conflict_pins():
    state = MemoryCuratorState.from_dict({"conflict_pin_ids": ["a", "a", " b", 5, "c"]})
    assert state.conflict_pin_ids == ["a", "c"]
    assert MemoryCuratorState.from_dict({"conflict_pin_ids": "corrupt"}).conflict_pin_ids == []
    assert MemoryCuratorState.from_dict(state.to_dict()) == state
    for bad in (["a", "a"], [" a"], [""], ["x" * 201], [f"m-{index}" for index in range(CONFLICT_PIN_MAX_ENTRIES + 1)]):
        with pytest.raises(ValueError, match="conflict_pin_ids"):
            validate_curator_state(replace(state, conflict_pin_ids=bad))


def test_conflict_pin_update_requires_the_active_lease(tmp_path):
    store = MemoryCuratorStateStore(tmp_path / "memory" / "curator" / "state.json")
    acquired = store.acquire(reason="admin", config_revision="rev", lease_seconds=60)
    assert acquired is not None
    lease_id = acquired[1]["lease_id"]

    with pytest.raises(CuratorLeaseLostError):
        store.update_conflict_pins("memory-curator-lease-other", add=["memory-a"])
    assert store.load().conflict_pin_ids == []

    assert store.update_conflict_pins(lease_id, add=["memory-a", "memory-b"]) == ("memory-a", "memory-b")
    assert store.update_conflict_pins(lease_id, remove=["memory-a"]) == ("memory-b",)
    assert MemoryCuratorStateStore(store.path).load().conflict_pin_ids == ["memory-b"]


def test_curator_pins_conflict_targets_into_later_windows_until_they_disappear(tmp_path):
    store, thread, message = _conversation(tmp_path)
    memory = _memory(tmp_path)
    target = _fact(memory, "公司服务器统一使用 HTTPS 部署。", "ops.deploy")
    source = _source(tmp_path, memory)
    payload = _valid_output(thread.thread_id, message.message_id, message.content)
    payload["candidates"][0]["conflicts_with"] = [target.entry_id]
    backend = _StaticStructuredBackend(payload)
    service = _service(tmp_path, backend, store, formal_memory_source=source)

    assert service.run(reason="admin").status == "succeeded"
    assert service.candidate_service.list()[0].status == "blocked_conflict"
    assert service.state_store.load().conflict_pin_ids == [target.entry_id]
    # 置顶只影响下一轮窗口，自己不会触发运行。
    assert service.run_if_due().status == "not_due" and backend.calls == 1

    unrelated = "今天中午吃了牛肉面。"
    assert target.entry_id not in [item.authority_id for item in source.read(query=unrelated)]
    second = store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": unrelated, "now": 12.0})
    backend.payload = _empty_extraction(thread.thread_id, second.message_id)
    assert service.run(reason="admin").status == "succeeded"
    assert target.entry_id in backend.prompts[-1]
    assert service.state_store.load().conflict_pin_ids == [target.entry_id]

    memory.apply_batch([{"action": "remove", "entry_id": target.entry_id, "source": "fixture"}])
    third = store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": "晚上去跑步。", "now": 13.0})
    backend.payload = _empty_extraction(thread.thread_id, third.message_id)
    assert service.run(reason="admin").status == "succeeded"
    assert service.state_store.load().conflict_pin_ids == []


# ---------- OL-3b：conservative_v2 同主题自动替换 ----------


def test_conservative_v2_is_an_accepted_policy_value():
    settings, warnings = normalize_memory_settings({"memory_curator_auto_promotion_policy": "conservative_v2"})
    assert settings.memory_curator_auto_promotion_policy == "conservative_v2" and warnings == []
    settings, warnings = normalize_memory_settings({"memory_curator_auto_promotion_policy": "conservative_v3"})
    assert settings.memory_curator_auto_promotion_policy == "conservative_v1" and warnings
    config = MemoryCuratorConfig.from_agent_config(SimpleNamespace(memory_curator_auto_promotion_policy="conservative_v2"))
    assert config.auto_promotion_policy == "conservative_v2"


@pytest.mark.parametrize("entry", ["single", "batch"])
def test_v2_automatic_promotion_supersedes_the_single_same_subject_entry(tmp_path, entry):
    service, conversations, thread = _runtime(tmp_path)
    prior = _seed_prior(service)
    candidate = _explicit_at(service, conversations, thread, content=_NEW_FACT, now=time.time() + 60)

    with automatic_promotion_policy("conservative_v2"):
        if entry == "single":
            result = service.promote(candidate.candidate_id, automatic=True)
        else:
            (result,) = service.promote_batch([candidate], reviewer="memory-promotion-service", automatic=True)

    assert (result.promoted, result.reason_code) == (True, AUTO_SUPERSEDED_REASON)
    assert result.superseded == {"entry_id": prior.entry_id, "version": prior.version,
                                 "content_hash": "sha256:" + memory_content_hash(prior.content)}
    assert result.to_dict()["superseded"]["entry_id"] == prior.entry_id
    (current,) = service.long_term.read_ids([prior.entry_id])
    assert (current.content, current.version) == (_NEW_FACT, prior.version + 1)
    assert [row.entry_id for row in service.long_term.all()] == [prior.entry_id]
    saved = service.candidates.get(candidate.candidate_id)
    assert (saved.status, saved.proposed_action, saved.target_entry_id, saved.target_entry_version,
            saved.conflicts_with) == ("promoted", "replace", prior.entry_id, prior.version, [])


@pytest.mark.parametrize(("policy", "automatic"), [
    ("conservative_v1", True), ("", True), ("manual_only", True), ("conservative_v2", False)])
def test_same_subject_conflict_stays_blocked_outside_v2_automatic_promotion(tmp_path, policy, automatic):
    service, conversations, thread = _runtime(tmp_path)
    prior = _seed_prior(service)
    candidate = _explicit_at(service, conversations, thread, content=_NEW_FACT, now=time.time() + 60)
    reviewer = "memory-promotion-service" if automatic else "explicit-user"
    if not automatic:
        service.review(candidate.candidate_id, approved=True, reviewer=reviewer)

    with automatic_promotion_policy(policy):
        result = service.promote(candidate.candidate_id, reviewer=reviewer, automatic=automatic)

    assert (result.promoted, result.status, result.reason_code) == (
        False, "blocked_conflict", "SUBJECT_SCOPE_CONFLICT_REQUIRES_REPLACE")
    (current,) = service.long_term.read_ids([prior.entry_id])
    assert (current.content, current.version) == (prior.content, prior.version)
    saved = service.candidates.get(candidate.candidate_id)
    assert (saved.proposed_action, saved.conflicts_with) == ("add", [prior.entry_id])


@pytest.mark.parametrize("case", ["prior_model_inferred", "prior_alias_scope", "prior_expiring", "evidence_older"])
def test_v2_refuses_unsafe_same_subject_supersede(tmp_path, case):
    service, conversations, thread = _runtime(tmp_path)
    scope_type, scope_key = ("project", "project:alpha") if case == "prior_alias_scope" else ("personal", "personal")
    prior = _seed_prior(
        service,
        origin="model_inferred" if case == "prior_model_inferred" else "user_explicit",
        scope_type=scope_type,
        scope_key="task:alpha" if case == "prior_alias_scope" else scope_key,
        expires_at=time.time() + 86_400 if case == "prior_expiring" else 0.0,
    )
    now = 12.0 if case == "evidence_older" else time.time() + 60
    candidate = _explicit_at(service, conversations, thread, content=_NEW_FACT, now=now,
                             scope_type=scope_type, scope_key=scope_key)

    with automatic_promotion_policy("conservative_v2"):
        result = service.promote(candidate.candidate_id, automatic=True)

    assert (result.promoted, result.status, result.reason_code) == (
        False, "blocked_conflict", "SUBJECT_SCOPE_CONFLICT_REQUIRES_REPLACE")
    (current,) = service.long_term.read_ids([prior.entry_id])
    assert (current.content, current.version) == (prior.content, prior.version)
    assert service.candidates.get(candidate.candidate_id).proposed_action == "add"


# LLM: 确定性交错：绑定已落盘、正式写入前的那一刻，另一写入者按同一旧版本抢先替换成功；
#   本次替换必须被版本 CAS 拒绝，候选按已绑定快照转 blocked_conflict，正式库保留抢先写入的内容。
def test_v2_concurrent_write_after_bind_loses_the_version_cas(tmp_path, monkeypatch):
    service, conversations, thread = _runtime(tmp_path)
    prior = _seed_prior(service)
    candidate = _explicit_at(service, conversations, thread, content=_NEW_FACT, now=time.time() + 60)
    original = service.long_term.apply_batch
    interleaved: list[int] = []

    def apply_batch(operations, *, _admission=None):
        if not interleaved and any(op.get("expected_version") is not None for op in operations):
            interleaved.append(len(operations))
            original([{"action": "replace", "entry_id": prior.entry_id, "content": "个人电脑使用 FreeBSD。",
                       "source": "concurrent-writer", "expected_version": prior.version}], _admission=_admission)
        return original(operations, _admission=_admission)

    monkeypatch.setattr(service.long_term, "apply_batch", apply_batch)
    with automatic_promotion_policy("conservative_v2"):
        result = service.promote(candidate.candidate_id, automatic=True)

    assert interleaved == [1]
    assert (result.promoted, result.status, result.reason_code) == (
        False, "blocked_conflict", "TARGET_ENTRY_CONCURRENT_WRITE")
    (current,) = service.long_term.read_ids([prior.entry_id])
    assert (current.content, current.version) == ("个人电脑使用 FreeBSD。", prior.version + 1)
    saved = service.candidates.get(candidate.candidate_id)
    assert (saved.status, saved.proposed_action, saved.target_entry_version, saved.conflicts_with) == (
        "blocked_conflict", "replace", prior.version, [prior.entry_id])


def test_v2_formal_change_between_bind_and_commit_is_a_stale_observation(tmp_path, monkeypatch):
    service, conversations, thread = _runtime(tmp_path)
    prior = _seed_prior(service)
    candidate = _explicit_at(service, conversations, thread, content=_NEW_FACT, now=time.time() + 60)
    original_bind = service.candidates.bind_same_subject_replace

    def bind_then_concurrent_write(expected, **kwargs):
        bound = original_bind(expected, **kwargs)
        service.long_term.replace(prior.entry_id, "个人电脑使用 FreeBSD。", expected_version=prior.version)
        return bound

    monkeypatch.setattr(service.candidates, "bind_same_subject_replace", bind_then_concurrent_write)
    with automatic_promotion_policy("conservative_v2"):
        result = service.promote(candidate.candidate_id, automatic=True)

    assert (result.promoted, result.status, result.reason_code) == (
        False, "blocked_conflict", "TARGET_ENTRY_STALE_OBSERVATION")
    (current,) = service.long_term.read_ids([prior.entry_id])
    assert current.content == "个人电脑使用 FreeBSD。"


def test_v2_crash_after_formal_replace_recovers_without_a_second_write(tmp_path, monkeypatch):
    service, conversations, thread = _runtime(tmp_path)
    prior = _seed_prior(service)
    candidate = _explicit_at(service, conversations, thread, content=_NEW_FACT, now=time.time() + 60)
    original = service.candidates.mark_promoted_admitted
    crashes: list[bool] = []

    def crash_once(*args, **kwargs):
        if not crashes:
            crashes.append(True)
            raise OSError("simulated candidate ledger write failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(service.candidates, "mark_promoted_admitted", crash_once)
    with automatic_promotion_policy("conservative_v2"):
        first = service.promote(candidate.candidate_id, automatic=True)
        assert first.promoted is False
        (current,) = service.long_term.read_ids([prior.entry_id])
        assert (current.content, current.version) == (_NEW_FACT, prior.version + 1)
        pending = service.candidates.get(candidate.candidate_id)
        assert (pending.status, pending.proposed_action, pending.commit_obligation) == ("approved", "replace", True)
        second = service.promote(candidate.candidate_id, automatic=True)

    assert (second.promoted, second.reason_code) == (True, "UPDATE_COMMIT_RECOVERED")
    (current,) = service.long_term.read_ids([prior.entry_id])
    assert current.version == prior.version + 1
    assert service.candidates.get(candidate.candidate_id).status == "promoted"


def test_v2_bound_evidence_cannot_refresh_the_target_version(tmp_path):
    service, conversations, thread = _runtime(tmp_path)
    prior = _seed_prior(service)
    candidate = _explicit_at(service, conversations, thread, content=_NEW_FACT, now=time.time() + 60)
    with automatic_promotion_policy("conservative_v2"):
        assert service.promote(candidate.candidate_id, automatic=True).promoted

    # 同一份旧证据不能再拿来指向替换后的新版本，重放不会造成第二次覆盖。
    with pytest.raises(CandidateIdentityConflictError):
        service.candidates.observe(CandidateObservation(
            candidate_type="long_term_fact", content=_NEW_FACT, subject_key="device.os",
            scope=MemoryScope("personal", "personal"), origin="user_explicit",
            source_message_refs=tuple(candidate.source_message_refs), learning_source=candidate.learning_source,
            proposed_action="replace", target_entry_id=prior.entry_id, target_entry_version=prior.version + 1,
            promotion_target="long_term", promotion_mode="auto_eligible", confidence=0.99))


# LLM: 端到端走真实 Curator → 晋升服务：v1 批次里同主题冲突转 blocked_conflict 并把旧条目置顶；
#   切到 v2 后新说法自动替换旧条目，回执带被替换身份，置顶随替换移除。策略只经 Curator 当前配置传入。
def test_curator_v2_policy_supersedes_and_promotion_receipts_move_conflict_pins(tmp_path):
    promotion, store, thread = _runtime(tmp_path)
    prior = _seed_prior(promotion)
    backend = _StaticStructuredBackend({})
    source = FormalMemorySource(long_term=promotion.long_term, lessons=promotion.lessons, hot=promotion.hot)
    base = _service(tmp_path / "owner", backend, store, formal_memory_source=source)

    def curator(policy: str) -> MemoryCuratorService:
        return MemoryCuratorService(config=replace(base.config, auto_promotion_policy=policy),
            dependencies=replace(base.dependencies, candidate_service=promotion.candidates,
                                 promotion_callback=lambda cid: promotion.promote(cid, automatic=True)))

    first = _explicit_at(promotion, store, thread, content=_NEW_FACT, now=time.time() + 60)
    backend.payload = _empty_extraction(thread.thread_id, first.source_message_refs[0]["message_id"])
    result = curator("conservative_v1").run(reason="admin")
    assert result.status == "succeeded"
    assert [(row["candidate_id"], row["status"]) for row in result.promotion_results] == [
        (first.candidate_id, "blocked_conflict")]
    assert base.state_store.load().conflict_pin_ids == [prior.entry_id]

    latest = "个人电脑已经换成 Linux 了。"
    second = _explicit_at(promotion, store, thread, content=latest, now=time.time() + 120)
    backend.payload = _empty_extraction(thread.thread_id, second.source_message_refs[0]["message_id"])
    result = curator("conservative_v2").run(reason="admin")
    assert result.status == "succeeded"
    (receipt,) = [row for row in result.promotion_results if row["candidate_id"] == second.candidate_id]
    assert (receipt["promoted"], receipt["reason_code"]) == (True, AUTO_SUPERSEDED_REASON)
    assert receipt["superseded"]["entry_id"] == prior.entry_id
    assert prior.entry_id in backend.prompts[-1]
    (current,) = promotion.long_term.read_ids([prior.entry_id])
    assert current.content == latest
    assert base.state_store.load().conflict_pin_ids == []
