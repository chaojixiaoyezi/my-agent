# LLM: B5 创建归属与精确回收回归；假 PID 只进入替身，真实进程测试须持有自己的句柄和出生身份。
# 模块用途: 默认收集 B5/R2/R3 的创建身份、别名、共享缓存、僵尸组长后代清理和未确认诊断回归。
from __future__ import annotations

import os
import sys
import threading
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import conftest as support
import pytest

from agent_py_agent.agent.tooling.background_process_launch import (
    start_background_process as _EARLY_START,
)


# LLM: 仅隔离本文件主动注入的假清理失败；真实外层 teardown 在 monkeypatch 撤销后仍写正式诊断列表。
# 函数用途: 让诊断断言互不污染，不把合成失败混成真实遗留进程。
@pytest.fixture(autouse=True)
def b5_diagnostic_isolation(monkeypatch):
    monkeypatch.setattr(support, "_UNCONFIRMED_PROCESS_REAPS", [])

# LLM: 替换全部信号/受管停止出口；伪 PID 只作为数据，测试绝不调用实际进程终止。
# 函数用途: 提供精确回收的可观测替身，验证安全拒绝和调用顺序。
@pytest.fixture
def b5_reap_probe(monkeypatch):
    from agent_py_agent.agent.tooling import process_registry as module
    from agent_py_agent.agent.tooling import process_session_cleanup as cleanup

    calls = []
    pid = 987654321
    entry = {"session_id": "bg-test", "pid": pid, "pid_birth_token": "birth-test",
             "store_root": "", "popen": None, "scope_level": "function"}
    monkeypatch.setattr(module, "_IS_WINDOWS", False)
    monkeypatch.setattr(module.os, "getpgid", lambda target: target)
    monkeypatch.setattr(module.os, "getpgrp", lambda: 777)
    monkeypatch.setattr(module, "_same_process", lambda *_: True)
    monkeypatch.setattr(module, "_process_instance_terminated", lambda *_: False)
    # LLM: 信号出口完全替身；返回与真实树清理同类型的回执，不用 None 掩盖确认语义。
    # 函数用途: 记录目标与出生身份并模拟已确认清理。
    def terminate(*args, **kwargs):
        calls.append((args, kwargs))
        return module.ProcessTerminationReceipt("test_tree", True, 0, 1)
    monkeypatch.setattr(module, "terminate_process_tree", terminate)
    monkeypatch.setattr(cleanup, "stop_process_session", lambda *args, **kwargs: calls.append("managed"))
    return SimpleNamespace(module=module, entry=entry, calls=calls)


# LLM: 只用合成记录接管，merge 保持真实；probe 的 PID=1 永远不得进入登记回调。
# 函数用途: 验证真正接管会通知，而普通出生观察不会通知，回调快照不污染缓存。
def test_b5_spawn_hook_is_creation_only_and_copies_snapshot(monkeypatch, tmp_path):
    from agent_py_agent.agent.tooling import process_registry as module
    from agent_py_agent.agent.tooling.process_session_records import LEGACY_PROCESS_SESSION_SCHEMA

    observed = []
    proc = module.BackgroundProcess("bg-test", "test", 987654321, 1.0,
                                    access_scope=module.ProcessAccessScope("test-owner", "test-thread"),
                                    pid_birth_token="birth-test",
                                    persisted_snapshot={"schema": LEGACY_PROCESS_SESSION_SCHEMA})
    registry = module.ProcessRegistry()
    monkeypatch.setattr(registry, "_visible_record_locked", lambda *_: proc)
    monkeypatch.setattr(registry, "_prune_finished_locked", lambda *_: None)
    monkeypatch.setattr(module, "_read_host_birth_token", lambda *_: "observed")
    monkeypatch.setattr(module, "_native_posix_probe_enabled", lambda: False)
    module.set_test_process_spawn_hook(observed.append)
    try:
        module.capture_process_birth_token(1)
        assert not observed
        registry.attach(proc.to_record(), SimpleNamespace(pid=proc.pid), tmp_path)
        assert len(observed) == 1
        assert observed[0] is not proc
        observed[0].persisted_snapshot["changed"] = True
        assert "changed" not in proc.persisted_snapshot
    finally:
        module.set_test_process_spawn_hook(None)


