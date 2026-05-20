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
# 类用途: 第一轮写出不完整且带外部资源的 HTML；第二轮检查系统把结构化验收失败交还给模型。
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
                    '"content":"<!doctype html><html><head><link rel=\\"stylesheet\\" href=\\"https://fonts.example/font.css\\"></head><body><main>Bad</main>"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        assert "delivery-contract-check" in prompt
        assert "HTML_INCOMPLETE_DOCUMENT" in prompt
        assert "HTML_EXTERNAL_RESOURCE_REF" in prompt
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


# LLM: _NoProgressDeliveryBackend reproduces repeated identical delivery failures with no workspace progress.
# 类用途: 连续两轮都写出同一个坏 HTML；系统应结构化阻塞收口，而不是继续第三轮空转。
class _NoProgressDeliveryBackend:
    name = "fake_no_progress_delivery_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls <= 4:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"write_file","path":"outputs/furniture_homepage/index.html",'
                    '"content":"<!doctype html><html><head><link rel=\\"stylesheet\\" href=\\"https://fonts.example/font.css\\"></head><body><main>Bad</main>"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        raise AssertionError("delivery should block after repeated unchanged failure")


# LLM: _PendingTargetsDeliveryBackend proves multi-file work is not blocked before missing bootstrap targets are materialized.
# 类用途: 第一轮只写 index.html，第二轮只读检查，第三轮再补 app.js；系统不应在第二轮因为缺少结构化目标文件就误判卡死。
class _PendingTargetsDeliveryBackend:
    name = "fake_pending_targets_delivery_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"write_file","path":"outputs/shopping_site/index.html",'
                    '"content":"<!doctype html><html><body><script src=\\"app.js\\"></script></body></html>"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        if self.calls == 2:
            assert "pending_materialization_targets" in prompt
            assert "outputs/shopping_site/app.js" in prompt
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"read_file","path":"outputs/shopping_site/index.html"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        if self.calls == 3:
            assert "no_progress_block_threshold" in prompt
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"write_file","path":"outputs/shopping_site/app.js",'
                    '"content":"console.log(\\"shop ready\\");"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        raise AssertionError("pending targets should complete before any blocked closeout")


# LLM: _DeliveryRepairRedirectBackend proves write-first staged recovery ignores inspection-only tool calls until the required repair happens.
# 类用途: 第一轮写空骨架，第二轮故意只读检查；系统应把它拽回“先补非空结构化数据，再调 builder”的通用恢复链路。
class _DeliveryRepairRedirectBackend:
    name = "fake_delivery_repair_redirect_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"write_file","path":"outputs/github_star_growth/source_data.json",'
                    '"content":"{\\"generated_at\\":\\"2026-05-20\\",\\"weeks\\":[],\\"note\\":\\"数据收集中...\\"}"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        if self.calls == 2:
            assert "STAGED_JSON_NO_ROWS" in prompt
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"read_file","path":"outputs/github_star_growth/source_data.json"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        if self.calls == 3:
            assert "delivery-required-repair" in prompt
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"write_file","path":"outputs/github_star_growth/source_data.json",'
                    '"content":"{\\"sheets\\":[{\\"name\\":\\"周榜单\\",\\"rows\\":[{\\"项目名\\":\\"demo-1\\",\\"地址\\":\\"https://example.com/1\\",\\"上升 star 数\\":120,\\"中文解释\\":\\"说明1\\",\\"推荐理由\\":\\"理由1\\"}]},{\\"name\\":\\"汇总\\",\\"rows\\":[{\\"项目名\\":\\"demo-2\\",\\"地址\\":\\"https://example.com/2\\",\\"上升 star 数\\":110,\\"中文解释\\":\\"说明2\\",\\"推荐理由\\":\\"理由2\\"}]}]}"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        if self.calls == 4:
            assert "STAGING_BUILDER_READY" in prompt
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"data_to_workbook","source_json_path":"outputs/github_star_growth/source_data.json",'
                    '"path":"outputs/github_star_growth/github_star_growth.xlsx"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        raise AssertionError("delivery repair should redirect inspection-only turns and then complete")


