"""watch_stream 工具:open/pull/status/close/list、跨进程复活、出站闸、审计面隔离。"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent.ingestion import watch_state as ws
from agent.ingestion.watch_tool import WatchStreamTool


class _FakeSource:
    """内存版游标源:GET ?since&limit 语义与正式测试台一致。"""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def feed(self, count: int, make=None) -> None:
        base = len(self.events)
        for index in range(count):
            seq = base + index
            event = (make or (lambda s: {"seq": s, "kind": "beat", "flag": False}))(seq)
            self.events.append(event)

    def handle(self, request) -> tuple[bool, object, str]:
        from urllib.parse import parse_qs, urlsplit

        url = request.url
        query = parse_qs(urlsplit(url).query)
        since = int(query.get("since", ["0"])[0])
        limit = int(query.get("limit", ["50"])[0])
        items = [dict(event) for event in self.events if event["seq"] >= since][:limit]
        next_cursor = (items[-1]["seq"] + 1) if items else max(since, len(self.events))
        return True, {"items": items, "next_cursor": next_cursor}, ""


@pytest.fixture()
def owner_home(tmp_path, monkeypatch, inline_watch_open):
    monkeypatch.setattr(ws, "registry", ws.WatchRegistry())
    return tmp_path / "owner"


def _tool(owner_home: Path, source: _FakeSource) -> WatchStreamTool:
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u-test"))
    tool = WatchStreamTool(agent)
    tool.allow_private_resolution = True
    tool._fetch_json = source.handle  # 注入内存源(网络闸另测)
    return tool


def _payload(result) -> dict:
    assert result.ok, result.output
    return json.loads(result.output)


def test_watch_stream_requires_explicit_action(owner_home) -> None:
    tool = _tool(owner_home, _FakeSource())

    assert tool.spec.required_parameters == ["action"]
    result = tool.execute({"watch_id": "ws-do-not-guess"})

    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert "不会把缺失动作猜成 pull" in result.output


class _SingleTaskManager:
    def __init__(self, task) -> None:
        self.task = task

    def load(self, run_id: str):
        if run_id != self.task.id:
            raise FileNotFoundError(run_id)
        return self.task

    def list_runs(self):
        return [self.task]

    def save(self, task) -> None:
        self.task = task


def _named_audit_tool(
    owner_home: Path,
    source: _FakeSource,
    *,
    status: str = "active",
    run_epoch: int = 1,
) -> tuple[WatchStreamTool, object, object, str]:
    from agent.common.audit_activation import (
        AUDIT_ATTR,
        AUDIT_OBJECTIVE_ATTR,
        AUDIT_RUN_EPOCH_ATTR,
        AUDIT_RUN_PROMPT_ATTR,
        AUDIT_SOURCE_BINDING_PENDING_ATTR,
        AUDIT_SOURCE_BINDING_TOOLS,
    )
    from agent.conversation.authority import CONVERSATION_REQUEST_ID_ATTR
    from agent.conversation.store import ConversationStore

    audit_id = "audit-open-transition"
    store = ConversationStore(owner_home / "conversation")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "u-test",
            "channel": "internal",
            "channel_conversation_id": "audit-open-transition",
            "channel_user_id": "u-test",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续研判来源",
            "status": status,
            "work_kind": "audit",
            "work_name": "来源启停竞态",
            "run_epoch": run_epoch,
        }
    )
    attrs = {
        AUDIT_ATTR: True,
        AUDIT_OBJECTIVE_ATTR: "持续研判来源",
        AUDIT_RUN_EPOCH_ATTR: run_epoch,
        AUDIT_RUN_PROMPT_ATTR: "本轮持续研判",
        AUDIT_SOURCE_BINDING_PENDING_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: audit_id,
        "conversation_task_id": audit_id,
    }
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(
            owner_home_dir=str(owner_home),
            owner_id="u-test",
        ),
        conversation_store=store,
        _current_run_params=SimpleNamespace(
            task_attributes=attrs,
            run_id="source-open-transition",
            request_id=audit_id,
        ),
    )
    task = SimpleNamespace(
        id="source-open-transition",
        goal="按用户现场确认的调用方法持续研判这一条来源",
        status="RUNNING",
        root_id=audit_id,
        parent_id=audit_id,
        child_ids=[],
        runner_active_attempt_id="source-open-attempt",
        attributes=attrs,
        allowed_tools=list(AUDIT_SOURCE_BINDING_TOOLS),
        allowed_skills=[],
        allowed_write_roots=[],
        role="worker",
        agent_name="audit-source-open-transition",
        acceptance_checks=[],
        context_packs=[],
        context_manifest=SimpleNamespace(
            required_read_paths=[],
            hint_read_paths=[],
        ),
        updated_at=1.0,
        created_at=1.0,
    )
    agent.subagents = _SingleTaskManager(task)
    agent._current_subagent_run_id = task.id
    agent._current_subagent_attempt_id = task.runner_active_attempt_id
    agent._current_task_attributes = attrs
    tool = WatchStreamTool(agent)
    tool.allow_private_resolution = True
    tool._fetch_json = source.handle
    return tool, agent, thread, audit_id


def _reactivate_named_audit_tool(agent: object, audit_id: str, *, source_goal: str) -> int:
    from agent.common.audit_activation import (
        AUDIT_ATTR,
        AUDIT_OBJECTIVE_ATTR,
        AUDIT_RUN_EPOCH_ATTR,
        AUDIT_RUN_PROMPT_ATTR,
        AUDIT_SOURCE_BINDING_PENDING_ATTR,
    )
    from agent.conversation.authority import CONVERSATION_REQUEST_ID_ATTR

    current = agent.conversation_store.load_task_link(audit_id)
    assert current is not None
    terminal = agent.conversation_store.update_task_status(
        {
            "task_id": audit_id,
            "status": "completed",
            "expected_status": current.status,
        }
    )
    assert terminal is not None
    active = agent.conversation_store.reactivate_audit(
        {
            "task_id": audit_id,
            "goal": "第二轮继续处理新到记录",
            "duration_seconds": 180,
        }
    )
    assert active is not None
    attrs = {
        AUDIT_ATTR: True,
        AUDIT_OBJECTIVE_ATTR: "第二轮沿用来源知识并处理新到记录",
        AUDIT_RUN_EPOCH_ATTR: active.run_epoch,
        AUDIT_RUN_PROMPT_ATTR: "第二轮继续处理新到记录",
        AUDIT_SOURCE_BINDING_PENDING_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: audit_id,
        "conversation_task_id": audit_id,
    }
    task = agent.subagents.task
    task.goal = source_goal
    task.attributes = attrs
    agent._current_run_params.task_attributes = attrs
    agent._current_run_params.run_id = task.id
    agent._current_task_attributes = attrs
    return int(active.run_epoch)


def test_open_pull_status_close_roundtrip(owner_home):
    source = _FakeSource()
    source.feed(600)
    source.events.append({"seq": 600, "kind": "beat", "flag": True})
    tool = _tool(owner_home, source)

    opened = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull?since=<next>&limit=<limit>", "watch_window_seconds": 1200}))
    watch_id = opened["watch_id"]
    assert opened["resumed_existing_watch"] is False
    assert opened["named_audit_effect"] == {
        "source_bound": False,
        "effective_prompt_updated": False,
    }

    # 学判据后再盯(生产正流程 learn→monitor):配了结构规则(flag=false 是常态)才做压缩降维。
    # 未配 spec 的冷启动=宁滥勿漏无条件全读(根因2,另见 test_ingestion_full_read),这里给规则
    # 以验证"已学出结构判据的稳态源"仍正常压组、稀有目标照抬。
    _payload(tool.execute({"action": "configure", "watch_id": watch_id, "spec": {"result_field": "flag", "normal_values": ["false"]}}))

    pulled = _payload(tool.execute({"action": "pull", "watch_id": watch_id}))
    flagged = [c for c in pulled["candidates"] if '"flag": true' in json.dumps(c["event"]).lower() or c["event"].get("flag") is True]
    assert flagged, pulled["candidates"]
    assert pulled["coverage"]["reached_stream_end"] is True
    assert pulled["coverage"]["cursor"] == 601
    assert pulled["watch"]["watch_window_seconds"] == 1200
    assert pulled["suppressed_events_this_call"] > 500

    status = _payload(tool.execute({"action": "status", "watch_id": watch_id}))
    assert status["coverage"]["cursor"] == 601

    closed = _payload(tool.execute({"action": "close", "watch_id": watch_id}))
    assert closed["watch"]["closed"] is True
    assert closed["spool_backlog_candidates_at_close"] == 0  # inline 模式无 spool 积压
    assert "discarded_backlog_note" not in closed

    listed = _payload(tool.execute({"action": "list"}))
    assert listed["count"] == 1 and listed["watches"][0]["closed"] is True


def test_no_fanout_hint_without_run_context(owner_home):
    """摄取层不根据打开来源数量规定 Agent 的委派结构。"""
    source = _FakeSource()
    source.feed(5)
    tool = _tool(owner_home, source)  # 无 _current_run_params → run_id=""
    tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull?since=<next>&limit=<limit>"})
    second = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:10/pull?since=<next>&limit=<limit>"}))
    assert "fanout_hint" not in second


def test_cancelled_named_audit_rejects_source_open_without_persisting_watch(
    owner_home,
) -> None:
    source = _FakeSource()
    source.feed(1)
    tool, _agent, _thread, _audit_id = _named_audit_tool(
        owner_home,
        source,
        status="cancelled",
    )

    result = tool.execute(
        {
            "action": "open",
            "url": "http://127.0.0.1:9/pull?since=<next>&limit=<limit>",
        }
    )

    assert result.ok is False
    assert result.error_code == "AUDIT_PARENT_INACTIVE"
    assert ws.list_states(owner_home) == []


def test_pending_audit_source_rejects_model_transport_rewrite(owner_home) -> None:
    from agent.common.audit_activation import AUDIT_SOURCE_OPEN_ATTR

    source = _FakeSource()
    source.feed(1)
    tool, agent, _thread, _audit_id = _named_audit_tool(owner_home, source)
    binding = {
        "source_id": "source-a",
        "url": "http://127.0.0.1:9/pull",
        "mode": "cursor",
        "http_request": {
            "method": "GET",
            "cursor_binding": {
                "location": "query",
                "name": "since",
                "initial": 0,
            },
            "page_size_binding": {"location": "query", "name": "limit"},
        },
    }
    agent._current_run_params.task_attributes[AUDIT_SOURCE_OPEN_ATTR] = binding

    rejected = tool.execute(
        {
            "action": "open",
            **binding,
            "url": "http://127.0.0.1:10/other",
        }
    )

    assert rejected.ok is False
    assert rejected.error_code == "TOOL_INVALID_ARGUMENTS"
    assert rejected.reported_error_code == "AUDIT_SOURCE_BINDING_CONFLICT"
    assert ws.list_states(owner_home) == []


def test_named_audit_clear_and_source_open_commit_are_serialized(
    owner_home,
    monkeypatch,
) -> None:
    from agent.conversation.named_work import stop_named_conversation_work
    from agent.ingestion import harvester, source_worker
    from agent.ingestion import watch_tool as watch_module

    source = _FakeSource()
    source.feed(1)
    tool, agent, thread, _audit_id = _named_audit_tool(owner_home, source)
    original_persist = watch_module.persist_state
    persisted = threading.Event()
    release_commit = threading.Event()

    def blocking_persist(state) -> None:
        original_persist(state)
        persisted.set()
        assert release_commit.wait(timeout=2.0)

    monkeypatch.setattr(watch_module, "persist_state", blocking_persist)
    monkeypatch.setattr(harvester, "ensure_harvester", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        source_worker,
        "ensure_audit_source_worker",
        lambda _agent, state: {
            "ok": True,
            "state": "active",
            "watch_id": state.watch_id,
            "created": True,
        },
    )
    open_results: list[object] = []
    clear_results: list[object] = []
    open_thread = threading.Thread(
        target=lambda: open_results.append(
            tool.execute(
                {
                    "action": "open",
                    "url": "http://127.0.0.1:9/pull?since=<next>&limit=<limit>",
                }
            )
        )
    )
    open_thread.start()
    assert persisted.wait(timeout=2.0)
    clear_thread = threading.Thread(
        target=lambda: clear_results.append(
            stop_named_conversation_work(
                agent,
                thread_id=thread.thread_id,
                kind="audit",
                name="来源启停竞态",
            )
        )
    )
    clear_thread.start()
    time.sleep(0.05)
    assert clear_thread.is_alive()
    release_commit.set()
    open_thread.join(timeout=2.0)
    clear_thread.join(timeout=2.0)

    assert not open_thread.is_alive()
    assert not clear_thread.is_alive()
    assert open_results and open_results[0].ok is True
    assert clear_results and clear_results[0].ok is True
    states = ws.list_states(owner_home)
    assert len(states) == 1
    assert states[0]["closed"] is True
    assert states[0]["close_reason"] == "named_audit_clear"


def test_named_audit_adapter_probe_uses_typed_identity_before_watch_commit(
    owner_home,
    monkeypatch,
) -> None:
    """A new dynamic source may read its pinned adapter before state persists."""

    from agent.ingestion.source_http import SourceHttpRequest
    from agent.ingestion.watch_tool import _OpenWatchContext, _resolve_open_watch_context

    source = _FakeSource()
    source.feed(1)
    tool, _agent, _thread, audit_id = _named_audit_tool(owner_home, source)
    seen_audit_ids: list[str] = []
    seen_page_limits: list[int] = []

    def plan(**kwargs):
        seen_audit_ids.append(str(kwargs["audit_id"]))
        seen_page_limits.append(int(kwargs["page_limit"]))
        # A source may need a wider page than its overlap before its opaque
        # checkpoint can advance.  Probing with one record would reject a
        # transport that the persisted watch can consume correctly.
        assert kwargs["page_limit"] > 5
        return SimpleNamespace(
            request=SourceHttpRequest(
                "http://127.0.0.1:9/pull?since=0&limit=1",
                "GET",
                {},
                None,
            ),
            context={},
        )

    def accept(**kwargs):
        seen_audit_ids.append(str(kwargs["audit_id"]))
        return SimpleNamespace(
            records=[{"seq": 0}],
            checkpoint={"next": 1},
            has_more=False,
        )

    monkeypatch.setattr("agent.ingestion.source_adapter.plan_source_request", plan)
    monkeypatch.setattr("agent.ingestion.source_adapter.accept_source_response", accept)
    context = _resolve_open_watch_context(
        tool,
        owner_home,
        {
            "action": "open",
            "url": "http://127.0.0.1:9/pull",
            "mode": "adapter",
            "http_request": {"method": "GET"},
            "source_adapter": {
                "path": "work/dynamic_source_adapter.py",
                "sha256": "a" * 64,
            },
            "source_id": "dynamic-source",
            "source_profile_ref": "work/dynamic-source.md",
        },
    )

    assert isinstance(context, _OpenWatchContext)
    assert context.state.source_envelope["valid"] is True
    assert context.state.audit_root_task_id == ""
    assert seen_audit_ids == [audit_id, audit_id]
    assert seen_page_limits == [context.state.tuning.page_limit]


def test_named_audit_next_run_reuses_checkpoint_and_resets_only_run_facts(
    owner_home,
    monkeypatch,
) -> None:
    from agent.ingestion import harvester, source_worker

    monkeypatch.setattr(harvester, "ensure_harvester", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        source_worker,
        "ensure_audit_source_worker",
        lambda _agent, state: {"ok": True, "state": "active", "watch_id": state.watch_id},
    )
    source = _FakeSource()
    tool, agent, _thread, audit_id = _named_audit_tool(owner_home, source, run_epoch=1)
    opened = _payload(
        tool.execute(
            {
                "action": "open",
                "url": "http://127.0.0.1:9/pull?since=<next>&limit=<limit>",
                "watch_window_seconds": 60,
            }
        )
    )
    state = ws.load_state(owner_home, opened["watch_id"])
    assert state is not None
    ws.registry.put(state)
    state.cursor = 240
    state.source_checkpoint = {
        "time_window": {"after": "2026-08-05T00:00:00Z"},
        "range": {"start": 241, "end": 340},
        "page_token": "opaque-next-page",
    }
    state.line_cursor = 77
    state.file_fragment_start = 900
    state.file_fragment_bytes = 31
    state.file_fragment_sha256 = "a" * 64
    state.totals["spool_candidates"] = 240
    ws.persist_state(state)
    read_cursor = {
        "generation": 3,
        "offset": 4096,
        "read_seq": 12,
        "candidates_consumed": 240,
        "candidates_acked": 240,
        "updated_at": 123.0,
    }
    read_cursor_path = ws.state_dir(owner_home) / f"{state.watch_id}.read.json"
    read_cursor_path.write_text(json.dumps(read_cursor), encoding="utf-8")
    ws.close_watch_state(state, reason="audit_window_settled")

    assert _reactivate_named_audit_tool(
        agent,
        audit_id,
        source_goal="第二轮仍只处理这一个来源",
    ) == 2
    resumed = _payload(
        tool.execute(
            {
                "action": "open",
                "url": "http://127.0.0.1:9/pull?since=<next>&limit=<limit>",
                "watch_window_seconds": 180,
            }
        )
    )
    persisted = ws.load_state(owner_home, opened["watch_id"])

    assert resumed["watch_id"] == opened["watch_id"]
    assert resumed["resumed_existing_watch"] is True
    assert resumed["watch"]["closed"] is False
    assert float(resumed["watch"].get("closed_at") or 0.0) == 0.0
    assert str(resumed["watch"].get("close_reason") or "") == ""
    assert int(resumed["watch"].get("close_pending_records") or 0) == 0
    assert persisted is not None
    assert persisted.closed is False
    assert persisted.closed_at == 0.0
    assert persisted.close_reason == ""
    assert persisted.close_pending_records == 0
    assert persisted.audit_run_epoch == 2
    assert persisted.cursor == 240
    assert persisted.source_checkpoint == {
        "time_window": {"after": "2026-08-05T00:00:00Z"},
        "range": {"start": 241, "end": 340},
        "page_token": "opaque-next-page",
    }
    assert persisted.line_cursor == 77
    assert persisted.file_fragment_start == 900
    assert persisted.file_fragment_bytes == 31
    assert persisted.file_fragment_sha256 == "a" * 64
    assert persisted.totals["spool_candidates"] == 240
    assert json.loads(read_cursor_path.read_text(encoding="utf-8")) == read_cursor
    assert persisted.audit_run_prompt == "第二轮继续处理新到记录"
    assert persisted.source_task_goal == "第二轮仍只处理这一个来源"


def test_named_audit_next_run_cannot_take_over_an_open_previous_epoch(
    owner_home,
    monkeypatch,
) -> None:
    from agent.ingestion import harvester, source_worker

    monkeypatch.setattr(harvester, "ensure_harvester", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        source_worker,
        "ensure_audit_source_worker",
        lambda _agent, state: {"ok": True, "state": "active", "watch_id": state.watch_id},
    )
    source = _FakeSource()
    tool, agent, _thread, audit_id = _named_audit_tool(owner_home, source, run_epoch=1)
    params = {
        "action": "open",
        "url": "http://127.0.0.1:9/pull?since=<next>&limit=<limit>",
        "watch_window_seconds": 60,
    }
    opened = _payload(tool.execute(params))
    assert _reactivate_named_audit_tool(
        agent,
        audit_id,
        source_goal="第二轮仍只处理这一个来源",
    ) == 2

    denied = tool.execute(params)
    persisted = ws.load_state(owner_home, opened["watch_id"])

    assert denied.ok is False
    assert denied.reported_error_code == "AUDIT_PREVIOUS_RUN_ACTIVE"
    assert persisted is not None
    assert persisted.closed is False
    assert persisted.audit_run_epoch == 1


def test_close_surfaces_unjudged_spool_backlog(owner_home):
    """g8 不足4·不静默弃判:close 时 spool 还有已抬未判候选 → 关闭回执如实亮出数目与提示
    (纯计数,不拦关闭;要盯完先 pull 清账再 close)。"""
    source = _FakeSource()
    tool = _tool(owner_home, source)
    state = ws.new_state(owner_home, "http://127.0.0.1:9/pull?since=<next>&limit=<limit>", {"background_harvest": 0})
    state.totals["spool_candidates"] = 97
    ws.persist_state(state)
    (ws.state_dir(owner_home) / f"{state.watch_id}.read.json").write_text(
        json.dumps({"read_seq": 0, "candidates_consumed": 40, "updated_at": 0}), encoding="utf-8"
    )

    closed = _payload(tool.execute({"action": "close", "watch_id": state.watch_id}))

    assert closed["spool_backlog_candidates_at_close"] == 57
    assert "57" in closed["discarded_backlog_note"]


def test_pull_resumes_from_disk_after_process_restart(owner_home):
    source = _FakeSource()
    source.feed(300)
    tool = _tool(owner_home, source)
    opened = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull?since=<next>&limit=<limit>"}))
    _payload(tool.execute({"action": "pull", "watch_id": opened["watch_id"]}))

    # 模拟进程重启:清空进程内注册表,新工具实例从盘上快照复活并续游标。
    ws.registry = ws.WatchRegistry()
    source.feed(50)
    tool2 = _tool(owner_home, source)
    pulled = _payload(tool2.execute({"action": "pull", "watch_id": opened["watch_id"]}))
    assert pulled["coverage"]["cursor"] == 350
    assert pulled["coverage"]["seen_this_call"] == 50
    assert pulled["engine_totals"]["events_seen"] == 350


def test_open_reuses_watch_for_same_url(owner_home):
    source = _FakeSource()
    source.feed(10)
    tool = _tool(owner_home, source)
    first = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull?since=<next>&limit=<limit>"}))
    second = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull?since=<next>&limit=<limit>"}))
    assert first["watch_id"] == second["watch_id"]
    assert second["resumed_existing_watch"] is True


def test_open_uses_learned_cursor_request_and_discovers_response_envelope(owner_home):
    events = [{"seq": index, "event_id": f"E-{index}"} for index in range(1, 8)]
    source = _FakeSource()
    tool = _tool(owner_home, source)

    def fetch(request):
        url = request.url
        from urllib.parse import parse_qs, urlsplit

        query = parse_qs(urlsplit(url).query)
        since = int(query.get("since", ["0"])[0])
        limit = int(query.get("limit", ["50"])[0])
        page = [dict(row) for row in events if row["seq"] > since][:limit]
        next_value = page[-1]["seq"] if page else max(0, since)
        return True, {"source": "demo", "events": page, "next": next_value}, ""

    tool.__dict__["_fetch_json"] = fetch
    opened = _payload(
        tool.execute(
            {
                "action": "open",
                "url": "http://127.0.0.1:9/events",
                "http_request": {
                    "method": "GET",
                    "cursor_binding": {
                        "location": "query",
                        "name": "since",
                        "initial": 0,
                        "offset": -1,
                    },
                    "page_size_binding": {
                        "location": "query",
                        "name": "limit",
                    },
                },
            }
        )
    )
    state = ws.load_state(owner_home, opened["watch_id"])
    assert state is not None
    assert state.source_mode == ""
    assert state.source_envelope["record_list_key"] == "events"
    assert state.source_envelope["cursor_field"] == "next"
    assert state.source_envelope["cursor_semantics"] == "last_seen"

    sampled = _payload(
        tool.execute(
            {
                "action": "sample",
                "watch_id": opened["watch_id"],
                "sample_count": 20,
            }
        )
    )
    assert sampled["sampled_events"] == 7
    assert [row["seq"] for row in sampled["raw_events"]] == list(range(1, 8))

    pulled = _payload(
        tool.execute({"action": "pull", "watch_id": opened["watch_id"]})
    )
    assert [row["event"]["seq"] for row in pulled["candidates"]] == list(range(1, 8))
    assert pulled["coverage"]["cursor"] == 8


def test_failed_poll_probe_preserves_typed_runtime_failure(owner_home):
    tool = _tool(owner_home, _FakeSource())
    fetched_urls: list[str] = []

    def fetch(request):
        url = request.url
        fetched_urls.append(url)
        return False, None, "ARTIFACT_TOO_LARGE"

    tool.__dict__["_fetch_json"] = fetch
    result = tool.execute(
        {
            "action": "open",
            "url": "http://127.0.0.1:9/events",
            "mode": "poll",
        }
    )

    assert result.ok is False
    assert result.error_code == "ARTIFACT_TOO_LARGE"
    assert "record_list_field" not in result.output
    assert fetched_urls == ["http://127.0.0.1:9/events"]
    assert list((owner_home / "watch_state").glob("ws-*.json")) == []


def test_second_cursor_probe_preserves_network_failure(owner_home):
    tool = _tool(owner_home, _FakeSource())
    calls = 0

    def fetch(_request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return True, {"events": [{"seq": 0}], "next": 1}, ""
        return False, "connection refused", "NETWORK_REQUEST_FAILED"

    tool.__dict__["_fetch_json"] = fetch
    result = tool.execute(
        {
            "action": "open",
            "url": "http://127.0.0.1:9/events?since=<next>",
            "record_list_field": "events",
            "cursor_field": "next",
            "cursor_semantics": "next_position",
        }
    )

    assert result.ok is False
    assert result.error_code == "NETWORK_REQUEST_FAILED"
    assert "connection refused" in result.output
    assert "record_list_field" not in result.output
    assert calls == 2
    assert list((owner_home / "watch_state").glob("ws-*.json")) == []


def test_ambiguous_cursor_envelope_fails_before_state_is_persisted(owner_home):
    source = _FakeSource()
    tool = _tool(owner_home, source)
    tool.__dict__["_fetch_json"] = lambda _url: (
        True,
        {
            "events": [{"seq": 1}],
            "metadata_rows": [{"name": "x"}],
            "offset": 1,
            "count": 1,
        },
        "",
    )

    result = tool.execute(
        {
            "action": "open",
            "url": "http://127.0.0.1:9/ambiguous?position=<next>&count=<limit>",
        }
    )

    assert not result.ok
    assert result.error_code == "SOURCE_ENVELOPE_INVALID"
    assert "events" in result.output
    assert "metadata_rows" in result.output
    assert "JSONPath" in result.output
    assert ws.list_states(owner_home) == []


@pytest.mark.parametrize(
    "raw_url",
    [
        ["http://127.0.0.1:9/a", "http://127.0.0.1:9/b"],
        '["http://127.0.0.1:9/a","http://127.0.0.1:9/b"]',
    ],
)
def test_open_rejects_multi_source_url_with_actionable_shape_error(
    owner_home,
    raw_url,
):
    tool = _tool(owner_home, _FakeSource())

    result = tool.execute(
        {
            "action": "open",
            "url": raw_url,
        }
    )

    assert not result.ok
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert "单个来源地址" in result.output
    assert "每次传一个 URL" in result.output


def test_explicit_cursor_structure_resolves_ambiguous_envelope(owner_home):
    source = _FakeSource()
    tool = _tool(owner_home, source)

    def fetch(request):
        url = request.url
        from urllib.parse import parse_qs, urlsplit

        since = int(parse_qs(urlsplit(url).query).get("since", ["0"])[0])
        events = [{"position": 1, "value": "kept"}] if since < 1 else []
        return True, {
            "events": events,
            "metadata_rows": [{"name": "x"}],
            "offset": 1,
            "count": len(events),
            "more": False,
        }, ""

    tool.__dict__["_fetch_json"] = fetch
    opened = _payload(
        tool.execute(
            {
                "action": "open",
                "url": "http://127.0.0.1:9/custom",
                "http_request": {
                    "method": "GET",
                    "cursor_binding": {
                        "location": "query",
                        "name": "since",
                        "initial": 0,
                        "offset": -1,
                    },
                },
                "record_list_field": "events",
                "cursor_field": "offset",
                "cursor_semantics": "last_seen",
                "has_more_field": "more",
            }
        )
    )
    state = ws.load_state(owner_home, opened["watch_id"])
    assert state is not None
    assert state.source_envelope["record_list_key"] == "events"
    assert state.source_envelope["cursor_field"] == "offset"
    assert state.source_envelope["has_more_field"] == "more"
    assert state.source_envelope["continuation_verified"] is True


def test_cursor_probe_rejects_response_cursor_used_as_request_parameter(owner_home):
    """A response next_cursor name is not evidence for the request parameter name."""

    source = _FakeSource()
    source.feed(60)
    tool = _tool(owner_home, source)

    result = tool.execute(
        {
            "action": "open",
            "url": "http://127.0.0.1:9/pull",
            "http_request": {
                "method": "GET",
                "cursor_binding": {
                    "location": "query",
                    "name": "next_cursor",
                    "initial": 0,
                },
            },
            "record_list_field": "items",
            "cursor_field": "next_cursor",
            "cursor_semantics": "next_position",
        }
    )

    assert result.ok is False
    assert result.error_code == "SOURCE_ENVELOPE_INVALID"
    assert "cursor_binding" in result.output
    assert ws.list_states(owner_home) == []


def test_published_prepare_probe_can_relearn_transport_at_same_url(owner_home):
    source = _FakeSource()
    source.feed(60)
    tool = _tool(owner_home, source)
    task_id = "audit-relearn-source"
    profile = owner_home / "tasks" / task_id / "work" / "sources" / "source-a.md"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text("source_id: source-a\n", encoding="utf-8")
    tool.agent._current_run_params = SimpleNamespace(
        task_attributes={
            "conversation_audit_prepare": True,
            "conversation_transient_workspace": True,
            "conversation_task_id": task_id,
            "conversation_turn_request_id": "prepare-relearn-source-1",
        }
    )

    first = _payload(
        tool.execute(
            {
                "action": "open",
                "url": "http://127.0.0.1:9/pull?since=<next>&limit=<limit>",
                "source_id": "source-a",
                "source_profile_ref": str(profile),
            }
        )
    )
    from agent.ingestion import watch_tool as watch_tool_module
    from agent.ingestion.watch_state import close_watch_state

    state = watch_tool_module.registry.get_or_load(owner_home, first["watch_id"])
    assert state is not None
    close_watch_state(state, reason="audit_prepare_published")
    tool.agent._current_run_params.task_attributes["conversation_turn_request_id"] = (
        "prepare-relearn-source-2"
    )

    second = _payload(
        tool.execute(
            {
                "action": "open",
                "url": "http://127.0.0.1:9/pull",
                "http_request": {
                    "method": "GET",
                    "cursor_binding": {
                        "location": "query",
                        "name": "since",
                        "initial": 0,
                    },
                    "page_size_binding": {
                        "location": "query",
                        "name": "limit",
                    },
                },
                "source_id": "source-a",
                "source_profile_ref": str(profile),
            }
        )
    )

    assert second["watch_id"] != first["watch_id"]
    assert second["resumed_existing_watch"] is False
    relearned = ws.load_state(owner_home, second["watch_id"])
    assert relearned is not None
    assert relearned.source_envelope["request"]["cursor_binding"]["name"] == "since"
    assert relearned.source_envelope["continuation_verified"] is True


def test_wrong_explicit_cursor_shape_does_not_fall_back_to_poll(owner_home):
    source = _FakeSource()
    tool = _tool(owner_home, source)
    tool.__dict__["_fetch_json"] = lambda _url: (
        True,
        {"source": "demo", "events": [], "next": 0},
        "",
    )

    result = tool.execute(
        {
            "action": "open",
            "url": "http://127.0.0.1:9/events?position=<next>&count=<limit>",
            "record_list_field": "records",
        }
    )

    assert not result.ok
    assert result.error_code == "SOURCE_ENVELOPE_INVALID"
    assert "events" in result.output
    assert "record_list_field='records'" in result.output
    assert "能自动识别时可省略" in result.output
    assert ws.list_states(owner_home) == []


def test_wrong_explicit_cursor_field_reports_observed_top_level_candidate(
    owner_home,
):
    tool = _tool(owner_home, _FakeSource())
    tool.__dict__["_fetch_json"] = lambda _url: (
        True,
        {"source": "demo", "events": [{"seq": 1}], "next": 1},
        "",
    )

    result = tool.execute(
        {
            "action": "open",
            "url": "http://127.0.0.1:9/events?position=<next>&count=<limit>",
            "record_list_field": "events",
            "cursor_field": "seq",
        }
    )

    assert not result.ok
    assert result.error_code == "SOURCE_ENVELOPE_INVALID"
    assert "cursor_field='seq'" in result.output
    assert "顶层整数候选: ['next']" in result.output


def test_private_host_blocked_without_grant(owner_home):
    tool = WatchStreamTool(SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u")))
    tool.allow_private_resolution = False
    result = tool.execute({"action": "open", "url": "http://192.168.77.10:8901/pull?since=<next>&limit=<limit>"})
    assert not result.ok
    assert result.error_code in {"NETWORK_PRIVATE_HOST_BLOCKED", "NETWORK_PRIVATE_IP_BLOCKED"}


def test_private_host_allowed_with_injected_grant(owner_home):
    source = _FakeSource()
    source.feed(5)
    tool = _tool(owner_home, source)
    tool.allow_private_resolution = None
    tool.allowed_private_hosts = ("192.168.77.10",)
    opened = _payload(tool.execute({"action": "open", "url": "http://192.168.77.10:8901/pull?since=<next>&limit=<limit>"}))
    assert opened["ok"] is True


def test_audit_file_is_ndjson_outside_report_surface(owner_home):
    source = _FakeSource()
    source.feed(120)
    tool = _tool(owner_home, source)
    opened = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull?since=<next>&limit=<limit>"}))
    _payload(tool.execute({"action": "pull", "watch_id": opened["watch_id"]}))
    audit_files = list((owner_home / "watch_state").glob("*.audit.ndjson"))
    assert audit_files, "审计账必须落盘"
    # 对账脚本的扫描面(.jsonl/.json/.md/.txt/.log)绝不能包含审计账后缀,否则原始事件 ID 混进上报面。
    assert all(path.suffix == ".ndjson" for path in audit_files)
    record = json.loads(audit_files[0].read_text(encoding="utf-8").splitlines()[0])
    assert "cursor_to" in record and "groups" in record


def test_pull_unknown_watch_id_lists_known(owner_home):
    source = _FakeSource()
    tool = _tool(owner_home, source)
    result = tool.execute({"action": "pull", "watch_id": "ws-nope"})
    assert not result.ok
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"


def test_source_error_persists_cursor_and_reports(owner_home):
    source = _FakeSource()
    source.feed(40)
    tool = _tool(owner_home, source)
    opened = _payload(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull?since=<next>&limit=<limit>"}))
    _payload(tool.execute({"action": "pull", "watch_id": opened["watch_id"]}))

    tool._fetch_json = lambda url: (False, "boom", "NETWORK_REQUEST_FAILED")
    result = tool.execute({"action": "pull", "watch_id": opened["watch_id"]})
    assert not result.ok
    body = json.loads(result.output)
    assert body["coverage"]["cursor"] == 40
    assert result.error_code == "NETWORK_REQUEST_FAILED"
