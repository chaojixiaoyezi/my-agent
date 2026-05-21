# LLM: Markdown repair-wave Live Lab case proves repair contracts work for prose documents too.
# 模块用途: 预置一个失败的 Markdown 周报 run，然后用自然语言测试 root 是否派小傻妞修复同一文档。

from __future__ import annotations

import json
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.subagent_dispatch_closeout_resolution import (
    blocking_task_ids,
    task_resolved_for_closeout,
)
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.manager_base import SubAgentManagerInitParams
from agent_py_agent.agent.subagents.services.hierarchy_leaf_targets import task_actual_target_tokens

from .state_assertions import (
    assert_no_subagent_state_blockers,
    assert_persisted_subagent_state_clean,
)

_REQUIRED_MARKDOWN_LINES = (
    "# 本周进展",
    "- 完成订单数据核对",
    "- 修复验收失败的报表内容",
    "## 风险",
    "- 等待最终业务验收",
)


# LLM: MarkdownRepairSeed is the deterministic setup handoff for the document repair canary.
# 类用途: 保存失败 seed 的 run_id 和 Markdown 产物路径，供真实 case 和单测断言使用。
@dataclass(frozen=True)
class MarkdownRepairSeed:
    run_id: str
    output_path: Path


# LLM: case_natural_markdown_repair_wave validates repair flow for a plain Markdown artifact.
# 函数用途: 先制造一个 Markdown 验收失败 run，再让主代理用普通用户话术安排小傻妞修复并完成验收。
def case_natural_markdown_repair_wave(lab) -> None:
    """Run a real-LLM repair wave starting from a rejected Markdown child."""

    lab.section("CASE natural_markdown_repair_wave")
    seed = seed_failed_markdown_child(lab.fixture_root)
    prompt = _natural_markdown_repair_wave_prompt()
    lab.record_prompt("natural_markdown_repair_wave", prompt)
    lab.log(f"seed_failed_run_id={seed.run_id}")
    lab.run_command(lab.agent_command("gateway", "start", "--force"), timeout=90)
    try:
        # LLM: Markdown repair-wave may include state read, dispatch, file repair, and acceptance in one gateway ask.
        # 函数用途: Markdown 修复真实 case 使用 gateway 总预算等待完整恢复闭环。
        response = lab.run_command(
            lab.agent_command(
                "gateway",
                "ask",
                prompt,
                "--timeout",
                str(lab.gateway_wait_timeout),
                "--json",
            ),
            timeout=lab.gateway_wait_timeout + 240,
        )
        response_path = lab.responses_dir / "natural_markdown_repair_wave.stdout.json"
        response_path.write_text(response.stdout, encoding="utf-8")
        lab.log(f"response_file={response_path}")
        assert_no_subagent_state_blockers(response.stdout)
    finally:
        lab.run_command(
            lab.agent_command("gateway", "stop", "--timeout", "15", "--kill", "--reason", "live lab done"),
            timeout=45,
            allow_fail=True,
        )
    output_path = lab.fixture_root / "lab_outputs" / "report" / "weekly.md"
    _assert_markdown_report_output(output_path)
    assert_markdown_repair_wave_created(lab.fixture_root, seed.run_id)
    assert_persisted_subagent_state_clean(lab.fixture_root)
    lab.log(f"natural_markdown_repair_wave_output={output_path}")


# LLM: _natural_markdown_repair_wave_prompt stays user-like and avoids internal orchestration jargon.
# 函数用途: 生成 Markdown 文档修复测试的自然语言提示词；不出现 dispatch、runner、contract 等内部词。
def _natural_markdown_repair_wave_prompt() -> str:
    return textwrap.dedent(
        """
        刚刚那个 Markdown 周报没通过检查。请你看一下当前小傻妞留下的状态和检查结果，
        再安排小傻妞把它修好，不要你自己直接写文件正文。

        目标还是 lab_outputs/report/weekly.md。
        这个 Markdown 要有本周进展标题、两条完成事项、风险段和等待最终业务验收这条风险。
        下面五行要一字不差出现在 Markdown 里：
        # 本周进展
        - 完成订单数据核对
        - 修复验收失败的报表内容
        ## 风险
        - 等待最终业务验收
        修好后请安排检查，确认文件存在且关键内容齐全。
        最后告诉我保存路径和检查结果。
        """
    ).strip()


