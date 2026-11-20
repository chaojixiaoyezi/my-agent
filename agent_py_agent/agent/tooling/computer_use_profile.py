"""Host-owned Computer Use MCP profile built from a vetted open-source executor."""

# LLM: This module is the only composition point for the bundled Computer Use executor. Keep GUI
# mechanics upstream; this file may only decide structured eligibility and produce an MCP profile.
# 模块用途: 把成熟开源桌面执行器接入现有 MCP/Tool Gateway，同时守住管理员、Full Access 与审批边界。

from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .mcp_declarations import MCPServerWithCatalog

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


# 固定适配器的 tools/list 目录快照。描述、可选参数、默认值和输入形状由真实握手的同源测试核对。
_COMPUTER_USE_CATALOG = {
    "click_screen": (
        '在屏幕绝对坐标 (x, y) 执行单次鼠标左键点击。若已启用 observe_window，优先用 click_candidate 按复核候选点击；点击后请观察目标窗口确认状态。',
        {
            "x": {"title": "X", "type": "integer"},
            "y": {"title": "Y", "type": "integer"},
        },
        ["x", "y"],
    ),
    "get_screen_size": (
        '只读获取当前主屏幕像素分辨率（宽与高），供核对屏幕坐标范围。',
        {},
        [],
    ),
    "take_screenshot": (
        '截取整屏或匹配标题窗口的图像：title_pattern 留空截整屏，填写时默认按模糊匹配（threshold 默认 10）或设 use_regex=true 按正则匹配；save_to_downloads 默认保持 false；use_wgc 仅用于 Windows 捕获。常规窗口只读采样优先用 observe_window。',
        {
            "title_pattern": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Title Pattern"},
            "use_regex": {"default": False, "title": "Use Regex", "type": "boolean"},
            "threshold": {"default": 10, "title": "Threshold", "type": "integer"},
            "scale_percent_for_ocr": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None, "title": "Scale Percent For Ocr"},
            "save_to_downloads": {"default": False, "title": "Save To Downloads", "type": "boolean"},
            "use_wgc": {"default": False, "title": "Use Wgc", "type": "boolean"},
        },
        [],
    ),
    "take_screenshot_with_ocr": (
        '截屏并返回带屏幕绝对坐标的 OCR 识别框列表 JSON（四点坐标、文本与置信度）；title_pattern、use_regex、threshold、scale_percent_for_ocr、save_to_downloads 语义同 take_screenshot。若已启用 observe_window，优先用 observe_window 获取可复核候选。',
        {
            "title_pattern": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Title Pattern"},
            "use_regex": {"default": False, "title": "Use Regex", "type": "boolean"},
            "threshold": {"default": 10, "title": "Threshold", "type": "integer"},
            "scale_percent_for_ocr": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None, "title": "Scale Percent For Ocr"},
            "save_to_downloads": {"default": False, "title": "Save To Downloads", "type": "boolean"},
        },
        [],
    ),
    "move_mouse": (
        '将鼠标指针移动到屏幕绝对坐标 (x, y)，不触发按下或点击。',
        {
            "x": {"title": "X", "type": "integer"},
            "y": {"title": "Y", "type": "integer"},
        },
        ["x", "y"],
    ),
    "mouse_down": (
        "按住指定鼠标按键不放（button 可选 'left'、'right'、'middle'，默认 'left'）。注意：系统不会自动松开按键，必须在同一操作序列中配对调用相同 button 的 mouse_up 释放；普通单次点击请用 click_screen，拖拽请用 drag_mouse。",
        {
            "button": {"default": "left", "title": "Button", "type": "string"},
        },
        [],
    ),
    "mouse_up": (
        "释放先前由 mouse_down 按住的鼠标按键（button 可选 'left'、'right'、'middle'，默认 'left'，须与同序列中的 mouse_down 配对一致）。",
        {
            "button": {"default": "left", "title": "Button", "type": "string"},
        },
        [],
    ),
    "drag_mouse": (
        '从屏幕坐标 (from_x, from_y) 按住拖拽至 (to_x, to_y)，duration 为拖拽秒数（默认 0.5），内部自动完成按下与释放；拖拽后请观察目标窗口确认结果。',
        {
            "from_x": {"title": "From X", "type": "integer"},
            "from_y": {"title": "From Y", "type": "integer"},
            "to_x": {"title": "To X", "type": "integer"},
            "to_y": {"title": "To Y", "type": "integer"},
            "duration": {"default": 0.5, "title": "Duration", "type": "number"},
        },
        ["from_x", "from_y", "to_x", "to_y"],
    ),
    "key_down": (
        "按住单个键盘按键不放（key 如 'shift'、'ctrl'、'alt'）。注意：系统不会自动松开按键，必须在同一操作序列中配对调用相同 key 的 key_up 释放，防止修饰键残留影响后续桌面输入；普通按键或组合键请优先用 press_keys。",
        {
            "key": {"title": "Key", "type": "string"},
        },
        ["key"],
    ),
    "key_up": (
        '释放先前由 key_down 按住的键盘按键（key 须与同序列中的 key_down 配对一致）。',
        {
            "key": {"title": "Key", "type": "string"},
        },
        ["key"],
    ),
    "press_keys": (
        "发送自动按下并释放的键盘按键：keys 支持单键字符串（如 'enter'）、顺序按键列表（如 ['a', 'b']）或组合键嵌套列表（如 [['ctrl', 'c']]）。输入普通文本请用 type_text 或 type_into_candidate；提交后请观察目标窗口确认效果。",
        {
            "keys": {"anyOf": [{"type": "string"}, {"items": {"anyOf": [{"type": "string"}, {"items": {"type": "string"}, "type": "array"}]}, "type": "array"}], "title": "Keys"},
        },
        ["keys"],
    ),
    "list_windows": (
        '只读列出当前系统打开的窗口列表，供核对窗口标题后再调用 observe_window 或 activate_window。',
        {},
        [],
    ),
    "wait_milliseconds": (
        '等待指定毫秒数（milliseconds，整数），供界面动画或加载过渡后再重新观察。',
        {
            "milliseconds": {"title": "Milliseconds", "type": "integer"},
        },
        ["milliseconds"],
    ),
    "activate_window": (
        '按标题匹配将窗口激活至前台：title_pattern 默认模糊匹配（threshold 范围 0-100，默认 60），use_regex=true 时按正则匹配。只读采样无需先激活窗口，可直接调用 observe_window。',
        {
            "title_pattern": {"title": "Title Pattern", "type": "string"},
            "use_regex": {"default": False, "title": "Use Regex", "type": "boolean"},
            "threshold": {"default": 60, "title": "Threshold", "type": "integer"},
        },
        ["title_pattern"],
    ),
    "type_text": (
        '输入文本；clear_existing=true 清空当前控件后输入。提交后必须读取目标应用核对实际内容。',
        {
            "text": {"title": "Text", "type": "string"},
            "clear_existing": {"default": False, "title": "Clear Existing", "type": "boolean"},
        },
        ["text"],
    ),
    "scroll_screen": (
        '在屏幕坐标 (x, y) 执行竖向滚轮（clicks 正数向上滚、负数向下滚；x、y 留空时在当前鼠标位置滚动）。滚动后请观察窗口确认可见内容变化。',
        {
            "clicks": {"title": "Clicks", "type": "integer"},
            "x": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None, "title": "X"},
            "y": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None, "title": "Y"},
        },
        ["clicks"],
    ),
}
_COMPUTER_USE_OBSERVATION_CATALOG = {
    "observe_window": (
        '只读采样一个窗口：不切焦点、不激活、不动鼠标。window 可填：留空（当前最前面的普通窗口）、上一次观察返回的别名 win:…、\n    或与窗口标题完全相同的文字（只认唯一匹配；多个同名窗口会报 window_ambiguous 并列出别名；真实标题以 win: 开头的窗口请用别名）。\n    找不到时结果里带当前可见窗口清单 windows（别名 + 标题）。结果里的 my_agent_observation 候选只能通过 click_candidate 的 candidate_id 使用；\n    没有候选时不会凭空造候选。',
        {
            "window": {"default": "", "title": "Window", "type": "string"},
        },
        [],
    ),
    "click_candidate": (
        '点击上一次 observe_window 给出的候选中心。只接受 candidate_id；点之前按当时快照逐项复核（窗口实例、可见、几何、遮挡、\n    区域像素），任一项变了就不点并返回 stale。返回值只证明提交了点击，结果要靠下一次 observe_window 确认。',
        {
            "candidate_id": {"default": "", "title": "Candidate Id", "type": "string"},
        },
        [],
    ),
    "type_into_candidate": (
        '往上一次 observe_window 给出的可编辑控件（actions 里有 type_into_candidate 的候选）里输入 text：先点一下让它拿到焦点，\n    确认有焦点才输入；clear_existing=true 先全选原有内容再替换（text 为空就是清空）。text 最多 500 个字符，不能有换行、Tab 等\n    控制字符（多行文字暂不支持，需要回车请另行按键）。点之前按当时快照复核控件（还在、角色、可用、外框、名称、可编辑），变了就\n    不点并返回 stale；点了但控件没拿到焦点返回 focus_not_acquired（已点击、未输入）。返回值只证明提交了输入，不读回内容，结果要靠\n    下一次 observe_window 确认。',
        {
            "text": {"title": "Text", "type": "string"},
            "candidate_id": {"default": "", "title": "Candidate Id", "type": "string"},
            "clear_existing": {"default": False, "title": "Clear Existing", "type": "boolean"},
        },
        ["text"],
    ),
}


