"""Event 控制真实文件交错，不能靠调度概率验证单调不变量。"""
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft, SessionTaskUpdate
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation.store_claims import ClaimStore
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.user_space import owner_epoch as epoch


@pytest.fixture
def stores(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    repo = RuntimeRepository(tmp_path / "runtime.db")
    config = SimpleNamespace(owner_epoch_fence_enabled=True)
    store.storage.owner_epoch_repository = repo
    store.storage.owner_epoch_config = config
    claims = ClaimStore(store.storage,
                        require_thread=lambda tid: SimpleNamespace(thread_id=tid),
                        load_thread=lambda tid: SimpleNamespace(thread_id=tid))
    return store, claims, repo, config


@pytest.mark.parametrize("kind", ["task", "claim"])
def test_late_old_epoch_closeout_writes_nothing(stores, kind):
    store, claims, repo, config = stores
    old = epoch.activate_owner_epoch(repo)
    task = store.session_tasks.create(SessionTaskDraft("sender", "target", "work"))
    claim = claims.acquire({"thread_id": "target", "task_id": task.task_id})
    path = (store.session_tasks._path(task.task_id) if kind == "task"
            else store.storage.background_claim_path("target"))
    before = path.read_bytes()
    ready, release = threading.Event(), threading.Event()

    def late_finish():
        with epoch.request_epoch_scope(repo, old, config):
            ready.set()
            assert release.wait(5)
            with pytest.raises(epoch.StaleOwnerEpoch):
                if kind == "task":
                    store.session_tasks.advance(task.task_id, SessionTaskUpdate("cancelled"))
                else:
                    claims.finish({"thread_id": "target", "claim_id": claim["claim_id"], "status": "cancelled"})

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(late_finish)
        assert ready.wait(5)
        epoch.activate_owner_epoch(repo)
        release.set()
        pending.result(timeout=5)
    assert path.read_bytes() == before


def test_cancel_then_late_bind_body_preserves_terminal_and_proof(stores):
    store, _, _, _ = stores
    task = store.session_tasks.create(SessionTaskDraft("sender", "target", "work"))
    entered, release = threading.Event(), threading.Event()

    def bind():
        entered.set()
        assert release.wait(5)
        return store.session_tasks.bind_body(task.task_id, "guidance-late")

    with ThreadPoolExecutor(max_workers=1) as pool:
        late = pool.submit(bind)
        assert entered.wait(5)
        terminal = store.session_tasks.advance(task.task_id, SessionTaskUpdate("cancelled", result_refs=("exit-proof",)))
        release.set()
        bound = late.result(timeout=5)
    assert bound.status == terminal.status == "cancelled"
    assert bound.result_refs == ("exit-proof",)


def test_finish_renew_acquire_archive_are_serialised(stores):
    store, claims, _, _ = stores
    first = claims.acquire({"thread_id": "target", "task_id": "first", "now": 1})
    ready, release = threading.Event(), threading.Event()

    def late_renew():
        ready.set()
        assert release.wait(5)
        return claims.renew({"thread_id": "target", "claim_id": first["claim_id"], "now": 3})

    with ThreadPoolExecutor(max_workers=1) as pool:
        late = pool.submit(late_renew)
        assert ready.wait(5)
        finished = claims.finish({"thread_id": "target", "claim_id": first["claim_id"], "status": "cancelled",
                                  "runtime_facts": {"exit_proof": "observed"}, "now": 2})
        release.set()
        assert late.result(timeout=5) is None
    assert finished["last_runtime_facts"] == {"exit_proof": "observed"}
    second = claims.acquire({"thread_id": "target", "task_id": "second", "now": 3})
    assert second is not None
    assert claims.acquire({"thread_id": "target", "task_id": "third", "now": 3}) is None
    assert claims.archive_stale(current=9, retention_seconds=1) == 0
    assert claims.load("target")["claim_id"] == second["claim_id"]
