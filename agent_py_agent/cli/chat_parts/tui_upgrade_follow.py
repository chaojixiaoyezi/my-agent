# LLM: TUI 随 Gateway 升级原地切换。判定只读 Gateway 状态文件里的结构化 runtime_prefix 与本进程 sys.prefix 比对，不解析文案。
#   空闲时（无回合、无排队、无待审批、输入框为空且有焦点、停在主视图并跟随底部）在 UI 事件循环线程上 os.execv 到 Gateway 同版
#   my-agent：不退出全屏、不恢复终端、不打印退出横幅，屏幕保持原样；新进程经环境变量 MY_AGENT_TUI_HANDOFF 拿到同一会话编号和
#   第一代进程保存的原始终端设置，先同步完成就绪检查与历史读取再首帧渲染，所以画面接续、会话不变，期间按键留在终端输入队列里不丢。
#   exec 失败时旧界面继续可用并提示。Windows 的 execv 会另起进程，不做原地切换，只提示重开。开关 tui_follow_gateway_upgrade（默认开）。
#   改动时同步 tests/test_tui_upgrade_follow.py、tests/test_cli_chat.py 的 handoff 用例、docs/design/TUI_DESIGN.md 与 CLI_REFERENCE.md。
# 模块用途: 部署新版 Gateway 后，正在用的 TUI 自己在空闲时原地换成同版客户端，用户看不到关闭重开。
from __future__ import annotations

import atexit
import io
import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ...agent.common.json_io import read_json_object_report

HANDOFF_ENV = "MY_AGENT_TUI_HANDOFF"
_HANDOFF_SCHEMA = "my_agent.tui_handoff.v1"
_SESSION_ID = re.compile(r"[A-Za-z0-9._:-]{1,200}")
POLL_SECONDS = 5.0
NOTICE_KIND = "gateway_upgrade"
BUSY_NOTICE_TEXT = "Gateway 已升级到新版本，界面空闲时会原地切换，不需要重开。"
REOPEN_NOTICE_TEXT = "Gateway 已升级到新版本，重开 my-agent 即可使用新界面。"
SWITCHING_NOTICE_TEXT = "正在切换到新版本…"
FAILED_NOTICE_TEXT = "切换到新版本失败，当前界面继续可用；方便时重开 my-agent 即可。"
_NOTICE_SECONDS = 6.0
_NOTICE_REPEAT_SECONDS = 30.0
_SWITCH_RENDER_DELAY_SECONDS = 0.25
# 新进程没能接管终端就退出时，把旧界面留下的全屏、鼠标、括号粘贴、隐藏光标都撤掉，让 shell 回到正常样子。
_TERMINAL_RESET = "\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1015l\x1b[?2004l\x1b[?25h\x1b[?1049l"


# LLM: 进程级唯一状态；child 表示本进程由上一代 TUI exec 而来。original_tty 始终是第一代进程启动时的终端设置。
# 类用途: 记住原地切换需要跨 exec 带过去的会话编号、原始终端设置，以及新进程启动期间暂存的输出。
@dataclass
class HandoffState:
    child: bool = False
    session_id: str = ""
    original_tty: list | None = None
    quiet: tuple | None = None
    terminal_released: bool = False
    # exec 之后自己的安装仍等于切换前的安装（入口指向了别的解释器）：本进程不再原地切换，避免每隔几秒循环 exec。
    follow_disabled: bool = False


_STATE = HandoffState()


# LLM: Windows 的 os.execv 会另起进程并让父进程退出，终端会被两个进程同时读取，所以只在 POSIX 上原地切换。
# 函数用途: 判断当前平台能否在同一终端里原地换进程。
def handoff_supported() -> bool:
    return os.name != "nt"


