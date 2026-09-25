# LLM: TUI 随 Gateway 升级自动重启。判定只读 Gateway 状态文件里的结构化 runtime_prefix 与本进程 sys.prefix 比对，不解析任何文案；
#   只在客户端确实空闲（无运行中回合、无排队、无待审批、输入框为空、停在主视图）时把重启目标写进 restart_target_ref 并请求退出，
#   真正的 execv 由 cmd_chat 在界面收尾之后执行；非空闲只在 footer 提示。开关 tui_follow_gateway_upgrade（默认开）。
#   改动时同步 tests/test_tui_upgrade_follow.py、docs/design/TUI_DESIGN.md、CLI_REFERENCE.md 的 chat 段。
# 模块用途: 部署新版 Gateway 后，正在用的 TUI 自己挑空闲时换成同版客户端，用户不用手动关掉重开。
from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ...agent.common.json_io import read_json_object_report

POLL_SECONDS = 5.0
NOTICE_KIND = "gateway_upgrade"
NOTICE_TEXT = "Gateway 已升级到新版本，界面空闲时会自动重启到同版客户端。"
_NOTICE_SECONDS = 6.0
_NOTICE_REPEAT_SECONDS = 30.0


# LLM: 状态文件是 Gateway 进程写的权威投影；status 不是 running 或没有 runtime_prefix 时返回空串，调用方不得把空串当“同版”。
# 函数用途: 从 Gateway 状态载荷取出它所属安装的 runtime_prefix。
def gateway_runtime_prefix(state: dict | None) -> str:
    if not isinstance(state, dict) or str(state.get("status") or "") != "running":
        return ""
    return str(state.get("runtime_prefix") or "").strip()


# LLM: 目标是 Gateway 所在安装的 my-agent 入口文件，存在才返回；prefix 相同、开关关闭或入口不存在都返回空串（不重启）。
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


# LLM: 空闲判定只看结构化事实：运行/排队计数、待审批控制器、输入框文本、导航深度；不看屏幕文字。
# 函数用途: 判断此刻重启界面不会打断用户或正在进行的工作。
def tui_idle_for_restart(*, is_running: bool, pending_jobs: int, input_text: str,
                         permission_active: bool, navigation_depth: int) -> bool:
    return (not is_running and int(pending_jobs or 0) <= 0 and not str(input_text or "").strip()
            and not permission_active and int(navigation_depth or 0) <= 0)


@dataclass
class UpgradeFollowContext:
    """Watcher inputs: Gateway state path, own prefix, idle probes, exit request and the restart target slot."""

    state_path: Path
    own_prefix: str
    enabled: bool
    stop_event: threading.Event
    restart_target_ref: list
    is_idle: Callable[[], bool]
    request_exit: Callable[[], None]
    set_notice: Callable[[str, float, str], None]
    poll_seconds: float = POLL_SECONDS


# LLM: 单次检查是纯决策+副作用两步：先算目标，再按空闲与否二选一（请求退出 / 提示）。返回值给测试和调用方用，不重复读文件。
# 函数用途: 读一次 Gateway 状态并决定是重启、提示还是什么都不做。
def check_upgrade_once(ctx: UpgradeFollowContext, *, last_notice_at: float) -> tuple[str, float]:
    report = read_json_object_report(ctx.state_path, context="cli.tui_upgrade_follow.state.read")
    target = upgrade_restart_target(report.payload, ctx.own_prefix, enabled=ctx.enabled)
    if not target:
        return "", last_notice_at
    if ctx.is_idle():
        ctx.restart_target_ref[0] = target
        ctx.request_exit()
        return "restart", last_notice_at
    now = time.monotonic()
    if now - last_notice_at >= _NOTICE_REPEAT_SECONDS:
        ctx.set_notice(NOTICE_TEXT, _NOTICE_SECONDS, NOTICE_KIND)
        last_notice_at = now
    return "notice", last_notice_at


# LLM: 守护线程只读文件、只调用注入的回调；stop_event 置位即退出，永不自己 exec 或改终端。
# 函数用途: 每隔几秒检查 Gateway 是否换了安装，空闲时触发界面自我重启。
def start_upgrade_follow_watcher(ctx: UpgradeFollowContext) -> threading.Thread:
    def body() -> None:
        last_notice_at = -_NOTICE_REPEAT_SECONDS
        while not ctx.stop_event.wait(ctx.poll_seconds):
            try:
                outcome, last_notice_at = check_upgrade_once(ctx, last_notice_at=last_notice_at)
            except (OSError, ValueError, TypeError):
                continue
            if outcome == "restart":
                return

    thread = threading.Thread(target=body, name="tui-upgrade-follow", daemon=True)
    thread.start()
    return thread


# LLM: 只在 cmd_chat 界面完全收尾后调用；execv 成功不返回，失败要把提示留在终端并按普通退出码返回。
# 函数用途: 用 Gateway 同版的 my-agent 可执行文件、原命令行参数重启当前进程。
def reexec_into_target(target: str, argv_tail: list[str], *, print_line: Callable[[str], None] = print) -> int:
    print_line("Gateway 已升级，界面正在重启到同版客户端…")
    try:
        sys.stdout.flush()
        os.execv(target, [target, *argv_tail])
    except OSError as exc:
        print_line(f"自动重启失败（{type(exc).__name__}），请手动重新运行 my-agent。")
        return 0
    return 0  # pragma: no cover - execv 成功不会返回


__all__ = [
    "NOTICE_KIND",
    "POLL_SECONDS",
    "UpgradeFollowContext",
    "check_upgrade_once",
    "gateway_runtime_prefix",
    "reexec_into_target",
    "start_upgrade_follow_watcher",
    "tui_idle_for_restart",
    "upgrade_restart_target",
]
