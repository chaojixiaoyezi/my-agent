"""J16 片 B 接入层：开关 → profile 并入两行声明与环境标记（属主范围不变）；观察工具调用三元组与底层处理器（假 mcp.types）；
X11 后端在假 Xlib 上的事实提取。"""
from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.plugin_observation import (
    OBSERVATION_META_EXTENSION,
    PluginToolObservation,
    PluginToolObservationRef,
)
from agent_py_agent.agent.tooling import computer_use_observation_tools as glue
from agent_py_agent.agent.tooling.computer_use_profile import (
    COMPUTER_USE_MCP_SERVER_NAME,
    OBSERVATION_ENV_FLAG,
    computer_use_mcp_servers,
    with_computer_use_observation,
)
from agent_py_agent.agent.tooling.mcp_client import MCPServerConfig, MCPToolInfo
from agent_py_agent.agent.tooling.mcp_declarations import resolve_server_declarations
from agent_py_agent.agent.tooling.screen_observation import ObservationError

OBSERVE_SCHEMA = {"type": "object", "properties": {"window": {"type": "string"}}}
CLICK_SCHEMA = {"type": "object", "properties": {"candidate_id": {"type": "string"}}}


def _profile(enabled=True, observation=True, admin=True, access="full-access"):
    servers = computer_use_mcp_servers({"other": {"command": "x"}}, enabled=enabled, is_local_admin=admin, access_mode=access,
                                       environ={"DISPLAY": ":99"}, python_executable="/py")
    return with_computer_use_observation(servers, enabled=observation)


def test_switch_adds_the_two_declarations_and_the_env_flag_only_inside_the_owner_scope():
    profile = _profile()[COMPUTER_USE_MCP_SERVER_NAME]
    assert profile["env"][OBSERVATION_ENV_FLAG] == "1" and profile["env"]["DISPLAY"] == ":99"
    assert profile["tool_effects"]["observe_window"] == "read_only" and profile["tool_effects"]["click_candidate"] == "dangerous"
    assert profile["tool_effects"]["list_windows"] == "read_only", "原有 16 项声明保持"
    assert profile["tool_approvals"] == {"observe_window": "always"}
    config = MCPServerConfig.from_mapping(COMPUTER_USE_MCP_SERVER_NAME, profile)
    assert config.tool_observations == {"observe_window": PluginToolObservation("window", 64),
                                        "click_candidate": PluginToolObservationRef("window", "candidate_id")}
    resolved = resolve_server_declarations(config, [MCPToolInfo("observe_window", "o", OBSERVE_SCHEMA), MCPToolInfo("click_candidate", "c", CLICK_SCHEMA),
                                                     MCPToolInfo("list_windows", "l", {})])
    assert resolved.tools["observe_window"].approval == "always" and resolved.notices == ()
    off = _profile(observation=False)[COMPUTER_USE_MCP_SERVER_NAME]
    assert OBSERVATION_ENV_FLAG not in off["env"] and "observe_window" not in off["tool_effects"] and off["tool_approvals"] == {}
    for scope in ({"admin": False}, {"access": "restricted"}, {"enabled": False}):
        servers = _profile(**scope)
        assert COMPUTER_USE_MCP_SERVER_NAME not in servers and servers == {"other": {"command": "x"}}, "观察开关不能绕过属主范围"
    assert glue.observation_tools_enabled({OBSERVATION_ENV_FLAG: "1"}) and not glue.observation_tools_enabled({OBSERVATION_ENV_FLAG: "true"})


# 类用途: 假观察核心：记录调用，按预设返回或抛结构化错误。
class _Observer:
    def __init__(self):
        self.calls, self.error = [], None
        self.result = {"window": "win:b:1", "generation": "b-1-1", "candidate_count": 1,
                       "my_agent_observation": {"schema": "plugin_observation.v1", "target": {"ref": "win:b:1", "generation": "b-1-1"}, "candidates": []}}

    def observe(self, window=None):
        self.calls.append(("observe", window))
        if self.error:
            raise self.error
        return dict(self.result)

    def click_candidate(self, meta, *, cancelled=None):
        self.received_cancelled = cancelled  # 记下接管层传下来的取消判定，用例断言它就是 CallContext.cancelled
        if cancelled is not None and cancelled():
            raise ObservationError("cancelled", "宿主已取消，未点击")
        self.calls.append(("click", meta))
        if self.error:
            raise self.error
        return {"clicked": {"key": meta["key"]}, "point": [1, 2]}


