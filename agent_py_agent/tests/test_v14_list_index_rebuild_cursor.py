import json
import os
from pathlib import Path
from threading import Event, Thread

import pytest

from agent_py_agent.agent.conversation.session_list_index import SessionListIndex


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
