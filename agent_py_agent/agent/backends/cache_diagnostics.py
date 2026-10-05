# LLM: 此模块仅计算出站请求的不可逆摘要；不修改请求，不记录正文/密钥，不声称能观察服务端 KV 缓存。
#   0.3 起改用链式摘要：第 i 条的摘要把第 i−1 条的链值也算进去，所以任意位置被改写都会改变它之后所有检查点的链值；
#   固定间隔检查点按 index 对齐；长度变化缺少同范围末点时明确不可比，不能把未核实的末块说成纯追加。
# 模块用途: 区分模型/系统/工具/历史前缀变化，并按原因码累计次数，帮助解释缓存下降；相同前缀不等于服务端必命中。
from __future__ import annotations

import hashlib
import json

# 链式摘要每个检查点之间的消息条数：块越小，定位越准，但保存的检查点越多。
_CHAIN_BLOCK_SIZE_COUNT = 32
# 最多保存多少个检查点（含最后的链值）。512 条盲区来自旧实现“只散列前 512 条消息”，
# 现在按 32 条一块、最多 128 块覆盖 4096 条消息；再长的线程只保留最后 128 块，并在 partial 上说明。
_CHAIN_CHECKPOINT_LIMIT_COUNT = 128
# 覆盖上限（条）= 128 × 32；超过后检查点只保留尾部窗口，定位范围有限（链值仍包含前面所有消息）。
_CHAIN_COVERAGE_LIMIT_COUNT = _CHAIN_BLOCK_SIZE_COUNT * _CHAIN_CHECKPOINT_LIMIT_COUNT
# 会切换服务端缓存分区的选项名（DeepSeek 实测：思考开关与推理强度各自一份缓存，换一个就整段不命中）。
_PARTITION_OPTION_KEYS = {"thinking": "thinking_changed", "reasoning_effort": "reasoning_effort_changed",
                          "reasoning": "reasoning_effort_changed"}
# 单条选项摘要发生变化时用的通用原因码；分区选项走上面的专用码。
_OPTIONS_CHANGED_CODE = "options_changed"

# LLM: 原因码是持久化白名单的单一来源（model_metrics 的累计计数与投影都从这里取），新增/改名必须同步
#   诊断比较与投影，避免两处名单漂移；旧历史码也收下，否则旧记录的原因会被静默丢掉。
# 函数用途: 返回本模块对外报出的全部原因码，供持久化白名单同步（避免两处名单漂移）。
def change_codes() -> tuple[str, ...]:
    # 含 0.0/0.1/0.2 就已报出的历史码（history_prefix_changed / history_shortened / history_appended）：
    #   它们仍可能出现在旧记录里，白名单必须收下，否则累计计数会静默丢掉这些原因。
    return tuple(sorted({*("endpoint_changed", "model_changed", "system_changed", "tools_changed", _OPTIONS_CHANGED_CODE,
                           "history_prefix_changed", "history_shortened", "history_appended"),
                         *_PARTITION_OPTION_KEYS.values()}))


# LLM: JSON 序列化顺序与出站数据一致，不能排序掩盖真正的顺序变化；摘要不用于权限或控制。
# 函数用途: 为一个请求片段生成 SHA256，原内容仅在本次计算中使用。
def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


# LLM: 链值必须把上一条的链值算进去，否则改写中间某条只影响它自己的摘要，后面检查点看不出来。
# 函数用途: 计算“上一条链值 + 本条消息”的链式摘要。
def _chained_digest(previous_chain: str, message: object) -> str:
    return hashlib.sha256(f"{previous_chain}\0{_digest(message)}".encode()).hexdigest()


# LLM: 扫描每条消息的链值，按固定间隔抽稀并保留末点；链仍包含全历史，窗口只限制变化定位。
#   长度变化不能直接比较两个不同范围的末链值，调用方必须按共同 index 比较并检查末块是否可证。
# 函数用途: 对消息序列生成稀疏检查点列表（[{index, chain}]，index 是 1 起的消息序号）。
def _chain_checkpoints(rows: list) -> list[dict]:
    chain, checkpoints = _digest("request_surface.v2.seed"), []
    for position, row in enumerate(rows, start=1):
        chain = _chained_digest(chain, row)
        if position % _CHAIN_BLOCK_SIZE_COUNT == 0 or position == len(rows):
            checkpoints.append({"index": position, "chain": chain})
    if len(checkpoints) > _CHAIN_CHECKPOINT_LIMIT_COUNT:
        checkpoints = checkpoints[-_CHAIN_CHECKPOINT_LIMIT_COUNT:]
    return checkpoints


