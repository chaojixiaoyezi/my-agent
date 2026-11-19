from __future__ import annotations

"""记忆第一波 S3：Daily 经历进入 session_search 检索投影（删原件即隐藏、有界对账自愈），上一轮用户消息参与召回融合。"""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime import loop_support
from agent_py_agent.agent.capability.session_search_tool import SessionSearchTool
from agent_py_agent.agent.conversation.models import (
    PREVIOUS_USER_TURN_PREVIEW_MAX_CHARS,
    ConversationHistorySeed,
    MessageLogEntry,
    PreviousUserTurn,
)
from agent_py_agent.agent.gateway_parts import request_context, request_prompt
from agent_py_agent.agent.local_storage.store import LocalStore
from agent_py_agent.agent.memory_store.daily import DailyMemoryEvent, DailyMemoryStore
from agent_py_agent.agent.memory_store.daily_index import (
    DAILY_EVENT_SOURCE_TYPE,
    DailyEventIndex,
    daily_event_text,
    visible_daily_hits,
)
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory, MemoryRecord
from agent_py_agent.agent.memory_store.recall import runtime_long_term_scope
from agent_py_agent.agent.retrieval.lexical import reciprocal_rank_fusion
from agent_py_agent.tests.test_memory_curator_v2 import (
    _conversation,
    _service,
    _StaticStructuredBackend,
    _valid_output,
)

_DAY = "1970-01-01"


# 函数用途: 跑一次真实 Curator 提交（带决定与下一步），并把 Daily 检索投影接到同一本地检索库。
def _committed(tmp_path: Path):
    conversations, thread, message = _conversation(tmp_path)
    payload = _valid_output(thread.thread_id, message.message_id, message.content)
    payload["daily_events"][0]["decisions"] = ["个人电脑统一改用 uv 管理依赖"]
    payload["daily_events"][0]["next_actions"] = ["下周把 CI 也迁到 uv"]
    service = _service(tmp_path, _StaticStructuredBackend(payload), conversations)
    store = LocalStore(tmp_path / "local" / "local.db")
    service.daily_index = DailyEventIndex(store, tmp_path / "memory" / "daily")
    result = service.run(reason="admin")
    assert result.status == "succeeded"
    return service, store, thread, result


# 函数用途: 构造只带本地检索库与 owner Daily 目录的 session_search 工具。
def _tool(tmp_path: Path, store: LocalStore) -> SessionSearchTool:
    home = SimpleNamespace(owner_memory_daily_dir=tmp_path / "memory" / "daily")
    return SessionSearchTool(SimpleNamespace(local_store=store, home_paths=home))


# 函数用途: 执行一次工具调用并返回成功载荷。
def _call(tool: SessionSearchTool, **params) -> dict:
    result = tool.execute(params)
    assert result.ok, result.output
    return json.loads(result.output)


# 函数用途: 返回结果里所有 daily_event 条目。
def _daily_rows(payload: dict) -> list[dict]:
    rows = payload.get("results") or payload.get("messages") or []
    return [row for row in rows if row.get("source_type") == DAILY_EVENT_SOURCE_TYPE]


# ---------- OL-5：提交后可检索，重放不空转 ----------


def test_committed_daily_event_reaches_search_recent_and_around(tmp_path):
    service, store, thread, _result = _committed(tmp_path)
    tool = _tool(tmp_path, store)
    [event] = service.daily_store.list(day=_DAY)

    found = _daily_rows(_call(tool, mode="search", query="uv"))
    recent = _daily_rows(_call(tool, mode="recent"))
    around = _call(tool, mode="around", around_id=found[0]["around_id"])

    assert [row["source_id"] for row in found] == [f"{_DAY}:{event.event_id}"]
    assert found[0]["title"] == "conversation"
    assert found[0]["scope"]["thread_id"] == thread.thread_id
    assert [row["id"] for row in recent] == [found[0]["around_id"]]
    anchor = next(row for row in around["messages"] if row.get("anchor"))
    assert anchor["content"] == "用户说明个人电脑使用 macOS。\n个人电脑统一改用 uv 管理依赖\n下周把 CI 也迁到 uv"
    record = store.get_record(found[0]["around_id"])
    assert record.metadata["event_id"] == event.event_id and record.metadata["date"] == _DAY
    assert record.metadata["refs"]["message_refs"][0]["message_id"] == event.message_refs[0]["message_id"]


