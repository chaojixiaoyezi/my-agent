#!/usr/bin/env python3
# LLM: 本模块只把 PTY recorder 的 raw ANSI+offset 事件账重放为固定屏幕快照；不得把快照当成会话或业务事实源。
# 模块用途: 在 checkpoint、resize 和最终字节位置还原终端文本、样式区间、光标与标题，供 TUI golden/diff 使用。

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import pyte
except ImportError as exc:  # pragma: no cover - CLI 环境缺 dev extra 时给出明确入口错误
    raise RuntimeError("TUI ANSI snapshot 需要安装 dev extra 中的 pyte>=0.8.2,<0.9") from exc

SNAPSHOT_SCHEMA_VERSION = 1
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
STYLE_FIELDS = (
    "fg",
    "bg",
    "bold",
    "italics",
    "underscore",
    "strikethrough",
    "reverse",
    "blink",
)


# LLM: AnsiReplayConfig 固定原始证据、初始尺寸和输出位置；rows/cols 必须与 recorder manifest 对齐。
# 类用途: 汇总一次 ANSI 屏幕重放的参数。
@dataclass(frozen=True)
class AnsiReplayConfig:
    raw_path: Path
    events_path: Path
    output_dir: Path
    name: str
    rows: int
    cols: int

    # LLM: 校验在读大文件和创建产物前执行，避免路径组件注入与无效屏幕尺寸。
    # 函数用途: 拒绝不安全输出名和非正行列数。
    def __post_init__(self) -> None:
        if not SAFE_NAME_RE.fullmatch(str(self.name or "")):
            raise ValueError("snapshot name must be a safe filename component")
        if self.rows <= 0 or self.cols <= 0:
            raise ValueError("snapshot rows and cols must be positive")


# LLM: SnapshotArtifacts 返回公开产物位置和内存 payload，不隐藏写盘副作用。
# 类用途: 表示一次重放生成的 JSON、文本和结构化快照。
@dataclass(frozen=True)
class SnapshotArtifacts:
    json_path: Path
    text_path: Path
    payload: dict[str, Any]


# LLM: replay_ansi_session 严格按 recorder JSONL 顺序消费 raw offset；缺口、重叠和越界全部 fail-closed。
# 函数用途: 重放一轮 ANSI 并把每个 checkpoint 与最终屏幕写成私有证据文件。
def replay_ansi_session(config: AnsiReplayConfig) -> SnapshotArtifacts:
    raw = config.raw_path.read_bytes()
    events = _load_events(config.events_path)
    screen = pyte.Screen(config.cols, config.rows)
    stream = pyte.ByteStream(screen)
    checkpoints: list[dict[str, Any]] = []
    processed = 0
    for event in events:
        event_type = str(event.get("type") or "")
        if event_type == "output":
            processed = _feed_output_event(raw, event, processed, stream)
        elif event_type == "action":
            _apply_action_event(event, screen, checkpoints, processed)
    if processed != len(raw):
        raise ValueError(f"ANSI event ledger covers {processed} of {len(raw)} raw bytes")
    checkpoints.append(_screen_snapshot(screen, label="final", offset=processed, at_ms=_last_at_ms(events)))
    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "source": {
            "raw_file": config.raw_path.name,
            "events_file": config.events_path.name,
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "byte_count": len(raw),
            "initial_size": {"rows": config.rows, "cols": config.cols},
        },
        "checkpoints": checkpoints,
    }
    return _write_artifacts(config, payload)


# LLM: JSONL reader 只接受 object 行并保留顺序，不根据时间戳重排已经权威排序的 recorder 事件。
# 函数用途: 读取录制事件账并报告精确坏行。
def _load_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid ANSI event JSON at line {line_number}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"ANSI event line {line_number} must be an object")
        events.append(value)
    return events