# LLM: 危险 PID、未知出生和 pytest 原组都必须在任何停止入口之前拒绝。
# 函数用途: 守住 PID=1、自身、出生不符和同组的零信号边界。
@pytest.mark.parametrize("unsafe", ["zero", "one", "self", "same_group", "no_birth", "changed_birth"])
def test_b5_reaper_rejects_unowned_targets(b5_reap_probe, monkeypatch, unsafe):
    probe = b5_reap_probe
    if unsafe in {"zero", "one", "self"}:
        probe.entry["pid"] = {"zero": 0, "one": 1, "self": os.getpid()}[unsafe]
    if unsafe == "same_group":
        monkeypatch.setattr(probe.module.os, "getpgid", lambda _: 777)
    if unsafe == "no_birth":
        probe.entry["pid_birth_token"] = ""
    if unsafe == "changed_birth":
        monkeypatch.setattr(probe.module, "_same_process", lambda *_: False)
    probe.module.reap_test_registered_process(probe.entry)
    assert probe.calls == []


# LLM: 模拟出口只记录调用；必须携带创建时出生身份，不能仅凭 PID 终止。
# 函数用途: 验证存活登记项确实触发精确树回收。
def test_b5_reaper_terminates_registered_live_tree(b5_reap_probe):
    probe = b5_reap_probe
    probe.module.reap_test_registered_process(probe.entry)
    assert probe.calls == [((probe.entry["pid"], None), {"expected_birth_token": "birth-test"})]


# LLM: 只登记合成快照，不执行 OS 操作；重复的启动/attach 通知只能保留一份句柄与归属。
# 函数用途: 验证账本独立保存创建身份，包含测试 nodeid 和函数作用域。
def test_b5_ledger_deduplicates_without_losing_ownership():
    from agent_py_agent.agent.tooling.process_registry import BackgroundProcess

    state = SimpleNamespace(entries={}, lock=threading.Lock())
    proc = BackgroundProcess("bg-test", "test", 987654321, 1.0,
                             pid_birth_token="birth-test", store_root="/test/store")
    support._record_test_process_spawn(state, "module.py::test_example", proc)
    support._record_test_process_spawn(state, "module.py::test_example", proc)
    assert len(state.entries) == 1
    entry = state.entries[("bg-test", proc.pid)]
    assert entry["test_node_id"] == "module.py::test_example"
    assert entry["pid_birth_token"] == "birth-test" and entry["store_root"] == "/test/store"
    assert entry["scope_level"] == "function"
    proc.pid_birth_token = "changed"
    assert entry["pid_birth_token"] == "birth-test"


# LLM: 所有伪进程只经替身启动；外部别名与模块入口都必须透明返回原对象，清理不发信号。
# 函数用途: 验证不经 attach 的直接启动及其提前导入别名也会进账。
def test_b5_capture_wraps_direct_launch_and_imported_alias(monkeypatch):
    from types import ModuleType

    from agent_py_agent.agent.tooling import background_process_launch as launch
    from agent_py_agent.agent.tooling import process_registry as module
    from agent_py_agent.agent.tooling.process_session_records import LEGACY_PROCESS_SESSION_SCHEMA

    proc = module.BackgroundProcess("bg-test", "test", 987654321, 1.0,
                                    pid_birth_token="birth-test",
                                    persisted_snapshot={"schema": LEGACY_PROCESS_SESSION_SCHEMA})
    hosted = SimpleNamespace(record=proc.to_record(), store_root=Path("/test/store"), process=None)
    # LLM: 返回合成启动结果，绝不调用 OS 或创建真实进程。
    # 函数用途: 为直接启动和导入别名提供同一可识别结果。
    def original(*args, **kwargs):
        return hosted
    alias = ModuleType("agent_py_agent.tests._b5_alias_probe")
    alias.start = original
    monkeypatch.setitem(sys.modules, alias.__name__, alias)
    monkeypatch.setattr(launch, "start_background_process", original)
    state = SimpleNamespace(entries={}, lock=threading.Lock(), patch=pytest.MonkeyPatch())
    try:
        support._install_test_process_capture(state, "module.py::test_example")
        assert launch.start_background_process(object()) is hosted
        assert alias.start(object()) is hosted
        assert len(state.entries) == 1
    finally:
        module.set_test_process_spawn_hook(None)
        state.patch.undo()