# LLM: seed_failed_markdown_child creates a normal failed child run instead of a prose-only fixture.
# 函数用途: 给 Live Lab 预置一个 AWAITING_ACCEPTANCE 的失败 Markdown run，包括坏文件、output.json 和 content_check 失败报告。
def seed_failed_markdown_child(fixture_root: Path) -> MarkdownRepairSeed:
    root = Path(fixture_root)
    output = root / "lab_outputs" / "report" / "weekly.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_broken_markdown_report(), encoding="utf-8")
    manager = _seed_manager(root)
    task = manager.create_run(
        goal=f"初版 Markdown 周报未通过检查，需要修复 {output}",
        thought="Live Lab seed: first child produced an incomplete Markdown report.",
        plan=["保留失败产物", "等待父级安排修复"],
        agent_name="小傻妞-Markdown初版失败",
        role="worker",
        extra_write_roots=[str(output.parent)],
        acceptance_checks=[
            "周报必须是 Markdown，并包含本周进展、两条完成事项和风险段",
            "修复必须覆盖同一个 lab_outputs/report/weekly.md 文件",
        ],
        attributes={"required_content_files": {"weekly.md": list(_REQUIRED_MARKDOWN_LINES)}},
    )
    task.status = "AWAITING_ACCEPTANCE"
    task.verification_status = "UNVERIFIED"
    task.artifact_refs = [str(output)]
    task.result = _seed_result(output)
    _write_seed_output(task, output)
    _write_failed_reports(task, output)
    manager.save(task)
    return MarkdownRepairSeed(run_id=task.id, output_path=output)


# LLM: assert_markdown_repair_wave_created verifies task facts and artifact content, not final prose.
# 函数用途: 检查失败 seed 后确实出现 DONE/VERIFIED 的 Markdown 修复小傻妞，并且目标文档内容合格。
def assert_markdown_repair_wave_created(fixture_root: Path, seed_run_id: str) -> None:
    tasks = _task_snapshots(Path(fixture_root))
    seed = _task_by_id(tasks, seed_run_id)
    repairs = [task for task in tasks if seed and _is_verified_markdown_repair(task, seed)]
    if not repairs:
        raise RuntimeError("没有发现已验证的 Markdown 修复小傻妞。")
    _assert_markdown_report_output(Path(fixture_root) / "lab_outputs" / "report" / "weekly.md")
    if seed and not task_resolved_for_closeout(seed, tasks):
        raise RuntimeError(f"失败 seed 没有被修复 run 覆盖: {seed_run_id}")
    blockers = blocking_task_ids(tasks)
    if blockers:
        raise RuntimeError(f"Markdown 修复后仍有未解决的小傻妞状态: {', '.join(blockers)}")


# LLM: _assert_markdown_report_output is the concrete artifact gate for the document canary.
# 函数用途: 检查周报 Markdown 真实文件内容，缺标题、事项或风险段都会失败。
def _assert_markdown_report_output(output_path: Path) -> None:
    if not output_path.is_file():
        raise RuntimeError(f"Markdown 周报不存在: {output_path}")
    text = output_path.read_text(encoding="utf-8", errors="replace")
    missing = [line for line in _REQUIRED_MARKDOWN_LINES if line not in text]
    if missing:
        raise RuntimeError(f"Markdown 报告缺少内容: {', '.join(missing)}")


# LLM: _seed_manager keeps fixture setup aligned with runtime SubAgentManager paths.
# 函数用途: 使用真实 manager 创建 seed run，避免测试台手写不完整 task.json。
def _seed_manager(root: Path) -> SubAgentManager:
    return SubAgentManager(
        root / ".my_agent" / "subagents",
        params=SubAgentManagerInitParams(workspace_root=root, workspace_roots=[root]),
    )


