"""Shared fake backends and contracts for main-agent delivery closeout tests."""

from __future__ import annotations

import re
from pathlib import Path

from agent_py_agent.agent.backend import ModelResponse


# LLM: DeliveryContractBackend proves valid artifacts close out after the model submits final text.
# 类用途: 第一轮写出合同要求的 HTML，第二轮用普通最终回复触发隐式验收。
class DeliveryContractBackend:
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
                    '{"tool":"write_file","path":"outputs/html_report/index.html",'
                    '"content":"<!doctype html><html><head><title>Maison</title></head><body>'
                    '<a href=\\"#story\\">Story</a>'
                    '<section id=\\"story\\">Done</section></body></html>"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        if self.calls == 2:
            return ModelResponse(text="产物已经写好，请系统验收。", backend=self.name)
        raise AssertionError("delivery contract should close out after implicit acceptance")


# LLM: FailedDeliveryContractBackend proves failed machine acceptance feeds repair instead of false closeout.
# 类用途: 第一轮写出不完整且带外部资源的 HTML；第二轮检查系统把结构化验收失败交还给模型。
class FailedDeliveryContractBackend:
    name = "fake_failed_delivery_contract_backend"

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
                    '{"tool":"write_file","path":"outputs/html_report/index.html",'
                    '"content":"<!doctype html><html><head><link rel=\\"stylesheet\\" href=\\"https://fonts.example/font.css\\"></head><body><main>Bad</main>"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        return ModelResponse(text="已收到结构化修复反馈。", backend=self.name)


# LLM: IncompleteDeliveryContractBackend reproduces a truncated HTML file that used to close out too early.
# 类用途: 写出半截单文件 HTML；第二轮确认系统返回机器验收失败而不是完成标记。
class IncompleteDeliveryContractBackend:
    name = "fake_incomplete_delivery_contract_backend"

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
                    '{"tool":"write_file","path":"outputs/html_report/index.html",'
                    '"content":"<!doctype html><html><head><link rel=\\"stylesheet\\" '
                    'href=\\"https://fonts.example/font.css\\"><style>body{color:#111}"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        return ModelResponse(text="已收到不完整 HTML 的结构化反馈。", backend=self.name)


# LLM: ArtifactFindingRepairBackend proves failed artifact findings can be repaired and revalidated.
# 类用途: 第一轮写出不合格 HTML，第二轮提交验收，第三轮按结构化返工单修复，第四轮隐式验收。
class ArtifactFindingRepairBackend:
    name = "fake_artifact_finding_repair_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"write_file","path":"outputs/html_report/index.html",'
                    '"content":"<!doctype html><html><head><link rel=\\"stylesheet\\" '
                    'href=\\"https://fonts.example/font.css\\"></head><body><main>Bad</main>"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        if self.calls == 2:
            return ModelResponse(text="初版已写好，请系统验收。", backend=self.name)
        if self.calls == 3:
            assert "delivery-contract-check" in prompt
            assert "HTML_INCOMPLETE_DOCUMENT" in prompt
            assert "HTML_EXTERNAL_RESOURCE_REF" in prompt
            assert "repair_required" in prompt
            return _write_file_response(
                "outputs/html_report/index.html",
                '<!doctype html><html><head><title>Maison</title><style>body{color:#111}</style></head><body><main>Ready</main></body></html>',
                self.name,
            )
        if self.calls == 4:
            return ModelResponse(text="修复后的产物已经写好，请系统验收。", backend=self.name)
        raise AssertionError("artifact finding repair should close out after implicit acceptance")


# LLM: MissingArtifactRepairBackend proves wrong-path output is repaired through artifact refs.
# 类用途: 第一轮写到错误路径，第二轮提交验收，第三轮按 ARTIFACT_MISSING 写到合同路径。
class MissingArtifactRepairBackend:
    name = "fake_missing_artifact_repair_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return _write_file_response(
                "outputs/wrong_homepage/index.html",
                '<!doctype html><html><head><title>Wrong</title></head><body><main>Wrong path</main></body></html>',
                self.name,
            )
        if self.calls == 2:
            return ModelResponse(text="初版已写好，请系统验收。", backend=self.name)
        if self.calls == 3:
            assert "delivery-contract-check" in prompt
            assert "ARTIFACT_MISSING" in prompt
            assert "repair_required" in prompt
            return _write_file_response(
                "outputs/html_report/index.html",
                '<!doctype html><html><head><title>Maison</title></head><body><main>Correct path</main></body></html>',
                self.name,
            )
        if self.calls == 4:
            return ModelResponse(text="修复后的产物已经写好，请系统验收。", backend=self.name)
        raise AssertionError("missing artifact repair should close out after implicit acceptance")