# LLM: 用替身使一条清理报错，第二条仍必须执行，原断言异常由最外层 hook 保留。
# 函数用途: 验证清理隔离、先撤销捕获和最终清空缓存。
def test_b5_cleanup_continues_after_one_reap_error(monkeypatch):
    from agent_py_agent.agent.tooling import process_registry as module

    calls = []
    # LLM: 只模拟回收失败，不触发真实停止或系统信号。
    # 函数用途: 注入第一条 OSError，记录第二条仍被处理。
    def reap(entry):
        assert module._TEST_PROCESS_SPAWN_HOOK is None
        calls.append(entry["pid"])
        if entry["pid"] == 2:
            raise OSError("gone")
    entries = {(str(pid), pid): {"session_id": str(pid), "pid": pid, "scope_level": "function"} for pid in (2, 3)}
    state = SimpleNamespace(entries=entries, patch=pytest.MonkeyPatch())
    item = SimpleNamespace(_test_process_ownership=state, nodeid="module.py::test_example")
    monkeypatch.setattr(module, "reap_test_registered_process", reap)
    monkeypatch.setattr(module.process_registry, "forget", lambda session_id, **_: calls.append(("forget", session_id)), raising=False)
    support._cleanup_test_process_entries(item)
    assert calls == [2, 3, ("forget", "2"), ("forget", "3")]


# LLM: 下一个函数重新接管同一个共享进程不能降级寿命或覆盖最初作用域。
# 函数用途: 防止 module/session 进程在下一条测试 attach 后被过早回收。
def test_b5_reattach_preserves_shared_process_scope(monkeypatch):
    from agent_py_agent.agent.tooling.process_registry import BackgroundProcess

    key = ("bg-shared", 987654321)
    shared = {key: {"pid_birth_token": "birth-test", "popen": None,
                    "scope_level": "module", "scope_owner": "module.py", "store_root": "/test/store"}}
    monkeypatch.setattr(support, "_SHARED_TEST_PROCESS_ENTRIES", shared)
    state = SimpleNamespace(entries={}, lock=threading.Lock())
    proc = BackgroundProcess(*key[:1], "test", key[1], 1.0, pid_birth_token="birth-test", store_root="/test/store")
    support._record_test_process_spawn(state, "module.py::next_test", proc)
    assert state.entries[key]["scope_level"] == "module"


# LLM: 参数化高作用域 fixture 由实际实例寿命裁决，不仅看下条测试 nodeid 是否相同。
# 函数用途: 覆盖 class/package 共享，以及 module/session 参数实例的到期回收。
@pytest.mark.parametrize("scope,expired,expected", [
    ("class", False, True), ("package", False, True), ("module", True, False), ("session", True, False),
])
def test_b5_scope_follows_fixture_instance(scope, expired, expected):
    entry = {"scope_level": scope, "scope_owner": "module.py", "scope_lifetime": {"expired": expired}}
    assert support._test_process_scope_still_live(entry, SimpleNamespace(nodeid="module.py::test_next")) is expected


# LLM: 模拟晚导入模块保留旧包装器；它的启动不能再写上一条测试账本。
# 函数用途: 验证撤销后遗留别名会绑定当前测试，而不是永久捕获旧 state。
def test_b5_old_launch_alias_binds_current_test(monkeypatch):
    from agent_py_agent.agent.tooling import background_process_launch as launch
    from agent_py_agent.agent.tooling import process_registry as module
    from agent_py_agent.agent.tooling.process_session_records import LEGACY_PROCESS_SESSION_SCHEMA

    proc = module.BackgroundProcess("bg-test", "test", 987654321, 1.0, pid_birth_token="birth-test",
                                    persisted_snapshot={"schema": LEGACY_PROCESS_SESSION_SCHEMA})
    hosted = SimpleNamespace(record=proc.to_record(), store_root=Path("/test/store"), process=None)
    monkeypatch.setattr(launch, "start_background_process", lambda *args, **kwargs: hosted)
    first = SimpleNamespace(entries={}, lock=threading.Lock(), patch=pytest.MonkeyPatch())
    second = SimpleNamespace(entries={}, lock=threading.Lock(), patch=pytest.MonkeyPatch())
    try:
        support._install_test_process_capture(first, "module.py::test_first")
        old_alias = launch.start_background_process
        first.patch.undo()
        support._install_test_process_capture(second, "module.py::test_second")
        assert old_alias(object()) is hosted
        assert not first.entries
        assert second.entries[(proc.session_id, proc.pid)]["test_node_id"] == "module.py::test_second"
    finally:
        module.set_test_process_spawn_hook(None)
        second.patch.undo()
        first.patch.undo()


# LLM: 高 scope MonkeyPatch 是后写入者，函数级观察器撤销不能覆盖其仍有效的替身。
# 函数用途: 验证外层清理保留模块/会话 fixture 后来安装的启动替身。
def test_b5_cleanup_preserves_later_scoped_patch(monkeypatch):
    from agent_py_agent.agent.tooling import background_process_launch as launch
    from agent_py_agent.agent.tooling import process_registry as module

    state = SimpleNamespace(entries={}, lock=threading.Lock(), patch=pytest.MonkeyPatch())
    item = SimpleNamespace(_test_process_ownership=state, nodeid="module.py::test_example")
    monkeypatch.setattr(module.process_registry, "clear", lambda: None)
    original = launch.start_background_process
    support._install_test_process_capture(state, item.nodeid)
    later = object()
    try:
        launch.start_background_process = later
        support._cleanup_test_process_entries(item)
        assert launch.start_background_process is later
    finally:
        launch.start_background_process = original
        state.patch.undo()


