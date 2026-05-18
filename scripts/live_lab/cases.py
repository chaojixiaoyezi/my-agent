# LLM: Live Lab validation script; keep CLI flags, artifact paths, and replay outputs stable for scenario tests.
# 模块用途: 支撑可见验收和回放场景，负责启动案例、整理输出或生成报告。

from __future__ import annotations

"""concrete Live Lab case implementations.

给人看的解释：
这里放每种测试场景具体做什么。
以后要加“记忆长任务”“工具边界任务”“问题任务”，优先在这里加一个新的 case。
"""

import json
import sys
import textwrap

from .constants import REPO_ROOT
from .file_repair_wave_case import case_natural_file_repair_wave
from .markdown_repair_wave_case import case_natural_markdown_repair_wave
from .shop_case import (
    _external_asset_refs,
    _has_disabled_control,
    case_natural_shop_subagent,
)
from .shop_repair_wave_case import case_natural_shop_repair_wave
from .state_assertions import (
    assert_no_subagent_state_blockers,
    assert_persisted_subagent_state_clean,
)

# LLM: Case imports stay explicit so adding a real canary also updates docs and tests in one place.
# 函数用途: 下面的 run_case 字典是 CLI suite 字符串到真实 case 函数的公开调度表。

# LLM: run_case 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
# 函数用途: 执行对应流程阶段，并把成功、失败和产物写入汇总状态。
def run_case(lab, case_name: str) -> None:
    """dispatches a suite case name to its implementation.

    给人看的解释：
    suite 里保存的是字符串，比如 `health`。
    这里把字符串转成真正要执行的函数。"""

    handlers = {
        "health": case_health,
        "bad_weather": case_bad_weather,
        "log_analysis_replay": case_log_analysis_replay,
        "gateway_ask": case_gateway_ask,
        "long_subagent": case_long_subagent,
        "natural_html_subagent": case_natural_html_subagent,
        "natural_shop_subagent": case_natural_shop_subagent,
        "natural_shop_repair_wave": case_natural_shop_repair_wave,
        # LLM: File repair canary stays split so generic cases.py does not grow CSV-specific assertions.
        "natural_file_repair_wave": case_natural_file_repair_wave,
        # LLM: Markdown repair canary keeps document checks out of the generic dispatcher.
        "natural_markdown_repair_wave": case_natural_markdown_repair_wave,
    }
    handlers[case_name](lab)


# LLM: case_health 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def case_health(lab) -> None:
    """validates CLI wiring and local observability without calling a model.

    给人看的解释：
    这是最便宜的健康检查：看 CLI 能不能启动、LocalStore/gateway 状态能不能读。
    它不调用真实 LLM，适合每次开发完先跑一下。"""

    lab.section("CASE health")
    lab.run_command(lab.agent_command("--help"), timeout=60)
    lab.run_command(lab.agent_command("status", "--json"), timeout=60)
    lab.run_command(lab.agent_command("local-doctor", "--json"), timeout=60)
    lab.run_command(lab.agent_command("gateway", "status"), timeout=60)


# LLM: case_bad_weather 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def case_bad_weather(lab) -> None:
    """runs focused recovery/guard scenarios that do not require real LLM calls.

    给人看的解释：
    这组是“坏天气测试”：伪造完成、gateway 崩溃残留、坏 JSON、runner 临时失败。
    它们用固定/模拟后端复现坑位，适合快速回归系统边界。"""

    lab.section("CASE bad_weather")
    workspace = lab.run_root / "bad_weather"
    for case_name in ["verification", "gateway-restart", "structured-repair", "runner-retry"]:
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


# LLM: case_log_analysis_replay 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
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


