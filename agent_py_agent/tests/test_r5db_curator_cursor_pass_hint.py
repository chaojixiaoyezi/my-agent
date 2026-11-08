# LLM: Astra R5B-04 回归：Curator 单线程多页续读热 transcript 时不再每页从 byte 0 定位上一页末条 ID；
#   提示只活在一次多页读取内，仍须同 inode、只增长或指纹相同并逐字节复核锚行；作用域外照旧不缓存热文件位置。
# 模块用途: 以 _scan_cursor 调用计数钉住续页定位次数，并钉住同长改写、换 inode、锚行改写都退回完整扫描。

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation import message_cursor
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.memory_store.curator_inputs import CuratorMessageSource, _collect_messages


@pytest.fixture
def hot_scans(monkeypatch):
    calls = {"scan": 0}
    real = message_cursor._scan_cursor

    def counting(*args, **kwargs):
        calls["scan"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(message_cursor, "_scan_cursor", counting)
    # 模拟持续写入中的热 transcript：粗时间信任窗口永远未过。
    monkeypatch.setattr(message_cursor, "cache_entry_trustworthy", lambda _mtime_ns: False)
    return calls


def _thread(tmp_path: Path, excluded: int):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({
        "canonical_user_id": "user-1", "channel": "internal", "channel_conversation_id": "chat-1",
        "channel_user_id": "user-1", "now": 10.0,
    })
    first = store.messages.append(dict(thread_id=thread.thread_id, role="user", content="start"))
    for index in range(excluded):
        store.messages.append(dict(thread_id=thread.thread_id, role="user", content=f"skip-{index}",
                                   metadata={"learning_excluded": True}))
    last = store.messages.append(dict(thread_id=thread.thread_id, role="user", content="normal tail"))
    return store, thread, first, last


# 函数用途: 13 页续读同一热线程只在首页定位一次，选出的消息与跳过游标不变。
def test_curator_pages_locate_hot_transcript_once(tmp_path: Path, hot_scans) -> None:
    store, thread, first, last = _thread(tmp_path, excluded=300)
    skipped: dict[str, str] = {}
    rows, errors = _collect_messages(source=CuratorMessageSource(store), cursors={thread.thread_id: first.message_id},
                                     limit=25, max_chars=200_000, skipped_cursors=skipped)
    assert not errors
    assert [row.message_id for row in rows] == [last.message_id]
    assert hot_scans["scan"] == 1


def _rows(store, thread):
    path = store.storage.message_path(thread.thread_id)
    rows, errors = message_cursor.read_after_rows(path, "", 1000)
    assert not errors
    return path, [row["message_id"] for row in rows]


# 函数用途: 作用域外热文件位置不缓存（每次定位都冷扫），作用域内复用；同长改写、换 inode 与锚行改写都退回冷扫。
@pytest.mark.parametrize("mutation", ["none", "same_size_rewrite", "replaced_inode", "anchor_rewrite"])
def test_pass_hint_is_scoped_and_revalidated(tmp_path: Path, hot_scans, mutation: str) -> None:
    store, thread, _first, _last = _thread(tmp_path, excluded=5)
    path, ids = _rows(store, thread)
    target = ids[3]
    message_cursor.locate_message_cursor(path, target)
    message_cursor.locate_message_cursor(path, target)
    assert hot_scans["scan"] == 2
    with message_cursor.pass_position_scope():
        message_cursor.read_after_rows(path, "", 1000)
        data = path.read_bytes()
        if mutation == "same_size_rewrite":
            path.write_bytes(data.replace(b"skip-0", b"SKIP-0"))
        elif mutation == "replaced_inode":
            path.unlink()
            path.write_bytes(data)
        elif mutation == "anchor_rewrite":
            path.write_bytes(data.replace(target.encode(), target[:-1].encode() + b"x") + data[-40:])
        cursor = None
        try:
            cursor = message_cursor.locate_message_cursor(path, target)
        except Exception:  # noqa: BLE001 锚行被改写时冷扫找不到原 ID，按原合同报损坏
            assert mutation == "anchor_rewrite"
        expected = 2 if mutation == "none" else 3
        assert hot_scans["scan"] == expected
        if cursor is not None:
            with path.open("rb") as handle:
                handle.seek(cursor.start)
                assert target.encode() in handle.readline()
    assert os.path.exists(path)
