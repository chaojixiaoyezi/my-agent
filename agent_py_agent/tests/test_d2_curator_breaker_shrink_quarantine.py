"""D-2：同一输入熔断后，重放逐次缩成更短前缀；缩到下限仍解析失败只隔离队首一条，新输入与日终收尾不再被永久挡住。"""
from __future__ import annotations

import json
import re

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.memory_store.curator_models import (
    CURATOR_OUTPUT_SCHEMA_VERSION,
    CURATOR_REPLAY_BREAKER_FAILURE_COUNT,
    CURATOR_REPLAY_BREAKER_OPEN,
)
from agent_py_agent.tests.test_curator_observation_identity import _service

_MANIFEST = re.compile(r'\{"audit_event_ids": \[[^\]]*\], "message_ids": \[[^\]]*\]\}')


# 类用途: 按提示里的身份清单作答；fails(本批消息与审计身份) 为真时返回非 JSON，error 存在时直接抛出，否则声明处理本批全部输入。
class _ShapeBackend:
    name = "shape-curator"

    def __init__(self, thread_id: str, fails) -> None:
        self.thread_id, self.fails, self.error, self.seen = thread_id, fails, None, []

    def generate_structured(self, prompt: str, *, response_schema: dict[str, object]) -> ModelResponse:
        del response_schema
        manifest = json.loads(_MANIFEST.search(prompt).group(0))
        ids, events = manifest["message_ids"], manifest["audit_event_ids"]
        self.seen.append([*ids, *events])
        if self.error is not None:
            raise self.error
        if self.fails(self.seen[-1]):
            return ModelResponse(text="这不是 JSON", backend=self.name)
        return ModelResponse(text=json.dumps({
            "schema_version": CURATOR_OUTPUT_SCHEMA_VERSION, "daily_events": [], "candidates": [],
            "processed_message_refs": [{"message_id": item} for item in ids],
            "processed_audit_refs": [{"event_id": item} for item in events], "unresolved_refs": [], "warnings": [],
            "next_cursor": {"per_thread_cursors": [{"thread_id": self.thread_id, "message_id": item} for item in ids[-1:]],
                            "last_audit_event_id": events[-1] if events else None}}, ensure_ascii=False), backend=self.name)


# 函数用途: 造一个真实会话及按顺序追加的 count 条用户消息。
def _thread(tmp_path, count: int):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "user-1", "channel": "internal",
                                          "channel_conversation_id": "chat-1", "channel_user_id": "user-1", "now": 10.0})
    messages = [store.messages.append({"thread_id": thread.thread_id, "role": "user", "channel": "internal",
                                       "content": f"第 {index} 条：我喜欢绿茶。", "now": 11.0 + index,
                                       "metadata": {"session_id": "session-1", "request_id": f"request-{index}"}})
                for index in range(count)]
    return store, thread, messages


# 函数用途: 在同一起始游标上连续解析失败，直到熔断打开。
def _trip(service) -> None:
    for _ in range(CURATOR_REPLAY_BREAKER_FAILURE_COUNT):
        assert service.run(reason="admin").failure_code == "CURATOR_SCHEMA_INVALID"
    assert service.state_store.load().last_failure_code == CURATOR_REPLAY_BREAKER_OPEN


def test_breaker_replay_shrinks_to_a_committable_prefix_without_losing_the_tail(tmp_path):
    store, thread, messages = _thread(tmp_path, 4)
    backend = _ShapeBackend(thread.thread_id, lambda ids: len(ids) > 2)
    service = _service(tmp_path, backend, store)
    _trip(service)
    assert all(len(ids) == 4 for ids in backend.seen), "熔断前仍按原合同重放整批"

    shrunk = service.run(reason="admin")
    assert (shrunk.status, shrunk.processed_messages) == ("succeeded", 2)
    assert "curator_replay_breaker_shrunk:messages=4->2,audit=0->0" in shrunk.warnings
    assert service.state_store.load().per_thread_cursors == {thread.thread_id: messages[1].message_id}

    rest = service.run(reason="admin")
    assert (rest.status, rest.processed_messages) == ("succeeded", 2), "舍弃的尾部留在原游标后，下一轮照常处理"
    assert service.state_store.load().per_thread_cursors == {thread.thread_id: messages[3].message_id}
    assert not any(item.startswith("curator_replay_breaker_quarantined")
                   for record in service.run_log.list() for item in record.warnings)


