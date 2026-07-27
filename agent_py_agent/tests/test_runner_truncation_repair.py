"""R11a 实锤钉子:结果块被 max_tokens 截断的修复链。

钉死契约:①截断→repair 完整=DONE(原行为);②截断→repair 又截断→
截断特征专项二试(极简块约束)=DONE,不再两次截断即 BLOCKED 终判;
③非截断形态的失败不触发二试(保守不无限重试)。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_agent.backends import BaseBackend

FULL_BLOCK = (
    "[SUBAGENT_RESULT]\n"
    '{"status": "DONE", "summary": "分析完成", "used_tools": [], "used_skills": [],'
    ' "evidence": [{"kind": "note", "summary": "完整证据", "ok": true}],'
    ' "evidence_packets": [{"id": "ep1", "claim": "done", "checked_scope": "all",'
    ' "evidence_refs": ["runner_result.json"], "artifact_refs": [], "confidence": 0.9}],'
    ' "capability_requests": [], "coverage_records": [], "artifacts": [],'
    ' "tests": [{"name": "t", "ok": true}], "patches": [], "lessons": [],'
    ' "next_actions": [], "blocked_reason": "", "failure_type": ""}\n'
    "[/SUBAGENT_RESULT]\n"
)
TRUNCATED = '正文分析很长……\n[SUBAGENT_RESULT]\n{"status": "DONE", "summary": "被 max_tokens 截'
PENDING_BLOCK = (
    "[SUBAGENT_RESULT]\n"
    '{"status": "PENDING", "summary": "已保存检查点，下一轮继续同一任务",'
    ' "used_tools": [], "used_skills": [], "evidence": [],'
    ' "evidence_packets": [], "capability_requests": [], "coverage_records": [],'
    ' "artifacts": [], "tests": [], "patches": [], "lessons": [],'
    ' "next_actions": ["继续读取剩余文件并生成交付物"], "blocked_reason": "",'
    ' "failure_type": "incomplete_deliverables"}\n'
    "[/SUBAGENT_RESULT]\n"
)


class _ScriptedBackend(BaseBackend):
    name = "scripted_truncation_backend"

    def __init__(self, scripts: list[str]):
        self.scripts = scripts
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.prompts.append(prompt)
        text = self.scripts[min(len(self.prompts), len(self.scripts)) - 1]
        return ModelResponse(text=text, backend=self.name)


def _run_with(scripts: list[str]):
    with tempfile.TemporaryDirectory() as td:
        agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), Path(td))
        agent.backend = _ScriptedBackend(scripts)
        task = agent.subagents.create_run(
            goal="截断修复链验证", thought="t", plan=["p"], allowed_tools=[], acceptance_checks=["c"]
        )
        result = agent.run_subagent(task.id, dry_run=False, probe=False)
        loaded = agent.subagents.load(task.id)
        return agent.backend, result, loaded


def test_truncated_then_full_repair_is_done():
    backend, result, loaded = _run_with([TRUNCATED, FULL_BLOCK])
    assert len(backend.prompts) == 2
    assert loaded.status == "DONE" and result.structured_repair_ok


def test_double_truncation_gets_compact_retry_and_recovers():
    backend, result, loaded = _run_with([TRUNCATED, TRUNCATED, FULL_BLOCK])
    assert len(backend.prompts) == 3, "截断特征必须触发专项二试"
    assert "最后一次极简格式修复" in backend.prompts[2], "二试带极简块硬约束"
    assert loaded.status == "DONE" and result.structured_repair_ok
    assert loaded.failure_type == ""


def test_empty_repair_gets_one_compact_retry_and_preserves_same_run_for_continuation():
    backend, result, loaded = _run_with(["继续读取剩余文件。", "", PENDING_BLOCK])

    assert len(backend.prompts) == 3
    assert "最后一次极简格式修复" in backend.prompts[2]
    assert len(backend.prompts[2]) < len(backend.prompts[1])
    assert result.structured_repair_ok
    assert loaded.status == "PENDING"
    assert loaded.failure_type == "incomplete_deliverables"
    assert loaded.id == result.run_id


def test_non_truncation_failure_does_not_retry():
    # repair 轮给"完全没有块"的回复:found=False,非截断特征,不二试。
    backend, result, loaded = _run_with([TRUNCATED, "我修复不了,没有结果块。", FULL_BLOCK])
    assert len(backend.prompts) == 2, "非截断失败保持单次 repair,不无限重试"
    assert loaded.status == "BLOCKED"
    del result
