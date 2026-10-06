"""DeepSeek 前缀缓存回归护栏（cachesim）：真实会话与工具循环 + 模拟前缀缓存。

为什么要有它：缓存按“(model, thinking 开关, reasoning_effort)”分区，换任何一个整段不命中；my-agent 曾经
因为“思考被误关”“压缩请求档位不一致”把命中率打掉。这里用真实的 Gateway 前台回合、真实的 openai_compatible
组包与工具循环，只把供应商传输换成模拟器（agent_py_agent/tests/cache_prefix_simulator.py），所以谁改了组包、
历史拼装或档位传递，命中率掉下来 CI 直接红。

判定只看结构化事实：模拟器记录的每次调用（分区、命中计量、是否被判 400）。压缩用例从真实 Gateway ask 触发 transcript Compact；异常路径只用 typed overflow 注入，供应商传输仍由缓存模拟器替换。
"""

from __future__ import annotations

import itertools
import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import http
from agent_py_agent.agent.backends.errors import ProviderContextWindowError
from agent_py_agent.agent.conversation import compact_request_budget
from agent_py_agent.agent.gateway_parts import request_context, request_execution
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.cache_prefix_simulator import (
    REASONING_REQUIRED_MESSAGE,
    PrefixCacheSimulator,
)
from agent_py_agent.tests.test_session_task_real_chain import (  # noqa: E402
    RealChain,
    _agent_config,
    _ready_gateway_paths,
    _text_of,
)

# 跨轮首调的最低命中率。理由：同一线程上一轮的全部内容（system/tools/历史）在同一分区里必然是本次请求的前缀，
# 只有“本轮新追加的 user 消息 + 新工具结果”是新增部分，通常占很小比例。取 90% 是留出小改动余量的保守线；
# 一旦组包顺序、system 提示或工具声明发生无谓变化，命中率会整块掉到 0 附近，远超这个余量。
CROSS_TURN_HIT_FLOOR = 0.90
# 单次场景的模型调用上限：脚本出招失效时会一直转，超过就明确报错而不是把测试挂死。
_CALL_GUARD_COUNT = 40


