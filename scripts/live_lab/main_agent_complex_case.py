
from __future__ import annotations

"""main-agent complex E2E cases."""

import json
import textwrap
from pathlib import Path

from .main_agent_complex_large_log import (
    assert_large_log_report,
    main_large_log_prompt,
    seed_large_log,
)

_main_large_log_prompt = main_large_log_prompt
_seed_large_log = seed_large_log
_assert_large_log_report = assert_large_log_report

def case_main_tool_failure_recovery(lab) -> None:
    lab.section("CASE main_tool_failure_recovery")
    _ensure_main_agent_only(lab)
    prompt = _main_tool_failure_prompt()
    lab.record_prompt("main_tool_failure_recovery", prompt)
    response = lab.run_command(
        lab.agent_command("run", prompt, "--save"),
        timeout=lab.args.timeout + 120,
    )
    (lab.responses_dir / "main_tool_failure_recovery.stdout.txt").write_text(response.stdout, encoding="utf-8")
    output = lab.fixture_root / "lab_outputs" / "tool-recovery" / "report.md"
    _assert_tool_recovery_report(
        output,
        protected_missing_path=lab.fixture_root / "notes" / "does-not-exist.md",
    )
    lab.log(f"tool_recovery_report={output}")


def case_main_large_log_audit(lab) -> None:
    lab.section("CASE main_large_log_audit")
    _ensure_main_agent_only(lab)
    log_path = lab.fixture_root / "logs" / "huge_app.log"
    seed_large_log(log_path)
    prompt = main_large_log_prompt()
    lab.record_prompt("main_large_log_audit", prompt)
    response = lab.run_command(
        lab.agent_command("run", prompt, "--save"),
        timeout=lab.args.timeout + 180,
    )
    (lab.responses_dir / "main_large_log_audit.stdout.txt").write_text(response.stdout, encoding="utf-8")
    output = lab.fixture_root / "lab_outputs" / "large-log-audit" / "report.md"
    assert_large_log_report(output)
    lab.log(f"large_log_report={output}")


def _ensure_main_agent_only(lab) -> None:
    marker = "# main-agent-complex overrides"
    text = lab.config_path.read_text(encoding="utf-8")
    if marker in text:
        return
    overrides = textwrap.dedent(
        f"""

        {marker}
        enable_subagents: false
        max_tool_rounds: 0
        request_timeout: {max(300, int(lab.args.timeout))}
        tool_read_max_chars: 50000
        tool_search_max_matches: 200
        tool_list_max_entries: 500
        """
    )
    lab.config_path.write_text(text + overrides, encoding="utf-8")

def _main_tool_failure_prompt() -> str:
    return textwrap.dedent(
        """
        请先尝试读取 notes/does-not-exist.md。
        如果这个文件不存在，不要停，也不要假装读到了；请改读 notes/small_task.md 和 README.md。
        然后把你怎么恢复、最终读到了什么、下一步建议，写到 lab_outputs/tool-recovery/report.md。
        报告要让普通人能看懂，别只写一句话。
        """
    ).strip()

def _assert_tool_recovery_report(output: Path, *, protected_missing_path: Path | None = None) -> None:
    if not output.exists():
        raise RuntimeError(f"工具失败恢复报告不存在: {output}")
    content = output.read_text(encoding="utf-8", errors="replace")
    lowered = content.lower()
    if "does-not-exist" not in lowered:
        raise RuntimeError("工具失败恢复报告没有提到最初缺失的文件。")
    if "small_task" not in lowered and "readme" not in lowered:
        raise RuntimeError("工具失败恢复报告没有提到替代读取的真实素材。")
    if len(content.strip()) < 120:
        raise RuntimeError("工具失败恢复报告过短，不足以说明恢复过程。")
    if protected_missing_path is not None and protected_missing_path.exists():
        raise RuntimeError(f"工具恢复不应创建原本缺失的输入文件: {protected_missing_path}")


__all__ = [
    "case_main_large_log_audit",
    "case_main_tool_failure_recovery",
]