# LLM: 只保存固定数量的检查点摘要和组件摘要，链值仍扫描全部消息；快照 partial 说明检查点窗口被裁、定位有限。
#   比较层还会在末块缺证据时置 partial，不能把它读成“整段历史都稳定”；旧 partial 同样保守表示证明不完整。
# 函数用途: 在真正 HTTP 请求处提取诊断事实，兼容 chat/messages/responses 三种字段形态。
def request_surface(payload: dict, endpoint: str) -> dict[str, object]:
    messages = payload.get("messages", payload.get("input", []))
    rows = messages if isinstance(messages, list) else [messages]
    system = payload.get("system", payload.get("instructions", []))
    if not system:
        system = [row for row in rows if isinstance(row, dict) and row.get("role") in {"system", "developer"}]
    options = {key: value for key, value in payload.items() if key not in {"model", "messages", "input", "system", "instructions", "tools", "stream"}}
    checkpoints = _chain_checkpoints(rows)
    return {"schema": "request_surface.v2", "endpoint": _digest(endpoint), "model": _digest(payload.get("model")),
            "system": _digest(system), "tools": _digest(payload.get("tools", [])), "options": _digest(options),
            # 分区选项单独留摘要：只有这几个键会切换服务端缓存分区，混在 options 里看不出是换了分区。
            "partition_options": {key: _digest(options.get(key)) for key in sorted(_PARTITION_OPTION_KEYS) if key in options},
            "chain_checkpoints": checkpoints, "message_count": len(rows),
            "partial": len(rows) > _CHAIN_COVERAGE_LIMIT_COUNT}


# LLM: 检查点只能证明“该检查点及之前”的链值整体未变。窗口滚动会裁掉最前面的检查点，所以比较必须按
#   检查点自己的 index 对齐，不能按列表位置 zip（否则 4096→4097 的只追加会被误报成块 32 的改写）。
#   共享前缀只能报到“第一个不同检查点之前的那个相同检查点”为止：检查点不同只能定位到块，
#   不能证明块内更早的消息也相同，报多了就是虚报。
# 函数用途: 返回 (第一个链值不同的共同检查点序号, 它之前最后一个相同检查点的序号)。
def _divergence_point(previous: list, current: list) -> tuple[int | None, int]:
    old = {row.get("index"): row.get("chain") for row in previous
           if isinstance(row, dict) and isinstance(row.get("index"), int)}
    new = {row.get("index"): row.get("chain") for row in current
           if isinstance(row, dict) and isinstance(row.get("index"), int)}
    shared = 0
    for index in sorted(old.keys() & new.keys()):
        if old[index] != new[index]:
            return index, shared
        shared = index
    return None, shared


# LLM: 同 schema 也不保证长度变化后的末块可比；只有共同检查点已证明整段较短历史相同，才能报纯追加/截短。
#   已观察到的不同链值仍可证明改写；否则保留已证明前缀并返回不可比，不重算正文、不扩大快照。
# 函数用途: 返回历史原因、变化检查点、已证明前缀与可比标志，避免漏比末块却断言纯追加。
def _history_changes(previous: dict, current: dict) -> tuple[list[str], int | None, int, bool]:
    divergent, shared = _divergence_point(previous.get("chain_checkpoints") or [],
                                          current.get("chain_checkpoints") or [])
    if divergent is not None:
        return ["history_prefix_changed"], divergent, shared, True
    if shared < min(previous.get("message_count", 0), current.get("message_count", 0)):
        return [], None, shared, False
    if current.get("message_count", 0) < previous.get("message_count", 0):
        return ["history_shortened"], None, shared, True
    if current.get("message_count", 0) > previous.get("message_count", 0):
        return ["history_appended"], None, shared, True
    return [], None, shared, True


