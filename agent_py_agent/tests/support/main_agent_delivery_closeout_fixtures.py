"""Shared fake backends and contracts for main-agent delivery closeout tests."""

from __future__ import annotations

import re
from pathlib import Path

from agent_py_agent.agent.backend import ModelResponse


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
            return _submit_for_acceptance_response("产物已经写好，请系统验收。", self.name)
        raise AssertionError("delivery contract should close out after explicit acceptance")


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
        if self.calls == 2:
            return _submit_for_acceptance_response("坏版本已写入，请系统验收。", self.name)
        return ModelResponse(text="已收到结构化修复反馈。", backend=self.name)


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
        if self.calls == 2:
            return _submit_for_acceptance_response("半截 HTML 已写入，请系统验收。", self.name)
        return ModelResponse(text="已收到不完整 HTML 的结构化反馈。", backend=self.name)


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
            return _submit_for_acceptance_response("初版已写好，请系统验收。", self.name)
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
            return _submit_for_acceptance_response("修复后的产物已经写好，请系统验收。", self.name)
        raise AssertionError("artifact finding repair should close out after explicit acceptance")


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
            return _submit_for_acceptance_response("初版已写好，请系统验收。", self.name)
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
            return _submit_for_acceptance_response("修复后的产物已经写好，请系统验收。", self.name)
        raise AssertionError("missing artifact repair should close out after explicit acceptance")


class OpenWriteSessionDeliveryBackend:
    name = "fake_legacy_write_session_delivery_backend"

    def __init__(self):
        self.calls = 0
        self.saw_open_session_context = False
        self.session_id = ""

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        session_id = session_id_from_prompt(prompt)
        if self.calls == 1:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"write_file","action":"begin","target_path":"outputs/static_site/app.js"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        if self.calls == 2:
            return _submit_for_acceptance_response("当前站点文件已准备验收。", self.name)
        if self.calls == 3:
            self.saw_open_session_context = True
            assert "open-file-write-session" in prompt
            assert "open_write_files" in prompt
            self.session_id = session_id
            return _session_append_finish_response(session_id, 'console.log("shop ready");', self.name)
        if self.calls == 4:
            return _submit_for_acceptance_response("open session 已关闭，请系统验收。", self.name)
        raise AssertionError("delivery should close out after open session is finished")


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
            return _submit_for_acceptance_response("坏版本已写入，请系统验收。", self.name)
        raise AssertionError("delivery should block after repeated unchanged failure")


class PendingTargetsDeliveryBackend:
    name = "fake_pending_targets_delivery_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return _write_file_response("outputs/static_site/index.html", '<!doctype html><html><body><script src="app.js"></script></body></html>', self.name)
        if self.calls == 2:
            return _submit_for_acceptance_response("站点初版已写入，请系统验收。", self.name)
        if self.calls == 3:
            assert "pending_materialization_targets" in prompt
            assert "outputs/static_site/app.js" in prompt
            return ModelResponse(text='[TOOL_CALL]\n{"tool":"read_file","path":"outputs/static_site/index.html"}\n[/TOOL_CALL]', backend=self.name)
        if self.calls == 4:
            assert "no_progress_block_threshold" in prompt
            return _write_file_response("outputs/static_site/app.js", 'console.log("shop ready");', self.name)
        if self.calls == 5:
            return _submit_for_acceptance_response("缺失文件已补齐，请系统验收。", self.name)
        raise AssertionError("pending targets should complete before any blocked closeout")


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
            return _submit_for_acceptance_response("产物已经修复并生成完毕，请系统验收。", self.name)
        raise AssertionError("closeout rework should let the agent repair and then complete")


class LocalProgressRedirectBackend:
    name = "fake_local_progress_redirect_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        artifact_ref = str(Path("blobs/tool_outputs/demo.json").resolve())
        if self.calls == 1:
            return _write_file_response("outputs/table_report/source_data.json", _empty_workbook_source_json(), self.name)
        if self.calls == 2:
            return _submit_for_acceptance_response("阶段数据已写入，请系统验收。", self.name)
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
            return _submit_for_acceptance_response("表格产物已生成，请系统验收。", self.name)
        raise AssertionError("local-progress guard should redirect remote exploration back to local staged work")


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
            return _submit_for_acceptance_response("恢复产物已经生成完毕，请系统验收。", self.name)
        raise AssertionError("fresh recovery attempt should repair before local-progress block")


class WrongToolDuringOpenSessionBackend:
    name = "fake_wrong_tool_during_open_session_backend"

    def __init__(self):
        self.calls = 0
        self.session_id = ""

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        session_id = session_id_from_prompt(prompt)
        if self.calls == 1:
            return ModelResponse(text='[TOOL_CALL]\n{"tool":"write_file","action":"begin","target_path":"outputs/static_site/app.js"}\n[/TOOL_CALL]', backend=self.name)
        if self.calls == 2:
            self.session_id = session_id
            return _write_file_response("outputs/static_site/app.js", "should not run", self.name)
        if self.calls == 3:
            assert "open_write_files" in prompt
            return _session_append_response(session_id or self.session_id, 'console.log("ok");', self.name)
        if self.calls == 4:
            return _session_finish_response(session_id or self.session_id, self.name)
        if self.calls == 5:
            return _submit_for_acceptance_response("open session 已关闭，请系统验收。", self.name)
        raise AssertionError("open session should finish before any unrelated write executes")


