#!/usr/bin/env python3
# LLM: 本脚本只观察 tmux pipe-pane 的原始字节和一次启动时间文件；不得向被测 TUI 注入按键或把显示文本当业务成功证据。
# 模块用途: 低开销测量终端程序从命令启动到指定首屏标记出现的毫秒数，供 My Agent、终端交互、会话运行时 使用同一口径比较。

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from pathlib import Path


# LLM: 探针配置只包含显式测试文件和显示字节；路径不存在或超时必须返回非零，不能沿用旧结果。
# 类用途: 汇总一次 TUI 启动观测所需的输入、输出和等待上限。
@dataclass(frozen=True)
class StartupProbeConfig:
    stream_path: Path
    start_path: Path
    result_path: Path
    needle: bytes
    timeout_seconds: float
    poll_seconds: float


# LLM: 主循环只比较 pipe 输出字节和 epoch 毫秒；它不解析 ANSI 状态、模型回复或 Gateway 业务状态。
# 函数用途: 等待启动时间和目标显示标记同时出现，写入耗时并返回是否成功。
def measure_startup(config: StartupProbeConfig) -> int:
    deadline = time.monotonic() + max(0.1, float(config.timeout_seconds))
    while time.monotonic() <= deadline:
        start_ms = _read_start_ms(config.start_path)
        if start_ms is not None and _stream_contains(config.stream_path, config.needle):
            elapsed_ms = max(0, time.time_ns() // 1_000_000 - start_ms)
            config.result_path.parent.mkdir(parents=True, exist_ok=True)
            config.result_path.write_text(str(elapsed_ms) + "\n", encoding="utf-8")
            print(elapsed_ms, flush=True)
            return 0
        time.sleep(max(0.005, float(config.poll_seconds)))
    return 2


# LLM: 空文件和非整数属于“尚未开始”，不能被解释为零毫秒启动。
# 函数用途: 读取 shell 在执行被测命令前写入的 epoch 毫秒。
def _read_start_ms(path: Path) -> int | None:
    try:
        value = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return value if value > 0 else None


# LLM: 原始输出按字节匹配以保留 ANSI/Unicode 真实性；读取错误只表示本拍未命中。
# 函数用途: 判断 pipe-pane 输出中是否已出现目标界面标记。
def _stream_contains(path: Path, needle: bytes) -> bool:
    if not needle:
        return False
    try:
        return needle in path.read_bytes()
    except OSError:
        return False


# LLM: CLI 参数必须显式指定三条证据路径和 UTF-8 标记，避免复用其它会话的残留文件。
# 函数用途: 解析命令行并执行一次启动观测。
def main() -> int:
    parser = argparse.ArgumentParser(description="测量 tmux TUI 输出标记的首次出现时间")
    parser.add_argument("--stream", required=True, type=Path)
    parser.add_argument("--start-file", required=True, type=Path)
    parser.add_argument("--result-file", required=True, type=Path)
    parser.add_argument("--needle", required=True)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--poll", type=float, default=0.02)
    args = parser.parse_args()
    return measure_startup(
        StartupProbeConfig(
            stream_path=args.stream,
            start_path=args.start_file,
            result_path=args.result_file,
            needle=args.needle.encode("utf-8"),
            timeout_seconds=args.timeout,
            poll_seconds=args.poll,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
