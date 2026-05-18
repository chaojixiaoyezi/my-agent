"""LLM: tests for main-agent machine delivery closeout after tool execution.

给人看的解释：
这个文件专门验证主代理真实任务的"产物已合格就自动停机"能力，避免产物已经写好还继续跑到超时。
"""

import json
import tempfile
from pathlib import Path

from agent_py_agent.agent.backend import ModelResponse
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent


# LLM: _DeliveryContractBackend proves valid artifact delivery stops the loop without another model turn.
# 类用途: 第一轮写出合同要求的 HTML；如果系统没自动收口，第二轮会让测试失败。
class _DeliveryContractBackend:
    name = "fake_delivery_contract_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"write_file","path":"outputs/furniture_homepage/index.html",'
                    '"content":"<!doctype html><html><body><a href=\\"#story\\">Story</a>'
                    '<section id=\\"story\\">Done</section></body></html>"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        raise AssertionError("delivery contract should close out before a second model call")


# LLM: _FailedDeliveryContractBackend proves failed machine acceptance feeds repair instead of false closeout.
# 类用途: 第一轮写出带占位链接的 HTML；第二轮检查系统把结构化验收失败交还给模型。
class _FailedDeliveryContractBackend:
    name = "fake_failed_delivery_contract_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"write_file","path":"outputs/furniture_homepage/index.html",'
                    '"content":"<!doctype html><html><body><a href=\\"#\\">Bad</a></body></html>"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        assert "delivery-contract-check" in prompt
        assert "HTML_PLACEHOLDER_LINK" in prompt
        return ModelResponse(text="已收到结构化修复反馈。", backend=self.name)


# LLM: _delivery_contract_prompt renders the same structured marker used by real task execution.
# 函数用途: 构造带机器交付合同的用户 prompt；自然语言部分不作为完成事实来源。
def _delivery_contract_prompt() -> str:
    contract = {
        "case_id": "furniture_homepage_html",
        "artifacts": [
            {
                "artifact_id": "homepage_html",
                "kind": "html",
                "preferred_path": "outputs/furniture_homepage/index.html",
                "required": True,
                "validation_contract": {"validator": "artifact_acceptance"},
            }
        ],
    }
    return "\n\n".join(
        [
            "用单文件 html 做一个高端家具品牌首页。",
            "MACHINE_DELIVERY_CONTRACT_JSON:",
            json.dumps(contract, ensure_ascii=False, indent=2),
        ]
    )


# LLM: Real-task delivery contracts should stop successful runs before extra model turns.
# 函数用途: 验证产物按机器合同验收通过后，主代理工具循环直接收口，不继续读写直到超时。
def test_tool_loop_closes_out_after_delivery_contract_passes():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=5)
        agent = SimpleAgent(cfg, workspace)
        backend = _DeliveryContractBackend()
        agent.backend = backend

        result = agent.run(_delivery_contract_prompt(), save=False)

        assert backend.calls == 1
        assert result.tool_rounds == 1
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert (workspace / "outputs/furniture_homepage/index.html").exists()
        assert (workspace / ".agent_delivery/closeout.json").exists()


# LLM: Failed delivery contracts must not pretend the task is complete.
# 函数用途: 验证产物验收失败时不会输出完成标记，而是把结构化 finding 传给下一轮模型修复。
def test_tool_loop_does_not_close_out_when_delivery_contract_fails():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=5)
        agent = SimpleAgent(cfg, workspace)
        backend = _FailedDeliveryContractBackend()
        agent.backend = backend

        result = agent.run(_delivery_contract_prompt(), save=False)
        report = json.loads((workspace / ".agent_delivery/closeout.json").read_text(encoding="utf-8"))

        assert backend.calls == 2
        assert result.response == "已收到结构化修复反馈。"
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" not in result.response
        assert report["ok"] is False
        assert report["artifacts"][0]["acceptance_report"]["findings"][0]["code"] == "HTML_PLACEHOLDER_LINK"
