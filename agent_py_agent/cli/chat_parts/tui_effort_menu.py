# LLM: TUI 里单独输入 /effort 时弹出的本地选择菜单，与 /model、/permissions 菜单共用 _dialog 与 _my_agent_model_menu_active
#   互斥标志。菜单只产出结构化档位值；选中后由调用方注入的 submit 把“/effort <值>”交给既有 Gateway 控制出站箱
#   （tui_actions._tui_submit_control_operation），本模块不发请求、不写线程、不解析回执文字。档位与中文名取自
#   backends/reasoning_control（与 Gateway 解析、发送同源）；改选项须同步 conversation/control_commands 的 /effort 语法、
#   tui_keybindings._dispatch_tui_slash_input 与 test_tui_effort_menu.py。
# 模块用途: 让 TUI 用户用上下键挑选本会话的智能程度，不用记 /effort 后面能跟什么。

from __future__ import annotations

from collections.abc import Callable

from prompt_toolkit.layout import HSplit
from prompt_toolkit.widgets import Label, RadioList

from ...agent.backends.reasoning_control import LEVEL_LABELS, REASONING_LEVELS
from .tui_model_menu import _dialog

# 第一项只查看、不改设置；其余每一项都对应一条 /effort 文字命令，值就是命令参数。
_VIEW_ROW = ("status", "查看当前设置，以及在当前模型上怎样生效")
_TAIL_ROWS = (("default", "default · 恢复全局默认（清除本会话设置）"), ("probe", "probe · 检测当前模型是否支持调节"))
_TITLE = "智能程度 /effort（只改本会话）"
_INTRO = "选中后回车；回执会说明在当前模型上实际怎么发送。"


# LLM: 行值必须是 /effort 能解析的参数（status 解析为查看）；显示文字带英文档位名，方便与 IM 里的文字命令对照。
# 函数用途: 生成菜单行：（命令参数，显示文字）。
def effort_menu_rows() -> list[tuple[str, str]]:
    levels = [(level, f"{level} · {LEVEL_LABELS[level]}") for level in REASONING_LEVELS]
    return [_VIEW_ROW, *levels, *_TAIL_ROWS]


# LLM: Esc/返回不提交任何命令；选中值只来自 RadioList 的结构化 value，不解析标签。
# 函数用途: 显示菜单并返回要发送的 /effort 命令文字；取消时返回空串。
async def choose_effort_command(app) -> str:
    choices = RadioList(effort_menu_rows(), select_on_focus=True)
    value = await _dialog(app, _TITLE, HSplit([Label(_INTRO), choices]),
                          (("确定", lambda: choices.current_value), ("返回", None)), focus=choices)
    return f"/effort {value}" if value else ""


# LLM: 已有弹窗或别的菜单在用时不叠加；占用标志在提交前释放，提交走调用方注入的 submit（控制出站箱），
#   菜单本身不触发模型请求。副作用：在 TUI 事件循环里起一个后台任务。
# 函数用途: 从按键处理里打开智能程度菜单，用户选定后提交对应的 /effort 命令。
def open_effort_menu(event, submit: Callable[[str], object]) -> None:
    app = event.app
    if app._my_agent_model_float_container.floats or getattr(app, "_my_agent_model_menu_active", False):
        return
    app._my_agent_model_menu_active = True

    # LLM: 任何退出（含取消、异常）都释放占用标志，保证之后还能再打开。
    # 函数用途: 在后台显示菜单并提交选择。
    async def run() -> None:
        try:
            command = await choose_effort_command(app)
        finally:
            app._my_agent_model_menu_active = False
            app.invalidate()
        if command:
            submit(command)
            app.invalidate()

    app.create_background_task(run())
