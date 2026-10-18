"""真实holder的借用预留覆盖发布、构造失败和关闭；不构造HTTP来源或授写权限。"""

import threading

import pytest

from agent_py_agent.agent.gateway_parts.coordinator_memory_resources import (
    CoordinatorMemoryResourcesUnavailable,
)
from agent_py_agent.tests.test_coordinator_memory_resources import domain, holder, home


# LLM: 只包原Condition并保同一RLock；测试调度点放在最外层原锁已释放之后，不能改变产品互斥或计数。
# 类用途: 精确暂停选中entry后的窗口，或观察真实等待者已经释放原锁。
class ObservedCondition:
    # LLM: 信号仅本次线程夹具持有，不保存产品factory/source或创建第二资源锁。
    # 函数用途: 保存原Condition和本测试调度事件。
    def __init__(self, original, *, selected=None, resume=None, waiting=None):
        self.original = original
        self.selected, self.resume, self.waiting = selected, resume, waiting
        self.local = threading.local()
        self.paused = False

    # LLM: 委托同一原可重入锁，depth只避免暂停require_open的内层退出。
    # 函数用途: 进入原Condition并记录当前测试线程嵌套层数。
    def __enter__(self):
        self.original.__enter__()
        self.local.depth = getattr(self.local, "depth", 0) + 1
        return self

    # LLM: 实际先释放原锁，再暂停选中调用，允许其它owner真实竞争原LRU。
    # 函数用途: 在确定的发布/选择间隙交出调度，不用sleep推测窗口。
    def __exit__(self, *args):
        self.local.depth -= 1
        result = self.original.__exit__(*args)
        if (
            not self.local.depth
            and self.selected is not None
            and threading.current_thread().name == "selected-owner"
            and not self.paused
        ):
            self.paused = True
            self.selected.set()
            assert self.resume.wait(5)
        return result

    # LLM: 仍由原Condition释放/重新取得原锁；信号只证明指定线程进入真实等待点。
    # 函数用途: 让测试知道Future或operation等待者已到达等待位置。
    def wait(self):
        if self.waiting is not None and threading.current_thread().name == "waiting-owner":
            self.waiting.set()
        return self.original.wait()

    # LLM: 唤醒仍完全委托原Condition，不改变产品关闭/归还顺序。
    # 函数用途: 转发原holder的等待者通知。
    def notify_all(self):
        self.original.notify_all()


@pytest.mark.parametrize("preexisting", [False, True])
def test_selection_reserves_first_and_reused_entry_before_unlock(tmp_path, preexisting):
    h, owner = holder(tmp_path, capacity=1), home(tmp_path, "a")
    value, calls = domain(), []

    def factory():
        calls.append(True)
        return value

    if preexisting:
        with h.borrow_current_owner(owner, factory):
            pass
    selected, resume, entered, finish = [threading.Event() for _ in range(4)]
    h._condition = ObservedCondition(h._condition, selected=selected, resume=resume)
    errors = []

    def run():
        try:
            with h.borrow_current_owner(owner, factory) as actual:
                assert actual is value
                actual.long_term._pending_access["original"] = 1.0
                entered.set()
                assert finish.wait(5)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run, name="selected-owner")
    thread.start()
    try:
        assert selected.wait(5)
        entry = h._entries[h._key(owner)]
        assert entry.borrowers == 1
        with pytest.raises(CoordinatorMemoryResourcesUnavailable):
            with h.borrow_current_owner(home(tmp_path, "b"), domain):
                pytest.fail("选中但未进入业务的原资源不得逐出")
        assert h._entries[h._key(owner)] is entry
        resume.set()
        assert entered.wait(5)
        assert len(calls) == 1
    finally:
        resume.set()
        finish.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert entry.borrowers == 0 and not entry.operation.locked()
    assert h._entries[h._key(owner)] is entry
    assert value.long_term._pending_access == {"original": 1.0}


