# LLM: 共享 pytest 防线；创建期绑定进程身份，最外层 teardown 精确回收，不扫描全机 PID 或吞掉原测试失败。
# 模块用途: 提供隔离 fixtures、桌面防线和测试派生进程的归属、共享寿命与回收诊断。
"""共享测试 fixtures - 为测试提供通用对象和 mock。"""
from __future__ import annotations

import os
import sys
import threading
import webbrowser
from contextlib import contextmanager, suppress
from dataclasses import replace
from datetime import datetime, timezone
from functools import partial, wraps
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.capability import CapabilityRouter, SkillsService
from agent_py_agent.agent.subagents.models import SubAgentTask
from agent_py_agent.agent.tooling import computer_use_macos, computer_use_x11
from agent_py_agent.tests._desktop_open_guard import (
    REAL_DESKTOP_MARKER,
    DesktopOpenGuard,
    failure_message,
)
from agent_py_agent.tests._repo_tree_guard import (
    REPO_ROOT,
    magicmock_failure_message,
    magicmock_fingerprint,
    magicmock_violation,
    remove_magicmock_dir,
    tracked_report_failure_message,
    tracked_report_fingerprint,
)
from agent_py_agent.tests._sandbox_capabilities import (
    capabilities_required,
    missing_capabilities,
    missing_capability_reason,
)
from agent_py_agent.tests._screen_capture_guard import (
    LANE_SKIP_MARKER,
    forbidden_real_macos_frameworks,
    forbidden_real_x11_libraries,
)

_DESKTOP_GUARD: DesktopOpenGuard | None = None
# 会话级 fixture 在最后一条测试收尾时就结束了，会话结束检查要用这份不清空的引用。
_FINISHED_DESKTOP_GUARD: DesktopOpenGuard | None = None
_TEST_PROCESS_FIXTURE_SCOPE = threading.local()
_SHARED_TEST_PROCESS_ENTRIES: dict[tuple[str, int], dict[str, Any]] = {}
_ACTIVE_TEST_PROCESS_OWNER: tuple[Any, str] | None = None
_ACTIVE_TEST_FIXTURE_SCOPE: tuple[Any, ...] | None = None
_TEST_PROCESS_ENTRIES_LOCK = threading.RLock()
_TEST_PROCESS_SUITE_CLOSING = False
_TESTS_SOURCE_ROOT = Path(__file__).resolve().parent
_UNCONFIRMED_PROCESS_REAPS: list[dict[str, Any]] = []


# LLM: 只复制创建时 host/child 出生与执行身份；重复通知不重绑已知实例，读状态和出生探针不产生清理权限。
# 函数用途: 去重登记完整创建快照，让目录消失后仍能按原 child 身份回收。
def _record_test_process_spawn(state, nodeid: str, proc, scope_context=None) -> None:
    key = (proc.session_id, proc.pid)
    entry = _test_process_spawn_entry(nodeid, proc, scope_context)
    reap_late = False
    with _TEST_PROCESS_ENTRIES_LOCK:
        previous = state.entries.get(key) or _SHARED_TEST_PROCESS_ENTRIES.get(key)
        if (previous and previous["pid_birth_token"] == proc.pid_birth_token
                and previous["store_root"] == proc.store_root):
            entry["popen"] = proc.process or previous["popen"]
            entry["scope_level"], entry["scope_owner"] = previous["scope_level"], previous["scope_owner"]
            entry["scope_lifetime"] = previous.get("scope_lifetime")
            _retain_test_process_identity(entry, previous)
        closed = getattr(state, "closed", False)
        defer = not _TEST_PROCESS_SUITE_CLOSING and _test_process_scope_still_live(entry, SimpleNamespace(nodeid=nodeid))
        reap_late = closed and not defer
        if not reap_late:
            target = _SHARED_TEST_PROCESS_ENTRIES if closed else state.entries
            target[key] = entry
    if reap_late:
        from agent_py_agent.agent.tooling import process_registry as module

        _reap_test_process_entry(module, entry)


# LLM: 身份字段只从已交接快照复制成独立标量；pgid 仅诊断，不用于无出生核验的组信号。
# 函数用途: 保存 host/child、访问 owner 和真实执行 task 的原创建绑定。
def _test_process_spawn_entry(nodeid, proc, scope_context):
    scope, owner, *lifetime = scope_context or _current_test_process_scope(nodeid)
    pgid = 0
    if os.name != "nt" and proc.pid > 1:
        with suppress(OSError):
            pgid = os.getpgid(proc.pid)
    return {"test_node_id": nodeid, "session_id": proc.session_id, "pid": proc.pid,
            "pid_birth_token": proc.pid_birth_token, "child_pid": proc.child_pid,
            "child_pid_birth_token": str(proc.persisted_snapshot.get("child_pid_birth_token") or ""),
            "pgid": pgid, "owner_id": proc.access_scope.owner_id,
            "task_id": str((proc.persisted_snapshot.get("execution_scope") or {}).get("root_task_id") or ""),
            "store_root": proc.store_root, "popen": proc.process, "scope_level": scope, "scope_owner": owner,
            "scope_lifetime": lifetime[0] if lifetime else None}


