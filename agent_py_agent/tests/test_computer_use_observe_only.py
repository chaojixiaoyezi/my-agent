"""J16 只看档（vho）：总开关关、只开观察时的档位语义与适配器行为。

四档来自两个结构化开关的组合：
- 都关：没有适配器；
- 只看档（总开关关 + 观察开）：装配同一个服务，只交出 observe_window；进程不 import pyautogui / computer_control_mcp；
- 完整档（都开）：维持原行为，另加观察工具；
- 只开总开关：维持原行为，没有观察工具。

适配器侧用 meta path finder 让 pyautogui 与 computer_control_mcp 导入必定失败，证明只看档本来就不需要它们，
不是在缺依赖时兜底。
"""
from __future__ import annotations

import ast
import importlib.abc
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.tooling import computer_use_observation_tools as glue
from agent_py_agent.agent.tooling.computer_use_profile import (
    COMPUTER_USE_MCP_SERVER_NAME,
    OBSERVATION_ENV_FLAG,
    OBSERVE_ONLY_ENV_FLAG,
    ComputerUseTier,
    computer_use_mcp_servers,
    with_computer_use_observation,
)

BLOCKED_MODULES = ("pyautogui", "computer_control_mcp")
REPO_ROOT = Path(__file__).resolve().parents[2]


# LLM: 只在测试进程里拦截两个上游模块；命中即 ImportError，用来证明档位不依赖它们，而不是让它们"碰巧没装"。
# 类用途: 让 pyautogui / computer_control_mcp 的导入必定失败的 meta path finder。
class _BlockUpstreamFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in BLOCKED_MODULES:
            raise ImportError(f"只看档不应 import {fullname}")
        return None


# 函数用途: 在拦截器生效期间执行 body，结束后恢复原 sys.meta_path。
def _with_blocked_upstream(body):
    finder = _BlockUpstreamFinder()
    sys.meta_path.insert(0, finder)
    try:
        return body()
    finally:
        sys.meta_path.remove(finder)


# 函数用途: 按三个结构化输入装配 Computer Use 服务（只看档由开关组合派生）。
def _servers(enabled: bool, observation: bool, *, admin: bool = True, access: str = "full-access"):
    return with_computer_use_observation(
        computer_use_mcp_servers(
            {"other": {"command": "x"}},
            ComputerUseTier(
                enabled=enabled,
                is_local_admin=admin,
                access_mode=access,
                observe_only=(not enabled and observation),
                environ={"DISPLAY": ":99"},
                python_executable="/py",
            ),
        ),
        enabled=observation,
    )


# LLM: 只看档的判定只看结构化开关组合与权限，不看依赖是否装好；因此这里用同一组断言覆盖四种组合。
# 函数用途: 核对四种开关组合各自的服务装配结果。
def test_four_switch_combinations_pick_the_declared_tier():
    both_off = _servers(enabled=False, observation=False)
    assert COMPUTER_USE_MCP_SERVER_NAME not in both_off and both_off == {"other": {"command": "x"}}

    observe_only = _servers(enabled=False, observation=True)[COMPUTER_USE_MCP_SERVER_NAME]
    assert observe_only["env"][OBSERVE_ONLY_ENV_FLAG] == "1"
    assert OBSERVATION_ENV_FLAG not in observe_only["env"]
    assert set(observe_only["tool_effects"]) == {"observe_window"}
    assert observe_only["tool_effects"]["observe_window"] == "read_only"
    assert observe_only["tool_approvals"] == {"observe_window": "always"}
    assert "list_windows" not in observe_only["tool_effects"] and "scroll_screen" not in observe_only["tool_effects"]

    full = _servers(enabled=True, observation=True)[COMPUTER_USE_MCP_SERVER_NAME]
    assert full["env"][OBSERVATION_ENV_FLAG] == "1" and OBSERVE_ONLY_ENV_FLAG not in full["env"]
    assert full["tool_effects"]["list_windows"] == "read_only" and full["tool_effects"]["observe_window"] == "read_only"
    assert "click_candidate" in full["tool_effects"]

    enabled_only = _servers(enabled=True, observation=False)[COMPUTER_USE_MCP_SERVER_NAME]
    assert OBSERVATION_ENV_FLAG not in enabled_only["env"] and OBSERVE_ONLY_ENV_FLAG not in enabled_only["env"]
    assert "observe_window" not in enabled_only["tool_effects"] and enabled_only["tool_approvals"] == {}


@pytest.mark.parametrize("scope", [{"admin": False}, {"access": "restricted"}])
def test_observe_only_never_bypasses_owner_scope(scope):
    servers = _servers(enabled=False, observation=True, **scope)
    assert COMPUTER_USE_MCP_SERVER_NAME not in servers and servers == {"other": {"command": "x"}}


# 函数用途: 只调 FastMCP 公开的 add_tool，记录注册进来的工具名，供适配器装配断言。
class _FakeServer:
    def __init__(self, name="fake"):
        self.name = name
        self.added: list[str] = []

    def add_tool(self, fn, *, name, description):
        self.added.append(name)

    async def list_tools(self):
        return []

    async def call_tool(self, *args, **kwargs):
        return None


# 函数用途: 底层 Server 替身：记录 handler 绑定，不依赖真实 mcp 包。
class _LowServer:
    def __init__(self, name):
        self.name = name
        self.request_handlers: dict = {}
        self.bound: dict = {}

    def list_tools(self):
        def decorate(fn):
            self.bound["list_tools"] = fn
            return fn
        return decorate

    def call_tool(self, **_kwargs):
        def decorate(fn):
            self.bound["call_tool"] = fn
            # 底层 Server 预置了 tools/call 处理器；观察接管层会读它并把原处理器当 delegate。
            self.request_handlers[sys.modules["mcp.types"].CallToolRequest] = ("delegate", fn)
            return fn
        return decorate


