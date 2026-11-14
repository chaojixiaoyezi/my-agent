"""W5 behavioral tests: Computer Use concise Chinese guidance, press/release & tier invariants, and structured three-part MCP error presentation."""

# ruff: noqa: UP007, UP045

import asyncio
import importlib.abc
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional, Union

from mcp import types
from mcp.server.fastmcp import FastMCP

from agent_py_agent.agent.agent_core.tool_runtime_ledger import persist_tool_runtime_ledger
from agent_py_agent.agent.plugin_observation import (
    OBSERVATION_CANDIDATE_UNKNOWN,
    OBSERVATION_ERROR_KEY,
    OBSERVATION_KEY,
    OBSERVATION_META_EXTENSION,
    OBSERVATION_SCHEMA,
    OBSERVATION_STALE,
    PluginToolObservation,
    PluginToolObservationRef,
)
from agent_py_agent.agent.runtime_db.executor_liveness import attempt_executor
from agent_py_agent.agent.runtime_db.host_commands import HostCommandRequest
from agent_py_agent.agent.runtime_db.managed_operation_store import ManagedOperationStore
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.tooling import computer_use_observation_tools as obs_glue
from agent_py_agent.agent.tooling import computer_use_server as cu_server
from agent_py_agent.agent.tooling.computer_use_profile import (
    COMPUTER_USE_MCP_SERVER_NAME,
    OBSERVATION_ENV_FLAG,
    OBSERVE_ONLY_ENV_FLAG,
    ComputerUseTier,
    computer_use_mcp_servers,
    with_computer_use_observation,
)
from agent_py_agent.agent.tooling.mcp_client import (
    MCPError,
    MCPServerConfig,
    MCPToolInfo,
    _normalize_call_result,
)
from agent_py_agent.agent.tooling.mcp_registration import (
    build_proxy_tool,
    refresh_registered_mcp_client,
)
from agent_py_agent.agent.tooling.mcp_transport import MCPTransport
from agent_py_agent.agent.tooling.models import ResourceScopePolicy
from agent_py_agent.agent.tooling.process_session_cleanup import ProcessSessionCleanupError
from agent_py_agent.agent.tooling.runtime_contracts import tool_arguments_hash
from agent_py_agent.agent.tooling.screen_observation import ObservationError
from agent_py_agent.agent.tooling.tool_operation_coordinator import replay_completed_tool_operation
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_test_call,
    execute_canonical_test_call,
    runtime_snapshot_for_tools,
)
from agent_py_agent.tests.test_plugin_observation import _Repo as _ObsLedgerRepo

EXPECTED_UPSTREAM_NON_TYPING_TOOLS = (
    "click_screen",
    "get_screen_size",
    "take_screenshot",
    "take_screenshot_with_ocr",
    "move_mouse",
    "mouse_down",
    "mouse_up",
    "drag_mouse",
    "key_down",
    "key_up",
    "press_keys",
    "list_windows",
    "wait_milliseconds",
    "activate_window",
)


class _BlockDesktopDepsFinder(importlib.abc.MetaPathFinder):
    """Ensure headless tests never import pyautogui or computer_control_mcp."""

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> None:
        if fullname.split(".")[0] in {"pyautogui", "computer_control_mcp"}:
            raise ImportError(f"headless test blocked desktop dependency: {fullname}")
        return None


