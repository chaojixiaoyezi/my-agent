"""当前冻结范围只消费可反查的原来源；本人未选空间查史合同不变。"""

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.capability.session_search_tool import SessionSearchTool
from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
from agent_py_agent.agent.conversation.history_index import index_conversation_message
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_store.recall import MemoryRecallScope
from agent_py_agent.agent.settings import AgentConfig


@pytest.fixture
def world(tmp_path):
    gateway = tmp_path / "gateway"
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            gateway_workspace=str(gateway),
        ),
        tmp_path / "workspace",
    )
    threads, messages = [], []
    for project in ("A", "B"):
        thread = agent.conversation_store.threads.get_or_create(
            {
                "channel": "chat",
                "channel_user_id": "local-agent",
                "canonical_user_id": "local-agent",
                "channel_conversation_id": project,
                "owner_id": agent.home_paths.owner_id,
                "owner_home": str(agent.home_paths.owner_home_dir),
            }
        )
        threads.append(thread)
        source_task = "task-" + project
        original = BusinessExecutionContext(
            agent.home_paths.owner_id,
            project,
            project,
            source_task,
            1,
            None,
            (),
            (),
            "business-context.v1",
        )
        agent.subagents.runtime_db.record_run_creation(
            owner_id=agent.home_paths.owner_id,
            thread_id=thread.thread_id,
            run_id="gwreq-" + project,
            conversation_task_id=source_task,
            task_run_metadata={
                "session_task_ref": source_task,
                "business_context": original.to_dict(),
            },
        )
        # 原宿主受理文件是此合成任务的权威；派生索引不带space也可按原精确ID回查。
        request_id = "gwreq-" + project
        path = gateway / "requests/done" / (request_id + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "id": request_id,
                    "host_request_owner.v1": {
                        "provider": "local",
                        "owner_kind": "main",
                        "owner_id": "main",
                    },
                    "host_session_space.v1": {
                        "owner_id": agent.home_paths.owner_id,
                        "thread_id": thread.thread_id,
                        "space_id": project,
                    },
                }
            )
        )
        row = agent.conversation_store.messages.append(
            {
                "thread_id": thread.thread_id,
                "role": "user",
                "content": "projectneedle private-" + project,
                "metadata": {"gateway_request_id": request_id},
            }
        )
        index_conversation_message(agent, agent.conversation_store, row)
        messages.append(row)
    params = RunParams(
        request_id="current",
        host_session_space={"space_id": "A"},
        task_attributes={"conversation_thread_id": threads[0].thread_id},
        memory_recall_scope=MemoryRecallScope.from_runtime(task_attributes={"project_id": "A"}),
    )
    agent._current_run_params = params
    return agent, threads, messages


def call(agent, **params):
    out = SessionSearchTool(agent).execute(params)
    assert out.ok, out.output
    return json.loads(out.output)


def test_scoped_search_filters_before_limit_and_recovers_old_index_by_original_id(world):
    agent, threads, messages = world
    for number in range(80):
        row = agent.conversation_store.messages.append(
            {
                "thread_id": threads[1].thread_id,
                "role": "user",
                "content": f"projectneedle private-B-{number}",
                "metadata": {"gateway_request_id": "gwreq-B"},
            }
        )
        index_conversation_message(agent, agent.conversation_store, row)
    page = call(agent, mode="search", query="projectneedle", limit=1)
    assert page["count"] == 1 and "private-A" in page["results"][0]["snippet"]
    assert "private-B" not in str(page) and page["coverage"] == "complete"
    # 当前展示线程改属A也不能替原B请求改范围。
    agent.conversation_store.threads.update_atomic(
        threads[1].thread_id, lambda row: replace(row, metadata={**row.metadata, "space_id": "A"})
    )
    denied = SessionSearchTool(agent).execute(
        {"mode": "message", "thread_id": threads[1].thread_id, "message_id": messages[1].message_id}
    )
    assert (
        not denied.ok
        and denied.error_code == "HISTORY_SCOPE_NOT_VISIBLE"
        and "private-B" not in denied.output
    )


