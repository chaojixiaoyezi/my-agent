"""RES-W4: operator copies are not records; canonical faults remain visible."""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
from functools import partial
from pathlib import Path

import pytest

from agent_py_agent.agent.common.opaque_id import OPAQUE_ID_MAX_LENGTH_CHARS
from agent_py_agent.agent.conversation import ConversationStore, store_index
from agent_py_agent.agent.conversation.message_selection import select_message_snapshot
from agent_py_agent.agent.conversation.models import new_id
from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft
from agent_py_agent.agent.conversation.store_layout import ConversationStorage


# LLM: Only temporary owner roots are used; retain the production nested workspace layout.
# 函数用途: 创建真实目录布局和一个已绑定会话，便于缓存对象保持存活的回归测试。
def _store(tmp_path):
    owner = tmp_path / "owner"
    root = owner / "workspace/runtime/workspaces/scope0/conversations"
    store = ConversationStore(root, compact_root=owner / "compact")
    binding = {
        "canonical_user_id": "owner", "channel": "internal",
        "channel_conversation_id": "one", "channel_user_id": "owner", "now": 1.0,
    }
    thread = store.threads.get_or_create(binding)
    return store, thread.thread_id, binding


# LLM: Read real serialized records but create only non-canonical copies in the test root.
# 函数用途: 模拟操作员备份、编辑器临时文件和手工副本。
def _stray_copies(path):
    names = (
        f"{path.stem} copy{path.suffix}", f"{path.stem}.bak{path.suffix}",
        f"{path.stem}.old{path.suffix}", f"{path.name}.bak",
        f"{path.name}~", f".{path.name}.tmp",
    )
    for name in names:
        (path.parent / name).write_bytes(path.read_bytes())
    return names


# LLM: Exercise all three real ScanIndex consumers, including both wake priorities.
# 函数用途: 证明副本不参与冷读或热读，也不修改权威记录。
@pytest.mark.parametrize("kind", ("urgent", "normal", "policy", "observation"))
def test_scans_ignore_operator_copies(tmp_path, kind, caplog):
    store, tid, _binding = _store(tmp_path)
    if kind in {"urgent", "normal"}:
        record = store.wakes.raise_signal({"thread_id": tid, "urgency": kind})
        path = store.storage.wake_signal_path(record)
        query = partial(store.wakes.pending_report, limit=0)
    elif kind == "policy":
        record = store.progress.create({"thread_id": tid, "interval_seconds": 300})
        path = store.storage.policy_path(record.policy_id)
        query = store.progress.list_report
    else:
        store.observations.append({
            "thread_id": tid, "event_type": "child_agent_event", "summary": "seed",
            "requires_main_agent": True,
        })
        path = store.storage.observation_path(tid)
        query = partial(store.observations.recent_report, tid, limit=0)
    before = path.read_bytes()
    names = _stray_copies(path)
    caplog.set_level(logging.DEBUG, logger="agent.conversation.store")
    for _ in range(2):
        assert store_index._scan_dir_match_names(path.parent, "*" + path.suffix) == frozenset({path.name})
        rows, errors = query()
        assert len(rows) == 1
        assert errors == []
        if kind == "observation":
            assert len(store.observations.unhandled_requiring_main(limit=0)) == 1
    assert path.read_bytes() == before
    assert all((path.parent / name).read_bytes() == before for name in names)
    assert any(record.levelno == logging.DEBUG for record in caplog.records)
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)


# LLM: Canonical names retain strict error handling, even when a record is not a regular file.
# 函数用途: 坏 JSON、同名目录和断链仍产生错误，不能被文件类型过滤吞掉。
@pytest.mark.parametrize("fault", ("json", "directory", "symlink"))
def test_broken_canonical_wake_still_reports_load_error(tmp_path, fault):
    store, tid, _binding = _store(tmp_path)
    signal = store.wakes.raise_signal({"thread_id": tid})
    path = store.storage.wake_signal_path(signal)
    path.unlink()
    if fault == "json":
        path.write_text("{")
    elif fault == "directory":
        path.mkdir()
    else:
        path.symlink_to(path.parent / "missing-target")
    rows, errors = store.wakes.pending_report(limit=0)
    assert rows == []
    assert len(errors) == 1
    assert errors[0]["path"] == str(path)


