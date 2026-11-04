"""用量追加的真实账本、重放和有界读回归。"""
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation import store_usage as usage
from agent_py_agent.agent.runtime_errors import DataCorruptionError


# LLM: 构造真实用量入口接受的输入；调用数和作用域由测试显式指定。
# 函数用途: 生成可独立重算的用量事件。
def _request(thread, event_id, *, count=1, scope=""):
    return {"thread_id": thread.thread_id, "event_id": event_id, "request_id": scope or event_id,
            "source": "test", "model_calls": {"schema": "model_call_summary.v1",
                "physical_model_attempt_count": count, "accounted_input_tokens": count * 3,
                "output_tokens": count, "models": ["test-model"],
                "model_attempt_counts": {"test-model": count},
                "usage_breakdown": {"provider": {"input_tokens": count * 3,
                    "output_tokens": count, "call_count": count}, "estimated": {}}}, "now": 1}


# LLM: 所有存储落在本例隔离目录，不替代产品读写方法。
# 函数用途: 创建真实用量会话。
def _store(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "local", "channel": "test",
        "channel_conversation_id": "usage", "channel_user_id": "local"})
    return store, thread


# LLM: 固定批次追平后计数热路，完整账本另作独立核对。
# 函数用途: 验证追加和快照成本不随历史增长。
@pytest.mark.parametrize("history", [1000, 10000])
@pytest.mark.parametrize("snapshot", [False, True])
def test_usage_append_cost_is_independent_of_history(tmp_path, monkeypatch, history, snapshot):
    # 冷迁移追平后才计量热路，完整账本另作独立汇总核对。
    store, thread = _store(tmp_path)
    path = store.model_usage._path(thread.thread_id)
    path.parent.mkdir(exist_ok=True)
    rows = [usage._thread_model_usage_event(_request(thread, f"old-{n}")).to_dict() for n in range(history)]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    # 新投影通过固定批次追平；旧实现没有此入口，直接测原热路径的解析量。
    factory = getattr(store.model_usage, "_index", None)
    if factory:
        index = factory(thread.thread_id)
        for _ in range(history // 128 + 5):
            with index._connection() as conn:
                if index.prepare(conn):
                    break
        else:
            pytest.fail("usage bounded backfill did not converge")
    counts = {"json": 0, "bytes": 0}
    loads = json.loads

    # LLM: 原解析值和异常原样透传，不缓存结果。
    # 函数用途: 累计真实 JSON 解析次数和输入字节。
    def observe(value, *args, **kwargs):
        counts["json"] += 1
        counts["bytes"] += len(value.encode() if isinstance(value, str) else value)
        return loads(value, *args, **kwargs)

    monkeypatch.setattr(json, "loads", observe)
    append = store.model_usage.append_snapshot_once if snapshot else store.model_usage.append_once
    before = len(os.listdir("/dev/fd")) if os.path.isdir("/dev/fd") else None
    first = append(_request(thread, "new", scope="same-scope"))
    assert append(_request(thread, "new", scope="same-scope")) == first
    if before is not None:
        assert len(os.listdir("/dev/fd")) == before
    assert counts["json"] <= 40, counts
    assert counts["bytes"] < 65536, counts
    monkeypatch.setattr(json, "loads", loads)
    summary = store.model_usage.summary(thread.thread_id)
    events, errors = store.model_usage.events_report(thread.thread_id)
    assert not errors and len(events) == history + 1
    assert summary["event_count"] == len(events)
    assert summary["provider"]["input_tokens"] == sum(event.model_calls["usage_breakdown"]["provider"]["input_tokens"] for event in events)


# LLM: 两个线程竞争同一事件；重启后沿真实账本计算快照差额。
# 函数用途: 验证重复结算和迟到快照不会重复计费。
def test_usage_duplicate_finalize_and_late_snapshot_after_restart(tmp_path):
    store, thread = _store(tmp_path)
    event = _request(thread, "concurrent")
    start = threading.Barrier(2)

    # LLM: Barrier 只规定起跑时机，去重由产品锁和事务完成。
    # 函数用途: 并发提交相同事件。
    def append():
        start.wait(timeout=5)
        return store.model_usage.append_once(event)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: append(), range(2)))
    assert results[0] == results[1]
    with pytest.raises(DataCorruptionError):
        store.model_usage.append_once(_request(thread, "concurrent", count=2))
    store.model_usage.append_snapshot_once(_request(thread, "s1", scope="snap", count=1))
    reopened = ConversationStore(store.storage.root)
    late = reopened.model_usage.append_snapshot_once(_request(thread, "s2", scope="snap", count=2))
    assert late.model_calls["physical_model_attempt_count"] == 1
    assert reopened.model_usage.append_snapshot_once(_request(thread, "s2", scope="snap", count=2)) == late
    assert reopened.model_usage.summary(thread.thread_id)["provider"]["call_count"] == 3