def _build_fake_upstream() -> tuple[SimpleNamespace, list[tuple[str, tuple[Any, ...], dict[str, Any]]]]:
    """Construct an in-process fake upstream module matching computer-control-mcp==0.3.13 signatures."""
    events: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    upstream_mcp = FastMCP("fake-upstream-computer-control")

    @upstream_mcp.tool()
    def click_screen(x: int, y: int) -> str:
        """Click at the specified screen coordinates."""
        events.append(("click_screen", (x, y), {}))
        return "clicked"

    @upstream_mcp.tool()
    def get_screen_size() -> dict[str, Any]:
        """Get the current screen resolution."""
        events.append(("get_screen_size", (), {}))
        return {"width": 1440, "height": 900}

    @upstream_mcp.tool()
    def type_text(text: str) -> str:
        """Type the specified text at the current cursor position."""
        events.append(("upstream_type_text", (text,), {}))
        return "upstream_typed"

    @upstream_mcp.tool()
    def take_screenshot(
        title_pattern: Optional[str] = None,
        use_regex: bool = False,
        threshold: int = 10,
        scale_percent_for_ocr: Optional[int] = None,
        save_to_downloads: bool = False,
        use_wgc: bool = False,
    ) -> str:
        """Get screenshot image."""
        events.append(
            (
                "take_screenshot",
                (),
                {
                    "title_pattern": title_pattern,
                    "use_regex": use_regex,
                    "threshold": threshold,
                    "scale_percent_for_ocr": scale_percent_for_ocr,
                    "save_to_downloads": save_to_downloads,
                    "use_wgc": use_wgc,
                },
            )
        )
        return "image"

    @upstream_mcp.tool()
    def take_screenshot_with_ocr(
        title_pattern: Optional[str] = None,
        use_regex: bool = False,
        threshold: int = 10,
        scale_percent_for_ocr: Optional[int] = None,
        save_to_downloads: bool = False,
    ) -> str:
        """Get OCR text from screenshot with absolute coordinates."""
        events.append(("take_screenshot_with_ocr", (), {"title_pattern": title_pattern}))
        return "[]"

    @upstream_mcp.tool()
    def move_mouse(x: int, y: int) -> str:
        """Move the mouse to the specified screen coordinates."""
        events.append(("move_mouse", (x, y), {}))
        return "moved"

    @upstream_mcp.tool()
    def mouse_down(button: str = "left") -> str:
        """Hold down a mouse button ('left', 'right', 'middle')."""
        events.append(("mouse_down", (button,), {}))
        return f"held:{button}"

    @upstream_mcp.tool()
    def mouse_up(button: str = "left") -> str:
        """Release a mouse button ('left', 'right', 'middle')."""
        events.append(("mouse_up", (button,), {}))
        return f"released:{button}"

    @upstream_mcp.tool()
    def drag_mouse(
        from_x: int,
        from_y: int,
        to_x: int,
        to_y: int,
        duration: float = 0.5,
    ) -> str:
        """Drag the mouse from one position to another."""
        events.append(("drag_mouse", (from_x, from_y, to_x, to_y, duration), {}))
        return "dragged"

    @upstream_mcp.tool()
    def key_down(key: str) -> str:
        """Hold down a specific keyboard key until released."""
        events.append(("key_down", (key,), {}))
        return f"key_held:{key}"

    @upstream_mcp.tool()
    def key_up(key: str) -> str:
        """Release a specific keyboard key."""
        events.append(("key_up", (key,), {}))
        return f"key_released:{key}"

    @upstream_mcp.tool()
    def press_keys(keys: Union[str, list[Union[str, list[str]]]]) -> str:
        """Press keyboard keys."""
        events.append(("press_keys", (keys,), {}))
        return "pressed"

    @upstream_mcp.tool()
    def list_windows() -> list[dict[str, Any]]:
        """List all open windows on the system."""
        events.append(("list_windows", (), {}))
        return [{"title": "Editor"}]

    @upstream_mcp.tool()
    def wait_milliseconds(milliseconds: int) -> str:
        """Wait for a specified number of milliseconds."""
        events.append(("wait_milliseconds", (milliseconds,), {}))
        return "waited"

    @upstream_mcp.tool()
    def activate_window(
        title_pattern: str,
        use_regex: bool = False,
        threshold: int = 60,
    ) -> str:
        """Activate a window by matching its title."""
        events.append(("activate_window", (title_pattern, use_regex, threshold), {}))
        return "activated"

    @upstream_mcp.tool()
    def custom_future_probe(flag: bool = True) -> str:
        """Upstream English probe description kept verbatim for unknown tools."""
        events.append(("custom_future_probe", (flag,), {}))
        return "probe_ok"

    ns = SimpleNamespace(
        mcp=upstream_mcp,
        click_screen=click_screen,
        get_screen_size=get_screen_size,
        type_text=type_text,
        take_screenshot=take_screenshot,
        take_screenshot_with_ocr=take_screenshot_with_ocr,
        move_mouse=move_mouse,
        mouse_down=mouse_down,
        mouse_up=mouse_up,
        drag_mouse=drag_mouse,
        key_down=key_down,
        key_up=key_up,
        press_keys=press_keys,
        list_windows=list_windows,
        wait_milliseconds=wait_milliseconds,
        activate_window=activate_window,
        custom_future_probe=custom_future_probe,
    )
    return ns, events


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def test_upstream_descriptions_preserve_signatures(monkeypatch) -> None:
    finder = _BlockDesktopDepsFinder()
    sys.meta_path.insert(0, finder)
    try:
        upstream, events = _build_fake_upstream()
        adapter_mcp = FastMCP("my-agent Computer Use")
        monkeypatch.setattr(cu_server, "mcp", adapter_mcp)

        scroll_calls: list[tuple[int, int | None, int | None]] = []
        typed_calls: list[tuple[str, bool]] = []
        fake_pyautogui = SimpleNamespace(
            scroll=lambda clicks, x=None, y=None: scroll_calls.append((clicks, x, y))
        )

        def fake_type_desktop_text(text: str, clear_existing: bool = False) -> dict[str, object]:
            typed_calls.append((text, clear_existing))
            return {"ok": True, "characters": len(text), "clear_existing": clear_existing}

        async def _inspect() -> tuple[dict[str, types.Tool], dict[str, types.Tool], Any]:
            upstream_tools = {t.name: t for t in await upstream.mcp.list_tools()}
            await cu_server.register_upstream_tools(fake_pyautogui, upstream, fake_type_desktop_text)
            low = obs_glue.build_adapter_server(adapter_mcp, {}, lambda: None)
            listed_res = await low.request_handlers[types.ListToolsRequest](
                types.ListToolsRequest(method="tools/list")
            )
            adapter_tools = {t.name: t for t in listed_res.root.tools}
            return upstream_tools, adapter_tools, low

        upstream_tools, adapter_tools, low = asyncio.run(_inspect())

        # All 14 known non-type_text upstream tools preserve exact inputSchema and receive concise Chinese descriptions.
        for name in EXPECTED_UPSTREAM_NON_TYPING_TOOLS:
            assert name in adapter_tools, f"missing upstream tool {name}"
            adapted = adapter_tools[name]
            orig = upstream_tools[name]
            assert adapted.inputSchema == orig.inputSchema, f"schema drifted for {name}"
            assert adapted.description != orig.description
            assert _contains_cjk(adapted.description or "")
            assert len(adapted.description or "") <= 220

        # Unknown upstream names retain their original upstream description verbatim and exact schema.
        assert "custom_future_probe" in adapter_tools
        assert (
            adapter_tools["custom_future_probe"].description
            == "Upstream English probe description kept verbatim for unknown tools."
        )
        assert (
            adapter_tools["custom_future_probe"].inputSchema
            == upstream_tools["custom_future_probe"].inputSchema
        )

        # type_text is replaced once by the local implementation (with clear_existing=False), never duplicated.
        type_spec = adapter_tools["type_text"]
        assert "clear_existing" in type_spec.inputSchema["properties"]
        assert "clear_existing" not in upstream_tools["type_text"].inputSchema["properties"]
        assert _contains_cjk(type_spec.description or "")

        # scroll_screen keeps Optional x/y schema and concise Chinese description.
        scroll_spec = adapter_tools["scroll_screen"]
        assert set(scroll_spec.inputSchema["properties"]) == {"clicks", "x", "y"}
        assert scroll_spec.inputSchema.get("required") == ["clicks"]
        assert _contains_cjk(scroll_spec.description or "")

        # Calling type_text and scroll_screen through the low-level server invokes local handlers, never upstream type_text.
        async def _call_locals() -> None:
            call_handler = low.request_handlers[types.CallToolRequest]
            res_type = await call_handler(
                types.CallToolRequest(
                    method="tools/call",
                    params=types.CallToolRequestParams(
                        name="type_text",
                        arguments={"text": "hello", "clear_existing": True},
                    ),
                )
            )
            assert res_type.root.isError is False
            res_scroll = await call_handler(
                types.CallToolRequest(
                    method="tools/call",
                    params=types.CallToolRequestParams(
                        name="scroll_screen",
                        arguments={"clicks": -3, "x": 120, "y": 240},
                    ),
                )
            )
            assert res_scroll.root.isError is False

        asyncio.run(_call_locals())
        assert typed_calls == [("hello", True)]
        assert scroll_calls == [(-3, 120, 240)]
        assert ("upstream_type_text", ("hello",), {}) not in events
    finally:
        sys.meta_path.remove(finder)


