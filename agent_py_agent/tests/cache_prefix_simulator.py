"""DeepSeek 前缀缓存模拟器与假供应商传输（cachesim 回归护栏的测试辅助）。

为什么要有它：3a 官网实测确认缓存按“(model, thinking 开关, reasoning_effort)”分区，换任何一个整段不命中；
my-agent 曾经因为“思考被误关”“压缩请求档位不一致”把命中率打掉。修复（sol1 / luna3）以后，
需要一条端到端护栏：谁的改动让缓存命中变差，CI 直接红。

用法（只替换 backends.http.post_json，其余全走真实代码）：
    sim = PrefixCacheSimulator()
    monkeypatch.setattr(http, "post_json", sim.wire)
    ... 真实 Gateway ask / 工具循环 / 压缩 ...
    sim.report()   # 每次调用：分区、是否命中、命中量、是否被判 400

模拟规则（都来自 server_probe_facts.md，不读真实网络）：
- 分区键 = (model, thinking 分区, reasoning_effort 分区)；
- 命中量 = 同分区里以前请求的最长公共前缀（按 system → tools → messages 逐条比，按消息边界算，不真分词）；
- 思考模式下，最后一条 user 之后的 assistant 缺 reasoning_content → 回 400。
- 思考模式下 `tool_choice` 只接受 `auto` / `none`；`required` 或指定工具名 → 回 400（官网实测）；
- `tool_choice=none` 时服务端不渲染工具定义，前缀里没有 tools 那一段，等价于“不带 tools”
  （实测 prompt token 4029 对 4029，而 auto 是 4430），所以 none 请求的前缀在 system 之后就和 auto 请求分叉。

计量说明：用“规范化序列化后的字符数”当 token 近似值。选它是因为逐条比较本就按消息边界，字符数与真实
token 数在“同一段前缀”上高度单调；这里要判定的是命中断层（90% 阈值），不是精确计费。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# DeepSeek 官方 OpenAI 方言端点；只有这个主机名才按思考模式判 400。
DEEPSEEK_HOST = "api.deepseek.com"
# 思考模式必须回传 reasoning_content 的错误文案（官网原文，用于断言报文可辨识）。
REASONING_REQUIRED_MESSAGE = (
    "The `reasoning_content` in the thinking mode must be passed back to the API."
)
# 思考模式不支持的 tool_choice 的错误文案（官网原文，用于断言报文可辨识）。
TOOL_CHOICE_MESSAGE = "Thinking mode does not support this tool_choice"


# LLM: 思考模式下 tool_choice 只认 auto / none；required 或指定工具名会被服务端拒绝（官网 2026-10-04 实测）。
#   只读结构化字段：tool_choice 是字符串，或是带 mode/name 的对象。
# 函数用途: 判断这次请求的 tool_choice 是否会被思考模式拒绝。
def unsupported_tool_choice(payload: dict) -> bool:
    choice = payload.get("tool_choice")
    if choice is None or choice == "auto":
        return False
    if choice == "none":
        return False
    if isinstance(choice, dict):
        mode = str(choice.get("type") or choice.get("mode") or "").strip().lower()
        return mode not in ("auto", "none")
    return True


# LLM: none 时服务端不渲染工具定义，前缀里没有 tools；auto 时渲染。这条直接决定前缀在 system 之后是否分叉，
#   所以前缀序列必须按它决定要不要放 tools 那一段，不能一律带上。
# 函数用途: 判断这次请求的前缀里是否包含工具定义段。
def renders_tools(payload: dict) -> bool:
    return payload.get("tool_choice") != "none" and bool(payload.get("tools"))


# LLM: 分区只由三个结构化键决定，不做任何正文推断；与官网实测一致：不带档位、high、显式开思考同区。
# 函数用途: 由出站 payload 算出这次请求的缓存分区键。
def partition_key(payload: dict) -> tuple[str, str, str]:
    thinking = payload.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "disabled":
        thinking_part = "disabled"
    else:
        thinking_part = "enabled"
    effort = payload.get("reasoning_effort")
    # 不带档位与 high 同区；其余档位各自独立（low / max 等）。
    effort_part = "default" if effort in (None, "", "high") else str(effort)
    return str(payload.get("model") or ""), thinking_part, effort_part


# LLM: 前缀比较按 system → tools → messages 的规范化序列化逐条做，顺序不能排序掩盖；只按消息边界算就够了，
#   不需要真分词（这里判定的是“断层”，不是精确计费）。
# 函数用途: 把一次请求摊平成可逐条比较的前缀序列。
def prefix_units(payload: dict) -> list[str]:
    units = [json.dumps(payload.get("system"), ensure_ascii=False, sort_keys=False)]
    # none 时服务端不渲染工具定义：前缀里就没有 tools 这一条，等价于不带 tools 的请求。
    if renders_tools(payload):
        units.append(json.dumps(payload["tools"], ensure_ascii=False, sort_keys=False))
    units.extend(json.dumps(message, ensure_ascii=False, sort_keys=False) for message in payload.get("messages") or [])
    return units


# LLM: 命中量按字符数近似 token：同一段前缀上字符数与 token 数单调，够用来判定跨轮命中率阈值。
# 函数用途: 计算一个前缀序列的字符计量。
def measure(units: list[str]) -> int:
    return sum(len(unit) for unit in units)


# LLM: 最长公共前缀按逐条全等算；只要有一条不同就停在那里，不继续往后找“巧合相同”的条目。
# 函数用途: 返回两个前缀序列的公共条数与命中计量。
def longest_common_prefix(left: list[str], right: list[str], block: int = 512) -> int:
    total = 0
    for old, new in zip(left, right):
        if old != new:
            break
        total += len(old)
    return total


# LLM: 思考模式下“最后一条 user 之后”的 assistant 必须带 reasoning_content；这一段缺了就 400（官网实测），
#   最后一条 user 之前的不影响。用它抓“发了会被拒的请求”。
# 函数用途: 判断这次请求是否会被模拟服务端按思考模式拒绝。
def requires_reasoning_content(payload: dict) -> bool:
    messages = [message for message in payload.get("messages") or [] if isinstance(message, dict)]
    last_user = max((index for index, message in enumerate(messages) if message.get("role") == "user"), default=-1)
    for message in messages[last_user + 1:]:
        if message.get("role") != "assistant":
            continue
        if not str(message.get("reasoning_content") or "").strip():
            return True
    return False


# LLM: 每次调用记一行结构化事实（分区、命中、计量、是否 400）；不记正文，不联网。
# 类用途: 一次被模拟的供应商调用的可断言事实。
@dataclass
class CallRecord:
    index: int
    model: str
    partition: tuple[str, str, str]
    hit_tokens: int
    prompt_tokens: int
    reused_prefix_units: int
    units: int
    rejected: bool
    purpose: str = "chat"
    # 请求历史里“不带 reasoning_content 的 assistant 消息”条数（思考模式下这些会让旧实现整段关思考）。
    history_missing_reasoning: int = 0


# LLM: 分区表按分区键保存“以前请求的最长前缀”；同一分区的后续请求命中公共前缀，并把更长的前缀记进去。
# 类用途: 进程内的 DeepSeek 前缀缓存 + 思考模式校验，接在假传输层后面。
@dataclass
class PrefixCacheSimulator:
    partitions: dict[tuple[str, str, str], list[str]] = field(default_factory=dict)
    calls: list[CallRecord] = field(default_factory=list)
    _ids: list[int] = field(default_factory=list)

    # LLM: 只按结构化字段判定，不解析正文；被判 400 的请求不进命中表（服务端没接受它）。
    # 函数用途: 处理一次出站请求，返回 OpenAI 形状的响应或 400 错误体。
    def serve(self, payload: dict) -> tuple[dict, CallRecord]:
        index = len(self.calls) + 1
        key = partition_key(payload)
        units = prefix_units(payload)
        prompt_tokens = measure(units)
        # 两种 400 各自独立：tool_choice 不支持、缺 reasoning_content；任一命中都算这次请求没被接受。
        thinking_on = key[1] == "enabled"
        bad_choice = thinking_on and unsupported_tool_choice(payload)
        rejected = thinking_on and (bad_choice or requires_reasoning_content(payload))
        known = self.partitions.get(key, [])
        hit = 0 if rejected else longest_common_prefix(known, units)
        reused = 0 if rejected else _common_units(known, units)
        record = CallRecord(index, key[0], key, hit, prompt_tokens, reused, len(units), rejected,
                            str(payload.get("purpose") or "chat"))
        record.history_missing_reasoning = _history_missing_reasoning(payload)
        with_hit = units
        # 两次相同前缀不会让缓存变长；只有更长的时候才更新该分区。
        if not rejected and len(with_hit) > len(known):
            self.partitions[key] = with_hit
        if rejected:
            record.hit_tokens = 0
        self.calls.append(record)
        if rejected:
            return _error_response(payload, TOOL_CHOICE_MESSAGE if bad_choice else REASONING_REQUIRED_MESSAGE), record
        return _ok_response(payload, index), record

    # LLM: 只看结构化记录，不做因果推断；供用例写断言的只读视图。
    # 函数用途: 按分区归集的命中率与分区切换事实。
    def report(self) -> dict:
        by_partition: dict[tuple[str, str, str], list[CallRecord]] = {}
        for record in self.calls:
            by_partition.setdefault(record.partition, []).append(record)
        return {
            "calls": list(self.calls),
            "partitions": {key: len(rows) for key, rows in by_partition.items()},
            "rejected": [record.index for record in self.calls if record.rejected],
            "hit_ratio": [round(record.hit_tokens / record.prompt_tokens, 4) if record.prompt_tokens else 0.0
                          for record in self.calls],
        }

    # 函数用途: 替换 backends.http.post_json 的假传输层（探测请求照实回答，其余交给模拟器）。
    def wire(self, request) -> dict:
        payload = json.loads(json.dumps(request.payload))
        names = [str((row.get("function") or {}).get("name") or "") for row in payload.get("tools") or []]
        if names == ["my_agent_capability_probe"]:
            # 回合开头的原生工具能力探针：必须回一个带同一 nonce 的结构化工具调用，否则本轮会被判
            # “不支持原生工具调用”而根本跑不起来（与真实探针的成功条件一致）；探针不进命中统计。
            return _probe_response(payload, len(self.calls) + 1)
        response, _ = self.serve(payload)
        return response


# 函数用途: 两个前缀序列的公共条数（用于报告“复用了几条消息”）。
def _common_units(left: list[str], right: list[str]) -> int:
    count = 0
    for old, new in zip(left, right):
        if old != new:
            break
        count += 1
    return count


# LLM: 只数结构化字段：assistant 消息里 reasoning_content 为空/缺失的条数（不含本轮最后一段之后的），
#   用来验证“缺思考的旧答复”是否真的进了后续请求历史。
# 函数用途: 统计请求历史里不带思考内容的 assistant 消息条数。
def _history_missing_reasoning(payload: dict) -> int:
    messages = [message for message in payload.get("messages") or [] if isinstance(message, dict)]
    last_user = max((index for index, message in enumerate(messages) if message.get("role") == "user"), default=-1)
    return sum(1 for message in messages[:last_user + 1]
               if message.get("role") == "assistant" and not str(message.get("reasoning_content") or "").strip())


# 函数用途: 造一个 OpenAI 形状的成功响应（内容为空、无工具调用，由场景脚本按需替换）。
def _ok_response(payload: dict, index: int, text: str | None = None) -> dict:
    return {"id": f"chatcmpl-sim-{index}", "object": "chat.completion", "created": index,
            "model": str(payload.get("model") or ""),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 10, "total_tokens": 1010}}


# LLM: 探针的成功条件在 backends/http.py 里：响应要带一个 name=my_agent_capability_probe、input.nonce
#   与请求里 nonce 一致的结构化工具调用。只从结构化 payload 的文本里取 nonce，不执行任何内容。
# 函数用途: 造一个能让原生工具能力探针通过的工具调用响应。
def _probe_response(payload: dict, index: int) -> dict:
    match = re.search(r"nonce ([0-9a-f]+)", json.dumps(payload, ensure_ascii=False))
    nonce = match.group(1) if match else ""
    return {"id": f"chatcmpl-sim-probe-{index}", "object": "chat.completion", "created": index,
            "model": str(payload.get("model") or ""),
            "choices": [{"index": 0, "finish_reason": "tool_calls",
                         "message": {"role": "assistant", "content": None, "tool_calls": [{
                             "id": f"call_sim_probe_{index}", "type": "function",
                             "function": {"name": "my_agent_capability_probe",
                                          "arguments": json.dumps({"nonce": nonce}, ensure_ascii=False)}}]}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 10, "total_tokens": 1010}}


# 函数用途: 造模拟服务端的 400 错误体（形状贴近 OpenAI 错误响应）。
def _error_response(payload: dict, message: str) -> dict:
    return {"error": {"message": message, "type": "invalid_request_error", "code": "invalid_request_error"}}
