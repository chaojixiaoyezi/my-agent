"""V02-02 回归：Gateway 模式 TUI Enter 的 /space、/team 必须由业务适配器执行。

缺陷：`_submit_input_area` 在 slash/business 分派之前先把任何合法 typed control 写进
durable control outbox。解析器把 `/space`、`/team` 判为合法控制（operation=business），
于是参数被丢成裸 `/space` / `/team` 发给 Gateway `/control`，落到无分支的 idle 兜底
（`当前没有运行中的内容，无需停止。`）。`/workflow` 不是 typed control，所以是正对照。

本模块只验证行为合同，不读取 git 历史、不连外网；业务请求只到 in-process loopback Gateway。
A：`_tui_submit_control_operation` 合同（unit）。
B：真实 `_submit_input_area` Enter seam + 只允许 `/client/business` 的假传输。
C：真实 Client → in-process Gateway → Client Envelope 往返。
"""
from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import urlsplit

import pytest
from prompt_toolkit.widgets import TextArea

from agent_py_agent.cli.chat_parts import control_runtime, tui, tui_actions, tui_keybindings
from agent_py_agent.cli.chat_parts.tui_interaction import TuiInteractionState
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

IDLE_STOP_TEXT = "当前没有运行中的内容，无需停止。"
BUSINESS_RAWS = ("/space list", "/space", "/space use space-1", "/team list")


# LLM: 只实现调用方需要的最小非阻塞接纳接口，用来证明“没有写控制 outbox”。
# 类用途: 记录控制操作提交，不执行、不发 HTTP。
class _CaptureReconciler:
    def __init__(self) -> None:
        self.rows: list[object] = []

    def enqueue_background(self, entry, callback=None) -> None:
        self.rows.append(entry)

    def enqueue_setting(self, entry) -> None:
        self.rows.append(entry)

    def enqueue(self, entry) -> None:
        self.rows.append(entry)


# LLM: 控制目标必须来自服务端执行投影，不能用本地 running/job 冒充。
# 函数用途: 发布一个合成执行快照，供 running 分支的实际绑定检查。
def _publish_execution(runtime: TuiRuntime, turn_id: str = "turn-v0202") -> None:
    from agent_py_agent.agent.gateway_parts.execution_target import GatewayExecutionTarget

    runtime.publish_execution_target(
        GatewayExecutionTarget("thread-v0202", "request", turn_id, "claim-v0202").to_dict()
    )


# 函数用途: 构造只含 _tui_submit_control_operation 所需字段的最小 Gateway 参数。
def _control_params(
    runtime: TuiRuntime,
    *,
    running: bool = False,
    reconciler: _CaptureReconciler | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        media_importing=False,
        use_gateway=True,
        state_lock=threading.Lock(),
        is_running_ref=[running],
        running_request_id_ref=[""],
        tui_runtime=runtime,
        control_operation_reconciler=reconciler,
    )


@pytest.mark.parametrize("raw", BUSINESS_RAWS)
@pytest.mark.parametrize("running", [False, True])
def test_space_and_team_are_not_conversation_controls(raw: str, running: bool) -> None:
    runtime = TuiRuntime("v0202-contract")
    if running:
        _publish_execution(runtime)
    reconciler = _CaptureReconciler()
    params = _control_params(runtime, running=running, reconciler=reconciler)

    consumed = tui_actions._tui_submit_control_operation(params, raw)

    assert consumed is False, f"{raw} 必须留给业务适配器"
    assert reconciler.rows == [], f"{raw} 不能写控制 outbox"
    assert runtime.notice() == "", f"{raw} 不能出现控制确认提示"


@pytest.mark.parametrize(
    ("raw", "kind"),
    [("/status", "status"), ("/stop", "stop")],
)
def test_unchanged_controls_still_enter_the_outbox(raw: str, kind: str) -> None:
    runtime = TuiRuntime("v0202-unchanged-controls")
    reconciler = _CaptureReconciler()
    params = _control_params(runtime, running=False, reconciler=reconciler)

    assert tui_actions._tui_submit_control_operation(params, raw) is True
    assert [row.command_kind for row in reconciler.rows] == [kind]


def test_workflow_still_falls_through_to_the_business_adapter() -> None:
    runtime = TuiRuntime("v0202-workflow-control")
    reconciler = _CaptureReconciler()
    params = _control_params(runtime, running=False, reconciler=reconciler)

    assert tui_actions._tui_submit_control_operation(params, "/workflow list") is False
    assert reconciler.rows == []


