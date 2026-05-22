# LLM: CLI delivery-contract helpers keep machine contracts out of user prompts.
# 模块用途: 读取 run 命令的结构化交付合同文件，供运行参数传递给主代理收口逻辑。

from __future__ import annotations

import json
import logging
from pathlib import Path

from agent_py_agent.agent.agent_core.delivery_contract_prompting import (
    DELIVERY_PREFLIGHT_FINDINGS_KEY,
    delivery_contract_preflight_findings,
)

LOGGER = logging.getLogger(__name__)


# LLM: delivery_contract_from_file loads JSON object contracts for RunParams.
# 函数用途: 读取 `--delivery-contract-file`，返回机器合同；缺失或坏 JSON 时不触发交付收口。
def delivery_contract_from_file(value: str) -> dict[str, object] | None:
    path_text = str(value or "").strip()
    if not path_text:
        return None
    try:
        payload = json.loads(Path(path_text).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("failed to read delivery contract: %s", exc)
        return None
    if not isinstance(payload, dict):
        LOGGER.warning("delivery contract is not a JSON object: %s", path_text)
        return None
    findings = delivery_contract_preflight_findings(payload)
    if findings:
        LOGGER.warning("delivery contract preflight findings: %s", [item.get("code") for item in findings])
        payload = dict(payload)
        payload[DELIVERY_PREFLIGHT_FINDINGS_KEY] = findings
    return payload


__all__ = ["delivery_contract_from_file"]
