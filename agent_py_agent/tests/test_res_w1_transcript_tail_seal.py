"""RES-W1: real-store torn tails, nonblocking projections and conservative dedupe."""
from __future__ import annotations

import json
import logging
import os
import re
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.display_checkpoint import display_checkpoint_metadata
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import request_history
from agent_py_agent.agent.gateway_parts.request_context import (
    GatewayConversationContext,
    GatewayConversationLoadRequest,
    _GatewayLoadErrors,
    gateway_conversation_context,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_compact_scoped_checkpoint import compact_case as _compact_case
from agent_py_agent.tests.test_transcript_cold_rotation import _commit_rotation

compact_case = _compact_case


# LLM: Only a real tmp_path ConversationStore; no store readers/writers are replaced.
# 函数用途: 创建有一条完整消息的独立线程，保留可逐字核对的规范前缀。
@pytest.fixture
def session(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner-w1", "channel": "chat"})
    first = store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": "first"})
    return store, thread.thread_id, first


# LLM: Missing API is an assertion failure before implementation, not an import/collection error.
# 函数用途: 经公共封口入口测试真实存储行为。
def _seal(store, thread_id):
    seal = getattr(store.messages, "seal_torn_tail", None)
    assert callable(seal), "MessageStore must expose the locked seal_torn_tail entry point"
    return seal(thread_id)


# LLM: Fault injection only appends bytes to the fixture's transcript; committed prefix stays intact.
# 函数用途: 模拟一次未写完的尾行并返回故障前的字节。
def _tear(path, tail=b'{"content": "PRIVATE_TORN_CONTENT'):
    prefix = path.read_bytes()
    with path.open("ab") as handle:
        handle.write(tail)
    return prefix


# LLM: Quarantine preserves exact invalid bytes, inode and every committed byte; retries are no-ops.
# 函数用途: 确认不完整JSON、坏UTF-8及非对象尾行被私有留档，不再污染历史读取。
@pytest.mark.parametrize("tail", [b'{"content": "PRIVATE_TORN_CONTENT', b'\xff\xfe', b'[]', b'false', b'"text"'])
def test_torn_tail_is_quarantined_without_rewriting_prefix(session, caplog, tail):
    store, thread_id, first = session
    path = store.storage.message_path(thread_id)
    prefix = _tear(path, tail)
    inode = path.stat().st_ino
    with caplog.at_level(logging.WARNING):
        assert _seal(store, thread_id) == "quarantined"
    sidecars = list(path.parent.glob(f".{path.name}.torn-*"))
    assert len(sidecars) == 1
    assert re.fullmatch(rf"\.{re.escape(path.name)}\.torn-\d+", sidecars[0].name)
    assert sidecars[0].read_bytes() == tail
    if os.name == 'posix':
        assert stat.S_IMODE(sidecars[0].stat().st_mode) == 0o600
    assert path.read_bytes() == prefix and path.stat().st_ino == inode
    assert store.messages.recent_projection_report(thread_id, limit=80, project=lambda row: row.message_id) == ([first.message_id], [])
    assert "TRANSCRIPT_TORN_TAIL_SEALED" in caplog.text and "action=quarantined" in caplog.text
    assert "PRIVATE_TORN_CONTENT" not in caplog.text
    assert _seal(store, thread_id) == ""
    assert list(path.parent.glob(f".{path.name}.torn-*")) == sidecars


# LLM: A syntactically complete object is preserved even when the crash lost only its LF.
# 函数用途: 完整对象只补换行，不隔离或丢失原消息。
def test_complete_object_without_lf_stays_visible(session, caplog):
    store, thread_id, first = session
    path = store.storage.message_path(thread_id)
    original = path.read_bytes()
    path.write_bytes(original[:-1])
    with caplog.at_level(logging.WARNING):
        assert _seal(store, thread_id) == "completed"
    assert path.read_bytes() == original
    assert store.messages.recent_report(thread_id) == ([first], [])
    assert not list(path.parent.glob(f".{path.name}.torn-*"))
    assert "action=completed" in caplog.text


# LLM: Empty/missing/LF-terminated ledgers must not be rewritten or logged as repaired.
# 函数用途: 确认正常和缺失文件的封口是幂等空操作。
@pytest.mark.parametrize("state", ["missing", "empty", "complete"])
def test_seal_noop_does_not_rewrite_or_create_transcript(session, caplog, state):
    store, thread_id, _first = session
    path = store.storage.message_path(thread_id)
    if state == "missing":
        path.unlink()
    elif state == "empty":
        path.write_bytes(b"")
    before = (path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None
    assert _seal(store, thread_id) == ""
    after = (path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None
    assert before == after
    assert "TRANSCRIPT_TORN_TAIL_SEALED" not in caplog.text


# LLM: Each public writer must seal under its existing lock before writing or deciding dedupe.
# 函数用途: 防止普通、幂等和展示消息追加时与半行粘连，重试不重复写入。
@pytest.mark.parametrize("writer", ["append", "append_once", "append_display_checkpoint"])
def test_each_append_entry_preserves_its_own_parseable_line(session, writer):
    store, thread_id, first = session
    path = store.storage.message_path(thread_id)
    prefix = _tear(path)
    request = {"thread_id": thread_id, "role": "assistant", "content": "after tear"}
    if writer == "append_display_checkpoint":
        metadata = display_checkpoint_metadata(
            thread_id=thread_id, task_id="task-w1", request_id=f"bg-main:{thread_id}:piece-1",
            gateway_request_id="gw-w1", kind="thinking_completed", phase="completed",
            block_id=f"bg-main:{thread_id}:piece-1:thinking:1", payload={"text": "visible"},
        )
        entry = store.messages.append_display_checkpoint(thread_id, metadata)
    elif writer == "append_once":
        entry = store.messages.append_once(request, dedupe_key="dedupe-w1")
        assert store.messages.append_once(request, dedupe_key="dedupe-w1") == entry
    else:
        entry = store.messages.append(request)
    raw = path.read_bytes()
    rows = [json.loads(line) for line in raw.split(b"\n") if line]
    assert raw.startswith(prefix) and raw.endswith(b"\n")
    assert [row["message_id"] for row in rows] == [first.message_id, entry.message_id]
    assert store.messages.history_page_report(thread_id).errors == ()


# LLM: Real Gateway context loader runs before the user append; no model or service is launched.
# 函数用途: 重现回合开始读历史先于追加的入口，验证第一次用户追加就成功。
def test_gateway_context_seals_before_first_user_append(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", enable_tools=False, my_agent_home=str(tmp_path / "home")), tmp_path)
    request = {"conversation": {"channel": "chat", "channel_conversation_id": "session-w1",
                                "channel_user_id": "local-agent", "canonical_user_id": "local-agent"}}
    initial = gateway_conversation_context(GatewayConversationLoadRequest(agent, request, "gw-initial", "first"))
    store = agent.conversation_store
    store.messages.append({"thread_id": initial.thread_id, "role": "assistant", "content": "old answer"})
    path = store.storage.message_path(initial.thread_id)
    _tear(path)
    loaded = gateway_conversation_context(GatewayConversationLoadRequest(agent, request, "gw-next", "next"))
    assert loaded.thread_id == initial.thread_id and not loaded.load_errors
    assert request_history.append_gateway_conversation_message(
        agent, request, loaded, request_id="gw-next", role="user", content="next",
    ) is True
    rows, errors = store.messages.recent_report(initial.thread_id)
    assert not errors and [row.content for row in rows] == ["old answer", "next"]


# LLM: Corrupt owner-wide repair files cannot poison unrelated threads; exact bytes remain archived.
# 函数用途: 一个坏修复包被隔离，两个线程都不再被它阻断。
def test_bad_repair_file_is_quarantined_and_not_a_blocking_error(session, caplog):
    store, thread_id, _first = session
    other = store.threads.get_or_create({"canonical_user_id": "other-w1"})
    repair_dir = store.storage.root / "message_repairs"
    repair_dir.mkdir()
    bad = repair_dir / "x.json"
    bad.write_bytes(b"{")
    errors = _GatewayLoadErrors()
    with caplog.at_level(logging.WARNING):
        request_history.repair_gateway_conversation_messages(store, thread_id, errors)
    assert not errors and len(errors.warnings) == 1
    assert errors.warnings[0]["context"] == "gateway.conversation.repair.read"
    assert not bad.exists() and (repair_dir / ".ledger_archive" / bad.name).read_bytes() == b"{"
    assert "GATEWAY_REPAIR_FILE_QUARANTINED" in caplog.text
    later = _GatewayLoadErrors()
    request_history.repair_gateway_conversation_messages(store, other.thread_id, later)
    assert not later and not later.warnings


# LLM: Only invalid JSON/non-object rows may be skipped; surviving dedupe facts still prevent retries.
# 函数用途: 热账本坏行不挡首次追加，重试仍按已有事实去重。
@pytest.mark.parametrize("garbage", [b"not json\n", b"[1, 2]\n", b"\xff\n"])
def test_hot_garbage_does_not_block_or_duplicate_gateway_append(session, caplog, garbage):
    store, thread_id, _first = session
    path = store.storage.message_path(thread_id)
    with path.open("ab") as handle:
        handle.write(garbage)
    agent = SimpleNamespace(conversation_store=store)
    conversation = GatewayConversationContext(thread_id=thread_id)
    with caplog.at_level(logging.WARNING):
        for _retry in range(2):
            assert request_history.append_gateway_conversation_message(
                agent, {}, conversation, request_id="gw-after-garbage", role="user", content="after garbage",
            ) is True
    rows, errors = store.messages.recent_report(thread_id)
    assert [row.content for row in rows] == ["first", "after garbage"] and len(errors) == 1
    assert garbage in path.read_bytes()
    assert "JSONL_ROW_SKIPPED context=gateway.dedupe" in caplog.text


# LLM: Real cold rotation plus an appended bad hot row yields an incomplete window, never a safe miss.
# 函数用途: 轮转账本读失败仍拒绝追加，不把空事实列表当成可以重写的证明。
def test_rotated_garbage_keeps_dedupe_fail_closed(compact_case, monkeypatch):
    case = compact_case
    _thread, _rows, _original, path = _commit_rotation(case, monkeypatch)
    assert path.with_suffix(".manifest.json").exists()
    with path.open("ab") as handle:
        handle.write(b"not json\n")
    before = path.read_bytes()
    facts, errors = case.store.messages.recent_projection_report(case.thread_id, limit=100, project=lambda row: row.message_id)
    assert not facts and errors and all("line_number" not in error for error in errors)
    assert request_history.append_gateway_conversation_message(
        SimpleNamespace(conversation_store=case.store), {}, GatewayConversationContext(thread_id=case.thread_id),
        request_id="gw-rotated", role="user", content="must not append",
    ) is False
    assert path.read_bytes() == before


# LLM: Valid object model errors stay blocking, unlike no-identity JSON garbage.
# 函数用途: 模型解析错误不进入窄跳过白名单。
def test_valid_object_parse_error_still_blocks_dedupe(session):
    store, thread_id, _first = session
    path = store.storage.message_path(thread_id)
    bad = {"message_id": "bad-model", "thread_id": thread_id, "role": "user", "created_at": "not a number"}
    with path.open("ab") as handle:
        handle.write(json.dumps(bad).encode() + b"\n")
    before = path.read_bytes()
    assert request_history.append_gateway_conversation_message(
        SimpleNamespace(conversation_store=store), {}, GatewayConversationContext(thread_id=thread_id),
        request_id="gw-parse", role="user", content="must not append",
    ) is False
    assert path.read_bytes() == before


# LLM: Repair's 200-row dedupe window uses the same narrow rule and never replays tools.
# 函数用途: 坏热行不挡延迟补交，成功后移除修复包，重复读取不重复消息。
def test_repair_dedupe_skips_only_hot_garbage(session, caplog):
    store, thread_id, _first = session
    path = store.storage.message_path(thread_id)
    with path.open("ab") as handle:
        handle.write(b"garbage\n")
    repair_dir = store.storage.root / "message_repairs"
    repair_dir.mkdir()
    repair = repair_dir / "gw-repair-assistant.json"
    repair.write_text(json.dumps({"thread_id": thread_id, "role": "assistant", "content": "repaired answer",
                                  "metadata": {"gateway_request_id": "gw-repair", "assistant_part_id": "final"}}))
    errors = _GatewayLoadErrors()
    with caplog.at_level(logging.WARNING):
        request_history.repair_gateway_conversation_messages(store, thread_id, errors)
        request_history.repair_gateway_conversation_messages(store, thread_id, errors)
    assert not errors and not repair.exists()
    rows, row_errors = store.messages.recent_report(thread_id)
    assert [row.content for row in rows] == ["first", "repaired answer"] and len(row_errors) == 1
    assert "JSONL_ROW_SKIPPED context=gateway.dedupe" in caplog.text


# LLM: Context-only projection errors degrade only for the typed Gateway collector; plain lists stay compatible.
# 函数用途: 近期产物坏行转警告，同时维持普通列表调用方的错误语义。
def test_recent_artifacts_degrade_without_changing_plain_list_errors(session):
    store, thread_id, _first = session
    with store.storage.message_path(thread_id).open("ab") as handle:
        handle.write(b"garbage\n")
    agent = SimpleNamespace(conversation_store=store)
    plain = []
    warnings = _GatewayLoadErrors()
    assert request_history.gateway_recent_artifacts(agent, thread_id, "current", plain) == ()
    assert request_history.gateway_recent_artifacts(agent, thread_id, "current", warnings) == ()
    assert plain and not warnings and warnings.warnings == plain


# LLM: File I/O failure in the best-effort turn-start seal is reported, not raised or called repaired.
# 函数用途: 封口失败通过专用上下文报告，且不要求修复目录存在。
def test_turn_start_seal_failure_is_a_warning_without_repair_dir(session):
    store, thread_id, _first = session
    path = store.storage.message_path(thread_id)
    path.unlink()
    path.mkdir()
    errors = _GatewayLoadErrors()
    request_history.repair_gateway_conversation_messages(store, thread_id, errors)
    assert not errors and len(errors.warnings) == 1
    assert errors.warnings[0]["context"] == "gateway.conversation.tail_seal"


# LLM: A context-only exception before row iteration must also use the nonblocking warning channel.
# 函数用途: 路径校验异常只影响近期产物投影，不升级成会话阻断。
def test_recent_artifacts_exception_uses_warning_channel(session):
    store, _thread_id, _first = session
    errors = _GatewayLoadErrors()
    assert request_history.gateway_recent_artifacts(SimpleNamespace(conversation_store=store), "../invalid", "current", errors) == ()
    assert not errors and len(errors.warnings) == 1
    assert errors.warnings[0]["context"] == "gateway.conversation.recent_artifacts"


# LLM: Fixture injection uses real message shapes; the display burst and bad row are LF-terminated.
# 函数用途: 生成会挤满初始物理尾窗的展示行，坏行不能使更早的真实消息消失。
def _display_burst(path, template, count, bad_row=b"garbage\n"):
    with path.open("ab") as handle:
        for index in range(count):
            row = {**template.to_dict(), "message_id": f"display-{index}", "role": "display", "metadata": {}}
            handle.write(json.dumps(row).encode() + b"\n")
        handle.write(bad_row)


# LLM: JSON/non-object and model-parse errors remain reported, but cannot prevent physical-window expansion.
# 函数用途: 真实行不足时跨展示突发继续翻倍读取，普通与投影读口同窗同错。
@pytest.mark.parametrize("limit", [100, 200])
@pytest.mark.parametrize("fault", ["json", "nonobject", "parse"])
def test_display_burst_window_keeps_earlier_facts_and_errors(session, limit, fault):
    store, thread_id, first = session
    path = store.storage.message_path(thread_id)
    bad = {"message_id": "bad-model", "thread_id": thread_id, "role": "user", "created_at": "not a number"}
    raw = {"json": b"garbage\n", "nonobject": b"[]\n", "parse": json.dumps(bad).encode() + b"\n"}[fault]
    _display_burst(path, first, limit * 2 + 25, raw)
    before = path.read_bytes()
    rows, errors = store.messages.recent_report(thread_id, limit=limit)
    projected, projection_errors = store.messages.recent_projection_report(thread_id, limit=limit, project=lambda row: row.message_id)
    assert [row.message_id for row in rows] == [first.message_id]
    assert projected == [first.message_id] and projection_errors == errors and len(errors) == 1
    assert errors[0]["context"] == ("conversation.messages.parse" if fault == "parse" else "conversation.messages.read")
    assert path.read_bytes() == before


# LLM: Both user and assistant retries must discover the existing request through display-heavy garbage windows.
# 函数用途: 同一请求重试不再追加第二条规范消息，逐字保持原账本。
@pytest.mark.parametrize("role", ["user", "assistant"])
def test_display_burst_gateway_retry_does_not_duplicate(session, role):
    store, thread_id, _first = session
    existing = store.messages.append({"thread_id": thread_id, "role": role, "content": "existing",
                                      "metadata": {"gateway_request_id": "retry-w1", "assistant_part_id": "final"}})
    path = store.storage.message_path(thread_id)
    _display_burst(path, existing, 225)
    before = path.read_bytes()
    for _retry in range(2):
        assert request_history.append_gateway_conversation_message(
            SimpleNamespace(conversation_store=store), {}, GatewayConversationContext(thread_id=thread_id),
            request_id="retry-w1", role=role, content="existing",
        ) is True
    assert path.read_bytes() == before
    rows, errors = store.messages.recent_report(thread_id, limit=100)
    assert sum(row.metadata.get("gateway_request_id") == "retry-w1" for row in rows) == 1
    assert len(errors) == 1


# LLM: Repair has the larger dedupe window; an existing canonical assistant row wins over queued replay.
# 函数用途: 展示突发夹坏行时也只移除已落账的修复包，不重复补交。
def test_display_burst_repair_does_not_duplicate(session):
    store, thread_id, _first = session
    payload = {"thread_id": thread_id, "role": "assistant", "content": "existing answer",
               "metadata": {"gateway_request_id": "repair-w1", "assistant_part_id": "final"}}
    existing = store.messages.append(payload)
    path = store.storage.message_path(thread_id)
    _display_burst(path, existing, 425)
    before = path.read_bytes()
    repair_dir = store.storage.root / "message_repairs"
    repair_dir.mkdir()
    repair = repair_dir / "repair-w1-assistant.json"
    repair.write_text(json.dumps(payload))
    errors = _GatewayLoadErrors()
    request_history.repair_gateway_conversation_messages(store, thread_id, errors)
    assert not errors and not repair.exists() and path.read_bytes() == before


# LLM: Only the archival helper is fault-injected for its unexpected-exception branch; the store remains real.
# 函数用途: 一百个无法隔离的坏包不耗尽正常修复预算，抛异常也只进入警告。
@pytest.mark.parametrize("failure", ["blocked_directory", "exception"])
def test_failed_quarantine_does_not_starve_readable_repair(session, monkeypatch, failure):
    store, thread_id, _first = session
    repair_dir = store.storage.root / "message_repairs"
    repair_dir.mkdir()
    if failure == "blocked_directory":
        (repair_dir / ".ledger_archive").write_text("not a directory")
    else:
        def archive_failure(*_args):
            raise RuntimeError("injected archive failure")
        monkeypatch.setattr(request_history, "archive_ledger_file", archive_failure)
    for index in range(100):
        (repair_dir / f"a-{index:03}.json").write_text("{")
    valid = repair_dir / "z-valid.json"
    valid.write_text(json.dumps({"thread_id": thread_id, "role": "assistant", "content": "must progress",
                                "metadata": {"gateway_request_id": "starvation-w1"}}))
    errors = _GatewayLoadErrors()
    request_history.repair_gateway_conversation_messages(store, thread_id, errors)
    assert not errors and not valid.exists()
    rows, row_errors = store.messages.recent_report(thread_id)
    assert not row_errors and [row.content for row in rows] == ["first", "must progress"]
    assert len(list(repair_dir.glob("a-*.json"))) == 100
    assert sum(error["context"] == "gateway.conversation.repair.read" for error in errors.warnings) == 100
    assert sum(error["context"] == "gateway.conversation.repair.quarantine" for error in errors.warnings) == (100 if failure == "exception" else 0)


# LLM: Non-starvation must not remove the existing cap on readable repair work.
# 函数用途: 坏包不占预算，正常修复仍每次最多处理一百个。
def test_repair_keeps_one_hundred_readable_file_budget(session):
    store, thread_id, _first = session
    repair_dir = store.storage.root / "message_repairs"
    repair_dir.mkdir()
    for index in range(101):
        (repair_dir / f"readable-{index:03}.json").write_text(json.dumps({
            "thread_id": thread_id, "role": "assistant", "content": f"repair-{index}",
            "metadata": {"gateway_request_id": f"budget-{index}"},
        }))
    errors = _GatewayLoadErrors()
    request_history.repair_gateway_conversation_messages(store, thread_id, errors)
    assert not errors and not errors.warnings
    # 旧文件已原子迁入可排序队列；本轮仍仅消费100条，剩余一条下轮处理。
    from agent_py_agent.agent.gateway_parts.history_ledger import GatewayHistoryLedger
    with GatewayHistoryLedger(store).connection() as conn:
        assert conn.execute("SELECT count(*) FROM repairs WHERE thread=?", (thread_id,)).fetchone()[0] == 1
    assert not list(repair_dir.glob("*.json"))
    rows, row_errors = store.messages.recent_report(thread_id, limit=0)
    assert not row_errors and [row.content for row in rows] == ["first", *(f"repair-{index}" for index in range(100))]