# ---------------------------------------------------------------------------
# B / C 共用的 in-process loopback Gateway 与薄客户端
# ---------------------------------------------------------------------------


# 函数用途: 在随机端口启动真实 Gateway；模型 echo、无工具、开启业务空间。
@pytest.fixture
def gateway(tmp_path, monkeypatch):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.settings import AgentConfig

    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "gateway-home"))
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            enable_tools=False,
            business_spaces_enabled=True,
            memory_path="memory.jsonl",
        ),
        tmp_path / "workspace",
    )
    server = GatewayHTTPServer(
        0,
        gateway_paths_from_root(agent.home_paths.root / "gateway"),
        params=GatewayHTTPServerParams(agent=agent),
    )
    server.start()
    try:
        yield SimpleNamespace(
            server=server,
            agent=agent,
            port=server.server.server_address[1],
            tmp=tmp_path,
        )
    finally:
        server.stop()


# 函数用途: 造一个与 TUI 相同的薄客户端；身份与凭据只来自隔离数据根。
def _thin_client(gateway: SimpleNamespace):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
    from agent_py_agent.cli.chat_client_context import GatewayChatClientAgent

    return GatewayChatClientAgent(
        SimpleNamespace(),
        SimpleNamespace(gateway_port=gateway.port, gateway_auth_token=""),
        gateway.tmp,
        [gateway.tmp],
        SimpleNamespace(root=gateway.agent.home_paths.root),
        owner_identity=OwnerIdentity.local_main(),
    )


# 函数用途: 只放行 /client/business 并原样转发到真实 Gateway，其余路径直接判失败。
@pytest.fixture
def business_only_wire(monkeypatch):
    from agent_py_agent.cli import chat_client_context

    sent: list[dict] = []
    forward = chat_client_context.urllib.request.urlopen

    def record(request, *args, **kwargs):
        path = urlsplit(request.full_url).path
        sent.append(
            {
                "path": path,
                "body": json.loads(request.data) if request.data else None,
            }
        )
        if path != "/client/business":
            raise AssertionError(f"V02-02 只应发送业务请求，实际发送了 {path}")
        return forward(request, *args, **kwargs)

    monkeypatch.setattr(chat_client_context.urllib.request, "urlopen", record)
    return sent


# 函数用途: 构造真实 Enter seam 需要的 params/runtime/输出收集器。
def _seam_setup(gateway: SimpleNamespace, *, running: bool, tmp_path) -> tuple:
    from agent_py_agent.agent.user_space.home_layout import home_paths

    input_area = TextArea(multiline=True)
    output: list[str] = []
    runtime = TuiRuntime("session-v0202")
    if running:
        _publish_execution(runtime)
    reconciler = _CaptureReconciler()
    params = SimpleNamespace(
        media_importing=False,
        input_area=input_area,
        interaction_state=TuiInteractionState(),
        tui_runtime=runtime,
        agent=_thin_client(gateway),
        args=SimpleNamespace(memory_limit=5),
        runtime_inject=[],
        prompt_files=[],
        use_gateway=True,
        paths=SimpleNamespace(root=tmp_path, processing=tmp_path / "gateway" / "processing"),
        state_lock=threading.Lock(),
        is_running_ref=[running],
        pending_jobs_ref=[0],
        running_prompt_ref=["正在核对"],
        running_request_id_ref=["req-v0202"],
        running_started_at_ref=[0.0],
        shutting_down_ref=[False],
        stop_event=threading.Event(),
        assistant_outputs=[],
        current_session_id="session-v0202",
        control_operation_reconciler=reconciler,
        exit_armed_at_ref=[0.0],
        eof_armed_at_ref=[0.0],
        escape_armed_at_ref=[0.0],
        escape_armed_text_ref=[""],
        local_run_ref=[None],
        home_paths=home_paths(tmp_path),
    )
    event = SimpleNamespace(
        app=SimpleNamespace(
            exit=MagicMock(),
            invalidate=lambda: None,
            create_background_task=asyncio.run,
        )
    )
    return event, params, runtime, input_area, output, reconciler