def test_call_observation_tool_keeps_the_payload_out_of_the_body_and_encodes_failures():
    observer = _Observer()
    body, structured, is_error = glue.call_observation_tool(observer, "observe_window", {"window": ""}, None)
    assert not is_error and "my_agent_observation" not in body and structured["my_agent_observation"]["target"]["ref"] == "win:b:1"
    assert observer.calls == [("observe", None)]
    body, structured, is_error = glue.call_observation_tool(observer, "observe_window", {"window": 5}, None)
    assert is_error and body["code"] == "OBSERVATION_INVALID_ARGUMENTS" and structured == {"my_agent_observation_error": {"code": "invalid_arguments"}}
    meta = {"version": "1", "key": "t1", "target": {"ref": "win:b:1", "generation": "b-1-1"}}
    context = glue.CallContext(meta=meta)
    body, structured, is_error = glue.call_observation_tool(observer, "click_candidate", {"candidate_id": "cand-0123456789abcdef"}, context)
    assert not is_error and body == structured == {"clicked": {"key": "t1"}, "point": [1, 2]}
    assert glue.call_observation_tool(observer, "click_candidate", {}, context)[2] is True, "缺 candidate_id 不执行"
    observer.error = ObservationError("stale", "候选区域的像素已变")
    body, structured, is_error = glue.call_observation_tool(observer, "click_candidate", {"candidate_id": "x"}, context)
    assert (is_error, body["code"], structured) == (True, "OBSERVATION_STALE", {"my_agent_observation_error": {"code": "stale"}})
    observer.error = ObservationError("not_found", "没有这一代")
    assert glue.call_observation_tool(observer, "click_candidate", {"candidate_id": "x"}, context)[1] == {"my_agent_observation_error": {"code": "not_found"}}
    observer.error = None
    assert observer.received_cancelled is context.cancelled, "接管层把 CallContext.cancelled 原样传给核心的 click_candidate"
    gone = glue.CallContext(meta=meta, cancelled=lambda: True)
    before = len(observer.calls)
    body, structured, is_error = glue.call_observation_tool(observer, "click_candidate", {"candidate_id": "x"}, gone)
    assert (is_error, structured) == (True, {"my_agent_observation_error": {"code": "cancelled"}})
    assert len(observer.calls) == before, "已取消的点击不碰 observer"
    assert glue.call_observation_tool(observer, "observe_window", {}, gone)[1] == {"my_agent_observation_error": {"code": "cancelled"}}
    assert len(observer.calls) == before, "已取消的观察也不碰 observer"


# 函数用途: 假的 mcp.types：只造 ServerResult / CallToolResult / TextContent / CallToolRequest 四个形状。
def _fake_types():
    return SimpleNamespace(
        ServerResult=lambda root: SimpleNamespace(root=root),
        CallToolResult=lambda **kw: SimpleNamespace(**kw),
        TextContent=lambda **kw: SimpleNamespace(**kw),
        CallToolRequest=type("CallToolRequest", (), {}),
    )