# LLM: 冷索引精确回退当前会话，损坏尾行不得当作成功结算。
# 函数用途: 验证迁移期汇总准确和坏账拒绝。
def test_usage_migration_never_reports_partial_totals_and_detects_bad_tail(tmp_path):
    store, thread = _store(tmp_path)
    path = store.model_usage._path(thread.thread_id)
    path.parent.mkdir(exist_ok=True)
    rows = [usage._thread_model_usage_event(_request(thread, f"old-{n}")).to_dict() for n in range(300)]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    for _ in range(5):
        assert store.model_usage.summary(thread.thread_id)["event_count"] == 300
    with path.open("a") as handle:
        handle.write("not json\n")
    with pytest.raises(DataCorruptionError):
        store.model_usage.append_once(_request(thread, "refused"))


# LLM: 只中断真实JSONL发布后的投影更新；再次提交必须重放同一事实，不得重复追加或计费。
# 函数用途: 验证用量原账与派生事务之间的崩溃窗口。
def test_usage_crash_after_canonical_append_replays_once(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.usage_index import UsageIndex

    store, thread = _store(tmp_path)
    request = _request(thread, "interrupted", count=2)

    # LLM: 异常只发生在生产已有的投影提交入口，账本追加及fsync不替代。
    # 函数用途: 模拟派生事务提交前的I/O中断。
    def interrupted(*args):
        raise OSError("simulated I/O error before usage projection commit")

    with monkeypatch.context() as patch:
        patch.setattr(UsageIndex, "committed", interrupted)
        with pytest.raises(OSError, match="simulated I/O error"):
            store.model_usage.append_once(request)
    raw = store.model_usage._path(thread.thread_id).read_bytes()
    reopened = ConversationStore(store.storage.root)
    first = reopened.model_usage.append_once(request)
    assert reopened.model_usage.append_once(request) == first
    assert reopened.model_usage._path(thread.thread_id).read_bytes() == raw
    assert reopened.model_usage.summary(thread.thread_id)["provider"]["call_count"] == 2
    assert reopened.model_usage.summary(thread.thread_id)["event_count"] == 1


# LLM: SQLite可以完全丢弃，当前会话仍从原JSONL精确重建；过渡期不能输出部分费用。
# 函数用途: 验证损坏投影恢复及后续正常去重。
def test_usage_corrupt_projection_rebuilds_exact_totals(tmp_path):
    store, thread = _store(tmp_path)
    path = store.model_usage._path(thread.thread_id)
    path.write_text("".join(json.dumps(usage._thread_model_usage_event(
        _request(thread, f"old-{n}")).to_dict()) + "\n" for n in range(300)))
    for _ in range(4):
        assert store.model_usage.summary(thread.thread_id)["event_count"] == 300
    store.model_usage._index(thread.thread_id).path.write_bytes(b"not sqlite")
    reopened = ConversationStore(store.storage.root)
    for _ in range(4):
        summary = reopened.model_usage.summary(thread.thread_id)
        assert summary["event_count"] == 300 and summary["provider"]["call_count"] == 300
    request = _request(thread, "after-rebuild", count=2)
    first = reopened.model_usage.append_once(request)
    assert reopened.model_usage.append_once(request) == first
    assert reopened.model_usage.summary(thread.thread_id)["provider"]["call_count"] == 302
    events, errors = reopened.model_usage.events_report(thread.thread_id)
    assert not errors and len(events) == 301


# LLM: 计数包装器不替换store、锁、SQLite或canonical值；只记录真实读取与追加文件的长度差。
# 函数用途: 在十倍历史下逐轮检查用量热路的字节、解析和FD成本。
@pytest.mark.parametrize("history", [1000, 10000])
def test_usage_hot_loop_io_and_fd_stay_flat(tmp_path, monkeypatch, history):
    from agent_py_agent.agent.conversation import usage_index as module

    store, thread = _store(tmp_path)
    path = store.model_usage._path(thread.thread_id)
    path.write_text("".join(json.dumps(usage._thread_model_usage_event(
        _request(thread, f"old-{n}")).to_dict()) + "\n" for n in range(history)))
    index = store.model_usage._index(thread.thread_id)
    for _ in range(history // 128 + 5):
        with index._connection() as conn:
            if index.prepare(conn):
                break
    else:
        pytest.fail("usage rebuild did not finish")
    counts = {"read_bytes": 0, "json_loads": 0}
    original_reader, original_loads = module._reader, json.loads

    # LLM: 原reader始终拥有FD，包装器不改变seek或异常。
    # 类用途: 计数真实读取量。
    class Counted:
        # LLM: 只借用当前with中的真实文件。
        # 函数用途: 保存原reader。
        def __init__(self, handle):
            self.handle = handle

        # LLM: 不涉及字节的所有操作都透传。
        # 函数用途: 保留原文件接口。
        def __getattr__(self, name):
            return getattr(self.handle, name)

        # LLM: 原read返回值及异常不变。
        # 函数用途: 累计点读字节。
        def read(self, *args):
            value = self.handle.read(*args)
            counts["read_bytes"] += len(value)
            return value

        # LLM: readline继续执行原预算约束。
        # 函数用途: 累计追加后缀读取字节。
        def readline(self, *args):
            value = self.handle.readline(*args)
            counts["read_bytes"] += len(value)
            return value

    # LLM: 保留原上下文管理器清理路径。
    # 函数用途: 将文件借给计数包装器。
    @contextmanager
    def reader(target):
        with original_reader(target) as handle:
            yield Counted(handle)

    # LLM: 不缓存或替代解析结果。
    # 函数用途: 记录实际JSON解析次数。
    def loads(*args, **kwargs):
        counts["json_loads"] += 1
        return original_loads(*args, **kwargs)

    monkeypatch.setattr(module, "_reader", reader)
    monkeypatch.setattr(json, "loads", loads)
    fd_root = "/dev/fd" if Path("/dev/fd").exists() else "/proc/self/fd"
    before = len(os.listdir(fd_root))
    for n in range(5):
        counts.update(dict.fromkeys(counts, 0))
        request = _request(thread, f"new-{n}", scope="one-scope", count=n + 1)
        size = path.stat().st_size
        event = store.model_usage.append_snapshot_once(request)
        assert store.model_usage.append_snapshot_once(request) == event
        assert path.stat().st_size - size < 16384
        assert counts["read_bytes"] < 16384 and counts["json_loads"] < 40, counts
        append_counts = dict(counts)
        counts.update(dict.fromkeys(counts, 0))
        assert store.model_usage.summary(thread.thread_id)["event_count"] == history + n + 1
        assert counts["read_bytes"] == 0 and counts["json_loads"] < 8, counts
        assert len(os.listdir(fd_root)) == before
    print(json.dumps({"history": history, "append": append_counts, "summary": counts, "fd_delta": 0}))


# LLM: 唯一替身是供应商传输；用户身份与owner分离，请求认领、工具循环、插话消费和费用结算全走真实Gateway。
# 函数用途: 在同一真实回合中核验工具安全点注入与用量索引结算。
def test_gateway_tool_loop_injects_guidance_and_settles_real_usage(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.gateway_parts import control_service
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
    from agent_py_agent.tests.test_session_task_real_chain import _real_chain

    chain = _real_chain(tmp_path, monkeypatch)
    # 用户标识不能沿用含斜杠的owner；只调整本例身份，不替换任何存储入口。
    chain._conversation = lambda label: {
        "canonical_user_id": "local-user",
        "channel": "chat",
        "channel_conversation_id": f"rc-session-{label}",
        "channel_user_id": "local-user",
    }
    chain.open_session("B")
    store = chain.agent.conversation_store
    errors, results = [], []
    script = chain.wire._script
    observed = []

    # LLM: 只观察原供应商脚本的当前回合正文；原等待点、响应、工具和存储都不替换。
    # 函数用途: 区分每次正文重复注入与同一输入在后续工具轮保留。
    def observe_script(turn):
        response = script(turn)
        observed.append((response[0], turn.own_text.count("RC-NOTE-W2WIRE")))
        return response

    monkeypatch.setattr(chain.wire, "_script", observe_script)

    # LLM: 后台线程调用产品原Gateway ask，异常交回主测试断言，不伪造执行结果。
    # 函数用途: 让真实模型请求挂起，以便控制入口提交插话。
    def ask():
        try:
            results.append(chain.ask("B", "RC-BUSY W2 tool loop and usage"))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=ask)
    worker.start()
    try:
        assert chain.wire.wait_holding("busy")
        scope = control_service.GatewayControlScope("local-user", "chat", "rc-session-B",
            metadata={"message_id": "w2-wire"}, resolved_owner=OwnerIdentity.local_main())
        steer = control_service.execute_gateway_conversation_control(chain.agent, chain.paths,
            parse_conversation_control("/btw RC-NOTE-W2WIRE bounded store integration"), scope)
        assert steer.ok, steer
        queued, load_errors = store.guidance.pending_report("request", steer.request_id)
        assert not load_errors
        assert len(queued) == 1 and "RC-NOTE-W2WIRE" in queued[0].message
        assert queued[0].target_id == steer.request_id
        assert queued[0].metadata["expected_turn_id"] == steer.request_id
    finally:
        chain.wire.release("busy")
        worker.join(timeout=25)
    assert not worker.is_alive() and not errors and results, errors
    assert all(result.get("ok") for result in results), json.dumps(results, ensure_ascii=False)
    # 同一输入应在工具轮历史中保留，但每次原始正文只能出现一次，不能用set去重后的可见性当消费次数。
    assert observed == [("user-busy-list", 0), ("user-busy-list", 1), ("user-busy-final", 1)], observed
    assert len(chain.tool_outputs("list_files")) == 1, "real tool-loop step was not executed exactly once"
    entry = queued[0]
    key = entry.metadata["dedupe_key"]
    receipt = store.guidance.receipt(key)
    assert receipt.status == "consumed"
    assert receipt.entry.guidance_id == entry.guidance_id
    pending, load_errors = store.guidance.pending_report("request", steer.request_id)
    assert not load_errors and pending == []
    # 只读真实不可变批次，逐项比对身份，不能仅凭文件数或上下文可见性宣称恰好消费一次。
    submissions = [json.loads(path.read_text())
        for path in store.storage.guidance_submission_batches_dir.glob("*/*.json")]
    acknowledgements = [json.loads(path.read_text())
        for path in store.storage.guidance_ack_batches_dir.glob("*/*.json")]
    assert len(submissions) == len(acknowledgements) == 1
    submission, acknowledgement = submissions[0], acknowledgements[0]
    assert submission["schema_version"] == "conversation_guidance_submission_batch.v1"
    assert submission["status"] == "submitted"
    assert acknowledgement["schema_version"] == "conversation_guidance_ack_batch.v3"
    assert submission["provider_call_id"] == acknowledgement["provider_call_id"] == receipt.submission_id
    assert receipt.submission_id and submission["attempt_id"] == receipt.attempt_id
    for batch in (submission, acknowledgement):
        assert batch["expected_turn_id"] == steer.request_id
        assert batch["items"] == [{"guidance_id": entry.guidance_id, "dedupe_key": key}]
    thread = chain.threads["B"]
    projected = [message for message in store.messages.recent(thread, limit=200)
        if message.metadata.get("guidance_id") == entry.guidance_id]
    assert len(projected) == 1 and projected[0].role == "user"
    assert projected[0].content == entry.message
    assert projected[0].metadata["gateway_request_id"] == steer.request_id
    events, load_errors = store.model_usage.events_report(thread)
    assert not load_errors and events
    assert all(event.request_id == steer.request_id for event in events)
    assert store.model_usage.summary(thread) == store.model_usage._exact_summary(thread)
    assert store.model_usage.summary(thread)["provider"]["call_count"] >= 2