def test_replaying_the_same_committed_event_writes_nothing(tmp_path):
    service, store, _thread, _result = _committed(tmp_path)
    events_before = store.stats()["event_count"]

    report = service.daily_index.index(service.daily_store.list(day=_DAY))

    assert report == {"indexed": 0, "unchanged": 1, "failed": 0}
    assert store.stats()["event_count"] == events_before


def test_index_write_failure_keeps_the_commit_and_is_repaired_from_the_daily_original(tmp_path, monkeypatch):
    conversations, thread, message = _conversation(tmp_path)
    service = _service(tmp_path, _StaticStructuredBackend(
        _valid_output(thread.thread_id, message.message_id, message.content)), conversations)
    store = LocalStore(tmp_path / "local" / "local.db")
    service.daily_index = DailyEventIndex(store, tmp_path / "memory" / "daily")
    original = store.upsert_record

    def broken(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, "upsert_record", broken)
    result = service.run(reason="admin")

    assert result.status == "succeeded"
    assert "daily_event_index_failed:1" in result.warnings
    assert service.state_store.load().per_thread_cursors[thread.thread_id] == message.message_id
    assert store.count_records(source_type=DAILY_EVENT_SOURCE_TYPE) == 0

    monkeypatch.setattr(store, "upsert_record", original)
    report = service.daily_index.maintain()

    assert report["backfilled"] == 1
    assert _daily_rows(_call(_tool(tmp_path, store), mode="search", query="macOS"))


# ---------- OL-5：手动删改 Daily 原件后立即不可见，维护有界清理/补建 ----------


def test_deleted_daily_shard_hides_rows_at_once_and_maintenance_removes_them(tmp_path):
    service, store, _thread, _result = _committed(tmp_path)
    tool = _tool(tmp_path, store)
    [hit] = _daily_rows(_call(tool, mode="search", query="uv"))
    content_file = store.files_dir / f"{hit['around_id']}.txt"
    assert content_file.exists()

    (tmp_path / "memory" / "daily" / f"{_DAY}.jsonl").unlink()

    assert _daily_rows(_call(tool, mode="search", query="uv")) == []
    assert _daily_rows(_call(tool, mode="recent")) == []
    around = _call(tool, mode="around", around_id=hit["around_id"])
    assert around["messages"] == [] and "Daily 原记录已删除" in around["hint"]

    report = service.daily_index.maintain()

    assert report["deleted"] == 1 and report["unreadable"] == 0
    assert store.count_records(source_type=DAILY_EVENT_SOURCE_TYPE) == 0
    assert store.search("uv", limit=5) == []
    assert not content_file.exists()


def test_rewritten_daily_shard_hides_the_stale_row_and_backfills_the_new_event(tmp_path):
    service, store, _thread, _result = _committed(tmp_path)
    tool = _tool(tmp_path, store)
    shard = tmp_path / "memory" / "daily" / f"{_DAY}.jsonl"
    shard.unlink()
    DailyMemoryStore(tmp_path / "memory" / "daily").append(DailyMemoryEvent(
        event_type="decision", summary="改为每周五统一发布。", actor="user", origin="user_explicit",
        created_at=f"{_DAY}T08:00:00+00:00"))

    assert _daily_rows(_call(tool, mode="search", query="uv")) == []
    assert _daily_rows(_call(tool, mode="search", query="发布")) == []

    report = service.daily_index.maintain()

    assert report["deleted"] == 1 and report["backfilled"] == 1
    [row] = _daily_rows(_call(tool, mode="search", query="发布"))
    assert row["title"] == "decision"


def test_deleted_local_store_is_rebuilt_from_daily_originals(tmp_path):
    service, store, _thread, _result = _committed(tmp_path)
    for path in (store.db_path, *store.db_path.parent.glob(store.db_path.name + "-*")):
        path.unlink(missing_ok=True)
    rebuilt = LocalStore(tmp_path / "local" / "local.db")
    index = DailyEventIndex(rebuilt, tmp_path / "memory" / "daily")

    report = index.maintain()

    assert report["backfilled"] == 1 and report["backfill_complete"] is True
    assert [row["title"] for row in _daily_rows(_call(_tool(tmp_path, rebuilt), mode="search", query="uv"))] == [
        "conversation"]