# LLM: _BootstrapMaterializationRedirectBackend proves the startup guard redirects pure inspection loops before any target exists.
# 类用途: 第一轮只 list_files；系统应要求先物化一个 target，随后再允许继续正常交付。
class _BootstrapMaterializationRedirectBackend:
    name = "fake_bootstrap_materialization_redirect_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"list_files","path":"outputs/shopping_site"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        if self.calls == 2:
            assert "bootstrap-materialization" in prompt
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"write_file","path":"outputs/shopping_site/index.html",'
                    '"content":"<!doctype html><html><body><script src=\\"app.js\\"></script></body></html>"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        if self.calls == 3:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"write_file","path":"outputs/shopping_site/app.js",'
                    '"content":"console.log(\\"bootstrapped\\");"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        raise AssertionError("bootstrap guard should redirect inspection-only startup and then complete")


# LLM: _BootstrapMaterializationProgressiveBackend proves startup inspection gets multiple redirects before the guard escalates to a block.
# 类用途: 前三轮都只做目录检查；系统应继续引导先物化目标，不应第二轮就硬拦死。第四轮开始落文件后，流程应能正常完成。
class _BootstrapMaterializationProgressiveBackend:
    name = "fake_bootstrap_materialization_progressive_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls in {1, 2, 3}:
            if self.calls > 1:
                assert "bootstrap-materialization" in prompt
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"list_files","path":"outputs/shopping_site"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        if self.calls == 4:
            assert "exploration_rounds_without_materialization" in prompt
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"write_file","path":"outputs/shopping_site/index.html",'
                    '"content":"<!doctype html><html><body><script src=\\"app.js\\"></script></body></html>"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        if self.calls == 5:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"write_file","path":"outputs/shopping_site/app.js",'
                    '"content":"console.log(\\"bootstrapped later\\");"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        raise AssertionError("bootstrap guard should allow several redirects before any final block")


# LLM: _LocalProgressRedirectBackend proves repeated remote exploration gets redirected back to local staged work even when read_artifact is still technically allowed.
# 类用途: 第一轮写空结构化骨架，随后连续两轮只读 artifact；系统应通过通用 local-progress guard 把模型拽回本地补数据并调用 builder。
class _LocalProgressRedirectBackend:
    name = "fake_local_progress_redirect_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        artifact_ref = str(Path("memory_archive/artifacts/tool_outputs/demo.json").resolve())
        if self.calls == 1:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"write_file","path":"outputs/github_star_growth/source_data.json",'
                    '"content":"{\\"sheets\\":[{\\"name\\":\\"周榜单\\",\\"columns\\":[\\"项目名\\",\\"地址\\",\\"上升 star 数\\",\\"中文解释\\",\\"推荐理由\\"]}],\\"generated_at\\":\\"2026-05-20\\"}"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        if self.calls in {2, 3}:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    f'{{"tool":"read_artifact","artifact_ref":"{artifact_ref}","offset":0,"max_chars":2000}}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        if self.calls == 4:
            assert "local-progress-guard" in prompt
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"write_file","path":"outputs/github_star_growth/source_data.json",'
                    '"content":"{\\"sheets\\":[{\\"name\\":\\"周榜单\\",\\"columns\\":[\\"项目名\\",\\"地址\\",\\"上升 star 数\\",\\"中文解释\\",\\"推荐理由\\"],\\"rows\\":[{\\"项目名\\":\\"demo-1\\",\\"地址\\":\\"https://example.com/1\\",\\"上升 star 数\\":120,\\"中文解释\\":\\"说明1\\",\\"推荐理由\\":\\"理由1\\"}]}]}"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        if self.calls == 5:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"data_to_workbook","source_json_path":"outputs/github_star_growth/source_data.json",'
                    '"path":"outputs/github_star_growth/github_star_growth.xlsx"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        raise AssertionError("local-progress guard should redirect remote exploration back to local staged work")