@pytest.mark.parametrize("mode", ["search", "recent", "around", "thread_search", "thread_browse"])
def test_all_index_modes_exclude_other_scope(world, mode):
    agent, threads, messages = world
    params = {"mode": mode}
    if mode in {"search", "thread_search"}:
        params["query"] = "projectneedle"
    if mode == "around":
        params["around_id"] = agent.local_store.make_record_id(
            "conversation_message", threads[0].thread_id + ":" + messages[0].message_id
        )
    page = call(agent, **params)
    assert "private-B" not in str(page) and "private-A" in str(page)


def test_unknown_old_provenance_and_forged_index_do_not_grant_scope(world):
    agent, threads, _ = world
    row = agent.conversation_store.messages.append(
        {"thread_id": threads[0].thread_id, "role": "user", "content": "projectneedle old-unknown"}
    )
    index_conversation_message(agent, agent.conversation_store, row)
    agent.local_store.upsert_record(
        source_type="conversation_message",
        source_id=threads[0].thread_id + ":" + row.message_id,
        title="private-B title",
        content="projectneedle private-B content",
        metadata={"space_id": "A", "thread_id": threads[0].thread_id},
    )
    page = call(agent, mode="search", query="projectneedle")
    assert (
        page["coverage"] == "partial"
        and "private-B" not in str(page)
        and "old-unknown" not in str(page)
    )


def test_formal_memory_scope_is_rechecked_and_personal_survives(world):
    agent, _, _ = world
    for kind, key, text in [
        ("project", "project:A", "memo-A"),
        ("project", "project:B", "memo-B"),
        ("personal", "personal", "memo-personal"),
    ]:
        rec = agent.memory.add(
            "user", text, kind="fact", attributes={"scope_type": kind, "scope_key": key}
        )
        agent.local_store.upsert_record(
            source_type="memory",
            source_id=rec.entry_id,
            title="memo",
            content=text,
            metadata={"attributes": {"scope_type": "project", "scope_key": "project:A"}},
        )
    page = call(agent, mode="search", query="memo", source_type="memory")
    assert "memo-A" in str(page) and "memo-personal" in str(page) and "memo-B" not in str(page)


def test_unselected_owner_and_original_approved_child_history_remain_available(world):
    agent, threads, messages = world
    for context in ("conversation", "task_local"):
        agent._current_run_params = RunParams(
            context_scope=context, task_attributes={"conversation_thread_id": threads[0].thread_id}
        )
        assert "private-B" in str(call(agent, mode="search", query="projectneedle"))
        assert "private-B" in str(
            call(
                agent,
                mode="message",
                thread_id=threads[1].thread_id,
                message_id=messages[1].message_id,
            )
        )


def test_around_preserves_both_sides_anchor_and_recent_preview(world):
    agent, threads, _ = world
    entries = []
    for text in ("older", "anchor", "newer"):
        row = agent.conversation_store.messages.append(
            {
                "thread_id": threads[0].thread_id,
                "role": "user",
                "content": "projectneedle " + text,
                "metadata": {"gateway_request_id": "gwreq-A"},
            }
        )
        index_conversation_message(agent, agent.conversation_store, row)
        entries.append(row)
    anchor = agent.local_store.make_record_id(
        "conversation_message", threads[0].thread_id + ":" + entries[1].message_id
    )
    page = call(agent, mode="around", around_id=anchor, window=1)
    assert (page["around_id"], page["thread_id"], page["context_kind"], page["window"]) == (
        anchor,
        threads[0].thread_id,
        "same_thread_records",
        1,
    )
    assert page["messages_before"] == page["messages_after"] == 1
    assert [x["content"] for x in page["messages"]] == [
        "projectneedle older",
        "projectneedle anchor",
        "projectneedle newer",
    ]
    assert page["messages"][1]["anchor"] is True
    assert not call(agent, mode="around", around_id=anchor, source_type="memory")["messages"]
    recent = call(agent, mode="recent")
    assert all("preview" in row and "content" not in row for row in recent["results"])


