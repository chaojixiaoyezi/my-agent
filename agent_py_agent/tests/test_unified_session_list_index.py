from dataclasses import replace
from types import SimpleNamespace

from agent_py_agent.agent.conversation import ConversationStore
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