# LLM: 只按结构化标记出招（# User Task 里的标记），不解析自然语言；每次调用都交给模拟器记账。
# 类用途: 场景脚本 —— 按“本轮是第几次调用”决定返回工具调用还是最终答复。
class CacheScenarioWire:
    def __init__(self, simulator: PrefixCacheSimulator) -> None:
        self.simulator = simulator
        self.turns: list[dict] = []
        self.payloads: list[dict] = []
        self._overflowed_markers: set[str] = set()
        # 每轮已经发出过几次工具调用；用结构化标记定位“本轮”，避免跨轮累加导致永远不出最终答复。
        self._tool_calls_in_turn: dict[str, int] = {}
        self._last_marker = ""

    # 函数用途: 替换 backends.http.post_json：先按脚本出招，再交给模拟器记命中事实。
    def __call__(self, request) -> dict:
        payload = json.loads(json.dumps(request.payload))
        names = [str((row.get("function") or {}).get("name") or "") for row in payload.get("tools") or []]
        if names == ["my_agent_capability_probe"]:
            # 探针请求不参与缓存命中统计：它既不是对话前缀，也只在回合开头出现一次。
            # 回合开头的原生工具能力探针：必须回一个带同一 nonce 的结构化工具调用，
            # 否则本轮会被判“不支持原生工具调用”而根本跑不起来（与真实探针的成功条件一致）。
            nonce = re.search(r"nonce ([0-9a-f]+)", json.dumps(payload, ensure_ascii=False))
            return _tool_call(payload, "my_agent_capability_probe", {"nonce": nonce.group(1) if nonce else ""})
        if len(self.simulator.calls) >= _CALL_GUARD_COUNT:
            # 脚本出招没收敛（例如一直返工具调用）：明确失败，不把测试挂死。
            raise AssertionError(f"场景超过 {_CALL_GUARD_COUNT} 次模型调用仍未结束：{self.turns[-5:]}")
        texts = [_text_of(message.get("content")) for message in payload.get("messages") or []
                 if message.get("role") == "user"]
        marker = _last_marker(payload)
        if marker.startswith("SIM-COMPACT-NEXT") and marker not in self._overflowed_markers:
            self._overflowed_markers.add(marker)
            raise ProviderContextWindowError("测试触发的 Gateway 前台 overflow")
        if marker != self._last_marker:
            self._last_marker = marker
            self._tool_calls_in_turn[marker] = 0
        if (marker.startswith("SIM-ACTIVE") and self._tool_calls_in_turn.get(marker, 0) == 2
                and marker not in self._overflowed_markers):
            # 回合进行中（已做两次工具往返、还没有已结束历史）注入 typed overflow，逼原恢复链压当前回合的工具往返。
            self._overflowed_markers.add(marker)
            raise ProviderContextWindowError("测试触发的回合进行中 overflow")
        scripted = self._script_for(marker, self._tool_calls_in_turn.get(marker, 0))
        self.payloads.append(payload)
        response, _ = self.simulator.serve(payload)
        if "error" in response:
            return response
        if scripted.get("tool"):
            self._tool_calls_in_turn[marker] = self._tool_calls_in_turn.get(marker, 0) + 1
            self.turns.append({"marker": marker, "kind": "tool", "index": len(self.simulator.calls)})
            return _tool_call(payload, scripted["tool"], scripted["arguments"])
        self.turns.append({"marker": marker, "kind": "final", "index": len(self.simulator.calls),
                           "reasoning": bool(scripted.get("reasoning"))})
        return _completion(payload, scripted.get("text") or "SIM-ACK 收到。",
                           reasoning_content=scripted.get("reasoning"))

    # 函数用途: 按结构化标记决定这一轮返工具调用还是最终答复（含“最终答复没有思考内容”那一种）。
    def _script_for(self, marker: str, tool_calls_done: int) -> dict:
        if marker.startswith("SIM-COMPACT-SEED"):
            return {"text": "SIM-COMPACT-SEED-DONE", "reasoning": "种子回合的思考。"}
        if marker.startswith("SIM-ACTIVE"):
            if tool_calls_done < 2:
                return {"tool": "list_files", "arguments": {"path": "."}}
            return {"text": "SIM-ACTIVE-DONE 活动回合完成。", "reasoning": "活动回合的思考。"}
        if marker.startswith("SIM-TURN1"):
            if tool_calls_done == 0:
                return {"tool": "list_files", "arguments": {"path": "."}}
            return {"text": "SIM-TURN1-DONE 第一轮完成。", "reasoning": "第一轮思考。"}
        if marker.startswith("SIM-TURN2"):
            # 第二轮最终答复故意不带 reasoning_content：官网实测这种答复合法（不带 tool_calls），
            # 但它会让“查全部历史”的旧实现把之后所有请求都关思考 —— 那正是本护栏要抓的回退。
            if tool_calls_done == 0:
                return {"tool": "list_files", "arguments": {"path": "."}}
            return {"text": "SIM-TURN2-DONE 第二轮完成（本轮没有任何思考内容）。"}
        if marker.startswith("SIM-TURN3"):
            if tool_calls_done == 0:
                return {"tool": "list_files", "arguments": {"path": "."}}
            return {"text": "SIM-TURN3-DONE 第三轮完成。", "reasoning": "第三轮思考。"}
        return {"text": "SIM-ACK 收到。"}


# 函数用途: 取最后一条 user 消息里的结构化标记（# User Task 的正文首词）。
def _last_marker(payload: dict) -> str:
    for message in reversed(payload.get("messages") or []):
        if message.get("role") != "user":
            continue
        text = _text_of(message.get("content")).lstrip()
        if text.startswith("# User Task"):
            lines = text.split("\n")
            return lines[1].strip() if len(lines) > 1 else ""
    return ""


# 每次工具调用的序号：真实供应商每次调用给的编号都不同，脚本也要不同，否则同名调用会撞成同一个编号。
_TOOL_CALL_SEQUENCE = itertools.count(1)