# LLM: 同一 host 的完整 child 对（含空值）与执行绑定均冻结；重复通知不授予新 child 身份。
# 函数用途: 合并创建通知时保留最初身份，防止首次未知 child 被后续可变对象重绑。
def _retain_test_process_identity(entry, previous) -> None:
    for name in ("pgid", "owner_id", "task_id"):
        entry[name] = previous.get(name, entry[name])
    entry["child_pid"] = previous.get("child_pid") or 0
    entry["child_pid_birth_token"] = previous.get("child_pid_birth_token") or ""


# LLM: 启动线程优先使用显式冻结上下文；fixture 等待工作线程时，共享当前真实 setup 实例的寿命。
# 函数用途: 给主线程和 setup 工作线程返回同一份作用域，不从线程名猜归属。
def _current_test_process_scope(nodeid):
    return getattr(_TEST_PROCESS_FIXTURE_SCOPE, "current", None) or _ACTIVE_TEST_FIXTURE_SCOPE or ("function", nodeid)


# LLM: 回调不捕获旧测试 state；旧导入别名也经本次 ownership 绑定，不重入 registry。
# 函数用途: 将即时 attach 快照交给当前测试账本。
def _capture_active_test_process_spawn(proc) -> None:
    owner = _ACTIVE_TEST_PROCESS_OWNER
    if owner is not None:
        _record_test_process_spawn(owner[0], owner[1], proc)


# LLM: 包装器只保留原启动函数；调用时冻结当前测试归属，晚导入别名不会永久持有旧 state。
# 函数用途: 原样启动并返回受管进程，把成功交接的身份交给启动时的测试。
def _test_background_launch_wrapper(original, module):
    # LLM: 异常及失败清理由原入口负责，捕获器不重复启动，不消费原 Popen。
    # 函数用途: 为没有 attach 的直接启动补登记。
    @wraps(original)
    def start(*args, **kwargs):
        owner = _ACTIVE_TEST_PROCESS_OWNER
        scope = _current_test_process_scope(owner[1]) if owner is not None else None
        hosted = original(*args, **kwargs)
        if owner is not None:
            proc = module.BackgroundProcess.from_record(hosted.record, store_root=hosted.store_root)
            proc.process = hosted.process
            _record_test_process_spawn(owner[0], owner[1], proc, scope)
        return hosted
    return start


# LLM: 不强行创造未配置的 import root；两个 root 已独立加载时按真实模块对象分别安装。
# 函数用途: 枚举本测试解释器里实际可用的进程模块，避免只按函数同一性漏掉另一 root。
def _test_process_module_pairs():
    import importlib

    pairs = []
    for root in ("agent_py_agent.agent", "agent"):
        registry = sys.modules.get(f"{root}.tooling.process_registry")
        launch = sys.modules.get(f"{root}.tooling.background_process_launch")
        if root == "agent_py_agent.agent":
            registry = importlib.import_module(f"{root}.tooling.process_registry")
            launch = importlib.import_module(f"{root}.tooling.background_process_launch")
        if registry is not None and launch is not None and not any(registry is pair[0] for pair in pairs):
            pairs.append((registry, launch))
    return pairs


# LLM: 包装真实启动与包内/顶层测试别名；保留原函数对供 setup 增量扫描，返回值与异常不变。
# 函数用途: 在测试期间记录受管接管与直接启动，并覆盖默认 pytest 顶层导入。
def _install_test_process_capture(state, nodeid: str) -> None:
    global _ACTIVE_TEST_PROCESS_OWNER
    _ACTIVE_TEST_PROCESS_OWNER = (state, nodeid)
    state.installed = {}
    state.spawn_wrappers = []
    for module, launch in _test_process_module_pairs():
        original = launch.start_background_process
        start = _test_background_launch_wrapper(original, module)
        module.set_test_process_spawn_hook(_capture_active_test_process_spawn)
        state.spawn_wrappers.append((original, start))
        _patch_test_launch_aliases(state, original, start)


# LLM: 包名、test 模块名或测试目录仅限定观察范围；替换仍须当前属性与原函数对象精确相同。
# 函数用途: 覆盖提前导入的包内与顶层测试别名，记录条件撤销需要的精确后像。
def _patch_test_launch_aliases(state, original, start) -> None:
    for name, loaded in list(sys.modules.items()):
        if loaded is None or not _is_test_launch_alias_module(name, loaded):
            continue
        for attr, value in list(vars(loaded).items()):
            if value is original and getattr(loaded, attr, None) is original:
                state.patch.setattr(loaded, attr, start)
                state.installed[(id(loaded), attr)] = start


# LLM: 无文件时接受显式合成测试名；有文件的包别名必须来自本仓库，不能凭 tests/agent 前缀触及第三方。
# 函数用途: 覆盖 tests.dynamic 等合成模块，同时保留真实仓库入口、顶层测试与 conftest 别名捕获。
def _is_test_launch_alias_module(name, module) -> bool:
    filename = getattr(module, "__file__", None)
    if not filename:
        return (name in ("tests", "conftest") or name.startswith(("tests.", "agent_py_agent.", "agent.", "test_"))
                or name.endswith("_test") or ".test_" in name)
    if not isinstance(filename, str):
        return False
    if name == "conftest" or name.startswith("test_") or name.endswith("_test"):
        return True
    try:
        source = Path(filename).resolve()
        return source.is_relative_to(_TESTS_SOURCE_ROOT) or (
            name.startswith(("agent_py_agent.", "agent.")) and source.is_relative_to(_TESTS_SOURCE_ROOT.parent))
    except (OSError, RuntimeError, ValueError):
        return False


