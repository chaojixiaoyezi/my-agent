"""B6 账本与展示合同：plugin_gate.decided 只读查询、观察计数读取、/plugins info 四段、TUI/IM 同文。

B5 未合入，账本行按设计第 9 节字段直接插入临时 owner 的 runtime.db；不启动插件进程、Gateway 或模型。
"""

from __future__ import annotations

import re
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.auth.middleware import AuthMiddleware
from agent_py_agent.agent.conversation.control_commands import ConversationControlCommand
from agent_py_agent.agent.gateway_parts import http_handlers, plugin_command_service
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.plugin_management import PluginManagement
from agent_py_agent.agent.runtime_db.repository import (
    PLUGIN_GATE_APPROVAL_UNAVAILABLE,
    RuntimeRepository,
)
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.tests.test_plugin_management import manager
from agent_py_agent.tests.test_plugin_manifest_v8 import _bundle, _v8

V8_PLUGIN = "sample-any"
OUTCOMES = ("ok", "timeout", "error", "malformed", "unavailable", "revoked")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


# 函数用途: 在临时 owner 里安装一个带订阅与收紧门的 v8 插件（安装允许，不启用、不启动进程）。
def v8_service(tmp_path, **changes):
    tmp_path.mkdir(parents=True, exist_ok=True)
    service, _ = manager(tmp_path, **changes)
    source = tmp_path / "watch.zip"
    source.write_bytes(_bundle(_v8()))
    result = service.command(f'/plugins install "{source}"', revision=service.catalog().revision,
                             request_id="install-watch")
    assert result["state"] == "succeeded", result
    return service


# 函数用途: 按设计第 9 节字段造一条 plugin_gate.decided 行并追加到临时库（写入方由 B5 负责，这里代插）。
def append_decision(service, **changes):
    row = {"plugin_id": V8_PLUGIN, "version": "1.0.0", "activation_id": "act-1", "gate_id": "guard-rm",
           "tool": "run_command", "call_id": "call-1", "operation_id": "op-1", "args_hash": "hash-1",
           "actor": "main", "outcome": "ok", "verdict": "ask", "reason_code": "RM_RF", "latency_ms": 41,
           "host_status": "allow", "final_status": "ask"}
    row.update(changes)
    repo = RuntimeRepository(runtime_db_path(service.context.owner.home_dir))
    return repo.append_event(event_type="plugin_gate.decided", attempt_id="attempt-display",
                             agent_run_id="run-display", payload=row)


# 函数用途: 只读打开临时 owner 的 runtime.db，读某插件的最近决定投影。
def decisions(service, plugin_id=V8_PLUGIN, limit=10):
    repo = RuntimeRepository(runtime_db_path(service.context.owner.home_dir), read_only=True)
    return repo.plugin_gate_decisions(plugin_id, limit=limit)


# 函数用途: 执行一次 /plugins info 并返回展示文字。
def info_message(service, plugin_id=V8_PLUGIN, request_id="info-1"):
    result = service.command(f"/plugins info {plugin_id}", revision=service.catalog().revision,
                             request_id=request_id)
    assert result["ok"] is True, result
    return result["message"]


# 类用途: 记录 stats 调用参数的假事件中心；只实现 B6 读取的只读快照接口。
class FakeHub:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.owner_keys = []

    def stats(self, owner_key):
        self.owner_keys.append(owner_key)
        return {plugin_id: dict(types) for plugin_id, types in self.snapshot.items()}


def test_each_outcome_row_keeps_all_structured_fields(tmp_path):
    service = v8_service(tmp_path)
    for outcome in OUTCOMES:
        append_decision(service, outcome=outcome, tool=f"tool-{outcome}",
                        reason_code=f"code_{outcome}", verdict="ask", final_status="ask")
    message = info_message(service)
    for outcome in OUTCOMES:
        assert f"征询 {outcome}" in message
        assert f"tool-{outcome}" in message
        assert f"原因码 code_{outcome}" in message
    assert message.count("征询 ") == len(OUTCOMES)
    assert len(_TIMESTAMP.findall(message)) >= len(OUTCOMES)


