# LLM: 仅在pytest临时所有者检查冻结退休台账合同；不替换授权门或真实文件移动。
# 模块用途: 补充失败事务不发布证明、链接父级保护和非法记录的退休安全反证。
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agent_py_agent.agent.common.json_io import write_private_json_file_atomic
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.memory_store import retention_apply as apply
from agent_py_agent.agent.memory_store import retired_thread_journal as journal
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.retention import MemoryRetentionService
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home


# LLM: 真实临时home下生成一条字段合法但容器已不存在的记录，不借文件缺失伪造行结构。
# 函数用途: 装配四态判定的有效输入与隔离路径。
def _row(tmp_path):
    home = ensure_my_agent_home(tmp_path / "home")
    row = {"schema": "retired_thread.v1", "thread_id": "thread-one", "action_id": "retention-one",
           "conversation_root": "conversations", "trash_container": "trash/retention/conversation/one",
           "retired_at": 1.0}
    return home, row


# LLM: 无效字段不能产生正面purged证明；bool、NaN、路径上跳和非对象均须未知。
# 函数用途: 逐类反证非法退休行不会授权消费者删除侧状态。
@pytest.mark.parametrize("update", [
    {"schema": "future"}, {"thread_id": "../other"}, {"thread_id": "a/b"},
    {"retired_at": True}, {"retired_at": float("nan")}, {"retired_at": float("inf")},
    {"retired_at": -1}, {"action_id": ""}, {"conversation_root": "/other/conversations"},
    {"conversation_root": "conversations/../other"}, {"trash_container": "/other/trash"},
    {"trash_container": "../other"},
])
def test_invalid_row_is_never_positive_purge_proof(tmp_path, update):
    home, row = _row(tmp_path)
    assert journal.retired_thread_state(home, {**row, **update}) == "unknown"
    assert not journal.retired_thread_purged(home, {**row, **update})


# LLM: 父目录链接不能被解释为目标缺失；消费者必须保护未知路径，且不跟随到其他所有者。
# 函数用途: 验证退休容器与线程父目录的链接边界。
@pytest.mark.parametrize("level", ["trash", "conversations"])
def test_linked_state_parent_is_unknown(tmp_path, level):
    home, row = _row(tmp_path)
    owner = Path(home.owner_home_dir)
    external = tmp_path / "outside"
    external.mkdir()
    target = owner / level
    if target.exists():
        target.rmdir()
    target.symlink_to(external, target_is_directory=True)
    assert journal.retired_thread_state(home, row) == "unknown"
    assert not journal.retired_thread_purged(home, row)
    assert list(external.iterdir()) == []


# LLM: 失败发生在真实提交叶之前，原回滚必须恢复线程；台账不能先于提交成为删除权威。
# 函数用途: 验证未提交的会话回收不发布退休证明。
def test_failed_retirement_has_no_journal_row(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    home, _ = _row(tmp_path)
    store = ConversationStore(Path(home.owner_home_dir) / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "old", "channel": "internal",
        "channel_conversation_id": "old", "channel_user_id": "old", "now": 1.0})
    store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": "original", "now": 1.0})
    for name in ("channel_bindings.json", "user_latest_threads.json"):
        path = store.storage.root / name
        payload = json.loads(path.read_text())
        write_private_json_file_atomic(path, {key: value for key, value in payload.items() if value != thread.thread_id})
    service = MemoryRetentionService(home_paths=home, candidates=CandidateService(home.owner_memory_candidates_jsonl))

    # LLM: 只模拟最终墓碑提交的普通I/O失败，真实规划、移动和原回滚不变。
    # 函数用途: 在退休成功证明发布之前拒绝本次提交。
    def unavailable(*args, **kwargs):
        raise OSError("simulated retirement commit failure")

    monkeypatch.setattr(apply, "_commit_trash", unavailable)
    report = service.apply(now=datetime(2026, 11, 5, tzinfo=timezone.utc))
    assert any(action.record_id == thread.thread_id and action.status == "failed" for action in report.actions)
    assert store.threads.load(thread.thread_id) is not None
    assert store.storage.message_path(thread.thread_id).is_file()
    assert journal.iter_retired_threads(home).rows == ()


# LLM: 过深但有完整换行的JSON仍是坏完整行，必须前进并继续读后续有效记录。
# 函数用途: 反证解码递归错误不能永久卡住台账字节游标。
def test_deep_complete_line_does_not_block_next_record(tmp_path):
    home, row = _row(tmp_path)
    path = Path(home.owner_data_dir) / "retention/retired_threads.jsonl"
    path.parent.mkdir(parents=True)
    depth = max(sys.getrecursionlimit() * 2, 20000)
    path.write_bytes(b"[" * depth + b"0" + b"]" * depth + b"\n" + json.dumps(row).encode() + b"\n")
    page = journal.iter_retired_threads(home, max_rows=2)
    assert page.rows == (row,) and page.complete
    assert page.next_offset == path.stat().st_size
