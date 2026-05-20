# LLM: Real task runtime issues classify process symptoms without reading prose as deliverable facts.
# 模块用途: 生成真实任务执行报告的结构化 issue codes；成功/失败仍由退出码和产物合同决定。

from __future__ import annotations

from pathlib import Path

from .main_agent_task_acceptance import TaskRunAcceptanceReport


# LLM: case_issue_codes merges process, artifact, and runtime-output diagnostic codes.
# 函数用途: 生成 case 级 issue 摘要；只读结构化退出码、验收摘要和统计型 stdout 症状。
def case_issue_codes(
    *,
    exit_code: int,
    acceptance: TaskRunAcceptanceReport,
    stdout_path: Path,
) -> tuple[str, ...]:
    issues: list[str] = []
    if exit_code != 0:
        issues.append(f"exit_code={exit_code}")
    failed = int(acceptance.summary.get("failed", 0))
    if failed:
        issues.append(f"artifact_acceptance_failed={failed}")
    issues.extend(output_diagnostic_issue_codes(exit_code, acceptance, stdout_path))
    return tuple(issues)


# LLM: output_diagnostic_issue_codes detects output pathologies without parsing task facts.
# 函数用途: 对 exit=0 但验收失败的情况补充空转/无工具等诊断，便于恢复策略选择。
def output_diagnostic_issue_codes(
    exit_code: int,
    acceptance: TaskRunAcceptanceReport,
    stdout_path: Path,
) -> list[str]:
    if exit_code != 0 or acceptance.ok:
        return []
    stdout = _safe_text(stdout_path)
    issues: list[str] = []
    if "tool_rounds=0" in stdout:
        issues.append("model_no_tool_progress")
    if line_repetition_ratio(stdout) >= 0.65:
        issues.append("model_repetitive_output")
    return issues


# LLM: line_repetition_ratio is statistical diagnostics, not natural-language fact extraction.
# 函数用途: 计算 stdout 中重复行比例，用于识别模型复读空转。
def line_repetition_ratio(text: str) -> float:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 12:
        return 0.0
    return 1.0 - (len(set(lines)) / len(lines))


# LLM: _safe_text reads bounded local logs for diagnostics only.
# 函数用途: 读取 stdout 尾部诊断模型运行状态；不会把自然语言内容当成交付事实。
def _safe_text(path: Path, *, max_chars: int = 200_000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-max_chars:]


__all__ = ["case_issue_codes", "line_repetition_ratio", "output_diagnostic_issue_codes"]