def test_hidden_rows_and_same_id_changes_never_reopen_after_authorization(world, monkeypatch):
    agent, threads, messages = world
    hidden = []
    for role, metadata in [
        ("system", {}),
        ("tool", {}),
        ("assistant", {"assistant_part_id": "commentary:1"}),
    ]:
        row = agent.conversation_store.messages.append(
            {
                "thread_id": threads[0].thread_id,
                "role": role,
                "content": "hidden-internal",
                "metadata": {**metadata, "gateway_request_id": "gwreq-A"},
            }
        )
        hidden.append(row)
    for row in hidden:
        result = SessionSearchTool(agent).execute(
            {"mode": "message", "thread_id": threads[0].thread_id, "message_id": row.message_id}
        )
        assert not result.ok and "hidden-internal" not in result.output
    page = call(agent, mode="thread_browse", limit=20)
    assert "hidden-internal" not in str(page)
    original = agent.conversation_store.messages.by_id_report
    reads = []

    def changing(tid, mid, **kwargs):
        reads.append(mid)
        row, errors = original(tid, mid, **kwargs)
        return (row if len(reads) == 1 else replace(row, content="forbidden-second-read")), errors

    monkeypatch.setattr(agent.conversation_store.messages, "by_id_report", changing)
    result = call(
        agent, mode="message", thread_id=threads[0].thread_id, message_id=messages[0].message_id
    )
    assert reads == [messages[0].message_id] and result["content"] == "projectneedle private-A"


def test_same_project_forged_index_thread_and_unproved_foreground_project(world):
    agent, threads, _ = world
    other = agent.conversation_store.threads.create(owner_id=agent.home_paths.owner_id)
    context = BusinessExecutionContext(
        agent.home_paths.owner_id,
        "A",
        "A",
        "same-project-task",
        1,
        None,
        (),
        (),
        "business-context.v1",
    )
    agent.subagents.runtime_db.record_run_creation(
        owner_id=agent.home_paths.owner_id,
        thread_id=other.thread_id,
        run_id="gwreq-same-project",
        conversation_task_id=context.task_id,
        task_run_metadata={
            "session_task_ref": context.task_id,
            "business_context": context.to_dict(),
        },
    )
    row = agent.conversation_store.messages.append(
        {
            "thread_id": other.thread_id,
            "role": "user",
            "content": "projectneedle same-project-other-thread",
            "metadata": {"gateway_request_id": "gwreq-same-project"},
        }
    )
    agent.local_store.upsert_record(
        source_type="conversation_message",
        source_id=other.thread_id + ":" + row.message_id,
        title="spoof",
        content=row.content,
        metadata={"thread_id": threads[0].thread_id},
    )
    page = call(agent, mode="thread_search", query="projectneedle")
    assert page["mode"] == "thread_discover" and page["thread_id"] == threads[0].thread_id
    assert "same-project-other-thread" not in str(page)
    # 无原project字段的前台六字段空间选择，不够证明旧项目归属。
    old = agent.conversation_store.messages.append(
        {
            "thread_id": threads[0].thread_id,
            "role": "user",
            "content": "projectneedle foreground-unproven",
            "metadata": {"gateway_request_id": "old-foreground"},
        }
    )
    gateway = agent.config.gateway_workspace
    from pathlib import Path

    (Path(gateway) / "requests/done/old-foreground.json").write_text(
        json.dumps(
            {
                "id": "old-foreground",
                "host_request_owner.v1": {
                    "provider": "local",
                    "owner_kind": "main",
                    "owner_id": "main",
                },
                "host_session_space.v1": {
                    "owner_id": agent.home_paths.owner_id,
                    "thread_id": threads[0].thread_id,
                    "space_id": "A",
                },
            }
        )
    )
    index_conversation_message(agent, agent.conversation_store, old)
    page = call(agent, mode="search", query="projectneedle")
    assert page["coverage"] == "partial" and "foreground-unproven" not in str(page)


def test_oversized_source_sentinels_are_charged_once_and_total_is_bounded(world, monkeypatch):
    from agent_py_agent.agent.capability import session_search_scope as module

    agent, _, _ = world
    guard = module.ScopedHistory(agent, agent._current_run_params.memory_recall_scope)
    guard.remaining_bytes = 513
    reads = []

    def too_large(*args, max_bytes, **kwargs):
        reads.append(max_bytes + 1)
        raise ValueError("managed file exceeds read limit")

    monkeypatch.setattr(module, "read_bytes_beneath", too_large)
    try:
        for ref in ("large-one", "large-one", "large-two"):
            try:
                guard._request(ref)
            except ValueError:
                pass
        assert sum(reads) == 513 and guard.remaining_bytes == 0
    finally:
        guard.close()