# LLM: termios 的 cc 列表混有单字节 bytes 与整数（VMIN/VTIME），编码成 JSON 时 bytes 用十六进制字符串、整数原样保留。
# 函数用途: 把终端设置转成可放进环境变量的 JSON 结构。
def encode_tty(attrs: list | None) -> list | None:
    if not attrs or len(attrs) != 7:
        return None
    *flags, cc = attrs
    return [int(value) for value in flags] + [[item.hex() if isinstance(item, bytes) else int(item) for item in cc]]


# LLM: 结构不对就返回 None（新进程只是不做终端还原），绝不把畸形值交给 tcsetattr。
# 函数用途: 从 JSON 结构还原终端设置。
def decode_tty(value: object) -> list | None:
    if not isinstance(value, list) or len(value) != 7 or not isinstance(value[6], list):
        return None
    try:
        flags = [int(item) for item in value[:6]]
        cc = [bytes.fromhex(item) if isinstance(item, str) else int(item) for item in value[6]]
    except (TypeError, ValueError):
        return None
    return [*flags, cc]


def _capture_tty() -> list | None:
    try:
        import termios
    except ImportError:
        return None
    try:
        stdin = sys.__stdin__
        if stdin is None or not stdin.isatty():
            return None
        return termios.tcgetattr(stdin.fileno())
    except (termios.error, OSError, ValueError):
        return None


def _parse_payload(raw: str) -> dict | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("schema") != _HANDOFF_SCHEMA:
        return None
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        return None
    return payload


# LLM: cmd_chat 开头调用一次。原地切换来的新进程：弹出环境变量（不传给后代进程）、记下会话与原始终端设置，并注册退出兜底；
#   不在这里换掉标准输出——cmd_chat 还要用真实 stdout 判断是不是终端（2026-09-25 真机发现：先换成缓冲会让新进程误入 plain 模式卡在 input()）。
#   普通启动：只记下当前终端设置，供以后切换时带过去。
# 函数用途: 识别本进程是不是原地切换来的，并准备好会话编号与终端恢复信息。
def adopt_handoff() -> HandoffState:
    payload = _parse_payload(os.environ.pop(HANDOFF_ENV, ""))
    if payload is None:
        if _STATE.original_tty is None:
            _STATE.original_tty = _capture_tty()
        return _STATE
    _STATE.child = True
    _STATE.session_id = payload["session_id"]
    _STATE.original_tty = decode_tty(payload.get("tty"))
    from_prefix = payload.get("from_prefix")
    if isinstance(from_prefix, str) and from_prefix and os.path.realpath(from_prefix) == os.path.realpath(sys.prefix):
        _STATE.follow_disabled = True
    atexit.register(_restore_terminal_at_exit)
    return _STATE


# LLM: 必须在 cmd_chat 判断完“是否终端/是否用 TUI”之后调用。旧画面仍在屏幕上，启动期间的任何打印都会画花它，所以先暂存。
# 函数用途: 原地切换来的新进程在界面接管前暂存所有输出。
def hold_setup_output() -> None:
    if not _STATE.child or _STATE.quiet is not None or _STATE.terminal_released:
        return
    buffer = io.StringIO()
    _STATE.quiet = (sys.stdout, sys.stderr, buffer)
    sys.stdout = sys.stderr = buffer


# LLM: 原地切换来的进程却不能用 TUI（理论上不该发生）时立刻撤掉旧界面的终端模式，再按普通命令行继续。
# 函数用途: 把终端从上一代的全屏 raw 状态还原成 shell 能用的样子。
def drop_handoff_screen() -> None:
    if not _STATE.child or _STATE.terminal_released:
        return
    try:
        sys.stdout.write(_TERMINAL_RESET)
        sys.stdout.flush()
    except (OSError, ValueError, AttributeError):
        pass
    _restore_original_tty()
    _STATE.terminal_released = True


# LLM: 必须在创建 prompt_toolkit Application 之前调用（它要真实 stdout）；暂存的是“已恢复会话”之类的启动提示，旧画面还在，直接丢弃。
# 函数用途: 新界面即将接管屏幕时，恢复真实的标准输出。
def release_quiet_streams() -> None:
    quiet = _STATE.quiet
    if quiet is None:
        return
    sys.stdout, sys.stderr = quiet[0], quiet[1]
    _STATE.quiet = None


