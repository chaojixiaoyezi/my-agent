#!/usr/bin/env python3
from __future__ import annotations

"""LLM: thin executable entrypoint for the Live Lab harness.

给人看的解释：
这个文件只负责启动测试台。
真正的参数解析、运行编排和测试 case 都拆在 `scripts/live_lab/` 里，避免入口脚本变成大杂烩。
"""

from live_lab.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
