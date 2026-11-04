"""R1：一个目标的坏队列只让本目标的修复标记失败并保留，不能连带其它目标读不到插话；真实 guidance 账本。"""
import json

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.store_guidance_records import _guidance_entry_from_request
from agent_py_agent.agent.runtime_errors import DataCorruptionError


# LLM: 旧版本留下的队列残行：缺行尾换行（追加中断）或单行超过1 MiB，严格切片读取器都拒收。
# 函数用途: 生成一条会被严格切片读取器拒绝的旧队列行字节。
def _legacy_residue(target_id, residue):
    row = _guidance_entry_from_request({"target_type": "thread", "target_id": target_id, "message": "旧消息"}).to_dict()
    if residue == "incomplete":
        return json.dumps(row).encode()
    row["message"] = "x" * (1 << 20)
    return json.dumps(row).encode() + b"\n"


@pytest.mark.parametrize("residue", ["incomplete", "oversized"])
def test_failing_marker_for_one_target_keeps_other_targets_readable(tmp_path, residue):
    store = ConversationStore(tmp_path / "conversations")
    guidance = store.guidance
    index = guidance.ledger.pending_index()
    other = guidance.append_once({"target_type": "thread", "target_id": "thread-b", "message": "给B的补充"}, dedupe_key="b-1")
    queue = store.storage.guidance_path("thread", "thread-a")
    queue.write_bytes(_legacy_residue("thread-a", residue))
    with pytest.raises(DataCorruptionError):
        guidance.append_once({"target_type": "thread", "target_id": "thread-a", "message": "给A的补充"}, dedupe_key="a-1")
    receipt_path = store.storage.guidance_dedupe_path("a-1")
    assert json.loads(receipt_path.read_text())["status"] == "pending", "canonical receipt must stay committed"
    marker = index.dirty_path(receipt_path)
    assert list(index.dirty_dir.glob("*.json")) == [marker]

    assert [entry.guidance_id for entry in guidance.pending("thread", "thread-b")] == [other.guidance_id]
    entries, errors = guidance.recent_report("thread", "thread-b")
    assert not errors and [entry.guidance_id for entry in entries] == [other.guidance_id]
    entries, errors = guidance.recent_report("thread", "thread-a")
    assert entries == [] and errors, "the failing target must keep reporting its repair error"
    assert marker.exists(), "a failing repair must keep its marker"

    # 运维移除坏行后，A的标记在下一次读取时修复并清除，原回执身份不变。
    queue.write_bytes(b"")
    entries, errors = guidance.recent_report("thread", "thread-a")
    expected = json.loads(receipt_path.read_text())["entry"]["guidance_id"]
    assert not errors and [entry.guidance_id for entry in entries] == [expected]
    assert not marker.exists()
    assert [entry.guidance_id for entry in guidance.pending("thread", "thread-a")] == [expected]
