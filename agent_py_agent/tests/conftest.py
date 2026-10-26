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


# LLM: 归属只来自当前测试的显式创建回调，读状态/出生探针不产生清理权限。
# 函数用途: 将创建快照去重写入本测试的进程账本。
def _record_test_process_spawn(state, nodeid: str, proc, scope_context=None) -> None:
    key = (proc.session_id, proc.pid)
    scope, owner, *lifetime = scope_context or _current_test_process_scope(nodeid)
    entry = {"test_node_id": nodeid, "session_id": proc.session_id, "pid": proc.pid,
             "pid_birth_token": proc.pid_birth_token, "store_root": proc.store_root,
             "popen": proc.process, "scope_level": scope, "scope_owner": owner,
             "scope_lifetime": lifetime[0] if lifetime else None}
    reap_late = False
    with _TEST_PROCESS_ENTRIES_LOCK:
        previous = state.entries.get(key) or _SHARED_TEST_PROCESS_ENTRIES.get(key)
        if (previous and previous["pid_birth_token"] == proc.pid_birth_token
                and previous["store_root"] == proc.store_root):
            entry["popen"] = proc.process or previous["popen"]
            entry["scope_level"], entry["scope_owner"] = previous["scope_level"], previous["scope_owner"]
            entry["scope_lifetime"] = previous.get("scope_lifetime")
        closed = getattr(state, "closed", False)
        defer = not _TEST_PROCESS_SUITE_CLOSING and _test_process_scope_still_live(entry, SimpleNamespace(nodeid=nodeid))
        reap_late = closed and not defer
        if not reap_late:
            target = _SHARED_TEST_PROCESS_ENTRIES if closed else state.entries
            target[key] = entry
    if reap_late:
        from agent_py_agent.agent.tooling.process_registry import reap_test_registered_process

        with suppress(Exception):
            reap_test_registered_process(entry)


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


# LLM: 包装真实启动入口及已导入别名，返回值和异常原样传播；不替换 subprocess 或探针。
# 函数用途: 在测试期间记录受管接管与未接管的直接启动结果。
def _install_test_process_capture(state, nodeid: str) -> None:
    global _ACTIVE_TEST_PROCESS_OWNER
    _ACTIVE_TEST_PROCESS_OWNER = (state, nodeid)
    state.installed = {}
    for module, launch in _test_process_module_pairs():
        original = launch.start_background_process
        start = _test_background_launch_wrapper(original, module)
        module.set_test_process_spawn_hook(_capture_active_test_process_spawn)
        _patch_test_launch_aliases(state, original, start)


# LLM: 只按同一函数对象替换当前解释器内的别名，不扫描 OS 进程或按名称选择 PID。
# 函数用途: 覆盖测试收集时提前导入的启动函数，同时记录撤销需要的精确后像。
def _patch_test_launch_aliases(state, original, start) -> None:
    for name, loaded in list(sys.modules.items()):
        if loaded is None or not name.startswith(("agent_py_agent.", "agent.")):
            continue
        for attr, value in list(vars(loaded).items()):
            if value is original:
                state.patch.setattr(loaded, attr, start)
                state.installed[(id(loaded), attr)] = start


# LLM: 此独立 MonkeyPatch 只含启动函数 setattr；仅撤销后像仍相同的项，保留高 scope fixture 的后续写入。
# 函数用途: 条件恢复 pytest setattr 栈，避免无条件 undo 提前盖掉模块/会话替身。
def _undo_test_process_capture(state) -> None:
    installed = getattr(state, "installed", {})
    state.patch._setattr[:] = [
        (target, name, previous) for target, name, previous in state.patch._setattr
        if (id(target), name) not in installed or getattr(target, name, None) is installed[(id(target), name)]
    ]
    state.patch.undo()


# LLM: 只能在最外层 teardown、其它 monkeypatch 撤销后调用；清理失败不得盖掉测试失败。
# 函数用途: 撤销捕获器，精确回收登记项，最后丢弃 registry 缓存。
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
    try:
        for key, entry in entries.items():
            if _test_process_scope_still_live(entry, nextitem):
                with _TEST_PROCESS_ENTRIES_LOCK:
                    _SHARED_TEST_PROCESS_ENTRIES[key] = entry
                continue
            with suppress(Exception):
                pairs[0][0].reap_test_registered_process(entry)
    finally:
        for module, _launch in pairs:
            with suppress(Exception):
                module.process_registry.clear()


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


# LLM: 创建捕获先于所有 fixture；保持桌面守卫游标；先委托给 pytest 内层 setup hook 处理 skipif，
#   再用同一个包装钩子应用能力门，防止平台不适用的测试被能力失败覆盖。
# 函数用途: 记录桌面守卫游标，并在 pytest 平台跳过检查之后按 marker skip 或 fail。
@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_setup(item):
    """先留存桌面防线游标，再在内层 pytest skipif 返回后执行能力门。"""
    _begin_test_process_ownership(item)
    if _DESKTOP_GUARD is not None:
        item._desktop_guard_mark = _DESKTOP_GUARD.mark()
    result = yield
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