@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt])
def test_constructor_failure_returns_exact_reserved_entry_once(tmp_path, error_type):
    h, owner, entries = holder(tmp_path), home(tmp_path, "a"), []
    error = error_type("synthetic original factory failure")

    def factory():
        entry = h._entries[h._key(owner)]
        entries.append(entry)
        assert entry.borrowers == 1
        raise error

    with pytest.raises(error_type) as caught:
        with h.borrow_current_owner(owner, factory):
            pytest.fail("构造失败不发布资源")
    assert caught.value is error
    assert entries[0].borrowers == 0 and not entries[0].operation.locked()
    assert not h._entries
    with h.borrow_current_owner(owner, domain):
        assert h._entries[h._key(owner)].borrowers == 1
    assert h._entries[h._key(owner)].borrowers == 0


def test_shared_failed_future_returns_each_original_reservation_once(tmp_path):
    h, owner = holder(tmp_path), home(tmp_path, "a")
    constructing, release, waiting = [threading.Event() for _ in range(3)]
    h._condition = ObservedCondition(h._condition, waiting=waiting)
    error, errors, entries, calls = OSError("synthetic shared factory failure"), [], [], []

    def factory():
        calls.append(True)
        entries.append(h._entries[h._key(owner)])
        constructing.set()
        assert release.wait(5)
        raise error

    def run():
        try:
            with h.borrow_current_owner(owner, factory):
                pytest.fail("失败Future不能产出资源")
        except BaseException as actual:
            errors.append(actual)

    first, second = threading.Thread(target=run), threading.Thread(target=run, name="waiting-owner")
    first.start()
    try:
        assert constructing.wait(5)
        second.start()
        assert waiting.wait(5)
        with h._condition:
            assert entries[0].borrowers == 2
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert len(errors) == 2 and all(actual is error for actual in errors)
    assert calls == [True] and not h._entries
    assert entries[0].borrowers == 0 and not entries[0].operation.locked()


@pytest.mark.parametrize("phase", ["constructor", "operation"])
def test_close_returns_waiter_and_original_reservations_once(tmp_path, phase):
    h, owner = holder(tmp_path), home(tmp_path, "a")
    entered, release, waiting = [threading.Event() for _ in range(3)]
    h._condition = ObservedCondition(h._condition, waiting=waiting)
    entries, results = [], []

    def factory():
        entries.append(h._entries[h._key(owner)])
        if phase == "constructor":
            entered.set()
            assert release.wait(5)
        return domain()

    def first_run():
        try:
            with h.borrow_current_owner(owner, factory):
                entered.set()
                assert release.wait(5)
            results.append("first-returned")
        except CoordinatorMemoryResourcesUnavailable:
            results.append("first-closed")

    def wait_run():
        try:
            with h.borrow_current_owner(owner, domain):
                pytest.fail("close后等待者不能进入原业务")
        except CoordinatorMemoryResourcesUnavailable:
            results.append("waiter-closed")

    first = threading.Thread(target=first_run)
    second = threading.Thread(target=wait_run, name="waiting-owner")
    first.start()
    try:
        assert entered.wait(5)
        second.start()
        assert waiting.wait(5)
        with h._condition:
            assert entries[0].borrowers == 2
        h.close_admission()
        second.join(1)
        assert not second.is_alive() and results == ["waiter-closed"]
        assert entries[0].borrowers == 1
        assert h._entries[h._key(owner)] is entries[0]
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert entries[0].borrowers == 0 and not entries[0].operation.locked()
    assert not h._entries
    assert results[-1] == ("first-closed" if phase == "constructor" else "first-returned")


def test_body_failure_returns_reservation_and_preserves_same_resource(tmp_path):
    h, owner, value = holder(tmp_path), home(tmp_path, "a"), domain()
    error = ValueError("synthetic original operation failure")
    with pytest.raises(ValueError) as caught:
        with h.borrow_current_owner(owner, lambda: value) as actual:
            entry = h._entries[h._key(owner)]
            assert entry.borrowers == 1
            assert actual is value and entry.operation.locked()
            actual.long_term._pending_access["original"] = 1.0
            raise error
    assert caught.value is error
    assert entry.borrowers == 0 and not entry.operation.locked()
    with h.borrow_current_owner(owner, lambda: pytest.fail("不能重建原资源")) as actual:
        assert actual is value and entry.borrowers == 1
    assert entry.borrowers == 0
    assert value.long_term._pending_access == {"original": 1.0}