# LLM: _WrongToolDuringOpenSessionBackend proves open sessions block unrelated new write tools.
# 类用途: begin 之后故意发新的 write_file；系统必须拦住并要求继续同一个 session，而不是执行新写入。
class _WrongToolDuringOpenSessionBackend:
    name = "fake_wrong_tool_during_open_session_backend"

    def __init__(self):
        self.calls = 0
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
            self.session_id = session_id
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"write_file","path":"outputs/rogue.txt","content":"should not run"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        if self.calls == 3:
            assert "open_file_write_sessions" in prompt
            session_id = session_id or self.session_id
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    f'{{"tool":"file_write_session","action":"append","session_id":"{session_id}",'
                    '"chunk_index":0,"content":"console.log(\\"ok\\");"}}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        if self.calls == 4:
            session_id = session_id or self.session_id
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    f'{{"tool":"file_write_session","action":"finish","session_id":"{session_id}"}}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        raise AssertionError("open session should finish before any unrelated write executes")


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
        "bootstrap_contract": {
            "materialization_targets": [
                {
                    "artifact_id": "shopping_site_root",
                    "kind": "web_project",
                    "target_type": "artifact",
                    "workspace_relative_path": "outputs/shopping_site",
                },
                {
                    "artifact_id": "shopping_site_root",
                    "kind": "web_project",
                    "target_type": "required_file",
                    "workspace_relative_path": "outputs/shopping_site/index.html",
                },
                {
                    "artifact_id": "shopping_site_root",
                    "kind": "web_project",
                    "target_type": "required_file",
                    "workspace_relative_path": "outputs/shopping_site/app.js",
                },
            ],
            "startup_actions": [{"action": "materialize_target", "priority": 1}],
        },
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