# LLM: No type probes belong in listing; a canonical FIFO must be listed without opening it.
# 函数用途: 只枚举 FIFO，避免读取会阻塞的管道，同时守住禁止类型过滤的合同。
def test_canonical_fifo_is_listed_without_reading(tmp_path):
    import os

    path = tmp_path / "wake-pipe.json"
    os.mkfifo(path)
    assert store_index._scan_dir_match_names(tmp_path, "*.json") == frozenset({path.name})


# LLM: The production ID generator and digest paths must not lose valid records.
# 函数用途: 覆盖各类生成 ID、摘要和规范 ID 长度边界。
@pytest.mark.parametrize("suffix", (".json", ".jsonl"))
def test_generated_ids_and_length_boundaries_are_canonical(suffix):
    ids = [new_id(prefix) for prefix in ("wake", "policy", "thread", "obs", "goal", "msg", "guidance", "task")]
    ids += [hashlib.sha256(b"record").hexdigest(), "a" * OPAQUE_ID_MAX_LENGTH_CHARS]
    assert all(store_index._canonical_scan_name(stem + suffix, "*" + suffix) for stem in ids)
    for stem in ("", "a" * (OPAQUE_ID_MAX_LENGTH_CHARS + 1), "a.b", "a b", "a~", "_a", "-a", "中文"):
        assert not store_index._canonical_scan_name(stem + suffix, "*" + suffix)


# LLM: General fnmatch patterns are outside the opaque-stem rule and keep their prior behavior.
# 函数用途: 非单星后缀模式不能被悄悄收紧。
@pytest.mark.parametrize("pattern", ("*", "wake-*.json", "*?.json", "*[ab].json", "**.json", "exact.json"))
def test_other_patterns_keep_original_matching(tmp_path, pattern):
    from fnmatch import fnmatchcase

    for name in ("a copy.json", "wake-x.bak.json", "exact.json", "a.json", "note.txt"):
        (tmp_path / name).touch()
    expected = frozenset(p.name for p in tmp_path.iterdir() if fnmatchcase(p.name, pattern))
    assert store_index._scan_dir_match_names(tmp_path, pattern) == expected


# LLM: Directory-listing failure keeps the legacy glob fallback, including non-canonical names.
# 函数用途: 不可枚举目录的 fallback 保持原样，不把新过滤扩到合同之外。
def test_unlistable_directory_keeps_glob_fallback(tmp_path, monkeypatch):
    path = tmp_path / "wake-x copy.json"
    path.touch()
    monkeypatch.setattr(store_index, "_scan_dir_match_names", lambda *_args: None)
    assert store_index.ScanIndex("fallback").ordered_paths(tmp_path, "*.json") == [path]


# LLM: Use an actual same-inode rewrite after warming both selection and dedupe caches.
# 函数用途: 截断或修改中间行后，只返回磁盘上的消息，不返回已失效缓存。
@pytest.mark.parametrize("mode", ("truncate_zero_inplace", "older_prefix_inplace", "edited_middle_inplace"))
def test_inplace_rewrite_invalidates_selection_and_dedupe(tmp_path, mode):
    store, tid, _binding = _store(tmp_path)
    original = [store.messages.append_once({"thread_id": tid, "role": "user", "content": f"row-{n}"}, dedupe_key=f"K{n}") for n in range(6)]
    path = store.storage.message_path(tid)
    assert [row.content for row in select_message_snapshot(store.messages, tid)] == [f"row-{n}" for n in range(6)]
    inode = path.stat().st_ino
    lines = path.read_bytes().splitlines(keepends=True)
    if mode == "truncate_zero_inplace":
        rewritten = b""
    elif mode == "older_prefix_inplace":
        rewritten = b"".join(lines[:2])
    else:
        rewritten = b"".join(lines[:3] + [lines[3].replace(b"row-3", b"ROW-3")] + lines[4:])
    with path.open("r+b") as handle:
        handle.write(rewritten)
        handle.truncate()
    assert path.stat().st_ino == inode
    expected = [json.loads(line)["content"] for line in rewritten.splitlines()]
    assert [row.content for row in select_message_snapshot(store.messages, tid)] == expected
    for n in (1, 3, 5):
        present = mode == "edited_middle_inplace" or (mode == "older_prefix_inplace" and n == 1)
        content = "ROW-3" if mode == "edited_middle_inplace" and n == 3 else f"row-{n}"
        row = store.messages.append_once({"thread_id": tid, "role": "user", "content": content}, dedupe_key=f"K{n}")
        assert (row.message_id == original[n].message_id) is present
        assert row.content == content
        if not present:
            expected.append(content)
    assert [row.content for row in select_message_snapshot(store.messages, tid)] == expected
    disk = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row.content for row in select_message_snapshot(store.messages, tid)] == [row["content"] for row in disk]
    assert store.messages.recent_report(tid, limit=0)[1] == []