# LLM: prompt_toolkit 退出时会把终端设回它启动时看到的样子；原地切换来的进程看到的是上一代的 raw 模式，所以这里再还原成第一代保存的设置。
# 函数用途: 界面退出后把终端交还给 shell 时调用。
def terminal_released() -> None:
    _STATE.terminal_released = True
    if _STATE.child:
        _restore_original_tty()


def _restore_original_tty() -> None:
    attrs = _STATE.original_tty
    if not attrs:
        return
    try:
        import termios
    except ImportError:
        return
    try:
        termios.tcsetattr(sys.__stdin__.fileno(), termios.TCSANOW, attrs)
    except (termios.error, OSError, ValueError, AttributeError):
        pass


# LLM: 只在原地切换来的进程里注册；新界面没能接管终端就退出（启动失败、会话打不开）时撤掉旧界面的终端模式，再补打启动期间暂存的错误提示。
# 函数用途: 进程退出时的兜底，保证 shell 不会停在全屏或无回显状态。
def _restore_terminal_at_exit() -> None:
    if not _STATE.child:
        return
    quiet = _STATE.quiet
    if quiet is not None:
        sys.stdout, sys.stderr = quiet[0], quiet[1]
        _STATE.quiet = None
    if not _STATE.terminal_released:
        try:
            sys.stdout.write(_TERMINAL_RESET)
            sys.stdout.flush()
        except (OSError, ValueError, AttributeError):
            pass
        _restore_original_tty()
        _STATE.terminal_released = True
    text = quiet[2].getvalue() if quiet is not None else ""
    if text:
        try:
            sys.stderr.write(text)
            sys.stderr.flush()
        except (OSError, ValueError, AttributeError):
            pass


# LLM: 只在 UI 事件循环线程、两次复核空闲之后调用；成功时不返回。失败返回异常类型名，调用方保留旧界面并提示。
# 函数用途: 把会话编号和原始终端设置交给新进程，并在同一终端里 exec 成 Gateway 同版的 my-agent。
def exec_handoff(target: str, argv_tail: list[str], session_id: str) -> str:
    payload = {"schema": _HANDOFF_SCHEMA, "session_id": session_id, "tty": encode_tty(_STATE.original_tty),
               "from_prefix": sys.prefix}
    os.environ[HANDOFF_ENV] = json.dumps(payload, separators=(",", ":"))
    try:
        for stream in (sys.stdout, sys.__stdout__):
            try:
                stream.flush()
            except (OSError, ValueError, AttributeError):
                pass
        os.execv(target, [target, *argv_tail])
    except OSError as exc:
        os.environ.pop(HANDOFF_ENV, None)
        return type(exc).__name__
    return ""  # pragma: no cover - execv 成功不会返回


# LLM: 状态文件是 Gateway 进程写的权威投影；status 不是 running 或没有 runtime_prefix 时返回空串，调用方不得把空串当“同版”。
# 函数用途: 从 Gateway 状态载荷取出它所属安装的 runtime_prefix。
def gateway_runtime_prefix(state: dict | None) -> str:
    if not isinstance(state, dict) or str(state.get("status") or "") != "running":
        return ""
    return str(state.get("runtime_prefix") or "").strip()


# LLM: 目标是 Gateway 所在安装的 my-agent 入口文件，存在才返回；prefix 相同、开关关闭或入口不存在都返回空串（不切换）。
# 函数用途: 判断 TUI 是否需要换到 Gateway 同版客户端，并给出要 exec 的可执行文件路径。
def upgrade_restart_target(state: dict | None, own_prefix: str, *, enabled: bool) -> str:
    if not enabled:
        return ""
    prefix = gateway_runtime_prefix(state)
    if not prefix or os.path.realpath(prefix) == os.path.realpath(str(own_prefix)):
        return ""
    candidates = (Path(prefix) / "Scripts" / "my-agent.exe",) if os.name == "nt" else (Path(prefix) / "bin" / "my-agent",)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return ""