def test_press_release_guidance_and_tiers(monkeypatch) -> None:
    finder = _BlockDesktopDepsFinder()
    sys.meta_path.insert(0, finder)
    try:
        upstream, events = _build_fake_upstream()
        full_mcp = FastMCP("my-agent Computer Use")
        monkeypatch.setattr(cu_server, "mcp", full_mcp)

        fake_pyautogui = SimpleNamespace(scroll=lambda clicks, x=None, y=None: None)

        def fake_type(text: str, clear_existing: bool = False) -> dict[str, object]:
            return {"ok": True}

        class _ObserverStub:
            def __init__(self, *, supports_ui_candidates: bool) -> None:
                self.supports_ui_candidates = supports_ui_candidates
                self.calls: list[str] = []

            def observe(self, window: str | None = None, *, want_screenshot: bool = False) -> dict[str, object]:
                self.calls.append("observe")
                return {
                    "window": "win:1",
                    "generation": "g-1",
                    "candidate_count": 0,
                    OBSERVATION_KEY: {
                        "schema": "plugin_observation.v1",
                        "target": {"ref": "win:1", "generation": "g-1"},
                        "candidates": [],
                    },
                }

        async def _verify_full_and_press_release() -> None:
            await cu_server.register_upstream_tools(fake_pyautogui, upstream, fake_type)
            observer_with_typing = _ObserverStub(supports_ui_candidates=True)
            low_full = obs_glue.build_adapter_server(
                full_mcp,
                {OBSERVATION_ENV_FLAG: "1"},
                lambda: observer_with_typing,
            )
            listed = await low_full.request_handlers[types.ListToolsRequest](
                types.ListToolsRequest(method="tools/list")
            )
            by_name = {t.name: t for t in listed.root.tools}

            # key_down/key_up and mouse_down/mouse_up remain 4 distinct tools with pairing & non-auto-release guidance.
            for pair in (("key_down", "key_up"), ("mouse_down", "mouse_up")):
                down_name, up_name = pair
                assert down_name in by_name and up_name in by_name
                down_desc = by_name[down_name].description or ""
                up_desc = by_name[up_name].description or ""
                assert up_name in down_desc
                assert down_name in up_desc
                assert "不会自动松开" in down_desc
                assert "同一操作序列" in down_desc

            assert {"observe_window", "click_candidate", "type_into_candidate"} <= set(by_name)

            # Executing key_down or mouse_down performs zero automatic release or hidden desktop cleanup.
            call_handler = low_full.request_handlers[types.CallToolRequest]
            r_kd = await call_handler(
                types.CallToolRequest(
                    method="tools/call",
                    params=types.CallToolRequestParams(name="key_down", arguments={"key": "shift"}),
                )
            )
            r_md = await call_handler(
                types.CallToolRequest(
                    method="tools/call",
                    params=types.CallToolRequestParams(name="mouse_down", arguments={"button": "right"}),
                )
            )
            assert r_kd.root.isError is False and r_md.root.isError is False
            assert events == [
                ("key_down", ("shift",), {}),
                ("mouse_down", ("right",), {}),
            ]

        asyncio.run(_verify_full_and_press_release())

        # Backend without UI candidate typing registers observe_window + click_candidate, omitting type_into_candidate.
        no_typing_mcp = FastMCP("my-agent Computer Use")
        low_no_typing = obs_glue.build_adapter_server(
            no_typing_mcp,
            {OBSERVATION_ENV_FLAG: "1"},
            lambda: _ObserverStub(supports_ui_candidates=False),
        )
        listed_no_typing = asyncio.run(
            low_no_typing.request_handlers[types.ListToolsRequest](
                types.ListToolsRequest(method="tools/list")
            )
        )
        assert [t.name for t in listed_no_typing.root.tools] == ["observe_window", "click_candidate"]

        # Observe-only tier from host-generated env loads zero upstream tools and blocks non-observe calls with 3-part facts.
        observe_only_servers = with_computer_use_observation(
            computer_use_mcp_servers(
                {},
                ComputerUseTier(
                    enabled=False,
                    is_local_admin=True,
                    access_mode="full-access",
                    observe_only=True,
                    environ={"DISPLAY": ":99"},
                    python_executable="/py",
                ),
            ),
            enabled=True,
        )
        obs_env = observe_only_servers[COMPUTER_USE_MCP_SERVER_NAME]["env"]
        assert obs_glue.observe_only_enabled(obs_env) is True
        obs_mcp = FastMCP("my-agent Computer Use")
        observer_obs_only = _ObserverStub(supports_ui_candidates=True)
        low_obs = obs_glue.build_adapter_server(obs_mcp, obs_env, lambda: observer_obs_only)

        async def _verify_observe_only() -> None:
            listed_obs = await low_obs.request_handlers[types.ListToolsRequest](
                types.ListToolsRequest(method="tools/list")
            )
            assert [t.name for t in listed_obs.root.tools] == ["observe_window"]
            obs_call = low_obs.request_handlers[types.CallToolRequest]
            denied = await obs_call(
                types.CallToolRequest(
                    method="tools/call",
                    params=types.CallToolRequestParams(
                        name="click_candidate",
                        arguments={"candidate_id": "cand-1"},
                    ),
                )
            )
            assert denied.root.isError is True
            assert denied.root.structuredContent == {
                OBSERVATION_ERROR_KEY: {"code": "tool_not_available_in_observe_only"}
            }
            denied_body = json.loads(denied.root.content[0].text)
            assert denied_body["code"] == "OBSERVATION_TOOL_NOT_AVAILABLE_IN_OBSERVE_ONLY"
            assert denied_body["effect_outcome"] == "not_started"
            assert denied_body["side_effect"] == "none"
            assert "observe_window" in denied_body["next_action"]
            assert observer_obs_only.calls == []

        asyncio.run(_verify_observe_only())
    finally:
        sys.meta_path.remove(finder)


