"""Conversation ledger keys resolve to the stable task path, not the per-request task id.

真机铁证(2026-08-07, celery→Go 复刻):gateway 每轮 /ask 派生新任务身份
(req_2 → req_2-continue → req_3 → req_0,全部指向同一 task_path),账本立在首轮
req_2 上;后续请求按新 conversation_task_id 读账本=读错位=0 pending=续跑不触发=
每轮只干一点就收口。task_path 在线程内稳定,账本 key 必须按它寻址。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.runtime.task_identity import progress_ledger_id


def _link(task_path: str) -> SimpleNamespace:
    return SimpleNamespace(task_path=task_path)


class _Store:
    def __init__(self, task_paths: dict[str, str]) -> None:
        self._task_paths = task_paths
        self.loaded: list[str] = []

    def load_task_link(self, task_id: str) -> SimpleNamespace | None:
        self.loaded.append(task_id)
        path = self._task_paths.get(task_id)
        return _link(path) if path is not None else None


def _params(
    *,
    conversation_task_id: str = "req_1",
    thread_id: str = "thread-abc",
    scope: str = "default",
    source: str = "gateway",
) -> SimpleNamespace:
    return SimpleNamespace(
        context_scope=scope,
        source=source,
        task_attributes={
            "conversation_thread_id": thread_id,
            "conversation_task_id": conversation_task_id,
        },
    )


def _agent(store: _Store | None) -> SimpleNamespace:
    return SimpleNamespace(conversation_store=store)


def test_same_task_path_shared_across_requests() -> None:
    # 同一 goal 的多轮请求:conversation_task_id 每轮漂移,但 task_path 稳定
    # → 账本 key 必须相同,否则续跑读错位账本。
    task_path = "/root/.my-agent/owners/test/users/user-b/tasks/2026-08-07/celery"
    store = _Store(
        {
            "req_2": task_path,
            "req_2-continue-59de46fb": task_path,
            "req_3": task_path,
            "req_0": task_path,
        }
    )
    keys = {
        progress_ledger_id(_agent(store), _params(conversation_task_id=tid))
        for tid in ("req_2", "req_2-continue-59de46fb", "req_3", "req_0")
    }
    assert len(keys) == 1
    key = keys.pop()
    assert key.startswith("task-path:")


def test_different_task_paths_get_different_keys() -> None:
    # 同一线程先后两个不同任务(不同目录):账本必须分账,不能混。
    store = _Store(
        {
            "req_a1": "/tasks/2026-08-07/task-a",
            "req_b1": "/tasks/2026-08-07/task-b",
        }
    )
    key_a = progress_ledger_id(_agent(store), _params(conversation_task_id="req_a1"))
    key_b = progress_ledger_id(_agent(store), _params(conversation_task_id="req_b1"))
    assert key_a != key_b


def test_missing_task_path_falls_back_to_conversation_task_id() -> None:
    # 任务尚未 materialize 路径时:回落到 conversation_task_id(首轮 id 恰是账本所在)。
    store = _Store({"req_2": ""})
    key = progress_ledger_id(_agent(store), _params(conversation_task_id="req_2"))
    assert key == "req_2"


def test_unknown_task_link_falls_back_to_conversation_task_id() -> None:
    # link 不存在(如历史任务被清理):回落 conversation_task_id,不伪造路径 key。
    store = _Store({})
    key = progress_ledger_id(_agent(store), _params(conversation_task_id="req_9"))
    assert key == "req_9"


def test_no_store_falls_back_to_conversation_task_id() -> None:
    # CLI 直连等无 conversation_store 场景:保持原行为。
    key = progress_ledger_id(_agent(None), _params(conversation_task_id="req_2"))
    assert key == "req_2"


def test_loader_exception_falls_back_to_conversation_task_id() -> None:
    class _BrokenStore:
        def load_task_link(self, task_id: str) -> SimpleNamespace:
            raise RuntimeError("boom")

    key = progress_ledger_id(_agent(_BrokenStore()), _params(conversation_task_id="req_2"))
    assert key == "req_2"


def test_non_main_scope_ignores_conversation_binding() -> None:
    # 子代理(scope 非 main)按自己 scoped/run 隔离,不走会话任务路径。
    store = _Store({"req_2": "/tasks/2026-08-07/task-a"})
    key = progress_ledger_id(
        _agent(store),
        _params(conversation_task_id="req_2", scope="subagent"),
        scoped_id="child-run-1",
    )
    assert key == "child-run-1"


def test_no_conversation_task_id_falls_through_to_scoped() -> None:
    key = progress_ledger_id(_agent(None), _params(conversation_task_id=""), scoped_id="scoped-1")
    assert key == "scoped-1"


def test_plain_request_falls_through_to_run_id() -> None:
    params = SimpleNamespace(
        context_scope="default",
        source="gateway",
        task_attributes={},
        run_id="run-1",
        task_id="task-1",
        request_id="req-1",
    )
    key = progress_ledger_id(_agent(None), params)
    assert key == "run-1"