# LLM: 最后一条测试关闭后返回的启动仍有原创建归属，不能只入一个永无消费者的共享表。
# 函数用途: 验证迟到的函数级派生进程立即走精确回收。
def test_b5_late_spawn_after_cleanup_is_reaped(monkeypatch):
    from agent_py_agent.agent.tooling import process_registry as module

    reaped = []
    monkeypatch.setattr(module, "reap_test_registered_process", reaped.append)
    monkeypatch.setattr(support, "_SHARED_TEST_PROCESS_ENTRIES", {})
    state = SimpleNamespace(entries={}, lock=threading.Lock(), closed=True)
    proc = module.BackgroundProcess("bg-late", "test", 987654321, 1.0, pid_birth_token="birth-test")
    support._record_test_process_spawn(state, "module.py::test_finished", proc)
    assert len(reaped) == 1
    assert reaped[0]["test_node_id"] == "module.py::test_finished"
    assert not support._SHARED_TEST_PROCESS_ENTRIES


# LLM: 高作用域 fixture 等待工作线程完成启动时，其真实实例寿命仍须贯穿该次登记。
# 函数用途: 验证线程池式创建不会被错误降级成 function scope。
def test_b5_fixture_scope_reaches_spawn_worker():
    from agent_py_agent.agent.tooling.process_registry import BackgroundProcess

    finalizers = []
    request = SimpleNamespace(node=SimpleNamespace(nodeid="module.py"), addfinalizer=finalizers.append)
    hook = support.pytest_fixture_setup(SimpleNamespace(scope="module"), request)
    state = SimpleNamespace(entries={}, lock=threading.Lock())
    proc = BackgroundProcess("bg-thread", "test", 987654321, 1.0, pid_birth_token="birth-test")
    next(hook)
    try:
        worker = threading.Thread(target=support._record_test_process_spawn, args=(state, "module.py::test_worker", proc))
        worker.start()
        worker.join(timeout=3)
        assert not worker.is_alive()
        assert state.entries[(proc.session_id, proc.pid)]["scope_level"] == "module"
    finally:
        with suppress(StopIteration):
            next(hook)
        for finalize in reversed(finalizers):
            finalize()


# LLM: 此模块按默认 pytest 作为顶层 test_* 收集，别名在任何函数 fixture 之前已导入。
# 函数用途: 钉住真实顶层测试模块的提前导入别名，不用包内伪模块替代此入口。
def test_b5_top_level_early_alias_is_wrapped():
    from agent_py_agent.agent.tooling import background_process_launch as launch

    assert _EARLY_START is launch.start_background_process
    assert callable(getattr(_EARLY_START, "__wrapped__", None))


# LLM: child 的出生绑定来自原创建快照，后续对象/字典变化不能重绑账本。
# 函数用途: 验证目录丢失前已冻结 host/child/pgid/owner/task 全部身份。
def test_b5_ledger_freezes_child_and_execution_identity(monkeypatch):
    from agent_py_agent.agent.tooling import process_registry as module

    monkeypatch.setattr(support.os, "getpgid", lambda _: 876543210)
    proc = module.BackgroundProcess("bg-child", "test", 987654321, 1.0,
        access_scope=module.ProcessAccessScope("owner-b5", "thread-b5"),
        child_pid=987654322, pid_birth_token="host-birth",
        persisted_snapshot={"child_pid_birth_token": "child-birth", "execution_scope": {"root_task_id": "task-b5"}})
    state = SimpleNamespace(entries={})
    support._record_test_process_spawn(state, "module.py::test_child", proc)
    proc.child_pid = 999
    proc.persisted_snapshot["child_pid_birth_token"] = "changed"
    proc.persisted_snapshot["execution_scope"]["root_task_id"] = "changed-task"
    support._record_test_process_spawn(state, "module.py::test_child", proc)
    entry = state.entries[(proc.session_id, proc.pid)]
    assert tuple(entry.get(key) for key in ("pid", "pid_birth_token", "child_pid", "child_pid_birth_token",
                                           "pgid", "owner_id", "task_id")) == (
        987654321, "host-birth", 987654322, "child-birth", 876543210, "owner-b5", "task-b5")


