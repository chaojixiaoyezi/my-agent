
from __future__ import annotations

"""backend stubs used by structured-repair and runner-retry scenario cases."""

import json

from ...agent.backend import ModelResponse


class ScenarioStructuredRepairBackend:

    name = "scenario_structured_repair_backend"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text=(
                    "我已经完成任务，但这次故意输出一个损坏的结构化结果块。\n"
                    "[SUBAGENT_RESULT]\n"
                    "{\n"
                    '  "status": "DONE",\n'
                    '  "summary": "这个 JSON 少了结尾，用来模拟模型输出损坏",\n'
                    '  "evidence": [\n'
                    '    {"kind": "note", "summary": "原始回复声称已有证据", "ok": true}\n'
                ),
                backend=self.name,
            )
        return ModelResponse(text=_structured_repair_success_text(), backend=self.name)


class ScenarioRetryBackend:

    name = "scenario_retry_backend"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        if "输出严格 JSON 格式" in prompt:
            return ModelResponse(
                text=json.dumps(
                    {
                        "analysis_reason": "场景测试模拟临时 runner 失败，允许重试。",
                        "root_cause": "transient_runner_failure",
                        "suggested_params": {},
                        "should_retry": True,
                        "should_split": False,
                        "confidence": 0.9,
                    },
                    ensure_ascii=False,
                ),
                backend=self.name,
            )
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("scenario transient runner failure")
        return ModelResponse(text=_runner_retry_success_text(), backend=self.name)


def _structured_repair_success_text() -> str:
    text = (
        "[SUBAGENT_RESULT]\n"
        "{\n"
        '  "status": "DONE",\n'
        '  "summary": "结构化输出损坏后已通过修复回合补齐。",\n'
        '  "used_tools": [],\n'
        '  "used_skills": [],\n'
        '  "evidence": [{"kind": "note", "summary": "修复回合生成了可解析证据", "ok": true}],\n'
        '  "evidence_packets": [_STRUCTURED_PACKET_],\n'
        '  "capability_requests": [],\n'
        '  "artifacts": [],\n'
        '  "tests": [_STRUCTURED_TEST_],\n'
        '  "patches": [],\n'
        '  "lessons": ["结构化输出损坏时先做格式修复，不新增事实"],\n'
        '  "next_actions": [],\n'
        '  "blocked_reason": "",\n'
        '  "failure_type": ""\n'
        "}\n"
        "[/SUBAGENT_RESULT]"
    )
    text = text.replace(
        "_STRUCTURED_PACKET_",
        _packet("evpkt-structured-repair", "结构化输出修复后可验收", "structured repair scenario"),
    )
    return text.replace("_STRUCTURED_TEST_", _file_check_test("structured repair", "坏 JSON 已修复"))


def _runner_retry_success_text() -> str:
    text = (
        "[SUBAGENT_RESULT]\n"
        "{\n"
        '  "status": "DONE",\n'
        '  "summary": "runner 在第二次尝试中完成，已生成可验收证据。",\n'
        '  "used_tools": [],\n'
        '  "used_skills": [],\n'
        '  "evidence": [{"kind": "note", "summary": "第二次 runner 尝试成功", "ok": true}],\n'
        '  "evidence_packets": [_RETRY_PACKET_],\n'
        '  "capability_requests": [],\n'
        '  "artifacts": [],\n'
        '  "tests": [_RETRY_TEST_],\n'
        '  "patches": [],\n'
        '  "lessons": ["临时 runner 错误可以由父代理有限重试恢复"],\n'
        '  "next_actions": [],\n'
        '  "blocked_reason": "",\n'
        '  "failure_type": ""\n'
        "}\n"
        "[/SUBAGENT_RESULT]"
    )
    text = text.replace(
        "_RETRY_PACKET_",
        _packet("evpkt-runner-retry", "第二次 runner 尝试成功并可验收", "runner retry scenario"),
    )
    return text.replace("_RETRY_TEST_", _file_check_test("runner retry", "第二次尝试通过"))


def _packet(packet_id: str, claim: str, checked_scope: str) -> str:
    return json.dumps({
        "id": packet_id,
        "claim": claim,
        "checked_scope": checked_scope,
        "evidence_refs": ["runner_result.json"],
        "artifact_refs": ["output.json"],
        "confidence": 0.9,
    }, ensure_ascii=False)


def _file_check_test(name: str, summary: str) -> str:
    return json.dumps({
        "name": name,
        "validation_method": "file_check",
        "file_path": "README.md",
        "ok": True,
        "summary": summary,
    }, ensure_ascii=False)