# LLM: OpenWriteSessionDeliveryBackend creates a valid-looking artifact while leaving staged writes open.
# 类用途: 复现真实 E2E 中目录验收提前收口的问题；系统必须先处理 open file_write_session。
class OpenWriteSessionDeliveryBackend:
    name = "fake_open_write_session_delivery_backend"

    def __init__(self):
        self.calls = 0
        self.saw_open_session_context = False
        self.session_id = ""

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        session_id = session_id_from_prompt(prompt)
        if self.calls == 1:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"file_write_session","action":"begin","target_path":"outputs/static_site/app.js"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        if self.calls == 2:
            return ModelResponse(text="当前站点文件已准备验收。", backend=self.name)
        if self.calls == 3:
            self.saw_open_session_context = True
            assert "open-file-write-session" in prompt
            assert "open_file_write_sessions" in prompt
            self.session_id = session_id
            return _session_append_finish_response(session_id, 'console.log("shop ready");', self.name)
        if self.calls == 4:
            return ModelResponse(text="open session 已关闭，请系统验收。", backend=self.name)
        raise AssertionError("delivery should close out after open session is finished")


# LLM: NoProgressDeliveryBackend reproduces repeated identical delivery failures with no workspace progress.
# 类用途: 连续写出同一个坏 HTML；系统应结构化阻塞收口，而不是继续空转。
class NoProgressDeliveryBackend:
    name = "fake_no_progress_delivery_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls in {1, 3, 5, 7}:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"write_file","path":"outputs/html_report/index.html",'
                    '"content":"<!doctype html><html><head><link rel=\\"stylesheet\\" href=\\"https://fonts.example/font.css\\"></head><body><main>Bad</main>"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        if self.calls in {2, 4, 6, 8}:
            return ModelResponse(text="坏版本已写入，请系统验收。", backend=self.name)
        raise AssertionError("delivery should block after repeated unchanged failure")


# LLM: PendingTargetsDeliveryBackend proves multi-file work is not blocked before missing bootstrap targets are materialized.
# 类用途: 第一轮只写 index，第二轮只读检查，第三轮再补 app；系统不应在中段误判卡死。
class PendingTargetsDeliveryBackend:
    name = "fake_pending_targets_delivery_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return _write_file_response("outputs/static_site/index.html", '<!doctype html><html><body><script src="app.js"></script></body></html>', self.name)
        if self.calls == 2:
            return ModelResponse(text="站点初版已写入，请系统验收。", backend=self.name)
        if self.calls == 3:
            assert "pending_materialization_targets" in prompt
            assert "outputs/static_site/app.js" in prompt
            return ModelResponse(text='[TOOL_CALL]\n{"tool":"read_file","path":"outputs/static_site/index.html"}\n[/TOOL_CALL]', backend=self.name)
        if self.calls == 4:
            assert "no_progress_block_threshold" in prompt
            return _write_file_response("outputs/static_site/app.js", 'console.log("shop ready");', self.name)
        if self.calls == 5:
            return ModelResponse(text="缺失文件已补齐，请系统验收。", backend=self.name)
        raise AssertionError("pending targets should complete before any blocked closeout")


# LLM: CloseoutReworkBackend proves failed closeout guidance can recover without a separate repair gate.
# 类用途: 第一轮写空骨架，第二轮允许只读确认；closeout 返工单随后推动补数据并调 builder。
class CloseoutReworkBackend:
    name = "fake_closeout_rework_backend"

    def __init__(self):
        self.calls = 0
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.prompts.append(prompt)
        self.calls += 1
        if self.calls == 1:
            return _write_structured_json_response(
                "outputs/table_report/source_data.json",
                '{"generated_at":"2026-05-20","sheets":[{"name":"数据清单","rows":[]}]}',
                self.name,
            )
        if self.calls == 2:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"submit_for_acceptance","note":"初版结构化数据已写入，请验收。"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        if self.calls == 3:
            assert "repair_guidance" in prompt
            assert "STAGED_JSON_NO_ROWS" in prompt
            return ModelResponse(text='[TOOL_CALL]\n{"tool":"read_file","path":"outputs/table_report/source_data.json"}\n[/TOOL_CALL]', backend=self.name)
        if self.calls == 4:
            assert "STAGED_JSON_NO_ROWS" in prompt
            return _write_structured_json_response(
                "outputs/table_report/source_data.json",
                _valid_workbook_source_json(),
                self.name,
            )
        if self.calls == 5:
            return _workbook_builder_response(self.name)
        if self.calls == 6:
            return ModelResponse(text="产物已经修复并生成完毕，请系统验收。", backend=self.name)
        raise AssertionError("closeout rework should let the agent repair and then complete")


