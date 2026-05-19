"""LLM: tests for main-agent machine delivery closeout after tool execution.

给人看的解释：
这个文件专门验证主代理真实任务的"产物已合格就自动停机"能力，避免产物已经写好还继续跑到超时。
"""

import json
import re
import tempfile
from pathlib import Path

from agent_py_agent.agent.agent_core.runtime_loop_models import RunParams
from agent_py_agent.agent.backend import ModelResponse
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent


# LLM: _DeliveryContractBackend proves valid artifact delivery stops the loop without another model turn.
# 类用途: 第一轮写出合同要求的 HTML；如果系统没自动收口，第二轮会让测试失败。
class _DeliveryContractBackend:
    name = "fake_delivery_contract_backend"

    def __init__(self):
        self.calls = 0
        self.prompts = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.prompts.append(prompt)
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"write_file","path":"outputs/furniture_homepage/index.html",'
                    '"content":"<!doctype html><html><head><title>Maison</title></head><body>'
                    '<a href=\\"#story\\">Story</a>'
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


# LLM: _IncompleteDeliveryContractBackend reproduces a truncated HTML file that used to close out too early.
# 类用途: 写出半截单文件 HTML；第二轮确认系统返回机器验收失败而不是完成标记。
class _IncompleteDeliveryContractBackend:
    name = "fake_incomplete_delivery_contract_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"write_file","path":"outputs/furniture_homepage/index.html",'
                    '"content":"<!doctype html><html><head><link rel=\\"stylesheet\\" '
                    'href=\\"https://fonts.example/font.css\\"><style>body{color:#111}"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        assert "HTML_INCOMPLETE_DOCUMENT" in prompt
        assert "HTML_EXTERNAL_RESOURCE_REF" in prompt
        return ModelResponse(text="已收到不完整 HTML 的结构化反馈。", backend=self.name)


# LLM: _OpenWriteSessionDeliveryBackend creates a valid-looking artifact while leaving staged writes open.
# 类用途: 复现真实 E2E 中目录验收提前收口的问题；系统必须先处理 open file_write_session。
class _OpenWriteSessionDeliveryBackend:
    name = "fake_open_write_session_delivery_backend"

    def __init__(self):
        self.calls = 0
        self.saw_open_session_context = False
        self.session_id = ""

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        session_id = _session_id_from_prompt(prompt)
        if self.calls == 1:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"file_write_session","action":"begin",'
                    '"target_path":"outputs/shopping_site/app.js"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        if self.calls == 2:
            self.saw_open_session_context = True
            assert "delivery-contract-open-file-write-sessions" in prompt
            assert "open_file_write_sessions" in prompt
            self.session_id = session_id
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    f'{{"tool":"file_write_session","action":"append","session_id":"{session_id}",'
                    '"chunk_index":0,"content":"console.log(\\"shop ready\\");"}}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        if self.calls == 3:
            assert "delivery-contract-open-file-write-sessions" in prompt
            session_id = session_id or self.session_id
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    f'{{"tool":"file_write_session","action":"finish","session_id":"{session_id}"}}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        raise AssertionError("delivery should close out after open session is finished")


# LLM: _delivery_contract_prompt returns only user-visible task prose.
# 函数用途: 构造普通用户任务文本；机器合同由 RunParams.delivery_contract 传入。
def _delivery_contract_prompt() -> str:
    return "用单文件 html 做一个高端家具品牌首页。"


# LLM: _delivery_contract is the machine-only contract fixture shared by prompt and RunParams tests.
# 函数用途: 生成主代理交付收口需要的结构化合同；测试不从普通自然语言里推断产物要求。
def _delivery_contract() -> dict[str, object]:
    return {
        "case_id": "furniture_homepage_html",
        "artifacts": [
            {
                "artifact_id": "homepage_html",
                "kind": "html",
                "preferred_path": "outputs/furniture_homepage/index.html",
                "required": True,
                "validation_contract": {
                    "validator": "artifact_acceptance",
                    "quality_requirements": {
                        "complete_html_document": True,
                        "single_file_no_external_assets": True,
                    },
                },
            }
        ],
    }


# LLM: _web_project_delivery_contract validates directory artifacts through static_site_check.
# 函数用途: 生成目录型 Web 产物合同，要求 index.html 和 app.js 都真实存在。
def _web_project_delivery_contract() -> dict[str, object]:
    return {
        "case_id": "shopping_site_flow",
        "artifacts": [
            {
                "artifact_id": "shopping_site_root",
                "kind": "web_project",
                "preferred_path": "outputs/shopping_site",
                "required": True,
                "validation_contract": {
                    "validator": "static_site_check",
                    "required_files": ["index.html", "app.js"],
                },
            }
        ],
    }


