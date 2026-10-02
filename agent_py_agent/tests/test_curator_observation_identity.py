"""Curator 整批提交不能被单条候选或一条长警告卡死，同一批输入也不能被无限重放。

背景（J13，2026-10-02）：隔离真实复测里同一批输入 3 次 CURATOR_COMMIT_FAILED，根因是
“candidate observation identity was reused for a different typed subject”；生产 10-02 也有同类提交失败。
提交失败后游标不动，同一批被反复重放，每次都是一次 4–5 万输出 token 的大调用。锁定：
1. 身份比对与身份计算同一口径：同一条消息里主题键只差大小写的两条候选按同一观察合并，整批提交、游标推进；
2. 与账本已有观察身份冲突的单条候选在提交前剔除，记结构化警告，其余照常提交（共用合并函数仍严格）；
3. 模型写的长警告/多警告先截短限数，不再让运行账校验失败；
4. 同一起始游标连续 CURATOR_REPLAY_BREAKER_FAILURE_COUNT 次确定性失败即熔断：state 记熔断码、退避一小时，
   运行账保留真实失败码并加警告；中间有成功或游标变化都不熔断；网络类失败不计入。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation
from agent_py_agent.agent.memory_store.candidates import (
    CandidateIdentityConflictError,
    CandidateService,
    merge_candidate_observations,
    split_identity_conflicts,
)
from agent_py_agent.agent.memory_store.curator import (
    MemoryCuratorDependencies,
    MemoryCuratorIdentity,
    MemoryCuratorService,
)
from agent_py_agent.agent.memory_store.curator_models import (
    CURATOR_OUTPUT_SCHEMA_VERSION,
    CURATOR_REPLAY_BREAKER_FAILURE_COUNT,
    CURATOR_REPLAY_BREAKER_OPEN,
    CURATOR_REPLAY_BREAKER_RETRY_SECONDS,
    MemoryCuratorConfig,
    curator_failure_retry_seconds,
    curator_replay_breaker_tripped,
)
from agent_py_agent.agent.memory_store.curator_run_log import (
    RUN_WARNING_MAX_CHARS,
    RUN_WARNINGS_MAX_COUNT,
    CuratorRunLog,
)
from agent_py_agent.agent.memory_store.curator_state import MemoryCuratorStateStore
from agent_py_agent.agent.memory_store.daily import DailyMemoryStore


# 函数用途: 按脚本依次返回 Curator 输出（字典按 JSON 返回、字符串原样返回、异常对象直接抛出），记调用次数。
class _Backend:
    name = "scripted-curator"

    def __init__(self, *script: object) -> None:
        self.script = list(script)
        self.calls = 0

    def generate_structured(self, prompt: str, *, response_schema: dict[str, object]) -> ModelResponse:
        del prompt, response_schema
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(item, BaseException):
            raise item
        text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        return ModelResponse(text=text, backend=self.name)


# 函数用途: 造一条真实会话里的一条用户消息（一句话里说了两件事）。
def _conversation(tmp_path: Path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "user-1", "channel": "internal",
                                          "channel_conversation_id": "chat-1", "channel_user_id": "user-1", "now": 10.0})
    message = store.messages.append({"thread_id": thread.thread_id, "role": "user", "channel": "internal",
                                     "content": "我对花生过敏，也对芒果过敏。", "now": 11.0,
                                     "metadata": {"session_id": "session-1", "request_id": "request-1"}})
    return store, thread, message


# 函数用途: 造真实 state/daily/candidate/run-log 权威上的 Curator 服务（max_retries=0：解析失败只调一次）。
def _service(tmp_path: Path, backend: object, store: ConversationStore) -> MemoryCuratorService:
    config = MemoryCuratorConfig(interval_seconds=60, turn_threshold=1, batch_message_limit=80, max_input_chars=40000,
                                 timeout_seconds=1, max_retries=0, daily_finalize_hour=23)
    return MemoryCuratorService(config=config, dependencies=MemoryCuratorDependencies(
        backend=backend, conversation_store=store, audit_dir=tmp_path / "audit",
        state_store=MemoryCuratorStateStore(tmp_path / "memory" / "curator" / "state.json"),
        daily_store=DailyMemoryStore(tmp_path / "memory" / "daily"),
        candidate_service=CandidateService(tmp_path / "memory" / "candidates.jsonl"),
        run_log=CuratorRunLog(tmp_path / "memory" / "curator" / "runs"),
        identity=MemoryCuratorIdentity(provider="scripted-curator", model="identity-test")))


# 函数用途: 一条用户明说的长期事实候选。
def _candidate(message_id: str, content: str, subject_key: str) -> dict:
    return {"candidate_type": "long_term_fact", "content": content, "subject_key": subject_key,
            "scope": {"scope_type": "personal", "scope_key": "personal", "applies_when": "", "excludes_when": ""},
            "origin": "user_explicit", "source_message_refs": [{"message_id": message_id}], "source_tool_refs": [],
            "source_artifact_refs": [], "observed_at": None, "valid_from": None, "valid_until": None, "confidence": 0.9,
            "proposed_action": "add", "target_entry_id": None, "conflicts_with": [], "promotion_target": "long_term"}


# 函数用途: 造一份 Curator 输出；extra 可给 processed=False（不声明处理了这条消息，游标不推进）与 warnings。
def _output(message, candidates: list[dict], extra: dict | None = None) -> dict:
    extra = extra or {}
    processed = extra.get("processed", True)
    refs = [{"message_id": message.message_id}] if processed else []
    cursor = [{"thread_id": message.thread_id, "message_id": message.message_id}] if processed else []
    return {"schema_version": CURATOR_OUTPUT_SCHEMA_VERSION, "daily_events": [], "candidates": candidates,
            "processed_message_refs": refs, "processed_audit_refs": [], "unresolved_refs": [],
            "warnings": extra.get("warnings", []), "next_cursor": {"per_thread_cursors": cursor, "last_audit_event_id": None}}


def test_case_variant_subjects_in_one_batch_merge_instead_of_failing_the_batch(tmp_path):
    # 主题键只差大小写、内容规范化后相同（只多了空白）：同一观察，合并且整批不失败。
    store, thread, message = _conversation(tmp_path)
    backend = _Backend(_output(message, [
        _candidate(message.message_id, "用户对花生过敏。", "health.allergy"),
        _candidate(message.message_id, "  用户对花生过敏。 ", "Health.Allergy"),
    ]))
    service = _service(tmp_path, backend, store)
    result = service.run(reason="admin")
    assert (result.status, result.failure_code, result.processed_messages) == ("succeeded", "", 1)
    assert service.state_store.load().per_thread_cursors == {thread.thread_id: message.message_id}
    [candidate] = service.candidate_service.list()
    assert candidate.occurrence_count == 1, "同一条消息、同一主题（忽略大小写）、同一内容按同一观察合并"
    [record] = service.run_log.list()
    assert not any(item.startswith("curator_candidate_identity_conflict_dropped") for item in record.warnings)


# 用户 2026-10-02 拍板第 6 条：同一条消息里同一主题、内容不同的是两件事，各存一条（J13 的花生/芒果）。
def test_same_message_same_subject_different_content_are_stored_separately(tmp_path):
    store, thread, message = _conversation(tmp_path)
    backend = _Backend(_output(message, [
        _candidate(message.message_id, "用户对花生过敏。", "health.allergy"),
        _candidate(message.message_id, "用户对芒果过敏。", "health.allergy"),
    ]))
    service = _service(tmp_path, backend, store)
    result = service.run(reason="admin")
    assert (result.status, result.failure_code, result.processed_messages) == ("succeeded", "", 1)
    candidates = service.candidate_service.list()
    assert {item.content for item in candidates} == {"用户对花生过敏。", "用户对芒果过敏。"}
    assert all(item.subject_key == "health.allergy" and item.occurrence_count == 1 for item in candidates)
    assert len({key for item in candidates for key in item.observation_keys}) == 2, "两条观察身份不同"


def test_exact_duplicate_content_in_one_batch_is_still_one_observation(tmp_path):
    store, _thread, message = _conversation(tmp_path)
    allergy = _candidate(message.message_id, "用户对花生过敏。", "health.allergy")
    service = _service(tmp_path, _Backend(_output(message, [allergy, dict(allergy)])), store)
    assert service.run(reason="admin").status == "succeeded"
    [candidate] = service.candidate_service.list()
    assert candidate.occurrence_count == 1 and len(candidate.observation_keys) == 1


def test_replaying_an_unclaimed_message_with_the_same_content_does_not_duplicate(tmp_path):
    # 第一批没声明处理这条消息（游标不动），候选已提交；下一批原样重放：同一观察，不新增、不虚增出现次数。
    store, thread, message = _conversation(tmp_path)
    peanut = _candidate(message.message_id, "用户对花生过敏。", "health.allergy")
    mango = _candidate(message.message_id, "用户对芒果过敏。", "health.allergy")
    backend = _Backend(_output(message, [peanut, mango], {"processed": False}), _output(message, [peanut, mango]))
    service = _service(tmp_path, backend, store)
    assert service.run(reason="admin").processed_messages == 0
    assert service.run(reason="admin").processed_messages == 1
    assert service.state_store.load().per_thread_cursors == {thread.thread_id: message.message_id}
    candidates = service.candidate_service.list()
    assert len(candidates) == 2 and all(item.occurrence_count == 1 for item in candidates)


def test_paraphrase_of_the_same_subject_in_one_batch_is_a_separate_candidate(tmp_path):
    # 已知代价（用户拍板按内容区分）：同一来源同一主题换了说法，宿主不按文案猜是不是一回事，各存一条；
    # 两条各自 occurrence_count=1，不会虚增同一条候选的出现次数。
    store, _thread, message = _conversation(tmp_path)
    backend = _Backend(_output(message, [
        _candidate(message.message_id, "用户对花生过敏。", "health.allergy"),
        _candidate(message.message_id, "用户对花生过敏（重述）。", "health.allergy"),
    ]))
    service = _service(tmp_path, backend, store)
    assert service.run(reason="admin").status == "succeeded"
    candidates = service.candidate_service.list()
    assert len(candidates) == 2 and all(item.occurrence_count == 1 for item in candidates)


def test_a_candidate_conflicting_with_the_ledger_is_dropped_and_the_batch_still_commits(tmp_path):
    store, thread, message = _conversation(tmp_path)
    allergy = _candidate(message.message_id, "用户对花生过敏。", "health.allergy")
    mango = _candidate(message.message_id, "用户喜欢喝手冲咖啡。", "diet.coffee")
    backend = _Backend(_output(message, [allergy], {"processed": False}),
                       _output(message, [allergy, mango]))
    service = _service(tmp_path, backend, store)
    assert service.run(reason="admin").processed_messages == 0
    # 另一条写入路径留下的同一观察身份、但主题不同的候选（直接改账本模拟）。
    ledger = tmp_path / "memory" / "candidates.jsonl"
    [row] = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    ledger.write_text(json.dumps({**row, "subject_key": "diet.preference"}, ensure_ascii=False) + "\n", encoding="utf-8")

    result = service.run(reason="admin")

    assert (result.status, result.failure_code, result.processed_messages) == ("succeeded", "", 1)
    assert service.state_store.load().per_thread_cursors == {thread.thread_id: message.message_id}
    subjects = sorted(item.subject_key for item in service.candidate_service.list())
    assert subjects == ["diet.coffee", "diet.preference"], "冲突那条被剔除，不改已有候选，其余照常写入"
    last = service.run_log.list()[-1]
    assert "curator_candidate_identity_conflict_dropped:1" in last.warnings


def test_long_or_many_model_warnings_are_bounded_before_the_run_audit(tmp_path):
    store, thread, message = _conversation(tmp_path)
    warnings = ["长" * 450, *[f"警告 {index}" for index in range(31)]]
    backend = _Backend(_output(message, [], {"warnings": warnings}))
    service = _service(tmp_path, backend, store)
    result = service.run(reason="admin")
    assert (result.status, result.processed_messages) == ("succeeded", 1)
    [record] = service.run_log.list()
    assert len(record.warnings) <= RUN_WARNINGS_MAX_COUNT and max(len(item) for item in record.warnings) <= RUN_WARNING_MAX_CHARS


# 函数用途: 连跑 count 次管理员运行，返回最后一次结果。
def _runs(service: MemoryCuratorService, count: int):
    result = None
    for _ in range(count):
        result = service.run(reason="admin")
    return result


def test_the_same_input_failing_deterministically_trips_the_replay_breaker(tmp_path):
    store, _thread, _message = _conversation(tmp_path)
    service = _service(tmp_path, _Backend("这不是 JSON"), store)
    before = _runs(service, CURATOR_REPLAY_BREAKER_FAILURE_COUNT - 1)
    assert before.failure_code == "CURATOR_SCHEMA_INVALID"
    assert service.state_store.load().last_failure_code == "CURATOR_SCHEMA_INVALID", "还没到上限"

    result = service.run(reason="admin")

    assert result.failure_code == "CURATOR_SCHEMA_INVALID", "运行结果与运行账保留真实失败码"
    assert service.state_store.load().last_failure_code == CURATOR_REPLAY_BREAKER_OPEN
    assert "curator_replay_breaker_open:CURATOR_SCHEMA_INVALID" in service.run_log.list()[-1].warnings
    assert curator_failure_retry_seconds(CURATOR_REPLAY_BREAKER_OPEN, 60) == CURATOR_REPLAY_BREAKER_RETRY_SECONDS == 3600
    state = service.state_store.load()
    soon = datetime.fromisoformat(state.last_failure_at) + timedelta(seconds=CURATOR_REPLAY_BREAKER_RETRY_SECONDS - 60)
    assert service._run_pending_when_due(state, "admin", soon).status == "not_due", "熔断后一小时内不再重放"


def test_a_success_in_between_keeps_the_breaker_closed(tmp_path):
    store, thread, message = _conversation(tmp_path)
    bad = "这不是 JSON"
    empty = _output(message, [], {"processed": False})
    service = _service(tmp_path, _Backend(bad, bad, empty, bad), store)
    _runs(service, 4)
    assert service.state_store.load().last_failure_code == "CURATOR_SCHEMA_INVALID", "中间有一次成功，不算连续"


def test_the_breaker_rule_needs_the_same_cursor_deterministic_codes_and_enough_failures():
    cursor = {"per_thread_cursors": {"t": "m1"}, "last_audit_event_id": ""}

    def row(code: str, at: dict = cursor, status: str = "failed") -> SimpleNamespace:
        return SimpleNamespace(status=status, failure_code=code, cursor_before=at)

    same = [row("CURATOR_COMMIT_FAILED"), row("CURATOR_SCHEMA_INVALID")]
    assert curator_replay_breaker_tripped("CURATOR_COMMIT_FAILED", cursor, same)
    assert not curator_replay_breaker_tripped("CURATOR_MODEL_FAILED", cursor, same), "网络类失败不计入"
    assert not curator_replay_breaker_tripped("CURATOR_COMMIT_FAILED", cursor, same[:1]), "次数不够"
    moved = [row("CURATOR_COMMIT_FAILED"), row("CURATOR_COMMIT_FAILED", {"per_thread_cursors": {}, "last_audit_event_id": ""})]
    assert not curator_replay_breaker_tripped("CURATOR_COMMIT_FAILED", cursor, moved), "游标不同就是另一批输入"
    mixed = [row("CURATOR_COMMIT_FAILED"), row("CURATOR_MODEL_FAILED")]
    assert not curator_replay_breaker_tripped("CURATOR_COMMIT_FAILED", cursor, mixed)
    succeeded = [row("", status="succeeded"), row("CURATOR_COMMIT_FAILED")]
    assert not curator_replay_breaker_tripped("CURATOR_COMMIT_FAILED", cursor, succeeded)


def test_transient_model_failures_never_trip_the_replay_breaker(tmp_path):
    store, _thread, _message = _conversation(tmp_path)
    service = _service(tmp_path, _Backend(ConnectionResetError("连接被重置")), store)
    _runs(service, CURATOR_REPLAY_BREAKER_FAILURE_COUNT + 1)
    assert service.state_store.load().last_failure_code == "CURATOR_MODEL_FAILED"


# 函数用途: 造一条带显式观察编号的观察（模拟两条写入路径共用同一观察身份）。
def _observation(subject_key: str, content: str) -> CandidateObservation:
    return CandidateObservation(candidate_type="long_term_fact", content=content, subject_key=subject_key,
                                scope={"scope_type": "personal", "scope_key": "personal"}, origin="user_explicit",
                                source_message_refs=({"ref_id": "message:m1", "message_id": "m1"},),
                                proposed_action="add", promotion_target="long_term", observation_id="shared-observation")


def test_the_shared_merge_stays_strict_and_the_split_only_drops_identity_conflicts():
    first = _observation("health.allergy", "对花生过敏。")
    with pytest.raises(CandidateIdentityConflictError) as caught:
        merge_candidate_observations([], [first, _observation("diet.coffee", "喜欢咖啡。")])
    assert isinstance(caught.value, ValueError), "原调用方按 ValueError 处理的全有或全无语义不变"
    merged, _ = merge_candidate_observations([], [first, _observation("Health.Allergy", "对花生过敏。")])
    assert len(merged) == 1, "只差大小写的主题是同一主题"
    kept, dropped = split_identity_conflicts([], [first, _observation("diet.coffee", "喜欢咖啡。"),
                                                  _observation("HEALTH.ALLERGY", "对花生过敏（重述）。")])
    assert (len(kept), dropped) == (2, 1) and kept[0] is first
    with pytest.raises(ValueError):
        split_identity_conflicts([], [_observation("health.allergy", "")])


def test_scope_alias_and_canonical_key_are_the_same_scope_for_identity():
    def project(scope_key: str) -> CandidateObservation:
        return CandidateObservation(candidate_type="long_term_fact", content="项目用 PostgreSQL。", subject_key="project.db",
                                    scope={"scope_type": "project", "scope_key": scope_key}, origin="user_explicit",
                                    source_message_refs=({"ref_id": "message:m1", "message_id": "m1"},),
                                    proposed_action="none", promotion_target="none", observation_id="shared-observation")

    merged, _ = merge_candidate_observations([], [project("task:abc"), project("project:abc")])
    assert len(merged) == 1, "旧别名 task:abc 与规范键 project:abc 是同一范围，与观察身份计算同一口径"
