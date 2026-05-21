# LLM: Main-agent large-log Live Lab helpers keep complex cases thin.
# 模块用途: 生成 100MB 日志 fixture，并验证主代理大文件审计产物。

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

LOG_SIZE_BYTES = 100 * 1024 * 1024


# LLM: _LargeLogMarker makes seeded large-log findings data-driven instead of nested writer logic.
# 类用途: 描述一条要插入大日志的错误线索，以及它应出现的大致字节位置。
@dataclass(frozen=True)
class _LargeLogMarker:
    offset: int
    line: bytes


# LLM: main_large_log_prompt asks for evidence-first auditing of a large file.
# 函数用途: 生成 100MB 日志审计提示词，要求主代理找线索并外置写报告。
def main_large_log_prompt() -> str:
    return textwrap.dedent(
        """
        这次你自己完成，不要派小傻妞。

        logs/huge_app.log 是一个很大的日志文件。请不要把日志全文复制到回复里。
        你要帮我找里面最重要的异常线索，重点关注付款、购物车、超时、trace id。
        最终把审计结果写到 lab_outputs/large-log-audit/report.md。

        报告里要包含：发现了哪些问题、关键证据、可能影响、建议怎么排查。
        """
    ).strip()


# LLM: seed_large_log creates deterministic large evidence without relying on external files.
# 函数用途: 生成约 100MB 的日志文件，并把关键错误放在不同位置，测试搜索和审计能力。
def seed_large_log(path: Path) -> None:
    if path.exists() and path.stat().st_size >= LOG_SIZE_BYTES:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    markers = _large_log_markers()
    written = 0
    with path.open("wb") as fh:
        for marker in markers:
            written = _write_until_large_log_offset(fh, written, marker.offset)
            fh.write(marker.line)
            written += len(marker.line)
        _write_until_large_log_offset(fh, written, LOG_SIZE_BYTES)


# LLM: assert_large_log_report validates that the audit found seeded high-signal failures.
# 函数用途: 检查日志审计报告是否抓到付款、购物车和 trace 证据。
def assert_large_log_report(output: Path) -> None:
    if not output.exists():
        raise RuntimeError(f"大日志审计报告不存在: {output}")
    content = output.read_text(encoding="utf-8", errors="replace")
    lowered = content.lower()
    required = ["payment_timeout", "cart_stuck", "trace-9f42"]
    missing = [item for item in required if item not in lowered]
    if missing:
        raise RuntimeError(f"大日志审计报告缺少关键线索: {missing}")
    if len(content.strip()) < 200:
        raise RuntimeError("大日志审计报告过短，不足以说明影响和建议。")


# LLM: _large_log_chunk centralizes the repeated filler row for deterministic logs.
# 函数用途: 返回大日志填充块；真实关键信息由 marker 单独插入，方便测试定位。
def _large_log_chunk() -> bytes:
    return (
        "2026-05-18T10:00:00Z INFO service=shop trace=warmup status=ok message=normal checkout heartbeat\n"
        * 1024
    ).encode("utf-8")


# LLM: _write_until_large_log_offset keeps large-log seeding flat and easy to audit.
# 函数用途: 往日志里写普通填充块直到达到目标偏移，返回已写字节数。
def _write_until_large_log_offset(fh: BinaryIO, written: int, target: int) -> int:
    chunk = _large_log_chunk()
    while written < target:
        fh.write(chunk)
        written += len(chunk)
    return written


# LLM: _large_log_markers defines seeded failures as data.
# 函数用途: 返回固定错误线索及其大致插入位置，供审计 case 和验收口径共享。
def _large_log_markers() -> list[_LargeLogMarker]:
    return [
        _LargeLogMarker(
            LOG_SIZE_BYTES // 4,
            b"2026-05-18T10:17:42Z ERROR service=payment trace=trace-9f42 "
            b"code=PAYMENT_TIMEOUT message=payment provider timeout after 30s\n",
        ),
        _LargeLogMarker(
            LOG_SIZE_BYTES // 2,
            b"2026-05-18T10:31:05Z WARN service=cart trace=trace-cart-77 "
            b"code=CART_STUCK message=cart update retried 8 times\n",
        ),
        _LargeLogMarker(
            LOG_SIZE_BYTES * 3 // 4,
            b"2026-05-18T10:45:19Z ERROR service=checkout trace=trace-checkout-18 "
            b"code=ORDER_CONFIRMATION_DELAY message=order confirmation delayed\n",
        ),
    ]


__all__ = ["assert_large_log_report", "main_large_log_prompt", "seed_large_log"]
