"""C5 登记组件探针固化（2026-10-02，ds2）：run_claim 的登记 / 车道闸 / 撤销在失败与取消路径下的行为。

来源：sol2 只读审查 C5 的“建议修”——把审查时临时使用的登记组件探针固化成仓库测试。
被测实现：sol2 竞态修复（车道闸，提交 ad8e3d74d，集成分支已有）之后的 run_claim：
`conversation_run_lane` 用 `_user_input_turn_registered`（登记 + finally 撤销）包住 `_held_conversation_run_lane`
（领取 → 心跳 → finally 释放租约）；`user_input_turn_gate` 让熔断的“判定 + 落账”与用户回合登记互斥。

每条用例都断言三件事：
1. 进程内登记计数最终回到 0（`user_input_turn_on_lane` 为假），不会漏撤销；
2. 车道闸被回收或该车道可被再次获取（登记表里不留悬挂键）；
3. 登记在撤销前确实生效过（避免断言恒真的假绿——这正是竞态那轮踩过的坑）。

用真实 `conversation_run_lane` / `SimpleAgent` 的 ConversationStore / 临时目录；不打桩替换被测函数本身。
唯一打桩处是“让收尾必然抛错”与“让心跳线程启动必然失败”的注入点，它们不是被断言的被测逻辑。
"""
from __future__ import annotations

import threading
import time

import pytest

from agent_py_agent.agent.concurrency.interrupt import is_interrupted
from agent_py_agent.agent.conversation import run_claim as run_claim_module
from agent_py_agent.agent.conversation import store_claims as store_claims_module
from agent_py_agent.agent.conversation.run_claim import (
    ConversationRunLaneRequest,
    conversation_run_lane,
    user_input_turn_gate,
    user_input_turn_on_lane,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings.config import AgentConfig


def _store_and_thread(tmp_path, name: str):
    """建一个真实 Store 和会话；每个用例用独立 owner home，互不共享登记键。"""
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / name / "home"), prompt_files=[]),
        tmp_path / name / "ws",
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {"canonical_user_id": "test-user", "channel": "internal", "channel_conversation_id": name}
    )
    return store, thread.thread_id


def _lane(store, thread_id, interrupt=lambda: False) -> ConversationRunLaneRequest:
    """带用户消息的车道请求（本文件所有用例都用这一种），参数收成 3 个。"""
    return ConversationRunLaneRequest(
        store=store, thread_id=thread_id, claim_task_id="gateway:req-c5", reason="gateway_foreground",
        lease_seconds=60, heartbeat_interval_seconds=30.0, interrupt_check=interrupt,
        carries_user_input=True,
    )