def test_real_large_sources_and_sentinel_reads_share_total_budget(world, monkeypatch):
    import os
    from pathlib import Path

    from agent_py_agent.agent.capability import session_search_scope as module

    agent, _, _ = world
    directory = Path(agent.config.gateway_workspace) / "requests/done"
    inodes = set()
    for name in ("large-real-one", "large-real-two", "large-real-three"):
        p = directory / (name + ".json")
        p.write_bytes(b"x" * (module._MAX_SOURCE_BYTES + 100))
        inodes.add(p.stat().st_ino)
    original = os.read
    observed = []

    def read(fd, size):
        raw = original(fd, size)
        if os.fstat(fd).st_ino in inodes:
            observed.append(len(raw))
        return raw

    monkeypatch.setattr(os, "read", read)
    guard = module.ScopedHistory(agent, agent._current_run_params.memory_recall_scope)
    try:
        for name in ("large-real-one", "large-real-one", "large-real-two", "large-real-three"):
            try:
                guard._request(name)
            except ValueError:
                pass
        assert sum(observed) == 4 * 1024 * 1024 and guard.remaining_bytes == 0
    finally:
        guard.close()


def test_same_space_other_project_is_not_authorized_by_current_selection(world):
    agent, threads, _ = world
    ctx = BusinessExecutionContext(
        agent.home_paths.owner_id,
        "A",
        "B",
        "old-project-task",
        1,
        None,
        (),
        (),
        "business-context.v1",
    )
    agent.subagents.runtime_db.record_run_creation(
        owner_id=agent.home_paths.owner_id,
        thread_id=threads[0].thread_id,
        run_id="old-project-run",
        conversation_task_id=ctx.task_id,
        task_run_metadata={"session_task_ref": ctx.task_id, "business_context": ctx.to_dict()},
    )
    row = agent.conversation_store.messages.append(
        {
            "thread_id": threads[0].thread_id,
            "role": "user",
            "content": "projectneedle other-project-B",
            "metadata": {"conversation_request_id": "old-project-run"},
        }
    )
    index_conversation_message(agent, agent.conversation_store, row)
    assert "other-project-B" not in str(call(agent, mode="search", query="projectneedle"))


def test_task_refs_keep_original_bounded_priority(world):
    from pathlib import Path

    agent, threads, _ = world
    p = Path(agent.config.gateway_workspace) / "requests/done/gwreq-A.json"
    source = json.loads(p.read_text())
    source.update(
        status="done",
        goal="projectneedle original task",
        conversation_runtime={
            "thread_id": threads[0].thread_id,
            "task_id": "task-A",
            "task_path": "/authorized/original-task",
        },
    )
    p.write_text(json.dumps(source))
    agent.local_store.upsert_record(
        source_type="gateway_request",
        source_id="gwreq-A",
        title="old task",
        content="projectneedle original task",
    )
    for number in range(5):
        row = agent.conversation_store.messages.append(
            {
                "thread_id": threads[0].thread_id,
                "role": "user",
                "content": f"projectneedle newer ordinary {number}",
                "metadata": {"gateway_request_id": "gwreq-A"},
            }
        )
        index_conversation_message(agent, agent.conversation_store, row)
    page = call(agent, mode="search", query="projectneedle", limit=1)
    assert page["results"][0]["task_ref"]["task_path"] == "/authorized/original-task"


