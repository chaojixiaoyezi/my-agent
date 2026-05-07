# LLM: CLI scenario case definition; keep fixture flow and expected gateway/subagent behavior stable.
# 模块用途: 定义一类命令行情景测试，用来复现和验证端到端流程。

from __future__ import annotations

"""implements the verification scenario that proves the parent agent rejects forged artifacts and self-declared completion.

给人看的解释：
这个文件验证父代理不会接受伪造 artifact / 自称完成。
如果子代理声称写了文件但文件实际不存在，父代理验收必须拒绝。
"""

import json
import time
from pathlib import Path

from ...agent.subagent import VerificationEvidence
from ..scenario_utils import (
    create_scenario_workspace,
    load_scenario_agent,
    print_scenario_step,
    write_scenario_summary,
)


# LLM: _forge_task_evidence 属于scenario CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _forge_task_evidence(agent):
    print_scenario_step(1, "构造伪造完成的子代理记录")
    task = agent.subagents.create_run(
        goal="极端场景：runner 声称写了 artifact，但文件实际不存在",
        thought="验证父代理验收不能只相信模型自称。",
        plan=["伪造 output.json", "触发验收", "确认验收拒绝"],
        allowed_tools=["read_file", "write_file"],
        acceptance_checks=["必须有 read_file 证据", "必须有 write_file 证据", "artifact 文件必须真实存在"],
    )
    task.status = "AWAITING_ACCEPTANCE"
    task.verification_status = "NEEDS_ACCEPTANCE"
    task.channel_status = "OK"
    task.used_tools = ["read_file", "write_file"]
    task.evidence.append(VerificationEvidence(
        kind="file_read", summary="伪造证据：声称 read_file 成功",
        path="README.md", ok=True, created_at=time.time(),
    ))
    task.evidence.append(VerificationEvidence(
        kind="file_write", summary="伪造证据：声称 write_file 写入 scenario_outputs/forged.md",
        path="scenario_outputs/forged.md", ok=True, created_at=time.time(),
    ))
    agent.subagents.save(task)
    _write_forged_output_files(task)
    return task


# LLM: _write_forged_output_files 属于scenario CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 把报告、摘要或状态写入磁盘，保持输出路径和 JSON 字段稳定。
def _write_forged_output_files(task):
    Path(task.output_json).write_text(
        json.dumps({
            "run_id": task.id, "status": "AWAITING_ACCEPTANCE",
            "artifacts": [{"path": "scenario_outputs/forged.md", "kind": "report",
                           "summary": "这个文件被故意留空不存在，用来测试验收防作弊。"}],
            "tests": [{"name": "fake-test", "command": "echo ok", "ok": True}],
            "patches": [], "blockers": [],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    Path(task.runner_result_json).write_text(
        json.dumps({
            "run_id": task.id, "structured_output_found": True,
            "structured_output_ok": True, "structured_parse_error": "",
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# LLM: _verification_setup 属于scenario CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _verification_setup(args):
    paths = create_scenario_workspace(args)
    print("MY-AGENT SCENARIO TEST")
    print("case=verification")
    print(f"run_root={paths.run_root}")
    print(f"fixture_root={paths.fixture_root}")
    print(f"config={paths.config}")

    agent = load_scenario_agent(paths.config)
    task = _forge_task_evidence(agent)
    return paths, agent, task


# LLM: _print_acceptance_records 属于scenario CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def _print_acceptance_records(report) -> None:
    for record in report.records:
        print(
            f"- decision={record.decision} ok={record.ok} applied={record.applied} "
            f"{record.before_status}/{record.before_verification_status}->"
            f"{record.after_status}/{record.after_verification_status}"
        )
        _print_failed_findings(record.findings)


# LLM: _print_failed_findings 属于scenario CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def _print_failed_findings(findings) -> None:
    for finding in findings:
        if not finding.ok:
            print(f"  [finding:{finding.severity}] {finding.name}: {finding.message}")


# LLM: _verification_final_ok 属于scenario CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _verification_final_ok(report, loaded) -> bool:
    return (
        report.records
        and report.records[0].decision == "REJECT"
        and not report.records[0].ok
        and loaded.status == "BLOCKED"
        and loaded.verification_status == "FAILED"
        and any(
            item.name == "artifact_paths_exist" and not item.ok
            for item in report.records[0].findings
        )
    )


# LLM: run_scenario_verification_case 属于scenario CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 执行对应流程阶段，并把成功、失败和产物写入汇总状态。
def run_scenario_verification_case(args) -> int:
    paths, agent, task = _verification_setup(args)

    print_scenario_step(2, "执行父代理验收")
    report = agent.subagents.write_acceptance_review_report(
        run_ids=[task.id],
        apply=True,
        reviewer="scenario-verification",
        note="forged artifact must be rejected",
    )
    loaded = agent.subagents.load(task.id)
    _print_acceptance_records(report)

    final_ok = _verification_final_ok(report, loaded)
    write_scenario_summary(
        paths,
        ok=final_ok,
        reason="verification guard passed" if final_ok else "verification guard failed",
        extra={
            "case": "verification",
            "run_id": task.id,
            "acceptance_report": str(agent.subagents.workspace / "subagent_acceptance_report.json"),
            "acceptance_md": str(agent.subagents.workspace / "SUBAGENT_ACCEPTANCE.md"),
        },
    )
    print(f"\nsummary_json={paths.summary_json}")
    print(f"summary_md={paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2