# LLM: _xlsx_delivery_contract models a staged data-to-workbook artifact without task-specific recovery code.
# 函数用途: 给 closeout 恢复动作测试提供通用表格阶段合同：先有 source_data，再调 builder 生成 workbook。
def _xlsx_delivery_contract() -> dict[str, object]:
    return {
        "case_id": "github_weekly_star_growth_xlsx",
        "artifacts": [
            {
                "artifact_id": "github_star_growth_workbook",
                "kind": "xlsx",
                "preferred_path": "outputs/github_star_growth/github_star_growth.xlsx",
                "required": True,
                "validation_contract": {
                    "validator": "spreadsheet_acceptance",
                    "required_columns": ["项目名", "地址", "上升 star 数", "中文解释", "推荐理由"],
                    "staging_contract": {
                        "builder_tool": "data_to_workbook",
                        "source_json_ref": "outputs/github_star_growth/source_data.json",
                        "workbook_ref": "outputs/github_star_growth/github_star_growth.xlsx",
                        "checkpoint_shape_hints": {
                            "outputs/github_star_growth/source_data.json": '{"sheets":[{"name":"本周榜单","columns":["项目名","地址","上升 star 数","中文解释","推荐理由"],"rows":[{"项目名":"..."}]}]}'
                        },
                        "checkpoint_refs": [
                            "outputs/github_star_growth/source_data.json",
                            "outputs/github_star_growth/github_star_growth.xlsx",
                        ],
                    },
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


def test_tool_loop_redirects_repeated_remote_exploration_back_to_local_progress():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        artifact_path = workspace / "memory_archive" / "artifacts" / "tool_outputs" / "demo.json"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(json.dumps({"items": [{"name": "demo"}]}, ensure_ascii=False), encoding="utf-8")
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=8)
        agent = SimpleAgent(cfg, workspace)
        backend = _LocalProgressRedirectBackend()
        agent.backend = backend

        result = agent.run(
            "整理 GitHub 项目并生成表格。",
            params=RunParams(delivery_contract=_xlsx_delivery_contract(), save=False),
        )

        assert backend.calls == 5
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert (workspace / "outputs/github_star_growth/github_star_growth.xlsx").exists()


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
        codes = [item["code"] for item in report["artifacts"][0]["acceptance_report"]["findings"]]
        assert {"HTML_INCOMPLETE_DOCUMENT", "HTML_EXTERNAL_RESOURCE_REF"} <= set(codes)


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


# LLM: Repeated identical delivery failures must terminate as blocked instead of consuming endless tool rounds.
# 函数用途: 验证 closeout 会识别“同一失败 + 无工作进展”的通用卡死模式，并输出结构化阻塞结果。
def test_tool_loop_blocks_after_repeated_unchanged_delivery_failure():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=5)
        agent = SimpleAgent(cfg, workspace)
        backend = _NoProgressDeliveryBackend()
        agent.backend = backend

        result = agent.run(
            _delivery_contract_prompt(),
            params=RunParams(delivery_contract=_delivery_contract(), save=False),
        )
        report = json.loads((workspace / ".agent_delivery/closeout.json").read_text(encoding="utf-8"))

        assert backend.calls == 4
        assert "[MAIN_AGENT_DELIVERY_BLOCKED]" in result.response
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" not in result.response
        assert report["ok"] is False
        assert report["delivery_progress"]["unchanged_failure_count"] >= 4
        assert report["delivery_progress"]["recovery_actions"][0]["code"] == "ACCEPTANCE_FAILED"


# LLM: Missing bootstrap targets should delay no-progress blocking for multi-file artifacts.
# 函数用途: 覆盖真实多文件任务中“先有 index、后补 app.js/source_data”的中段阶段，确保系统先给补齐结构化目标的机会。
def test_tool_loop_keeps_running_while_bootstrap_targets_are_still_missing():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=6)
        agent = SimpleAgent(cfg, workspace)
        backend = _PendingTargetsDeliveryBackend()
        agent.backend = backend

        result = agent.run(
            "做一个购物网站。",
            params=RunParams(delivery_contract=_web_project_delivery_contract(), save=False),
        )
        report = json.loads((workspace / ".agent_delivery/closeout.json").read_text(encoding="utf-8"))

        assert backend.calls == 3
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert "[MAIN_AGENT_DELIVERY_BLOCKED]" not in result.response
        assert report["ok"] is True
        assert (workspace / "outputs/shopping_site/index.html").exists()
        assert (workspace / "outputs/shopping_site/app.js").read_text(encoding="utf-8") == (
            'console.log("shop ready");'
        )


# LLM: Startup bootstrap contracts should redirect inspection-only first turns until one target is materialized.
# 函数用途: 验证一个目标都还没出现时，list_files 不会被执行消耗轮次，而是先逼主代理落一个最小有效产物。
def test_tool_loop_redirects_inspection_only_calls_before_any_bootstrap_target_exists():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=5)
        agent = SimpleAgent(cfg, workspace)
        backend = _BootstrapMaterializationRedirectBackend()
        agent.backend = backend

        result = agent.run(
            "做一个购物网站。",
            params=RunParams(delivery_contract=_web_project_delivery_contract(), save=False),
            allowed_tools=["list_files", "write_file"],
        )
        report = json.loads((workspace / ".agent_delivery/closeout.json").read_text(encoding="utf-8"))

        assert backend.calls == 3
        assert report["ok"] is True
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert (workspace / "outputs/shopping_site/index.html").exists()
        assert (workspace / "outputs/shopping_site/app.js").read_text(encoding="utf-8") == (
            'console.log("bootstrapped");'
        )


# LLM: bootstrap startup should allow multiple redirects before escalating to a final block.
# 函数用途: 验证开工阶段连续几轮只检查目录时，系统会持续引导落地目标，而不是过早阻断导致真实任务没机会补回正轨。
def test_tool_loop_allows_multiple_bootstrap_redirects_before_blocking():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=7)
        agent = SimpleAgent(cfg, workspace)
        backend = _BootstrapMaterializationProgressiveBackend()
        agent.backend = backend

        result = agent.run(
            "做一个购物网站。",
            params=RunParams(delivery_contract=_web_project_delivery_contract(), save=False),
            allowed_tools=["list_files", "write_file"],
        )
        report = json.loads((workspace / ".agent_delivery/closeout.json").read_text(encoding="utf-8"))

        assert backend.calls == 5
        assert report["ok"] is True
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert (workspace / "outputs/shopping_site/index.html").exists()
        assert (workspace / "outputs/shopping_site/app.js").read_text(encoding="utf-8") == (
            'console.log("bootstrapped later");'
        )