# LLM: Resolve the current managed directory inventory during test collection, without initializing it.
# 函数用途: 自动把新增目录纳入删除矩阵；不在模块级持有可变容器。
def _deletion_targets():
    storage = ConversationStorage(Path("conversations"), initialize=False)
    managed = tuple(str(path.relative_to(storage.root)) for path in storage.managed_dirs())
    return managed + (
        "channel_bindings.json", "user_latest_threads.json", "wake_queue",
        "wake_queue/task_locks", "wake_queue/task_responsibility.sqlite3",
        "wake_queue/task_responsibility.guard", ".",
    )


# LLM: Exercise the real receipt, input/turn indexes, submission and acknowledgement writers.
# 函数用途: 写完一条完整 guidance 生命周期，让所有派生目录都有实际记录。
def _write_guidance(store, tid, tag):
    turn = f"turn-{tag}"
    entry = store.guidance.append_once({
        "target_type": "thread", "target_id": tid, "message": tag,
        "metadata": {"expected_turn_id": turn, "gateway_input_request_id": f"input-{tag}"},
    }, dedupe_key=f"guidance-{tag}")
    assert store.guidance.claim_for_turn(entry, expected_turn_id=turn, attempt_id=f"attempt-{tag}")
    assert store.guidance.submissions.mark_submitted(turn, [entry], attempt_id=f"attempt-{tag}", provider_call_id=f"call-{tag}") == (entry.guidance_id,)
    assert store.guidance.acknowledgements.consume_submitted(turn, [entry], provider_call_id=f"call-{tag}") == (entry.guidance_id,)
    assert store.guidance.recent_report("thread", tid, limit=0)[1] == []


# LLM: Every deletion target maps to its own production writer and matching reader, not ensure_dirs().
# 函数用途: 用各领域自身的写入口修复缺失目录，不让集中初始化掩盖写路径问题。
def _write_record(store, tid, kind, tag):
    request = {"thread_id": tid, "now": 100.0 if tag == "seed" else 10_000.0}
    if kind == "messages":
        store.messages.append({**request, "role": "user", "content": tag})
        assert store.messages.recent_report(tid, limit=0)[1] == []
    elif kind == "model_usage":
        store.model_usage.append_once({**request, "event_id": f"usage-{tag}", "request_id": f"request-{tag}", "source": "test", "model_calls": {"schema": "model_call_summary.v1", "output_tokens": 1}})
        assert store.model_usage.events_report(tid)[1] == []
    elif kind == "tasks":
        store.tasks.bind({**request, "task_id": f"task-{tag}", "title": tag})
        link = store.tasks.update_status({**request, "task_id": f"task-{tag}", "status": "done"})
        assert link.status == "done"
        # Task reader resilience is owned by RES-W5, not this directory recreation test.
    elif kind in {"session_tasks", "session_tasks/dedupe"}:
        store.session_tasks.create(SessionTaskDraft(tid, tid, tag, dedupe_key=f"session-{tag}"))
        assert store.session_tasks.list_report(limit=0)[1] == []
    elif kind == "session_pair_rate":
        assert store.session_pair_rate.record(tid, "other-thread", now=request["now"]) >= 1
        assert store.session_pair_rate.count(tid, "other-thread", now=request["now"]) >= 1
    elif kind == "progress_policies":
        store.progress.create({**request, "interval_seconds": 300})
        assert store.progress.list_report()[1] == []
    elif kind == "observations":
        store.observations.append({**request, "event_type": "child_agent_event", "summary": tag, "requires_main_agent": True})
        assert store.observations.recent_report(tid, limit=0)[1] == []
    elif kind.startswith("guidance"):
        _write_guidance(store, tid, tag)
    elif kind == "goals":
        store.goals.create({**request, "objective": tag})
        assert store.goals.list_report(tid)[1] is None
    elif kind == "background_claims":
        assert store.claims.acquire({**request, "task_id": f"task-{tag}"}) is not None
        assert store.claims.load_report(tid)[1] is None
    elif kind.startswith("wake_queue"):
        if kind in {"wake_queue/task_locks", "wake_queue/task_responsibility.guard", "wake_queue/task_responsibility.sqlite3"}:
            with store.wakes.task_responsibility_guard(tid, f"task-{tag}"):
                assert store.storage.wake_task_index_lock_path.exists()
        else:
            signal = store.wakes.raise_signal({
                **request, "urgency": "normal" if kind.endswith("/normal") else "urgent",
                "reason": "urgent_child_event", "root_task_id": f"task-{tag}",
                "dedupe_key": f"{kind}-{tag}",
            })
            if kind.endswith("/handled"):
                handled = store.wakes.mark_handled(signal.wake_signal_id, now=request["now"] + 1)
                assert handled.status == "handled"
        assert store.wakes.pending_report(limit=0)[1] == []
    else:
        pytest.fail(f"A managed directory needs an explicit writer/reader case: {kind}")