def test_recent_ten_newest_first_and_eleventh_hidden(tmp_path):
    service = v8_service(tmp_path)
    for index in range(1, 12):
        append_decision(service, reason_code=f"code-{index:02d}", tool=f"tool-{index:02d}")
    message = info_message(service)
    assert "code-11" in message and "code-02" in message
    assert "code-01" not in message  # 最旧一条被窗口挤出
    assert message.index("code-11") < message.index("code-02")
    assert len(decisions(service, limit=50)) == 11  # 库内仍有 11 条，只是展示截断


def test_unavailable_counted_separately_even_outside_recent_window(tmp_path):
    service = v8_service(tmp_path)
    append_decision(service, final_status=PLUGIN_GATE_APPROVAL_UNAVAILABLE, reason_code="blocked-old")
    for index in range(2, 12):
        append_decision(service, reason_code=f"code-{index:02d}")
    append_decision(service, final_status=PLUGIN_GATE_APPROVAL_UNAVAILABLE, reason_code="blocked-new")
    message = info_message(service)
    assert "无法审批：2 次" in message
    assert "blocked-new" in message
    assert "blocked-old" not in message  # 窗口外的“无法审批”不再显示，但计数仍统计


def test_message_body_never_leaks_into_query_or_display(tmp_path):
    service = v8_service(tmp_path)
    secret = "要删除整个目录，先确认一次"
    append_decision(service, reason_code="RM_RF", message=secret)
    rows = decisions(service)
    assert len(rows) == 1
    assert "message" not in rows[0]
    assert set(rows[0]) == {"plugin_id", "version", "activation_id", "gate_id", "tool", "call_id",
                            "operation_id", "args_hash", "actor", "outcome", "verdict", "reason_code",
                            "latency_ms", "host_status", "final_status", "created_at"}
    assert secret not in info_message(service)


def test_unavailable_count_only_for_the_matching_plugin(tmp_path):
    service = v8_service(tmp_path)
    append_decision(service, final_status=PLUGIN_GATE_APPROVAL_UNAVAILABLE, reason_code="blocked")
    append_decision(service, plugin_id="other-plugin", final_status=PLUGIN_GATE_APPROVAL_UNAVAILABLE,
                    reason_code="blocked-other")
    repo = RuntimeRepository(runtime_db_path(service.context.owner.home_dir), read_only=True)
    assert repo.plugin_gate_unavailable_count(V8_PLUGIN) == 1
    assert repo.plugin_gate_unavailable_count("other-plugin") == 1
    assert repo.plugin_gate_decisions(V8_PLUGIN)[0]["reason_code"] == "blocked"


# 类用途: 最小 HTTP handler：给 plugin_http_response 提供来源、身份与回包接口。
class Handler:
    def __init__(self, body, *, user="local-agent"):
        self.body = body
        self.headers = {"X-User-Id": user, "X-Channel": "local"}
        self.client_address = ("127.0.0.1", 12345)
        self._auth_middleware = None
        self.reply = None

    def _read_json(self):
        return self.body

    def _send_json(self, status, payload):
        self.reply = status, payload


# 函数用途: 组装带事件中心的 Gateway server 投影（不监听端口、不启动进程）。
#   base_root 是 Gateway 的 owner 根目录（home_paths.root）：请求 owner 由它加身份解析，必须与安装插件的 owner 同根。
#   hub 只挂在 server 上（与 plugin_panels_http.plugin_event_hub 的真实做法一致）；
#   server.agent 是另一个对象，上不挂 hub——这正是 B6 初审 M1 抓出的真实拓扑。
def gateway_server(base_root, hub=None, paths=None):
    base = SimpleNamespace(config=AgentConfig(auth_enabled=False, gateway_per_user_owner_scoping=True),
                           home_paths=home_paths(base_root))
    return SimpleNamespace(agent=base, paths=paths, auth_middleware=None, bind_host="127.0.0.1",
                           plugin_event_hub=hub)