def delivery_contract_prompt() -> str:
    return "用单文件 html 做一个高端家具品牌首页。"


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


def session_id_from_prompt(prompt: str) -> str:
    match = re.search(r'"session_id":\s*"([^"]+)"', prompt)
    return match.group(1) if match else ""


def _static_site_targets() -> list[dict[str, object]]:
    return [
        {"artifact_id": "static_site_root", "kind": "web_project", "target_type": "artifact", "workspace_relative_path": "outputs/static_site"},
        {"artifact_id": "static_site_root", "kind": "web_project", "target_type": "required_file", "workspace_relative_path": "outputs/static_site/index.html"},
        {"artifact_id": "static_site_root", "kind": "web_project", "target_type": "required_file", "workspace_relative_path": "outputs/static_site/app.js"},
    ]


def _xlsx_validation_contract() -> dict[str, object]:
    return {
        "validator": "spreadsheet_acceptance",
        "required_columns": ["记录名", "地址", "指标值", "中文说明", "说明依据"],
        "staging_contract": {
            "builder_tool": "write_file",
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


def _write_file_response(path: str, content: str, backend: str) -> ModelResponse:
    escaped = content.replace("\\", "\\\\").replace('"', '\\"')
    return ModelResponse(text=f'[TOOL_CALL]\n{{"tool":"write_file","path":"{path}","content":"{escaped}"}}\n[/TOOL_CALL]', backend=backend)


def _write_structured_json_response(path: str, json_payload: str, backend: str) -> ModelResponse:
    return ModelResponse(
        text=f'[TOOL_CALL]\n{{"tool":"write_file","path":"{path}","data":{json_payload}}}\n[/TOOL_CALL]',
        backend=backend,
    )


def _session_append_response(session_id: str, content: str, backend: str) -> ModelResponse:
    escaped = content.replace("\\", "\\\\").replace('"', '\\"')
    return ModelResponse(text=f'[TOOL_CALL]\n{{"tool":"write_file","action":"append","session_id":"{session_id}","chunk_index":0,"content":"{escaped}"}}\n[/TOOL_CALL]', backend=backend)


def _session_append_finish_response(session_id: str, content: str, backend: str) -> ModelResponse:
    escaped = content.replace("\\", "\\\\").replace('"', '\\"')
    return ModelResponse(
        text=(
            "[TOOL_CALL]\n"
            f'{{"tool":"write_file","action":"append","session_id":"{session_id}","chunk_index":0,"content":"{escaped}"}}\n'
            "[/TOOL_CALL]\n"
            "[TOOL_CALL]\n"
            f'{{"tool":"write_file","action":"finish","session_id":"{session_id}"}}\n'
            "[/TOOL_CALL]"
        ),
        backend=backend,
    )


def _session_finish_response(session_id: str, backend: str) -> ModelResponse:
    return ModelResponse(text=f'[TOOL_CALL]\n{{"tool":"write_file","action":"finish","session_id":"{session_id}"}}\n[/TOOL_CALL]', backend=backend)


def _submit_for_acceptance_response(note: str, backend: str) -> ModelResponse:
    escaped = note.replace("\\", "\\\\").replace('"', '\\"')
    return ModelResponse(text=f'[TOOL_CALL]\n{{"tool":"submit_for_acceptance","note":"{escaped}"}}\n[/TOOL_CALL]', backend=backend)


def _workbook_builder_response(backend: str) -> ModelResponse:
    return ModelResponse(
        text='[TOOL_CALL]\n{"tool":"write_file","source_json_path":"outputs/table_report/source_data.json","path":"outputs/table_report/table_report.xlsx"}\n[/TOOL_CALL]',
        backend=backend,
    )


def _empty_workbook_source_json() -> str:
    return '{"sheets":[{"name":"数据清单","columns":["记录名","地址","指标值","中文说明","说明依据"]}],"generated_at":"2026-05-20"}'


def _valid_workbook_source_json() -> str:
    return (
        '{"sheets":[{"name":"数据清单","columns":["记录名","地址","指标值","中文说明","说明依据"],'
        '"rows":[{"记录名":"demo-1","地址":"https://example.com/1","指标值":120,"中文说明":"说明1","说明依据":"理由1"}]},'
        '{"name":"汇总","columns":["记录名","地址","指标值","中文说明","说明依据"],'
        '"rows":[{"记录名":"demo-2","地址":"https://example.com/2","指标值":110,"中文说明":"说明2","说明依据":"理由2"}]}]}'
    )
