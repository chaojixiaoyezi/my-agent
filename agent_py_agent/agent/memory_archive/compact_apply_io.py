# LLM: Compact apply IO helpers; keep writes append-only/non-destructive and formatting stable.
# 模块用途: 为 compact apply 写入 JSON、Markdown 和 JSONL ledger，集中管理父目录创建和编码格式。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# LLM: append_jsonl is the only compact apply ledger append helper; do not rewrite existing rows here.
# 函数用途: 把一条 JSON 记录追加到 JSONL 文件，并自动创建父目录。
def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


# LLM: write_json keeps compact apply artifacts machine-readable and stable for resume tooling.
# 函数用途: 将字典写成带缩进的 UTF-8 JSON 文件，并自动创建父目录。
def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# LLM: write_text stores compact context markdown without changing any original memory source.
# 函数用途: 写入普通文本/Markdown 文件，并自动创建父目录。
def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


__all__ = ["append_jsonl", "write_json", "write_text"]