def test_unreadable_daily_shard_hides_rows_but_maintenance_does_not_delete_them(tmp_path):
    service, store, _thread, _result = _committed(tmp_path)
    shard = tmp_path / "memory" / "daily" / f"{_DAY}.jsonl"
    shard.write_text(shard.read_text(encoding="utf-8") + "{broken\n", encoding="utf-8")

    assert _daily_rows(_call(_tool(tmp_path, store), mode="search", query="uv")) == []
    report = service.daily_index.maintain()

    assert report["deleted"] == 0 and report["unreadable"] >= 1
    assert store.count_records(source_type=DAILY_EVENT_SOURCE_TYPE) == 1


def test_symlinked_daily_shard_and_missing_daily_dir_fail_closed(tmp_path):
    service, store, _thread, _result = _committed(tmp_path)
    hits = store.search("uv", limit=5, source_type=DAILY_EVENT_SOURCE_TYPE)
    shard = tmp_path / "memory" / "daily" / f"{_DAY}.jsonl"
    moved = tmp_path / "elsewhere.jsonl"
    os.replace(shard, moved)
    shard.symlink_to(moved)

    assert visible_daily_hits(tmp_path / "memory" / "daily", hits) == []
    assert visible_daily_hits(None, hits) == []
    non_daily = SimpleNamespace(source_type="memory", metadata={}, source_id="m1", title="t", content="c")
    assert visible_daily_hits(None, [non_daily]) == [non_daily]


def test_curator_maintenance_tick_runs_the_bounded_daily_reconciliation(tmp_path):
    service, store, _thread, _result = _committed(tmp_path)
    (tmp_path / "memory" / "daily" / f"{_DAY}.jsonl").unlink()

    report = service.run_maintenance_if_due()

    assert report["daily_index"]["deleted"] == 1
    assert "daily_index" not in report.get("errors", {})
    assert store.count_records(source_type=DAILY_EVENT_SOURCE_TYPE) == 0


def test_maintenance_pages_are_bounded_and_resume_from_the_cursor(tmp_path):
    store = LocalStore(tmp_path / "local.db")
    daily = DailyMemoryStore(tmp_path / "daily")
    events = [daily.append(DailyMemoryEvent(event_type="todo", summary=f"第 {index} 件待办", actor="user",
                                            origin="user_explicit", created_at=f"1970-01-0{index}T00:00:00+00:00"))
              for index in range(1, 6)]
    index = DailyEventIndex(store, tmp_path / "daily")

    reports = [index.maintain(max_rows=2, max_shards=2) for _ in range(4)]

    assert [report["shards"] for report in reports[:3]] == [2, 2, 1]
    assert [report["backfill_complete"] for report in reports[:3]] == [False, False, True]
    assert [report["checked"] for report in reports] == [0, 2, 2, 1]
    assert reports[-1]["prune_complete"] is True
    assert store.count_records(source_type=DAILY_EVENT_SOURCE_TYPE) == len(events)
    assert sum(report["backfilled"] for report in reports) == len(events)


def test_simple_agent_wires_the_daily_index_to_the_owner_local_store(tmp_path):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.config import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    index = agent.memory_curator.daily_index

    assert isinstance(index, DailyEventIndex)
    assert index.local_store is agent.local_store
    assert index.daily_dir == Path(agent.home_paths.owner_memory_daily_dir)


def test_daily_event_text_keeps_summary_decisions_and_next_actions_in_order():
    event = DailyMemoryEvent(event_type="decision", summary="定方案", actor="user",
                             decisions=("用 uv",), next_actions=("迁 CI",))
    assert daily_event_text(event) == "定方案\n用 uv\n迁 CI"


# ---------- OL-7：上一条用户消息参与召回融合 ----------


def test_weighted_rrf_keeps_unweighted_default_and_prefers_the_heavier_ranking():
    rankings = [["a", "b"], ["c", "a"]]

    assert reciprocal_rank_fusion(rankings) == reciprocal_rank_fusion(rankings, weights=(1.0, 1.0))
    weighted = [item for item, _score in reciprocal_rank_fusion([["b"], ["c"]], weights=(1.0, 0.5))]
    assert weighted == ["b", "c"]
    assert [item for item, _ in reciprocal_rank_fusion(rankings, weights=(1.0, 0.5))][0] == "a"
    with pytest.raises(ValueError):
        reciprocal_rank_fusion(rankings, weights=(1.0,))
    with pytest.raises(ValueError):
        reciprocal_rank_fusion(rankings, weights=(1.0, 0.0))