def test_lowlevel_handler_intercepts_only_observation_tools_and_reads_host_meta(monkeypatch):
    types = _fake_types()
    monkeypatch.setitem(sys.modules, "mcp", SimpleNamespace(types=types))
    monkeypatch.setitem(sys.modules, "mcp.types", types)
    observer, delegated = _Observer(), []

    async def delegate(request):
        delegated.append(request.params.name)
        return "delegated"

    low = SimpleNamespace(request_handlers={types.CallToolRequest: delegate})
    glue.install_observation_handler(low, observer)
    handler = low.request_handlers[types.CallToolRequest]
    assert handler is not delegate
    meta = SimpleNamespace(model_extra={OBSERVATION_META_EXTENSION: {"version": "1", "key": "t1", "target": {"ref": "win:b:1", "generation": "b-1-1"}}})
    plain = SimpleNamespace(params=SimpleNamespace(name="list_windows", arguments={}, meta=None))
    assert asyncio.run(handler(plain)) == "delegated" and delegated == ["list_windows"], "其它工具原样交给 FastMCP"
    clicked = asyncio.run(handler(SimpleNamespace(params=SimpleNamespace(name="click_candidate", arguments={"candidate_id": "cand-x"}, meta=meta))))
    assert clicked.root.isError is False and clicked.root.structuredContent == {"clicked": {"key": "t1"}, "point": [1, 2]}
    assert json.loads(clicked.root.content[0].text) == {"clicked": {"key": "t1"}, "point": [1, 2]}
    assert observer.calls[-1] == ("click", {"version": "1", "key": "t1", "target": {"ref": "win:b:1", "generation": "b-1-1"}})
    observed = asyncio.run(handler(SimpleNamespace(params=SimpleNamespace(name="observe_window", arguments=None, meta=None))))
    assert observed.root.isError is False and "my_agent_observation" in observed.root.structuredContent
    assert "my_agent_observation" not in json.loads(observed.root.content[0].text), "观察载荷只进 structuredContent"
    observer.error = ObservationError("not_found", "没有")
    failed = asyncio.run(handler(SimpleNamespace(params=SimpleNamespace(name="click_candidate", arguments={"candidate_id": "cand-x"}, meta=None))))
    assert failed.root.isError is True and failed.root.structuredContent == {"my_agent_observation_error": {"code": "not_found"}}


def test_owner_scope_direct_call_by_name_is_rejected_when_the_tool_is_not_in_the_snapshot(tmp_path):
    from agent_py_agent.agent.tooling.action_policy import ActionPolicy, ActionPolicyRequest
    from agent_py_agent.tests._tool_runtime_harness import (
        canonical_test_call,
        runtime_snapshot_for_tools,
    )

    snapshot = runtime_snapshot_for_tools({})  # 非 local/main owner 的目录里没有 Computer Use 服务，自然也没有观察工具
    for name in ("mcp__computer_use__observe_window", "mcp__computer_use__click_candidate"):
        decision = ActionPolicy().decide(ActionPolicyRequest(canonical_test_call(snapshot, name, {}), snapshot, tmp_path, approval_mode="auto"))
        assert decision.status == "deny" and "TOOL_NOT_IN_RUNTIME_SNAPSHOT" in decision.reason_codes


# 类用途: 假的底层 Server：记录 list_tools / call_tool 装饰器绑定的协程，处理器放在公开的 request_handlers 字典里。
class _LowServer:
    def __init__(self, name):
        self.name, self.bound, self.request_handlers = name, {}, {}

    def list_tools(self):
        def decorator(fn):
            self.bound["list_tools"] = fn
            return fn
        return decorator

    def call_tool(self, *, validate_input=True):
        def decorator(fn):
            self.bound["call_tool"] = fn
            self.request_handlers[sys.modules["mcp.types"].CallToolRequest] = ("delegate", fn)
            return fn
        return decorator


# 类用途: 假 FastMCP：固定名字、上游工具目录、add_tool 记录。
class _FastMCP:
    def __init__(self):
        self.name, self.tools, self.added = "my-agent Computer Use", [{"name": "list_windows", "inputSchema": {}}], []

    async def list_tools(self):
        return list(self.tools) + [{"name": name, "inputSchema": {}} for _, name, _ in self.added]

    async def call_tool(self, name, arguments):
        return {"called": name}

    def add_tool(self, fn, name, description):
        self.added.append((fn, name, description))


@pytest.mark.parametrize("flag,expect_tools", [({}, 1), ({OBSERVATION_ENV_FLAG: "0"}, 1), ({OBSERVATION_ENV_FLAG: "1"}, 3)])
def test_build_adapter_server_registers_observation_tools_only_when_the_flag_is_on(monkeypatch, flag, expect_tools):
    types = _fake_types()
    monkeypatch.setitem(sys.modules, "mcp", SimpleNamespace(types=types))
    monkeypatch.setitem(sys.modules, "mcp.types", types)
    monkeypatch.setitem(sys.modules, "mcp.server", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "mcp.server.lowlevel", SimpleNamespace(Server=_LowServer))
    fastmcp, factory_calls = _FastMCP(), []
    low = glue.build_adapter_server(fastmcp, flag, lambda: factory_calls.append(1) or _Observer())
    assert low.name == fastmcp.name and low.bound["list_tools"] == fastmcp.list_tools and low.bound["call_tool"] == fastmcp.call_tool
    listed = asyncio.run(low.bound["list_tools"]())
    assert len(listed) == expect_tools and [t["name"] for t in listed][:1] == ["list_windows"]
    handler = low.request_handlers[types.CallToolRequest]
    if expect_tools == 1:
        assert handler == ("delegate", fastmcp.call_tool) and fastmcp.added == [] and factory_calls == [], "关时目录与处理器都不变，不碰 X11"
    else:
        assert [name for _, name, _ in fastmcp.added] == ["observe_window", "click_candidate"] and factory_calls == [1]
        assert callable(handler) and not isinstance(handler, tuple), "开时接管层已装上"


