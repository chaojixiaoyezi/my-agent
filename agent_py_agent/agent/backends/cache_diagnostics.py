# LLM: 此模块仅计算出站请求的不可逆摘要；不修改请求，不记录正文/密钥，不声称能观察服务端 KV 缓存。
# 模块用途: 区分模型/系统/工具/历史前缀变化，帮助解释缓存下降；相同前缀不等于服务端必命中。
from __future__ import annotations

import hashlib
import json

_MESSAGE_HASH_LIMIT = 512


# LLM: JSON 序列化顺序与出站数据一致，不能排序掩盖真正的顺序变化；摘要不用于权限或控制。
# 函数用途: 为一个请求片段生成 SHA256，原内容仅在本次计算中使用。
def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


# LLM: 只保存固定数量的 message 摘要和组件摘要；超出上限明确 partial，不能推断完整前缀稳定。
# 函数用途: 在真正 HTTP 请求处提取诊断事实，兼容 chat/messages/responses 三种字段形态。
def request_surface(payload: dict, endpoint: str) -> dict[str, object]:
    messages = payload.get("messages", payload.get("input", []))
    rows = messages if isinstance(messages, list) else [messages]
    system = payload.get("system", payload.get("instructions", []))
    if not system:
        system = [row for row in rows if isinstance(row, dict) and row.get("role") in {"system", "developer"}]
    options = {key: value for key, value in payload.items() if key not in {"model", "messages", "input", "system", "instructions", "tools", "stream"}}
    return {"schema": "request_surface.v1", "endpoint": _digest(endpoint), "model": _digest(payload.get("model")),
            "system": _digest(system), "tools": _digest(payload.get("tools", [])), "options": _digest(options),
            "messages": [_digest(row) for row in rows[:_MESSAGE_HASH_LIMIT]], "message_count": len(rows),
            "partial": len(rows) > _MESSAGE_HASH_LIMIT}


# LLM: 变化是客户端可证实事实，不是缓存失效的因果证明；首次调用和裁剪窗口外不伪造比较基线。
# 函数用途: 给诊断账本写简明原因码，不触发 Compact、重试或删历史。
def compare_request_surfaces(previous: dict, current: dict) -> dict[str, object]:
    if not previous:
        return {"baseline_available": False, "changes": [], "server_cache_state": "unknown"}
    changes = [key + "_changed" for key in ("endpoint", "model", "system", "tools", "options") if previous.get(key) != current.get(key)]
    old, new = previous.get("messages", []), current.get("messages", [])
    shared = 0
    for left, right in zip(old, new):
        if left != right:
            break
        shared += 1
    if shared < min(len(old), len(new)):
        changes.append("history_prefix_changed")
    elif current.get("message_count", 0) < previous.get("message_count", 0):
        changes.append("history_shortened")
    elif current.get("message_count", 0) > previous.get("message_count", 0):
        changes.append("history_appended")
    return {"baseline_available": True, "changes": changes, "shared_message_prefix": shared,
            "partial": bool(previous.get("partial") or current.get("partial")), "server_cache_state": "unknown"}


# LLM: 诊断展示只保留已定义原因码和计数，不允许摘要、正文、URL 或密钥混入持久会话投影。
# 函数用途: 把最近一次请求比较写成可安全保存的诊断快照，重启后仍能解释最近缓存读数。
def public_cache_diagnostic(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not isinstance(value.get("baseline_available"), bool):
        return {}
    allowed = {key + "_changed" for key in ("endpoint", "model", "system", "tools", "options")}
    allowed.update({"history_prefix_changed", "history_shortened", "history_appended"})
    changes = value.get("changes")
    count = value.get("shared_message_prefix")
    return {
        "baseline_available": value["baseline_available"],
        "changes": [item for item in changes if isinstance(item, str) and item in allowed][:8] if isinstance(changes, list) else [],
        "shared_message_prefix": min(_MESSAGE_HASH_LIMIT, max(0, count)) if type(count) is int else 0,
        "partial": value.get("partial") is True,
        "server_cache_state": "unknown",
    }
