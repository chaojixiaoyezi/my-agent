"""Real C lease withdrawal synchronization, not native W authorization acceptance."""
import copy
import os
import threading
from pathlib import Path

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_coordinator as c


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    target = (tmp_path / "explicit-home").resolve()
    monkeypatch.setenv("MY_AGENT_HOME", str(target))
    assert target != (Path.home() / ".my-agent").resolve()
    yield
    assert not target.exists()


# LLM: 使用真实flock和原lease，不以任意布尔/JSON伪装C权限；仅测在途责任同步。
# 函数用途: 创建一个实际持锁的C并保证测试不遗留内核锁。
@pytest.fixture
def lease(tmp_path):
    with c.coordinator_guard(tmp_path / 'gateway', 2) as actual:
        yield actual


# LLM: pin必须是原lease里登记的同一对象，不接受复制、重复结清或伪造对象。
# 函数用途: 检查在途责任只能结清一次，不能误删别的责任。
def test_commit_pin_exact_identity_and_replay_rejected(lease):
    pin = c.pin_coordinator_commit(lease, ('owner-a', 'worker-a'))
    for wrong in (object(), copy.copy(pin)):
        with pytest.raises(c.CoordinatorUnavailable):
            c.retire_coordinator_commit(lease, wrong)
    assert lease._commit_pins == {pin}
    c.retire_coordinator_commit(lease, pin)
    with pytest.raises(c.CoordinatorUnavailable):
        c.retire_coordinator_commit(lease, pin)
    assert not lease._commit_pins


# LLM: 原FD/inode和PID即使相同，复制lease也不能取得登记对象权威。
# 函数用途: 拒绝复制的C租约建立在途责任。
def test_copied_lease_cannot_pin_or_retire(lease):
    copied = copy.copy(lease)
    with pytest.raises(c.CoordinatorUnavailable):
        c.pin_coordinator_commit(copied, ('owner-a', 'worker-a'))
    pin = c.pin_coordinator_commit(lease, ('owner-a', 'worker-a'))
    try:
        with pytest.raises(c.CoordinatorUnavailable):
            c.retire_coordinator_commit(copied, pin)
        assert lease._commit_pins == {pin}
    finally:
        c.retire_coordinator_commit(lease, pin)


# LLM: 等待W短提交期间不能持原lifecycle RLock，其他C短操作必须可进入。
# 函数用途: 验证登记pin后实际锁可被另一线程取得。
def test_pin_does_not_hold_lifecycle_gate(lease):
    pin = c.pin_coordinator_commit(lease, ('owner-a', 'worker-a'))
    acquired = []

    def probe():
        taken = lease._lifecycle_gate.acquire(blocking=False)
        acquired.append(taken)
        if taken:
            lease._lifecycle_gate.release()

    thread = threading.Thread(target=probe)
    try:
        thread.start()
        thread.join(2)
        assert not thread.is_alive() and acquired == [True]
    finally:
        c.retire_coordinator_commit(lease, pin)


# LLM: closing拒新登记但保持原flock至实际pin全结清；Condition等待释放原gate。
# 函数用途: 用真实第二次争锁证明在途写责任未结清时不能启动替代C。
def test_guard_withdraw_waits_for_all_pins_and_rejects_new_work(tmp_path):
    root = tmp_path / 'gateway'
    guard = c.coordinator_guard(root, 2)
    actual = guard.__enter__()
    pins = []
    ended, errors = threading.Event(), []

    def close():
        try:
            guard.__exit__(None, None, None)
        except BaseException as exc:
            errors.append(exc)
        finally:
            ended.set()

    thread = None
    try:
        pins.extend([c.pin_coordinator_commit(actual, ('owner-a', 'worker-a')),
                     c.pin_coordinator_commit(actual, ('owner-b', 'worker-b'))])
        thread = threading.Thread(target=close)
        thread.start()
        with actual._commit_changed:
            assert actual._commit_changed.wait_for(lambda: actual._closing, timeout=2)
        assert not ended.is_set()
        assert c.current_coordinator(root, 2) is None
        with pytest.raises(c.CoordinatorUnavailable):
            c.pin_coordinator_commit(actual, ('owner-c', 'worker-c'))
        with pytest.raises(c.CoordinatorUnavailable):
            with c.coordinator_guard(root, 2):
                pytest.fail('flock released with two pending commits')
        c.retire_coordinator_commit(actual, pins.pop())
        assert not ended.wait(.05)
        with pytest.raises(c.CoordinatorUnavailable):
            with c.coordinator_guard(root, 2):
                pytest.fail('flock released with one pending commit')
        c.retire_coordinator_commit(actual, pins.pop())
        assert ended.wait(2) and not errors
        thread.join(2)
        with c.coordinator_guard(root, 2) as replacement:
            assert replacement is not actual
            assert c.current_coordinator(root, 2) is replacement
            with pytest.raises(c.CoordinatorUnavailable):
                c.pin_coordinator_commit(actual, ('owner-a', 'worker-a'))
    finally:
        for pin in pins:
            c.retire_coordinator_commit(actual, pin)
        if thread is not None:
            thread.join(2)
        else:
            guard.__exit__(None, None, None)


# LLM: pid不符时不能操作继承的同步状态；不修改主机真实PID，不冒充完整fork验收。
# 函数用途: 注入PID不符，确认复制进程上下文不能增加或结清责任。
def test_pid_mismatch_cannot_pin_or_retire(lease, monkeypatch):
    pin = c.pin_coordinator_commit(lease, ('owner-a', 'worker-a'))
    real_pid = os.getpid()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(c.os, 'getpid', lambda: real_pid + 100000)
            with pytest.raises(c.CoordinatorUnavailable):
                c.pin_coordinator_commit(lease, ('owner-b', 'worker-b'))
            with pytest.raises(c.CoordinatorUnavailable):
                c.retire_coordinator_commit(lease, pin)
        assert lease._commit_pins == {pin}
    finally:
        c.retire_coordinator_commit(lease, pin)


# LLM: 责任标签只是非空不可变身份metadata，不授予owner业务权限。
# 函数用途: 错形标签不得产生可结清的责任。
@pytest.mark.parametrize('key', [None, '', (), ('',), ['owner-a']])
def test_invalid_scope_key_is_rejected(lease, key):
    with pytest.raises(c.CoordinatorUnavailable):
        c.pin_coordinator_commit(lease, key)
    assert not lease._commit_pins
