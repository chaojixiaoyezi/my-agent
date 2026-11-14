# LLM: 实验脚本只调用已注册的诊断场景；不能恢复已停用的结果块修复协议或冒充真实 TUI。
# 模块用途: 组织 Live Lab 的隔离诊断流程与产物目录，真实模型场景按显式选择运行。
from __future__ import annotations

"""concrete Live Lab case implementations.

这里放每种测试场景具体做什么。
以后要加“记忆长任务”“工具边界任务”“问题任务”，优先在这里加一个新的 case。
"""

import textwrap

from .main_agent_artifact_case import (
    case_main_artifact_readback,
    case_main_compact_resume_roundtrip,
)
from .main_agent_compact_stress import case_main_compact_stress_long_read
from .main_agent_complex_case import (
    case_main_large_log_audit,
    case_main_tool_failure_recovery,
)
from .main_agent_limit_cases import (
    case_main_adversarial_mix,
    case_main_dense_rounds,
    case_main_failure_storm,
    case_main_interrupt_honesty,
)

# 2026-05-18: main-complex/main-artifact cases stay imported here only for suite dispatch; task logic lives in split main-agent case modules.

def run_case(lab, case_name: str) -> None:
    """dispatches a suite case name to its implementation.

    suite 里保存的是字符串，比如 `health`。
    这里把字符串转成真正要执行的函数。"""

    handlers = {
        "health": case_health,
        "gateway_ask": case_gateway_ask,
        "main_artifact_readback": case_main_artifact_readback,
        "main_compact_resume_roundtrip": case_main_compact_resume_roundtrip,
        "main_compact_stress_long_read": case_main_compact_stress_long_read,
        "main_tool_failure_recovery": case_main_tool_failure_recovery,
        "main_large_log_audit": case_main_large_log_audit,
        "main_dense_rounds": case_main_dense_rounds,
        "main_failure_storm": case_main_failure_storm,
        "main_interrupt_honesty": case_main_interrupt_honesty,
        "main_adversarial_mix": case_main_adversarial_mix,
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