# LLM: case_gateway_ask 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
# 函数用途: 协调 gateway 请求、进程状态、worker 或本地文件之间的流转。
def case_gateway_ask(lab) -> None:
    """starts gateway and sends one real LLM ask through the runtime path.

    给人看的解释：
    这是最小真实模型路径：
    先启动后台 gateway，再把一条 prompt 投进去，等真实模型回包，最后关闭 gateway。
    你能在终端看到我们发了什么、命令怎么跑、返回 JSON 是什么。"""

    lab.section("CASE gateway_ask")
    prompt = textwrap.dedent(
        """
        LIVE LAB REAL LLM TASK:
        请用 5 条中文要点评估当前 my-agent 的 memory、tools、gateway 三块里各一个风险和一个下一步建议。
        不要调用工具，不要改文件。
        最后一行必须输出：LIVE_LAB_OK
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
                str(lab.args.timeout),
                "--no-save",
                "--json",
            ),
            timeout=lab.args.timeout + 60,
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


# LLM: case_long_subagent 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def case_long_subagent(lab) -> None:
    """runs the existing happy-path scenario with real gateway, runners, and acceptance.

    给人看的解释：
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


# LLM: case_natural_html_subagent is the user-language E2E canary; avoid orchestration jargon in its prompt.
# 函数用途: 用普通用户说法要求主代理派小傻妞完成一个单文件 HTML 页面，并检查真实产物是否落在公共输出目录。
def case_natural_html_subagent(lab) -> None:
    """runs a natural-language subagent task against the real gateway path."""

    lab.section("CASE natural_html_subagent")
    prompt = _natural_html_prompt()
    lab.record_prompt("natural_html_subagent", prompt)
    lab.run_command(lab.agent_command("gateway", "start", "--force"), timeout=90)
    try:
        response = lab.run_command(
            lab.agent_command(
                "gateway",
                "ask",
                prompt,
                "--timeout",
                str(lab.args.timeout),
                "--json",
            ),
            timeout=lab.args.timeout + 120,
        )
        response_path = lab.responses_dir / "natural_html_subagent.stdout.json"
        response_path.write_text(response.stdout, encoding="utf-8")
        lab.log(f"response_file={response_path}")
        assert_no_subagent_state_blockers(response.stdout)
    finally:
        lab.run_command(
            lab.agent_command("gateway", "stop", "--timeout", "15", "--kill", "--reason", "live lab done"),
            timeout=45,
            allow_fail=True,
        )
    output_path = lab.fixture_root / "lab_outputs" / "furniture-home" / "index.html"
    _assert_natural_html_output(output_path)
    assert_persisted_subagent_state_clean(lab.fixture_root)
    lab.log(f"natural_html_output={output_path}")


# LLM: _natural_html_prompt must stay close to real user wording so the test catches prompt-contract drift.
# 函数用途: 生成自然语言测试提示词；不出现 dispatch、runner、contract 等专业词，避免把测试做成只会考试。
def _natural_html_prompt() -> str:
    return textwrap.dedent(
        """
        我想做一个真实可看的页面。请你安排小傻妞帮你完成，不要你自己直接写正文。

        任务是：用单文件 html 做一个高端现代家具品牌的网站首页，风格高级、简洁、有设计感，适合真实商业品牌使用。只输出完整 html，不要注释。

        请把最终页面保存到 lab_outputs/furniture-home/index.html。
        不要依赖外部图片、外部字体或外部脚本；需要视觉效果就用 CSS、渐变、色块或内联样式完成。
        链接不要写成 /collections 这种需要真实路由的地址；如果要链接，就用页面内真实存在的 #section-id。
        完成后你自己检查一下：文件存在、能作为网页打开、页面里没有空链接、没有坏链接、没有 disabled 按钮。
        最后告诉我保存路径和检查结果。
        """
    ).strip()


# LLM: _assert_natural_html_output validates visible artifact facts, including static-only links, not the model's prose.
# 函数用途: 检查小傻妞真实写出的 HTML 产物，避免主代理只口头说完成，或交付 /shop 这类单文件页面打不开的坏链接。
def _assert_natural_html_output(output_path) -> None:
    if not output_path.exists():
        raise RuntimeError(f"自然语言 HTML 产物不存在: {output_path}")
    content = output_path.read_text(encoding="utf-8", errors="replace")
    lower = content.lower()
    required_terms = ["<html", "</html>", "<body", "</body>"]
    missing = [term for term in required_terms if term not in lower]
    if missing:
        raise RuntimeError(f"自然语言 HTML 产物缺少基本标签: {missing}")
    if "href=\"#\"" in lower or 'href="/' in lower or _has_disabled_control(lower):
        raise RuntimeError("自然语言 HTML 产物包含空链接、根路径坏链接或 disabled 按钮。")
    external_assets = _external_asset_refs(lower)
    if external_assets:
        raise RuntimeError(f"自然语言 HTML 产物依赖外部资源: {external_assets[:5]}")
    if "家具" not in content and "furniture" not in lower:
        raise RuntimeError("自然语言 HTML 产物不像家具品牌页面。")