def _wait_until(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "等待条件超时"
        time.sleep(0.01)


def _lane_key(store, thread_id: str) -> str:
    """被测登记键 = 本会话执行租约文件路径；测试用它直接核对登记表内容。"""
    return str(store.storage.background_claim_path(thread_id))


def _acquire_at(store, thread_id: str, task_id: str, current: float):
    return store.claims.acquire(
        {
            "thread_id": thread_id,
            "task_id": task_id,
            "reason": "test",
            "lease_seconds": 10,
            "now": current,
        }
    )


def _prepare_dead_owner(tmp_path, monkeypatch, name: str, new_process: dict):
    store, thread_id = _store_and_thread(tmp_path, name)
    original = _acquire_at(store, thread_id, "old-attempt", 100.0)
    assert original is not None
    monkeypatch.setattr(store_claims_module, "build_process_identity", lambda: dict(new_process))
    monkeypatch.setattr(store_claims_module, "process_identity_is_live", lambda value: value == new_process)
    return store, thread_id, original


def _parallel_claims(store, thread_id: str):
    barrier = threading.Barrier(3)
    claims = []

    def acquire() -> None:
        barrier.wait()
        claims.append(_acquire_at(store, thread_id, "new-attempt", 111.0))

    workers = [threading.Thread(target=acquire) for _ in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(3)
    return claims


def _assert_claim_lost(claim: dict, reason: str, error_type: str) -> None:
    claim_lost = claim.get("claim_lost")
    assert isinstance(claim_lost, dict)
    assert claim_lost.get("state") == "claim_lost"
    assert claim_lost.get("reason") == reason
    assert claim_lost.get("error_type", "") == error_type


def _attempt_new_action(started_actions: list[str]) -> None:
    if is_interrupted():
        raise InterruptedError("安全点观察到 claim 丢失")
    started_actions.append("new action")


# LLM: 这条 helper 只是把“期待抛错 + 跑一个用户回合”的两层 with 收在一处，行为仍走真实 conversation_run_lane。
# 函数用途: 跑一个用户回合并期待它抛出 exc_type；body 为 None 时回合体里不做任何断言（“不该进入”的用例用）。
def _run_lane_expecting(store, thread_id, exc_type, body=None) -> None:
    with pytest.raises(exc_type):
        _lane_body(store, thread_id, body)


# LLM: 单独一层是为了让上面的 pytest.raises 只里套一次 with，尺寸守卫按嵌套层数计。
# 函数用途: 在真实车道里跑 body（None 则空跑），异常按原样向上抛。
def _lane_body(store, thread_id, body) -> None:
    with conversation_run_lane(_lane(store, thread_id)):
        if body is not None:
            body()


def _assert_lane_reusable(store, thread_id: str) -> None:
    """车道闸可回收/车道可再次获取：占位租约释放后能重新领到执行权。"""
    again = store.claims.acquire(
        {"thread_id": thread_id, "task_id": "probe-reuse", "reason": "background", "lease_seconds": 60}
    )
    assert again is not None, "前一片结束后车道应能被再次领取"
    store.claims.finish(
        {"thread_id": thread_id, "claim_id": again["claim_id"], "task_id": "probe-reuse", "status": "finished"}
    )


# ---------------------------------------------------------------- 1. 领取车道失败


def test_acquire_failure_unregisters_and_leaves_the_lane_gate_reclaimable(tmp_path):
    """触发方式：先让占位租约持有车道，并让 interrupt_check 立刻为真；用户回合的首次领取未成功就中断。"""
    store, thread_id = _store_and_thread(tmp_path, "acquire-failure")
    holder = store.claims.acquire(
        {"thread_id": thread_id, "task_id": "holder", "reason": "background", "lease_seconds": 60}
    )
    assert holder is not None
    with pytest.raises(InterruptedError):
        with conversation_run_lane(_lane(store, thread_id, interrupt=lambda: True)):
            pytest.fail("领取失败时不应进入回合体")

    assert not user_input_turn_on_lane(store, thread_id)
    assert store.claims.load(thread_id).get("task_id") == "holder", "领取失败不得动到别人的租约"
    store.claims.finish(
        {"thread_id": thread_id, "claim_id": holder["claim_id"], "task_id": "holder", "status": "finished"}
    )
    _assert_lane_reusable(store, thread_id)


# ---------------------------------------------------------------- 2. 回合执行中抛错


def test_turn_error_releases_claim_and_unregisters(tmp_path):
    """触发方式：回合体内部抛 RuntimeError；与正常结束走同一 finally，登记和租约都应收尾。"""
    store, thread_id = _store_and_thread(tmp_path, "turn-error")
    seen = {}

    def turn_body() -> None:
        with conversation_run_lane(_lane(store, thread_id)) as claim:
            seen["claim"] = claim.get("claim_id")
            seen["registered"] = user_input_turn_on_lane(store, thread_id)
            raise RuntimeError("boom-turn")

    with pytest.raises(RuntimeError, match="boom-turn"):
        turn_body()
    assert seen["claim"], "必须先真的领到租约"
    assert seen["registered"], "执行期间登记应生效"

    assert not user_input_turn_on_lane(store, thread_id)
    finished = store.claims.load(thread_id)
    assert finished.get("status") == "failed", "抛错结束要把租约记成 failed 而不是仍在运行"
    assert finished.get("phase") == "failed"
    _assert_lane_reusable(store, thread_id)


# ---------------------------------------------------------------- 3. 收尾阶段抛错


def test_error_while_finishing_still_unregisters(tmp_path):
    """触发方式：把租约文件的原子写换成必抛实现（只用于制造收尾失败），回合体本身正常结束。

    这里“收尾阶段抛错”同时命中领取：`conversation_run_lane` 先登记、再 `_acquire_conversation_run_claim`，
    而领取也走同一个原子写，所以异常在进入回合体之前抛出、租约文件从未落盘。
    要固定的是登记语义：例外从 `with` 里穿出来时，`_user_input_turn_registered.finally` 仍要把登记撤销回 0，
    且该车道随后可被重新领取（不留悬挂键、不会让熔断永远判不了空片）。
    """
    store, thread_id = _store_and_thread(tmp_path, "finish-error")
    from agent_py_agent.agent.conversation import store_claims

    original_write = store_claims.update_json_file_atomic
    calls: list[object] = []

    def failing_write(path, updater):
        calls.append(path)
        raise RuntimeError("boom-finish")

    store_claims.update_json_file_atomic = failing_write  # type: ignore[assignment]
    try:
        _run_lane_expecting(store, thread_id, RuntimeError)
    finally:
        store_claims.update_json_file_atomic = original_write  # type: ignore[assignment]

    assert len(calls) == 1, "原子写只应被调用一次（领取阶段），说明异常在领取时就抛出"
    assert not user_input_turn_on_lane(store, thread_id), "收尾抛错不得让登记残留"
    assert store.claims.load(thread_id) == {}, "租约文件从未落盘（行为锁定）"
    _assert_lane_reusable(store, thread_id)


# ---------------------------------------------------------------- 4. 心跳启动失败


def test_heartbeat_start_failure_unregisters(tmp_path):
    """触发方式：把心跳线程的 start 换成必抛实现（只用于制造启动失败），租约已领到、回合体从未进入。

    现在的 `_held_conversation_run_lane` 在 start() 处抛错时，finally 尚未建立，因此不会调用
    `heartbeat.stop()`（未启动的线程没有可 join 的运行体），也不会释放刚领到的租约——租约留在
    `running`，要靠租约 TTL 或宿主终态收尾。这是审查建议里点名要固化的行为，本用例如实锁定它，
    同时确认登记仍回到 0、车道闸不留悬挂键。
    """
    store, thread_id = _store_and_thread(tmp_path, "heartbeat-start")
    import agent_py_agent.agent.conversation.run_claim as run_claim_module

    started: list[object] = []

    def failing_start(heartbeat) -> None:
        started.append(heartbeat)
        raise RuntimeError("boom-heartbeat-start")

    original_start = run_claim_module.ConversationRunClaimHeartbeat.start
    run_claim_module.ConversationRunClaimHeartbeat.start = failing_start
    try:
        _run_lane_expecting(store, thread_id, RuntimeError)
    finally:
        run_claim_module.ConversationRunClaimHeartbeat.start = original_start

    assert len(started) == 1, "start 应被调用一次后失败"
    assert not user_input_turn_on_lane(store, thread_id)
    assert store.claims.load(thread_id).get("status") == "running", "心跳未启动时租约保持 running（行为锁定）"
    # 车道闸不回收该 running 租约：把它按终态收尾后才能重新领取。
    store.claims.finish(
        {"thread_id": thread_id, "claim_id": store.claims.load(thread_id).get("claim_id"),
         "task_id": "gateway:req-c5", "status": "cancelled"}
    )
    _assert_lane_reusable(store, thread_id)


# ---------------------------------------------------------------- 5. 同一会话两个等待者分别取消


def test_two_waiters_on_one_lane_cancel_independently(tmp_path):
    """触发方式：同一会话两个携带用户消息的回合同时排队（车道被占位租约占住），各自被自己的 interrupt 取消。"""
    store, thread_id = _store_and_thread(tmp_path, "two-waiters")
    import agent_py_agent.agent.conversation.run_claim as run_claim_module

    holder = store.claims.acquire(
        {"thread_id": thread_id, "task_id": "holder", "reason": "background", "lease_seconds": 60}
    )
    assert holder is not None
    stops = {1: threading.Event(), 2: threading.Event()}
    observed: dict[int, list[bool]] = {1: [], 2: []}

    def waiter(index: int) -> None:
        def interrupt_check() -> bool:
            observed[index].append(user_input_turn_on_lane(store, thread_id))
            return stops[index].is_set()

        try:
            with conversation_run_lane(_lane(store, thread_id, interrupt=interrupt_check)):
                pytest.fail("车道被占时不应进入回合体")
        except InterruptedError:
            pass

    first = threading.Thread(target=waiter, args=(1,), daemon=True)
    second = threading.Thread(target=waiter, args=(2,), daemon=True)
    first.start()
    second.start()
    _wait_until(lambda: observed[1] and observed[2])
    assert observed[1][0] and observed[2][0], "两个等待者排队期间都应登记在场"

    stops[1].set()
    first.join(10)
    assert not first.is_alive()
    assert user_input_turn_on_lane(store, thread_id), "取消一个等待者不得撤销另一个的登记"

    stops[2].set()
    second.join(10)
    assert not second.is_alive()
    assert not user_input_turn_on_lane(store, thread_id), "两个都取消后登记计数应回到 0"
    assert run_claim_module._USER_INPUT_TURNS == {}, "登记表不应残留任何会话的键"

    store.claims.finish(
        {"thread_id": thread_id, "claim_id": holder["claim_id"], "task_id": "holder", "status": "finished"}
    )
    _assert_lane_reusable(store, thread_id)


# ---------------------------------------------------------------- 6. 跨会话互不影响


def test_two_threads_in_one_store_do_not_share_registration_or_gate(tmp_path):
    """触发方式：同一 Store 两个会话，A 上跑用户回合，检查 B 的登记查询与车道闸都不受影响。"""
    store, thread_a = _store_and_thread(tmp_path, "two-threads")
    import agent_py_agent.agent.conversation.run_claim as run_claim_module

    thread_b = store.threads.get_or_create(
        {"canonical_user_id": "test-user", "channel": "internal", "channel_conversation_id": "two-threads-b"}
    ).thread_id
    assert store.storage.background_claim_path(thread_a) != store.storage.background_claim_path(thread_b)
    leave = threading.Event()
    inside = threading.Event()

    def user_turn_a() -> None:
        with conversation_run_lane(_lane(store, thread_a)):
            inside.set()
            leave.wait(10)

    worker = threading.Thread(target=user_turn_a, daemon=True)
    worker.start()
    _wait_until(inside.is_set)
    assert user_input_turn_on_lane(store, thread_a)
    assert not user_input_turn_on_lane(store, thread_b), "B 会话不应被 A 的登记带上"
    # 登记表本身：A 的键（租约文件路径）在、计数为 1；B 的键完全不在。
    # 这条直接核对登记表的断言，是“登记必须经车道闸”的回归位——登记绕过闸时键的形状/内容会变。
    assert run_claim_module._USER_INPUT_TURNS.get(_lane_key(store, thread_a)) == 1
    assert _lane_key(store, thread_b) not in run_claim_module._USER_INPUT_TURNS
    with user_input_turn_gate(store, thread_b) as present:
        assert present is False, "B 的车道闸应看到“不在场”"
    leave.set()
    worker.join(10)
    assert not user_input_turn_on_lane(store, thread_a)
    _assert_lane_reusable(store, thread_a)


# ---------------------------------------------------------------- 7. 不同 owner 路径互相隔离


def test_two_owner_stores_are_isolated_even_for_same_named_thread(tmp_path):
    """触发方式：两个独立 owner home 各建一个同名会话；B owner 上的用户回合不得影响 A owner 的登记。"""
    store_a, thread_a = _store_and_thread(tmp_path, "owner-a")
    store_b, thread_b = _store_and_thread(tmp_path, "owner-b")
    assert store_a.storage.root != store_b.storage.root
    assert store_a.storage.background_claim_path(thread_a) != store_b.storage.background_claim_path(thread_b)
    leave = threading.Event()
    inside = threading.Event()

    def user_turn_b() -> None:
        with conversation_run_lane(_lane(store_b, thread_b)):
            inside.set()
            leave.wait(10)

    worker = threading.Thread(target=user_turn_b, daemon=True)
    worker.start()
    _wait_until(inside.is_set)
    assert user_input_turn_on_lane(store_b, thread_b)
    assert not user_input_turn_on_lane(store_a, thread_a), "另一个 owner 的同名会话不应被登记带上"
    leave.set()
    worker.join(10)
    assert not user_input_turn_on_lane(store_b, thread_b)
    assert not user_input_turn_on_lane(store_a, thread_a)
    _assert_lane_reusable(store_b, thread_b)


@pytest.mark.parametrize(
    ("owner_liveness", "expected_liveness"),
    [(True, "alive"), (None, "unverifiable")],
    ids=["alive", "unverifiable"],
)
def test_expired_live_or_unverifiable_owner_stays_recovery_pending(
    tmp_path, monkeypatch, owner_liveness, expected_liveness
):
    store, thread_id = _store_and_thread(tmp_path, f"expired-{expected_liveness}")
    original = _acquire_at(store, thread_id, "old-attempt", 100.0)
    assert original is not None
    monkeypatch.setattr(
        store_claims_module, "process_identity_is_live", lambda _identity: owner_liveness
    )

    replacement = _acquire_at(store, thread_id, "new-attempt", 111.0)

    assert replacement is None, "过期不能把存活或不可核验的执行者当成死亡"
    current = store.claims.load(thread_id)
    assert current["claim_id"] == original["claim_id"]
    assert current.get("claim_epoch") == 1
    assert current["phase"] == "recovery_pending"
    assert current["takeover"] == {
        "allowed": False,
        "reason": "recovery_pending",
        "owner_liveness": expected_liveness,
    }


def test_dead_owner_takeover_increments_one_epoch_for_concurrent_claimers(tmp_path, monkeypatch):
    new_process = {"host_id": "test-host", "pid": 99001, "start_time": 123.0}
    store, thread_id, original = _prepare_dead_owner(
        tmp_path, monkeypatch, "dead-owner-takeover", new_process
    )
    granted = [claim for claim in _parallel_claims(store, thread_id) if claim is not None]
    assert len(granted) == 1, "同一死亡证明只能通过一次原子接管"
    assert original.get("claim_epoch") == 1
    assert granted[0].get("claim_epoch") == 2
    current = store.claims.load(thread_id)
    assert current["claim_id"] == granted[0]["claim_id"]
    assert current.get("claim_epoch") == 2


def test_old_heartbeat_first_renews_same_epoch_and_blocks_new_claim(tmp_path, monkeypatch):
    store, thread_id = _store_and_thread(tmp_path, "renew-before-takeover")
    original = _acquire_at(store, thread_id, "old-attempt", 100.0)
    assert original is not None
    monkeypatch.setattr(store_claims_module, "process_identity_is_live", lambda _identity: True)

    renewed = store.claims.renew(
        {
            "thread_id": thread_id,
            "claim_id": original["claim_id"],
            "claim_epoch": 1,
            "lease_seconds": 10,
            "now": 111.0,
        }
    )
    competitor = _acquire_at(store, thread_id, "new-attempt", 112.0)

    assert renewed is not None
    assert renewed.get("claim_epoch") == 1
    assert competitor is None
    current = store.claims.load(thread_id)
    assert current["claim_id"] == original["claim_id"]
    assert current.get("claim_epoch") == 1
    assert current["expires_at"] == 121.0


def test_old_epoch_cannot_renew_or_finish_even_when_claim_id_matches(tmp_path, monkeypatch):
    new_process = {"host_id": "test-host", "pid": 99002, "start_time": 124.0}
    store, thread_id, _original = _prepare_dead_owner(
        tmp_path, monkeypatch, "epoch-fence", new_process
    )
    replacement = _acquire_at(store, thread_id, "new-attempt", 111.0)
    assert replacement is not None
    assert replacement.get("claim_epoch") == 2

    stale_renewal = store.claims.renew(
        {
            "thread_id": thread_id,
            "claim_id": replacement["claim_id"],
            "claim_epoch": 1,
            "lease_seconds": 10,
            "now": 112.0,
        }
    )
    stale_finish = store.claims.finish(
        {
            "thread_id": thread_id,
            "claim_id": replacement["claim_id"],
            "claim_epoch": 1,
            "task_id": "stale-attempt",
            "status": "failed",
            "now": 112.0,
        }
    )

    assert stale_renewal is None, "相同 claim_id 的旧 epoch 不能续租新执行者"
    assert stale_finish is None, "相同 claim_id 的旧 epoch 不能收尾新执行者"
    current = store.claims.load(thread_id)
    assert current["status"] == "running"
    assert current.get("claim_epoch") == 2
    assert current["task_id"] == "new-attempt"


class _OneHeartbeatWait:
    """只让后台心跳执行一次；测试同步，不等待真实租约时间。"""

    def __init__(self):
        self._waited = False
        self._stopped = False

    def wait(self, _timeout):
        if self._stopped:
            return True
        if self._waited:
            return True
        self._waited = True
        return False

    def set(self):
        self._stopped = True


class _ImmediateHeartbeatStart:
    """启动真实心跳线程，但将等待替换为一次受控迭代。"""

    def __init__(self, actual_start, heartbeats):
        self._actual_start = actual_start
        self._heartbeats = heartbeats

    def __call__(self, heartbeat):
        heartbeat.stop_event = _OneHeartbeatWait()
        self._heartbeats.append(heartbeat)
        self._actual_start(heartbeat)

    def __get__(self, instance, _owner):
        return lambda: self(instance) if instance is not None else self


@pytest.fixture(
    params=[
        {"error": None, "epoch_mismatch": False, "reason": "renew_rejected", "error_type": ""},
        {"error": OSError("offline"), "epoch_mismatch": False, "reason": "renew_exception", "error_type": "OSError"},
        {"error": None, "epoch_mismatch": True, "reason": "epoch_mismatch", "error_type": ""},
    ],
    ids=["renew-rejected", "renew-exception", "epoch-replaced"],
)
def claim_loss_scenario(request):
    return request.param


def _configure_claim_loss(claims, monkeypatch, scenario, heartbeats):
    renewal_attempted = threading.Event()
    allow_renewal_return = threading.Event()

    def lose_claim(request):
        renewal_attempted.set()
        assert allow_renewal_return.wait(1), "测试应释放受控续租结果"
        if scenario["error"] is not None:
            raise scenario["error"]
        if scenario["epoch_mismatch"]:
            return {**request, "claim_epoch": request["claim_epoch"] + 1}
        return None

    monkeypatch.setattr(claims, "renew", lose_claim)
    starter = _ImmediateHeartbeatStart(
        run_claim_module.ConversationRunClaimHeartbeat.start, heartbeats
    )
    monkeypatch.setattr(run_claim_module.ConversationRunClaimHeartbeat, "start", starter)
    return renewal_attempted, allow_renewal_return


def test_lost_claim_is_structured_and_interrupts_before_next_action(
    tmp_path, monkeypatch, claim_loss_scenario
):
    scenario = claim_loss_scenario
    store, thread_id = _store_and_thread(tmp_path, f"claim-loss-{scenario['reason']}")
    heartbeats = []
    renewal_attempted, allow_renewal_return = _configure_claim_loss(
        store.claims, monkeypatch, scenario, heartbeats
    )
    started_actions = []

    with pytest.raises(InterruptedError):
        with conversation_run_lane(_lane(store, thread_id)) as claim:
            assert renewal_attempted.wait(1), "应当执行一次注入的续租"
            allow_renewal_return.set()
            _wait_until(is_interrupted, timeout=1)
            _assert_claim_lost(claim, scenario["reason"], scenario["error_type"])
            _attempt_new_action(started_actions)

    assert not started_actions
    assert len(heartbeats) == 1
    assert store.claims.load(thread_id)["status"] == "cancelled"
    assert not is_interrupted(), "中断标志应在心跳内部作用域退出时清理"
