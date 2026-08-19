# LLM: 本模块只把 typed TUI snapshot 投影为终端 tab title；它不读取 secret、环境提示词或格式化 transcript 文案。
# 模块用途: 设置 终端交互 风格的静态/运行中动画终端标题，并在退出时恢复终端标题。

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable

from .tui_view_model import TuiViewSnapshot

TITLE_ANIMATION_FRAMES = ("⠂", "⠐")
TITLE_STATIC_PREFIX = "✳"
TITLE_ANIMATION_INTERVAL_SECONDS = 0.96
TITLE_MAX_CHARS = 80
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


# LLM: TuiTerminalTitleController 缓存最后写入值，避免每帧重复 OSC；title 来源只允许公开 user block/agent name 与 typed running 状态。
# 类用途: 根据当前会话内容更新终端标签标题。
class TuiTerminalTitleController:
    # LLM: clock 可注入以固定动画测试；agent_name 只作为无用户消息时的公开回退标题。
    # 函数用途: 创建终端标题控制器。
    def __init__(
        self,
        agent_name: str,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.agent_name = _sanitize_title(agent_name) or "my-agent"
        self._clock = clock
        self._last_title = ""
        self._lock = threading.Lock()

    # LLM: update 只调用 output 的公开 set_title；相同标题不重复发送，运行动画由 monotonic frame 决定。
    # 函数用途: 将当前 typed snapshot 写入终端标题并返回实际标题。
    def update(self, output: object, snapshot: TuiViewSnapshot) -> str:
        title = self._title_for_snapshot(snapshot)
        with self._lock:
            if title == self._last_title:
                return title
            self._last_title = title
        setter = getattr(output, "set_title", None)
        if callable(setter):
            setter(title)
        return title

    # LLM: clear 使用 output 公共 clear_title 并清本地缓存；退出后下次 app 不能误认为旧 title 仍已设置。
    # 函数用途: 在 TUI 退出时恢复终端标题。
    def clear(self, output: object) -> None:
        with self._lock:
            self._last_title = ""
        clearer = getattr(output, "clear_title", None)
        if callable(clearer):
            clearer()

    # LLM: 标题正文取首条真实 user block 的首行；动画判断只看 status.phase 和 permission overlay。
    # 函数用途: 计算当前终端标题字符串。
    def _title_for_snapshot(self, snapshot: TuiViewSnapshot) -> str:
        body = self.agent_name
        for block in snapshot.stable_blocks:
            if block.role != "user" or not block.text.strip():
                continue
            body = _sanitize_title(block.text.splitlines()[0]) or self.agent_name
            break
        body = body[: max(1, TITLE_MAX_CHARS - 2)].rstrip()
        running = snapshot.status.phase in {"running", "interrupting"}
        if running and snapshot.permission is None:
            frame = int(self._clock() / TITLE_ANIMATION_INTERVAL_SECONDS) % len(
                TITLE_ANIMATION_FRAMES
            )
            prefix = TITLE_ANIMATION_FRAMES[frame]
        else:
            prefix = TITLE_STATIC_PREFIX
        return f"{prefix} {body}"[:TITLE_MAX_CHARS]


# LLM: title 清理移除 OSC 可注入控制字符并折叠空白；用户正文只能作为纯单行终端标签。
# 函数用途: 规范终端标题的公开文本。
def _sanitize_title(value: object) -> str:
    cleaned = _CONTROL_CHARACTERS.sub(" ", str(value or ""))
    return " ".join(cleaned.split())


__all__ = [
    "TITLE_ANIMATION_FRAMES",
    "TITLE_ANIMATION_INTERVAL_SECONDS",
    "TITLE_STATIC_PREFIX",
    "TuiTerminalTitleController",
]