# LLM: 全部是结构化事实：运行/排队计数、待审批控制器、输入框文本与焦点、导航深度、是否跟随底部；不看屏幕文字。
# 类用途: 汇总判断“此刻切换不会打断用户”所需的界面状态。
@dataclass(frozen=True)
class TuiIdleFacts:
    is_running: bool
    pending_jobs: int
    input_text: str
    permission_active: bool
    navigation_depth: int
    following: bool = True
    input_focused: bool = True


# 函数用途: 只有没有进行中的工作、没有待审批、没在打字/看菜单/翻历史/看子代理时才允许原地切换。
def tui_idle_for_restart(facts: TuiIdleFacts) -> bool:
    return (not facts.is_running and int(facts.pending_jobs or 0) <= 0 and not str(facts.input_text or "").strip()
            and not facts.permission_active and int(facts.navigation_depth or 0) <= 0
            and facts.following and facts.input_focused)


# LLM: pending 表示已排到 UI 线程、尚未 exec；failed 表示本进程 exec 失败过，之后只保留旧界面，不再反复尝试。
# 类用途: 升级守护线程的输入与跨线程标志。
@dataclass
class UpgradeFollowContext:
    state_path: Path
    own_prefix: str
    enabled: bool
    stop_event: threading.Event
    is_idle: Callable[[], bool]
    set_notice: Callable[[str, float, str], None]
    request_handoff: Callable[[str], None] | None = None
    poll_seconds: float = POLL_SECONDS
    pending: bool = False
    failed: bool = False


# LLM: 单次检查：先算目标，再按空闲与否二选一（排队原地切换 / 限频提示）；pending 或 failed 时什么都不做。
# 函数用途: 读一次 Gateway 状态并决定是切换、提示还是保持不动。
def check_upgrade_once(ctx: UpgradeFollowContext, *, last_notice_at: float) -> tuple[str, float]:
    if ctx.pending or ctx.failed:
        return "", last_notice_at
    report = read_json_object_report(ctx.state_path, context="cli.tui_upgrade_follow.state.read")
    target = upgrade_restart_target(report.payload, ctx.own_prefix, enabled=ctx.enabled)
    if not target:
        return "", last_notice_at
    if handoff_supported() and ctx.request_handoff is not None and ctx.is_idle():
        ctx.pending = True
        ctx.request_handoff(target)
        return "handoff", last_notice_at
    now = time.monotonic()
    if now - last_notice_at >= _NOTICE_REPEAT_SECONDS:
        can_switch = handoff_supported() and ctx.request_handoff is not None
        ctx.set_notice(BUSY_NOTICE_TEXT if can_switch else REOPEN_NOTICE_TEXT, _NOTICE_SECONDS, NOTICE_KIND)
        last_notice_at = now
    return "notice", last_notice_at


# LLM: 守护线程只读文件、只调用注入的回调；stop_event 置位或切换失败即退出，永不自己 exec。
# 函数用途: 每隔几秒检查 Gateway 是否换了安装，空闲时触发原地切换。
def start_upgrade_follow_watcher(ctx: UpgradeFollowContext) -> threading.Thread:
    def body() -> None:
        last_notice_at = -_NOTICE_REPEAT_SECONDS
        while not ctx.stop_event.wait(ctx.poll_seconds) and not ctx.failed:
            try:
                _outcome, last_notice_at = check_upgrade_once(ctx, last_notice_at=last_notice_at)
            except (OSError, ValueError, TypeError):
                continue

    thread = threading.Thread(target=body, name="tui-upgrade-follow", daemon=True)
    thread.start()
    return thread