# LLM: Seed every required domain and real on-disk derived artifacts before any deletion.
# 函数用途: 预热同一个存储对象及其多种缓存，避免只测空库初始化。
def _seed_history(store, tid):
    for kind in (
        "messages", "model_usage", "tasks", "session_tasks", "session_pair_rate",
        "progress_policies", "observations", "guidance", "goals", "background_claims",
        "wake_queue/urgent", "wake_queue/normal", "wake_queue/handled", "wake_queue/task_locks",
    ):
        _write_record(store, tid, kind, "seed")
    assert store.messages.recent_report(tid, limit=0)[0]
    assert store.wakes.pending_report(limit=0)[0]
    assert store.progress.list_report()[0]
    assert store.observations.unhandled_requiring_main(limit=0)


# LLM: Keep the same cached store while deleting one exact target; rebind only when authority is lost.
# 函数用途: 验证各目录和索引文件丢失后按需重建，保留其他记录并支持新建 Store 读取。
@pytest.mark.parametrize("target", _deletion_targets())
def test_cached_store_self_heals_runtime_deletion(tmp_path, target):
    store, tid, binding = _store(tmp_path)
    _seed_history(store, tid)
    root = store.storage.root
    deleted = root / target
    assert deleted.exists(), f"Seed must create the actual artifact: {target}"
    transcript = store.storage.message_path(tid)
    transcript_bytes = transcript.read_bytes()
    old_task = store.storage.task_path("task-seed")
    task_bytes = old_task.read_bytes()
    sentinel = root / "surviving-sibling.keep"
    sentinel.write_bytes(b"preserve surviving records")
    if deleted.is_dir():
        shutil.rmtree(deleted)
    else:
        deleted.unlink()
    assert not deleted.exists()

    rebind = target in {"threads", "channel_bindings.json", "."}
    if target in {"threads", "channel_bindings.json", "user_latest_threads.json", "."}:
        current = store.threads.get_or_create(dict(binding))
        assert (current.thread_id != tid) is rebind
        write_tid = current.thread_id
        assert deleted.exists()
        assert store.threads.list_report(limit=0)[1] == []
    else:
        write_tid = tid
        _write_record(store, tid, target, "after")
        assert deleted.exists()
        assert store.threads.get_or_create(dict(binding)).thread_id == tid

    if target == ".":
        _seed_history(store, write_tid)
    else:
        assert sentinel.read_bytes() == b"preserve surviving records"
        if target != "messages":
            assert transcript.read_bytes() == transcript_bytes
        if target != "tasks":
            assert old_task.read_bytes() == task_bytes
    fresh = ConversationStore(root, compact_root=store.storage.compact_root)
    assert fresh.threads.get_or_create(dict(binding)).thread_id == write_tid
    fresh.messages.append({"thread_id": write_tid, "role": "user", "content": "after restart"})
    assert fresh.messages.recent_report(write_tid, limit=0)[1] == []