# 函数用途: 按真实 IM 链提交一条控制命令，返回持久回执投影。
#   IM 没有 handler 目录握手，控制命令从 http_handlers._handle_persistent_control_operation 进入，
#   那里是唯一能拿到 server 的地方（hub 也从 server 取）；不能再用 /client/plugins 假装 IM 入口。
def im_control(server, text, *, conversation_id, message_id):
    handler = Handler({})
    command = ConversationControlCommand(kind="plugins", value=text)
    scope = GatewayControlScope(user_id="local-agent", channel="local", conversation_id=conversation_id,
                                metadata={"message_id": message_id})
    http_handlers._handle_persistent_control_operation(
        handler, server, command=command, command_text=text, scope=scope)
    status, payload = handler.reply
    assert status == 200, payload
    return payload
    return payload


def test_tui_http_and_im_render_byte_identical_text(tmp_path, monkeypatch):
    # 两条入口按真实拓扑各走各的链，但事件中心只有一个来源：server。
    #   TUI 走 HTTP 路由 /client/plugins（目录握手 + 命令）；IM 走 _handle_persistent_control_operation。
    #   两条链都不许把 hub 挂到 server.agent 上——那正是 B6 初审 M1 抓出的错误拓扑。
    monkeypatch.setattr(http_handlers, "_request_channel", lambda handler: ("local-agent", "local"))
    monkeypatch.setattr(http_handlers, "_request_identity", lambda handler: ("local-agent", None))
    # manager(base) 的 owner 落在 <base>/home；Gateway 根要指向同一个 home 目录，两条入口才解析到装插件的那个 owner。
    service = v8_service(tmp_path)
    append_decision(service, reason_code="RM_RF")
    hub = FakeHub({V8_PLUGIN: {"prompt_submitted": {"delivered": 2, "coalesced": 0, "failed": 0,
                                                    "unavailable": 0, "last_error_code": "",
                                                    "last_delivered_at": 0}}})
    server = gateway_server(tmp_path / "home", hub, paths=gateway_paths_from_root(tmp_path / "gw"))
    catalog_body = {"operation": "catalog", "conversation_id": "session-a"}
    catalog = plugin_command_service.plugin_http_response(Handler(catalog_body), server, catalog_body)
    revision = catalog["catalog"]["revision"]
    # TUI 命令：目录握手 + 命令
    tui_body = {"operation": "command", "conversation_id": "session-a",
                "command": f"/plugins info {V8_PLUGIN}", "catalog_revision": revision}
    tui = plugin_command_service.plugin_http_response(Handler(tui_body), server, tui_body, text=tui_body["command"])
    assert tui["ok"] is True, tui
    # IM 命令：走真实 IM 链，正文相同、身份与会话不同（IM 由宿主实时冻结目录版本，不看 catalog_revision）
    im = im_control(server, f"/plugins info {V8_PLUGIN}", conversation_id="session-b", message_id="im-msg-1")
    assert im["ok"] is True, im
    assert tui["message"] == im["message"]
    assert "无法审批：0 次" in im["message"]
    # M1 的核心断言：计数来自 server 上的 hub，不是“暂无记录”（修复前 IM 链读 server.agent，恒为 None）
    assert "观察计数：暂无记录" not in im["message"], "IM 必须能读到 server 上的事件中心计数"
    assert "prompt_submitted：送达 2、合并 0、失败 0、不可用 0" in im["message"]
    # 反向钉住单一来源：server 上没挂 hub 时，同一条 IM 链只能降级成“暂无记录”，不能报错、不能新建 hub。
    bare = gateway_server(tmp_path / "home", None, paths=gateway_paths_from_root(tmp_path / "gw-bare"))
    degraded = im_control(bare, f"/plugins info {V8_PLUGIN}", conversation_id="session-c", message_id="im-msg-2")
    assert degraded["ok"] is True, degraded
    assert "观察计数：暂无记录" in degraded["message"]


