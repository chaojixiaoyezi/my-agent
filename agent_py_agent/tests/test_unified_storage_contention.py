"""测真实路径锁/LocalStore事务；计时是观测值，不把绝对速度当跨机器合同。"""
from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.common.json_io import locked_json_path, write_json_file_atomic_unlocked
from agent_py_agent.agent.gateway_parts.io import locked_file_transition, update_json_file_atomic
from agent_py_agent.agent.io.sqlite_connection import sqlite_error_details
from agent_py_agent.agent.local_storage import LocalStore


def _measure_writer(path, mode, index, ready, start, results):
    path = Path(path)
    store = LocalStore(path) if mode == "shared_sqlite" else None
    ready.put(index)
    if not start.wait(30):
        results.put({"error": "start_timeout"})
        return
    waits, holds, busy = [], [], 0
    try:
        for _ in range(4):
            before = time.monotonic()
            if store is not None:
                with store._connection() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    acquired = time.monotonic()
                    conn.execute("INSERT INTO metadata(key,value) VALUES(?,?)", (f"{index}-{len(waits)}", "durable"))
                    conn.commit()
            elif index % 2:
                with locked_json_path(path):
                    acquired = time.monotonic()
                    payload = json.loads(Path(path).read_text()) if Path(path).exists() else {}
                    write_json_file_atomic_unlocked(Path(path), {"n": payload.get("n", 0) + 1})
            else:
                mark = []

                def update(data):
                    mark.append(time.monotonic())
                    return {"n": data.get("n", 0) + 1}

                update_json_file_atomic(Path(path), update)
                acquired = mark[0]
            waits.append(acquired - before)
            holds.append(time.monotonic() - acquired)
    except BaseException as error:
        busy += int(sqlite_error_details(error).get("category") in {"busy", "locked"})
        results.put({"error": type(error).__name__, "details": sqlite_error_details(error), "busy": busy})
        return
    results.put({"wait_seconds": waits, "hold_seconds": holds, "busy": busy, "commits": len(waits)})


@pytest.mark.parametrize("workers", [2, 4, 8, 12])
@pytest.mark.parametrize("mode", ["same_object", "different_threads", "different_owners", "shared_sqlite"])
def test_actual_storage_lock_domains(tmp_path, request, workers, mode):
    context = multiprocessing.get_context("spawn")
    ready, results, start = context.Queue(), context.Queue(), context.Event()
    paths = [tmp_path / (f"owner-{i}" if mode == "different_owners" else "owner-0") /
             (f"thread-{i}.json" if mode == "different_threads" else "shared.json") for i in range(workers)]
    if mode == "shared_sqlite":
        paths = [tmp_path / "shared.db"] * workers
        LocalStore(paths[0])
    processes = [context.Process(target=_measure_writer, args=(str(path), mode, i, ready, start, results))
                 for i, path in enumerate(paths)]
    for process in processes:
        process.start()
    try:
        assert len({ready.get(timeout=30) for _ in processes}) == workers
        start.set()
        reports = [results.get(timeout=30) for _ in processes]
    finally:
        start.set()
        for process in processes:
            process.join(30)
            assert not process.is_alive() and process.exitcode == 0
        ready.close()
        results.close()
    assert all("error" not in report for report in reports), reports
    assert sum(report["commits"] for report in reports) == workers * 4
    if mode == "shared_sqlite":
        with LocalStore(paths[0])._connection() as conn:
            assert conn.execute("SELECT count(*) FROM metadata WHERE value='durable'").fetchone()[0] == workers * 4
    else:
        assert sum(json.loads(path.read_text())["n"] for path in set(paths)) == workers * 4
    observation = {"mode": mode, "workers": workers, "reports": reports}
    request.node.user_properties.append(("storage_measurement", json.dumps(observation)))
    output = os.environ.get("W8_MEASUREMENT_OUTPUT")
    if output:
        root = Path(output)
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{mode}-{workers}.json").write_text(json.dumps(observation, indent=2))


def _paused_holder(path, ready, release):
    with locked_json_path(Path(path)):
        ready.set()
        assert release.wait(30)


def test_paused_cross_process_holder_keeps_other_owner_and_loop_advancing(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    blocked, other = tmp_path / "owner-a/state.json", tmp_path / "owner-b/state.json"
    holder = context.Process(target=_paused_holder, args=(str(blocked), ready, release))
    holder.start()
    try:
        assert ready.wait(30)

        async def observe():
            ticks = []

            async def tick():
                for _ in range(4):
                    ticks.append(time.monotonic())
                    await asyncio.sleep(0)

            async def try_busy():
                with pytest.raises(BlockingIOError):
                    with locked_file_transition(blocked, blocking=False):
                        pytest.fail("同一对象不能在另一进程持锁时取得写权")
                with locked_json_path(other, blocking=False):
                    write_json_file_atomic_unlocked(other, {"advanced": True})
                await asyncio.sleep(0)

            await asyncio.gather(tick(), try_busy())
            assert len(ticks) == 4

        asyncio.run(observe())
        assert json.loads(other.read_text()) == {"advanced": True}
        assert not blocked.exists()
    finally:
        release.set()
        holder.join(30)
        assert holder.exitcode == 0
    update_json_file_atomic(blocked, lambda data: {"recovered": True}, blocking=False)
    assert json.loads(blocked.read_text()) == {"recovered": True}


def test_shared_database_busy_is_typed_and_original_obligation_can_resume(tmp_path):
    path = tmp_path / "shared.db"
    store = LocalStore(path)
    with store._connection() as holder, store._connection() as contender:
        holder.execute("BEGIN IMMEDIATE")
        contender.execute("PRAGMA busy_timeout=0")
        with pytest.raises(Exception) as caught:
            contender.execute("INSERT INTO metadata VALUES('obligation','pending')")
        assert sqlite_error_details(caught.value)["category"] == "busy"
        holder.rollback()
        contender.execute("INSERT INTO metadata VALUES('obligation','pending')")
        contender.commit()
    with LocalStore(path)._connection() as observer:
        assert observer.execute("SELECT value FROM metadata WHERE key='obligation'").fetchone()[0] == "pending"
