# LLM: CLI delivery-contract helpers keep machine contracts out of user prompts.
# 模块用途: 读取 run 命令的结构化交付合同文件，供运行参数传递给主代理收口逻辑。

from __future__ import annotations

import json
from pathlib import Path


# LLM: delivery_contract_from_file loads JSON object contracts for RunParams.
# 函数用途: 读取 `--delivery-contract-file`，返回机器合同；缺失或坏 JSON 时不触发交付收口。
def delivery_contract_from_file(value: str) -> dict[str, object] | None:
    path_text = str(value or "").strip()
    if not path_text:
        return None
    try:
        payload = json.loads(Path(path_text).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


__all__ = ["delivery_contract_from_file"]