# LLM: 受管目录被删除且 host 已退出；系统信号出口全部替身，只允许原 child 出生实例进入终止。
# 函数用途: 验证 child 回退不依赖 Store 或仍存活的 host，也不盲发旧 PGID 信号。
def test_b5_deleted_store_and_exited_host_reaps_frozen_child(b5_reap_probe, monkeypatch, tmp_path):
    probe = b5_reap_probe
    root = tmp_path / "removed-authority"
    root.mkdir()
    root.rmdir()
    probe.entry.update(store_root=str(root), child_pid=987654322, child_pid_birth_token="child-birth", pgid=777)
    monkeypatch.setattr(probe.module, "_same_process", lambda pid, birth: pid == 987654322 and birth == "child-birth")
    monkeypatch.setattr(probe.module, "_process_instance_terminated", lambda pid, _: pid == probe.entry["pid"])
    probe.module.reap_test_registered_process(probe.entry)
    assert probe.calls == [((987654322, None), {"expected_birth_token": "child-birth"})]


# LLM: 未确认事实必须保留供会话诊断；不能用吞异常把清理伪装成成功。
# 函数用途: 验证普通未确认回执和回收异常都会留下结构化身份诊断。
@pytest.mark.parametrize("failure", ["unconfirmed", "exception"])
def test_b5_unconfirmed_reap_keeps_diagnostic(monkeypatch, failure):
    from agent_py_agent.agent.tooling import process_registry as module

    diagnostics = getattr(support, "_UNCONFIRMED_PROCESS_REAPS", [])
    before = len(diagnostics)
    # LLM: 不操作 OS，只模拟探针权限失败或返回真实形状的未确认回执。
    # 函数用途: 注入一条清理未确认，检查原测试仍能正常执行断言。
    def reap(entry):
        if failure == "exception":
            raise PermissionError("test probe denied")
        return {"confirmed": False, "reason": "identity_unavailable"}
    monkeypatch.setattr(module, "reap_test_registered_process", reap)
    entry = {"session_id": "bg-diagnostic", "pid": 987654321, "pid_birth_token": "host-birth",
             "child_pid": 987654322, "child_pid_birth_token": "child-birth", "scope_level": "function"}
    state = SimpleNamespace(entries={(entry["session_id"], entry["pid"]): entry}, patch=pytest.MonkeyPatch())
    support._cleanup_test_process_entries(SimpleNamespace(_test_process_ownership=state, nodeid="module.py::test_diag"))
    assert len(diagnostics) == before + 1
    assert diagnostics[-1]["session_id"] == entry["session_id"]
    assert diagnostics[-1]["child_pid_birth_token"] == "child-birth"


# LLM: 使用真实 Store 与 registry 缓存，假 Popen 仅提供 poll；从未实际派生或向假 PID 发信号。
# 函数用途: 构造跨两条测试有效的 module 共享资源，验证原无 root 查询入口。
@pytest.fixture(scope="module")
def b5_shared_cached_process(tmp_path_factory):
    from agent_py_agent.agent.tooling import process_registry as module
    from agent_py_agent.agent.tooling.process_session_records import LEGACY_PROCESS_SESSION_SCHEMA

    store = module.ProcessSessionStore(tmp_path_factory.mktemp("b5-shared-store"))
    proc = module.BackgroundProcess("bg-b5-shared-cache", "test", 987654323, 1.0,
        access_scope=module.ProcessAccessScope("owner-b5", "thread-b5"), pid_birth_token="shared-birth",
        persisted_snapshot={"schema": LEGACY_PROCESS_SESSION_SCHEMA})
    payload = store.write(proc.to_record())
    exit_code = {"value": None}
    handle = SimpleNamespace(pid=proc.pid, poll=lambda: exit_code["value"])
    record = module.process_registry.attach(payload, handle, store.root)
    try:
        yield record.session_id
    finally:
        exit_code["value"] = 0


# LLM: 首次测试结束只结束 function scope，不能清掉共享 fixture 的无 root 索引。
# 函数用途: 首次按原 session_id 查询共享进程。
def test_b5_module_shared_cache_first(b5_shared_cached_process):
    from agent_py_agent.agent.tooling.process_registry import process_registry

    assert process_registry.get(b5_shared_cached_process) is not None


# LLM: 紧接上一条测试；复用真实 module fixture，不重新 attach 或额外传 store_root 掩盖缓存丢失。
# 函数用途: 验证第二条测试仍能沿原无 root 查询找到共享进程。
def test_b5_module_shared_cache_second(b5_shared_cached_process):
    from agent_py_agent.agent.tooling.process_registry import process_registry

    assert process_registry.get(b5_shared_cached_process) is not None


