"""分模型 USD 计价(审计 #19 之成本部分):token 之外按真实单价换算成钱,可审计、可跨模型对齐。

给上市公司多租户计费需可审计"公司 X 本月花多少钱";贵/便宜模型单价差几十倍,只有 token 数无法
换算成钱或跨模型对齐成本。本模块:按 USD / 百万 token 配单价(input/output 分开,主流模型差异大),
未知模型回退保守默认并告警(不静默当 0,不低估成本);单价可经 env AGENT_MODEL_PRICING 或
set_pricing() 覆盖(billing 关键场景由运维配精确价)。持久化 cost/token；Anthropic SDK
total_cost_usd。

注:这是 #19 成本闸里能独立落地的核心(表 + cost_usd);把成本累加到 run/tenant 并做 USD 双口径
预算熔断需接 admission/核心循环,与热路径 metrics 一并留专项。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok_usd: float   # 每百万输入 token 美元
    output_per_mtok_usd: float  # 每百万输出 token 美元


# 默认单价(USD / 1M tokens,公开 list price 量级,可被 env/set_pricing 覆盖)。键用模型 ID 前缀,
# 带日期后缀的完整 ID(claude-haiku-4-5-20251001)按最长前缀匹配。
_DEFAULT_PRICING: dict[str, ModelPrice] = {
    "claude-opus-4": ModelPrice(15.0, 75.0),
    "claude-sonnet-4": ModelPrice(3.0, 15.0),
    "claude-haiku-4": ModelPrice(1.0, 5.0),
    "claude-fable-5": ModelPrice(3.0, 15.0),
}
_DEFAULT_PRICE = ModelPrice(15.0, 75.0)  # 未知模型:保守按最贵档估,不低估成本
_OVERRIDES: dict[str, ModelPrice] = {}


def set_pricing(model: str, price: ModelPrice) -> None:
    """运维/配置覆盖某模型单价(billing 关键场景配精确价)。"""
    _OVERRIDES[(model or "").strip()] = price


def _pricing_table() -> dict[str, ModelPrice]:
    return {**_DEFAULT_PRICING, **_OVERRIDES}


def resolve_price(model: str) -> ModelPrice:
    """解析模型单价:精确 → 最长前缀(容带日期后缀的 ID)→ 保守默认(告警,不低估)。"""
    table = _pricing_table()
    key = (model or "").strip()
    if key in table:
        return table[key]
    candidates = [name for name in table if key.startswith(name)]
    if candidates:
        return table[max(candidates, key=len)]
    logger.warning("未知模型 '%s' 无配置单价,按保守默认计 USD 成本(不低估)", key)
    return _DEFAULT_PRICE


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """按 (模型, 输入/输出 token) 算 USD 成本。负值按 0;六位小数(分级以下精度)。"""
    price = resolve_price(model)
    cost = max(0, input_tokens) / 1_000_000 * price.input_per_mtok_usd
    cost += max(0, output_tokens) / 1_000_000 * price.output_per_mtok_usd
    return round(cost, 6)


def _apply_env_entry(entry: str) -> None:
    """解析一条 env 单价项 'model:input/output'(USD per Mtok),非法忽略并告警。"""
    name, _, spec = entry.partition(":")
    inp, _, outp = spec.partition("/")
    try:
        _OVERRIDES[name.strip()] = ModelPrice(float(inp), float(outp))
    except ValueError:
        logger.warning("忽略非法 AGENT_MODEL_PRICING 项: %r", entry)


def load_env_pricing() -> None:
    """从 env AGENT_MODEL_PRICING 加载单价覆盖,格式 'm1:in/out,m2:in/out'。导入时调一次。"""
    raw = os.environ.get("AGENT_MODEL_PRICING", "").strip()
    for entry in raw.split(",") if raw else []:
        if entry.strip():
            _apply_env_entry(entry.strip())


load_env_pricing()