def test_frozen_exact_large_row_preflight_and_shared_actual_bytes(world, monkeypatch):
    from agent_py_agent.agent.capability.session_search_scope import ScopedHistory
    from agent_py_agent.agent.conversation import message_scan

    agent, threads, rows = world
    huge = agent.conversation_store.messages.append({
        "thread_id": threads[0].thread_id, "role": "user",
        "content": "projectneedle " + "x" * (6 * 1024 * 1024),
        "metadata": {"gateway_request_id": "gwreq-A"},
    })
    opened = message_scan._open_hot_transcript
    actual = [0]

    class Counted:
        def __init__(self, raw):
            self.raw = raw

        def __getattr__(self, key):
            return getattr(self.raw, key)

        def read(self, *args):
            data = self.raw.read(*args)
            actual[0] += len(data)
            return data

        def readline(self, *args):
            data = self.raw.readline(*args)
            actual[0] += len(data)
            return data

    monkeypatch.setattr(message_scan, "_open_hot_transcript", lambda *a: Counted(opened(*a)))
    guard = ScopedHistory(agent, agent._current_run_params.memory_recall_scope)
    try:
        assert guard.message(threads[0].thread_id, huge.message_id) is None
        # 原文件确有6MiB单行，但缺locator时仅读LF尾界，未为查询建全量索引。
        assert actual[0] == guard.read_budget.size == 1
        assert guard.unknown
        # 多次候选共享同一预算，不因每个源/已知ID重置。
        guard.remaining_bytes = 2
        for _ in range(5):
            assert guard.message(threads[1].thread_id, rows[1].message_id) is None
        assert actual[0] <= 3
        assert guard.read_budget.size <= guard.read_budget.max_bytes
    finally:
        guard.close()



def test_sqlite_real_lease_progress_interrupt_cleanup_and_expiration():
    import sqlite3

    from agent_py_agent.agent.io.sqlite_lease import SQLiteConnectionLease, SQLiteLeaseExpiredError

    with sqlite3.connect(':memory:') as raw:
        lease = SQLiteConnectionLease(raw, lambda: True)
        saved = lease.set_progress_handler
        saved(lambda: 1, 1)
        with pytest.raises(sqlite3.OperationalError, match='interrupted'):
            lease.execute('SELECT 1').fetchall()
        lease._invalidate()
        with pytest.raises(SQLiteLeaseExpiredError):
            saved(lambda: 1, 1)
        next_lease = SQLiteConnectionLease(raw, lambda: True)
        assert next_lease.execute('SELECT 2').fetchone()[0] == 2
        next_lease._invalidate()



def test_missing_history_interface_is_not_retryable_or_sensitive(world, monkeypatch):
    from agent_py_agent.agent.capability import session_search_tool as module

    agent, _, _ = world
    def fail(*args):
        raise AttributeError('secret/path token=do-not-publish missing set_progress_handler')
    monkeypatch.setattr(module, '_store_for', fail)
    out = SessionSearchTool(agent).execute({'mode': 'recent'})
    data = json.loads(out.output)
    assert not out.ok and out.error_code == 'HISTORY_QUERY_UNAVAILABLE'
    assert out.retryable is False and out.recommended_action == 'change_strategy'
    assert data['exception_type'] == 'AttributeError'
    assert out.effect_outcome == 'not_started' and out.failure_stage == 'execution' and out.phase == 'history_query'
    assert 'do-not-publish' not in out.output and 'secret/path' not in out.output
    assert '停止原样重试' in data['hint']


@pytest.mark.parametrize('structured', [False, True])
def test_only_structured_sqlite_busy_allows_retry(world, monkeypatch, structured):
    import sqlite3

    from agent_py_agent.agent.capability import session_search_tool as module

    agent, _, _ = world
    error = sqlite3.OperationalError('database is locked private-path')
    if structured:
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    def fail(*args):
        raise error
    monkeypatch.setattr(module, '_store_for', fail)
    out = SessionSearchTool(agent).execute({'mode': 'recent'})
    assert out.retryable is structured
    assert out.error_code == ('HISTORY_STORAGE_BUSY' if structured else 'HISTORY_QUERY_UNAVAILABLE')
    assert 'private-path' not in out.output


def test_corrupt_history_reports_integrity_failure_without_retry(world, monkeypatch):
    import sqlite3

    from agent_py_agent.agent.capability import session_search_tool as module

    agent, _, _ = world
    error = sqlite3.DatabaseError('sensitive damaged source')
    error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
    def fail(*args):
        raise error
    monkeypatch.setattr(module, '_store_for', fail)
    out = SessionSearchTool(agent).execute({'mode': 'recent'})
    assert out.error_code == 'HISTORY_SOURCE_CORRUPT' and out.retryable is False
    assert out.effect_outcome == 'not_started'
    assert json.loads(out.output)['exception_type'] == 'DatabaseError'
    assert 'sensitive' not in out.output