class _AdapterWireClient:
    """In-process MCP client normalizing real ServerResult(CallToolResult) wire dicts without subprocesses."""

    def __init__(self, config: MCPServerConfig, responder: Any) -> None:
        self.config = config
        self._responder = responder
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self.publication = None

    def is_running(self) -> bool:
        return True

    def list_tools(self, *, transport: Any = None) -> list[MCPToolInfo]:
        return list(self._responder.tools)

    def publish_tools(self, transport: Any, publish: Any) -> int:
        return publish()

    def call_tool(self, name: str, arguments: dict[str, Any], **options: Any) -> dict[str, Any]:
        self.calls.append((name, dict(arguments), dict(options)))
        outcome = self._responder(name, arguments, options)
        if isinstance(outcome, Exception):
            raise outcome
        return _normalize_call_result(outcome)


def _server_result_to_wire_dict(server_result: Any) -> dict[str, Any]:
    return server_result.root.model_dump(mode="json", by_alias=True, exclude_none=True)


def _run_managed_call(
    tmp_path: Path,
    repo: RuntimeRepository,
    proxy: Any,
    request_id: str,
    arguments: dict[str, Any],
) -> tuple[Any, Any]:
    arg_hash = tool_arguments_hash(arguments)
    request = HostCommandRequest(
        "owner-w5",
        "actor-w5",
        "cli",
        "thread-w5",
        request_id,
        proxy.model_spec.name,
        arg_hash.removeprefix("sha256:"),
    )
    binding = repo.register_host_command(request)
    repo.create_attempt(
        binding.agent_run_id,
        reuse_pending=True,
        reject_running=True,
        expected_pending_attempt_id=binding.attempt_id,
    )
    store = ManagedOperationStore(repo)
    with attempt_executor(repo, binding.run_id, binding.attempt_id):
        execution = execute_canonical_test_call(
            tmp_path,
            tools={proxy.model_spec.name: proxy},
            tool_name=proxy.model_spec.name,
            arguments=arguments,
            run_id=binding.run_id,
            attempt_id=binding.attempt_id,
            operation_id=request.operation_id,
            operation_owner_id=request.owner_id,
            operation_store=store,
            operation_store_required=True,
            trusted_run_context={"run_scope": {"run_id": binding.run_id, "task_id": binding.task_id}},
        )
    record = store.get_tool_operation(
        owner_id=request.owner_id,
        run_id=binding.run_id,
        task_id=binding.task_id,
        attempt_id=binding.attempt_id,
        operation_id=request.operation_id,
        tool_name=proxy.model_spec.name,
        args_hash=arg_hash,
    )
    return execution.result, record