def test_floor_input_that_still_fails_is_quarantined_and_newer_input_resumes(tmp_path):
    store, thread, messages = _thread(tmp_path, 3)
    poison = messages[0].message_id
    backend = _ShapeBackend(thread.thread_id, lambda ids: poison in ids)
    service = _service(tmp_path, backend, store)
    _trip(service)

    # 第一次熔断重放 3->2 仍含坏输入、未到下限：照常失败，不隔离也不推进游标。
    first = service.run(reason="admin")
    assert first.failure_code == "CURATOR_SCHEMA_INVALID"
    state = service.state_store.load()
    assert state.last_failure_code == CURATOR_REPLAY_BREAKER_OPEN and not state.per_thread_cursors
    assert "curator_replay_breaker_shrunk:messages=3->2,audit=0->0" in service.run_log.list()[-1].warnings

    # 再缩到下限只剩坏输入仍失败：只按跳过游标隔离这一条，运行账留准确身份，原消息仍在会话里。
    quarantined = service.run(reason="admin")
    assert (quarantined.status, quarantined.processed_messages) == ("succeeded", 0)
    assert backend.seen[-1] == [poison]
    assert service.state_store.load().per_thread_cursors == {thread.thread_id: poison}
    assert f"curator_replay_breaker_quarantined:message={thread.thread_id}/{poison}" in service.run_log.list()[-1].warnings
    rows, errors = store.messages.recent_report(thread.thread_id, limit=0)
    assert not errors and [row.message_id for row in rows] == [item.message_id for item in messages]

    resumed = service.run(reason="admin")
    assert (resumed.status, resumed.processed_messages) == ("succeeded", 2)
    assert backend.seen[-1] == [item.message_id for item in messages[1:]]
    assert service.state_store.load().per_thread_cursors == {thread.thread_id: messages[2].message_id}


def test_floor_transient_failure_is_never_quarantined(tmp_path):
    store, thread, messages = _thread(tmp_path, 1)
    backend = _ShapeBackend(thread.thread_id, lambda ids: True)
    service = _service(tmp_path, backend, store)
    _trip(service)
    backend.error = ConnectionResetError("连接被重置")

    result = service.run(reason="admin")

    assert result.failure_code == "CURATOR_MODEL_FAILED"
    assert not service.state_store.load().per_thread_cursors, "网络类失败不能当作坏输入跳过"
    assert not any(item.startswith("curator_replay_breaker_quarantined")
                   for record in service.run_log.list() for item in record.warnings)


def test_floor_audit_event_that_still_fails_is_quarantined_by_event_id(tmp_path):
    store, thread, _messages = _thread(tmp_path, 0)
    audit = tmp_path / "audit"
    audit.mkdir()
    rows = [{"event_id": event_id, "action": "tool_call", "created_at": "1970-01-01T00:00:12+00:00", "status": "ok",
             "tool_name": "read_file", "tool_call_id": f"call-{event_id}", "tool_success": True,
             "effect_outcome": "confirmed", "operation_id": f"operation-{event_id}", "run_id": "run-1",
             "task_id": "task-1", "request_id": "request-1", "thread_id": thread.thread_id,
             "content_preview": f"Completed read {event_id}."} for event_id in ("event-poison", "event-good")]
    (audit / "1970-01-01.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    backend = _ShapeBackend(thread.thread_id, lambda ids: "event-poison" in ids)
    service = _service(tmp_path, backend, store)
    _trip(service)

    # 两条审计缩一次就到下限：仍失败时只跳过队首这一条，审计游标按既有跳过游标落到它上面。
    quarantined = service.run(reason="admin")
    assert (quarantined.status, quarantined.processed_audit_events) == ("succeeded", 0)
    assert backend.seen[-1] == ["event-poison"]
    assert service.state_store.load().last_processed_audit_event_id == "event-poison"
    assert "curator_replay_breaker_quarantined:audit=event-poison" in service.run_log.list()[-1].warnings

    resumed = service.run(reason="admin")
    assert (resumed.status, resumed.processed_audit_events) == ("succeeded", 1)
    assert service.state_store.load().last_processed_audit_event_id == "event-good"