# LLM: raw slice 必须恰好承接上一 output offset，禁止靠跳过/覆盖字节制造看似可用的屏幕。
# 函数用途: 验证并向 pyte 增量喂入一个 output 事件。
def _feed_output_event(raw: bytes, event: dict[str, Any], processed: int, stream: Any) -> int:
    offset = int(event.get("offset", -1))
    length = int(event.get("length", -1))
    end = offset + length
    if offset != processed or length < 0 or end > len(raw):
        raise ValueError(
            f"invalid ANSI output span offset={offset} length={length} processed={processed} raw={len(raw)}"
        )
    stream.feed(raw[offset:end])
    return end


# LLM: 只有 recorder 的 typed resize/checkpoint 动作影响重放状态，输入正文和未知 action 不进入快照元数据。
# 函数用途: 应用 resize 或冻结一个命名 checkpoint。
def _apply_action_event(
    event: dict[str, Any],
    screen: Any,
    checkpoints: list[dict[str, Any]],
    processed: int,
) -> None:
    offset = int(event.get("offset", processed))
    if offset != processed:
        raise ValueError(f"action offset {offset} does not match processed raw offset {processed}")
    action = str(event.get("action") or "")
    if action == "resize":
        rows = int(event.get("rows", 0))
        cols = int(event.get("cols", 0))
        if rows <= 0 or cols <= 0:
            raise ValueError("resize action requires positive rows and cols")
        screen.resize(lines=rows, columns=cols)
    elif action == "checkpoint":
        label = str(event.get("label") or f"checkpoint-{len(checkpoints) + 1}")
        if any(item["label"] == label for item in checkpoints):
            raise ValueError(f"duplicate ANSI checkpoint label: {label}")
        checkpoints.append(
            _screen_snapshot(screen, label=label, offset=processed, at_ms=int(event.get("at_ms", 0)))
        )


# LLM: 快照保持固定 rows 和每行 styled run 几何；plain text 仅右裁空格，不能替代样式验收。
# 函数用途: 从当前 pyte Screen 提取稳定 JSON 结构。
def _screen_snapshot(screen: Any, *, label: str, offset: int, at_ms: int) -> dict[str, Any]:
    lines: list[str] = []
    styled_lines: list[list[dict[str, Any]]] = []
    for row in range(screen.lines):
        cells = [screen.buffer[row][column] for column in range(screen.columns)]
        lines.append("".join(str(cell.data) for cell in cells).rstrip())
        styled_lines.append(_style_runs(cells))
    cursor = screen.cursor
    return {
        "label": label,
        "at_ms": max(0, int(at_ms)),
        "offset": max(0, int(offset)),
        "rows": int(screen.lines),
        "cols": int(screen.columns),
        "title": str(getattr(screen, "title", "") or ""),
        "icon_name": str(getattr(screen, "icon_name", "") or ""),
        "cursor": {"row": int(cursor.y), "col": int(cursor.x), "hidden": bool(cursor.hidden)},
        "lines": lines,
        "style_runs": styled_lines,
    }


# LLM: style run 压缩保留所有非默认背景空格的列范围，避免 plain rstrip 丢失用户块和 overlay 几何。
# 函数用途: 将一行 pyte Char 合并成连续同样式区间。
def _style_runs(cells: list[Any]) -> list[dict[str, Any]]:
    significant_end = _significant_cell_end(cells)
    if significant_end == 0:
        return []
    runs: list[dict[str, Any]] = []
    start = 0
    current_style = _cell_style(cells[0])
    for index in range(1, significant_end):
        style = _cell_style(cells[index])
        if style == current_style:
            continue
        runs.append(_style_run(cells, start, index, current_style))
        start = index
        current_style = style
    runs.append(_style_run(cells, start, significant_end, current_style))
    return runs


# LLM: 行尾显著性包括非空字符和非默认样式，默认空白不膨胀 golden。
# 函数用途: 找到需要保留的最后一个 cell 之后的位置。
def _significant_cell_end(cells: list[Any]) -> int:
    for index in range(len(cells) - 1, -1, -1):
        cell = cells[index]
        if str(cell.data) != " " or _cell_style(cell) != _default_style():
            return index + 1
    return 0