# LLM: LocalProgressRedirectBackend proves repeated remote exploration gets redirected back to staged local work.
# 类用途: 第一轮写空结构化骨架，随后连续只读 artifact；系统应回到本地补数据并调用 builder。
class LocalProgressRedirectBackend:
    name = "fake_local_progress_redirect_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        artifact_ref = str(Path("memory_archive/artifacts/tool_outputs/demo.json").resolve())
        if self.calls == 1:
            return _write_file_response("outputs/table_report/source_data.json", _empty_workbook_source_json(), self.name)
        if self.calls == 2:
            return ModelResponse(text="阶段数据已写入，请系统验收。", backend=self.name)
        if self.calls == 3:
            return ModelResponse(text=f'[TOOL_CALL]\n{{"tool":"read_artifact","artifact_ref":"{artifact_ref}","offset":0,"max_chars":2000}}\n[/TOOL_CALL]', backend=self.name)
        if self.calls == 4:
            assert "local-progress-guard" in prompt
            return _write_structured_json_response(
                "outputs/table_report/source_data.json",
                _valid_workbook_source_json(),
                self.name,
            )
        if self.calls == 5:
            return _workbook_builder_response(self.name)
        if self.calls == 6:
            return ModelResponse(text="表格产物已生成，请系统验收。", backend=self.name)
        raise AssertionError("local-progress guard should redirect remote exploration back to local staged work")


# LLM: RecoveryAttemptRepairBackend proves fresh recovery attempts do not inherit stale no-progress debt.
# 类用途: 先故意只读一次恢复上下文；系统应走阶段修复合同，而不是被旧 local-progress 计数直接阻断。
class RecoveryAttemptRepairBackend:
    name = "fake_recovery_attempt_repair_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(text='[TOOL_CALL]\n{"tool":"read_file","path":"recovery_packet.json"}\n[/TOOL_CALL]', backend=self.name)
        if self.calls == 2:
            assert "delivery-contract-check" in prompt
            assert "repair_guidance" in prompt
            assert "LOCAL_PROGRESS_GUARD_BLOCKED" not in prompt
            return _write_structured_json_response(
                "outputs/table_report/source_data.json",
                _valid_workbook_source_json(),
                self.name,
            )
        if self.calls == 3:
            return _workbook_builder_response(self.name)
        if self.calls == 4:
            return ModelResponse(text="恢复产物已经生成完毕，请系统验收。", backend=self.name)
        raise AssertionError("fresh recovery attempt should repair before local-progress block")


# LLM: WrongToolDuringOpenSessionBackend proves open sessions block writes to the same unfinished target.
# 类用途: begin 之后故意覆盖同一个目标；系统必须要求继续同一个 session，而不是执行冲突写入。
class WrongToolDuringOpenSessionBackend:
    name = "fake_wrong_tool_during_open_session_backend"

    def __init__(self):
        self.calls = 0
        self.session_id = ""

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        session_id = session_id_from_prompt(prompt)
        if self.calls == 1:
            return ModelResponse(text='[TOOL_CALL]\n{"tool":"file_write_session","action":"begin","target_path":"outputs/static_site/app.js"}\n[/TOOL_CALL]', backend=self.name)
        if self.calls == 2:
            self.session_id = session_id
            return _write_file_response("outputs/static_site/app.js", "should not run", self.name)
        if self.calls == 3:
            assert "open_file_write_sessions" in prompt
            return _session_append_response(session_id or self.session_id, 'console.log("ok");', self.name)
        if self.calls == 4:
            return _session_finish_response(session_id or self.session_id, self.name)
        if self.calls == 5:
            return ModelResponse(text="open session 已关闭，请系统验收。", backend=self.name)
        raise AssertionError("open session should finish before any unrelated write executes")