def test_declaration_functions_only_describe_and_register_the_tools():
    registered = []
    glue.register_observation_tools(SimpleNamespace(add_tool=lambda fn, name, description: registered.append((fn, name, description))))
    assert [(name, fn.__name__) for fn, name, _ in registered] == [("observe_window", "observe_window"), ("click_candidate", "click_candidate")]
    assert all("candidate" in description for _, _, description in registered)
    for fn, _, _ in registered:
        with pytest.raises(RuntimeError):
            fn()


# ---------------------------------------------------------------------------
# X11 后端：假 Xlib / mss / OCR
# ---------------------------------------------------------------------------

# 类用途: 假 X 窗口：属性、几何、根坐标、叠放用的字段；rect 是 (x, y, w, h)，其余用关键字（viewable / props / children / override / name）。
class _Win:
    def __init__(self, xid, rect, **options):
        self.id, self._geo, self._viewable, self._props = xid, rect, options.get("viewable", True), options.get("props") or {}
        self._children, self._override = list(options.get("children", ())), options.get("override", False)
        self._name = options.get("name", "表单".encode())

    def get_attributes(self):
        return SimpleNamespace(map_state=2 if self._viewable else 0, override_redirect=self._override)

    def get_geometry(self):
        return SimpleNamespace(x=0, y=0, width=self._geo[2], height=self._geo[3])

    def get_full_property(self, atom, kind):
        value = self._props.get(atom)
        return SimpleNamespace(value=value) if value is not None else None

    def get_wm_name(self):
        return self._name

    def query_tree(self):
        return SimpleNamespace(children=self._children)

    def translate_coords(self, other, x, y):
        return SimpleNamespace(x=other._geo[0] + x, y=other._geo[1] + y)


# 类用途: 假 Display：按名字 intern 原子、按 id 找窗口。
class _Display:
    def __init__(self, root, windows):
        self._root, self._windows, self._atoms = root, windows, {}

    def intern_atom(self, name):
        return self._atoms.setdefault(name, "atom:" + name)

    def screen(self):
        return SimpleNamespace(root=self._root)

    def create_resource_object(self, kind, xid):
        return self._windows.get(xid) or _GoneWin(xid)


# 类用途: 已消失的窗口：真 Xlib 里 create_resource_object 不会报错，之后任何请求才抛 XError。
class _GoneWin:
    def __init__(self, xid):
        self.id = xid

    def _gone(self, *args, **kwargs):
        from Xlib.error import XError
        raise XError()

    get_attributes = get_geometry = get_full_property = get_wm_name = query_tree = translate_coords = _gone


@pytest.fixture
def fake_xlib(monkeypatch):
    class XError(Exception):
        pass
    monkeypatch.setitem(sys.modules, "Xlib", SimpleNamespace(X=SimpleNamespace(IsViewable=2, AnyPropertyType=0), error=SimpleNamespace(XError=XError)))
    monkeypatch.setitem(sys.modules, "Xlib.X", sys.modules["Xlib"].X)
    monkeypatch.setitem(sys.modules, "Xlib.error", sys.modules["Xlib"].error)
    monkeypatch.setitem(sys.modules, "Xlib.display", SimpleNamespace(Display=lambda: (_ for _ in ()).throw(RuntimeError("no DISPLAY"))))
    return XError


