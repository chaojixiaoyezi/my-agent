"""记忆整理按“消息来源会话的主代理模型”走（用户 2026-10-02 拍板第 2 条细化）。

memory_curator_model_profile 留空时，一批里来自不同会话、不同模型的消息按会话模型分组，每组一次完整运行
（租约、提取、校验、一次事务提交都沿原合同）。锁定：
1. 两个会话两个模型 → 两次调用各用各的模型、只看到自己会话的消息、各自推进游标；
2. 后一组失败时前一组已提交、不重复落账，失败组游标不动；
3. 会话自己的模型输出坏 JSON 时，本次运行用 owner 默认模型补跑一次，成功照常提交并留结构化原因；
4. 别的组在中间成功不打断本组的“同一输入连续确定性失败”计数，熔断照常；
5. 工具审计事件只跟默认模型那组走；
6. 组合根路由器只读会话的结构化模型选择：没选 → 默认，选了可用 → 用它，选了不可用 → 退回默认并带原因，
   指定了整理档案 → 不路由；都在该 owner 自己的目录里解析（含管理员共享）。
7. 指定的整理档案在某个 owner 那里解析不到（3a 2026-10-02 定）→ 该 owner 改用自己的默认模型，
   运行记录带 curator_profile_unavailable_fallback:<原因>；能解析到的 owner 照常用指定档案。
8. 会话自己的模型连不上（3a 2026-10-02 生产：qwen 那组连续 ProviderTransientError，排在最前卡住整个 owner）→ 同一组同一
   起始游标连续失败 CURATOR_TRANSIENT_FALLBACK_FAILURE_COUNT 次后，下一次运行直接用 owner 默认模型、游标推进，记录带
   curator_thread_model_failed:<码>:transient；默认模型自己连不上照旧退避；别组夹在中间成功不清零计数。
9. 上一次推进本组游标的就是这种连不上的默认补跑时（3a 2026-10-02 生产后续），下一批只给会话自己的模型 1 次机会，
   失败 1 次就直接换默认；会话自己的模型成功推进后，回到连续 2 次。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.curator import (
    MemoryCuratorDependencies,
    MemoryCuratorIdentity,
    MemoryCuratorService,
)
from agent_py_agent.agent.memory_store.curator_inputs import (
    CuratorAuditInput,
    CuratorInputBatch,
    CuratorMessageInput,
)
from agent_py_agent.agent.memory_store.curator_models import (
    CURATOR_OUTPUT_SCHEMA_VERSION,
    CURATOR_REPLAY_BREAKER_OPEN,
    CuratorModelRoute,
    CuratorModelRouting,
    MemoryCuratorConfig,
)
from agent_py_agent.agent.memory_store.curator_routing import (
    CURATOR_TRANSIENT_FALLBACK_FAILURE_COUNT,
    combined_run_result,
    group_breaker_history,
    group_cursor_view,
    route_batch,
    transient_fallback_code,
    transient_fallback_warning,
)
from agent_py_agent.agent.memory_store.curator_run_log import CuratorRunLog
from agent_py_agent.agent.memory_store.curator_state import MemoryCuratorStateStore
from agent_py_agent.agent.memory_store.daily import DailyMemoryStore

_MESSAGE_ID = re.compile(r'"message_id":\s*"([^"]+)"')


# 函数用途: 按提示里出现的消息编号回显合规输出（每条消息一条候选、全部声明已处理）；坏 JSON 模式只回非 JSON 文本。
class _EchoBackend:
    def __init__(self, name: str, thread_of: dict[str, str], *, bad_threads: frozenset[str] = frozenset()) -> None:
        self.name = name
        self.thread_of = thread_of
        self.bad_threads = bad_threads
        self.seen: list[tuple[str, ...]] = []

    def generate_structured(self, prompt: str, *, response_schema: dict[str, object]) -> ModelResponse:
        del response_schema
        ids = tuple(item for item in dict.fromkeys(_MESSAGE_ID.findall(prompt)) if item in self.thread_of)
        self.seen.append(ids)
        if {self.thread_of[item] for item in ids} & self.bad_threads:
            return ModelResponse(text="this is not json", backend=self.name)
        candidates = [{"candidate_type": "long_term_fact", "content": f"{self.name} 记下 {item}", "subject_key": f"fact.{item}",
                       "scope": {"scope_type": "personal", "scope_key": "personal", "applies_when": "", "excludes_when": ""},
                       "origin": "user_explicit", "source_message_refs": [{"message_id": item}], "source_tool_refs": [],
                       "source_artifact_refs": [], "observed_at": None, "valid_from": None, "valid_until": None,
                       "confidence": 0.9, "proposed_action": "add", "target_entry_id": None, "conflicts_with": [],
                       "promotion_target": "long_term"} for item in ids]
        cursors = {self.thread_of[item]: item for item in ids}
        output = {"schema_version": CURATOR_OUTPUT_SCHEMA_VERSION, "daily_events": [], "candidates": candidates,
                  "processed_message_refs": [{"message_id": item} for item in ids], "processed_audit_refs": [],
                  "unresolved_refs": [], "warnings": [],
                  "next_cursor": {"per_thread_cursors": [{"thread_id": t, "message_id": m} for t, m in cursors.items()],
                                  "last_audit_event_id": None}}
        return ModelResponse(text=json.dumps(output, ensure_ascii=False), backend=self.name)

    # 函数用途: 这个后端看到过的会话编号（按调用依次）。
    def threads_seen(self) -> list[set[str]]:
        return [{self.thread_of[item] for item in ids} for ids in self.seen]


# 函数用途: 造两个真实会话 A（更早）和 B，各一条用户消息；返回 store、两个会话编号和“消息 → 会话”映射。
def _two_threads(tmp_path: Path):
    store = ConversationStore(tmp_path / "conversations")
    thread_of: dict[str, str] = {}
    ids = []
    for index, label in enumerate(("a", "b")):
        thread = store.threads.get_or_create({"canonical_user_id": "user-1", "channel": "internal",
                                              "channel_conversation_id": f"chat-{label}", "channel_user_id": "user-1",
                                              "now": 10.0 + index * 10})
        thread_of[_append(store, thread.thread_id, f"会话 {label} 的第一句。", 11.0 + index * 10)] = thread.thread_id
        ids.append(thread.thread_id)
    return store, ids[0], ids[1], thread_of


# 函数用途: 给会话追加一条用户消息，返回消息编号（调用方自己登记“消息 → 会话”）。
def _append(store: ConversationStore, thread_id: str, text: str, now: float) -> str:
    message = store.messages.append({"thread_id": thread_id, "role": "user", "channel": "internal", "content": text,
                                     "now": now, "metadata": {"session_id": "s", "request_id": "r"}})
    return message.message_id


# 函数用途: 默认组 p-default（model-d）与会话 A 的 p-a（model-a）；B 没列出，走默认组。
def _routing(default_backend, a_backend, thread_a: str, reasons: dict[str, str] | None = None) -> CuratorModelRouting:
    default = CuratorModelRoute("p-default", default_backend, "prov-d", "model-d", is_default=True)
    return CuratorModelRouting(default, {"p-a": CuratorModelRoute("p-a", a_backend, "prov-a", "model-a")},
                               {thread_a: "p-a"}, dict(reasons or {}))


# 函数用途: 造真实 state/daily/candidate/run-log 上的 Curator 服务，注入路由器（max_retries=0：解析失败只调一次）。
def _service(tmp_path: Path, store: ConversationStore, routing: CuratorModelRouting) -> MemoryCuratorService:
    config = MemoryCuratorConfig(interval_seconds=60, turn_threshold=1, batch_message_limit=80, max_input_chars=40000,
                                 timeout_seconds=1, max_retries=0, daily_finalize_hour=23)
    return MemoryCuratorService(config=config, dependencies=MemoryCuratorDependencies(
        backend=routing.default.backend, conversation_store=store, audit_dir=tmp_path / "audit",
        state_store=MemoryCuratorStateStore(tmp_path / "memory" / "curator" / "state.json"),
        daily_store=DailyMemoryStore(tmp_path / "memory" / "daily"),
        candidate_service=CandidateService(tmp_path / "memory" / "candidates.jsonl"),
        run_log=CuratorRunLog(tmp_path / "memory" / "curator" / "runs"),
        identity=MemoryCuratorIdentity(provider="prov-d", model="model-d"),
        model_router=lambda thread_ids: routing))


def test_two_threads_with_two_models_are_curated_separately(tmp_path):
    store, thread_a, thread_b, thread_of = _two_threads(tmp_path)
    default, model_a = _EchoBackend("default", thread_of), _EchoBackend("model-a", thread_of)
    service = _service(tmp_path, store, _routing(default, model_a, thread_a, {thread_b: "profile_not_found"}))

    result = service.run(reason="admin")

    assert (result.status, result.processed_messages, result.candidates) == ("succeeded", 2, 2)
    assert "curator_model_groups:2" in result.warnings
    assert model_a.threads_seen() == [{thread_a}] and default.threads_seen() == [{thread_b}], "各组只看自己会话的消息"
    assert set(service.state_store.load().per_thread_cursors) == {thread_a, thread_b}
    records = service.run_log.list()
    assert [(item.status, item.model) for item in records] == [("succeeded", "model-a"), ("succeeded", "model-d")]
    assert "curator_thread_model_fallback:profile_not_found:1" in records[1].warnings, "退回默认的会话带结构化原因"
    contents = sorted(item.content for item in service.candidate_service.list())
    assert contents[0].startswith("default 记下") and contents[1].startswith("model-a 记下")


def test_second_group_failure_keeps_the_first_group_committed_once(tmp_path):
    store, thread_a, thread_b, thread_of = _two_threads(tmp_path)
    default = _EchoBackend("default", thread_of, bad_threads=frozenset({thread_b}))
    model_a = _EchoBackend("model-a", thread_of)
    service = _service(tmp_path, store, _routing(default, model_a, thread_a))

    failed = service.run(reason="admin")

    assert (failed.status, failed.failure_code, failed.model) == ("failed", "CURATOR_SCHEMA_INVALID", "model-d")
    assert failed.processed_messages == 1, "第一组已提交的计数计入汇总"
    assert set(service.state_store.load().per_thread_cursors) == {thread_a}, "失败组游标不动"
    assert len(service.candidate_service.list()) == 1
    default.bad_threads = frozenset()
    retried = service.run(reason="admin")
    assert (retried.status, retried.processed_messages) == ("succeeded", 1)
    assert model_a.threads_seen() == [{thread_a}], "已提交的第一组不再重放"
    assert len(service.candidate_service.list()) == 2, "不重复落账"


def test_bad_json_thread_model_falls_back_to_the_owner_default_once(tmp_path):
    store, thread_a, thread_b, thread_of = _two_threads(tmp_path)
    default = _EchoBackend("default", thread_of)
    model_a = _EchoBackend("model-a", thread_of, bad_threads=frozenset({thread_a}))
    service = _service(tmp_path, store, _routing(default, model_a, thread_a))

    result = service.run(reason="admin")

    assert (result.status, result.processed_messages) == ("succeeded", 2)
    assert model_a.threads_seen() == [{thread_a}], "会话自己的模型只试一次"
    assert default.threads_seen() == [{thread_a}, {thread_b}], "默认模型补跑 A，再正常处理 B"
    first = service.run_log.list()[0]
    assert (first.status, first.model) == ("succeeded", "model-d"), "运行记录写实际用的默认模型"
    assert "curator_thread_model_failed:CURATOR_SCHEMA_INVALID" in first.warnings
    assert set(service.state_store.load().per_thread_cursors) == {thread_a, thread_b}


# 函数用途: 每次调用都抛供应商异常的后端（网络/服务端失败的替身），记调用次数。
class _BrokenBackend:
    name = "broken"

    def __init__(self) -> None:
        self.calls = 0

    def generate_structured(self, prompt: str, *, response_schema: dict[str, object]) -> ModelResponse:
        del prompt, response_schema
        self.calls += 1
        raise RuntimeError("provider unavailable")


def test_a_network_like_failure_of_the_thread_model_is_not_retried_on_the_default(tmp_path):
    store, thread_a, _thread_b, thread_of = _two_threads(tmp_path)
    default, model_a = _EchoBackend("default", thread_of), _BrokenBackend()
    service = _service(tmp_path, store, _routing(default, model_a, thread_a))

    result = service.run(reason="admin")

    assert (result.status, result.failure_code, result.model) == ("failed", "CURATOR_MODEL_FAILED", "model-a")
    assert model_a.calls == 1 and default.seen == [], "非确定性失败不补跑、也不接着跑别的组"
    assert service.state_store.load().per_thread_cursors == {}
    assert not any(item.startswith("curator_thread_model_failed") for item in service.run_log.list()[0].warnings)


def test_breaker_follows_the_failing_group_even_when_another_group_succeeds_in_between(tmp_path):
    store, thread_a, thread_b, thread_of = _two_threads(tmp_path)
    # A 自己的模型和默认模型对 A 都给坏 JSON（补跑也失败）；B 一直正常。
    default = _EchoBackend("default", thread_of, bad_threads=frozenset({thread_a}))
    model_a = _EchoBackend("model-a", thread_of, bad_threads=frozenset({thread_a}))
    service = _service(tmp_path, store, _routing(default, model_a, thread_a))

    first = service.run(reason="admin")
    assert (first.status, first.model) == ("failed", "model-a")
    for round_index in range(2):
        # B 先来一条新消息，A 随后也来一条：A 的最近更新更晚，这一轮 B 先跑并成功，A 再失败。
        thread_of[_append(store, thread_b, f"B 的新消息 {round_index}", 100.0 + round_index * 10)] = thread_b
        thread_of[_append(store, thread_a, f"A 的新消息 {round_index}", 101.0 + round_index * 10)] = thread_a
        result = service.run(reason="admin")
        assert (result.status, result.failure_code) == ("failed", "CURATOR_SCHEMA_INVALID")
    records = service.run_log.list()
    assert [item.status for item in records] == ["failed", "succeeded", "failed", "succeeded", "failed"]
    assert "curator_replay_breaker_open:CURATOR_SCHEMA_INVALID" in records[-1].warnings, "别组成功不打断本组计数"
    assert service.state_store.load().last_failure_code == CURATOR_REPLAY_BREAKER_OPEN
    assert "curator_thread_model_failed:CURATOR_SCHEMA_INVALID" in records[0].warnings


_TRANSIENT = CURATOR_TRANSIENT_FALLBACK_FAILURE_COUNT


def test_thread_model_that_keeps_failing_transiently_yields_to_the_default(tmp_path):
    store, thread_a, thread_b, thread_of = _two_threads(tmp_path)
    default, model_a = _EchoBackend("default", thread_of), _BrokenBackend()
    service = _service(tmp_path, store, _routing(default, model_a, thread_a))

    for _ in range(2):  # 3a 定 N=2：连续失败两次，第三次运行改用默认模型
        failed = service.run(reason="admin")
        assert (failed.status, failed.failure_code, failed.model) == ("failed", "CURATOR_MODEL_FAILED", "model-a")
    assert default.seen == [] and service.state_store.load().per_thread_cursors == {}

    result = service.run(reason="admin")

    assert (result.status, result.processed_messages) == ("succeeded", 2)
    assert model_a.calls == _TRANSIENT, "连不上的模型这次不再试"
    assert default.threads_seen() == [{thread_a}, {thread_b}], "默认模型补跑 A，再正常处理 B"
    assert set(service.state_store.load().per_thread_cursors) == {thread_a, thread_b}, "游标推进"
    record = service.run_log.list()[_TRANSIENT]
    assert (record.status, record.model) == ("succeeded", "model-d"), "运行记录写实际用的默认模型"
    assert "curator_thread_model_failed:CURATOR_MODEL_FAILED:transient" in record.warnings
    # 成功推进过本组游标，计数从头算：新消息来了先给会话自己的模型机会。
    thread_of[_append(store, thread_a, "A 的新消息", 100.0)] = thread_a
    again = service.run(reason="admin")
    assert (again.status, again.model) == ("failed", "model-a") and model_a.calls == _TRANSIENT + 1


# 函数用途: 能切换成“连不上”的回显后端，记调用次数（默认模型自己也出故障的替身）。
class _FlakyBackend(_EchoBackend):
    def __init__(self, name: str, thread_of: dict[str, str]) -> None:
        super().__init__(name, thread_of)
        self.down = False

    def generate_structured(self, prompt: str, *, response_schema: dict[str, object]) -> ModelResponse:
        if self.down:
            raise RuntimeError("provider unavailable")
        return super().generate_structured(prompt, response_schema=response_schema)


def test_default_model_failing_too_backs_off_and_is_still_used_directly(tmp_path):
    store, thread_a, _thread_b, thread_of = _two_threads(tmp_path)
    default, model_a = _FlakyBackend("default", thread_of), _BrokenBackend()
    service = _service(tmp_path, store, _routing(default, model_a, thread_a))
    for _ in range(_TRANSIENT):
        service.run(reason="admin")
    default.down = True

    failed = service.run(reason="admin")

    assert (failed.status, failed.failure_code, failed.model) == ("failed", "CURATOR_MODEL_FAILED", "model-d"), "记在默认模型名下"
    assert model_a.calls == _TRANSIENT
    assert service.state_store.load().last_failure_code == "CURATOR_MODEL_FAILED", "照旧退避，不是熔断"
    assert "curator_thread_model_failed:CURATOR_MODEL_FAILED:transient" in service.run_log.list()[-1].warnings
    default.down = False
    result = service.run(reason="admin")
    assert result.status == "succeeded" and model_a.calls == _TRANSIENT, "默认模型恢复后直接用它，不回头试本组模型"
    assert thread_a in service.state_store.load().per_thread_cursors


def test_another_groups_success_in_between_does_not_reset_the_transient_count(tmp_path):
    store, thread_a, thread_b, thread_of = _two_threads(tmp_path)
    default, model_a = _EchoBackend("default", thread_of), _BrokenBackend()
    service = _service(tmp_path, store, _routing(default, model_a, thread_a))
    service.run(reason="admin")
    # B 先来一条新消息、A 随后也来一条：这一轮 B 先跑并成功，A 再失败。
    thread_of[_append(store, thread_b, "B 的新消息", 100.0)] = thread_b
    thread_of[_append(store, thread_a, "A 的新消息", 101.0)] = thread_a
    service.run(reason="admin")
    assert [item.status for item in service.run_log.list()] == ["failed", "succeeded", "failed"]

    result = service.run(reason="admin")

    assert result.status == "succeeded" and model_a.calls == 2, "别组成功不清零本组的连续失败"
    assert thread_a in service.state_store.load().per_thread_cursors


def test_unreadable_run_log_keeps_trying_the_thread_model(tmp_path, monkeypatch):
    store, thread_a, _thread_b, thread_of = _two_threads(tmp_path)
    default, model_a = _EchoBackend("default", thread_of), _BrokenBackend()
    service = _service(tmp_path, store, _routing(default, model_a, thread_a))
    for _ in range(_TRANSIENT):
        service.run(reason="admin")

    def unreadable(count: int):
        raise OSError("run log unreadable")

    monkeypatch.setattr(service.run_log, "recent_finished", unreadable)
    result = service.run(reason="admin")
    assert (result.status, result.model) == ("failed", "model-a") and default.seen == [], "读不到账按没达到处理"
    assert model_a.calls == _TRANSIENT + 1, "照常试了会话自己的模型"


def test_default_group_failures_are_never_rerouted(tmp_path):
    store, _thread_a, _thread_b, _thread_of = _two_threads(tmp_path)
    broken = _BrokenBackend()
    routing = CuratorModelRouting(CuratorModelRoute("p-default", broken, "prov-d", "model-d", is_default=True))
    service = _service(tmp_path, store, routing)

    results = [service.run(reason="admin") for _ in range(_TRANSIENT + 1)]

    assert all((item.status, item.failure_code) == ("failed", "CURATOR_MODEL_FAILED") for item in results)
    assert broken.calls == _TRANSIENT + 1, "默认组没有别的模型可换，每次照常试、照常退避"
    assert not any(warning.startswith("curator_thread_model_failed") for row in service.run_log.list() for warning in row.warnings)


def test_transient_fallback_needs_consecutive_connection_failures_on_the_same_input():
    from types import SimpleNamespace

    view = group_cursor_view(("ta",), include_audit=False)
    same = {"per_thread_cursors": {"ta": "m1", "tb": "m5"}, "last_audit_event_id": ""}
    moved = {"per_thread_cursors": {"ta": "m2", "tb": "m5"}, "last_audit_event_id": ""}

    def failed(code: str, before: dict = same):
        return SimpleNamespace(status="failed", failure_code=code, cursor_before=before)

    other_b = {"per_thread_cursors": {"ta": "m1", "tb": "m9"}, "last_audit_event_id": ""}
    assert transient_fallback_code(same, [failed("CURATOR_MODEL_TIMEOUT"), failed("CURATOR_MODEL_FAILED", other_b)],
                                   view) == "CURATOR_MODEL_TIMEOUT", "超时也算；别的会话游标变了不影响本组"
    assert transient_fallback_code(same, [failed("CURATOR_MODEL_FAILED")], view) == "", "只失败一次不够"
    assert transient_fallback_code(same, [failed("CURATOR_MODEL_FAILED"), failed("CURATOR_SCHEMA_INVALID")], view) == "", \
        "确定性失败有自己的补跑和熔断"
    assert transient_fallback_code(same, [failed("CURATOR_MODEL_FAILED"), failed("CURATOR_MODEL_FAILED", moved)], view) == "", \
        "不是同一批输入"
    success = SimpleNamespace(status="succeeded", failure_code="", cursor_before=same, warnings=())
    assert transient_fallback_code(same, [success, failed("CURATOR_MODEL_FAILED"), failed("CURATOR_MODEL_FAILED")], view) == ""


def test_one_connection_failure_is_enough_right_after_a_transient_default_fallback():
    from types import SimpleNamespace

    view = group_cursor_view(("ta",), include_audit=False)
    after = {"per_thread_cursors": {"ta": "m2"}, "last_audit_event_id": ""}
    before = {"per_thread_cursors": {"ta": "m1"}, "last_audit_event_id": ""}

    def failed(code: str = "CURATOR_MODEL_FAILED", cursor: dict = after):
        return SimpleNamespace(status="failed", failure_code=code, cursor_before=cursor, warnings=())

    def advanced(*warnings: str):
        return SimpleNamespace(status="succeeded", failure_code="", cursor_before=before, warnings=tuple(warnings))

    fallback = advanced("dropped_evidence:x", transient_fallback_warning("CURATOR_MODEL_TIMEOUT"))
    assert transient_fallback_code(after, [failed(), fallback], view) == "CURATOR_MODEL_FAILED", "补跑推进后只失败 1 次就换"
    assert transient_fallback_code(after, [fallback], view) == "", "补跑推进后还没失败：先给会话自己的模型一次机会"
    assert transient_fallback_code(after, [failed(), advanced()], view) == "", "会话模型自己成功推进后要连续 2 次"
    assert transient_fallback_code(after, [failed(), advanced("curator_thread_model_failed:CURATOR_SCHEMA_INVALID")], view) == "", \
        "坏 JSON 的补跑不算连不上"
    assert transient_fallback_code(after, [failed("CURATOR_SCHEMA_INVALID"), fallback], view) == "", "失败得是连接类"
    assert transient_fallback_code(after, [failed(cursor=before), fallback], view) == "", "不是同一批输入"
    assert transient_fallback_code(after, [failed(), failed(), advanced()], view) == "CURATOR_MODEL_FAILED"


# 函数用途: 可切换“连不上”的回显后端，记总调用次数（会话自己的模型时好时坏的替身）。
class _SwitchableBackend(_FlakyBackend):
    def __init__(self, name: str, thread_of: dict[str, str]) -> None:
        super().__init__(name, thread_of)
        self.calls = 0

    def generate_structured(self, prompt: str, *, response_schema: dict[str, object]) -> ModelResponse:
        self.calls += 1
        return super().generate_structured(prompt, response_schema=response_schema)


def test_after_a_transient_fallback_the_thread_model_gets_one_chance_per_new_batch(tmp_path):
    store, thread_a, _thread_b, thread_of = _two_threads(tmp_path)
    default, model_a = _EchoBackend("default", thread_of), _SwitchableBackend("model-a", thread_of)
    model_a.down = True
    service = _service(tmp_path, store, _routing(default, model_a, thread_a))
    for _ in range(2):
        service.run(reason="admin")
    assert service.run(reason="admin").status == "succeeded" and model_a.calls == 2, "第一次照原规则连续 2 次后才换"

    thread_of[_append(store, thread_a, "A 的新消息 1", 100.0)] = thread_a
    once = service.run(reason="admin")
    assert (once.status, once.model, model_a.calls) == ("failed", "model-a", 3), "补跑推进后来新消息：先给会话模型一次机会"
    again = service.run(reason="admin")
    assert (again.status, again.model, model_a.calls) == ("succeeded", "model-d", 3), "只失败 1 次就直接换默认"
    assert transient_fallback_warning("CURATOR_MODEL_FAILED") in service.run_log.list()[-1].warnings
    assert service.state_store.load().per_thread_cursors[thread_a] != "", "游标推进"

    model_a.down = False
    thread_of[_append(store, thread_a, "A 的新消息 2", 110.0)] = thread_a
    recovered = service.run(reason="admin")
    assert (recovered.status, recovered.model, model_a.calls) == ("succeeded", "model-a", 4), "会话模型恢复，自己推进"
    model_a.down = True
    thread_of[_append(store, thread_a, "A 的新消息 3", 120.0)] = thread_a
    results = [service.run(reason="admin") for _ in range(3)]
    assert [(item.status, item.model) for item in results] == [
        ("failed", "model-a"), ("failed", "model-a"), ("succeeded", "model-d")], "恢复后阈值回到连续 2 次"
    assert model_a.calls == 6


# 函数用途: 造一条批内消息输入（纯函数用例用，不落盘）。
def _message(message_id: str, thread_id: str) -> CuratorMessageInput:
    return CuratorMessageInput(message_id, thread_id, "user", "internal", 1.0, "x", "x", "h", {})


# 函数用途: 造一条工具审计输入（纯函数用例用，不落盘）。
def _audit(event_id: str) -> CuratorAuditInput:
    return CuratorAuditInput(event_id, "tool_call", "2026-10-02T00:00:00Z", "succeeded", "op", "call", "read_file", True,
                             "", "", "", "", "", "", "", "h", "", "", "", 0, "")


def test_routing_wide_fallback_warning_reaches_the_run_record(tmp_path):
    store, _thread_a, _thread_b, thread_of = _two_threads(tmp_path)
    default = CuratorModelRoute("fixed-id", _EchoBackend("default", thread_of), "prov-d", "model-d", is_default=True)
    routing = CuratorModelRouting(default, warnings=("curator_profile_unavailable_fallback:profile_not_found",))
    service = _service(tmp_path, store, routing)

    result = service.run(reason="admin")

    assert (result.status, result.processed_messages) == ("succeeded", 2)
    [record] = service.run_log.list()
    assert record.warnings[0] == "curator_profile_unavailable_fallback:profile_not_found", "整批警告进运行记录且排最前"
    audit_only = route_batch(CuratorInputBatch(messages=(), audit_events=(_audit("e1"),)), routing)
    assert audit_only.warnings == routing.warnings, "只有审计事件的运行也带整批警告"


def test_audit_events_travel_only_with_the_default_group():
    default = CuratorModelRoute("p-default", object(), "prov-d", "model-d", is_default=True)
    other = CuratorModelRoute("p-a", object(), "prov-a", "model-a")
    routing = CuratorModelRouting(default, {"p-a": other}, {"ta": "p-a"}, {"tb": "profile_disabled"})
    batch = CuratorInputBatch(messages=(_message("m1", "ta"), _message("m2", "tb"), _message("m3", "ta")),
                              audit_events=(_audit("e1"),))

    routed = route_batch(batch, routing)

    assert routed.route is other and [item.message_id for item in routed.batch.messages] == ["m1", "m3"]
    assert routed.batch.audit_events == () and routed.include_audit is False
    assert routed.remaining_groups == 1, "默认组还有消息，审计事件跟它一起，不另算一组"
    later = route_batch(CuratorInputBatch(messages=(_message("m2", "tb"),), audit_events=(_audit("e1"),)), routing)
    assert later.route is default and later.batch.audit_events and later.include_audit
    assert later.warnings == ("curator_thread_model_fallback:profile_disabled:1",)
    only_audit = route_batch(CuratorInputBatch(messages=(_message("m1", "ta"),), audit_events=(_audit("e1"),)), routing)
    assert only_audit.remaining_groups == 1, "默认组没有消息但有审计事件，留到最后单独跑"
    audit_run = route_batch(CuratorInputBatch(messages=(), audit_events=(_audit("e1"),)), routing)
    assert audit_run.route is default and audit_run.remaining_groups == 0


def test_group_breaker_history_ignores_other_groups():
    view = group_cursor_view(("ta",), include_audit=False)
    same = {"per_thread_cursors": {"ta": "m1", "tb": "m5"}, "last_audit_event_id": ""}
    moved_b = {"per_thread_cursors": {"ta": "m1", "tb": "m6"}, "last_audit_event_id": ""}
    moved_a = {"per_thread_cursors": {"ta": "m2", "tb": "m6"}, "last_audit_event_id": ""}

    class Row:
        def __init__(self, status, model, before, after=None):
            self.status, self.provider, self.model = status, "p", model
            self.cursor_before, self.cursor_after = before, after or {}

    rows = [Row("succeeded", "model-d", same, moved_b), Row("failed", "model-d", same), Row("failed", "model-a", same)]
    assert group_breaker_history(rows, ("p", "model-a"), view) == [rows[2]], "别组的成功和别的模型的失败都不算"
    advanced = Row("succeeded", "model-d", same, moved_a)
    assert group_breaker_history([advanced], ("p", "model-a"), view) == [advanced], "推进了本组游标的成功会打断计数"


def test_combined_result_sums_counts_and_keeps_the_last_status():
    from agent_py_agent.agent.memory_store.curator_models import CuratorRunResult

    first = CuratorRunResult("r1", "succeeded", "admin", "p", "model-a", processed_messages=2, candidates=1, warnings=("w1",))
    last = CuratorRunResult("r2", "failed", "admin", "p", "model-d", processed_messages=0, failure_code="X")
    combined = combined_run_result([first, last])
    assert (combined.run_id, combined.status, combined.failure_code, combined.processed_messages) == ("r2", "failed", "X", 2)
    assert combined.warnings == ("w1", "curator_model_groups:2")
    assert combined_run_result([first]) is first


# ---- 组合根路由器：真实模型目录 + 真实会话存储 ----

# 函数用途: 在 Host（测试用最小宿主）上建两个档案（owner 默认选第一个）和三个会话。
@pytest.fixture
def owner(tmp_path):
    from agent_py_agent.agent.capability.config import CapabilityConfig
    from agent_py_agent.tests.test_model_profiles import Host, add

    host = Host(tmp_path / "config")
    host.capability_config = CapabilityConfig()
    host.conversation_store = ConversationStore(tmp_path / "conversations")
    from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation

    default_id, _ = add(host, model_name="MiniMax-M2.7")
    other_id, _ = add(host, model_name="deepseek-v4-flash", api_base="https://example.test/deepseek",
                      model_backend="openai_compatible")
    execute_model_profile_operation(host, "set_default", {"profile_id": default_id})
    threads = [host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice",
                                                               "channel": "chat", "channel_conversation_id": f"c{i}"})
               for i in range(3)]
    return host, default_id, other_id, [thread.thread_id for thread in threads]


# 函数用途: 直接改会话上的结构化模型选择（绕过校验，用来造“选了已删除档案”的会话）。
def _select(host, thread_id: str, profile_id: str) -> None:
    from dataclasses import replace

    def update(latest):
        revision = latest.model_selection_revision + 1
        return replace(latest, model_profile_id=profile_id, model_selection_revision=revision,
                       model_selection_source="explicit", model_selection_last_explicit_revision=revision)

    host.conversation_store.threads.update_atomic(thread_id, update)


def test_router_reads_each_threads_model_and_falls_back_with_a_reason(owner):
    from agent_py_agent.agent.core import _curator_model_router
    from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorConfig as Config
    from agent_py_agent.agent.settings.model_profiles import (
        model_profiles_path,
        read_model_profiles,
    )

    host, default_id, other_id, (unselected, chose_other, chose_missing) = owner
    assert read_model_profiles(model_profiles_path(host.home_paths))["selected"] == default_id
    _select(host, chose_other, other_id)
    _select(host, chose_missing, "00000000-0000-4000-8000-000000000000")
    before = {thread: host.conversation_store.threads.load(thread).model_profile_id for thread in (unselected, chose_other)}

    router = _curator_model_router(host, host.config, Config(model_profile=""))
    routing = router((unselected, chose_other, chose_missing))

    assert routing.default.group_key == default_id and routing.default.model == "MiniMax-M2.7"
    assert routing.route_for(chose_other).model == "deepseek-v4-flash"
    assert routing.route_for(unselected) is routing.default and unselected not in routing.fallback_reasons
    assert routing.route_for(chose_missing) is routing.default
    assert routing.fallback_reasons[chose_missing] == "profile_not_found"
    after = {thread: host.conversation_store.threads.load(thread).model_profile_id for thread in (unselected, chose_other)}
    assert after == before, "路由只读，不给没选模型的会话写默认值"


def test_a_fixed_curator_profile_disables_routing(owner):
    from agent_py_agent.agent.core import _curator_model_router
    from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorConfig as Config

    host, default_id, _other_id, _threads = owner
    assert _curator_model_router(host, host.config, Config(model_profile=default_id)) is None


# 函数用途: 在 owner 夹具的同一配置目录下建管理员，加两个模型：team-model 设为其他用户的初始模型，shared-extra 只共享。
def _admin_with_shared_models(tmp_path):
    from agent_py_agent.agent.settings.shared_model_catalog import (
        set_initial_profile,
        set_shared_profile,
    )
    from agent_py_agent.tests.test_model_profiles import add
    from agent_py_agent.tests.test_shared_model_catalog import admin_host

    admin = admin_host(tmp_path / "config")
    team_id, _ = add(admin, model_name="team-model")
    extra_id, _ = add(admin, model_name="shared-extra")
    private_id, _ = add(admin, model_name="admin-private")
    set_initial_profile(admin, team_id)
    set_shared_profile(admin, extra_id, True)
    return admin, team_id, extra_id, private_id


def test_router_resolves_admin_shared_models_in_the_owners_own_catalog(tmp_path, owner):
    from agent_py_agent.agent.core import _curator_model_router
    from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorConfig as Config
    from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation

    host, _default_id, _other_id, (unselected, chose_shared, chose_private) = owner
    _admin, _team_id, extra_id, private_id = _admin_with_shared_models(tmp_path)
    execute_model_profile_operation(host, "set_default", {"profile_id": "default"})
    _select(host, chose_shared, "shared:" + extra_id)
    _select(host, chose_private, "shared:" + private_id)

    routing = _curator_model_router(host, host.config, Config(model_profile=""))((unselected, chose_shared, chose_private))

    assert routing.default.model == "team-model", "普通用户的默认 = 管理员指定的初始模型"
    assert routing.route_for(chose_shared).model == "shared-extra", "管理员共享给它的模型能解析"
    assert routing.route_for(chose_private) is routing.default and routing.fallback_reasons[chose_private]
    assert routing.warnings == ("curator_default_model_source:admin_initial",)


def test_unresolvable_fixed_profile_falls_back_to_each_owners_default(tmp_path, owner):
    from agent_py_agent.agent.core import _build_memory_curator_backend, _curator_model_router
    from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorConfig as Config

    host, _default_id, _other_id, threads = owner
    admin, _team_id, _extra_id, private_id = _admin_with_shared_models(tmp_path)
    assert _curator_model_router(admin, admin.config, Config(model_profile=private_id)) is None, "能解析到的 owner 照常用指定档案"

    config = Config(model_profile=private_id)
    _backend, _provider, model = _build_memory_curator_backend(host, host.config, config)
    router = _curator_model_router(host, host.config, config)
    routing = router(tuple(threads))

    assert model == "MiniMax-M2.7" and routing.default.model == "MiniMax-M2.7", "改用该 owner 自己的默认模型"
    assert routing.warnings == ("curator_profile_unavailable_fallback:profile_not_found",)
    assert routing.routes == {} and routing.thread_groups == {}, "指定档案退回时不按会话分组"
    assert router(tuple(threads)).default.group_key == private_id


# 函数用途: 造一个自己没有任何模型档案的普通用户（model-less owner），带会话存储；可选部署配置不带模型。
def _model_less_owner(tmp_path, *, deployment_model: bool = True):
    from dataclasses import replace

    from agent_py_agent.agent.capability.config import CapabilityConfig
    from agent_py_agent.tests.test_model_profiles import Host

    host = Host(tmp_path / "config", owner="providers/feishu/users/ou_test")
    host.capability_config = CapabilityConfig()
    host.conversation_store = ConversationStore(tmp_path / "owner-conversations")
    if not deployment_model:
        host.config = replace(host.config, model_backend="openai_compatible", model_name="", api_base="")
    return host


def test_model_less_owner_uses_the_same_default_as_its_main_agent(tmp_path):
    from agent_py_agent.agent.core import _curator_model_router
    from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorConfig as Config
    from agent_py_agent.agent.settings.model_profiles import (
        model_profiles_path,
        selected_model_config,
    )

    host = _model_less_owner(tmp_path)
    assert not model_profiles_path(host.home_paths).exists(), "这个 owner 自己没有模型目录"
    router = _curator_model_router(host, host.config, Config(model_profile=""))

    deployment = router(())
    assert deployment.default.model == selected_model_config(host).model_name == "deployment-model"
    assert deployment.warnings == ("curator_default_model_source:deployment_default",)
    _admin, _team_id, _extra_id, _private_id = _admin_with_shared_models(tmp_path)
    initial = router(())
    assert initial.default.model == selected_model_config(host).model_name == "team-model", "管理员指定初始模型后跟着主代理换"
    assert initial.warnings == ("curator_default_model_source:admin_initial",)


def test_model_less_owner_without_any_default_fails_typed_and_records_the_source(tmp_path):
    from agent_py_agent.agent.backends.factory import get_backend
    from agent_py_agent.agent.core import _curator_model_router
    from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorConfig as Config
    from agent_py_agent.agent.settings.model_profiles import selected_model_config

    host = _model_less_owner(tmp_path, deployment_model=False)
    main_config = selected_model_config(host)
    assert get_backend(main_config.model_backend, main_config).name == "unconfigured", "主代理本身也没有模型"
    routing = _curator_model_router(host, host.config, Config(model_profile=""))(())
    assert routing.default.backend.name == "unconfigured", "记忆整理不越过主代理去找别的模型"
    store, _thread_a, _thread_b, _thread_of = _two_threads(tmp_path)
    service = _service(tmp_path, store, routing)

    result = service.run(reason="admin")

    assert (result.status, result.failure_code) == ("failed", "CURATOR_MODEL_NOT_CONFIGURED")
    [record] = service.run_log.list()
    assert "curator_default_model_source:deployment_default" in record.warnings, "失败记录写明默认模型来自部署配置"
    assert service.state_store.load().per_thread_cursors == {}
