# LLM: owner 级“按分词身份”的供应商校准比值缓存，唯一位置 home_paths.owner_context_calibration_json
#   （O/data/context/calibration.json）。它是线程观测（provider_context_observation）的派生缓存：线程与本轮观测优先，
#   这里只在两者都对不上时（新会话、刚换过模型的线程、压缩提交后、同进程稳定表面变化）给出比值，由 context_pressure 按
#   保守比例折算（不低于 50%），预检、压缩候选门与状态条用同一个数。键是跨进程稳定、不含凭据的分词身份摘要，值只有数字。
#   读坏、缺失、写失败都只退回原始估算，绝不让模型调用失败。改动同步 test_context_calibration_carry.py 与
#   docs/design/CONVERSATION_CONTEXT_DESIGN.md。
# 模块用途: 让同一个模型在新会话和压缩之后的第一次调用也能用上此前真实调用的“本地估算 / 供应商实际”比值。
from __future__ import annotations

import time
from pathlib import Path

from ...common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_json_file_atomic_unlocked,
)

SCHEMA = "owner_context_calibration.v1"
# 每个 owner 最多留这么多个分词身份（按最近观测保留），文件永远很小。
_MAX_ENTRIES = 32
# 本地估算低于这个量的调用不更新比值：问候、表达轮这类小请求的比值受固定开销影响大，会把大上下文的折算带偏。
MIN_CARRY_RAW_TOKENS = 4096


# LLM: 只认 home_paths 上的规范字段；没有 owner home（测试替身、无宿主调用）返回 None，调用方按“没有缓存”处理。
# 函数用途: 取得当前 owner 的校准比值缓存文件位置。
def owner_calibration_path(agent: object) -> Path | None:
    value = getattr(getattr(agent, "home_paths", None), "owner_context_calibration_json", None)
    return Path(value) if value else None


# LLM: 只读；schema 不符、数字不是正整数都视为没有。返回 {raw_estimated_tokens, provider_input_tokens, observed_at} 或 {}。
# 函数用途: 读出某个分词身份最近一次真实调用的“本地估算 / 供应商实际”。
def read_owner_ratio(agent: object, key: str) -> dict[str, object]:
    path = owner_calibration_path(agent)
    if path is None or not key:
        return {}
    payload = read_json_object_report(path, context="context_calibration_carry.read").payload
    entries = payload.get("entries") if payload.get("schema") == SCHEMA else None
    entry = entries.get(key) if isinstance(entries, dict) else None
    return _valid_entry(entry)


# LLM: 副作用：在 owner 文件锁内读改写 O/data/context/calibration.json（原子替换），超出上限按最旧观测淘汰。
#   小请求不写；任何读写错误都吞掉返回 False（这是遥测性质的派生缓存，不能让成功的模型调用变成失败）。
# 函数用途: 用一次成功调用的真实用量更新这个分词身份的校准比值。
def record_owner_ratio(agent: object, key: str, raw_tokens: int, provider_tokens: int) -> bool:
    path = owner_calibration_path(agent)
    if path is None or not key or raw_tokens < MIN_CARRY_RAW_TOKENS or provider_tokens <= 0:
        return False
    entry = {"raw_estimated_tokens": int(raw_tokens), "provider_input_tokens": int(provider_tokens), "observed_at": time.time()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with locked_json_path(path):
            payload = read_json_object_report(path, context="context_calibration_carry.write").payload
            entries = payload.get("entries") if payload.get("schema") == SCHEMA else None
            kept = {k: v for k, v in (entries or {}).items() if isinstance(k, str) and _valid_entry(v)}
            kept[key] = entry
            newest = sorted(kept.items(), key=lambda item: float(item[1]["observed_at"]), reverse=True)[:_MAX_ENTRIES]
            write_json_file_atomic_unlocked(path, {"schema": SCHEMA, "entries": dict(newest)})
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    return True


# 函数用途: 校验一条缓存记录，两个 token 数都必须是正整数（布尔不算），不合格返回空字典。
def _valid_entry(entry: object) -> dict[str, object]:
    if not isinstance(entry, dict):
        return {}
    raw, provider = entry.get("raw_estimated_tokens"), entry.get("provider_input_tokens")
    if not all(isinstance(value, int) and not isinstance(value, bool) and value > 0 for value in (raw, provider)):
        return {}
    observed = entry.get("observed_at")
    return {"raw_estimated_tokens": raw, "provider_input_tokens": provider,
            "observed_at": float(observed) if isinstance(observed, (int, float)) and not isinstance(observed, bool) else 0.0}


__all__ = ["MIN_CARRY_RAW_TOKENS", "SCHEMA", "owner_calibration_path", "read_owner_ratio", "record_owner_ratio"]
