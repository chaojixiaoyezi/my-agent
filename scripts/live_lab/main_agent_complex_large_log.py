
from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

LOG_SIZE_BYTES = 100 * 1024 * 1024


@dataclass(frozen=True)
class _LargeLogMarker:
    offset: int
    line: bytes


def main_large_log_prompt() -> str:
    return textwrap.dedent(
        """
        logs/huge_app.log 是一个很大的日志文件。请不要把日志全文复制到回复里。
        你要帮我找里面最重要的异常线索，重点关注付款、流程状态、超时、trace id。
        最终把审计结果写到 lab_outputs/large-log-audit/report.md。

        报告里要包含：发现了哪些问题、关键证据、可能影响、建议怎么排查。
        """
    ).strip()


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


def _large_log_chunk() -> bytes:
    return (
        "2026-05-18T10:00:00Z INFO service=shop trace=warmup status=ok message=normal checkout heartbeat\n"
        * 1024
    ).encode("utf-8")


def _write_until_large_log_offset(fh: BinaryIO, written: int, target: int) -> int:
    chunk = _large_log_chunk()
    while written < target:
        fh.write(chunk)
        written += len(chunk)
    return written


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
