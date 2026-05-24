# LLM: Main-agent complex Live Lab cases validate root-agent durability without subagent delegation.
# 模块用途: 提供主代理复杂任务真实模型测试；这些 case 会关闭小傻妞，专门观察主代理自己是否稳。

from __future__ import annotations

"""main-agent complex E2E cases.

给人看的解释：
这里的测试不让主代理派小傻妞，而是让主代理自己完成任务。
这样能先把“单个 my-agent 是否足够硬”测出来，再决定什么时候继续压子代理链路。
"""

import json
import textwrap
from pathlib import Path

from .main_agent_complex_large_log import (
    assert_large_log_report,
    main_large_log_prompt,
    seed_large_log,
)

# LLM: Compatibility aliases keep existing tests importing old private helper names.
# 模块用途: 大日志逻辑已拆到 main_agent_complex_large_log，这里保留旧入口避免调用方断裂。
_main_large_log_prompt = main_large_log_prompt
_seed_large_log = seed_large_log
_assert_large_log_report = assert_large_log_report

# LLM: case_main_tool_failure_recovery makes a real tool miss recoverable instead of terminal.
# 函数用途: 让主代理先遇到一个缺失文件，再改读正确素材并写出恢复报告。
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
    _assert_tool_recovery_report(output)
    lab.log(f"tool_recovery_report={output}")


# LLM: case_main_large_log_audit checks that big files are searched/audited by evidence, not pasted into context.
# 函数用途: 准备一个 100MB 日志，让主代理找关键错误并写审计报告。
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


# LLM: _ensure_main_agent_only appends deterministic isolation overrides without touching user config.
# 函数用途: 在本轮 Live Lab 临时配置里关闭子代理，保证这些 case 测的是主代理自己。
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

# LLM: _main_tool_failure_prompt creates a normal user recovery task with one intentional bad file.
# 函数用途: 生成工具失败恢复测试提示词，要求主代理遇到缺失文件后继续完成任务。
def _main_tool_failure_prompt() -> str:
    return textwrap.dedent(
        """
        这次你自己完成，不要派小傻妞。

        请先尝试读取 notes/does-not-exist.md。
        如果这个文件不存在，不要停，也不要假装读到了；请改读 notes/small_task.md 和 README.md。
        然后把你怎么恢复、最终读到了什么、下一步建议，写到 lab_outputs/tool-recovery/report.md。
        报告要让普通人能看懂，别只写一句话。
        """
    ).strip()

# LLM: _assert_tool_recovery_report checks recovery evidence from the real output file.
# 函数用途: 检查工具失败恢复报告必须提到缺失文件、替代素材和真实输出。
def _assert_tool_recovery_report(output: Path) -> None:
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


__all__ = [
    "case_main_large_log_audit",
    "case_main_tool_failure_recovery",
]