def test_x11_backend_lists_stacking_order_with_states_and_computes_above_rects(fake_xlib):
    from agent_py_agent.agent.tooling.computer_use_x11 import X11Backend

    form = _Win(0x10, (100, 50, 320, 200), props={"atom:_NET_WM_DESKTOP": [0], "atom:_NET_FRAME_EXTENTS": [2, 2, 20, 2]})
    menu = _Win(0x20, (150, 80, 50, 30), override=True)
    dock = _Win(0x30, (0, 0, 1024, 24), props={"atom:_NET_WM_WINDOW_TYPE": ["atom:_NET_WM_WINDOW_TYPE_DOCK"], "atom:_NET_WM_DESKTOP": [0xFFFFFFFF]})
    hidden = _Win(0x40, (5, 5, 10, 10), props={"atom:_NET_WM_STATE": ["atom:_NET_WM_STATE_HIDDEN"], "atom:_NET_WM_NAME": list("隐藏".encode())})
    root = _Win(0x1, (0, 0, 1024, 768), props={"atom:_NET_CLIENT_LIST_STACKING": [0x30, 0x10, 0x40, 0x99], "atom:_NET_CURRENT_DESKTOP": [0]},
                children=[form, menu, dock, hidden])
    backend = X11Backend(_Display(root, {0x1: root, 0x10: form, 0x20: menu, 0x30: dock, 0x40: hidden}))
    windows = backend.list_windows()
    assert [w.native_id for w in windows] == [0x30, 0x10, 0x40], "消失的 0x99 跳过，顺序按叠放底→顶"
    form_info = windows[1]
    assert (form_info.geometry.origin, form_info.geometry.size, form_info.geometry.scale) == ((100, 50), (320, 200), (1, 1))
    assert form_info.title == "表单" and form_info.normal and form_info.desktop == 0 and form_info.current_desktop == 0 and not form_info.hidden
    assert windows[0].normal is False and windows[0].desktop is None and windows[2].hidden and windows[2].title == "隐藏"
    rects = backend.above_rects(0x10)
    assert (150, 80, 50, 30) in rects, "override-redirect 的菜单算上层"
    assert (98, 30, 324, 224) not in rects and (3, 3, 10, 10) not in rects, "自己和隐藏窗口不算"
    assert backend.above_rects(0x30) == [(98, 30, 324, 222), (150, 80, 50, 30)], "排在 dock 上面的表单带 _NET_FRAME_EXTENTS 边框（左右各 2、上 20、下 2）"


def test_x11_backend_capture_ocr_and_click_use_the_public_library_calls(monkeypatch, fake_xlib):
    from agent_py_agent.agent.tooling.computer_use_x11 import X11Backend, X11Libraries
    from agent_py_agent.agent.tooling.screen_observation import WindowInfo
    from agent_py_agent.agent.tooling.screen_observation_store import WindowGeometry

    grabs, clicks = [], []

    class _Grabber:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def grab(self, monitor):
            grabs.append(monitor)
            return SimpleNamespace(width=monitor["width"], height=monitor["height"], rgb=bytes(monitor["width"] * monitor["height"] * 3))

    monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace(uint8="u8", frombuffer=lambda data, dtype: SimpleNamespace(reshape=lambda h, w, c: _Arr()),
                                                              ascontiguousarray=lambda a: a))
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", SimpleNamespace(RapidOCR=lambda: (lambda image: ([[[[10, 20], [70, 20], [70, 38], [10, 38]], "提交", 0.9]], 0.1))))

    class _Arr:
        def __getitem__(self, item):
            return self

    libraries = X11Libraries(open_display=lambda: None, grabber=_Grabber, click=lambda x, y: clicks.append((x, y)))
    backend = X11Backend(_Display(_Win(0x1, (0, 0, 10, 10)), {}), libraries)
    info = WindowInfo(native_id=0x10, title="t", geometry=WindowGeometry((100, 50), (320, 200), (1, 1)), viewable=True, hidden=False, desktop=0, current_desktop=0)
    capture = backend.capture(info)
    buffer = capture.buffer
    assert grabs == [{"left": 100, "top": 50, "width": 320, "height": 200}] and (buffer.width, buffer.height) == (320, 200)
    assert (capture.kind, capture.fallback_reason) == ("screen_region", None), "mss 按区域截屏，如实报 screen_region"
    regions = backend.ocr(buffer)
    assert [(r.text, r.region) for r in regions] == [("提交", (10, 20, 60, 18))]
    backend.ensure_click_permitted()
    backend.click(150, 89)
    assert clicks == [(150, 89)], "X11 注入点击不需要额外授权"