# LLM: 只复用本次安装时冻结的原函数身份，不包装 fixture 后写的 fake，不改变 owner 或恢复栈。
# 函数用途: setup 结束后增量捕获新导入模块留下的原启动别名。
def _refresh_test_launch_aliases(state) -> None:
    for original, start in state.spawn_wrappers:
        _patch_test_launch_aliases(state, original, start)


# LLM: 此独立 MonkeyPatch 只含启动函数 setattr；仅撤销后像仍相同的项，保留高 scope fixture 的后续写入。
# 函数用途: 条件恢复 pytest setattr 栈，避免无条件 undo 提前盖掉模块/会话替身。
def _undo_test_process_capture(state) -> None:
    installed = getattr(state, "installed", {})
    state.patch._setattr[:] = [
        (target, name, previous) for target, name, previous in state.patch._setattr
        if (id(target), name) not in installed or getattr(target, name, None) is installed[(id(target), name)]
    ]
    state.patch.undo()


# LLM: 最外层 teardown、其它 monkeypatch 撤销后调用；保留共享索引，只忘记到期项，未确认与异常留诊断不盖原失败。
# 函数用途: 撤销捕获器并逐项精确回收本测试到期资源，让共享 fixture 继续可查。
def _cleanup_test_process_entries(item, nextitem=None) -> None:
    global _ACTIVE_TEST_PROCESS_OWNER, _TEST_PROCESS_SUITE_CLOSING
    state = getattr(item, "_test_process_ownership", None)
    if state is None:
        return
    _ACTIVE_TEST_PROCESS_OWNER = None
    pairs = _test_process_module_pairs()
    for module, _launch in pairs:
        module.set_test_process_spawn_hook(None)
    with suppress(Exception):
        _undo_test_process_capture(state)
    with _TEST_PROCESS_ENTRIES_LOCK:
        _TEST_PROCESS_SUITE_CLOSING = nextitem is None
        state.closed = True
        entries = {**_SHARED_TEST_PROCESS_ENTRIES, **state.entries}
        _SHARED_TEST_PROCESS_ENTRIES.clear()
        state.entries.clear()
    expired = []
    try:
        for key, entry in entries.items():
            if _test_process_scope_still_live(entry, nextitem):
                _keep_shared_test_process_entry(key, entry)
                continue
            expired.append(entry)
            _reap_test_process_entry(pairs[0][0], entry)
    finally:
        _forget_expired_test_process_entries(pairs, expired)


# LLM: 共享表所有写入与提取使用同一锁，避免迟到工作线程在快照/清空窗口丢记录。
# 函数用途: 保留尚未到期 fixture 的进程身份与后续缓存查找义务。
def _keep_shared_test_process_entry(key, entry) -> None:
    with _TEST_PROCESS_ENTRIES_LOCK:
        _SHARED_TEST_PROCESS_ENTRIES[key] = entry


# LLM: 清理异常或非明确 confirmed=True 的结果均保留脱敏结构化诊断；不改原测试断言和 pytest 退出码。
# 函数用途: 统一正常与迟到进程的精确回收，把未确认事实留给会话结束检查。
def _reap_test_process_entry(module, entry) -> None:
    try:
        result = module.reap_test_registered_process(entry)
    except Exception as exc:
        result = {"confirmed": False, "error_type": type(exc).__name__}
    if not isinstance(result, dict) or result.get("confirmed") is not True:
        _record_unconfirmed_test_reap(entry, result, "reap")


# LLM: 只保留创建身份和清理回执，不复制命令、环境变量或 Popen；追加与读取共享表共锁，不吞掉失败事实。
# 函数用途: 收集回收未确认或缓存清理异常，供测试断言及会话诊断读取。
def _record_unconfirmed_test_reap(entry, result, stage) -> None:
    keys = ("test_node_id", "session_id", "pid", "pid_birth_token", "child_pid", "child_pid_birth_token",
            "pgid", "owner_id", "task_id", "store_root", "scope_level")
    diagnostic = {key: entry.get(key) for key in keys}
    diagnostic.update(stage=stage, result=result if isinstance(result, dict) else {"confirmed": False, "reason": "no_receipt"})
    with _TEST_PROCESS_ENTRIES_LOCK:
        _UNCONFIRMED_PROCESS_REAPS.append(diagnostic)


