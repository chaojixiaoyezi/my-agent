"""R5A-W2：增量输入读取失败（CURATOR_INPUT_*）在同一游标上连续重放也进熔断，不再每个维护周期整批重采。"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.curator import (
    MemoryCuratorDependencies,
    MemoryCuratorIdentity,
    MemoryCuratorService,
)
from agent_py_agent.agent.memory_store.curator_models import (
    CURATOR_REPLAY_BREAKER_FAILURE_COUNT,
    CURATOR_REPLAY_BREAKER_OPEN,
    CURATOR_REPLAY_BREAKER_RETRY_SECONDS,
    MemoryCuratorConfig,
    curator_replay_breaker_tripped,
)
from agent_py_agent.agent.memory_store.curator_run_log import CuratorRunLog
from agent_py_agent.agent.memory_store.curator_state import MemoryCuratorStateStore
from agent_py_agent.agent.memory_store.daily import DailyMemoryStore


# 类用途: 输入读取失败时不应被调用的模型；被调用即记数。
class _NeverCalledBackend:
    name = "never-called"

    def __init__(self) -> None:
        self.calls = 0

    def generate_structured(self, prompt: str, *, response_schema: dict[str, object]) -> ModelResponse:
        del prompt, response_schema
        self.calls += 1
        return ModelResponse(text="{}", backend=self.name)


# 函数用途: 造一条真实会话消息与一个损坏的审计账（整行不是 JSON），使增量输入读取确定性失败。
def _service_with_unreadable_audit(tmp_path: Path, backend: object) -> MemoryCuratorService:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "user-1", "channel": "internal",
                                          "channel_conversation_id": "chat-1", "channel_user_id": "user-1", "now": 10.0})
    store.messages.append({"thread_id": thread.thread_id, "role": "user", "channel": "internal",
                           "content": "我对花生过敏。", "now": 11.0,
                           "metadata": {"session_id": "session-1", "request_id": "request-1"}})
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    (audit_dir / "audit.jsonl").write_text('{"event_id": "raw:1", "event": "tool"}\n{坏行\n', encoding="utf-8")
    config = MemoryCuratorConfig(interval_seconds=60, turn_threshold=1, batch_message_limit=80, max_input_chars=40000,
                                 timeout_seconds=1, max_retries=0, daily_finalize_hour=23)
    return MemoryCuratorService(config=config, dependencies=MemoryCuratorDependencies(
        backend=backend, conversation_store=store, audit_dir=audit_dir,
        state_store=MemoryCuratorStateStore(tmp_path / "memory" / "curator" / "state.json"),
        daily_store=DailyMemoryStore(tmp_path / "memory" / "daily"),
        candidate_service=CandidateService(tmp_path / "memory" / "candidates.jsonl"),
        run_log=CuratorRunLog(tmp_path / "memory" / "curator" / "runs"),
        identity=MemoryCuratorIdentity(provider="never-called", model="r5a-w2")))


# 函数用途: 同一游标连续输入读取失败到上限后，state 记熔断码、一小时内不再重放，运行账保留真实失败码。
def test_unreadable_input_on_the_same_cursor_trips_the_replay_breaker(tmp_path):
    backend = _NeverCalledBackend()
    service = _service_with_unreadable_audit(tmp_path, backend)
    results = [service.run(reason="admin") for _ in range(CURATOR_REPLAY_BREAKER_FAILURE_COUNT)]
    codes = [result.failure_code for result in results]
    assert all(code.startswith("CURATOR_INPUT_") for code in codes), codes
    assert backend.calls == 0, "输入读取失败不得调用模型"
    state = service.state_store.load()
    assert state.last_failure_code == CURATOR_REPLAY_BREAKER_OPEN, "同一游标的输入读取失败必须熔断"
    assert f"curator_replay_breaker_open:{codes[-1]}" in service.run_log.list()[-1].warnings
    soon = datetime.fromisoformat(state.last_failure_at) + timedelta(seconds=CURATOR_REPLAY_BREAKER_RETRY_SECONDS - 60)
    assert service._run_pending_when_due(state, "admin", soon).status == "not_due", "熔断后一小时内不再整批重采"


# 函数用途: 规则层：输入读取失败码族计入确定性失败；网络类失败仍不计入，游标变化仍视为另一批输入。
def test_the_rule_counts_input_failures_but_not_transient_ones():
    cursor = {"per_thread_cursors": {"t": "m1"}, "last_audit_event_id": "raw:1"}

    def row(code: str, at: dict = cursor) -> SimpleNamespace:
        return SimpleNamespace(status="failed", failure_code=code, cursor_before=at)

    same = [row("CURATOR_INPUT_READ_FAILED"), row("CURATOR_INPUT_READ_FAILED")]
    assert curator_replay_breaker_tripped("CURATOR_INPUT_READ_FAILED", cursor, same)
    assert curator_replay_breaker_tripped("CURATOR_INPUT_TOOL_REFERENCE_READ_FAILED", cursor, same)
    assert not curator_replay_breaker_tripped("CURATOR_MODEL_FAILED", cursor, same)
    moved = [row("CURATOR_INPUT_READ_FAILED"), row("CURATOR_INPUT_READ_FAILED", {"per_thread_cursors": {}, "last_audit_event_id": ""})]
    assert not curator_replay_breaker_tripped("CURATOR_INPUT_READ_FAILED", cursor, moved)