# LLM: 词面臂之外的任何嵌入调用都算违规；第二路必须不碰向量臂。
# 类用途: 一被调用就让测试失败的嵌入端。
class _ForbiddenEmbedder:
    def embed(self, texts):
        raise AssertionError(f"embedding must not run: {texts!r}")


# 函数用途: 写一条带 project 范围的正式事实。
def _project_fact(entry_id: str, content: str, project: str) -> MemoryRecord:
    return MemoryRecord(role="user", content=content, kind="fact", entry_id=entry_id, created_at=10.0,
                        updated_at=10.0, attributes={"origin": "user_explicit", "scope_type": "project",
                                                     "scope_key": f"project:{project}", "subject_key": entry_id})


# 函数用途: 组装 moneywise/博客两个项目的端口与时区事实。
def _ellipsis_memory(tmp_path) -> JsonlMemory:
    memory = JsonlMemory(tmp_path / "memory.jsonl", embedder=_ForbiddenEmbedder())
    for record in (
        _project_fact("port-blog", "博客项目的数据库端口是 5432，数据库端口只开内网。", "blog"),
        _project_fact("port-moneywise", "moneywise 项目的数据库端口是 5433。", "moneywise"),
        _project_fact("tz-moneywise", "moneywise 的时间统一存 UTC 时区。", "moneywise"),
    ):
        memory.add_record(record)
    return memory


# 函数用途: 构造带上一条用户消息冻结事实的运行参数、请求与 agent。
def _turn_context(memory, *, preview="moneywise 的时间存什么时区？", owner_kind="personal", actor=None,
                  turn_actor="", turn_request="req-2", prompt="那它数据库端口呢？"):
    turn = PreviousUserTurn(preview=preview, owner_id="local/main", thread_id="t1", request_id=turn_request,
                            actor_id=turn_actor, message_id="m1")
    params = SimpleNamespace(conversation_history_seed=ConversationHistorySeed(previous_user_turn=turn),
                             host_channel_actor=actor)
    request = SimpleNamespace(request_id="req-2", user_prompt=prompt)
    agent = SimpleNamespace(memory=memory, home_paths=SimpleNamespace(owner_id="local/main", owner_kind=owner_kind))
    return agent, request, params


# 函数用途: 跑一次“当前轮候选 + 上一轮融合”，返回入选条目 ID。
def _fused_ids(memory, agent, request, params, top_k=2) -> list[str]:
    scope = runtime_long_term_scope(memory)
    current = memory.search_scoped_lexical_candidates(
        request.user_prompt, top_k, loop_support.runtime_recall_predicate(scope, request.user_prompt))
    routed = SimpleNamespace(findings=[])
    fused = loop_support._fuse_previous_turn_recall(agent, request, params, scope, current, top_k, routed)
    return [record.entry_id for record in fused]


def test_follow_up_turn_fuses_the_previous_user_message_and_never_embeds(tmp_path):
    memory = _ellipsis_memory(tmp_path)
    agent, request, params = _turn_context(memory)
    scope = runtime_long_term_scope(memory)
    current_only = [record.entry_id for record in memory.search_scoped_lexical_candidates(
        request.user_prompt, 2, loop_support.runtime_recall_predicate(scope, request.user_prompt))]

    fused = _fused_ids(memory, agent, request, params)

    assert current_only[0] == "port-blog"
    assert fused[0] == "port-moneywise" and len(fused) == 2
    assert memory._pending_access == {}


@pytest.mark.parametrize("variant", ["group", "actor", "request", "echo", "empty"])
def test_previous_turn_is_skipped_without_matching_identity_or_new_text(tmp_path, variant):
    memory = _ellipsis_memory(tmp_path)
    options = {
        "group": {"owner_kind": "group"},
        "actor": {"actor": SimpleNamespace(actor_id="ou_member_b"), "turn_actor": "ou_member_a"},
        "request": {"turn_request": "req-1"},
        "echo": {"preview": "那它数据库端口呢？"},
        "empty": {"preview": "  "},
    }[variant]
    agent, request, params = _turn_context(memory, **options)
    scope = runtime_long_term_scope(memory)
    current = memory.search_scoped_lexical_candidates(
        request.user_prompt, 2, loop_support.runtime_recall_predicate(scope, request.user_prompt))

    fused = loop_support._fuse_previous_turn_recall(agent, request, params, scope, current, 2,
                                                    SimpleNamespace(findings=[]))

    assert fused is current


