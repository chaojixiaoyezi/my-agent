# LLM: bench 只从结构化工具意图与原用量账读取事实；选择、请求打开、实际执行三个概念不可混算。
# 模块用途: 裁决第一步包意图并生成分组分语言统计，不保存模型正文或工具参数正文。
from __future__ import annotations

from collections import defaultdict
from statistics import mean


# LLM: 只接受 native 的 name/input 结构，不解析自然语言或把 skill_id 当 package_id。
# 函数用途: 保留工具名、动作和包编号，剔除正文、路径和任意其它参数。
def tool_identities(calls: list[dict]) -> list[dict]:
    return [_tool_identity(call) for call in calls if isinstance(call, dict)]


# LLM: 未知字段一律不复制；action 只用于区分 get/search，不推断工具执行成功。
# 函数用途: 投影一个工具意图的最小身份。
def _tool_identity(call: dict) -> dict:
    arguments = call.get("input")
    arguments = arguments if isinstance(arguments, dict) else {}
    row = {"name": str(call.get("name") or "")}
    return {**row, **{key: arguments[key] for key in ("action", "package_id") if isinstance(arguments.get(key), str)}}


# LLM: 对口来自冻结题目的 positive_packs；误开记录任意非正例包，无关题正例为空仍能计算。
# 函数用途: 判断首次工具意图是否请求打开对口包，search 和错误选择器不算打开。
def judge(positive: list[str], calls: list[dict]) -> dict:
    tools = tool_identities(calls)
    opened = sorted({row["package_id"] for row in tools if row["name"] == "skill_search"
                     and row.get("action") == "get" and row.get("package_id")})
    return {"tools": tools, "opened_packs": opened, **judge_packages(positive, opened)}


# LLM: 同时选中对口和非对口时两项均真，不能用对口掩盖误选。
# 函数用途: 给一组包编号算对口和误开标志。
def judge_packages(positive: list[str], opened: list[str]) -> dict:
    return {"matched": bool(set(positive) & set(opened)), "misopened": bool(set(opened) - set(positive))}


# LLM: 缺报保持 None，不能把 preflight 估算或部分 usage 冒充供应商计费 token。
# 函数用途: 从同一请求的原 ModelCallRecord 读取每字段实际回报，HTTP 重试与错误另列。
def usage_fields(record: object) -> dict:
    names = {"input_tokens": "accounted_input_tokens", "output_tokens": "output_tokens",
             "cache_read_input_tokens": "cached_input_tokens", "cache_write_input_tokens": "cache_creation_input_tokens"}
    reported = getattr(record, "provider_usage_fields", None) or ()
    tokens = {name: getattr(record, attr) if name in reported else None for name, attr in names.items()}
    return {**tokens, "call_id": record.call_id, "status": record.status,
            "duration_seconds": record.total_latency_seconds, "provider_http_attempts": record.provider_attempt_count,
            "usage_reported_fields": list(reported), "error_code": record.error_code,
            "backend": record.backend, "model": record.model}


# LLM: 这是未调用的观察，不是供应商回报，未知不能填成零，也不能冒充 ModelCallRecord。
# 函数用途: 为未触发或失败的样本保留最小观察字段。
def observation_fields() -> dict:
    return {"tools": [], "opened_packs": [], "matched": False, "misopened": False, "call_id": None,
            "status": "not_called", "duration_seconds": None, "provider_http_attempts": 0,
            "input_tokens": None, "output_tokens": None, "cache_read_input_tokens": None,
            "cache_write_input_tokens": None, "usage_reported_fields": [], "error_code": "BENCH_NOT_CALLED"}


# LLM: 各观察点分表，C 选择不冒充主模型 get；失败进入分母但单列，不从平均数中静默丢弃。
# 函数用途: 汇总组、观察点和语言，未知 token 只在缺报计数里出现。
def summary(rows: list[dict]) -> str:
    groups = defaultdict(list)
    for row in rows:
        groups[(row["arm"], row["phase"], row["lang"])].append(row)
        groups[(row["arm"], row["phase"], "ALL")].append(row)
    lines = ["# 首次选包测量", "", "这里只记录选择或请求打开的意图，模型工具没有执行；假模型不证明自然召回。",
             "C 的 selection 与 main 分开；对口率分母为对口句，误开率分母为无关句。",
             "率按独立样本去重；平均值括号是已报告调用数/调用数，观察行不算物理调用。缺报为未知，不补零。",
             "HTTP 重试可能只报告末次 token；probe/decision/其它 auxiliary 独立列出，不算主 get。", "",
             "| 组 | 观察点 | 语言 | 对口率 | 误开率 | 平均输入 token | 平均缓存读 | 平均缓存写 | 平均输出 token | 失败数 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    lines.extend(_summary_row(key, group) for key, group in sorted(groups.items()))
    return "\n".join(lines) + "\n"


# LLM: 对口题里的误包也保留在 JSONL；本表误开率严格按任务要求只统计无关题。
# 函数用途: 渲染一个分组的明确分母、平均用量与失败数。
def _summary_row(key: tuple, rows: list[dict]) -> str:
    samples = list({(row.get("id", index), row.get("repeat"), row.get("thread_id")): row
                    for index, row in enumerate(rows)}.values())
    positives = [row for row in samples if row["positive_packs"]]
    unrelated = [row for row in samples if not row["positive_packs"]]
    rates = [_rate(sum(bool(row["matched"]) for row in positives), len(positives)),
             _rate(sum(bool(row["misopened"]) for row in unrelated), len(unrelated))]
    if key[1] not in {"main", "selection"}:
        rates = ["未适用", "未适用"]
    calls = [row for row in rows if row.get("record_kind") != "observation"]
    tokens = [_average(calls, name) for name in ("input_tokens", "cache_read_input_tokens", "cache_write_input_tokens", "output_tokens")]
    failures = str(sum(row["status"] not in {"ok", "finished"} or bool(row.get("selection_error")) and key[1] == "selection" for row in rows))
    return "| " + " | ".join([*key, *rates, *tokens, failures]) + " |"


# LLM: 零分母不能伪造百分率；计算与展示在同一个入口。
# 函数用途: 渲染成功数、总数及百分比。
def _rate(hits: int, total: int) -> str:
    return f"{hits}/{total} ({hits / total:.2%})" if total else "0/0 (未适用)"


# LLM: 仅取数字字段，None 不进平均；公开可复核有效分母。
# 函数用途: 渲染一个用量字段的均值和缺报覆盖。
def _average(rows: list[dict], key: str) -> str:
    values = [row[key] for row in rows if isinstance(row.get(key), (int, float))]
    return f"{mean(values):.2f} ({len(values)}/{len(rows)})" if values else f"未知 (0/{len(rows)})"
