from __future__ import annotations

"""统一文本协议工具块解析器（R0 #89）。

给人看的解释：
文本协议的 [TOOL_CALL]...[/TOOL_CALL] 块解析只有一个入口 scan_text_blocks：
- 最终裁决（tool_protocol_adapter 全文扫描）与
- 流式 UI 转发（ToolBoundaryChunkFilter 增量 feed）
都挂在这同一个确定性扫描器上，保证同一段文本无论怎么切分成网络 chunk，
解析出的块、违规、未闭合计数完全一致（等价性天然成立——裁决永远对完整
文本重扫，filter 只管 UI 转发什么）。IncrementalTextToolParser 是 J.3 的
「stream/final 共用同一解析器」落点：stream 的增量 feed 只是「累积 +
重扫 scan_text_blocks」的封装，结构判定（open/close/未闭合/坏块）没有
第二套实现。

安全红线（2026-08-09 用户复核；G5 2026-08-10 收紧为整轮零执行）：
任何协议错误 → 整轮零执行——未闭合（[/TOOL_CALL] 缺失）、截断/JSON
损坏、fence 包裹、控制块混排、超限，任一出现即「不完整响应」，本响应
所有 text calls 都没有执行权（J.5：terminal violation 时整轮无执行权；
G5 扩展到所有协议错误）。scan 仍提取全部块（候选与坏块原因），零执行
是裁决层（tool_protocol_adapter）的策略。
"""

import json
from dataclasses import dataclass
from typing import Any

_TEXT_OPEN = "[TOOL_CALL]"
_TEXT_CLOSE = "[/TOOL_CALL]"

# J-6 常量：文本协议防护上限。未闭合 open 数 > 1 的整轮拒绝规则与流式
# MalformedToolProtocolStreamAbort 对齐（边界层与裁决层同一条红线）。
MAX_UNCLOSED_OPEN_MARKERS = 1  # 未闭合 open 数 > 1 → 整轮拒绝（好块也不执行）
MAX_BLOCK_CHARS = 40_000  # 单个未闭合块体长度上限（finalize 级，仅未闭合块）
MAX_RESPONSE_CHARS = 200_000  # 整个响应文本上限（terminal，整轮拒绝）
MAX_TEXT_CALLS = 64  # 好块数量上限（G5：超限 → 整轮拒绝，不执行任何块）


@dataclass(frozen=True)
class ScannedTextBlock:
    open_index: int
    close_index: int | None  # None = 未闭合块（永不执行）
    body_chars: int
    payload: dict[str, Any] | None  # 好块：解析后的 payload；坏块 None
    error: str | None  # 坏块原因；好块 None


@dataclass(frozen=True)
class TextBlockScan:
    blocks: tuple[ScannedTextBlock, ...]
    unclosed_count: int

    @property
    def payloads(self) -> list[dict[str, Any]]:
        return [block.payload for block in self.blocks if block.payload is not None]

    @property
    def errors(self) -> list[str]:
        return [block.error for block in self.blocks if block.error is not None]


def scan_text_blocks(text: str) -> TextBlockScan:
    """确定性全文扫描 [TOOL_CALL] 块（adapter 语义）。

    - prose 容忍：plain find open，块与块之间的 prose 忽略；完全没有块标记
      = 普通文本回复（非违规）。
    - 字符串感知 close：[/TOOL_CALL] 只有当其前的块体 raw_decode 成完整 JSON
      对象（无残留）才算闭合；JSON 字符串里未闭合的 marker 文本被跳过。
    - 坏块记 error 不阻塞扫描（继续找后续块，候选完整）；整轮零执行由裁决层
      tool_protocol_adapter 决定（G5：任何协议错误 → 本响应所有块都不执行），
      scan 本身不裁。
    """
    if _TEXT_OPEN not in text and _TEXT_CLOSE not in text:
        return TextBlockScan((), 0)
    blocks: list[ScannedTextBlock] = []
    unclosed = 0
    cursor = 0
    length = len(text)
    while True:
        open_at = text.find(_TEXT_OPEN, cursor)
        if open_at < 0:
            break
        body_start = open_at + len(_TEXT_OPEN)
        close = _find_text_close(text, body_start)
        if close is None:
            # 未闭合 = 坏块，绝不执行；继续扫描后续块（坏块不阻塞扫描，
            # 整轮零执行由裁决层 tool_protocol_adapter 决定）。
            unclosed += 1
            blocks.append(
                ScannedTextBlock(
                    open_index=open_at,
                    close_index=None,
                    body_chars=len(text[body_start:]),
                    payload=None,
                    error="text tool block is not closed",
                )
            )
            cursor = body_start
            continue
        # 块被 markdown 围栏包裹（仅空白相隔）= 模型在展示示例而非发起调用，
        # 仍判违规；普通 prose 前缀（「我先看一下…」）宽容提取，这是弱模型
        # 真实输出形态。
        # 成对判定：open 前与 close 后（仅空白相隔）都有 ``` 才判"块被围栏
        # 包裹"。单侧相邻的 ``` 可能是相邻块的围栏（前一个块的闭围栏 / 后
        # 一个块的开围栏），不能连坐当前好块（pre-existing bug：混合用例
        # 把正常块的 close 后碰到的下一块围栏开误判成它自己违规）。
        before = open_at
        while before > 0 and text[before - 1].isspace():
            before -= 1
        after = close + len(_TEXT_CLOSE)
        while after < length and text[after].isspace():
            after += 1
        if (
            text[max(0, before - 3) : before] == "```"
            and text[after : after + 3] == "```"
        ):
            blocks.append(
                ScannedTextBlock(
                    open_at, close, close - body_start, None,
                    "text tool block must not be wrapped in Markdown fences",
                )
            )
            cursor = close + len(_TEXT_CLOSE)
            continue
        raw = text[body_start:close].strip()
        if not raw:
            blocks.append(
                ScannedTextBlock(
                    open_at, close, close - body_start, None,
                    "text tool block must contain raw JSON, not Markdown",
                )
            )
            cursor = close + len(_TEXT_CLOSE)
            continue
        # 不再做字符串级反引号检查：JSON 字符串值里反引号合法（Go raw string/
        # 正则/模板高频，如 write_file 写 Go 代码），字符串级误杀会砍掉合法调用
        # （真机铁证 2026-08-08 celery 复刻：补 broker.go 被"raw JSON, not
        # Markdown"连拦 3 轮 break）。真正的围栏包裹已由上方 before/after 检查
        # 捕获，块内 ```json 围栏由 json.loads 失败兜底。
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            blocks.append(
                ScannedTextBlock(
                    open_at, close, close - body_start, None,
                    "text tool block does not contain one valid JSON object",
                )
            )
            cursor = close + len(_TEXT_CLOSE)
            continue
        if not isinstance(payload, dict):
            blocks.append(
                ScannedTextBlock(
                    open_at, close, close - body_start, None,
                    "text tool block payload must be an object",
                )
            )
            cursor = close + len(_TEXT_CLOSE)
            continue
        tool_name = str(payload.get("tool") or "").strip()
        if not tool_name:
            blocks.append(
                ScannedTextBlock(
                    open_at, close, close - body_start, None,
                    "text tool block is missing tool name",
                )
            )
            cursor = close + len(_TEXT_CLOSE)
            continue
        blocks.append(
            ScannedTextBlock(
                open_index=open_at,
                close_index=close,
                body_chars=close - body_start,
                payload=dict(payload),
                error=None,
            )
        )
        cursor = close + len(_TEXT_CLOSE)
    return TextBlockScan(tuple(blocks), unclosed)