def test_mcp_error_roundtrip_and_no_blind_retry(tmp_path: Path) -> None:
    repo = RuntimeRepository(tmp_path / "w5-runtime.db")

    # 1) Local schema validation before remote call: handler never runs, effect_outcome=not_started.
    raw_calls: list[dict[str, Any]] = []

    class _SimpleResponder:
        tools = [
            MCPToolInfo(
                "scroll_screen",
                "竖向滚轮",
                {
                    "type": "object",
                    "properties": {"clicks": {"type": "integer"}},
                    "required": ["clicks"],
                    "additionalProperties": False,
                },
            )
        ]

        def __call__(self, name: str, arguments: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
            raw_calls.append({"name": name, "arguments": arguments})
            return {"content": [{"type": "text", "text": "{}"}], "isError": False}

    simple_cfg = MCPServerConfig(name="computer_use", command="py", tool_effects={"scroll_screen": "mutating"})
    simple_client = _AdapterWireClient(simple_cfg, _SimpleResponder())
    scroll_proxy = build_proxy_tool(
        simple_client,
        "computer_use",
        _SimpleResponder.tools[0],
        effect="mutating",
    )
    bad_local = execute_canonical_test_call(
        tmp_path,
        tools={scroll_proxy.model_spec.name: scroll_proxy},
        tool_name=scroll_proxy.model_spec.name,
        arguments={"clicks": "not-an-int"},
    ).result
    assert bad_local.ok is False
    assert bad_local.handler_executed is False
    assert bad_local.effect_outcome == "not_started"
    assert bad_local.error_code == "TOOL_PARAMETER_TYPE_INVALID"
    assert simple_client.calls == [] and raw_calls == []

    # 2) Real local observation adapter -> encode_call_result -> _normalize_call_result -> ObservationBinding / MCPProxyTool.
    class _ObservationScenario:
        tools = [
            MCPToolInfo("observe_window", "观察窗口", {"type": "object", "properties": {"window": {"type": "string"}}}),
            MCPToolInfo(
                "type_into_candidate",
                "输入候选控件",
                {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "candidate_id": {"type": "string"},
                        "clear_existing": {"type": "boolean"},
                    },
                    "required": ["text"],
                },
            ),
        ]

        def __init__(self) -> None:
            self.mode = "observe_ok"

        def observe(self, window: str | None = None, *, want_screenshot: bool = False) -> dict[str, object]:
            if self.mode == "window_ambiguous":
                raise ObservationError(
                    "window_ambiguous",
                    "多个同名窗口",
                    {"windows": [{"alias": "win:1", "title": "Editor"}, {"alias": "win:2", "title": "Editor"}], "truncated": False},
                )
            return {
                "window": "win:1",
                "generation": "gen-1",
                "candidate_count": 1,
                OBSERVATION_KEY: {
                    "schema": OBSERVATION_SCHEMA,
                    "target": {"ref": "win:1", "generation": "gen-1"},
                    "frame": {"space": "screen_points", "origin": [0, 0], "size": [800, 600], "scale": [1, 1]},
                    "candidates": [
                        {
                            "key": "ax:input-1",
                            "role": "textbox",
                            "label": "Search",
                            "actions": ["type_into_candidate"],
                            "region": [10, 20, 120, 28],
                        }
                    ],
                },
            }

        def type_into_candidate(
            self,
            meta: Any,
            text: str,
            clear_existing: bool = False,
            *,
            cancelled: Any = None,
        ) -> dict[str, object]:
            if self.mode == "provider_stale":
                raise ObservationError("stale", "控件位置已变化")
            if self.mode == "focus_failed_after_click":
                raise ObservationError("focus_not_acquired", "已点击但控件未获得焦点", clicked=True)
            if self.mode == "type_failed_after_click":
                raise ObservationError("type_failed", "已点击，输入中途中断", clicked=True)
            return {"typed": {"key": "ax:input-1"}, "characters": len(text)}

        def __call__(self, name: str, arguments: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
            req_meta = options.get("request_meta") or {}
            ctx = obs_glue.CallContext(meta=req_meta.get(OBSERVATION_META_EXTENSION))
            body, structured, is_error = obs_glue.call_observation_tool(self, name, arguments, ctx)
            return _server_result_to_wire_dict(obs_glue.encode_call_result(body, structured, is_error))

    scenario = _ObservationScenario()
    obs_cfg = MCPServerConfig(
        name="computer_use",
        command="py",
        tool_effects={"observe_window": "read_only", "type_into_candidate": "dangerous"},
        tool_approvals={"observe_window": "always", "type_into_candidate": "always"},
        tool_observations={
            "observe_window": PluginToolObservation("window", 64),
            "type_into_candidate": PluginToolObservationRef("window", "candidate_id"),
        },
    )

    obs_event_repo = _ObsLedgerRepo()
    obs_registry = SimpleNamespace(
        tools={},
        _construction_params=SimpleNamespace(runtime_repo=obs_event_repo),
    )
    obs_client = _AdapterWireClient(obs_cfg, scenario)
    transport = MCPTransport(SimpleNamespace(pid=0, stderr=None), "computer_use", max_line_chars=4096, connect_timeout=1)
    assert refresh_registered_mcp_client(obs_registry, obs_client, transport=transport) == 2

    observe_proxy = obs_registry.tools["mcp__computer_use__observe_window"]
    type_proxy = obs_registry.tools["mcp__computer_use__type_into_candidate"]
    run_scope = {"run_id": "run-1", "task_id": "task-1"}

    # Mint a real observation candidate first through the production runtime ledger.
    obs_ok = observe_proxy.execute({"window": "", "__run_scope": run_scope, "__operation_id": "op-obs-1"})
    assert obs_ok.ok is True
    persist_tool_runtime_ledger(
        SimpleNamespace(subagents=SimpleNamespace(runtime_db=obs_event_repo), local_store=SimpleNamespace()),
        {
            "run_id": "run-1",
            "task_id": "task-1",
            "operation_id": "op-obs-1",
            "attempt_id": "attempt-1",
            "tool": obs_ok.tool,
            "ok": True,
            "error_code": "",
            "idempotency_key": "",
            "tool_result_envelope": obs_ok.result_envelope,
        },
    )
    minted_id = json.loads(obs_ok.output)["structuredContent"][OBSERVATION_KEY]["candidates"][0]["candidate_id"]

    # 2a) Pre-send unknown candidate_id rejected locally before calling server (not_started, 0 new wire calls).
    calls_before = len(obs_client.calls)
    pre_refused = type_proxy.execute({"text": "abc", "candidate_id": "cand-deadbeef00000000", "__run_scope": run_scope})
    assert len(obs_client.calls) == calls_before
    assert pre_refused.ok is False
    assert pre_refused.error_code == "TOOL_INVALID_ARGUMENTS"
    assert pre_refused.reported_error_code == OBSERVATION_CANDIDATE_UNKNOWN
    assert pre_refused.effect_outcome == "not_started"
    assert "observe_window" in pre_refused.next_action

    # 2b) Provider-side stale candidate lifted to OBSERVATION_STALE + not_started + observe_window advice.
    scenario.mode = "provider_stale"
    lifted_stale = type_proxy.execute({"text": "abc", "candidate_id": minted_id, "__run_scope": run_scope})
    assert lifted_stale.ok is False
    assert lifted_stale.error_code == "TOOL_INVALID_ARGUMENTS"
    assert lifted_stale.reported_error_code == OBSERVATION_STALE
    assert lifted_stale.effect_outcome == "not_started"
    assert "observe_window" in lifted_stale.next_action
    lifted_inner = json.loads(json.loads(lifted_stale.output)["result"])
    assert lifted_inner["code"] == "OBSERVATION_STALE"
    assert lifted_inner["effect_outcome"] == "not_started"

    # 2c) Post-click focus/typing failure preserves clicked=True, effect_outcome="failed", TOOL_RUNTIME_ERROR, MCP_REMOTE_ERROR, retryable=False.
    for mode, expected_code, expected_side_effect in (
        ("focus_failed_after_click", "OBSERVATION_FOCUS_NOT_ACQUIRED", "clicked_before_failure"),
        ("type_failed_after_click", "OBSERVATION_TYPE_FAILED", "partial_input_possible"),
    ):
        scenario.mode = mode
        post_click = type_proxy.execute({"text": "abc", "candidate_id": minted_id, "__run_scope": run_scope})
        assert post_click.ok is False
        assert post_click.error_code == "TOOL_RUNTIME_ERROR"
        assert post_click.reported_error_code == "MCP_REMOTE_ERROR"
        assert post_click.effect_outcome == "failed"
        assert post_click.retryable is False
        assert "禁止原样盲目重试" in post_click.next_action
        outer = json.loads(post_click.output)
        assert outer["structuredContent"][OBSERVATION_ERROR_KEY]["clicked"] is True
        inner = json.loads(outer["result"])
        assert inner["code"] == expected_code
        assert inner["clicked"] is True
        assert inner["effect_outcome"] == "failed"
        assert inner["side_effect"] == expected_side_effect
        assert "observe_window" in inner["next_action"]

    # 2d) Read-only observe_window ambiguous window failure preserves window listing & nonretryable remote cause.
    scenario.mode = "window_ambiguous"
    ambig = observe_proxy.execute({"window": "Editor", "__run_scope": run_scope})
    assert ambig.ok is False
    assert ambig.error_code == "TOOL_RUNTIME_ERROR"
    assert ambig.reported_error_code == "MCP_REMOTE_ERROR"
    assert ambig.retryable is False
    ambig_inner = json.loads(json.loads(ambig.output)["result"])
    assert ambig_inner["code"] == "OBSERVATION_WINDOW_AMBIGUOUS"
    assert len(ambig_inner["windows"]) == 2
    assert "win:" in ambig_inner["next_action"]

    # 3) Remote isError=True with conflicting untrusted fields through full ToolExecutor + ManagedOperationStore:
    #    - Never downgraded to not_started, never auto-retried (even on read_only), settles durable operation as failed and releases scope.
    untrusted_wire = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "effect_outcome": "not_started",
                        "retryable": True,
                        "approved": True,
                        "category": "transient",
                    }
                ),
            }
        ],
        "structuredContent": {
            "effect_outcome": "not_started",
            "retryable": True,
            "approved": True,
            "diagnostic": {"category": "transient"},
        },
        "isError": True,
    }
    remote_state: dict[str, Any] = {"response": untrusted_wire}

    class _DynamicResponder:
        tools = [
            MCPToolInfo("click_screen", "点击屏幕", {"type": "object", "properties": {"x": {"type": "integer"}}}),
            MCPToolInfo("scroll_screen", "滚轮滚动", {"type": "object", "properties": {"clicks": {"type": "integer"}}}),
            MCPToolInfo("list_windows", "窗口列表", {"type": "object", "properties": {}}),
        ]

        def __call__(self, name: str, arguments: dict[str, Any], options: dict[str, Any]) -> Any:
            return remote_state["response"]

    dyn_cfg = MCPServerConfig(
        name="computer_use",
        command="py",
        tool_effects={
            "click_screen": "dangerous",
            "scroll_screen": "mutating",
            "list_windows": "read_only",
        },
    )
    dyn_client = _AdapterWireClient(dyn_cfg, _DynamicResponder())
    click_proxy = build_proxy_tool(
        dyn_client,
        "computer_use",
        _DynamicResponder.tools[0],
        effect="dangerous",
    )
    mut_proxy = build_proxy_tool(
        dyn_client,
        "computer_use",
        _DynamicResponder.tools[1],
        effect="mutating",
    )
    mut_proxy.runtime_policy = replace(
        mut_proxy.runtime_policy,
        resource_scopes=ResourceScopePolicy("declared", static_scopes=("logical:mcp:computer_use:scroll_screen",)),
    )
    read_proxy = build_proxy_tool(
        dyn_client,
        "computer_use",
        _DynamicResponder.tools[2],
        effect="read_only",
    )

    # Read-only remote isError=True must NOT auto-retry despite untrusted transient fields.
    read_res = execute_canonical_test_call(
        tmp_path,
        tools={read_proxy.model_spec.name: read_proxy},
        tool_name=read_proxy.model_spec.name,
        arguments={},
    ).result
    assert len(dyn_client.calls) == 1
    assert read_res.ok is False
    assert read_res.error_code == "TOOL_RUNTIME_ERROR"
    assert read_res.metadata["reported_error_code"] == "MCP_REMOTE_ERROR"
    assert read_res.retryable is False
    assert read_res.output_trust == "external_data"
    assert read_res.metadata["next_action"]

    # Dangerous proxy direct call also reports determinate failed outcome and forbids blind retry.
    dang_res = click_proxy.execute({"x": 10})
    assert len(dyn_client.calls) == 2
    assert dang_res.ok is False
    assert dang_res.error_code == "TOOL_RUNTIME_ERROR"
    assert dang_res.reported_error_code == "MCP_REMOTE_ERROR"
    assert dang_res.effect_outcome == "failed"
    assert dang_res.retryable is False
    assert "禁止原样盲目重试" in dang_res.next_action

    # Mutating remote isError=True through ToolExecutor + ManagedOperationStore settles as failed (not not_started),
    # retryable=False, preserves next_action across replay, and releases scope.
    mut_res, mut_record = _run_managed_call(tmp_path, repo, mut_proxy, "req-remote-err", {"clicks": -3})
    assert len(dyn_client.calls) == 3
    assert mut_res.ok is False
    assert mut_res.error_code == "TOOL_RUNTIME_ERROR"
    assert mut_res.metadata["reported_error_code"] == "MCP_REMOTE_ERROR"
    assert mut_res.effect_outcome == "failed"
    assert mut_res.retryable is False
    assert mut_res.output_trust == "external_data"
    assert "禁止原样盲目重试" in mut_res.metadata["next_action"]
    assert mut_record.status == "failed"
    assert mut_record.result["effect_outcome"] == "failed"
    replayed = replay_completed_tool_operation(mut_record)
    assert replayed.retryable is False
    assert replayed.next_action == mut_res.metadata["next_action"]
    rendered = mut_res.render_for_prompt()
    assert "retryable=false" in rendered
    assert "[tool-next-action]" in rendered

    # Subsequent independent call on same logical scope succeeds because determinate remote failure settled the operation.
    remote_state["response"] = {"content": [{"type": "text", "text": "scrolled_ok"}], "isError": False}
    next_res, next_record = _run_managed_call(tmp_path, repo, mut_proxy, "req-after-err", {"clicks": 2})
    assert next_res.ok is True and next_record.status == "succeeded"

    # 4) Uncertain transport / cleanup / unexpected exception vs pre-send refusal.
    for req_id, failure, expected_reported in (
        ("req-timeout-unk", MCPError("transport timed out", code="MCP_TIMEOUT", effect_outcome="unknown"), "MCP_TIMEOUT"),
        (
            "req-cleanup-unk",
            ProcessSessionCleanupError(RuntimeError("cleanup unconfirmed"), {"session_id": "s-1"}, (), False),
            "PROCESS_SESSION_CLEANUP_UNCONFIRMED",
        ),
        ("req-unexpected-unk", RuntimeError("unexpected adapter crash"), "TOOL_RUNTIME_ERROR"),
    ):
        remote_state["response"] = failure
        unk_res, unk_record = _run_managed_call(
            tmp_path,
            repo,
            mut_proxy,
            req_id,
            {"clicks": len(req_id)},
        )
        assert unk_res.ok is False
        assert unk_res.effect_outcome == "unknown"
        assert unk_res.retryable is False
        assert unk_res.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
        assert unk_res.metadata["reported_error_code"] == expected_reported
        assert unk_res.metadata["next_action"]
        assert unk_record.status == "unknown"

    # Pre-send connection refusal keeps effect_outcome="not_started" and specific MCP_CONNECTION_CLOSED code.
    remote_state["response"] = MCPError("connection closed before send", code="MCP_CONNECTION_CLOSED", effect_outcome="not_started")
    unsent_res, unsent_record = _run_managed_call(tmp_path, repo, mut_proxy, "req-unsent", {"clicks": 99})
    assert unsent_res.ok is False
    assert unsent_res.effect_outcome == "not_started"
    assert unsent_res.metadata["reported_error_code"] == "MCP_CONNECTION_CLOSED"
    assert "not_started" in unsent_res.metadata["next_action"]
    assert unsent_record.status == "failed"
    assert unsent_record.result["effect_outcome"] == "not_started"

    # Verify canonical snapshot lookup works cleanly without extra dependencies.
    snap = runtime_snapshot_for_tools({click_proxy.model_spec.name: click_proxy})
    assert canonical_test_call(snap, click_proxy.model_spec.name, {"x": 1}).tool_name == click_proxy.model_spec.name