def test_other_owner_records_and_counts_never_appear(tmp_path):
    service_a = v8_service(tmp_path / "a")
    append_decision(service_a, reason_code="owner-a-only")
    service_b = v8_service(tmp_path / "b")
    append_decision(service_b, final_status=PLUGIN_GATE_APPROVAL_UNAVAILABLE, reason_code="owner-b-blocked")
    message_b = info_message(service_b)
    assert "owner-a-only" not in message_b
    assert "无法审批：1 次" in message_b
    message_a = info_message(service_a)
    assert "owner-b-blocked" not in message_a
    assert "无法审批：0 次" in message_a


def test_hub_stats_read_with_owner_home_key(tmp_path):
    service_a = v8_service(tmp_path / "a")
    service_b = v8_service(tmp_path / "b")
    hub = FakeHub({})
    for service in (service_a, service_b):
        managed = PluginManagement(replace(service.context, event_hub=hub))
        managed.command(f"/plugins info {V8_PLUGIN}", revision=managed.catalog().revision, request_id="x")
    assert hub.owner_keys == [str(service_a.context.owner.home_dir), str(service_b.context.owner.home_dir)]


def test_legacy_plugin_and_empty_v8_show_placeholders(tmp_path):
    legacy, source = manager(tmp_path)
    result = legacy.command(f'/plugins install "{source}"', revision=legacy.catalog().revision,
                            request_id="install-legacy")
    assert result["state"] == "succeeded", result
    message = info_message(legacy, plugin_id="sample-peek")
    assert "事件订阅：没有订阅" in message
    assert "收紧工具：没有收紧" in message
    assert "网络与沙箱：旧版插件没有事件与收紧声明，不适用。" in message
    assert "最近收紧决定（最多 10 次，新到旧）：\n  暂无记录" in message
    assert "无法审批：0 次" in message
    assert "观察计数：暂无记录" in message

    fresh = v8_service(tmp_path / "v8")
    v8_message = info_message(fresh)
    assert "事件订阅：prompt_submitted（不含正文）" in v8_message
    assert "收紧门 guard-rm：能看到这些工具的完整参数：run_command（脱敏后最多4000字）。" in v8_message
    assert "网络与沙箱：禁止联网（含本机回环）；沙箱要求强制使用插件进程沙箱，不可用时不得启用。" in v8_message
    assert "暂无记录" in v8_message


def test_observed_counts_rendered_and_missing_hub_degrades(tmp_path):
    service = v8_service(tmp_path)
    assert "观察计数：暂无记录" in info_message(service)  # direct 入口没有 hub，不报错
    hub = FakeHub({V8_PLUGIN: {"tool_call_finished": {
        "delivered": 5, "coalesced": 2, "failed": 1, "unavailable": 0,
        "last_error_code": "MCP_REMOTE_ERROR", "last_delivered_at": 1759480000.5}}})
    managed = PluginManagement(replace(service.context, event_hub=hub))
    message = info_message(managed)
    assert "tool_call_finished：送达 5、合并 2、失败 1、不可用 0" in message
    assert "最近错误码 MCP_REMOTE_ERROR" in message and "最近送达 " in message


def test_broken_hub_or_ledger_read_never_breaks_info(tmp_path):
    service = v8_service(tmp_path)
    append_decision(service, reason_code="RM_RF")

    class BrokenHub:
        def stats(self, owner_key):
            raise RuntimeError("boom")

    managed = PluginManagement(replace(service.context, event_hub=BrokenHub()))
    result = managed.command(f"/plugins info {V8_PLUGIN}", revision=managed.catalog().revision,
                             request_id="info-broken")
    assert result["ok"] is True, result
    assert "观察计数：暂无记录" in result["message"]
    assert "原因码 RM_RF" in result["message"]


def test_info_for_unknown_plugin_still_reports_not_found(tmp_path):
    service = v8_service(tmp_path)
    result = service.command("/plugins info missing-plugin", revision=service.catalog().revision,
                             request_id="info-missing")
    assert result["ok"] is False and result["message"] == "未找到该插件。"