# LLM: 模块选择只扩展测试来源，仍按原函数对象身份替换，并验证恢复；无关库同名函数不能被改。
# 函数用途: 覆盖 test_*、*_test、测试目录来源、包前缀与无关模块边界。
@pytest.mark.parametrize("name,in_tests,wrapped", [
    ("test_b5_alias_probe", False, True), ("b5_alias_test", False, True),
    ("b5_fixture_source", True, True), ("agent_py_agent.tests.b5_probe", False, True),
    ("agent.b5_probe", False, True), ("unrelated_b5_library", False, False),
])
def test_b5_alias_module_boundaries(monkeypatch, name, in_tests, wrapped):
    from types import ModuleType

    alias = ModuleType(name)
    if in_tests:
        alias.__file__ = str(Path(__file__).with_name("_b5_source_helper.py"))
    original, replacement = object(), object()
    alias.start, alias.other = original, object()
    unrelated = alias.other
    monkeypatch.setitem(sys.modules, name, alias)
    state = SimpleNamespace(patch=pytest.MonkeyPatch(), installed={})
    try:
        support._patch_test_launch_aliases(state, original, replacement)
        assert alias.start is (replacement if wrapped else original)
        assert alias.other is unrelated
    finally:
        state.patch.undo()
    assert alias.start is original


# LLM: 本 fixture 在最初捕获扫描后才插入原函数别名，setup 完成后的增量扫描必须覆盖它。
# 函数用途: 提供收集/fixture 初始化期间晚到的顶层测试模块别名。
@pytest.fixture
def b5_setup_imported_alias(request):
    from types import ModuleType

    alias = ModuleType("test_b5_setup_alias_probe")
    alias.start = request.node._test_process_ownership.spawn_wrappers[0][0]
    sys.modules[alias.__name__] = alias
    try:
        yield alias
    finally:
        if sys.modules.get(alias.__name__) is alias:
            del sys.modules[alias.__name__]


# LLM: 通过真实 pytest setup 包装钩子而非手动 refresh 验证，不启动真实进程。
# 函数用途: 确认函数 fixture 创建的新模块级别名在测试体执行前已登记包装。
def test_b5_setup_rescans_late_alias(b5_setup_imported_alias):
    from agent_py_agent.agent.tooling import background_process_launch as launch

    assert b5_setup_imported_alias.start is launch.start_background_process


# LLM: host 已消失则其 birth 也不再匹配；child 独立守保护 PID/原组/出生未知与复用边界，不把 gone 假成僵尸。
# 函数用途: 保留 child 回退零信号断言，同时为原实例消失、复用及探针异常提供一致的观察替身。
@pytest.mark.parametrize("unsafe", ["one", "self", "same_group", "no_birth", "changed_birth", "reused", "probe_error"])
def test_b5_child_fallback_birth_and_group_guards(b5_reap_probe, monkeypatch, unsafe):
    probe = b5_reap_probe
    child = {"one": 1, "self": os.getpid()}.get(unsafe, 987654322)
    probe.entry.update(child_pid=child, child_pid_birth_token="" if unsafe == "no_birth" else "child-birth")
    # LLM: host 已消失；只为 probe_error 注入 child 查询故障，不碰 OS。
    # 函数用途: 明确区分同 PID 新实例、未知和观察异常。
    def terminated(pid, birth):
        if pid == child and unsafe == "probe_error":
            raise PermissionError("test probe denied")
        return pid == probe.entry["pid"] or unsafe == "reused"
    monkeypatch.setattr(probe.module, "_process_instance_terminated", terminated)
    monkeypatch.setattr(probe.module, "_same_process",
                        lambda pid, _: pid == child and unsafe not in {"changed_birth", "reused", "probe_error"})
    if unsafe == "same_group":
        monkeypatch.setattr(probe.module.os, "getpgid", lambda pid: 777 if pid == child else pid)
    result = probe.module.reap_test_registered_process(probe.entry)
    assert not probe.calls
    assert result["confirmed"] is (unsafe == "reused")


