"""holder只是实例责任，不是来源或权限；所有构造使用临时owner。"""

import threading
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)


@dataclass
class Domain:
    long_term: object


def holder(tmp_path, capacity=2):
    from agent_py_agent.agent.gateway_parts.coordinator_memory_resources import (
        CoordinatorMemoryResources,
    )

    config = AgentConfig(my_agent_home=str(tmp_path / "home"), owner_agent_pool_max_agents=capacity)
    control = SimpleNamespace(config=config)
    return CoordinatorMemoryResources(control, object(), object())


def home(tmp_path, name):
    root = tmp_path / "home"
    return home_paths_with_owner(
        home_paths(root), resolve_owner_home(root, OwnerIdentity.provider_user("feishu", name))
    )


def domain():
    return Domain(SimpleNamespace(_pending_access={}))


def test_holder_constructor_zero_io_and_original_config(tmp_path):
    h = holder(tmp_path)
    assert not list(tmp_path.iterdir())
    assert h._entries == {} and h._references[-1] is h.control.config
    assert not any("source" in name or "actor" in name for name in vars(h))


def test_same_owner_reuses_actual_reference_and_only_once_constructor(tmp_path):
    h, calls = holder(tmp_path), []
    original = domain()
    for _ in range(2):
        with h.borrow_current_owner(
            home(tmp_path, "a"), lambda: calls.append(True) or original
        ) as value:
            assert value is original
    assert calls == [True]


def test_clean_n_plus_one_evicts_idle_reference(tmp_path):
    h = holder(tmp_path)
    for name in ["a", "b", "c"]:
        with h.borrow_current_owner(home(tmp_path, name), domain) as value:
            value.long_term._pending_access["record"] = 1.0
            # 本测试只验证原pending义务为空后的逐出；真实flush另测，不能把clear当落盘。
            value.long_term._pending_access.clear()
    assert len(h._entries) == 2
    assert all(key[4] != home(tmp_path, "a").owner_id for key in h._entries)


def test_dirty_full_only_same_owner_can_reenter_and_flush_own_pending(tmp_path):
    from agent_py_agent.agent.gateway_parts.coordinator_memory_resources import (
        CoordinatorMemoryResourcesUnavailable,
    )

    h = holder(tmp_path, 1)
    original = domain()
    with h.borrow_current_owner(home(tmp_path, "a"), lambda: original):
        original.long_term._pending_access["a"] = 1.0
    with pytest.raises(CoordinatorMemoryResourcesUnavailable):
        with h.borrow_current_owner(home(tmp_path, "b"), domain):
            pytest.fail("不得逐出未落盘义务")
    with h.borrow_current_owner(home(tmp_path, "a"), domain) as value:
        assert value is original and value.long_term._pending_access == {"a": 1.0}
        value.long_term._pending_access.clear()
    with h.borrow_current_owner(home(tmp_path, "b"), domain):
        pass


def test_concurrent_first_constructor_shared_and_failed_original_object(tmp_path):
    h, entered, release, outcomes = holder(tmp_path), threading.Event(), threading.Event(), []
    error = OSError("synthetic constructor")

    def factory():
        entered.set()
        assert release.wait(3)
        raise error

    def run():
        try:
            with h.borrow_current_owner(home(tmp_path, "a"), factory):
                pytest.fail("不能发布半构造资源")
        except BaseException as exc:
            outcomes.append(exc)

    first, second = threading.Thread(target=run), threading.Thread(target=run)
    first.start()
    assert entered.wait(3)
    second.start()
    # condition等待者已进入后释放，不靠长sleep完成主链。
    threading.Event().wait(0.02)
    release.set()
    first.join(3)
    second.join(3)
    assert not first.is_alive() and not second.is_alive()
    assert outcomes == [error, error] and not h._entries
    with h.borrow_current_owner(home(tmp_path, "a"), domain):
        pass


@pytest.mark.parametrize("waiting", ["constructor", "operation"])
def test_close_wakes_unacquired_waiter_and_late_constructor_not_published(tmp_path, waiting):
    from agent_py_agent.agent.gateway_parts.coordinator_memory_resources import (
        CoordinatorMemoryResourcesUnavailable,
    )

    h, entered, release, outcomes = holder(tmp_path), threading.Event(), threading.Event(), []
    original = domain()

    def factory():
        if waiting == "constructor":
            entered.set()
            assert release.wait(3)
        return original

    def first():
        try:
            with h.borrow_current_owner(home(tmp_path, "a"), factory):
                entered.set()
                assert release.wait(3)
        except CoordinatorMemoryResourcesUnavailable:
            outcomes.append("first-closed")

    def second():
        try:
            with h.borrow_current_owner(home(tmp_path, "a"), domain):
                pytest.fail("closed waiter must not borrow")
        except CoordinatorMemoryResourcesUnavailable:
            outcomes.append("second-closed")

    a, b = threading.Thread(target=first), threading.Thread(target=second)
    a.start()
    assert entered.wait(3)
    b.start()
    threading.Event().wait(0.02)
    h.close_admission()
    b.join(1)
    assert not b.is_alive() and "second-closed" in outcomes
    assert original.long_term._pending_access == {}
    release.set()
    a.join(3)
    assert not a.is_alive() and not h._entries


def test_same_owner_operations_serialized_and_different_owner_unblocked(tmp_path):
    h, held, release, second_entered = (
        holder(tmp_path),
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )

    def first():
        with h.borrow_current_owner(home(tmp_path, "a"), domain):
            held.set()
            assert release.wait(3)

    def second():
        with h.borrow_current_owner(home(tmp_path, "a"), domain):
            second_entered.set()

    a, b = threading.Thread(target=first), threading.Thread(target=second)
    a.start()
    assert held.wait(3)
    b.start()
    with h.borrow_current_owner(home(tmp_path, "b"), domain):
        assert not second_entered.is_set()
    release.set()
    a.join(3)
    b.join(3)
    assert second_entered.is_set()


def test_holder_retains_actual_c1_memory_and_local_store_instances(tmp_path):
    from agent_py_agent.agent.gateway_parts.owner_resources import (
        resolve_owner_resource_configuration,
    )
    from agent_py_agent.agent.memory_store.owner_resources import build_owner_memory_authorities

    h = holder(tmp_path)
    h.control.home_root = tmp_path / "home"
    owner = OwnerIdentity.provider_user("feishu", "real-resources")
    scoped, config, paths = resolve_owner_resource_configuration(h.control, owner)
    resources = build_owner_memory_authorities(
        config,
        scoped,
        paths,
        None,
        embedder=None,
        semantic_status={"state": "disabled"},
        vector_identity=None,
    )
    for _ in range(2):
        with h.borrow_current_owner(scoped, lambda: resources) as actual:
            assert actual is resources
            assert actual.local_store is resources.local_store
            assert actual.candidates is resources.candidates
            assert actual.long_term is resources.long_term
    # 这里只在无C的夹具构造真实资源；不把默认构造当作C3初始化门已闭合。
    assert not any("source" in key or "actor" in key for key in vars(h))
