
from __future__ import annotations

"""concrete Live Lab case implementations.

这里放每种测试场景具体做什么。
以后要加“记忆长任务”“工具边界任务”“问题任务”，优先在这里加一个新的 case。
"""

import json
import sys
import textwrap

from .constants import REPO_ROOT
from .main_agent_artifact_case import (
    case_main_artifact_readback,
    case_main_compact_resume_roundtrip,
)
from .main_agent_compact_stress import case_main_compact_stress_long_read
from .main_agent_complex_case import (
    case_main_large_log_audit,
    case_main_tool_failure_recovery,
)

# 2026-05-18: main-complex/main-artifact cases stay imported here only for suite dispatch; task logic lives in split main-agent case modules.

def run_case(lab, case_name: str) -> None:
    """dispatches a suite case name to its implementation.

    suite 里保存的是字符串，比如 `health`。
    这里把字符串转成真正要执行的函数。"""

    handlers = {
        "health": case_health,
        "bad_weather": case_bad_weather,
        "log_analysis_replay": case_log_analysis_replay,
        "gateway_ask": case_gateway_ask,
        "long_subagent": case_long_subagent,
        "main_artifact_readback": case_main_artifact_readback,
        "main_compact_resume_roundtrip": case_main_compact_resume_roundtrip,
        "main_compact_stress_long_read": case_main_compact_stress_long_read,
        "main_tool_failure_recovery": case_main_tool_failure_recovery,
        "main_large_log_audit": case_main_large_log_audit,
    }
    handlers[case_name](lab)


def case_health(lab) -> None:
    """validates CLI wiring and local observability without calling a model.

    这是最便宜的健康检查：看 CLI 能不能启动、LocalStore/gateway 状态能不能读。
    它不调用真实 LLM，适合每次开发完先跑一下。"""

    lab.section("CASE health")
    lab.run_command(lab.agent_command("--help"), timeout=60)
    lab.run_command(lab.agent_command("status", "--json"), timeout=60)
    lab.run_command(lab.agent_command("local-doctor", "--json"), timeout=60)
    lab.run_command(lab.agent_command("gateway", "status"), timeout=60)


def case_bad_weather(lab) -> None:
    """runs focused recovery/guard scenarios that do not require real LLM calls.

    这组是“坏天气测试”：gateway 崩溃残留、坏 JSON、runner 临时失败。
    它们用固定/模拟后端复现坑位，适合快速回归系统边界。"""

    lab.section("CASE bad_weather")
    workspace = lab.run_root / "bad_weather"
    for case_name in ["gateway-restart", "structured-repair", "runner-retry"]:
        lab.log(f"### scenario-test --case {case_name}")
        lab.run_command(
            lab.agent_command(
                "scenario-test",
                "--case",
                case_name,
                "--workspace",
                str(workspace),
                "--count",
                str(max(lab.args.count, 1)),
                "--max-runners",
                "1",
                "--max-cycles",
                str(max(lab.args.max_cycles, 2)),
                "--timeout",
                str(lab.args.timeout),
            ),
            timeout=lab.args.timeout + 90,
        )


def case_log_analysis_replay(lab) -> None:
    """Run the offline SecurityAlertV1 replay without a model call."""

    lab.section("CASE log_analysis_replay")
    from .log_analysis_replay import run_security_alert_v1_replay

    summary = run_security_alert_v1_replay(output_root=lab.run_root / "log_analysis_replay")
    lab.log(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    lab.log(f"stored_events={summary.get('stored_events')} total_events={summary.get('total_events')}")
    lab.log(f"case_path={summary.get('case_path')}")
    lab.log(f"route_path={summary.get('route_path')}")
    lab.log(f"report_path={summary.get('report_path')}")
    lab.log(f"evidence_paths={summary.get('evidence_paths')}")
    lab.log(f"failed_stage={summary.get('failed_stage')}")
    if not summary.get("ok"):
        raise RuntimeError(f"log analysis replay failed at {summary.get('failed_stage')}: {summary.get('error')}")


def case_gateway_ask(lab) -> None:
    """starts gateway and sends one real LLM ask through the runtime path.

    这是最小真实模型路径：
    先启动后台 gateway，再把一条 prompt 投进去，等真实模型回包，最后关闭 gateway。
    你能在终端看到我们发了什么、命令怎么跑、返回 JSON 是什么。"""

    lab.section("CASE gateway_ask")
    prompt = textwrap.dedent(
        """
        请用 5 条中文要点评估当前 my-agent 的记忆、工具、后台通道三块里各一个风险和一个下一步建议。
        不要调用工具，不要改文件。
        最后一行写：本次检查完成
        """
    ).strip()
    lab.record_prompt("gateway_ask", prompt)
    lab.run_command(lab.agent_command("gateway", "start", "--force"), timeout=90)
    try:
        response = lab.run_command(
            lab.agent_command(
                "gateway",
                "ask",
                prompt,
                "--timeout",
                str(lab.gateway_wait_timeout),
                "--no-save",
                "--json",
            ),
            timeout=lab.gateway_wait_timeout + 60,
        )
        response_path = lab.responses_dir / "gateway_ask.stdout.json"
        response_path.write_text(response.stdout, encoding="utf-8")
        lab.log(f"response_file={response_path}")
    finally:
        lab.run_command(
            lab.agent_command("gateway", "stop", "--timeout", "15", "--kill", "--reason", "live lab done"),
            timeout=45,
            allow_fail=True,
        )


def case_long_subagent(lab) -> None:
    """runs the existing happy-path scenario with real gateway, runners, and acceptance.

    这是比较接近真实工作的长链路测试：
    gateway 收到任务、主代理派工、runner 读写文件、父代理验收，全部关在隔离 fixture 里。"""

    lab.section("CASE long_subagent")
    sys.path.insert(0, str(REPO_ROOT))
    from agent_py_agent.cli.scenario_utils import (
        build_scenario_prompt,
        build_scenario_runner_instruction,
    )

    lab.record_prompt("long_subagent_main", build_scenario_prompt(max(lab.args.count, 1)))
    lab.record_prompt(
        "long_subagent_runner_instruction",
        build_scenario_runner_instruction(),
        label="RUNNER INSTRUCTION SENT TO SUBAGENTS",
    )
    workspace = lab.run_root / "long_subagent"
    lab.run_command(
        lab.agent_command(
            "scenario-test",
            "--case",
            "happy",
            "--workspace",
            str(workspace),
            "--count",
            str(max(lab.args.count, 1)),
            "--max-runners",
            str(max(lab.args.max_runners, 1)),
            "--max-cycles",
            str(max(lab.args.max_cycles, 1)),
            "--timeout",
            str(lab.args.timeout),
        ),
        timeout=lab.args.timeout * max(lab.args.max_cycles, 1) + 180,
    )