# LLM: 缓存删除不是停止授权；两个 owner 同名 session 只删除原 root，缺 root 且歧义时不猜。
# 函数用途: 验证 forget 的目录隔离和未指定目录时的安全拒绝，同时保留 Store 正文。
@pytest.mark.parametrize("explicit_root", [True, False])
def test_b5_forget_preserves_other_owner_mapping(tmp_path, explicit_root):
    from agent_py_agent.agent.tooling import process_registry as module
    from agent_py_agent.agent.tooling.process_session_records import LEGACY_PROCESS_SESSION_SCHEMA

    registry = module.ProcessRegistry()
    roots = [module.ProcessSessionStore(tmp_path / name) for name in ("owner-a", "owner-b")]
    for index, store in enumerate(roots):
        proc = module.BackgroundProcess("bg-same", "test", 987654330 + index, 1.0,
            access_scope=module.ProcessAccessScope(f"owner-{index}", "thread-b5"), pid_birth_token="birth-test",
            status="exited", exit_code=0, finished_at=2.0, persisted_snapshot={"schema": LEGACY_PROCESS_SESSION_SCHEMA})
        registry._cache_payload(store.write(proc.to_record()), store.root)
    registry.forget("bg-same", store_root=roots[0].root if explicit_root else None)
    assert len(registry._processes) == (1 if explicit_root else 2)
    assert registry.get("bg-same", store_root=roots[1].root) is not None
    assert roots[0].load("bg-same").record


# LLM: 空 child 是创建快照的一部分，不是允许后续重复通知授权另一进程的通配符。
# 函数用途: 阻止同一 host 的重复通知把首次未知 child 重绑成另一个真实出生实例。
def test_b5_empty_child_snapshot_cannot_be_rebound():
    from agent_py_agent.agent.tooling.process_registry import BackgroundProcess

    proc = BackgroundProcess("bg-empty-child", "test", 987654321, 1.0, pid_birth_token="host-birth")
    state = SimpleNamespace(entries={})
    support._record_test_process_spawn(state, "module.py::test_empty_child", proc)
    proc.child_pid = 987654322
    proc.persisted_snapshot["child_pid_birth_token"] = "later-child-birth"
    support._record_test_process_spawn(state, "module.py::test_empty_child", proc)
    entry = state.entries[(proc.session_id, proc.pid)]
    assert (entry["child_pid"], entry["child_pid_birth_token"]) == (0, "")


# LLM: 受管 Store 也不能借当前 child 把首次空绑定升级为停止授权；出口全替身，不触发真实信号。
# 函数用途: 验证空 child 快照与已绑定另一 child 的 Store 不匹配时拒绝受管停止。
def test_b5_empty_child_snapshot_rejects_current_store_child(b5_reap_probe, monkeypatch):
    from agent_py_agent.agent.tooling import process_session_cleanup as cleanup

    probe = b5_reap_probe
    probe.entry.update(store_root="/test/store", child_pid=0, child_pid_birth_token="")
    payload = {"schema": next(iter(probe.module.MANAGED_PROCESS_SESSION_SCHEMAS)),
               "pid": probe.entry["pid"], "pid_birth_token": probe.entry["pid_birth_token"],
               "child_pid": 987654322, "child_pid_birth_token": "later-child-birth"}
    monkeypatch.setattr(probe.module, "ProcessSessionStore",
                        lambda root: SimpleNamespace(load=lambda session: SimpleNamespace(record=payload, load_error=None)))
    # LLM: 模拟受管停止确认，用调用账证明身份门是否在信号入口前拒绝。
    # 函数用途: 不杀进程，只记录不应发生的受管停止调用。
    def stop(*args, **kwargs):
        probe.calls.append("managed")
        return SimpleNamespace(confirmed=True)
    monkeypatch.setattr(cleanup, "stop_process_session", stop)
    assert not probe.module._stop_test_registered_session(probe.entry, probe.entry["pid"], probe.entry["pid_birth_token"])
    assert not probe.calls