# LLM: style 字段列表固定为 pyte 公开 Char 属性，输出值转成 JSON 稳定标量。
# 函数用途: 提取一个 cell 的前景、背景和文本属性。
def _cell_style(cell: Any) -> dict[str, Any]:
    return {field: getattr(cell, field) for field in STYLE_FIELDS}


# LLM: 默认样式从明确常量构造，不能从首格推断，因为首格可能正好带背景或反色。
# 函数用途: 返回空白终端 cell 的标准样式。
def _default_style() -> dict[str, Any]:
    return {
        "fg": "default",
        "bg": "default",
        "bold": False,
        "italics": False,
        "underscore": False,
        "strikethrough": False,
        "reverse": False,
        "blink": False,
    }


# LLM: run 文本与 start/end 列共同保留宽度位置，不能只输出去空白字符串。
# 函数用途: 构造一个半开列区间的样式 run。
def _style_run(cells: list[Any], start: int, end: int, style: dict[str, Any]) -> dict[str, Any]:
    return {
        "start": start,
        "end": end,
        "text": "".join(str(cell.data) for cell in cells[start:end]),
        "style": style,
    }


# LLM: artifact writer 使用 0600 并同时生成机器 JSON 与固定行数人读文本，不覆盖 raw/source 证据。
# 函数用途: 写出 snapshot JSON 和屏幕文本。
def _write_artifacts(config: AnsiReplayConfig, payload: dict[str, Any]) -> SnapshotArtifacts:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = config.output_dir / f"{config.name}.screen.json"
    text_path = config.output_dir / f"{config.name}.screen.txt"
    _write_private_text(json_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    _write_private_text(text_path, _render_text(payload["checkpoints"]))
    return SnapshotArtifacts(json_path, text_path, payload)


# LLM: 人读文本保留每个 checkpoint 的完整固定高度，并显式写 label/offset/size 方便人工复核。
# 函数用途: 把结构化快照序列渲染为纯文本证据。
def _render_text(checkpoints: list[dict[str, Any]]) -> str:
    sections: list[str] = []
    for checkpoint in checkpoints:
        header = (
            f"### {checkpoint['label']} offset={checkpoint['offset']} "
            f"size={checkpoint['cols']}x{checkpoint['rows']}"
        )
        sections.append(header + "\n" + "\n".join(checkpoint["lines"]))
    return "\n\n".join(sections) + "\n"


# LLM: 私有文本写入在 open 时即设 0600，并对已存在文件再次收紧权限。
# 函数用途: 安全写入可能含测试 prompt/输出的 snapshot 证据。
def _write_private_text(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


# LLM: final 时间只取 recorder 最后一条结构化 at_ms，不读取 ANSI 文本中的时间。
# 函数用途: 返回事件账末端的非负毫秒时间。
def _last_at_ms(events: list[dict[str, Any]]) -> int:
    return max(0, int(events[-1].get("at_ms", 0))) if events else 0


# LLM: CLI parser 明确要求 raw/events/初始尺寸，避免从文件名猜测终端几何。
# 函数用途: 定义 ANSI snapshot 命令行参数。
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="将 TUI raw ANSI 重放为 checkpoint 屏幕快照")
    parser.add_argument("--raw", required=True, type=Path)
    parser.add_argument("--events", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--rows", required=True, type=int)
    parser.add_argument("--cols", required=True, type=int)
    return parser


# LLM: main 只把参数转成交付结构并打印两条产物路径，不在 stdout 回显屏幕正文。
# 函数用途: 执行命令行 ANSI 重放。
def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    artifacts = replay_ansi_session(
        AnsiReplayConfig(
            raw_path=args.raw,
            events_path=args.events,
            output_dir=args.output_dir,
            name=args.name,
            rows=args.rows,
            cols=args.cols,
        )
    )
    print(artifacts.json_path)
    print(artifacts.text_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