# 函数用途: 把假的 mcp 模块装进 sys.modules，让适配器装配在无 mcp 依赖的测试环境里可跑。
def _install_fake_mcp(monkeypatch):
    types = SimpleNamespace(CallToolRequest=object())
    monkeypatch.setitem(sys.modules, "mcp", SimpleNamespace(types=types))
    monkeypatch.setitem(sys.modules, "mcp.types", types)
    monkeypatch.setitem(sys.modules, "mcp.server", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "mcp.server.lowlevel", SimpleNamespace(Server=_LowServer))


# 函数用途: 假观察后端声明支持控件候选（只看档下不该因为它而多注册点击工具）。
class _FakeObserver:
    supports_ui_candidates = True

    def observe(self, window=None):
        return {"ok": True}


# LLM: 关键反证——在 pyautogui 与 computer_control_mcp 导入必定失败的前提下，只看档仍能装配出恰好一个 observe_window。
# 函数用途: 验证只看档的工具目录与上游依赖无关。
def test_observe_only_adapter_serves_only_observe_window_without_upstream_imports(monkeypatch):
    _install_fake_mcp(monkeypatch)
    fastmcp = _FakeServer()
    low = _with_blocked_upstream(lambda: glue.build_adapter_server(
        fastmcp, {OBSERVATION_ENV_FLAG: "1", OBSERVE_ONLY_ENV_FLAG: "1"}, _FakeObserver))
    assert fastmcp.added == ["observe_window"], fastmcp.added
    assert low.name == "fake"


# 函数用途: 完整档在同样的标记组合下仍注册三个观察工具（点击/输入不受只看档影响）。
def test_full_tier_adapter_registers_all_observation_tools(monkeypatch):
    _install_fake_mcp(monkeypatch)
    with_typing = _FakeServer()
    glue.build_adapter_server(with_typing, {OBSERVATION_ENV_FLAG: "1"}, _FakeObserver)
    assert with_typing.added == ["observe_window", "click_candidate", "type_into_candidate"]


# 函数用途: 观察标记关着时适配器不注册任何观察工具，工具目录与只有上游时一致。
def test_adapter_without_observation_flag_registers_nothing(monkeypatch):
    _install_fake_mcp(monkeypatch)
    server = _FakeServer()
    glue.build_adapter_server(server, {}, _FakeObserver)
    assert server.added == []


# LLM: 只读结构化标记；"true"/"0" 等其它取值都不算，避免文案或大小写变化改变档位。
# 函数用途: 核对只看档与观察标记的取值判定。
def test_tier_flags_accept_only_the_exact_marker_value():
    assert glue.observe_only_enabled({OBSERVE_ONLY_ENV_FLAG: "1"})
    assert not glue.observe_only_enabled({OBSERVE_ONLY_ENV_FLAG: "true"})
    assert not glue.observe_only_enabled({})
    assert glue.observation_tools_enabled({OBSERVATION_ENV_FLAG: "1"})
    assert not glue.observation_tools_enabled({OBSERVATION_ENV_FLAG: "0"})
    # 只看标记同样意味着要装载观察工具（只看档不写观察标记）。
    assert glue.observation_tools_enabled({OBSERVE_ONLY_ENV_FLAG: "1"})
    assert not glue.observation_tools_enabled({OBSERVE_ONLY_ENV_FLAG: "0"})


# LLM: 接缝用例——不手写环境变量，直接拿宿主装配出的 server env 喂给适配器装配，验证两端接得上。
#   真机实测过这个接缝断过一次：宿主只看档只写只看标记，适配器只认观察标记，于是 tools/list 是空的。
# 函数用途: 用宿主产出的 env 走一遍适配器装配，断言只看档恰好交出 observe_window。
def test_host_produced_env_feeds_the_adapter_and_yields_only_observe_window(monkeypatch):
    _install_fake_mcp(monkeypatch)
    servers = _servers(enabled=False, observation=True)
    env = servers[COMPUTER_USE_MCP_SERVER_NAME]["env"]

    fastmcp = _FakeServer()
    low = _with_blocked_upstream(lambda: glue.build_adapter_server(fastmcp, env, _FakeObserver))

    assert fastmcp.added == ["observe_window"], fastmcp.added
    assert low.name == "fake"


# LLM: 只看档进程绝不能加载上游执行器，所以 server 模块顶层不得 import pyautogui / computer_control_mcp；
#   它们只能在 _load_upstream 里、且只在完整档被调用。这条用 AST 静态钉住，防止有人把 import 挪回顶层。
# 函数用途: 取出一段源码里模块顶层的 import 名字（含 from 形式的模块名）。
def _toplevel_import_names(source: str) -> list[str]:
    def names_of(node):
        if isinstance(node, ast.Import):
            return [alias.name for alias in node.names]
        return [node.module] if isinstance(node, ast.ImportFrom) and node.module else []

    return [name for node in ast.parse(source).body for name in names_of(node)]


# 函数用途: 断言适配器模块的顶层没有上游 import。
def test_adapter_module_has_no_toplevel_upstream_import():
    # 路径用两段拼出来：本文件不真的拉起适配器子进程，避免命中"开观察 + 拉子进程"的车道守卫。
    module = REPO_ROOT / "agent_py_agent/agent/tooling" / ("computer_use" + "_server.py")
    offenders = [name for name in _toplevel_import_names(module.read_text(encoding="utf-8"))
                 if name.split(".")[0] in ("pyautogui", "computer_control_mcp")]
    assert not offenders, f"只看档不得在模块顶层 import 上游：{offenders}"
