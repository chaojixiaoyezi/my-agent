# LLM: 只用 pytest 临时 owner、真实 ConversationStore 冷转（产品原 128 行阈值）与真实插件领域入口；
#   不改游标、授权或分页算法，冷段损坏只在临时目录模拟一次。
# 模块用途: 回归 P1-R9-07——插件历史与请求回复读取 canonical 冷段+热尾，冻结快照，坏冷段不当作空成功。
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.context_compactor import RuntimeCompactPolicy
from agent_py_agent.agent.conversation import compact_checkpoint as checkpoints
from agent_py_agent.agent.conversation.models import ConversationCompactCommit
from agent_py_agent.agent.plugin_host import history
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_host.schemas import thaw
from agent_py_agent.tests.test_plugin_host_v2_conversations import case as _case
from agent_py_agent.tests.test_plugin_host_v2_requests import _public_call, _public_request
from agent_py_agent.tests.test_plugin_host_v2_requests import public_case as _public_case

case = _case
public_case = _public_case
# 超过产品原 128 行冷转阈值；每行很短，按行数触发而不是缩小阈值。
ROWS = 140
POLICY = RuntimeCompactPolicy(
    context_window_tokens=1000, trigger_percent=90, trigger_tokens=900,
    recovery_target_percent=60, recovery_target_tokens=600,
    allow_persistent_apply=True, recent_tail_max_turns=4, recent_tail_tokens=100,
    failure_threshold=3, failure_cooldown_seconds=300.0,
)


# LLM: 走真实 Compact 检查点与线程 CAS，冷转由原 writer 在消息锁内完成；不替换阈值或覆盖算法。
# 函数用途: 在已有消息末尾补足行数后提交一次压缩，使旧前缀进入冷段，返回热文件路径与原始完整字节。
def _rotate(store, tid, *, turn_id="turn-one"):
    rows = [store.messages.append({
        "thread_id": tid, "role": "user" if number % 2 == 0 else "assistant",
        "content": f"历史第{number}行", "metadata": {"turn_id": turn_id},
    }) for number in range(ROWS)]
    path = store.storage.message_path(tid)
    original = path.read_bytes()
    every, errors = store.messages.recent_report(tid, limit=0)
    assert not errors
    boundary = store.messages.byte_offset_after(tid, rows[-2].message_id)
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_compact_dir=store.storage.resolve_compact_root()),
                            backend=SimpleNamespace(name="test", model_name="test-model"))
    thread = store.threads.require(tid)
    checkpoint_id = checkpoints.write_compact_checkpoint(agent, checkpoints.CompactCheckpointRequest(
        thread=thread, summary="已覆盖旧行", operation_evidence={}, compact_rows=tuple(every[:-1]),
        retained_tail=(every[-1],), source_end_byte_offset=boundary,
        projected_tokens_before=900, projected_tokens_after=300, policy=POLICY, forced=False,
    ))
    store.threads.update_compact_state(tid, commit=ConversationCompactCommit(
        summary="已覆盖旧行", operation_evidence={}, checkpoint_id=checkpoint_id,
        compacted_through_message_id=rows[-2].message_id, compacted_through_byte_offset=boundary,
        source_messages=len(every) - 1, source_tool_pairs=0,
    ), expected_generation=thread.compact_generation)
    manifest = json.loads(path.with_suffix(".manifest.json").read_bytes())
    assert manifest["segment_count"] >= 1 and len(path.read_bytes()) < len(original)
    return path, original


# LLM: 只经公开 messages.list 与真实游标续页收集，不读取内部存储。
# 函数用途: 按给定页大小读完整段可见历史，返回全部公开行与页数。
def _all_pages(case, tid, limit):
    items, pages, cursor = [], [], None
    while True:
        payload = {"thread_id": tid, "limit": limit, **({"cursor": cursor} if cursor else {})}
        page = thaw(case.call("messages.list", payload).data)
        pages.append(page)
        items.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            return items, pages


# LLM: 冷转后历史必须从会话开头完整、按序、按可见序号返回；跨页游标冻结同一快照，后来追加不混入旧快照。
# 函数用途: 验证插件历史覆盖冷段+热尾而不是只读物理热文件。
def test_plugin_history_reads_cold_prefix_and_freezes_snapshot(case):
    tid = case.create()["thread_id"]
    store = case.stores["alice"]
    _rotate(store, tid)
    first = thaw(case.call("messages.list", {"thread_id": tid, "limit": 50}).data)
    store.messages.append({"thread_id": tid, "role": "user", "content": "快照之后的新消息",
                           "metadata": {"turn_id": "turn-two"}})
    items, pages = [*first["items"]], [first]
    cursor = first["next_cursor"]
    while cursor:
        page = thaw(case.call("messages.list", {"thread_id": tid, "limit": 50, "cursor": cursor}).data)
        assert page["snapshot_id"] == first["snapshot_id"]
        items.extend(page["items"])
        pages.append(page)
        cursor = page["next_cursor"]
    assert [row["content"] for row in items] == [f"历史第{number}行" for number in range(ROWS)]
    assert [row["seq"] for row in items] == list(range(1, ROWS + 1))
    assert len(pages) == 3 and pages[-1]["complete"]
    fresh, _ = _all_pages(case, tid, 100)
    assert [row["content"] for row in fresh][-1] == "快照之后的新消息" and len(fresh) == ROWS + 1


