"""只暂停原构造返回至借用登记之间的调度点；真实 holder 和 JsonlMemory，不造 Agent/source。"""

import threading

from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.tests.test_coordinator_memory_resources import Domain, holder, home


def test_constructed_owner_cannot_be_evicted_before_original_borrow_is_registered(
    tmp_path, monkeypatch
):
    h = holder(tmp_path, capacity=1)
    a, b = home(tmp_path, "a"), home(tmp_path, "b")
    published, resume, old_inside, retire = [threading.Event() for _ in range(4)]
    built, old_values, errors = [], [], []
    original_construct = h._construct

    def factory_a():
        value = Domain(JsonlMemory(tmp_path / "a/memory.jsonl"))
        built.append(value)
        return value

    def paused_construct(key, entry, factory):
        original_construct(key, entry, factory)
        if threading.current_thread().name == "original-a":
            published.set()
            assert resume.wait(5)

    monkeypatch.setattr(h, "_construct", paused_construct)

    def original_borrow():
        try:
            with h.borrow_current_owner(a, factory_a) as resource:
                old_values.append(resource)
                resource.long_term._pending_access["retained-obligation"] = 1.0
                old_inside.set()
                assert retire.wait(5)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=original_borrow, name="original-a")
    thread.start()
    try:
        assert published.wait(5)
        # 正常应拒绝逐出正在领取的 A；当前前像会误判 borrowers=0 并删掉它。
        from agent_py_agent.agent.gateway_parts.coordinator_memory_resources import (
            CoordinatorMemoryResourcesUnavailable,
        )

        try:
            with h.borrow_current_owner(
                b, lambda: Domain(JsonlMemory(tmp_path / "b/memory.jsonl"))
            ):
                pass
        except CoordinatorMemoryResourcesUnavailable:
            resume.set()
            assert old_inside.wait(5)
            assert len(built) == 1
            return
        with h.borrow_current_owner(a, factory_a) as new_resource:
            resume.set()
            assert old_inside.wait(5)
            assert old_values[0] is new_resource, (
                "同 owner 同时进入两份 JsonlMemory/两把 operation 锁；旧实例 pending 已脱离 holder",
                len(built),
                list(h._entries),
                old_values[0].long_term._pending_access,
            )
    finally:
        resume.set()
        retire.set()
        thread.join(5)
        assert not thread.is_alive()
        assert not errors