# LLM: 变化是客户端可证实事实，不是缓存失效的因果证明；首次调用和裁剪窗口外不伪造比较基线。
#   选项变化分成“会切换缓存分区的”与“其它”两类：thinking_changed / reasoning_effort_changed 说明整段前缀不再共享缓存，
#   其它选项仍只报 options_changed。历史变化用链式检查点定位：不同就报 history_prefix_changed 并带上第一个不同的块，
#   共享前缀只报到它之前的相同检查点（不虚报）。基线 schema 与本次不同（旧 v1 快照没有链式检查点）时历史不可比：
#   不报 history_*，用 comparable=False 说明无法比较。相同 schema 但较短末块未被共同检查点核实时也如此，
#   partial=True 表示历史证明不完整；组件/选项仍独立比较，不因历史不可比而隐去已证实的变化。
# 函数用途: 给诊断账本写简明原因码，不触发 Compact、重试或删历史。
def compare_request_surfaces(previous: dict, current: dict) -> dict[str, object]:
    if not previous:
        return {"baseline_available": False, "changes": [], "server_cache_state": "unknown"}
    comparable = previous.get("schema") == current.get("schema")
    changes = [key + "_changed" for key in ("endpoint", "model", "system", "tools") if previous.get(key) != current.get(key)]
    if comparable:
        changes.extend(_option_changes(previous, current))
        history, divergent, shared, comparable = _history_changes(previous, current)
    else:
        # 旧 v1 基线没有分区选项信息，只比较两边都有的整体 options；历史不可比，什么都不报。
        history, divergent, shared = [], None, 0
        if previous.get("options") != current.get("options"):
            changes.append(_OPTIONS_CHANGED_CODE)
    changes.extend(history)
    return {"baseline_available": True, "comparable": comparable, "changes": changes,
            "shared_message_prefix": shared, "changed_block_index": divergent,
            "partial": bool(previous.get("partial") or current.get("partial") or not comparable),
            "server_cache_state": "unknown"}


# LLM: 只按 payload 的结构化键比较，不解析正文；reasoning 与 reasoning_effort 是同一档位的两个键名，
#   用“键名集合 + 有效档位摘要”的签名比较，换写法与真换档位都只报一次（否则会重复计数）；
#   其余选项合并成 options_changed。
# 函数用途: 分出“换缓存分区”的选项变化与其它选项变化。
def _option_changes(previous: dict, current: dict) -> list[str]:
    old, new = previous.get("partition_options") or {}, current.get("partition_options") or {}
    changes = []
    if old.get("thinking") != new.get("thinking"):
        changes.append("thinking_changed")
    if _reasoning_signature(old) != _reasoning_signature(new):
        changes.append("reasoning_effort_changed")
    if previous.get("options") != current.get("options") and not changes:
        changes.append(_OPTIONS_CHANGED_CODE)
    return changes


# LLM: 签名 = 键名集合 + 有效档位摘要：reasoning=high 换成 reasoning_effort=high 是一次变化（换写法），
#   不能报两条，也不能因为 options 整体摘要不同而落进 options_changed 兜底；两个键都在时以规范键为准。
# 函数用途: 取一个请求的推理档位签名，供一次比较。
def _reasoning_signature(options: dict) -> tuple[tuple[str, ...], object]:
    keys = tuple(sorted(key for key in ("reasoning", "reasoning_effort") if key in options))
    return keys, _reasoning_digest(options)


# LLM: reasoning_effort 是规范键，reasoning 是旧别名；两个都在时以规范键为准，都缺时按“没有档位”处理。
# 函数用途: 取一个请求的推理档位摘要（归一化别名后的有效值）。
def _reasoning_digest(options: dict) -> object:
    if "reasoning_effort" in options:
        return options["reasoning_effort"]
    return options.get("reasoning")


# LLM: 诊断展示只保留已定义原因码和计数，不允许摘要、正文、URL 或密钥混入持久会话投影。
#   缺 block 键的旧记录按“没有定位信息”给 None，不猜测块号，也不让旧格式读失败；
#   comparable=False 表示版本不兼容或缺同范围末点、历史无法确定分类（旧记录缺该键按可比较读）；partial 保留证明不完整。
# 函数用途: 把最近一次请求比较写成可安全保存的诊断快照，重启后仍能解释最近缓存读数。
def public_cache_diagnostic(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not isinstance(value.get("baseline_available"), bool):
        return {}
    allowed = set(change_codes())
    changes = value.get("changes")
    count = value.get("shared_message_prefix")
    block = value.get("changed_block_index")
    return {
        "baseline_available": value["baseline_available"],
        "comparable": value.get("comparable") is not False,
        "changes": [item for item in changes if isinstance(item, str) and item in allowed][:8] if isinstance(changes, list) else [],
        "shared_message_prefix": max(0, count) if type(count) is int else 0,
        "changed_block_index": block if type(block) is int and block > 0 else None,
        "partial": value.get("partial") is True,
        "server_cache_state": "unknown",
    }
