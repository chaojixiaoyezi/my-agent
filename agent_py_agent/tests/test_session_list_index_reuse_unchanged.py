"""验证 SessionListIndex._refresh 重建时复用未变化文件 stamp 跳过 reader，同时保留其它路径全量重读与 artifact_refs。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_py_agent.agent.conversation.session_list_index import SessionListIndex


def _make_task_payload(task_id: str, created_at: float, *, artifact_id: str | None = None) -> dict:
    metadata = {}
    if artifact_id is not None:
        metadata["result_artifacts"] = [{"artifact_id": artifact_id, "path": f"out/{artifact_id}.txt"}]
    return {
        "task_id": task_id,
        "created_at": created_at,
        "target_thread_id": "owner",
        "metadata": metadata,
    }


def test_rebuild_after_single_addition_reads_only_new_file_and_preserves_artifact_tasks(tmp_path: Path) -> None:
    root = tmp_path / "tasks"
    root.mkdir()
    total = 512
    for n in range(total):
        art_id = f"art-{n:03}" if n in {0, total - 1} else None
        (root / f"t{n:04}.json").write_text(
            json.dumps(_make_task_payload(f"t{n:04}", float(n + 1), artifact_id=art_id)),
            encoding="utf-8",
        )

    read_calls: list[str] = []

    def reader(path: Path):
        read_calls.append(path.name)
        try:
            return json.loads(path.read_text(encoding="utf-8")), None
        except (OSError, ValueError) as exc:
            return {}, {"code": "READ_ERROR", "detail": str(exc)}

    index = SessionListIndex(root, kind="session_task", reader=reader)
    for _ in range(16):
        page = index.page(limit=5, filter_key="owner")
        if page["coverage"] == "complete":
            break
    else:
        pytest.fail("initial 512-file rebuild did not complete")

    for _ in range(20):
        if index.artifact_tasks("art-000")["coverage"] == "complete":
            break
    else:
        pytest.fail("initial artifact backfill did not complete")
    assert index.artifact_tasks("art-000")["items"][0]["task_id"] == "t0000"
    assert index.artifact_tasks("art-511")["items"][0]["task_id"] == "t0511"

    # 新增一个非首页排名的任务文件（created_at=0.0，不会落在每页返回的 top-1 项内）。
    (root / "t_new.json").write_text(
        json.dumps(_make_task_payload("t_new", 0.0, artifact_id="art-new")),
        encoding="utf-8",
    )

    read_calls.clear()
    refresh_reads: list[str] = []
    in_refresh = False
    original_refresh = index._refresh

    def tracked_refresh(conn):
        nonlocal in_refresh
        in_refresh = True
        try:
            return original_refresh(conn)
        finally:
            in_refresh = False

    def tracked_reader(path: Path):
        if in_refresh:
            refresh_reads.append(path.name)
        return reader(path)

    index.reader = tracked_reader
    index._refresh = tracked_refresh  # type: ignore[method-assign]

    batches = 0
    for _ in range(16):
        batches += 1
        page = index.page(limit=1, filter_key="owner")
        if page["coverage"] == "complete":
            break
    else:
        pytest.fail("post-addition rebuild did not complete")

    # 重建批次仅对新增的 1 个文件调用 reader；512 个未改文件全靠 stamp 命中复用。
    assert refresh_reads == ["t_new.json"]
    assert len(read_calls) == 1 + batches

    # pre-existing 与新文件的 artifact_tasks 索引均完整可用。
    read_calls.clear()
    for art_id, expected_task in (("art-000", "t0000"), ("art-511", "t0511"), ("art-new", "t_new")):
        res = index.artifact_tasks(art_id)
        assert res["coverage"] == "complete"
        assert res["load_errors"] == []
        assert [item["task_id"] for item in res["items"]] == [expected_task]
    # 仅每条返回的候选重读一次原正文，无全量重读。
    assert len(read_calls) == 3


def test_rebuild_rereads_modified_file_and_prunes_deleted_file(tmp_path: Path) -> None:
    root = tmp_path / "tasks"
    root.mkdir()
    for n in range(10):
        (root / f"t{n:02}.json").write_text(
            json.dumps(_make_task_payload(f"t{n:02}", float(n))),
            encoding="utf-8",
        )

    refresh_reads: list[str] = []
    in_refresh = False

    def reader(path: Path):
        if in_refresh:
            refresh_reads.append(path.name)
        return json.loads(path.read_text(encoding="utf-8")), None

    index = SessionListIndex(root, kind="session_task", reader=reader)
    original_refresh = index._refresh

    def tracked_refresh(conn):
        nonlocal in_refresh
        in_refresh = True
        try:
            return original_refresh(conn)
        finally:
            in_refresh = False

    index._refresh = tracked_refresh  # type: ignore[method-assign]
    assert index.page(limit=10)["coverage"] == "complete"

    refresh_reads.clear()
    # 原位改写 t03.json（改变尺寸和内容/时间戳）、删除 t01.json、新增 t10.json。
    (root / "t03.json").write_text(
        json.dumps(_make_task_payload("t03", 999.0, artifact_id="art-updated") | {"extra": "padding-to-change-stamp"}),
        encoding="utf-8",
    )
    (root / "t01.json").unlink()
    (root / "t10.json").write_text(
        json.dumps(_make_task_payload("t10", 100.0)),
        encoding="utf-8",
    )

    page = index.page(limit=10)
    assert page["coverage"] == "complete"
    assert set(refresh_reads) == {"t03.json", "t10.json"}
    returned_ids = [item["task_id"] for item in page["items"]]
    assert returned_ids[0] == "t03"
    assert "t01" not in returned_ids
    assert "t10" in returned_ids
    assert index.artifact_tasks("art-updated")["items"][0]["task_id"] == "t03"


def test_rebuild_reusing_unchanged_rows_still_bumps_revision_and_expires_old_cursor(tmp_path: Path) -> None:
    root = tmp_path / "tasks"
    root.mkdir()
    for n in range(260):
        (root / f"t{n:04}.json").write_text(
            json.dumps(_make_task_payload(f"t{n:04}", float(n + 1))),
            encoding="utf-8",
        )

    index = SessionListIndex(root, kind="session_task", reader=lambda p: (json.loads(p.read_text()), None))
    for _ in range(10):
        if index.page(limit=5)["coverage"] == "complete":
            break

    # 新增一个文件触发新一轮全量重建，第 1 批含大量未变化行，依然必须推进 revision 并使旧分页游标失效。
    (root / "t_extra.json").write_text(
        json.dumps(_make_task_payload("t_extra", 0.5)),
        encoding="utf-8",
    )
    p1 = index.page(limit=5)
    assert p1["coverage"] == "partial"
    assert p1["next_cursor"] is not None
    with index._connection() as conn:
        rev_before = int(index._meta(conn, "revision", "0"))

    p2 = index.page(limit=5, cursor=p1["next_cursor"])
    assert p2["coverage"] == "expired"
    with index._connection() as conn:
        rev_after = int(index._meta(conn, "revision", "0"))
    assert rev_after > rev_before


def test_non_refresh_callers_still_reread_and_artifact_errors_survive_unchanged_rebuild(tmp_path: Path) -> None:
    root = tmp_path / "tasks"
    root.mkdir()
    p0 = root / "t0.json"
    p0.write_text(json.dumps(_make_task_payload("t0", 1.0, artifact_id="art-0")), encoding="utf-8")
    p_bad = root / "t_bad.json"
    p_bad.write_text("{corrupt", encoding="utf-8")

    reads: list[str] = []

    def reader(path: Path):
        reads.append(path.name)
        try:
            return json.loads(path.read_text(encoding="utf-8")), None
        except (OSError, ValueError) as exc:
            return {}, {"code": "READ_ERROR", "detail": str(exc)}

    index = SessionListIndex(root, kind="session_task", reader=reader)
    assert index.page(limit=10)["coverage"] == "complete"
    assert index.artifact_tasks("art-0")["coverage"] == "partial"  # 存在 t_bad 的 artifact_errors

    # 1. publish() 即使在文件 stamp 相同的情况下也保持默认 reuse_unchanged=False 全量重读。
    reads.clear()
    index.publish(p0)
    assert reads == ["t0.json"]

    # 2. _probe_file_identities() 在 stamp 不变时不读，stamp 变化时走默认全量重读。
    # 3. artifact_tasks() 回填（artifact_backfill_complete != '1'）仍以默认 reuse_unchanged=False 全量重读。
    with index._connection() as conn:
        conn.execute("DELETE FROM meta WHERE key IN ('artifact_backfill_complete', 'artifact_backfill_after')")
    reads.clear()
    art_page = index.artifact_tasks("art-0")
    assert set(reads) >= {"t0.json", "t_bad.json"}
    assert art_page["coverage"] == "partial"

    # 4. 添加新文件触发重建：未变化的健康文件（t0.json）不重读，位于 artifact_errors 的记录（t_bad.json）每轮重建重读一次并在仍不可解析时保留其错误行。
    (root / "t1.json").write_text(
        json.dumps(_make_task_payload("t1", 2.0, artifact_id="art-1")),
        encoding="utf-8",
    )
    in_refresh = False
    refresh_reads: list[str] = []
    original_refresh = index._refresh

    def tracked_refresh(conn):
        nonlocal in_refresh
        in_refresh = True
        try:
            return original_refresh(conn)
        finally:
            in_refresh = False

    def spy_reader(path: Path):
        if in_refresh:
            refresh_reads.append(path.name)
        return reader(path)

    index._refresh = tracked_refresh  # type: ignore[method-assign]
    index.reader = spy_reader
    assert index.page(limit=10)["coverage"] == "complete"
    assert sorted(refresh_reads) == ["t1.json", "t_bad.json"]
    assert index.artifact_tasks("art-0")["coverage"] == "partial"
    assert index.artifact_tasks("art-1")["items"][0]["task_id"] == "t1"
    with index._connection() as conn:
        assert conn.execute("SELECT task_path FROM artifact_errors").fetchall()[0][0] == "t_bad.json"


def test_refresh_entry_skips_unchanged_while_non_refresh_callers_reread_and_subclasses_stay_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_py_agent.agent.conversation.background_agents import BackgroundAgentListIndex
    from agent_py_agent.agent.conversation.session_task_pending_index import SessionTaskPendingIndex

    root = tmp_path / "tasks"
    root.mkdir()
    p0 = root / "t0.json"
    p0.write_text(json.dumps(_make_task_payload("t0", 10.0, artifact_id="art-stale")), encoding="utf-8")

    record_calls: list[str] = []
    original_record = SessionListIndex._record

    def spy_record(self, conn, path: Path, generation: int):
        record_calls.append(path.name)
        return original_record(self, conn, path, generation)

    monkeypatch.setattr(SessionListIndex, "_record", spy_record)
    index = SessionListIndex(root, kind="session_task", reader=lambda p: (json.loads(p.read_text()), None))

    # 冷建库首次走 _record。
    record_calls.clear()
    assert index.page(limit=5)["coverage"] == "complete"
    assert record_calls == ["t0.json"]

    # 新增 t1 后重建：_refresh_entry 对未变的 t0 跳过 _record，仅对新文件 t1 调用 _record。
    (root / "t1.json").write_text(json.dumps(_make_task_payload("t1", 5.0)), encoding="utf-8")
    record_calls.clear()
    assert index.page(limit=5)["coverage"] == "complete"
    assert record_calls == ["t1.json"]

    # publish 保持直接调用 _record 全量重读。
    record_calls.clear()
    index.publish(p0)
    assert record_calls == ["t0.json"]

    # _probe_file_identities 与 page() stale-row 修复均保持直接调用 _record 全量重读。
    with p0.open("r+", encoding="utf-8") as handle:
        data = json.load(handle)
        data["created_at"] = 20.0
        handle.seek(0)
        json.dump(data, handle)
        handle.truncate()
    record_calls.clear()
    with index._connection() as conn:
        assert index._probe_file_identities(conn) is True
    assert record_calls == ["t0.json"]

    with p0.open("r+", encoding="utf-8") as handle:
        data = json.load(handle)
        data["created_at"] = 30.0
        handle.seek(0)
        json.dump(data, handle)
        handle.truncate()
    with monkeypatch.context() as m:
        m.setattr(index, "_probe_file_identities", lambda _conn: False)
        with index._connection() as conn:
            conn.execute("UPDATE entries SET stamp='stale' WHERE path='t0.json'")
            index._put_meta(conn, "complete", "1")
        record_calls.clear()
        index.page(limit=5)
        assert record_calls == ["t0.json"]

    # artifact_tasks() 回填与 stale-ref 修复均保持调用 _record 全量重读。
    with index._connection() as conn:
        conn.execute("DELETE FROM meta WHERE key IN ('artifact_backfill_complete', 'artifact_backfill_after')")
    record_calls.clear()
    assert index.artifact_tasks("art-stale")["coverage"] == "complete"
    assert set(record_calls) == {"t0.json", "t1.json"}

    p0.write_text(json.dumps(_make_task_payload("t0", 30.0, artifact_id="art-new")), encoding="utf-8")
    with index._connection() as conn:
        conn.execute("INSERT OR IGNORE INTO artifact_refs VALUES ('art-stale', 't0.json')")
        conn.execute("UPDATE entries SET stamp='stale'")
    with monkeypatch.context() as m:
        m.setattr(index, "_refresh", lambda _conn: True)
        record_calls.clear()
        res = index.artifact_tasks("art-stale")
        assert res["items"] == []
        assert record_calls == ["t0.json"]

    # 继承 _record 的子类 BackgroundAgentListIndex 同样在重建时跳过未变线程，仅读新增线程。
    bg_root = tmp_path / "threads"
    bg_root.mkdir()
    (bg_root / "th0.json").write_text(json.dumps({"thread_id": "th0", "owner_id": "o1", "updated_at": 1.0}))
    bg_reads: list[str] = []

    def bg_reader(path: Path):
        bg_reads.append(path.name)
        return json.loads(path.read_text(encoding="utf-8")), None

    bg = BackgroundAgentListIndex(bg_root, reader=bg_reader)
    assert bg.page(limit=5, filter_key="o1")["coverage"] == "complete"
    (bg_root / "th1.json").write_text(json.dumps({"thread_id": "th1", "owner_id": "o1", "updated_at": 2.0}))
    bg_reads.clear()
    record_calls.clear()
    assert bg.page(limit=1, filter_key="o1")["coverage"] == "complete"
    assert record_calls == ["th1.json"]
    assert bg_reads == ["th1.json", "th1.json"]  # _record + page 返回项各读一次新增 th1，不重读 th0。

    # 覆写 _record 的子类 SessionTaskPendingIndex 必须委托给自有 _record 维护 task_references.generation。
    pending_root = tmp_path / "pending_tasks"
    pending_root.mkdir()
    (pending_root / "pt0.json").write_text(
        json.dumps({"task_id": "pt0", "status": "done", "created_at": 1.0}),
        encoding="utf-8",
    )
    pending = SessionTaskPendingIndex(pending_root, reader=lambda p: (json.loads(p.read_text()), None))
    assert pending.page(limit=5)["coverage"] == "complete"
    (pending_root / "pt1.json").write_text(
        json.dumps({"task_id": "pt1", "status": "done", "created_at": 2.0,
                    "metadata": {"task_learning_enqueue": {"status": "pending"}}}),
        encoding="utf-8",
    )
    page = pending.page(limit=5)
    assert page["coverage"] == "complete"
    assert [item["task_id"] for item in page["items"]] == ["pt1"]
    with pending._connection() as conn:
        refs = {row[0]: row[1] for row in conn.execute("SELECT path, generation FROM task_references")}
        gen = int(pending._meta(conn, "generation", "0"))
    assert refs == {"pt0.json": gen, "pt1.json": gen}


def test_single_transient_read_error_on_final_publish_heals_on_next_rebuild(tmp_path: Path) -> None:
    root = tmp_path / "tasks"
    root.mkdir()
    payload = _make_task_payload("t0", 1.0, artifact_id="art-0")
    (root / "t0.json").write_text(json.dumps(payload), encoding="utf-8")
    (root / "t1.json").write_text(json.dumps(_make_task_payload("t1", 2.0, artifact_id="art-1")), encoding="utf-8")
    fail_once: set[str] = set()
    reads: list[str] = []

    def reader(path: Path):
        reads.append(path.name)
        if path.name in fail_once:
            fail_once.discard(path.name)  # one simulated I/O error; the file is readable afterwards
            return {}, {"code": "SESSION_TASK_READ_FAILED", "path": str(path)}
        return json.loads(path.read_text(encoding="utf-8")), None

    index = SessionListIndex(root, kind="session_task", reader=reader)
    assert index.artifact_tasks("art-0")["coverage"] == "complete"

    # Final canonical write (atomic replace, as SessionTaskStore does); its list-index publish hits one read error.
    staged = root / "t0.json.tmp"
    staged.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(staged, root / "t0.json")
    fail_once.add("t0.json")
    index.publish(root / "t0.json")
    with index._connection() as conn:
        assert [row[0] for row in conn.execute("SELECT task_path FROM artifact_errors")] == ["t0.json"]
    info = root.stat()  # deterministic rebuild trigger on coarse-timestamp filesystems
    os.utime(root, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))

    reads.clear()
    healed = index.artifact_tasks("art-0")
    assert healed["coverage"] == "complete"
    assert [item["task_id"] for item in healed["items"]] == ["t0"]
    assert "t1.json" not in reads  # the healthy unchanged row is still not re-read
    assert index.artifact_tasks("art-1")["coverage"] == "complete"
    with index._connection() as conn:
        assert conn.execute("SELECT 1 FROM artifact_errors LIMIT 1").fetchone() is None


def test_canonical_reply_linkage_single_read_error_converges_via_directory_refresh(tmp_path: Path) -> None:
    from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact
    from agent_py_agent.agent.common.json_io import write_json_file_atomic
    from agent_py_agent.agent.conversation import ConversationStore, session_tasks
    from agent_py_agent.agent.conversation.business_access import BusinessActor
    from agent_py_agent.agent.conversation.business_observability import ObservationSourceError
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft, SessionTaskUpdate
    from agent_py_agent.agent.conversation.task_artifact_observation import task_artifact
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_quota import owner_quota_enforcer_from_policy

    home = ensure_my_agent_home(tmp_path / "app")
    owner = home.owner_home_dir
    owner_id = owner.relative_to(home.owners_dir).as_posix()
    policy = json.loads(home.owner_quota_json.read_text(encoding="utf-8"))
    policy["max_disk_mb"] = 1024
    write_json_file_atomic(home.owner_quota_json, policy)
    assert owner_quota_enforcer_from_policy(owner).max_bytes == 1024 * 1024 * 1024

    store = ConversationStore(owner / "conversations")
    service = SimpleNamespace(owner_id=owner_id)
    authority = (service, BusinessActor(owner_id, "audit-observer", service, lambda *_: True), "artifacts.get")
    thread = store.threads.create(owner_id=owner_id)

    task = store.session_tasks.create(SessionTaskDraft(thread.thread_id, thread.thread_id, "produce artifact"))
    store.session_tasks.advance(task.task_id, SessionTaskUpdate(status="accepted"))
    assert store.session_tasks._list_index().artifact_tasks("absent")["coverage"] == "complete"

    task_root = owner / "tasks" / task.task_id
    artifact_path = task_root / "output.txt"
    task_root.mkdir(parents=True)
    artifact_path.write_text("verified output\n", encoding="utf-8")
    artifact = register_artifact(
        ArtifactRegistration(
            task_root,
            artifact_path,
            artifact_id="artifact-under-review",
            task_id=task.task_id,
            mime_type="text/plain",
            source="write_file",
            status="ready",
        )
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": task.task_id, "goal": task.goal, "task_path": str(task_root)}
    )
    completed = store.session_tasks.advance(
        task.task_id,
        SessionTaskUpdate(
            status="done",
            result_refs=(str(artifact_path),),
            result_artifacts=(
                {
                    "path": str(artifact_path),
                    "artifact_id": artifact.artifact_id,
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                },
            ),
        ),
    )

    healthy = store.session_tasks.create(SessionTaskDraft(thread.thread_id, thread.thread_id, "independent artifact"))
    store.session_tasks.advance(healthy.task_id, SessionTaskUpdate(status="accepted"))
    healthy_root = owner / "tasks" / healthy.task_id
    healthy_root.mkdir(parents=True)
    healthy_path = healthy_root / "output.txt"
    healthy_path.write_text("independent verified output\n", encoding="utf-8")
    healthy_artifact = register_artifact(
        ArtifactRegistration(
            healthy_root,
            healthy_path,
            artifact_id="healthy-artifact",
            task_id=healthy.task_id,
            mime_type="text/plain",
            source="write_file",
            status="ready",
        )
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": healthy.task_id,
            "goal": healthy.goal,
            "task_path": str(healthy_root),
        }
    )
    store.session_tasks.advance(
        healthy.task_id,
        SessionTaskUpdate(
            status="done",
            result_refs=(str(healthy_path),),
            result_artifacts=(
                {
                    "path": str(healthy_path),
                    "artifact_id": healthy_artifact.artifact_id,
                    "sha256": healthy_artifact.sha256,
                    "size_bytes": healthy_artifact.size_bytes,
                },
            ),
        ),
    )

    ready = task_artifact(store, owner, owner_id, artifact.artifact_id, authority=authority)
    assert ready[0] and ready[1]["available"]
    healthy_ready = task_artifact(store, owner, owner_id, healthy_artifact.artifact_id, authority=authority)
    assert healthy_ready[0] and healthy_ready[1]["available"]

    index = store.session_tasks._list_index()
    with index._connection() as conn:
        assert index._meta(conn, "artifact_backfill_complete") == "1"

    original_read_bytes = session_tasks.read_bytes_beneath
    error_count = 0
    target = store.session_tasks._path(task.task_id)

    def one_source_error(root_dir, parts, **kwargs):
        nonlocal error_count
        if Path(root_dir) / Path(*parts) == target and error_count == 0:
            error_count += 1
            raise OSError(5, "simulated I/O error")
        return original_read_bytes(root_dir, parts, **kwargs)

    with patch.object(session_tasks, "read_bytes_beneath", one_source_error):
        settled = store.session_tasks.record_turn_reply_linkage(
            task.task_id,
            reply_message_id="delivered-reply",
            result_report_receipt="settled-receipt",
        )
    assert error_count == 1
    assert completed.status == "done" and settled is not None and settled.status == "done"
    assert settled.metadata.get("reply_message_id") == "delivered-reply"
    assert settled.metadata.get("result_report_receipt") == "settled-receipt"
    canonical_bytes = target.read_bytes()

    with index._connection() as conn:
        assert [row[0] for row in conn.execute("SELECT task_path FROM artifact_errors")] == [target.name]

    def observe(identifier: str) -> dict[str, object]:
        try:
            result = task_artifact(store, owner, owner_id, identifier, authority=authority)
            return {"found": result[0], "available": result[1]["available"] if result[1] else None}
        except ObservationSourceError as error:
            return {"error": error.code, "reason": error.details.get("reason")}

    for number in range(8):
        store.session_tasks.create(SessionTaskDraft(thread.thread_id, thread.thread_id, f"independent {number}"))
        page = store.session_tasks.list_page(limit=100)
        assert page["coverage"] == "complete"
        assert page["load_errors"] == []

    assert error_count == 1
    assert target.read_bytes() == canonical_bytes
    assert observe(artifact.artifact_id) == {"found": True, "available": True}
    assert observe(healthy_artifact.artifact_id) == {"found": True, "available": True}
    with index._connection() as conn:
        assert conn.execute("SELECT count(*) FROM artifact_errors").fetchone()[0] == 0
        assert index._meta(conn, "artifact_backfill_complete") == "1"