# 函数用途: 造一个带工具调用的响应（调用编号逐次不同，与真实供应商一致）。
def _tool_call(payload: dict, name: str, arguments: dict) -> dict:
    return {"id": "chatcmpl-sim-tool", "object": "chat.completion", "created": 1,
            "model": str(payload.get("model") or ""),
            "choices": [{"index": 0, "finish_reason": "tool_calls",
                         "message": {"role": "assistant", "content": None, "tool_calls": [{
                             "id": f"call_sim_{name}_{next(_TOOL_CALL_SEQUENCE)}", "type": "function",
                             "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 10, "total_tokens": 1010}}


# 函数用途: 造一个纯文本响应，可选带 reasoning_content。
def _completion(payload: dict, text: str, *, reasoning_content: str | None = None) -> dict:
    message: dict = {"role": "assistant", "content": text}
    if reasoning_content:
        message["reasoning_content"] = reasoning_content
    return {"id": "chatcmpl-sim-text", "object": "chat.completion", "created": 1,
            "model": str(payload.get("model") or ""),
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 10, "total_tokens": 1010}}


# 函数用途: 真实环境：真 SimpleAgent、真 Gateway 请求目录，只把供应商传输换成模拟器。
def _environment(tmp_path: Path, monkeypatch) -> tuple[RealChain, PrefixCacheSimulator, CacheScenarioWire]:
    # 线程推理档位设成 max，与生产一致（官网线程都是 max）。
    from agent_py_agent.agent.core import SimpleAgent

    # 档位是全局默认 model_reasoning_effort（线程没有单独设置时回落到它，见 settings/reasoning_effort.py）。
    # 端点用真实 DeepSeek 主机名：思考开关只对已核对端点生效（_requires_thinking_disabled / _disable_thinking_for），
    # 换成别的地址就绕过了要护栏的那条分支。传输已被替换，不会真的联网。
    config = _agent_config(tmp_path, model_reasoning_effort="max")
    # 端点与模型名按真实 DeepSeek 形态设置：思考开关只对已核对端点生效，换成别的地址就绕过了要护栏的那条分支。
    config = replace(config, api_base="https://api.deepseek.com/v1", model_name="deepseek-v4-flash")
    agent = SimpleAgent(config, tmp_path / "root")
    simulator = PrefixCacheSimulator()
    scenario = CacheScenarioWire(simulator)
    monkeypatch.setattr(http, "post_json", scenario)
    # 复用既有真实链路的会话建立路径：必须先经真实 Gateway 预检开出一个渠道会话，
    # 否则第二次 ask 会因为会话/线程状态没准备好而阻塞（这是产品链路的既有前提，不是本用例的特殊要求）。
    chain = RealChain(agent, _ready_gateway_paths(agent), scenario, {}, scheduler=None)
    chain.open_session("SIM")
    return chain, simulator, scenario


# 函数用途: 每一轮第一次调用的位置：按脚本记录里每个新标记的首次出现切分（结构事实，不猜自然语言）。
def _turn_starts(turns: list[dict]) -> list[int]:
    starts, seen = [], set()
    for entry in turns:
        if entry["marker"] not in seen:
            seen.add(entry["marker"])
            starts.append(entry["index"])
    return starts


# 函数用途: 每轮第一次调用的命中率（供跨轮断言）。
def cross_turn_hit_ratios(simulator: PrefixCacheSimulator, turns: list[dict]) -> list[float]:
    starts = set(_turn_starts(turns))
    return [round(record.hit_tokens / record.prompt_tokens, 4)
            for record in simulator.calls if record.index in starts]


# LLM: 这是本文件的核心回归：三轮真实对话（每轮带工具调用、第二轮最终答复没有思考内容、中间插话、一次压缩），
#   在整个场景跑完后按结构化记录断言命中与分区。
# 函数用途: 端到端跑三轮回合并汇总模拟器事实。
def run_three_turn_scenario(tmp_path: Path, monkeypatch) -> dict:
    chain, simulator, scenario = _environment(tmp_path, monkeypatch)
    responses = []
    prompts = ("SIM-TURN1 第一条：看一下工作区。",
               # 中间插话：一条不带工具调用的短消息，模拟用户在轮间补一句。
               "SIM-TURN2 第二条：再看一眼。",
               "SIM-TURN3 第三条：最后确认一次。")
    for index, prompt in enumerate(prompts):
        if index == 1:
            # 插话走真实前台回合（同一条会话线程），它本身也算一次请求；
            # 只要它不切换分区、不改写已有前缀，就不该影响后续命中。
            responses.append(chain.ask("SIM", "SIM-ACK 插一句：先不用动手。"))
        responses.append(chain.ask("SIM", prompt))
    return {"chain": chain, "simulator": simulator, "scenario": scenario, "responses": responses}


def test_three_real_turns_keep_the_cache_prefix_hot(tmp_path, monkeypatch):
    """正向对照：跨轮第一次调用必须命中上一轮内容的 90% 以上，且整个场景里没有分区切换、没有被判 400。"""
    result = run_three_turn_scenario(tmp_path, monkeypatch)
    simulator = result["simulator"]
    assert not simulator.report()["rejected"], "有请求被判思考模式 400；这些请求在真实服务端也会被拒"

    turns = result["scenario"].turns
    assert len(simulator.calls) >= 6, f"场景没有真正跑起来：只有 {len(simulator.calls)} 次模型调用"
    ratios = cross_turn_hit_ratios(simulator, turns)
    assert len(ratios) >= 3, f"没有识别出三轮调用：每轮起点 {_turn_starts(turns)}"
    first_turn, later = ratios[0], ratios[1:]
    assert first_turn == 0.0, "第一轮没有任何缓存前缀可命中"
    assert min(later) >= CROSS_TURN_HIT_FLOOR, (
        f"跨轮首调命中率低于 {CROSS_TURN_HIT_FLOOR}：{ratios}；"
        "整块未命中通常来自 thinking/档位分区切换或历史前缀被改写")
    # 关键回归点：场景里第二轮的最终答复没有思考内容，之后每一轮的历史里都累积着这样的旧答复。
    # 只要“思考开关只看最后一条 user 之后”（当前实现），这些历史就不该把整条线程切到关思考分区；
    # 一旦退回“查全部历史”，后续请求会带 thinking={"type":"disabled"}，分区键就变了。
    missing = [record.history_missing_reasoning for record in simulator.calls]
    assert max(missing) >= 1, f"场景没有造出“缺思考的旧答复进历史”的条件：{missing}"
    disabled_calls = [record.index for record in simulator.calls if record.partition[1] == "disabled"]
    assert not disabled_calls, (
        f"历史里有缺思考的旧答复（最多 {max(missing)} 条）时，请求被切到了关思考分区：第 {disabled_calls} 次调用；"
        "这会整段未命中另一个缓存分区，并让模型从此不再思考")
    # 整个场景只应有一个分区；出现第二个分区就说明中途换了缓存分区。
    assert len(simulator.report()["partitions"]) == 1, f"出现了分区切换：{simulator.report()['partitions']}"


# LLM: 只观察 conversation_compact_summary 这一种摘要请求：记录请求对象和它在模拟器里的调用区间，原样转发不改行为。
# 函数用途: 给 Gateway 前台 Compact 的摘要入口挂观察点，返回（摘要请求列表，[(请求, 起始调用号, 结束调用号)]）。
def _observe_compact_requests(monkeypatch, simulator, purpose: str = "conversation_compact_summary") -> tuple[list, list]:
    compact_requests = []
    compact_request_call_ranges = []
    original_send = compact_request_budget._generate_auxiliary_with_retry

    def observe_compact_request(request):
        if request.purpose != purpose:
            return original_send(request)
        compact_requests.append(request)
        call_start = len(simulator.calls)
        try:
            return original_send(request)
        finally:
            compact_request_call_ranges.append((request, call_start, len(simulator.calls)))

    monkeypatch.setattr(compact_request_budget, "_generate_auxiliary_with_retry", observe_compact_request)
    return compact_requests, compact_request_call_ranges


# LLM: 失败信息带出 follow-up 后每次调用的分区/命中与出站形状，方便定位是没走 Compact 还是没对齐前缀；不读正文。
# 函数用途: 断言 follow-up 确实先进入 transcript Compact 摘要入口、再发送业务请求。
def _assert_compact_entered(scenario, compact_calls, compact_requests, calls_before_followup) -> None:
    assert compact_requests, (
        "真实 Gateway follow-up 未进入 transcript Compact 摘要入口；"
        f"calls={[(call.partition, call.rejected, call.hit_tokens, call.prompt_tokens, call.units) for call in compact_calls]}；"
        f"wire_shapes={[{'tool_count': len(payload.get('tools') or []), 'tool_choice': payload.get('tool_choice'), 'roles': [row.get('role') for row in payload.get('messages') or []]} for payload in scenario.payloads[calls_before_followup:]]}；"
        f"turns={scenario.turns[-4:]}"
    )
    assert len(compact_calls) >= 2, "follow-up 没有先做 Compact 再发送业务请求"


def test_compaction_call_reuses_the_conversation_history_prefix(tmp_path, monkeypatch):
    """真实 Gateway 前台 Compact 的摘要请求必须复用主请求分区与已提交历史前缀。"""
    chain, simulator, scenario = _environment(tmp_path, monkeypatch)
    compact_requests, compact_request_call_ranges = _observe_compact_requests(monkeypatch, simulator)
    seed_prompt = "SIM-COMPACT-SEED\n" + ("stable prior conversation text " * 3_000)
    chain.ask("SIM", seed_prompt)
    prior_chat = simulator.calls[-1]
    calls_before_followup = len(simulator.calls)

    chain.ask("SIM", "SIM-COMPACT-NEXT\n继续核对。")

    compact_calls = simulator.calls[calls_before_followup:]
    _assert_compact_entered(scenario, compact_calls, compact_requests, calls_before_followup)
    assert compact_request_call_ranges, "Compact 摘要请求没有对应的模拟器传输调用"
    observed_request, call_start, call_end = compact_request_call_ranges[0]
    assert observed_request is compact_requests[0]
    assert call_end > call_start, "Compact 摘要请求没有进入模拟器传输"
    assert call_start >= calls_before_followup
    assert simulator.calls[call_start] in compact_calls
    assert compact_requests[0].thread_id == chain.threads["SIM"]
    assert compact_requests[0].purpose == "conversation_compact_summary"
    compact_call = simulator.calls[call_start]
    assert not compact_call.rejected, "Gateway Compact 摘要请求被模拟服务端拒绝"
    assert compact_call.partition == prior_chat.partition, (
        f"Gateway Compact 落在不同分区：Compact {compact_call.partition} vs 主请求 {prior_chat.partition}")
    assert compact_call.hit_tokens >= CROSS_TURN_HIT_FLOOR * prior_chat.prompt_tokens, (
        f"Gateway Compact 未复用主请求前缀：命中 {compact_call.hit_tokens}，"
        f"主请求前缀 {prior_chat.prompt_tokens}；出站messages={len(scenario.payloads[calls_before_followup].get('messages') or [])}")
    assert not compact_calls[-1].rejected, "Compact 后重新发送的业务请求被模拟服务端拒绝"
    assert scenario.turns[-1]["marker"] == "SIM-COMPACT-NEXT"
    assert scenario.turns[-1]["kind"] == "final"


# LLM: 生产 10-05 统计：压缩类辅助调用 48 小时 DeepSeek 88 次、4290 万输入，命中 0%（GPT 同样 0%）。长任务回合里
#   多数压缩发生在回合进行中（没有已结束历史，压当前回合的工具往返），这条路此前发空工具目录 + none、历史只带上一代
#   摘要，与主请求从工具段就分叉。这里走真实 Gateway ask：两次工具往返后注入 typed overflow，由原恢复链做回合中压缩，
#   断言摘要请求与上一次主请求同分区、命中其前缀的 90% 以上，且恢复后的业务请求正常完成。
# 函数用途: 钉住回合中压缩的摘要请求复用正在运行的主请求前缀。
def test_active_turn_compaction_reuses_the_running_request_prefix(tmp_path, monkeypatch):
    chain, simulator, scenario = _environment(tmp_path, monkeypatch)
    live_requests, live_ranges = _observe_compact_requests(monkeypatch, simulator, purpose="compact_live_tool_summary")

    chain.ask("SIM", "SIM-ACTIVE\n看两次目录再收尾。")

    assert live_requests, (
        "回合中 overflow 没有进入活动回合压缩摘要入口；"
        f"calls={[(call.partition, call.rejected, call.hit_tokens, call.prompt_tokens) for call in simulator.calls]}；"
        f"turns={scenario.turns}")
    # 恢复链先发工具循环的缓存安全摘要，再发跨片归档摘要；两次都必须复用溢出前最后一次成功主请求的前缀。
    parent = simulator.calls[live_ranges[0][1] - 1]
    for observed, call_start, call_end in live_ranges:
        assert call_end > call_start, "活动回合摘要请求没有进入模拟器传输"
        summary_call = simulator.calls[call_start]
        assert not summary_call.rejected, "活动回合摘要请求被模拟服务端拒绝"
        assert summary_call.partition == parent.partition, (
            f"活动回合摘要落在不同分区：{summary_call.partition} vs 主请求 {parent.partition}")
        assert summary_call.hit_tokens >= CROSS_TURN_HIT_FLOOR * parent.prompt_tokens, (
            f"活动回合摘要（tool_choice={observed.tool_choice}）未复用主请求前缀："
            f"命中 {summary_call.hit_tokens}，主请求前缀 {parent.prompt_tokens}")
    assert scenario.turns[-1]["marker"].startswith("SIM-ACTIVE") and scenario.turns[-1]["kind"] == "final"
    assert not simulator.report()["rejected"]