# LLM: 构造 Store 不存在、组长 birth 仍匹配但已僵尸、同组后代仍活的虚拟树；信号出口完全替身。
# 函数用途: 用事件顺序证明必须先拿原组长身份清树，不能先 poll 回收组长。
def _b5_zombie_tree_probe(monkeypatch, tmp_path, leader_kind, confirmed):
    from agent_py_agent.agent.tooling import process_registry as module

    host, child = 987654341, 987654342
    leader = host if leader_kind == "host" else child
    birth = "host-birth" if leader_kind == "host" else "child-birth"
    events, tree = [], SimpleNamespace(descendant_alive=True, pgid=leader)
    # LLM: 仅模拟 Popen.poll；在后代仍活时调用即明确失败，不回收任何真实进程。
    # 函数用途: 捕捉错误的提前回收组长顺序。
    def poll():
        assert not tree.descendant_alive, "leader polled before same-group descendant cleanup"
        events.append("poll")
        return 0
    handle = SimpleNamespace(pid=host, poll=poll)
    entry = {"session_id": "bg-r3-zombie", "pid": host, "pid_birth_token": "host-birth",
             "child_pid": child if leader_kind == "child" else 0, "child_pid_birth_token": "child-birth",
             "store_root": str(tmp_path / "missing-store"), "popen": handle if leader_kind == "host" else None}
    assert not Path(entry["store_root"]).exists()
    # LLM: 树快照和终止均为替身，回执确认值原样传播，不把未确认伪装成成功。
    # 函数用途: 模拟先处理原同组后代再允许回收组长的唯一终止出口。
    def terminate(pid, proc, *, expected_birth_token):
        assert (pid, expected_birth_token) == (leader, birth)
        assert tree.pgid == leader and tree.descendant_alive and "poll" not in events
        events.append("snapshot_and_terminate_tree")
        tree.descendant_alive = not confirmed
        if confirmed and proc is not None:
            proc.poll()
        return module.ProcessTerminationReceipt("test_tree", confirmed, 0, 2, () if confirmed else (987654343,))
    monkeypatch.setattr(module, "_IS_WINDOWS", False)
    monkeypatch.setattr(module.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(module.os, "getpgrp", lambda: 777)
    monkeypatch.setattr(module, "_same_process", lambda pid, token: (pid, token) == (leader, birth))
    monkeypatch.setattr(module, "_process_instance_terminated", lambda *_: True)
    monkeypatch.setattr(module, "terminate_process_tree", terminate)
    return SimpleNamespace(module=module, entry=entry, events=events, tree=tree)


# LLM: 僵尸组长与旧实例消失不同；保留 birth 的 host/child 都必须走树快照，真实回执不能被 already_gone 覆盖。
# 函数用途: 覆盖缺 Store 的两类僵尸组长，含同组后代已确认/未确认终止。
@pytest.mark.parametrize("leader_kind", ["host", "child"])
@pytest.mark.parametrize("confirmed", [True, False])
def test_b5_missing_store_zombie_leader_reaps_tree_first(monkeypatch, tmp_path, leader_kind, confirmed):
    probe = _b5_zombie_tree_probe(monkeypatch, tmp_path, leader_kind, confirmed)
    result = probe.module.reap_test_registered_process(probe.entry)
    assert probe.events == ["snapshot_and_terminate_tree"] + (["poll"] if confirmed and leader_kind == "host" else [])
    assert probe.tree.descendant_alive is not confirmed
    assert result["confirmed"] is confirmed
    assert result["instances"][-1]["method"] == "test_tree"


# LLM: tests.dynamic 无 __file__，保存真实启动入口的直接别名；只验证捕获和条件恢复，不启动 OS 进程。
# 函数用途: 钉住合成 tests 包模块不能被筛掉，恢复时必须返回原函数对象。
def test_b5_synthetic_tests_dynamic_alias_is_patched_and_restored(monkeypatch):
    from types import ModuleType

    from agent_py_agent.agent.tooling import background_process_launch as launch
    from agent_py_agent.agent.tooling import process_registry as module

    alias = ModuleType("tests.dynamic")
    original = launch.start_background_process
    alias.start_background_process = original
    assert getattr(alias, "__file__", None) is None
    monkeypatch.setitem(sys.modules, alias.__name__, alias)
    replacement = support._test_background_launch_wrapper(original, module)
    state = SimpleNamespace(patch=pytest.MonkeyPatch(), installed={})
    try:
        support._patch_test_launch_aliases(state, original, replacement)
        assert alias.start_background_process is replacement
        assert state.installed[(id(alias), "start_background_process")] is replacement
    finally:
        support._undo_test_process_capture(state)
    assert alias.start_background_process is original


# LLM: 无来源合成测试模块可入范围；有来源时不凭 tests/agent 包名改写外部第三方模块。
# 函数用途: 验证缺文件名称规则及已知外部来源的排除边界。
@pytest.mark.parametrize("name,filename,expected", [
    ("tests", None, True), ("tests.dynamic", "", True), ("agent_py_agent.tests", None, True),
    ("agent_py_agent.tests.dynamic", None, True), ("conftest", None, True), ("test_dynamic", None, True),
    ("vendor.test_dynamic", None, True), ("unrelated_dynamic", None, False),
    ("tests.dynamic", "external.py", False), ("vendor.test_dynamic", "external.py", False),
    ("agent_py_agent.tests.dynamic", "external.py", False), ("agent.dynamic", "external.py", False),
])
def test_b5_synthetic_alias_source_boundaries(tmp_path, name, filename, expected):
    from types import ModuleType

    alias = ModuleType(name)
    if filename is not None:
        alias.__file__ = str(tmp_path / filename) if filename else ""
    assert support._is_test_launch_alias_module(name, alias) is expected