def test_first_turn_and_memory_without_lexical_search_keep_current_results(tmp_path):
    memory = _ellipsis_memory(tmp_path)
    agent, request, _params = _turn_context(memory)
    first_turn = SimpleNamespace(conversation_history_seed=ConversationHistorySeed(), host_channel_actor=None)
    current = [MemoryRecord(role="user", content="x", entry_id="only")]
    routed = SimpleNamespace(findings=[])

    assert loop_support._fuse_previous_turn_recall(agent, request, first_turn, None, current, 2, routed) is current
    bare = SimpleNamespace(memory=SimpleNamespace(), home_paths=agent.home_paths)
    _agent, _request, params = _turn_context(memory)
    assert loop_support._fuse_previous_turn_recall(bare, request, params, None, current, 2, routed) is current


# 函数用途: 生成一条带 Gateway 请求编号的会话行。
def _row(message_id: str, role: str, content: str, request_id: str) -> MessageLogEntry:
    return MessageLogEntry(message_id=message_id, thread_id="t1", role=role, content=content,
                           metadata={"gateway_request_id": request_id})


# LLM: 统计序列被完整遍历的次数，证明观察者与原选择规则共用同一遍读取。
# 类用途: 只读行序列，记录 __iter__ 调用次数。
class _CountingRows(tuple):
    iterations = 0

    def __iter__(self):
        type(self).iterations += 1
        return super().__iter__()


def test_history_source_observer_reads_rows_once_and_keeps_the_selection(tmp_path):
    long_text = "前一轮 " + "很长的说明 " * 200
    rows = _CountingRows((
        _row("m1", "user", "moneywise 的时间存什么时区？", "req-1"),
        _row("m2", "assistant", "存 UTC。", "req-1"),
        _row("m3", "user", long_text, "req-1b"),
        _row("m4", "assistant", "好的。", "req-1b"),
        _row("m5", "user", "那它数据库端口呢？", "req-2"),
    ))
    plain = request_context._gateway_history_source(tuple(rows), "req-2", None)
    observer = request_context._PreviousUserTurnObserver()
    _CountingRows.iterations = 0

    observed = request_context._gateway_history_source(rows, "req-2", None, observer=observer)

    assert _CountingRows.iterations == 1
    assert [row.message_id for row in observed.rows] == [row.message_id for row in plain.rows] == [
        "m1", "m2", "m3", "m4"]
    assert observer.message_id == "m3"
    assert len(observer.preview) == PREVIOUS_USER_TURN_PREVIEW_MAX_CHARS
    assert observer.preview == " ".join(long_text.split())[:PREVIOUS_USER_TURN_PREVIEW_MAX_CHARS]


def test_frozen_previous_turn_carries_identity_and_skips_group_and_system_turns():
    observer = request_context._PreviousUserTurnObserver()
    observer.preview, observer.message_id = "上一条", "m3"
    personal = SimpleNamespace(home_paths=SimpleNamespace(owner_id="feishu/ou_a", owner_kind="personal"))
    group = SimpleNamespace(home_paths=SimpleNamespace(owner_id="feishu/oc_g", owner_kind="group"))
    im_scope = SimpleNamespace(channel="feishu", channel_user_id="ou_a")

    turn = observer.freeze(personal, "t1", {}, "req-2", im_scope)

    assert turn == PreviousUserTurn(preview="上一条", owner_id="feishu/ou_a", thread_id="t1", request_id="req-2",
                                    actor_id="ou_a", message_id="m3")
    assert observer.freeze(group, "t1", {}, "req-2", im_scope) is None
    assert observer.freeze(personal, "t1", {"system_task": {"kind": "x"}}, "req-2", im_scope) is None
    assert observer.freeze(personal, "t1", {"source": "plugin"}, "req-2", im_scope) is None
    local = observer.freeze(personal, "t1", {}, "req-2", SimpleNamespace(channel="local", channel_user_id="local-cli"))
    assert local is not None and local.actor_id == ""


def test_seed_carries_the_previous_turn_without_changing_seed_equality():
    turn = PreviousUserTurn(preview="上一条", owner_id="o", thread_id="t1", request_id="req-2")
    source = request_context._gateway_history_source((_row("m1", "user", "上一条", "req-1"),), "req-2", None)
    context = request_context.GatewayConversationContext(thread_id="t1", history_source=source,
                                                         previous_user_turn=turn)

    seed = request_prompt.gateway_conversation_history_seed(context)

    assert seed.previous_user_turn is turn
    assert seed == ConversationHistorySeed(source=source)