class IncrementalTextToolParser:
    """统一增量文本工具协议解析器（J.3：stream 与 final 共用同一实现）。

    扫描核心唯一：scan_text_blocks。final 裁决对完整文本调 complete(text)；
    stream 用 feed(chunk) 累积，每次 feed 后对累积文本重扫取结构视图（
    第一个块 open / 第一个完整块结束 / 未闭合计数 / 长写块体）。增量只是
    「累积 + 重扫」的封装——任意 chunk 切分与 one-shot 的解析结果完全
    一致（J.7 天然成立，因为裁决永远对完整文本重扫，视图只供 UI 转发与
    流式中止使用，不参与执行决策）。重扫成本对 200KB 上限响应可忽略。
    """

    def __init__(self) -> None:
        self._text = ""

    def feed(self, chunk: str) -> None:
        self._text += str(chunk or "")

    @property
    def text(self) -> str:
        return self._text

    def complete(self, text: str) -> TextBlockScan:
        """final 全文解析（= scan_text_blocks，唯一扫描实现）。"""
        return scan_text_blocks(str(text or ""))

    def scan(self) -> TextBlockScan:
        """对当前累积文本重扫（stream 视图的唯一结构来源）。"""
        return scan_text_blocks(self._text)

    def first_open_index(self) -> int | None:
        """第一个块 open 的位置；无任何块 → None（可见文本转发边界）。"""
        blocks = self.scan().blocks
        return blocks[0].open_index if blocks else None

    def next_close_at_or_after(self, cursor: int) -> int | None:
        """第一个 close_index >= cursor 的块位置；无 → None。

        与 first_complete_block_end 同一 scan，但按 cursor 过滤（长写
        上限判定用：open 之后是否已有有效 close）。
        """
        for block in self.scan().blocks:
            if block.close_index is not None and block.close_index >= cursor:
                return block.close_index
        return None


def _find_text_close(text: str, body_start: int) -> int | None:
    cursor = body_start
    while True:
        pos = text.find(_TEXT_CLOSE, cursor)
        if pos < 0:
            return None
        if _inline_json_tool_end_marker_valid(text, body_start, pos):
            return pos
        cursor = pos + len(_TEXT_CLOSE)


def _inline_json_tool_end_marker_valid(text: str, body_start: int, marker_pos: int) -> bool:
    """marker 前的块体已是完整 JSON 对象（无残留）才算真正闭合。

    与流式 boundary 的 close 判定共用同一实现，保证"未闭合"在流式中止与
    全文裁决两层语义一致（JSON 字符串里的 marker 文本在 JSON 不完整时被
    跳过，不会提前误判闭合）。
    """
    raw = text[body_start:marker_pos].strip().strip("`")
    if not raw:
        return False
    try:
        parsed, end = json.JSONDecoder().raw_decode(raw)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict) and not raw[end:].strip()


__all__ = [
    "IncrementalTextToolParser",
    "MAX_BLOCK_CHARS",
    "MAX_RESPONSE_CHARS",
    "MAX_TEXT_CALLS",
    "MAX_UNCLOSED_OPEN_MARKERS",
    "ScannedTextBlock",
    "TextBlockScan",
    "scan_text_blocks",
]
