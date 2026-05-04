from __future__ import annotations

"""LLM: implements the verification scenario that proves the parent agent rejects forged artifacts and self-declared completion.

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


def _verification_setup(args):
    """Setup for verification case: create workspace, agent, and forge a fake completed task."""
    paths = create_scenario_workspace(args)
    print("MY-AGENT SCENARIO TEST")
    print("case=verification")
    print(f"run_root={paths.run_root}")
    print(f"fixture_root={paths.fixture_root}")
    print(f"config={paths.config}")

    agent = load_scenario_agent(paths.config)
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
    task.evidence.append(
        VerificationEvidence(
            kind="file_read",
            summary="伪造证据：声称 read_file 成功",
            path="README.md",
            ok=True,
            created_at=time.time(),
        )
    )
    task.evidence.append(
        VerificationEvidence(
            kind="file_write",
            summary="伪造证据：声称 write_file 写入 scenario_outputs/forged.md",
            path="scenario_outputs/forged.md",
            ok=True,
            created_at=time.time(),
        )
    )
    agent.subagents.save(task)
    Path(task.output_json).write_text(
        json.dumps(
            {
                "run_id": task.id,
                "status": "AWAITING_ACCEPTANCE",
                "artifacts": [
                    {
                        "path": "scenario_outputs/forged.md",
                        "kind": "report",
                        "summary": "这个文件被故意留空不存在，用来测试验收防作弊。",
                    }
                ],
                "tests": [{"name": "fake-test", "command": "echo ok", "ok": True}],
                "patches": [],
                "blockers": [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    Path(task.runner_result_json).write_text(
        json.dumps(
            {
                "run_id": task.id,
                "structured_output_found": True,
                "structured_output_ok": True,
                "structured_parse_error": "",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return paths, agent, task


def run_scenario_verification_case(args) -> int:
    """LLM: verify that the parent agent rejects forged artifacts and self-declared completion.

    新手说明:
    验证系统防作弊机制——子代理声称完成了任务但实际上文件不存在时，
    父代理的验收逻辑必须能识别并拒绝这种伪造。
    """

    paths, agent, task = _verification_setup(args)

    print_scenario_step(2, "执行父代理验收")
    report = agent.subagents.write_acceptance_review_report(
        run_ids=[task.id],
        apply=True,
        reviewer="scenario-verification",
        note="forged artifact must be rejected",
    )
    loaded = agent.subagents.load(task.id)
    for record in report.records:
        print(
            f"- decision={record.decision} ok={record.ok} applied={record.applied} "
            f"{record.before_status}/{record.before_verification_status}->"
            f"{record.after_status}/{record.after_verification_status}"
        )
        for finding in record.findings:
            if not finding.ok:
                print(f"  [finding:{finding.severity}] {finding.name}: {finding.message}")

    final_ok = (
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