def _catalog_entries(catalog: Mapping[str, tuple], names: Iterable[str]) -> list[dict[str, Any]]:
    entries = []
    for name in names:
        description, properties, required = catalog[name]
        schema = {"type": "object", "title": f"{name}Arguments", "properties": deepcopy(properties)}
        if required:
            schema["required"] = list(required)
        entries.append({"name": name, "description": description, "inputSchema": schema})
    return entries


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
    if observe_only:
        gui_env[OBSERVE_ONLY_ENV_FLAG] = "1"
        declarations = _COMPUTER_USE_OBSERVE_ONLY_DECLARATIONS
        catalog = _catalog_entries(_COMPUTER_USE_OBSERVATION_CATALOG, declarations)
    else:
        declarations = _COMPUTER_USE_TOOL_DECLARATIONS
        catalog = _catalog_entries(_COMPUTER_USE_CATALOG, _COMPUTER_USE_CATALOG)
    servers[COMPUTER_USE_MCP_SERVER_NAME] = MCPServerWithCatalog({
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
    }, catalog)
    return servers


# LLM: 复制写：只在完整档（服务已按总开关装配）给该服务加观察环境标记并并入观察工具声明；只看档的声明和环境标记
#   已在 computer_use_mcp_servers 里按档位写好，这里再动会把上游工具声明混进只看档。不新建第二个服务，也不绕过属主范围。
# 函数用途: 完整档下按 computer_use_observation_enabled 决定是否打开屏幕观察工具。
def with_computer_use_observation(servers: Mapping[str, Any], *, enabled: bool) -> dict[str, Any]:
    result = dict(servers)
    profile = result.get(COMPUTER_USE_MCP_SERVER_NAME)
    if not enabled or not isinstance(profile, dict):
        return result
    env = dict(profile.get("env") or {})
    if str(env.get(OBSERVE_ONLY_ENV_FLAG) or "") == "1":
        # 只看档已经只含观察声明与只看标记；再并入完整档观察工具声明就等于把上游档位混回来。
        return result
    declarations = _declaration_maps(_COMPUTER_USE_OBSERVATION_DECLARATIONS)
    settings = {
        **profile,
        "env": {**env, OBSERVATION_ENV_FLAG: "1"},
        **{key: {**dict(profile.get(key) or {}), **value} for key, value in declarations.items()},
    }
    if isinstance(profile, MCPServerWithCatalog):
        # 适配器当前仅 macOS 后端提供可编辑控件候选。
        observed_names = [name for name in _COMPUTER_USE_OBSERVATION_CATALOG
                          if name != "type_into_candidate" or sys.platform == "darwin"]
        catalog = [entry for entry in profile.catalog if entry["name"] not in _COMPUTER_USE_OBSERVATION_CATALOG]
        catalog.extend(_catalog_entries(_COMPUTER_USE_OBSERVATION_CATALOG, observed_names))
        settings = MCPServerWithCatalog(settings, catalog)
    result[COMPUTER_USE_MCP_SERVER_NAME] = settings
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
