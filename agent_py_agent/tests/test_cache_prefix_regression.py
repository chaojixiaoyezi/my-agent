"""DeepSeek 前缀缓存回归护栏（cachesim）：真实会话与工具循环 + 模拟前缀缓存。

为什么要有它：缓存按“(model, thinking 开关, reasoning_effort)”分区，换任何一个整段不命中；my-agent 曾经
因为“思考被误关”“压缩请求档位不一致”把命中率打掉。这里用真实的 Gateway 前台回合、真实的 openai_compatible
组包与工具循环，只把供应商传输换成模拟器（agent_py_agent/tests/cache_prefix_simulator.py），所以谁改了组包、
历史拼装或档位传递，命中率掉下来 CI 直接红。

判定只看结构化事实：模拟器记录的每次调用（分区、命中计量、是否被判 400）。已有未修复项用 strict xfail 标出
（原因写对应修复分支名），修复合入时 strict 会以 XPASS 失败，提醒去掉标记。
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import http
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
        if marker != self._last_marker:
            self._last_marker = marker
            self._tool_calls_in_turn[marker] = 0
        scripted = self._script_for(marker, self._tool_calls_in_turn.get(marker, 0))
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


# 函数用途: 造一个带工具调用的响应。
def _tool_call(payload: dict, name: str, arguments: dict) -> dict:
    return {"id": "chatcmpl-sim-tool", "object": "chat.completion", "created": 1,
            "model": str(payload.get("model") or ""),
            "choices": [{"index": 0, "finish_reason": "tool_calls",
                         "message": {"role": "assistant", "content": None, "tool_calls": [{
                             "id": f"call_sim_{name}", "type": "function",
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


@pytest.mark.xfail(strict=True, reason="压缩请求与主对话对齐未修：luna3（worker/cache-compact）修好后去掉本标记")
def test_compaction_call_reuses_the_conversation_history_prefix(tmp_path, monkeypatch):
    """压缩类辅助调用必须复用主对话的缓存前缀。

    官网实测（ds_probe4/5）：压缩类请求要复用主对话缓存，必须同分区、带同一套 tools、用 auto、
    system 和历史逐条相同（压缩指令放最后一条 user）；否则前缀在 system 之后分叉，命中为 0。
    现状（本分支基点对照实测）：压缩请求不带 reasoning_effort、不带历史，落在 `(model, enabled, default)`，
    prompt 只有 46 字符、命中 0 —— 这正是压缩命中率低的成因。修复方向在 luna3 手上（worker/cache-compact）；
    这里按已知未修标 strict xfail，修好会 XPASS 失败，提醒去掉标记。
    """
    chain, simulator, _scenario = _environment(tmp_path, monkeypatch)
    chain.ask("SIM", "SIM-TURN1 第一条：看一下工作区。")
    chat = [record for record in simulator.calls if not record.rejected]
    assert chat, "主对话没有产生模型调用，场景没跑起来"
    # “主对话历史前缀”取主对话已建立的最长前缀（最后一条请求的完整 prompt 计量）。
    history_prefix = max(record.prompt_tokens for record in chat)

    # 压缩调用走真实辅助模型入口；用调用前后的记录差识别它，不解析 payload 正文。
    from agent_py_agent.agent.conversation.auxiliary_model_call import (
        AuxiliaryModelCallRequest,
        generate_auxiliary_model_response,
    )
    before = len(simulator.calls)
    generate_auxiliary_model_response(AuxiliaryModelCallRequest(
        agent=chain.agent, prompt="把以上对话压缩成摘要。", purpose="compact",
        thread_id=chain.threads["SIM"], request_id="sim-compact", run_id="sim-compact-run",
    ))
    compact = simulator.calls[before:]
    assert compact, "压缩调用没有走辅助模型入口，场景没跑起来"
    assert all(not record.rejected for record in compact), "压缩请求被模拟服务端拒绝"

    chat_partitions = {record.partition for record in chat}
    assert {record.partition for record in compact} <= chat_partitions, (
        f"压缩调用落在不同分区：压缩 {[r.partition for r in compact]} vs 对话 {chat_partitions}")
    hit = max(record.hit_tokens for record in compact)
    assert hit >= CROSS_TURN_HIT_FLOOR * history_prefix, (
        f"压缩请求没有复用主对话历史前缀：命中 {hit}，主对话历史前缀 {history_prefix}；"
        "对齐后应当命中 system/tools/历史的绝大部分（≥90%）")