def test_lowlevel_handler_runs_observations_off_the_event_loop_one_at_a_time(monkeypatch):
    import threading
    import time

    types = _fake_types()
    monkeypatch.setitem(sys.modules, "mcp", SimpleNamespace(types=types))
    monkeypatch.setitem(sys.modules, "mcp.types", types)
    depth = {"now": 0, "max": 0, "thread_names": set()}

    class _SlowObserver(_Observer):
        def observe(self, window=None):
            depth["now"] += 1
            depth["max"] = max(depth["max"], depth["now"])
            depth["thread_names"].add(threading.current_thread().name)
            time.sleep(0.05)
            depth["now"] -= 1
            return super().observe(window)

    async def delegate(request):
        return "delegated"

    handler = glue.observation_call_handler(delegate, _SlowObserver())
    request = SimpleNamespace(params=SimpleNamespace(name="observe_window", arguments={}, meta=None))

    async def two_at_once():
        return await asyncio.gather(handler(request), handler(request))

    results = asyncio.run(two_at_once())
    assert all(r.root.isError is False for r in results) and depth["max"] == 1, "两次观察串行，不并发写快照环"
    assert "MainThread" not in depth["thread_names"], "阻塞的采样不在事件循环线程上跑，取消通知才能被处理"


def test_cancelled_click_queued_behind_a_cancelled_slow_observation_never_clicks(monkeypatch):
    import time

    types = _fake_types()
    monkeypatch.setitem(sys.modules, "mcp", SimpleNamespace(types=types))
    monkeypatch.setitem(sys.modules, "mcp.types", types)

    class _Slow(_Observer):
        def observe(self, window=None):
            time.sleep(0.3)
            return super().observe(window)

    observer = _Slow()

    async def delegate(request):
        return "delegated"

    handler = glue.observation_call_handler(delegate, observer)
    observe = SimpleNamespace(params=SimpleNamespace(name="observe_window", arguments={}, meta=None))
    click_meta = SimpleNamespace(model_extra={OBSERVATION_META_EXTENSION: {"version": "1", "key": "t1", "target": {"ref": "win:b:1", "generation": "b-1-1"}}})
    click = SimpleNamespace(params=SimpleNamespace(name="click_candidate", arguments={"candidate_id": "cand-x"}, meta=click_meta))

    async def scenario():
        slow = asyncio.ensure_future(handler(observe))
        await asyncio.sleep(0.05)
        slow.cancel()  # 宿主 /stop：观察被丢弃，线程里的 OCR 还在跑、锁还占着
        queued = asyncio.ensure_future(handler(click))
        await asyncio.sleep(0.05)
        queued.cancel()  # 宿主也取消了排队中的点击
        await asyncio.sleep(0.6)  # 等锁放开、排队线程醒来

    asyncio.run(scenario())
    assert [call for call in observer.calls if call[0] == "click"] == [], "用户按了停止之后屏幕上不会多点一下"
    assert observer.calls == [("observe", None)], observer.calls


def test_cancellation_during_recheck_reaches_the_real_observer_through_the_glue():
    from agent_py_agent.tests.test_screen_observation_core import _meta, _observer

    backend, observer = _observer()
    meta = _meta(observer.observe())
    flag = {"cancelled": False}
    original_capture = backend.capture

    def capture_then_cancel(info):  # 复核重采样期间宿主取消了：只有接管层把 cancelled 传下去，核心才看得见
        flag["cancelled"] = True
        return original_capture(info)

    backend.capture = capture_then_cancel
    context = glue.CallContext(meta=meta, cancelled=lambda: flag["cancelled"])
    body, structured, is_error = glue.call_observation_tool(observer, "click_candidate", {"candidate_id": "cand-x"}, context)
    assert (is_error, structured, body["code"]) == (True, {"my_agent_observation_error": {"code": "cancelled"}}, "OBSERVATION_CANCELLED")
    assert backend.clicks == [], "复核期间被取消，真正点击前拦住"
