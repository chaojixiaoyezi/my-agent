import json
import os
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.session_list_index import SessionListIndex
from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft
from agent_py_agent.agent.session.manager import SessionManager


def test_session_pagination_isolated_mutable_results_and_new_writer(tmp_path):
    config = SimpleNamespace(session_workspace=str(tmp_path / "sessions"), user_id="owner")
    manager = SessionManager(config)
    assert callable(getattr(manager, "list_page", None))
    sessions = [manager.create_session(metadata={"nested": {"x": n}}) for n in range(12)]
    first = manager.list_page(limit=5)
    assert first["coverage"] == "complete" and len(first["items"]) == 5
    first["items"][0].metadata["nested"]["x"] = "changed locally"
    assert manager.list_page(limit=5)["items"][0].metadata["nested"]["x"] != "changed locally"
    seen = [row.session_id for row in first["items"]]
    cursor = first["next_cursor"]
    while cursor:
        page = manager.list_page(limit=5, cursor=cursor)
        seen.extend(row.session_id for row in page["items"])
        cursor = page["next_cursor"]
    assert len(seen) == len(set(seen)) == len(sessions)
    other = SessionManager(config)
    other.delete_session(sessions[-1].session_id)
    assert sessions[-1].session_id not in {s.session_id for s in manager.list_page(limit=100)["items"]}
    hidden = other.create_session(user_id="foreign")
    assert hidden.session_id not in {s.session_id for s in manager.list_page(limit=100)["items"]}


def test_task_pagination_only_loads_selected_bodies_after_rebuild(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations").session_tasks
    assert callable(getattr(store, "list_page", None))
    tasks = [store.create(SessionTaskDraft("A", "B", str(n)), now=float(n)) for n in range(140)]
    page = store.list_page(limit=5)
    while page["coverage"] == "partial":
        page = store.list_page(limit=5)
    assert [t.task_id for t in page["items"]] == [t.task_id for t in reversed(tasks[-5:])]
    import agent_py_agent.agent.conversation.session_tasks as module
    read = module.read_json_file_report
    reads = []

    def observe(path, **kwargs):
        reads.append(path)
        return read(path, **kwargs)

    monkeypatch.setattr(module, "read_json_file_report", observe)
    again = store.list_page(limit=5)
    assert len(again["items"]) == 5
    assert len(reads) <= 7
    assert [t.task_id for t in store.list_report(limit=5)[0]] == [t.task_id for t in tasks[-5:]]


def test_old_writer_inplace_reorder_is_detected_without_directory_mtime(tmp_path):
    import json

    from agent_py_agent.agent.conversation.session_list_index import SessionListIndex

    root = tmp_path / "records"
    root.mkdir()
    def read(path):
        return json.loads(path.read_text()), None
    for n in range(8):
        (root / f"{n}.json").write_text(json.dumps({"task_id": str(n), "created_at": n, "target_thread_id": "B"}))
    index = SessionListIndex(root, kind="session_task", reader=read)
    assert index.page(limit=1)["items"][0]["task_id"] == "7"
    directory = root.stat().st_mtime_ns
    path = root / "0.json"
    with path.open("r+") as handle:
        data = json.load(handle)
        data["created_at"] = 9
        handle.seek(0)
        json.dump(data, handle)
        handle.truncate()
    assert root.stat().st_mtime_ns == directory
    seen = []
    for _ in range(8):
        seen.append(index.page(limit=1)["items"][0]["task_id"])
    assert "0" in seen


def test_corrupt_disposable_index_rebuilds_canonical_and_expires_old_cursor(tmp_path):
    import json

    from agent_py_agent.agent.conversation.session_list_index import SessionListIndex

    root = tmp_path / "records"
    root.mkdir()
    for n in range(4):
        (root / f"{n}.json").write_text(json.dumps({"task_id": str(n), "created_at": n}))
    index = SessionListIndex(root, kind="session_task", reader=lambda p: (json.loads(p.read_text()), None))
    first = index.page(limit=2)
    index.path.write_bytes(b"not a sqlite database")
    assert index.page(limit=2, cursor=first["next_cursor"])["coverage"] == "expired"
    assert [t["task_id"] for t in index.page(limit=4)["items"]] == ["3", "2", "1", "0"]


# LLM: 只造临时 canonical 文件；使用真实目录顺序，不替换重建或连接逻辑。
# 函数用途: 让晚发现的一批排在旧游标之前，包含同时间戳和过滤条件。
def _records(tmp_path, count, equal):
    root = tmp_path / "records"
    root.mkdir()
    for n in range(count):
        (root / f"t{n:04}.json").write_text("{}")
    with os.scandir(root) as entries:
        paths = [Path(entry.path) for entry in entries]
    for n, path in enumerate(paths):
        path.write_text(json.dumps({"thread_id": path.stem, "owner_id": "owner",
                                    "updated_at": n // 3 if equal else n}))
    return root, {p.stem for p in paths}


# LLM: 每个被接受的遍历须完整或显式失效，失效后允许从第一页重新开始。
# 函数用途: 有界地走完真实索引，拒绝遗漏后的 complete。
def _traverse(index, expected):
    cursor, seen, expired = None, [], 0
    for _ in range(40):
        page = index.page(limit=50, cursor=cursor, filter_key="owner")
        if page["coverage"] == "expired":
            expired += 1
            cursor, seen = None, []
            continue
        seen.extend(item["thread_id"] for item in page["items"])
        assert len(seen) == len(set(seen))
        cursor = page["next_cursor"]
        if cursor is None and page["coverage"] == "complete":
            assert set(seen) == expected
            return expired
        if cursor is None:
            seen = []
    pytest.fail("stable rebuild did not finish a complete traversal")


# LLM: 覆盖整批及尾批，只接受完整遍历或明确游标失效。
# 函数用途: 验证后续回填批次不会静默漏行。
@pytest.mark.parametrize("count", [256, 289])
@pytest.mark.parametrize("equal", [False, True])
def test_rebuild_expires_cursor_before_new_batch_can_skip_rows(tmp_path, count, equal):
    root, expected = _records(tmp_path, count, equal)
    index = SessionListIndex(root, kind="thread", reader=lambda p: (json.loads(p.read_text()), None))
    assert _traverse(index, expected) >= 1
    assert _traverse(index, expected) == 0


# LLM: 使用真实 canonical 和原索引事务，不替代 page 或 publish。
# 函数用途: 验证回填期间发布会使已有游标失效。
def test_publish_interleaved_with_rebuild_expires_issued_cursor(tmp_path):
    root, expected = _records(tmp_path, 256, False)
    index = SessionListIndex(root, kind="thread", reader=lambda p: (json.loads(p.read_text()), None))
    first = index.page(limit=50, filter_key="owner")
    publish, finished = Event(), Event()

    # LLM: Event 固定页发出后的发布时机，修订由产品代码推进。
    # 函数用途: 在已有游标后发布新成员。
    def writer():
        assert publish.wait(5)
        path = root / "new.json"
        path.write_text(json.dumps({"thread_id": "new", "owner_id": "owner", "updated_at": 999}))
        index.publish(path)
        finished.set()

    thread = Thread(target=writer)
    thread.start()
    publish.set()
    assert finished.wait(5)
    thread.join(5)
    assert not thread.is_alive()
    assert index.page(limit=50, cursor=first["next_cursor"], filter_key="owner")["coverage"] == "expired"
    _traverse(index, expected | {"new"})