def pytest_sessionfinish(session, exitstatus):
    guard = _DESKTOP_GUARD or _FINISHED_DESKTOP_GUARD
    records = guard.unattributed() if guard is not None else []
    if records:
        session.config.get_terminal_writer().line(failure_message(records, "测试会话中（未能归属到单条测试的后台进程）"))
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


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


# B5 专属回归受两文件白名单约束，显式运行：pytest agent_py_agent/tests/conftest.py -k test_b5 -q。
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
    monkeypatch.setattr(module, "terminate_process_tree", lambda *args, **kwargs: calls.append((args, kwargs)))
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
    _record_test_process_spawn(state, "module.py::test_example", proc)
    _record_test_process_spawn(state, "module.py::test_example", proc)
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
        _install_test_process_capture(state, "module.py::test_example")
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
    entries = {(str(pid), pid): {"pid": pid, "scope_level": "function"} for pid in (2, 3)}
    state = SimpleNamespace(entries=entries, patch=pytest.MonkeyPatch())
    item = SimpleNamespace(_test_process_ownership=state, nodeid="module.py::test_example")
    monkeypatch.setattr(module, "reap_test_registered_process", reap)
    monkeypatch.setattr(module.process_registry, "clear", lambda: calls.append("clear"))
    _cleanup_test_process_entries(item)
    assert calls == [2, 3, "clear"]


# LLM: 下一个函数重新接管同一个共享进程不能降级寿命或覆盖最初作用域。
# 函数用途: 防止 module/session 进程在下一条测试 attach 后被过早回收。
def test_b5_reattach_preserves_shared_process_scope(monkeypatch):
    from agent_py_agent.agent.tooling.process_registry import BackgroundProcess

    key = ("bg-shared", 987654321)
    shared = {key: {"pid_birth_token": "birth-test", "popen": None,
                    "scope_level": "module", "scope_owner": "module.py", "store_root": "/test/store"}}
    monkeypatch.setattr(sys.modules[__name__], "_SHARED_TEST_PROCESS_ENTRIES", shared)
    state = SimpleNamespace(entries={}, lock=threading.Lock())
    proc = BackgroundProcess(*key[:1], "test", key[1], 1.0, pid_birth_token="birth-test", store_root="/test/store")
    _record_test_process_spawn(state, "module.py::next_test", proc)
    assert state.entries[key]["scope_level"] == "module"


# LLM: 参数化高作用域 fixture 由实际实例寿命裁决，不仅看下条测试 nodeid 是否相同。
# 函数用途: 覆盖 class/package 共享，以及 module/session 参数实例的到期回收。
@pytest.mark.parametrize("scope,expired,expected", [
    ("class", False, True), ("package", False, True), ("module", True, False), ("session", True, False),
])
def test_b5_scope_follows_fixture_instance(scope, expired, expected):
    entry = {"scope_level": scope, "scope_owner": "module.py", "scope_lifetime": {"expired": expired}}
    assert _test_process_scope_still_live(entry, SimpleNamespace(nodeid="module.py::test_next")) is expected


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
        _install_test_process_capture(first, "module.py::test_first")
        old_alias = launch.start_background_process
        first.patch.undo()
        _install_test_process_capture(second, "module.py::test_second")
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
    _install_test_process_capture(state, item.nodeid)
    later = object()
    try:
        launch.start_background_process = later
        _cleanup_test_process_entries(item)
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
    monkeypatch.setattr(sys.modules[__name__], "_SHARED_TEST_PROCESS_ENTRIES", {})
    state = SimpleNamespace(entries={}, lock=threading.Lock(), closed=True)
    proc = module.BackgroundProcess("bg-late", "test", 987654321, 1.0, pid_birth_token="birth-test")
    _record_test_process_spawn(state, "module.py::test_finished", proc)
    assert len(reaped) == 1
    assert reaped[0]["test_node_id"] == "module.py::test_finished"
    assert not _SHARED_TEST_PROCESS_ENTRIES


# LLM: 高作用域 fixture 等待工作线程完成启动时，其真实实例寿命仍须贯穿该次登记。
# 函数用途: 验证线程池式创建不会被错误降级成 function scope。
def test_b5_fixture_scope_reaches_spawn_worker():
    from agent_py_agent.agent.tooling.process_registry import BackgroundProcess

    finalizers = []
    request = SimpleNamespace(node=SimpleNamespace(nodeid="module.py"), addfinalizer=finalizers.append)
    hook = pytest_fixture_setup(SimpleNamespace(scope="module"), request)
    state = SimpleNamespace(entries={}, lock=threading.Lock())
    proc = BackgroundProcess("bg-thread", "test", 987654321, 1.0, pid_birth_token="birth-test")
    next(hook)
    try:
        worker = threading.Thread(target=_record_test_process_spawn, args=(state, "module.py::test_worker", proc))
        worker.start()
        worker.join(timeout=3)
        assert not worker.is_alive()
        assert state.entries[(proc.session_id, proc.pid)]["scope_level"] == "module"
    finally:
        with suppress(StopIteration):
            next(hook)
        for finalize in reversed(finalizers):
            finalize()