# LLM: 只从各已加载 registry 撤下本轮到期的精确 session/原目录；共享项不调用 clear，也不碰其它 owner 同名映射。
# 函数用途: 回收后清掉到期会话索引，缓存异常同样保留诊断而不遮盖原测试失败。
def _forget_expired_test_process_entries(pairs, entries) -> None:
    for entry in entries:
        session = entry.get("session_id")
        if not session:
            continue
        for module, _launch in pairs:
            try:
                module.process_registry.forget(session, store_root=entry.get("store_root") or None)
            except Exception as exc:
                _record_unconfirmed_test_reap(entry, {"confirmed": False, "error_type": type(exc).__name__}, "forget")


# LLM: scope 必须来自真实 fixture 上下文或显式账本字段；函数结束不清理仍有效的共享资源。
# 函数用途: 按下一个测试项判断 module/session 共享进程的作用域是否到期。
def _test_process_scope_still_live(entry, nextitem) -> bool:
    if nextitem is None or entry.get("scope_expired"):
        return False
    lifetime = entry.get("scope_lifetime")
    if lifetime is not None:
        return not lifetime["expired"]
    scope = entry.get("scope_level", "function")
    if scope == "session":
        return True
    owner = entry.get("scope_owner") or str(entry.get("test_node_id", "")).split("::")[0]
    return scope == "module" and owner == nextitem.nodeid.split("::")[0]


# LLM: setup 包装钩子在所有 fixture 创建前安装捕获，含 module/session setup；最外层 teardown 负责撤销。
# 函数用途: 建立当前测试的并发安全进程账本与独立 MonkeyPatch。
def _begin_test_process_ownership(item) -> None:
    global _TEST_PROCESS_SUITE_CLOSING
    _TEST_PROCESS_SUITE_CLOSING = False
    state = SimpleNamespace(entries={}, lock=threading.Lock(), patch=pytest.MonkeyPatch(), closed=False)
    item._test_process_ownership = state
    _install_test_process_capture(state, item.nodeid)


# LLM: 用实际 pytest fixture scope 标记创建资源，不按函数名、PID 或测试名字猜共享寿命。
# 函数用途: 让创建回调分清函数、模块和会话级 fixture 的进程归属。
@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef, request):
    global _ACTIVE_TEST_FIXTURE_SCOPE
    previous = getattr(_TEST_PROCESS_FIXTURE_SCOPE, "current", None)
    active_previous = _ACTIVE_TEST_FIXTURE_SCOPE
    lifetime = {"expired": False}
    request.addfinalizer(partial(_expire_test_process_fixture, lifetime))
    _TEST_PROCESS_FIXTURE_SCOPE.current = (fixturedef.scope, request.node.nodeid, lifetime)
    _ACTIVE_TEST_FIXTURE_SCOPE = _TEST_PROCESS_FIXTURE_SCOPE.current
    try:
        return (yield)
    finally:
        _TEST_PROCESS_FIXTURE_SCOPE.current = previous
        _ACTIVE_TEST_FIXTURE_SCOPE = active_previous


# LLM: 在 fixture 其它 finalizer 后标记本实例到期；不在仍有测试替身的环境里发信号。
# 函数用途: 让 class/package/module/session 参数实例的进程在外层收尾时准确回收。
def _expire_test_process_fixture(lifetime) -> None:
    lifetime["expired"] = True


# LLM: 账本在 setup 最外层已创建；finally 保留捕获到其它 fixture 收尾后，原外层钩子再统一关闭。
# 函数用途: 为每条测试提供自己的派生进程账本，不借用测试的 monkeypatch 清理顺序。
@pytest.fixture(autouse=True)
def _background_process_ownership(request):
    state = request.node._test_process_ownership
    try:
        yield state.entries
    finally:
        pass  # 捕获器不能在本 fixture 结束时先撤销，否则会漏掉其它 finalizer 派生的进程。


@pytest.fixture
def gateway_client_credential(_isolate_my_agent_home):
    """旧 HTTP 行为测试显式模拟 Gateway 已完成 G1 初始化；不影响缺凭据的拒绝测试。"""
    from agent_py_agent.agent.gateway_parts.local_client_token import ensure_local_client_credential
    from agent_py_agent.agent.user_space.home_layout import home_paths

    return ensure_local_client_credential(home_paths().root)


# LLM: G3 插件命令回归：插件提交测试要连本机假 Gateway（真实 HTTP + 凭据分流），不能再 mock 传输函数。
# 函数用途: 按用例提供的响应器起假 Gateway；用例结束统一回收，避免监听线程泄漏。
@pytest.fixture
def gateway_stub_factory():
    from agent_py_agent.tests._gateway_stub import GatewayStub

    stubs = []

    def make(responder):
        stub = GatewayStub(responder).start()
        stubs.append(stub)
        return stub

    yield make
    for stub in stubs:
        stub.stop()


# LLM: G3 回归用例的 Variant 参数（网关开关/模式/输入/期望文案）收进一个 fixture，避免测试函数参数超限；
#   fixture 直接读本次 parametrize 的请求参数，不改变参数化语义。
# 函数用途: 把一个变体的四元参数打包给用例，替代 7 个函数参数。
@pytest.fixture
def plugin_submit_variant(request):
    from types import SimpleNamespace

    params = request.node.callspec.params
    return SimpleNamespace(
        use_gateway=params["use_gateway"], mode=params["mode"],
        raw=params["raw_message"][0], message=params["raw_message"][1],
    )