# LLM: write-first staged recovery must redirect inspection-only calls before letting the task drift further.
# 函数用途: 验证 closeout 已经要求先补非空结构化数据时，系统会拦下 read_file 这类只读动作，推动主代理先修阶段产物。
def test_tool_loop_redirects_inspection_only_calls_during_required_delivery_repair():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=6)
        agent = SimpleAgent(cfg, workspace)
        backend = _DeliveryRepairRedirectBackend()
        agent.backend = backend

        result = agent.run(
            "整理 GitHub 周升星项目并生成表格。",
            params=RunParams(delivery_contract=_xlsx_delivery_contract(), save=False),
            allowed_tools=["write_file", "read_file", "data_to_workbook"],
        )
        report = json.loads((workspace / ".agent_delivery/closeout.json").read_text(encoding="utf-8"))

        assert backend.calls == 4
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert report["ok"] is True
        assert (workspace / "outputs/github_star_growth/github_star_growth.xlsx").exists()


# LLM: Contract-driven recovery actions should prefer fixing empty staged JSON before invoking any builder tool.
# 函数用途: 验证 source_data.json 为空时，closeout 先产出 STAGED_JSON_NO_ROWS，而不会误导模型直接调 builder。
def test_delivery_closeout_prefers_checkpoint_quality_actions_before_builder():
    from agent_py_agent.agent.agent_core.main_agent_delivery_closeout import (
        DeliveryContractValidationRequest,
        _enrich_delivery_progress,
        _validate_contract_artifacts,
    )

    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        source = workspace / "outputs/github_star_growth/source_data.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text('{"weekly_top10": []}', encoding="utf-8")
        contract = _xlsx_delivery_contract()
        report = _validate_contract_artifacts(
            DeliveryContractValidationRequest(
                contract=contract,
                artifacts=contract["artifacts"],
                workspace_root=workspace,
                params=RunParams(delivery_contract=contract, save=False),
            )
        )

        enriched = _enrich_delivery_progress(report, {}, workspace, contract=contract)
        actions = {item["code"]: item for item in enriched["delivery_progress"]["recovery_actions"]}

        assert "STAGED_JSON_NO_ROWS" in actions
        assert actions["STAGED_JSON_NO_ROWS"]["recommended_action"] == "write_non_empty_structured_rows"
        assert actions["STAGED_JSON_NO_ROWS"]["checkpoint_ref"] == "outputs/github_star_growth/source_data.json"
        assert "checkpoint_shape_hint" in actions["STAGED_JSON_NO_ROWS"]
        assert actions["STAGED_JSON_NO_ROWS"]["required_columns"] == ["项目名", "地址", "上升 star 数", "中文解释", "推荐理由"]
        assert "STAGING_BUILDER_READY" not in actions