# LLM: 界面侧回调：exec 必须在 prompt_toolkit 事件循环线程上、两次渲染之间执行，才不会把半截转义序列留在终端里。
# 类用途: TUI 交给调度器的最小钩子集合。
@dataclass
class HandoffHooks:
    app: object
    is_idle: Callable[[], bool]
    stop_event: threading.Event
    set_notice: Callable[[str, float, str], None]
    clear_notice: Callable[[str], None]
    session_id: str
    argv_tail: list[str] = field(default_factory=list)


# LLM: 两段式：先在 UI 线程复核空闲并显示“正在切换”，留一帧渲染时间，再复核一次才 exec；任一次不空闲都撤回，交还守护线程稍后再试。
# 类用途: 把守护线程的切换请求安全地落到 UI 事件循环线程。
class HandoffScheduler:
    def __init__(self, hooks: HandoffHooks, ctx: UpgradeFollowContext) -> None:
        self.hooks, self.ctx = hooks, ctx

    # 函数用途: 守护线程调用；把切换排到 UI 事件循环，界面没在运行就撤回。
    def request(self, target: str) -> None:
        loop = getattr(self.hooks.app, "loop", None)
        if loop is None or not getattr(self.hooks.app, "is_running", False):
            self.ctx.pending = False
            return
        loop.call_soon_threadsafe(lambda: self._begin(target, loop))

    def _begin(self, target: str, loop) -> None:
        if self.hooks.stop_event.is_set() or not self.hooks.is_idle():
            self.ctx.pending = False
            return
        self.hooks.set_notice(SWITCHING_NOTICE_TEXT, 10.0, NOTICE_KIND)
        _invalidate(self.hooks.app)
        loop.call_later(_SWITCH_RENDER_DELAY_SECONDS, lambda: self._finish(target))

    def _finish(self, target: str) -> None:
        if self.hooks.stop_event.is_set() or not self.hooks.is_idle():
            self.hooks.clear_notice(NOTICE_KIND)
            self.ctx.pending = False
            _invalidate(self.hooks.app)
            return
        exec_handoff(target, self.hooks.argv_tail, self.hooks.session_id)
        self.ctx.failed, self.ctx.pending = True, False
        self.hooks.set_notice(FAILED_NOTICE_TEXT, 8.0, NOTICE_KIND)
        _invalidate(self.hooks.app)


def _invalidate(app: object) -> None:
    invalidate = getattr(app, "invalidate", None)
    if callable(invalidate):
        invalidate()


# LLM: TUI 在 worker 启动后调用一次；返回守护线程供测试等待。
# 函数用途: 组装升级守护线程与原地切换调度器并启动。
def start_tui_upgrade_follow(*, state_path: Path, own_prefix: str, enabled: bool, hooks: HandoffHooks) -> threading.Thread:
    ctx = UpgradeFollowContext(
        state_path=state_path, own_prefix=own_prefix, enabled=enabled, stop_event=hooks.stop_event,
        is_idle=hooks.is_idle, set_notice=hooks.set_notice, poll_seconds=POLL_SECONDS,
    )
    if not _STATE.follow_disabled:
        ctx.request_handoff = HandoffScheduler(hooks, ctx).request
    return start_upgrade_follow_watcher(ctx)


__all__ = [
    "HANDOFF_ENV",
    "HandoffHooks",
    "HandoffScheduler",
    "HandoffState",
    "NOTICE_KIND",
    "TuiIdleFacts",
    "UpgradeFollowContext",
    "adopt_handoff",
    "check_upgrade_once",
    "decode_tty",
    "encode_tty",
    "exec_handoff",
    "gateway_runtime_prefix",
    "drop_handoff_screen",
    "handoff_supported",
    "hold_setup_output",
    "release_quiet_streams",
    "start_tui_upgrade_follow",
    "start_upgrade_follow_watcher",
    "terminal_released",
    "tui_idle_for_restart",
    "upgrade_restart_target",
]
