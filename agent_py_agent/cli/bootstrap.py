from __future__ import annotations

# LLM: This module is the dependency-light CLI bootstrap boundary. Keep imports here limited
# to the standard library so interactive commands can parse arguments and paint their first
# frame before importing the agent runtime, extensions, providers, or other command families.
# 模块用途: 提供命令行启动最早阶段所需的默认路径、终端编码设置和通用参数开关；这里不能
# 引入智能体、模型、插件等重模块，否则会重新拖慢 TUI 首屏。
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "agent_config.yaml"
DEFAULT_CAPABILITY_CONFIG = ROOT / "config" / "capability_config.yaml"


# LLM: Keep this parser helper runtime-free; chat and non-chat parsers share its exact option
# contract, so changing either flag requires updating every parser-focused test.
# 函数用途: 给支持恢复上下文的命令添加一对互斥开关，并用 None 表示沿用配置默认值。
def add_resume_context_switches(command) -> None:
    group = command.add_mutually_exclusive_group()
    group.add_argument(
        "--resume-context",
        dest="resume_context",
        action="store_true",
        help="本次请求临时启用恢复上下文注入",
    )
    group.add_argument(
        "--no-resume-context",
        dest="resume_context",
        action="store_false",
        help="本次请求临时关闭恢复上下文注入",
    )
    command.set_defaults(resume_context=None)


# LLM: This is the sole stdio reconfiguration entrypoint. It must remain safe before config
# loading and preserve replacement semantics for malformed output on every CLI path.
# 函数用途: 在命令解析和界面启动前把标准输入输出统一为 UTF-8，避免中文终端因编码报错退出。
def configure_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            _reconfigure_stdio_stream(stream_name, reconfigure)


# LLM: Failure to reconfigure one stream is observable but non-fatal; do not turn a terminal
# encoding mismatch into a CLI startup blocker.
# 函数用途: 单独设置一条标准流；失败时写入原始 stderr，同时允许程序继续启动。
def _reconfigure_stdio_stream(stream_name: str, reconfigure) -> None:
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except Exception as exc:
        print(
            f"stdio reconfigure failed stream={stream_name} error_code={type(exc).__name__} error={exc}",
            file=sys.__stderr__,
        )
