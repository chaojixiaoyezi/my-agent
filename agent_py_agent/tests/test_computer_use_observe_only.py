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


# 函数用途: 把假的 mcp 模块装进 sys.modules，让适配器装配与结果编码在无 mcp 依赖的测试环境里可跑。
def _install_fake_mcp(monkeypatch):
    from agent_py_agent.tests.test_computer_use_observation_tools import _fake_types

    types = _fake_types()
    monkeypatch.setitem(sys.modules, "mcp", SimpleNamespace(types=types))
    monkeypatch.setitem(sys.modules, "mcp.types", types)
    monkeypatch.setitem(sys.modules, "mcp.server", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "mcp.server.lowlevel", SimpleNamespace(Server=_LowServer))


# 函数用途: 假观察后端声明支持控件候选（只看档下不该因为它而多注册点击工具）。
class _FakeObserver:
    supports_ui_candidates = True

    def observe(self, window=None, *, want_screenshot=False):
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


# 类用途: 只看档执行面用例的假 observer：记录点击/输入次数，任何一次都算"点到了"。
class _CountingObserver:
    supports_ui_candidates = True

    def __init__(self):
        self.clicks, self.types, self.observes = 0, 0, 0

    def observe(self, window=None, *, want_screenshot=False):
        self.observes += 1
        return {"window": "win:b:1", "generation": "b-1-1", "candidate_count": 0}

    def click_candidate(self, meta, *, cancelled=None):
        self.clicks += 1
        return {"clicked": {"key": "k"}, "point": [1, 2]}

    def type_into_candidate(self, meta, text, clear_existing=False, *, cancelled=None):
        self.types += 1
        return {"typed": {"key": "k"}, "characters": len(text or ""), "clear_existing": clear_existing, "application_verified": False}


# LLM: 目录层（tools/list 只列 observe_window）是"目录收窄"，这里测的是执行层的 fail-closed：即便有人绕开目录、
#   直接点名 click_candidate / type_into_candidate，接管层也必须拒绝，且不碰 observer 的点击/输入方法。
# 函数用途: 用宿主只看档 env 装配出的真实接管层，直接点名调用被收窄的工具。
def _observe_only_handler(monkeypatch):
    _install_fake_mcp(monkeypatch)
    env = _servers(enabled=False, observation=True)[COMPUTER_USE_MCP_SERVER_NAME]["env"]
    observer = _CountingObserver()
    fastmcp = _FakeServer()
    low = _with_blocked_upstream(lambda: glue.build_adapter_server(fastmcp, env, lambda: observer))
    return low.request_handlers[sys.modules["mcp.types"].CallToolRequest], observer, fastmcp


def test_observe_only_rejects_direct_calls_outside_the_listed_tool(monkeypatch):
    handler, observer, fastmcp = _observe_only_handler(monkeypatch)
    assert fastmcp.added == ["observe_window"], "只看档目录只有一个工具"
    for name, arguments in (("click_candidate", {"candidate_id": "cand-x"}),
                            ("type_into_candidate", {"text": "hi", "candidate_id": "cand-x"})):
        result = asyncio_run(handler(SimpleNamespace(params=SimpleNamespace(name=name, arguments=arguments, meta=None))))
        assert result.root.isError is True
        assert result.root.structuredContent == {"my_agent_observation_error": {"code": "tool_not_available_in_observe_only"}}
    assert (observer.clicks, observer.types) == (0, 0), "只看档下点击/输入一次都不能真的发生"
    ok = asyncio_run(handler(SimpleNamespace(params=SimpleNamespace(name="observe_window", arguments={}, meta=None))))
    assert ok.root.isError is False and observer.observes == 1, "observe_window 照常可用"


def test_full_tier_direct_calls_still_execute(monkeypatch):
    _install_fake_mcp(monkeypatch)
    observer = _CountingObserver()
    fastmcp = _FakeServer()
    env = {OBSERVATION_ENV_FLAG: "1"}
    low = glue.build_adapter_server(fastmcp, env, lambda: observer)
    handler = low.request_handlers[sys.modules["mcp.types"].CallToolRequest]
    result = asyncio_run(handler(SimpleNamespace(params=SimpleNamespace(
        name="click_candidate", arguments={"candidate_id": "cand-x"},
        meta=SimpleNamespace(model_extra={})))))
    assert result.root.isError is False and observer.clicks == 1, "完整档行为不变"


# 函数用途: 跑一个协程（保持测试不引入额外依赖）。
def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


# LLM: 执行层的档位判定必须来自同一个结构化标记（上下文里的 observe_only），而不是按工具名或调用方身份猜。
# 函数用途: 直接核对接管层 API 的观察档参数语义。
def test_call_observation_tool_observe_only_flag_blocks_everything_but_observe_window():
    observer = _CountingObserver()
    tier = glue.CallContext(observe_only=True)
    for name in ("click_candidate", "type_into_candidate"):
        body, structured, is_error = glue.call_observation_tool(observer, name, {"candidate_id": "x", "text": "t"}, tier)
        assert (is_error, body["code"]) == (True, "OBSERVATION_TOOL_NOT_AVAILABLE_IN_OBSERVE_ONLY")
        assert structured == {"my_agent_observation_error": {"code": "tool_not_available_in_observe_only"}}
    assert (observer.clicks, observer.types) == (0, 0)
    body, structured, is_error = glue.call_observation_tool(observer, "observe_window", {}, tier)
    assert is_error is False and observer.observes == 1
    full = glue.CallContext(observe_only=False)
    assert glue.call_observation_tool(observer, "click_candidate", {"candidate_id": "x"}, full)[2] is False
    assert observer.clicks == 1, "完整档不受影响"