# LLM: _session_id_from_prompt reads structured tool result JSON from the previous model/tool turn.
# 函数用途: 测试后端从 file_write_session begin 回执里取 session_id，不靠自然语言描述。
def _session_id_from_prompt(prompt: str) -> str:
    match = re.search(r'"session_id":\s*"([^"]+)"', prompt)
    return match.group(1) if match else ""


# LLM: Real-task delivery contracts should stop successful runs before extra model turns.
# 函数用途: 验证产物按机器合同验收通过后，主代理工具循环直接收口，不继续读写直到超时。
def test_tool_loop_closes_out_after_delivery_contract_passes():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=5)
        agent = SimpleAgent(cfg, workspace)
        backend = _DeliveryContractBackend()
        agent.backend = backend

        result = agent.run(
            _delivery_contract_prompt(),
            params=RunParams(delivery_contract=_delivery_contract(), save=False),
        )

        assert backend.calls == 1
        assert result.tool_rounds == 1
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert (workspace / "outputs/furniture_homepage/index.html").exists()
        assert (workspace / ".agent_delivery/closeout.json").exists()


# LLM: Delivery closeout must use RunParams contracts without requiring prompt markers.
# 函数用途: 验证系统交付合同可以通过结构化运行参数传入，不依赖 user_prompt 中的机器 JSON 标记。
def test_tool_loop_closes_out_from_structured_run_params_delivery_contract():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=5)
        agent = SimpleAgent(cfg, workspace)
        backend = _DeliveryContractBackend()
        agent.backend = backend

        result = agent.run(
            "用单文件 html 做一个高端家具品牌首页。",
            params=RunParams(delivery_contract=_delivery_contract(), save=False),
        )

        assert backend.calls == 1
        assert "outputs/furniture_homepage/index.html" in backend.prompts[0]
        assert "[tool-system delivery-contract]" in backend.prompts[0]
        assert "不得引用 http/https 外部" in backend.prompts[0]
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
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

        result = agent.run(
            _delivery_contract_prompt(),
            params=RunParams(delivery_contract=_delivery_contract(), save=False),
        )
        report = json.loads((workspace / ".agent_delivery/closeout.json").read_text(encoding="utf-8"))

        assert backend.calls == 2
        assert result.response == "已收到结构化修复反馈。"
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" not in result.response
        assert report["ok"] is False
        assert report["artifacts"][0]["acceptance_report"]["findings"][0]["code"] == "HTML_PLACEHOLDER_LINK"


# LLM: Incomplete contracted artifacts must not trigger delivery completion.
# 函数用途: 覆盖真实家具 E2E 中半截 HTML 被误收口的问题，要求 contract findings 进入下一轮。
def test_tool_loop_rejects_incomplete_delivery_contract_artifact():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=5)
        agent = SimpleAgent(cfg, workspace)
        backend = _IncompleteDeliveryContractBackend()
        agent.backend = backend

        result = agent.run(
            _delivery_contract_prompt(),
            params=RunParams(delivery_contract=_delivery_contract(), save=False),
        )
        report = json.loads((workspace / ".agent_delivery/closeout.json").read_text(encoding="utf-8"))
        codes = [item["code"] for item in report["artifacts"][0]["acceptance_report"]["findings"]]

        assert backend.calls == 2
        assert result.response == "已收到不完整 HTML 的结构化反馈。"
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" not in result.response
        assert {"HTML_INCOMPLETE_DOCUMENT", "HTML_EXTERNAL_RESOURCE_REF"} <= set(codes)


# LLM: Delivery closeout must not pass while any chunked write session remains open.
# 函数用途: 有 open file_write_session manifest 时，即使目录已存在也不能输出完成标记。
def test_tool_loop_delivery_closeout_blocks_open_file_write_sessions():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "outputs/shopping_site").mkdir(parents=True)
        (workspace / "outputs/shopping_site/index.html").write_text(
            "<!doctype html><html><body><main>Shop</main></body></html>",
            encoding="utf-8",
        )
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=3)
        agent = SimpleAgent(cfg, workspace)
        backend = _OpenWriteSessionDeliveryBackend()
        agent.backend = backend

        result = agent.run(
            "做一个购物网站。",
            params=RunParams(delivery_contract=_web_project_delivery_contract(), save=False),
            allowed_tools=["file_write_session"],
        )

        assert backend.calls == 3
        assert backend.saw_open_session_context is True
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert (workspace / "outputs/shopping_site/app.js").read_text(encoding="utf-8") == (
            'console.log("shop ready");'
        )