# 函数用途: 把 Enter 路径之外的执行入口全部改成“调用即失败”，证明命令没有进入模型/停止。
def _block_non_business_paths(monkeypatch, output: list[str]) -> None:
    blocked = MagicMock(side_effect=AssertionError("业务命令不能进入模型/停止/普通输入执行"))
    monkeypatch.setattr(tui, "_cprint", output.append)
    monkeypatch.setattr(control_runtime, "execute_chat_control", blocked)
    for name in (
        "_tui_submit_active_turn_input",
        "_tui_enqueue_job",
        "_tui_submit_agent_input",
        "_dispatch_agent_interrupt",
    ):
        monkeypatch.setattr(tui_keybindings, name, blocked)
    monkeypatch.setattr(
        tui_keybindings,
        "_agent_navigation_snapshot",
        lambda _params: SimpleNamespace(
            active_run_id="", active_thread_id="", terminal=False, can_send_input=True
        ),
    )


@pytest.mark.parametrize(
    ("raw", "action"),
    [
        ("/space list", "spaces.list"),
        ("/space use space-1", "sessions.space.set"),
        ("/team list", "background_agents.list"),
    ],
)
@pytest.mark.parametrize("running", [False, True])
def test_enter_dispatches_space_and_team_as_business(
    gateway, business_only_wire, tmp_path, monkeypatch, raw: str, action: str, running: bool
) -> None:
    event, params, _runtime, input_area, output, reconciler = _seam_setup(
        gateway, running=running, tmp_path=tmp_path
    )
    _block_non_business_paths(monkeypatch, output)
    input_area.text = raw

    tui_keybindings._submit_input_area(event, params)

    actions = (["sessions.space.get", action] if raw.startswith("/space use ") else [action])
    assert [item["path"] for item in business_only_wire] == ["/client/business"] * len(actions)
    assert [item["body"]["action"] for item in business_only_wire] == actions
    assert reconciler.rows == []
    assert output, "业务命令必须打印回执"
    assert IDLE_STOP_TEXT not in "\n".join(output)
    assert params.stop_event.is_set() is False
    assert params.pending_jobs_ref == [0]
    assert params.is_running_ref == [running]
    assert input_area.text == ""


def test_enter_replays_the_pty_probe_sequence(
    gateway, business_only_wire, tmp_path, monkeypatch
) -> None:
    event, params, _runtime, input_area, output, reconciler = _seam_setup(
        gateway, running=False, tmp_path=tmp_path
    )
    _block_non_business_paths(monkeypatch, output)

    for raw in ("/space list", "/space use space-1", "/workflow list", "/team list"):
        input_area.text = raw
        tui_keybindings._submit_input_area(event, params)

    assert [item["body"]["action"] for item in business_only_wire] == [
        "spaces.list",
        "sessions.space.get",
        "sessions.space.set",
        "workflows.list",
        "background_agents.list",
    ]
    assert all(item["path"] == "/client/business" for item in business_only_wire)
    assert reconciler.rows == []
    assert IDLE_STOP_TEXT not in "\n".join(output)


# ---------------------------------------------------------------------------
# C / 真实 Client → in-process Gateway → Client Envelope 往返
# ---------------------------------------------------------------------------


def _receipt_from(output: list[str]) -> dict:
    for line in reversed(output):
        text = str(line).strip()
        if text.startswith("{"):
            return json.loads(text)
    raise AssertionError(f"没有 JSON 回执：{output!r}")


def test_real_loopback_space_list_roundtrip(gateway, business_only_wire, tmp_path, monkeypatch) -> None:
    from agent_py_agent.cli.space_commands import (
        BusinessCommandSession,
        business_client_for_agent,
        consume_business_command,
    )

    client = _thin_client(gateway)
    created = consume_business_command(
        "/space create 验收空间", session=BusinessCommandSession(business_client_for_agent(client))
    )
    assert created["ok"] is True, created
    assert created["state"] == "completed"
    space_id = created["data"]["space_id"]
    business_only_wire.clear()

    event, params, _runtime, input_area, output, reconciler = _seam_setup(
        gateway, running=False, tmp_path=tmp_path
    )
    _block_non_business_paths(monkeypatch, output)
    params.agent = client
    input_area.text = "/space list"

    tui_keybindings._submit_input_area(event, params)

    assert [item["body"]["action"] for item in business_only_wire] == ["spaces.list"]
    assert [item["path"] for item in business_only_wire] == ["/client/business"]
    assert reconciler.rows == []
    assert IDLE_STOP_TEXT not in "\n".join(output)
    receipt = _receipt_from(output)
    assert receipt["ok"] is True, receipt
    assert receipt["state"] == "completed", receipt
    assert any(
        item.get("space_id") == space_id for item in receipt["data"].get("items", [])
    ), receipt