# LLM: pytest_configure 是测试共享 marker 的唯一注册入口；pyproject 和本 hook 的名称必须一致。
# 函数用途: 注册桌面防线与沙箱能力 marker，保证收集和报告都使用同一协议。
def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        f"{REAL_DESKTOP_MARKER}: 测试确实需要调用真实的 open/xdg-open/osascript/通知/剪贴板程序；默认全部被测试防线拦截",
    )
    config.addinivalue_line(
        "markers",
        "sandbox_capability(*names): 需要 my-agent 会话沙箱里不可用的能力（ps、nested_sandbox_exec、"
        "background_launcher_identity）；缺失时按环境 skip，设 MY_AGENT_TEST_REQUIRE_CAPABILITIES=1 时 fail",
    )


# LLM: 能力判定集中在公共层，用例只声明 marker；缺能力时默认 skip、强制开关打开时 fail，
#   这样"沙箱里跳过"和"沙箱外必须真跑"用同一处逻辑，不会各文件各写一套。
# 函数用途: 读 marker 并返回动作、结构化缺项和固定失败原因。
def _capability_gate_action(item) -> tuple[str, list[str], str] | None:
    marker = item.get_closest_marker("sandbox_capability")
    if marker is None or not marker.args:
        return None
    missing = missing_capabilities([str(name) for name in marker.args])
    if not missing:
        return None
    kind = "FAIL" if capabilities_required() else "SKIP"
    return kind, missing, missing_capability_reason(missing)


@pytest.fixture(scope="session", autouse=True)
def _desktop_open_guard(tmp_path_factory):
    """会话级防线：shim 目录放到 PATH 最前，webbrowser.open* 换成只记录的替身，任何测试都打不开用户桌面程序。

    子进程经 PATH 继承 shim（插件 MCP 子进程经 build_safe_env 保留 PATH）；自己改写 PATH 的测试自行负责。
    """
    global _DESKTOP_GUARD, _FINISHED_DESKTOP_GUARD
    guard = DesktopOpenGuard.install(tmp_path_factory.mktemp("desktop_open_guard"))
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PATH", guard.path_with_shims(os.environ.get("PATH", "")))
        for name in guard.originals:
            patch.setattr(webbrowser, name, guard.python_opener(name))
        _DESKTOP_GUARD = guard
        try:
            yield guard
        finally:
            _DESKTOP_GUARD, _FINISHED_DESKTOP_GUARD = None, guard


