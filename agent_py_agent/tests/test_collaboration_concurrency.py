"""协作 store 并发安全(多 agent 同时读写共享 store 的真实场景):不崩、不丢、不损坏。

JSONL append 有文件锁(io/jsonl.append_jsonl),case JSON 走 update_json_file_atomic。这里真起线程压
并发证据提交 / 请求状态更新 / 开 case,断言:① 不抛异常 ② 证据不丢(锁串行 append)③ 请求 dedup 后
仍是 1 条且终态合法(last-write-wins 可接受)④ case 全建无损坏。
"""

from __future__ import annotations

import threading

from agent_py_agent.agent.collaboration import AgentCapability, CollaborationStore


def _store(tmp_path) -> CollaborationStore:
    return CollaborationStore(tmp_path / "collab")


def _case_with_request(store: CollaborationStore):
    store.register_agent(AgentCapability(agent_id="src", capabilities=("query",)))
    case = store.open_case({"thread_id": "t", "task_id": "task", "title": "并发", "created_by": "src", "now": 1.0})
    request = store.request_collaboration(
        {"case_id": case.case_id, "requester_agent_id": "src", "target_agent_ids": ("helper",),
         "question": "q", "now": 2.0}
    )
    return case, request


def _run(targets: list) -> list:
    errors: list[Exception] = []
    threads = [threading.Thread(target=fn, args=(errors,)) for fn in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_concurrent_evidence_submission_no_loss(tmp_path) -> None:
    """20 线程并发提交证据 → 文件锁串行 append,全部落盘不丢。"""
    store = _store(tmp_path)
    case, request = _case_with_request(store)

    def submit(i: int, errors: list) -> None:
        try:
            store.submit_evidence({
                "case_id": case.case_id, "request_id": request.request_id, "source_agent_id": f"h-{i}",
                "matched": True, "summary": f"e{i}", "evidence_refs": (f"artifact://e{i}",), "now": float(10 + i),
            })
        except Exception as exc:
            errors.append(exc)

    errors = _run([lambda errs, i=i: submit(i, errs) for i in range(20)])
    assert not errors, f"并发提交证据出错: {errors[:3]}"
    assert len(store.case_evidence(case.case_id)) == 20  # ⭐ 20 条全落盘,无丢


def test_concurrent_request_status_update_no_corruption(tmp_path) -> None:
    """15 线程并发更新同一 request → 不崩、dedup 后仍 1 条、终态合法。

    rerouted 不是协议状态，这类更新保留读到的当前状态；按 case 串行后每次更新都基于上一次结果，终态不会回到 open。"""
    store = _store(tmp_path)
    case, request = _case_with_request(store)
    statuses = ["completed", "declined", "rerouted"]

    def update(i: int, errors: list) -> None:
        try:
            store.update_request_status({
                "case_id": case.case_id, "request_id": request.request_id, "status": statuses[i % 3],
                "actor_agent_id": f"h-{i}", "summary": f"u{i}", "now": float(10 + i),
            })
        except Exception as exc:
            errors.append(exc)

    errors = _run([lambda errs, i=i: update(i, errs) for i in range(15)])
    assert not errors, f"并发更新出错: {errors[:3]}"
    reqs = store.case_requests(case.case_id)
    assert len(reqs) == 1  # dedup by id,仍 1 条(无半损坏的多条)
    assert reqs[0].status in statuses  # 终态是某个合法更新(JSONL 未损坏)


def test_stale_request_snapshot_cannot_overwrite_a_newer_status(tmp_path, monkeypatch) -> None:
    """复现 2026-09-27 全仓分片里的偶发失败：先读到 open 的更新最后才写入，把已完成的请求写回 open。

    慢线程读到 open 后停在“算新快照”处；快线程随后提交 completed。按 case 串行时快线程必须等慢线程写完再读，
    终态是 completed；没有串行时慢线程最后写入旧快照，终态回到 open。
    """
    store = _store(tmp_path)
    case, request = _case_with_request(store)
    slow_has_read, release_slow = threading.Event(), threading.Event()
    original = store._updated_request_snapshot

    def gated_snapshot(current, status_text, kwargs):
        if status_text == "rerouted":
            slow_has_read.set()
            release_slow.wait(5)
        return original(current, status_text, kwargs)

    monkeypatch.setattr(store, "_updated_request_snapshot", gated_snapshot)

    def update(status: str, index: int) -> None:
        store.update_request_status({
            "case_id": case.case_id, "request_id": request.request_id, "status": status,
            "actor_agent_id": f"h-{index}", "now": float(10 + index),
        })

    slow = threading.Thread(target=update, args=("rerouted", 1))
    slow.start()
    assert slow_has_read.wait(5)
    fast = threading.Thread(target=update, args=("completed", 2))
    fast.start()
    fast.join(0.5)  # 没有串行时快线程在这里已经写完；有串行时它还在等锁
    release_slow.set()
    slow.join(5)
    fast.join(5)
    assert not slow.is_alive() and not fast.is_alive()
    assert store.case_requests(case.case_id)[0].status == "completed"


def test_stale_case_snapshot_cannot_overwrite_a_newer_status(tmp_path, monkeypatch) -> None:
    """case 状态同理：带非法状态文本的更新保留读到的 open，不能在别人关闭 case 之后把它写回 open。"""
    store = _store(tmp_path)
    case, _request = _case_with_request(store)
    slow_has_read, release_slow = threading.Event(), threading.Event()
    original = store._write_case

    def gated_write(updated) -> None:
        if updated.metadata.get("raw_case_status") == "archived":
            slow_has_read.set()
            release_slow.wait(5)
        original(updated)

    monkeypatch.setattr(store, "_write_case", gated_write)
    slow = threading.Thread(target=store.update_case_status, args=(case.case_id,), kwargs={"status": "archived", "now": 5.0})
    slow.start()
    assert slow_has_read.wait(5)
    fast = threading.Thread(target=store.update_case_status, args=(case.case_id,), kwargs={"status": "closed", "now": 6.0})
    fast.start()
    fast.join(0.5)
    release_slow.set()
    slow.join(5)
    fast.join(5)
    assert not slow.is_alive() and not fast.is_alive()
    assert store.load_case(case.case_id).status == "closed"


def test_concurrent_case_creation_all_persisted(tmp_path) -> None:
    """20 线程并发开 case → 全部创建,list_cases 数对,无损坏。"""
    store = _store(tmp_path)
    store.register_agent(AgentCapability(agent_id="src", capabilities=("query",)))

    def open_one(i: int, errors: list) -> None:
        try:
            store.open_case({
                "thread_id": f"t{i}", "task_id": f"task-{i}", "title": f"case{i}",
                "created_by": "src", "now": float(i),
            })
        except Exception as exc:
            errors.append(exc)

    errors = _run([lambda errs, i=i: open_one(i, errs) for i in range(20)])
    assert not errors, f"并发开 case 出错: {errors[:3]}"
    assert len(store.list_cases()) == 20  # 20 个全建,无丢无损坏


def test_concurrent_mixed_operations_store_stays_readable(tmp_path) -> None:
    """混合并发(开 case + 提交证据 + 更新状态 + 读)→ store 始终可读、自洽,不崩。"""
    store = _store(tmp_path)
    case, request = _case_with_request(store)

    def submit(i: int, errors: list) -> None:
        try:
            store.submit_evidence({"case_id": case.case_id, "request_id": request.request_id,
                                   "source_agent_id": f"h-{i}", "summary": f"e{i}",
                                   "evidence_refs": (f"a://{i}",), "now": float(i)})
        except Exception as exc:
            errors.append(exc)

    def reader(_i: int, errors: list) -> None:
        try:
            for _ in range(10):
                store.case_evidence(case.case_id)
                store.case_requests(case.case_id)
                store.list_cases()
        except Exception as exc:
            errors.append(exc)

    targets = [lambda errs, i=i: submit(i, errs) for i in range(10)]
    targets += [lambda errs, i=i: reader(i, errs) for i in range(5)]
    errors = _run(targets)
    assert not errors, f"混合并发读写出错: {errors[:3]}"  # ⭐ 读写交织不崩、store 自洽
    assert len(store.case_evidence(case.case_id)) == 10