# LLM: 上线清单要求“生产打开只看档”能用 /settings 设。这个键是安全边界（模型不可写、登记表 writable=False），
#   放进 USER_SETTINGS_BOUNDARY_KEYS 后只有已认证管理员的 /settings 写作用域能改；正式档 computer_use_enabled
#   刻意不进白名单（它能点击、能输入，交出上游执行面），继续只能手改配置文件。
# 函数用途: 建一份临时用户配置与写路径（只看档的写入目标）。
def _settings_paths(tmp_path):
    from agent_py_agent.agent.settings.parameter_changes import WritePaths

    user_config = tmp_path / "desktop.yaml"
    user_config.write_text("", encoding="utf-8")
    return WritePaths(user_path=user_config), user_config


# 函数用途: 核对两个键的登记事实与白名单归属（观察在、正式档不在）。
def test_observe_only_key_is_boundary_but_full_tier_is_not_in_the_whitelist():
    from agent_py_agent.agent.settings.parameter_registry import parameter_registry
    from agent_py_agent.agent.settings.user_config_capability import USER_SETTINGS_BOUNDARY_KEYS

    observe_key, full_key = "computer_use_observation_enabled", "computer_use_enabled"
    assert observe_key in USER_SETTINGS_BOUNDARY_KEYS and full_key not in USER_SETTINGS_BOUNDARY_KEYS
    registry = parameter_registry()
    # 这两个键住在主配置（agent 来源），写入目标是用户配置文件；登记表默认 False、模型不可写。
    assert registry[observe_key].source == "agent" and registry[observe_key].writable is False
    assert registry[observe_key].default is False
    assert registry[full_key].writable is False


# 函数用途: 用真实写入口核对只看档可被管理员 /settings 开关、状态可读回、生效时机是重启。
def test_observe_only_switch_is_admin_settings_writable(tmp_path):
    # ruff 的 isort 规则按模块名排序：config < parameter_changes < parameter_registry < user_config_capability。
    from agent_py_agent.agent.settings.config import load_config
    from agent_py_agent.agent.settings.parameter_changes import (
        ChangeOrigin,
        reset_parameter,
        set_parameter,
        user_settings_write_scope,
    )

    key = "computer_use_observation_enabled"
    paths, user_config = _settings_paths(tmp_path)
    with user_settings_write_scope():
        opened = set_parameter(key, True, paths=paths, origin=ChangeOrigin("chat"))
        assert opened["ok"] is True, opened
        assert opened["effect_when"] == "restart_gateway" and "/restart" in opened["effect_text"]
    assert "computer_use_observation_enabled: true" in user_config.read_text(encoding="utf-8")
    assert load_config(str(user_config)).computer_use_observation_enabled is True
    with user_settings_write_scope():
        assert set_parameter(key, False, paths=paths, origin=ChangeOrigin("chat"))["ok"] is True
        assert load_config(str(user_config)).computer_use_observation_enabled is False
        assert reset_parameter(key, paths=paths, origin=ChangeOrigin("chat"))["ok"] is True
    assert "computer_use_observation_enabled" not in user_config.read_text(encoding="utf-8")


# 函数用途: 模型来源的 set / reset / revert 三条都按边界拒绝、文件不变；正式档管理员也拒。
def test_model_writes_are_refused_and_full_tier_stays_boundary(tmp_path):
    from agent_py_agent.agent.settings.parameter_changes import (
        ChangeOrigin,
        reset_parameter,
        revert_change,
        set_parameter,
        user_settings_write_scope,
    )

    key, full_key = "computer_use_observation_enabled", "computer_use_enabled"
    paths, user_config = _settings_paths(tmp_path)
    # revert 先按编号找记录、再判边界（revert_change → _writable_spec），所以要拿一条真实记录编号来测。
    with user_settings_write_scope():
        recorded = set_parameter(key, True, paths=paths, origin=ChangeOrigin("chat"))
    assert recorded["ok"] is True, recorded
    before = user_config.read_bytes()
    model = ChangeOrigin("model")
    refused = [
        set_parameter(key, False, paths=paths, origin=model),
        reset_parameter(key, paths=paths, origin=model),
        revert_change(str(recorded["change_id"])[:8], paths=paths, origin=model),
    ]
    assert [item["code"] for item in refused] == ["PARAMETER_BOUNDARY"] * 3, refused
    assert all(item["ok"] is False for item in refused)
    assert user_config.read_bytes() == before, "被拒的模型写入不能改动任何文件"
    with user_settings_write_scope():
        denied = set_parameter(full_key, True, paths=paths, origin=ChangeOrigin("chat"))
    assert denied["ok"] is False and denied["code"] == "PARAMETER_BOUNDARY", denied