@pytest.fixture(scope="session", autouse=True)
def _real_screen_guard():
    """会话级防线：两个桌面后端唯一的真实库加载入口换成直接失败（见 _screen_capture_guard），测试永远碰不到真实屏幕。

    X11 的入口只在 Linux 车道容器（车道标记为 1）里不换，那里是 Xvfb 假桌面；注入假库的后端不走这两个入口；
    子进程那一层由 test_screen_capture_guard 的扫描守卫兜住。
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(computer_use_macos, "load_real_macos_frameworks", forbidden_real_macos_frameworks)
        if os.environ.get(LANE_SKIP_MARKER) != "1":
            patch.setattr(computer_use_x11, "load_real_x11_libraries", forbidden_real_x11_libraries)
        yield


@pytest.fixture(autouse=True)
def _desktop_open_guard_per_test(request, _desktop_open_guard):
    """声明了真实调用的测试放行：PATH 去掉 shim、webbrowser 恢复原实现。

    用自己的 MonkeyPatch，不借用测试的 monkeypatch；检查放在 pytest_runtest_teardown 之后统一做。
    """
    if not hasattr(request.node, "_desktop_guard_mark"):
        # 本会话第一条测试在 setup 钩子里还没有防线实例，在这里补记游标。
        request.node._desktop_guard_mark = _desktop_open_guard.mark()
    if request.node.get_closest_marker(REAL_DESKTOP_MARKER) is None:
        yield
        return
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PATH", _desktop_open_guard.path_without_shims(os.environ.get("PATH", "")))
        for name, original in _desktop_open_guard.originals.items():
            patch.setattr(webbrowser, name, original)
        yield


# LLM: 创建捕获先于所有 fixture；setup 完成后再按冻结函数增量扫描；保留桌面游标与原 skipif 顺序，
#   再用同一个包装钩子应用能力门，防止平台不适用的测试被能力失败覆盖。
# 函数用途: 记录桌面守卫游标，并在 pytest 平台跳过检查之后按 marker skip 或 fail。
@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_setup(item):
    """先留存桌面防线游标，再在内层 pytest skipif 返回后执行能力门。"""
    _begin_test_process_ownership(item)
    if _DESKTOP_GUARD is not None:
        item._desktop_guard_mark = _DESKTOP_GUARD.mark()
    result = yield
    _refresh_test_launch_aliases(item._test_process_ownership)
    action = _capability_gate_action(item)
    if action is None:
        return result
    kind, missing, reason = action
    item.user_properties.append(("sandbox_capability_missing", ",".join(missing)))
    if kind == "FAIL":
        pytest.fail(reason, pytrace=False)
    pytest.skip(reason)
    return result


# LLM: 回收必须在全部正常 fixture/monkeypatch finalizer 之后；finally 保留原测试及 teardown 异常。
# 函数用途: 最外层精确回收本测试登记资源，再按原合同检查桌面防线。
@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    """本测试完整收尾之后（它的 fixture 与 monkeypatch 都已撤销）再检查期间的拦截记录，有就让这条测试报错。

    测试体、夹具收尾和插件进程退出时的调用都在这里一次归属；读记录只用底层 os，测试的 IO 哨兵和打桩碰不到。
    """
    guard = _DESKTOP_GUARD
    mark = getattr(item, "_desktop_guard_mark", None)
    try:
        result = yield
    finally:
        _cleanup_test_process_entries(item, nextitem)
    if guard is None or mark is None:
        return result
    records = guard.take_since(mark)
    if records and item.get_closest_marker(REAL_DESKTOP_MARKER) is None:
        pytest.fail(failure_message(records, f"测试 {item.nodeid} "), pytrace=False)
    return result


# LLM: 桌面防线仍保持原失败合同；进程未确认诊断只汇总事实，不改变原测试退出码。
# 函数用途: 在会话结束报告桌面违规及仍需复核的进程清理记录数量。
def pytest_sessionfinish(session, exitstatus):
    guard = _DESKTOP_GUARD or _FINISHED_DESKTOP_GUARD
    records = guard.unattributed() if guard is not None else []
    if records:
        session.config.get_terminal_writer().line(failure_message(records, "测试会话中（未能归属到单条测试的后台进程）"))
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    if _UNCONFIRMED_PROCESS_REAPS:
        session.config.get_terminal_writer().line(f"测试派生进程清理未确认：{len(_UNCONFIRMED_PROCESS_REAPS)} 条，诊断已保留。")


@pytest.fixture(autouse=True)
def _repo_tree_guard(request):
    """仓库树防线：测试不能往起跑目录写 MagicMock/，也不能改写仓库根被跟踪的 CODE_SIZE_REPORT.md（见 _repo_tree_guard）。

    起跑目录取 pytest 启动时的 cwd，测试里切目录不影响；前后指纹不同就让这条测试在收尾时报错，
    这条测试新建的 MagicMock/ 顺手删掉，会话开始前的残留只比对不删除；报告文件只报错、不自动恢复。
    """
    root = str(request.config.invocation_params.dir)
    before, report_before = magicmock_fingerprint(root), tracked_report_fingerprint(REPO_ROOT)
    yield
    after, problems = magicmock_fingerprint(root), []
    if magicmock_violation(before, after):
        if before is None:
            remove_magicmock_dir(root)
        problems.append(magicmock_failure_message(request.node.nodeid, root, created=before is None))
    if tracked_report_fingerprint(REPO_ROOT) != report_before:
        problems.append(tracked_report_failure_message(request.node.nodeid, REPO_ROOT))
    if problems:
        pytest.fail("\n".join(problems), pytrace=False)


@pytest.fixture(autouse=True)
def _isolate_my_agent_home(tmp_path_factory, monkeypatch):
    """默认把每个测试的 MY_AGENT_HOME 隔离到临时目录。

    防两件事:① 测试污染用户真实 ~/.my-agent(测试不该写用户数据)② runtime workspace 在全局 home
    累积泄漏(实测曾累积 16 万目录、拖垮 collaboration 测试)。
    显式传 home/my_agent_home 的测试不受影响——resolve_my_agent_home 里 value 优先于 env;
    需要特定 MY_AGENT_HOME 的用例可在测试体内 monkeypatch 覆盖(后设生效)。
    """
    home = tmp_path_factory.mktemp("ma_home")
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    # Tests start real loopback HTTP servers.  A desktop/system proxy must not
    # intercept those private test fixtures; preserve any operator exclusions
    # while making the standard loopback set explicit for both spellings.
    exclusions = _loopback_no_proxy(os.environ.get("NO_PROXY", ""))
    monkeypatch.setenv("NO_PROXY", exclusions)
    monkeypatch.setenv(
        "no_proxy",
        _loopback_no_proxy(os.environ.get("no_proxy", "")),
    )
    # 在 Gateway 托管的工具进程里跑测试时会继承托管标记；清掉它，生命周期命令测试才不受宿主影响。
    monkeypatch.delenv("MY_AGENT_HOSTING_GATEWAY_PID", raising=False)



@pytest.fixture(autouse=True)
def _isolate_decision_reach_counts(monkeypatch):
    """每个测试用自己的决策点到达计数待写队列，测完丢弃。

    待写队列（conversation/decision_reach_counts）是进程级的；Gateway 收尾会真实落盘全部待写计数，
    不隔离的话会把别的测试留下的计数写进它们（可能已删除的）临时 home。
    """
    from agent_py_agent.agent.conversation import decision_reach_counts

    monkeypatch.setattr(decision_reach_counts, "_PENDING", {})
    monkeypatch.setattr(decision_reach_counts, "_LAST_FLUSH", {})


@pytest.fixture(autouse=True)
def _isolate_model_call_admission(monkeypatch):
    """每个测试用自己的模型调用准入表，测完丢弃。

    准入表（contracts/model_call_ledger）是进程级的：Gateway 收尾会关门、进程内不重开，账本构造时登记进去。
    不隔离的话，一条跑过收尾的测试之后，同一测试进程里所有账本都不再接新调用；别的测试留下的在途调用也会混进停机结清。
    """
    from agent_py_agent.agent.contracts import model_call_ledger

    monkeypatch.setattr(model_call_ledger, "_ADMISSION_REGISTRY", model_call_ledger._ModelCallAdmissionRegistry())


@pytest.fixture(autouse=True)
def _isolate_gateway_bound_ports():
    """每条用例前后保存并复原本机 Gateway 端口注册表（G4）。

    注册表（attempt/sandbox._LOCAL_GATEWAY_PORTS）是进程级集合：哪条用例起了 Gateway 却没走到 stop（比如中途失败），
    它登记的端口会漏给同一 pytest 进程后面的用例，构造模型命令沙箱时多出端口拒绝规则，用例结果就会依赖运行顺序。
    """
    from agent_py_agent.agent.attempt import sandbox

    with sandbox._LOCAL_GATEWAY_PORTS_LOCK:
        saved = set(sandbox._LOCAL_GATEWAY_PORTS)
    try:
        yield
    finally:
        with sandbox._LOCAL_GATEWAY_PORTS_LOCK:
            sandbox._LOCAL_GATEWAY_PORTS.clear()
            sandbox._LOCAL_GATEWAY_PORTS.update(saved)


def _loopback_no_proxy(current: str) -> str:
    values = [item.strip() for item in str(current or "").split(",") if item.strip()]
    for item in ("127.0.0.1", "localhost", "::1"):
        if item not in values:
            values.append(item)
    return ",".join(values)


@pytest.fixture
def inline_watch_open(monkeypatch):
    """Make ordinary watch tests use the internal non-background lane.

    ``background_harvest`` is intentionally not a model-facing tool argument.
    Tests that exercise the older inline ordinary-watch behavior inject the
    host-owned tuning before the state is persisted instead of teaching callers
    a removed public parameter.
    """

    import importlib

    # The suite intentionally exercises both supported import roots.  Patch the
    # module used by each root; patching only one leaves the other free to start
    # a background harvester and makes otherwise-inline assertions race.
    for module_name in (
        "agent.ingestion.watch_tool",
        "agent_py_agent.agent.ingestion.watch_tool",
    ):
        watch_tool = importlib.import_module(module_name)
        original_new_state = watch_tool.new_state

        def _new_state(*args, _original=original_new_state, **kwargs):
            state = _original(*args, **kwargs)
            tuning = replace(state.tuning, background_harvest=0)
            state.tuning = tuning
            state.engine.tuning = tuning
            return state

        monkeypatch.setattr(watch_tool, "new_state", _new_state)


@pytest.fixture
def skill_catalog_factory():
    """Build the production SkillsService without restoring the deleted registry path."""

    def build(root: Path, *, extra_roots: list[Path] | None = None, policy_overrides=None):
        owner = root / "owner"
        shared = root / "shared"
        builtin = root / "builtin"
        workspace = root / "workspace"
        for directory in (owner / "skills", shared, builtin, workspace):
            directory.mkdir(parents=True, exist_ok=True)
        policy_values = {
            "owner_id": "test-owner",
            "skills_enabled": True,  # 总闸 effective flag(默认开启,测试覆盖关闭路径时置 False)
            "enabled_skill_sources": ("workspace", "owner", "shared", "builtin"),
            "enabled_shared_skills": (),
            "disabled_skills": (),
        }
        policy_values.update(policy_overrides or {})
        policy = SimpleNamespace(**policy_values)
        home = SimpleNamespace(
            owner_home_dir=owner,
            shared_skills_dir=shared,
            shared_builtin_dir=builtin,
        )
        service = SkillsService(
            home_paths=home,
            workspace_root=workspace,
            policy_provider=lambda: policy,
        )
        service.set_extra_roots(extra_roots or [])
        snapshot = service.snapshot_for(workspace)
        return SimpleNamespace(
            service=service,
            snapshot=snapshot,
            router=CapabilityRouter(skill_snapshot=snapshot),
            home=home,
            workspace=workspace,
            policy=policy,
        )

    return build


# ---------------------------------------------------------------------------
# SubAgentTask fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_subagent_task() -> SubAgentTask:
    """创建示例 SubAgentTask。"""
    return SubAgentTask(
        id="test-task-001",
        goal="测试任务目标",
        thought="测试思考过程",
        plan=["步骤1", "步骤2", "步骤3"],
        status="PLANNING",
        depth=0,
    )


# ---------------------------------------------------------------------------
# SubAgentTask mock fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_task(tmp_path: Path) -> MagicMock:
    """Create a mock SubAgentTask with file paths set under tmp_path."""
    from agent_py_agent.agent.subagents.models import SubAgentTask
    task = MagicMock(spec=SubAgentTask)
    task.id = "test-run-123"
    task.status = "RUNNING"
    task.verification_status = "UNVERIFIED"
    task.failure_type = ""
    task.result = ""
    task.ended_at = 0.0
    task.updated_at = 0.0
    task.heartbeat_at = 0.0
    task.runner_attempts = 0
    task.runner_last_attempt_at = 0.0
    task.runner_last_error = ""
    task.runner_active_attempt_id = ""
    task.runner_abandoned_attempt_ids = []
    task.used_tools = []
    task.used_skills = []
    task.evidence = []
    task.evidence_packets = []
    task.findings = []
    task.evidence_refs = []
    task.artifact_refs = []
    task.blockers = []
    task.capability_requests = []
    task.capability_grants = []
    task.allowed_tools = ["tool_a", "tool_b"]
    task.allowed_skills = ["skill_x"]
    task.runner_prompt_file = str(tmp_path / "prompt.txt")
    task.runner_response_file = str(tmp_path / "response.txt")
    task.runner_result_file = str(tmp_path / "result.md")
    task.runner_result_json = str(tmp_path / "result.json")
    task.output_json = str(tmp_path / "output.json")
    task.debrief_file = str(tmp_path / "debrief.md")
    return task


# ---------------------------------------------------------------------------
# LocalStore mock fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_connection_ctx():
    """创建可配置 mock 连接上下文管理器。"""
    @contextmanager
    def _ctx(mock_conn: MagicMock):
        yield mock_conn
    return _ctx


@pytest.fixture
def mock_local_store(make_fake_store_func):
    """创建配置好的 FakeStore 实例（用于 LocalStoreMaintenanceMixin 测试）。"""
    return make_fake_store_func()


def _fake_store_options(kwargs):
    options = {
        "fts_available": kwargs.pop("fts_available", True),
        "db_path": kwargs.pop("db_path", None),
        "files_dir": kwargs.pop("files_dir", None),
        "events_path": kwargs.pop("events_path", None),
        "mock_conn": kwargs.pop("mock_conn", None),
    }
    if kwargs:
        raise TypeError(f"Unexpected FakeStore options: {sorted(kwargs)}")
    return options


def _attach_mock_connection(store, mock_conn) -> None:
    @contextmanager
    def conn_ctx():
        yield mock_conn

    store._connection = conn_ctx


# LLM: 维护测试替身复用真实 schema 完成标记钩子；连接仍由用例注入，不初始化磁盘或以空钩子掩盖提交合同。
# 函数用途: 为本地存储维护测试组装最小同构 Store，避免共享测试访问真实用户库。
@pytest.fixture
def make_fake_store_func():
    """Factory 函数：创建自定义 FakeStore。"""
    from agent_py_agent.agent.local_storage.maintenance import LocalStoreMaintenanceMixin
    from agent_py_agent.agent.local_storage.schema import LocalStoreSchemaMixin

    def _make(**kwargs) -> MagicMock:
        """创建 FakeStore 实例。"""
        options = _fake_store_options(kwargs)

        # LLM: 与真实 LocalStore 一样由 schema 提供回填完成钩子；不调用 schema 初始化，SQL 只发往注入的假连接。
        # 类用途: 在维护单测中保留真实回填事务行为，正文、事件和连接由现有测试替身隔离。
        class FakeStore(LocalStoreSchemaMixin, LocalStoreMaintenanceMixin):
            # LLM: 最小Store替身显式选择原False策略，不借缺字段模拟C写权；原SQL/FTS断言保持。
            # 函数用途: 初始化默认本地测试对象，后续维护仍只用原连接与正文替身。
            def __init__(self):
                self.coordinator_io = False
                self._fts_available = options["fts_available"]
                self.db_path = options["db_path"] or Path("/tmp/test.db")
                self.files_dir = options["files_dir"] or Path("/tmp/files")
                self.events_path = options["events_path"] or Path("/tmp/events.jsonl")

            @property
            def fts_available(self) -> bool:
                return self._fts_available

            def _read_content(self, row) -> str:
                return "mock content"

            def _replace_fts_row(self, conn, row_id, title, content):
                pass

            def _record_event(self, conn, event_type, source_id, metadata):
                pass

            def _resolve_content_path(self, content_path) -> Path:
                return Path(content_path) if content_path else Path("/tmp/unknown")

            def _record_filters(self, source_type=None):
                if source_type:
                    return "WHERE source_type = ?", (source_type,)
                return "", ()

        store = FakeStore()
        if options["mock_conn"]:
            _attach_mock_connection(store, options["mock_conn"])
        return store
    return _make


# ---------------------------------------------------------------------------
# Mock connection helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_db_conn():
    """创建配置好的 mock 数据库连接。"""
    mock_conn = MagicMock()
    mock_result = MagicMock()
    mock_result.fetchall.return_value = []
    mock_result.fetchone.return_value = (0,)
    mock_conn.execute.return_value = mock_result
    return mock_conn


@pytest.fixture
def db_conn_with_records(records: list[dict[str, Any]]) -> MagicMock:
    """创建包含记录数据的 mock 数据库连接。"""
    mock_conn = MagicMock()
    mock_result = MagicMock()
    mock_result.fetchall.return_value = records
    mock_result.fetchone.return_value = (len(records),)
    mock_conn.execute.return_value = mock_result
    return mock_conn
