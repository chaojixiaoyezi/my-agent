"""Host-owned Computer Use MCP profile built from a vetted open-source executor."""

# LLM: This module is the only composition point for the bundled Computer Use executor. Keep GUI
# mechanics upstream; this file may only decide structured eligibility and produce an MCP profile.
# 模块用途: 把成熟开源桌面执行器接入现有 MCP/Tool Gateway，同时守住管理员、Full Access 与审批边界。

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

COMPUTER_USE_MCP_SERVER_NAME = "computer_use"
COMPUTER_USE_PACKAGE = "computer-control-mcp==0.3.13"
# 适配器子进程里打开屏幕观察工具的环境标记：宿主只在主配置开关为 true 时写入，适配器只认 "1"
OBSERVATION_ENV_FLAG = "MY_AGENT_COMPUTER_USE_OBSERVATION"
# 只看档标记：总开关关、只开观察时写 "1"。适配器据此走纯观察装配路径——不 import pyautogui / computer_control_mcp，
# 也绝不注册上游工具；这是显式档位，不是缺依赖时的兜底降级。
OBSERVE_ONLY_ENV_FLAG = "MY_AGENT_COMPUTER_USE_OBSERVE_ONLY"

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


# J16 屏幕观察工具的声明（和插件 manifest v5 同形，宿主写死、不接受适配器自报）：读屏涉及隐私，observe_window 只读但审批 always；
# click_candidate / type_into_candidate（片 G）只接受候选 ID，dangerous。只在 computer_use_observation_enabled 为 true 时并入 Computer Use
# 服务声明。type_into_candidate 只有后端能给控件候选时适配器才注册（如 Linux X11 不注册）：声明了但没发现的工具按片 A 的规则只记 notice。
_COMPUTER_USE_OBSERVATION_DECLARATIONS: dict[str, dict[str, Any]] = {
    "observe_window": {"effect": "read_only", "approval": "always",
                       "observation": {"target_kind": "window", "max_candidates": 64}},
    "click_candidate": {"effect": "dangerous", "observation_ref": {"target_kind": "window", "param": "candidate_id"}},
    "type_into_candidate": {"effect": "dangerous", "observation_ref": {"target_kind": "window", "param": "candidate_id"}},
}

# 只看档（总开关关 + 观察开）只交出这一项：没有上游工具，也没有点击/输入候选；审批照旧每次都问本人。
# 单独一份表而不是从上面过滤，是为了让档位边界一眼可读、也不随上面增删工具被意外放宽。
_COMPUTER_USE_OBSERVE_ONLY_DECLARATIONS: dict[str, dict[str, Any]] = {
    "observe_window": _COMPUTER_USE_OBSERVATION_DECLARATIONS["observe_window"],
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


# LLM: 档位与宿主事实收成一个不可变载体：开关、属主、权限、运行环境一起决定装配结果，避免调用点散落判断。
#   只看档由调用方从两个开关派生（总开关关 + 观察开），不是缺依赖时的兜底。
# 类用途: 描述一次 Computer Use 装配要用的档位与属主事实。
@dataclass(frozen=True)
class ComputerUseTier:
    enabled: bool
    is_local_admin: bool
    access_mode: str
    observe_only: bool = False
    environ: Mapping[str, str] | None = None
    python_executable: str | None = None


# LLM: The reserved profile must be deterministic and copy-on-write. A config flag alone never
# grants GUI access: both structured local/main identity and effective Full Access are required.
#   只看档（observe_only=true 且总开关关）是显式档位：仍装配同一个服务、只并入 observe_window 声明，绝不注册上游工具；
#   完整档仍按原规则装配。两档都不绕过属主与 Full Access。
# 函数用途: 按当前 owner、最终权限与档位注入官方 Computer Use MCP 配置，不修改用户原始配置。
def computer_use_mcp_servers(configured_servers: object, tier: ComputerUseTier) -> dict[str, Any]:
    enabled, is_local_admin, access_mode = tier.enabled, tier.is_local_admin, tier.access_mode
    observe_only, environ, python_executable = tier.observe_only, tier.environ, tier.python_executable
    servers = dict(configured_servers) if isinstance(configured_servers, dict) else {}
    # 名称由底座保留：关闭或无权时必须移除，不能靠手写同名 mcp_servers 绕过唯一开关。
    servers.pop(COMPUTER_USE_MCP_SERVER_NAME, None)
    if (not enabled and not observe_only) or not is_local_admin or str(access_mode) != "full-access":
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
    declarations = _COMPUTER_USE_TOOL_DECLARATIONS
    if observe_only:
        gui_env[OBSERVE_ONLY_ENV_FLAG] = "1"
        declarations = _COMPUTER_USE_OBSERVE_ONLY_DECLARATIONS
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
        **_declaration_maps(declarations),
    }
    return servers


# LLM: 复制写：只在完整档（服务已按总开关装配）给该服务加观察环境标记并并入观察工具声明；只看档的声明和环境标记
#   已在 computer_use_mcp_servers 里按档位写好，这里再动会把上游工具声明混进只看档。不新建第二个服务，也不绕过属主范围。
# 函数用途: 完整档下按 computer_use_observation_enabled 决定是否打开屏幕观察工具。
def with_computer_use_observation(servers: Mapping[str, Any], *, enabled: bool) -> dict[str, Any]:
    result = dict(servers)
    profile = result.get(COMPUTER_USE_MCP_SERVER_NAME)
    if not enabled or not isinstance(profile, dict):
        return result
    if str(dict(profile.get("env") or {}).get(OBSERVE_ONLY_ENV_FLAG) or "") == "1":
        # 只看档已经只含观察声明与只看标记；再并入完整档观察工具声明就等于把上游档位混回来。
        return result
    declarations = _declaration_maps(_COMPUTER_USE_OBSERVATION_DECLARATIONS)
    result[COMPUTER_USE_MCP_SERVER_NAME] = {
        **profile,
        "env": {**dict(profile.get("env") or {}), OBSERVATION_ENV_FLAG: "1"},
        **{key: {**dict(profile.get(key) or {}), **value} for key, value in declarations.items()},
    }
    return result


__all__ = [
    "COMPUTER_USE_MCP_SERVER_NAME",
    "COMPUTER_USE_PACKAGE",
    "ComputerUseTier",
    "OBSERVATION_ENV_FLAG",
    "OBSERVE_ONLY_ENV_FLAG",
    "computer_use_mcp_servers",
    "with_computer_use_observation",
]
