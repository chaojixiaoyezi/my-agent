# LLM: CLI scenario case definition; keep fixture flow and expected gateway/subagent behavior stable.
# 模块用途: 定义一类命令行情景测试，用来复现和验证端到端流程。

from __future__ import annotations

"""backend stubs used by structured-repair and runner-retry scenario cases."""

import json

from ...agent.backend import ModelResponse


# LLM: ScenarioStructuredRepairBackend 是scenario CLI的数据契约；字段名会被调用方和测试读取。
# 类用途: 定义本模块对外传递的数据字段，字段名需要和调用方保持一致。
class ScenarioStructuredRepairBackend:

    name = "scenario_structured_repair_backend"

    # LLM: __init__ 属于scenario CLI；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def __init__(self) -> None:
        self.calls = 0

    # LLM: generate 属于scenario CLI；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
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


# LLM: ScenarioRetryBackend 是scenario CLI的数据契约；字段名会被调用方和测试读取。
# 类用途: 定义本模块对外传递的数据字段，字段名需要和调用方保持一致。
class ScenarioRetryBackend:

    name = "scenario_retry_backend"

    # LLM: __init__ 属于scenario CLI；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def __init__(self) -> None:
        self.calls = 0

    # LLM: generate 属于scenario CLI；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
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


# LLM: _structured_repair_success_text keeps the scenario output contract readable and traceable.
# 函数用途: 生成结构化修复场景的成功 SUBAGENT_RESULT，包含 evidence packet refs 和安全 file_check。
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


# LLM: _runner_retry_success_text keeps retry scenario output aligned with strict closeout.
# 函数用途: 生成 runner retry 成功 SUBAGENT_RESULT，显式提供 evidence packet refs 和 file_check 测试事实。
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


# LLM: _packet returns a compact JSON object string for scenario evidence packets.
# 函数用途: 复用 scenario evidence packet 结构，确保每个成功 stub 都带 evidence/artifact refs。
def _packet(packet_id: str, claim: str, checked_scope: str) -> str:
    return json.dumps({
        "id": packet_id,
        "claim": claim,
        "checked_scope": checked_scope,
        "evidence_refs": ["runner_result.json"],
        "artifact_refs": ["output.json"],
        "confidence": 0.9,
    }, ensure_ascii=False)


# LLM: _file_check_test returns a safe test fact for parent auto-policy dry-run.
# 函数用途: 生成不会触发命令执行风险的 file_check 测试条目，并保留 ok/summary 兼容旧验收。
def _file_check_test(name: str, summary: str) -> str:
    return json.dumps({
        "name": name,
        "validation_method": "file_check",
        "file_path": "README.md",
        "ok": True,
        "summary": summary,
    }, ensure_ascii=False)
