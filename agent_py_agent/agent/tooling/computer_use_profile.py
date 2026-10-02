"""Host-owned Computer Use MCP profile built from a vetted open-source executor."""

# LLM: This module is the only composition point for the bundled Computer Use executor. Keep GUI
# mechanics upstream; this file may only decide structured eligibility and produce an MCP profile.
# 模块用途: 把成熟开源桌面执行器接入现有 MCP/Tool Gateway，同时守住管理员、Full Access 与审批边界。

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Any

COMPUTER_USE_MCP_SERVER_NAME = "computer_use"
COMPUTER_USE_PACKAGE = "computer-control-mcp==0.3.13"

_GUI_SESSION_ENV_KEYS = (
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "DBUS_SESSION_BUS_ADDRESS",
)

# 宿主写死的逐工具声明表，一张表产出 mcp_servers 里的 tool_effects / tool_approvals / tool_observations 三项，和插件 manifest v5
# 同形；不接受适配器握手自报。只读项不会改变桌面；指针移动/切窗会改变用户观察状态但不输入数据。截图与 OCR
# 可能读取敏感屏幕内容且 upstream 还带 save_to_downloads 参数，静态 effect 无法按参数降级，
# 因此继续按 dangerous 进入精确审批。其余点击、键入、按键与拖拽也保持 dangerous。
# 未列出的工具沿用 default_effect=dangerous 与默认审批；读屏类观察工具（J16 片 B 起）在这里加 approval=always 与 observation 声明。
_COMPUTER_USE_TOOL_DECLARATIONS: dict[str, dict[str, Any]] = {
    "get_screen_size": {"effect": "read_only"},
    "list_windows": {"effect": "read_only"},
    "wait_milliseconds": {"effect": "read_only"},
    "move_mouse": {"effect": "mutating"},
    "activate_window": {"effect": "mutating"},
    "scroll_screen": {"effect": "mutating"},
}


# LLM: 三张映射只从同一张表派生，空项不写键值；observation / observation_ref 原样复制成 mcp_servers.tool_observations 的单项形状，
#   由 MCPServerConfig.from_mapping 解析成 v5 数据类并在发布前核对。
# 函数用途: 把声明表拆成 mcp_servers 配置里的逐工具 effect / 审批 / 观察映射。
def _declaration_maps(table: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    maps: dict[str, dict[str, Any]] = {"tool_effects": {}, "tool_approvals": {}, "tool_observations": {}}
    for tool_name, declaration in table.items():
        if "effect" in declaration:
            maps["tool_effects"][tool_name] = declaration["effect"]
        if "approval" in declaration:
            maps["tool_approvals"][tool_name] = declaration["approval"]
        observation = {key: dict(declaration[key]) for key in ("observation", "observation_ref") if key in declaration}
        if observation:
            maps["tool_observations"][tool_name] = observation
    return maps


# LLM: The reserved profile must be deterministic and copy-on-write. A config flag alone never
# grants GUI access: both structured local/main identity and effective Full Access are required.
# 函数用途: 按当前 owner 与最终权限注入官方 Computer Use MCP 配置，不修改用户原始配置。
def computer_use_mcp_servers(
    configured_servers: object,
    *,
    enabled: bool,
    is_local_admin: bool,
    access_mode: str,
    environ: Mapping[str, str] | None = None,
    python_executable: str | None = None,
) -> dict[str, Any]:
    servers = dict(configured_servers) if isinstance(configured_servers, dict) else {}
    # 名称由底座保留：关闭或无权时必须移除，不能靠手写同名 mcp_servers 绕过唯一开关。
    servers.pop(COMPUTER_USE_MCP_SERVER_NAME, None)
    if not enabled or not is_local_admin or str(access_mode) != "full-access":
        return servers

    source_env = os.environ if environ is None else environ
    gui_env = {
        key: str(source_env[key])
        for key in _GUI_SESSION_ENV_KEYS
        if str(source_env.get(key) or "").strip()
    }
    # Upstream's development mode sends diagnostic lines to stderr. Production mode prints them
    # to stdout, which shares the JSON-RPC transport and must never be selected by this adapter.
    gui_env["ENV"] = "development"
    servers[COMPUTER_USE_MCP_SERVER_NAME] = {
        "command": str(python_executable or sys.executable),
        "args": ["-m", "agent_py_agent.agent.tooling.computer_use_server"],
        "env": gui_env,
        "connect_timeout": 60,
        # OCR 官方说明在普通 1080p 机器约需 20 秒；慢机保留充足预算，仍可被 /stop 取消。
        "timeout": 180,
        # OCR 坐标可能较长；原始截图是单行 base64，放宽传输硬限但模型侧仍走有界投影。
        "max_content_chars": 256 * 1024,
        "max_line_chars": 32 * 1024 * 1024,
        # 当前 MiniMax-M2.7 不支持 会话运行时/终端交互 的原生 tool-search 引用协议；官方
        # Computer Use 必须首轮可见，普通第三方 MCP 仍保留默认渐进披露。
        "catalog_category": "computer_use",
        "default_effect": "dangerous",
        **_declaration_maps(_COMPUTER_USE_TOOL_DECLARATIONS),
    }
    return servers


__all__ = [
    "COMPUTER_USE_MCP_SERVER_NAME",
    "COMPUTER_USE_PACKAGE",
    "computer_use_mcp_servers",
]