# LLM: Skeleton-only staged JSON must stay in "fill data first" mode instead of pretending the builder can run.
# 函数用途: 验证只有 sheet/meta 骨架、没有真实行数据时，closeout 仍返回 STAGED_JSON_NO_ROWS，而不是 builder ready。
def test_delivery_closeout_treats_skeleton_only_staged_json_as_not_ready():
    from agent_py_agent.agent.agent_core.main_agent_delivery_closeout import (
        DeliveryContractValidationRequest,
        _enrich_delivery_progress,
        _validate_contract_artifacts,
    )

    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        source = workspace / "outputs/github_star_growth/source_data.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            json.dumps(
                {
                    "generated_date": "2026-05-20",
                    "sheets": [
                        {
                            "week": "2026-W01",
                            "projects": [],
                        }
                    ],
                    "metadata": {"total_weeks": 20},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        contract = _xlsx_delivery_contract()
        report = _validate_contract_artifacts(
            DeliveryContractValidationRequest(
                contract=contract,
                artifacts=contract["artifacts"],
                workspace_root=workspace,
                params=RunParams(delivery_contract=contract, save=False),
            )
        )

        enriched = _enrich_delivery_progress(report, {}, workspace, contract=contract)
        actions = {item["code"]: item for item in enriched["delivery_progress"]["recovery_actions"]}

        assert "STAGED_JSON_NO_ROWS" in actions
        assert "STAGING_BUILDER_READY" not in actions


# LLM: Required tabular columns should force staged JSON into a builder-compatible table shape.
# 函数用途: 验证 weeks/projects 这类非表格结构不会在声明 required_columns 时被误判为 builder-ready。
def test_delivery_closeout_rejects_non_tabular_checkpoint_when_columns_required():
    from agent_py_agent.agent.agent_core.main_agent_delivery_closeout import (
        DeliveryContractValidationRequest,
        _enrich_delivery_progress,
        _validate_contract_artifacts,
    )

    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        source = workspace / "outputs/github_star_growth/source_data.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            json.dumps(
                {
                    "weeks": [
                        {
                            "week_id": 1,
                            "projects": [
                                {
                                    "name": "demo",
                                    "url": "https://example.com/demo",
                                    "stars": 10,
                                }
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        contract = _xlsx_delivery_contract()
        report = _validate_contract_artifacts(
            DeliveryContractValidationRequest(
                contract=contract,
                artifacts=contract["artifacts"],
                workspace_root=workspace,
                params=RunParams(delivery_contract=contract, save=False),
            )
        )

        enriched = _enrich_delivery_progress(report, {}, workspace, contract=contract)
        actions = {item["code"]: item for item in enriched["delivery_progress"]["recovery_actions"]}

        assert "STAGED_JSON_REQUIRED_COLUMNS_MISSING" in actions
        assert actions["STAGED_JSON_REQUIRED_COLUMNS_MISSING"]["missing_columns"] == "项目名,地址,上升 star 数,中文解释,推荐理由"
        assert "checkpoint_shape_hint" in actions["STAGED_JSON_REQUIRED_COLUMNS_MISSING"]
        assert "STAGING_BUILDER_READY" not in actions


# LLM: Staged workbook JSON must satisfy contract-declared columns before builder execution.
# 函数用途: 验证 required_columns 会在 source_data.json 阶段提前检查，避免错列名数据进入 xlsx builder。
def test_delivery_closeout_requires_staged_json_required_columns_before_builder():
    from agent_py_agent.agent.agent_core.main_agent_delivery_closeout import (
        DeliveryContractValidationRequest,
        _enrich_delivery_progress,
        _validate_contract_artifacts,
    )

    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        source = workspace / "outputs/github_star_growth/source_data.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            json.dumps(
                {
                    "sheets": [
                        {
                            "name": "week-1",
                            "columns": ["项目名", "地址", "周上升star数"],
                            "rows": [{"项目名": "demo", "地址": "https://example.com", "周上升star数": 10}],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        contract = _xlsx_delivery_contract()
        report = _validate_contract_artifacts(
            DeliveryContractValidationRequest(
                contract=contract,
                artifacts=contract["artifacts"],
                workspace_root=workspace,
                params=RunParams(delivery_contract=contract, save=False),
            )
        )

        enriched = _enrich_delivery_progress(report, {}, workspace, contract=contract)
        actions = {item["code"]: item for item in enriched["delivery_progress"]["recovery_actions"]}

        assert "STAGED_JSON_REQUIRED_COLUMNS_MISSING" in actions
        assert actions["STAGED_JSON_REQUIRED_COLUMNS_MISSING"]["recommended_action"] == (
            "repair_structured_checkpoint_json"
        )
        assert "上升 star 数" in actions["STAGED_JSON_REQUIRED_COLUMNS_MISSING"]["missing_columns"]
        assert "STAGING_BUILDER_READY" not in actions


def test_delivery_closeout_requires_staged_json_evidence_before_builder():
    from agent_py_agent.agent.agent_core.main_agent_delivery_closeout import (
        DeliveryContractValidationRequest,
        _enrich_delivery_progress,
        _validate_contract_artifacts,
    )

    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        source = workspace / "outputs/github_star_growth/source_data.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            json.dumps(
                {
                    "sheets": [
                        {
                            "name": "week-1",
                            "columns": ["项目名", "地址", "上升 star 数", "中文解释", "推荐理由"],
                            "rows": [
                                {
                                    "项目名": "demo",
                                    "地址": "https://example.com",
                                    "上升 star 数": 10,
                                    "中文解释": "demo",
                                    "推荐理由": "demo",
                                }
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        contract = _xlsx_delivery_contract()
        artifact = contract["artifacts"][0]
        artifact["validation_contract"]["evidence_contract"] = {
            "required_fields": ["上升 star 数"],
            "require_verified": True,
        }
        report = _validate_contract_artifacts(
            DeliveryContractValidationRequest(
                contract=contract,
                artifacts=contract["artifacts"],
                workspace_root=workspace,
                params=RunParams(delivery_contract=contract, save=False),
            )
        )

        enriched = _enrich_delivery_progress(report, {}, workspace, contract=contract)
        actions = {item["code"]: item for item in enriched["delivery_progress"]["recovery_actions"]}

        assert "EVIDENCE_REQUIRED_FIELD_MISSING" in actions
        assert actions["EVIDENCE_REQUIRED_FIELD_MISSING"]["recommended_action"] == "repair_evidence_refs"
        assert actions["EVIDENCE_REQUIRED_FIELD_MISSING"]["checkpoint_ref"] == (
            "outputs/github_star_growth/source_data.json"
        )
        assert "STAGING_BUILDER_READY" not in actions


# LLM: Contract-driven recovery actions should expose builder readiness only after staged JSON is structurally usable.
# 函数用途: 验证 source_data.json 已有非空结构化数据时，closeout 才会生成 invoke_builder_tool 动作。
def test_delivery_closeout_adds_generic_staging_builder_action_for_ready_source():
    from agent_py_agent.agent.agent_core.main_agent_delivery_closeout import (
        DeliveryContractValidationRequest,
        _enrich_delivery_progress,
        _validate_contract_artifacts,
    )

    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        source = workspace / "outputs/github_star_growth/source_data.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            json.dumps(
                {
                    "sheets": [
                        {
                            "name": "week-1",
                            "columns": ["项目名", "地址", "上升 star 数", "中文解释", "推荐理由"],
                            "rows": [
                                {
                                    "项目名": "demo",
                                    "地址": "https://example.com",
                                    "上升 star 数": 10,
                                    "中文解释": "demo",
                                    "推荐理由": "demo",
                                }
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        contract = _xlsx_delivery_contract()
        report = _validate_contract_artifacts(
            DeliveryContractValidationRequest(
                contract=contract,
                artifacts=contract["artifacts"],
                workspace_root=workspace,
                params=RunParams(delivery_contract=contract, save=False),
            )
        )

        enriched = _enrich_delivery_progress(report, {}, workspace, contract=contract)
        actions = {item["code"]: item for item in enriched["delivery_progress"]["recovery_actions"]}

        assert "STAGING_BUILDER_READY" in actions
        assert actions["STAGING_BUILDER_READY"]["recommended_action"] == "invoke_builder_tool"
        assert actions["STAGING_BUILDER_READY"]["builder_tool"] == "data_to_workbook"
        assert actions["STAGING_BUILDER_READY"]["source_ref"] == "outputs/github_star_growth/source_data.json"


# LLM: Invalid staged JSON should produce a repair action with parse context instead of a builder action.
# 函数用途: 验证阶段 JSON 损坏时，closeout 会要求先修 JSON，而不是继续下游构建。
def test_delivery_closeout_reports_invalid_checkpoint_json():
    from agent_py_agent.agent.agent_core.main_agent_delivery_closeout import (
        DeliveryContractValidationRequest,
        _enrich_delivery_progress,
        _validate_contract_artifacts,
    )

    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        source = workspace / "outputs/github_star_growth/source_data.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text('[{"项目名":"demo","地址":"https://example.com"', encoding="utf-8")
        contract = _xlsx_delivery_contract()
        report = _validate_contract_artifacts(
            DeliveryContractValidationRequest(
                contract=contract,
                artifacts=contract["artifacts"],
                workspace_root=workspace,
                params=RunParams(delivery_contract=contract, save=False),
            )
        )

        enriched = _enrich_delivery_progress(report, {}, workspace, contract=contract)
        actions = {item["code"]: item for item in enriched["delivery_progress"]["recovery_actions"]}

        assert "STAGED_JSON_INVALID" in actions
        assert actions["STAGED_JSON_INVALID"]["recommended_action"] == "repair_structured_checkpoint_json"
        assert actions["STAGED_JSON_INVALID"]["checkpoint_ref"] == "outputs/github_star_growth/source_data.json"
        assert "parse_error" in actions["STAGED_JSON_INVALID"]
        assert "STAGING_BUILDER_READY" not in actions


# LLM: Repeated staged JSON failures should route into repair guard before no-progress closeout blocks.
# 函数用途: 验证 JSON 阶段文件损坏时，即使失败重复，也先给结构化修复链路接管机会，而不是直接熔断。
def test_delivery_closeout_defers_no_progress_block_when_write_first_repair_exists():
    from agent_py_agent.agent.agent_core.main_agent_delivery_closeout import (
        DeliveryContractValidationRequest,
        _enrich_delivery_progress,
        _should_block_on_no_progress,
        _validate_contract_artifacts,
    )

    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        source = workspace / "outputs/github_star_growth/source_data.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text('[{"项目名":"demo","地址":"https://example.com"', encoding="utf-8")
        contract = _xlsx_delivery_contract()
        report = _validate_contract_artifacts(
            DeliveryContractValidationRequest(
                contract=contract,
                artifacts=contract["artifacts"],
                workspace_root=workspace,
                params=RunParams(delivery_contract=contract, save=False),
            )
        )
        first = _enrich_delivery_progress(report, {}, workspace, contract=contract)
        previous = {
            "ok": False,
            "delivery_progress": {
                "failure_fingerprint": first["delivery_progress"]["failure_fingerprint"],
                "work_progress_fingerprint": first["delivery_progress"]["work_progress_fingerprint"],
                "unchanged_failure_count": 5,
            },
        }

        enriched = _enrich_delivery_progress(report, previous, workspace, contract=contract)
        actions = {item["code"]: item for item in enriched["delivery_progress"]["recovery_actions"]}

        assert "STAGED_JSON_INVALID" in actions
        assert actions["STAGED_JSON_INVALID"]["recommended_action"] == "repair_structured_checkpoint_json"
        assert enriched["delivery_progress"]["unchanged_failure_count"] >= 6
        assert _should_block_on_no_progress(enriched, contract=contract, workspace_root=workspace) is False


# LLM: New write tools must not run while a file_write_session is still open for the same task.
# 函数用途: 验证 open session 存在时，系统会拒绝新的 write_file，迫使模型先 append/finish 当前 session。
def test_tool_loop_blocks_unrelated_write_tools_while_open_file_write_session_exists():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "outputs/shopping_site").mkdir(parents=True)
        (workspace / "outputs/shopping_site/index.html").write_text(
            "<!doctype html><html><body><main>Shop</main></body></html>",
            encoding="utf-8",
        )
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=6)
        agent = SimpleAgent(cfg, workspace)
        backend = _WrongToolDuringOpenSessionBackend()
        agent.backend = backend

        result = agent.run(
            "做一个购物网站。",
            params=RunParams(delivery_contract=_web_project_delivery_contract(), save=False),
            allowed_tools=["file_write_session", "write_file"],
        )

        assert backend.calls == 4
        assert not (workspace / "outputs/rogue.txt").exists()
        assert (workspace / "outputs/shopping_site/app.js").read_text(encoding="utf-8") == (
            'console.log("ok");'
        )
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