# LLM: _write_seed_output mirrors runner output refs without pretending the bad run passed.
# 函数用途: 写入 output.json，让路径/产物合同和 sibling 覆盖逻辑能读取同一目标文件。
def _write_seed_output(task, output: Path) -> None:
    payload = {"artifacts": [{"path": str(output)}], "files_modified": [str(output)], "status": "failed_acceptance"}
    Path(task.output_json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# LLM: _write_failed_reports seeds parent-acceptance refs consumed by structured repair advice.
# 函数用途: 写 acceptance_review、test_execution 和 followup，小报告点明 content_check 缺失内容。
def _write_failed_reports(task, output: Path) -> None:
    reports = Path(task.reports_dir)
    reports.mkdir(parents=True, exist_ok=True)
    now = time.time()
    (reports / "acceptance_review.json").write_text(
        json.dumps({"decision": "REJECT", "run_id": task.id, "generated_at": now}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (reports / "test_execution.json").write_text(
        json.dumps(_test_execution_payload(task.id, output), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (reports / "parent_acceptance_auto_followup.json").write_text(
        json.dumps(_followup_payload(task.id, output, now), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# LLM: _test_execution_payload is generic content-check failure evidence for document artifacts.
# 函数用途: 生成父级测试失败记录，包含 content_check 目标文件和缺失文本证据。
def _test_execution_payload(run_id: str, output: Path) -> dict[str, object]:
    missing = _REQUIRED_MARKDOWN_LINES
    return {
        "run_id": run_id,
        "total_tests": len(missing),
        "failed": len(missing),
        "records": [
            {
                "test_name": f"content check {index}",
                "validation_method": "content_check",
                "file_path": str(output),
                "content_pattern": line,
                "executed": True,
                "exit_code": 1,
                "error": f"missing content: {line}",
                "validation_result": {"ok": False, "path": str(output), "missing_content": line},
            }
            for index, line in enumerate(missing, start=1)
        ],
    }


# LLM: _followup_payload mirrors the parent follow-up file shape enough for refs-first repair advice.
# 函数用途: 给 repair worker 提供 failed_tests 摘要；不自动执行任何后续动作。
def _followup_payload(run_id: str, output: Path, generated_at: float) -> dict[str, object]:
    return {
        "schema": "parent_acceptance_auto_followup.v1",
        "generated_at": generated_at,
        "run_id": run_id,
        "followup": {
            "status": "needs_manual_rescue",
            "action": "plan_rescue",
            "reason": "父级 content_check 失败，需要修复 Markdown 后重新验收。",
            "test_failed": len(_REQUIRED_MARKDOWN_LINES),
            "failed_tests": [{"name": "content check", "error": str(output)}],
        },
    }


# LLM: _task_snapshots loads small task records only; artifact bodies stay out of the test harness context.
# 函数用途: 从隔离 workspace 读取 task.json，转换成属性对象供状态收口 helpers 判断。
def _task_snapshots(root: Path) -> list[SimpleNamespace]:
    tasks: list[SimpleNamespace] = []
    for path in sorted((root / ".my_agent" / "subagents").glob("subagent-*/task.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(payload, dict):
            tasks.append(SimpleNamespace(**payload))
    return tasks


# LLM: _is_verified_markdown_repair requires terminal status plus structured artifact target overlap.
# 函数用途: 判断某个 run 是否覆盖同一个失败 Markdown 产物；不再从名称或 goal 里猜“修复”字样。
def _is_verified_markdown_repair(task: SimpleNamespace, seed: SimpleNamespace) -> bool:
    if str(getattr(task, "id", "") or "") == str(getattr(seed, "id", "") or ""):
        return False
    if str(getattr(task, "status", "") or "").upper() != "DONE":
        return False
    if str(getattr(task, "verification_status", "") or "").upper() != "VERIFIED":
        return False
    seed_targets = task_actual_target_tokens(seed)
    task_targets = task_actual_target_tokens(task)
    return bool(seed_targets and task_targets and seed_targets.issubset(task_targets))


# LLM: _task_by_id performs exact lookup so user-provided ids never become glob patterns.
# 函数用途: 按 run_id 找到 seed task；找不到时返回 None 让调用方给出明确失败。
def _task_by_id(tasks: list[SimpleNamespace], run_id: str) -> SimpleNamespace | None:
    for task in tasks:
        if str(getattr(task, "id", "") or "") == run_id:
            return task
    return None


# LLM: _seed_result keeps target refs available in legacy result parsing.
# 函数用途: 兼容通过 task.result 读取 artifact_path 的旧状态判断路径。
def _seed_result(output: Path) -> str:
    return "[SUBAGENT_RESULT] " + json.dumps({"artifacts": [{"path": str(output)}]}, ensure_ascii=False) + " [/SUBAGENT_RESULT]"


# LLM: _broken_markdown_report is intentionally incomplete so content-check repair advice has facts.
# 函数用途: 生成一个存在但明显不合格的周报 Markdown，只含草稿标题和待补充内容。
def _broken_markdown_report() -> str:
    return "# 草稿\n- 待补充\n"


# LLM: _complete_markdown_report is a compact valid document fixture used by contract tests.
# 函数用途: 提供满足周报验收的 Markdown 内容，避免测试夹具本身缺字段。
def _complete_markdown_report() -> str:
    return "\n".join([*_REQUIRED_MARKDOWN_LINES, ""])