# LLM: delivery_contract_prompt returns only user-visible task prose.
# 函数用途: 构造普通用户任务文本；机器合同由 RunParams.delivery_contract 传入。
def delivery_contract_prompt() -> str:
    return "用单文件 html 做一个高端家具品牌首页。"


# LLM: delivery_contract is the machine-only contract fixture shared by prompt and RunParams tests.
# 函数用途: 生成主代理交付收口需要的结构化合同；测试不从普通自然语言里推断产物要求。
def delivery_contract() -> dict[str, object]:
    return {
        "case_id": "html_delivery_case",
        "artifacts": [
            {
                "artifact_id": "homepage_html",
                "kind": "html",
                "preferred_path": "outputs/html_report/index.html",
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


# LLM: web_project_delivery_contract validates directory artifacts through static_site_check.
# 函数用途: 生成目录型 Web 产物合同，要求 index.html 和 app.js 都真实存在。
def web_project_delivery_contract() -> dict[str, object]:
    return {
        "case_id": "web_project_case",
        "bootstrap_contract": {
            "materialization_targets": _static_site_targets(),
            "startup_actions": [{"action": "materialize_target", "priority": 1}],
        },
        "artifacts": [
            {
                "artifact_id": "static_site_root",
                "kind": "web_project",
                "preferred_path": "outputs/static_site",
                "required": True,
                "validation_contract": {
                    "validator": "static_site_check",
                    "required_files": ["index.html", "app.js"],
                },
            }
        ],
    }


# LLM: xlsx_delivery_contract models a staged data-to-workbook artifact without task-specific recovery code.
# 函数用途: 给 closeout 恢复动作测试提供通用表格阶段合同：先有 source_data，再调 builder 生成 workbook。
def xlsx_delivery_contract() -> dict[str, object]:
    return {
        "case_id": "workbook_recovery_case",
        "artifacts": [
            {
                "artifact_id": "table_report_workbook",
                "kind": "xlsx",
                "preferred_path": "outputs/table_report/table_report.xlsx",
                "required": True,
                "validation_contract": _xlsx_validation_contract(),
            }
        ],
    }


# LLM: session_id_from_prompt reads structured tool result JSON from the previous model/tool turn.
# 函数用途: 测试后端从 file_write_session begin 回执里取 session_id，不靠自然语言描述。
def session_id_from_prompt(prompt: str) -> str:
    match = re.search(r'"session_id":\s*"([^"]+)"', prompt)
    return match.group(1) if match else ""


# LLM: _static_site_targets returns structured bootstrap materialization targets for a static site.
# 函数用途: 声明目录、index 和 app.js 三个目标，不用 prompt 文字推断。
def _static_site_targets() -> list[dict[str, object]]:
    return [
        {"artifact_id": "static_site_root", "kind": "web_project", "target_type": "artifact", "workspace_relative_path": "outputs/static_site"},
        {"artifact_id": "static_site_root", "kind": "web_project", "target_type": "required_file", "workspace_relative_path": "outputs/static_site/index.html"},
        {"artifact_id": "static_site_root", "kind": "web_project", "target_type": "required_file", "workspace_relative_path": "outputs/static_site/app.js"},
    ]


# LLM: _xlsx_validation_contract returns the reusable spreadsheet validation and staging contract.
# 函数用途: 声明列要求、builder、source_json 和 checkpoint shape hint。
def _xlsx_validation_contract() -> dict[str, object]:
    return {
        "validator": "spreadsheet_acceptance",
        "required_columns": ["记录名", "地址", "指标值", "中文说明", "说明依据"],
        "staging_contract": {
            "builder_tool": "data_to_workbook",
            "source_json_ref": "outputs/table_report/source_data.json",
            "workbook_ref": "outputs/table_report/table_report.xlsx",
            "checkpoint_shape_hints": {
                "outputs/table_report/source_data.json": '{"sheets":[{"name":"数据清单","columns":["记录名","地址","指标值","中文说明","说明依据"],"rows":[{"记录名":"..."}]}]}'
            },
            "checkpoint_refs": [
                "outputs/table_report/source_data.json",
                "outputs/table_report/table_report.xlsx",
            ],
        },
    }


# LLM: _write_file_response builds one write_file tool call response.
# 函数用途: 让测试后端用统一格式输出真实工具调用 JSON。
def _write_file_response(path: str, content: str, backend: str) -> ModelResponse:
    escaped = content.replace("\\", "\\\\").replace('"', '\\"')
    return ModelResponse(text=f'[TOOL_CALL]\n{{"tool":"write_file","path":"{path}","content":"{escaped}"}}\n[/TOOL_CALL]', backend=backend)


# LLM: _write_structured_json_response builds the generic JSON checkpoint writer call.
# 函数用途: 在测试中按机器 writer_tool 合同写阶段 JSON，不绕回普通文本写入。
def _write_structured_json_response(path: str, json_payload: str, backend: str) -> ModelResponse:
    return ModelResponse(
        text=f'[TOOL_CALL]\n{{"tool":"write_structured_json","path":"{path}","data":{json_payload}}}\n[/TOOL_CALL]',
        backend=backend,
    )


# LLM: _session_append_response builds one file_write_session append call response.
# 函数用途: 用真实 session_id 写入 chunk，覆盖 open-session 修复路径。
def _session_append_response(session_id: str, content: str, backend: str) -> ModelResponse:
    escaped = content.replace("\\", "\\\\").replace('"', '\\"')
    return ModelResponse(text=f'[TOOL_CALL]\n{{"tool":"file_write_session","action":"append","session_id":"{session_id}","chunk_index":0,"content":"{escaped}"}}\n[/TOOL_CALL]', backend=backend)


# LLM: _session_append_finish_response appends and finishes in one model turn.
# 函数用途: 让隐式验收测试先关闭 open session，再由下一轮最终回复触发 closeout。
def _session_append_finish_response(session_id: str, content: str, backend: str) -> ModelResponse:
    escaped = content.replace("\\", "\\\\").replace('"', '\\"')
    return ModelResponse(
        text=(
            "[TOOL_CALL]\n"
            f'{{"tool":"file_write_session","action":"append","session_id":"{session_id}","chunk_index":0,"content":"{escaped}"}}\n'
            "[/TOOL_CALL]\n"
            "[TOOL_CALL]\n"
            f'{{"tool":"file_write_session","action":"finish","session_id":"{session_id}"}}\n'
            "[/TOOL_CALL]"
        ),
        backend=backend,
    )


# LLM: _session_finish_response builds one file_write_session finish call response.
# 函数用途: 完成 open write session，允许后续 closeout 验收通过。
def _session_finish_response(session_id: str, backend: str) -> ModelResponse:
    return ModelResponse(text=f'[TOOL_CALL]\n{{"tool":"file_write_session","action":"finish","session_id":"{session_id}"}}\n[/TOOL_CALL]', backend=backend)


# LLM: _workbook_builder_response builds the data_to_workbook call for staged xlsx tests.
# 函数用途: 将 source_data.json 物化成 table_report.xlsx。
def _workbook_builder_response(backend: str) -> ModelResponse:
    return ModelResponse(
        text='[TOOL_CALL]\n{"tool":"data_to_workbook","source_json_path":"outputs/table_report/source_data.json","path":"outputs/table_report/table_report.xlsx"}\n[/TOOL_CALL]',
        backend=backend,
    )


# LLM: _empty_workbook_source_json returns a structured-but-empty staged data payload.
# 函数用途: 触发 STAGED_JSON_NO_ROWS/local-progress 修复路径。
def _empty_workbook_source_json() -> str:
    return '{"sheets":[{"name":"数据清单","columns":["记录名","地址","指标值","中文说明","说明依据"]}],"generated_at":"2026-05-20"}'


# LLM: _valid_workbook_source_json returns minimal builder-compatible staged data.
# 函数用途: 提供两张 sheet 和必要列，供 data_to_workbook 生成可验收 xlsx。
def _valid_workbook_source_json() -> str:
    return (
        '{"sheets":[{"name":"数据清单","columns":["记录名","地址","指标值","中文说明","说明依据"],'
        '"rows":[{"记录名":"demo-1","地址":"https://example.com/1","指标值":120,"中文说明":"说明1","说明依据":"理由1"}]},'
        '{"name":"汇总","columns":["记录名","地址","指标值","中文说明","说明依据"],'
        '"rows":[{"记录名":"demo-2","地址":"https://example.com/2","指标值":110,"中文说明":"说明2","说明依据":"理由2"}]}]}'
    )