# LLM: 缺失冷段与改写冷段都无法证明完整历史：首读 500、续页 409，绝不返回只含热尾的“成功”页。
# 函数用途: 验证损坏冷成员使快照失效而不是变成空或截断成功。
@pytest.mark.parametrize("damage", ["missing", "rewritten"])
def test_damaged_cold_segment_is_not_partial_success(case, damage):
    tid = case.create()["thread_id"]
    store = case.stores["alice"]
    path, _original = _rotate(store, tid)
    first = thaw(case.call("messages.list", {"thread_id": tid, "limit": 10}).data)
    segment = sorted((path.parent / "archive" / tid).glob("seg-*.jsonl"))[0]
    if damage == "missing":
        segment.unlink()
    else:
        raw = segment.read_bytes()
        segment.write_bytes(raw.replace("历史第1行".encode(), "历史第9行".encode(), 1))
    with pytest.raises(HostAPIError) as fresh:
        case.call("messages.list", {"thread_id": tid, "limit": 10})
    assert (fresh.value.code, fresh.value.status) == ("CORRUPT_HISTORY", 500)
    with pytest.raises(HostAPIError) as resumed:
        case.call("messages.list", {"thread_id": tid, "limit": 10, "cursor": first["next_cursor"]})
    assert (resumed.value.code, resumed.value.status) == ("CURSOR_EXPIRED", 409)


# LLM: 热文件被删而分卷清单仍在属于损坏；只有从未写过消息的线程才是合法空历史。
# 函数用途: 验证缺失热文件不被当成空成功。
def test_missing_hot_file_with_manifest_is_corrupt(case):
    tid = case.create()["thread_id"]
    store = case.stores["alice"]
    path, _original = _rotate(store, tid)
    path.unlink()
    with pytest.raises(HostAPIError) as error:
        case.call("messages.list", {"thread_id": tid})
    assert (error.value.code, error.value.status) == ("CORRUPT_HISTORY", 500)
    empty = case.create("空线程", key="create_empty_0001")["thread_id"]
    page = thaw(case.call("messages.list", {"thread_id": empty}).data)
    assert page["items"] == [] and page["complete"]


# LLM: 冷目录、索引都必须从受信存储根逐段 no-follow 读取；链接回原内容也拒绝，不能借链接把读口带出规范存储。
# 函数用途: 验证冷归档目录被换成符号链接时历史读取失败关闭。
def test_cold_archive_symlink_is_refused(case, tmp_path):
    tid = case.create()["thread_id"]
    store = case.stores["alice"]
    path, _original = _rotate(store, tid)
    archive = path.parent / "archive" / tid
    moved = tmp_path / "moved-archive"
    archive.rename(moved)
    archive.symlink_to(moved, target_is_directory=True)
    with pytest.raises(HostAPIError) as error:
        case.call("messages.list", {"thread_id": tid})
    assert (error.value.code, error.value.status) == ("CORRUPT_HISTORY", 500)


# LLM: 物理 _open 仍是 Bot 调用方依赖的可 fileno/fstat/seek 句柄，只读当前热文件。
# 函数用途: 锁定 Bot 兼容口不被换成虚拟快照。
def test_physical_open_stays_for_bot_callers(case):
    tid = case.create()["thread_id"]
    store = case.stores["alice"]
    path, _original = _rotate(store, tid)
    with history._open(store, tid) as handle:
        info = os.fstat(handle.fileno())
        assert (info.st_dev, info.st_ino) == (path.stat().st_dev, path.stat().st_ino)
        handle.seek(0)
        assert handle.read() == path.read_bytes()


# LLM: 请求最终回复位于冷段时仍按 8 KiB UTF-8 片段经真实公开 poll 与回复游标完整读回，不跨请求拼接。
# 函数用途: 验证 request_replies 读取 canonical 冷段+热尾。
def test_request_reply_in_cold_prefix_is_streamed_completely(public_case):
    request_id, tid = _public_request(public_case)
    store = public_case[2]
    text = "回复正文" * 3000
    store.messages.append({"thread_id": tid, "role": "assistant", "content": text,
                           "metadata": {"turn_id": request_id, "phase": "final"}})
    _rotate(store, tid)
    with history._open(store, tid) as handle:
        assert text.encode("utf-8") not in handle.read()
    result = thaw(_public_call(public_case, "requests.poll", {"request_id": request_id}).data)
    pieces = list(result["replies"])
    while result["has_more_replies"]:
        result = thaw(_public_call(public_case, "requests.poll", {
            "request_id": request_id, "reply_cursor": result["reply_cursor"]}).data)
        pieces.extend(result["replies"])
    assert "".join(row["content"] for row in pieces) == text
    assert all(len(row["content"].encode("utf-8")) <= 8192 for row in pieces)
    assert all(row["request_id"] == request_id for row in pieces) and pieces[-1]["complete"]


# LLM: 回复游标冻结的旧快照遇到冷段损坏必须过期，不能返回空回复冒充“暂无更多”。
# 函数用途: 验证 request_replies 的冷成员损坏走失效而非空成功。
def test_request_reply_cursor_expires_on_damaged_cold_segment(public_case):
    request_id, tid = _public_request(public_case)
    store = public_case[2]
    store.messages.append({"thread_id": tid, "role": "assistant", "content": "长回复" * 4000,
                           "metadata": {"turn_id": request_id, "phase": "final"}})
    path, _original = _rotate(store, tid)
    first = thaw(_public_call(public_case, "requests.poll", {"request_id": request_id}).data)
    assert first["replies"] and first["has_more_replies"]
    for segment in (Path(path.parent) / "archive" / tid).glob("seg-*.jsonl"):
        segment.unlink()
    with pytest.raises(HostAPIError) as error:
        _public_call(public_case, "requests.poll", {"request_id": request_id, "reply_cursor": first["reply_cursor"]})
    assert (error.value.code, error.value.status) == ("CURSOR_EXPIRED", 409)
    with pytest.raises(HostAPIError) as fresh:
        _public_call(public_case, "requests.poll", {"request_id": request_id})
    assert (fresh.value.code, fresh.value.status) == ("STORE_UNAVAILABLE", 503)
