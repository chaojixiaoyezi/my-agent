from __future__ import annotations

"""LLM: provides scenario-test workspace setup, fixture writing, subprocess helpers, and summary output.

给人看的解释：
场景测试需要临时项目、隔离配置、固定 prompt、gateway 子进程和最终报告。
这些通用小工具都放这里，具体测试 case 只描述自己要验证的行为。
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..agent.core import SimpleAgent
from ..agent.subagents.models import SubAgentBoardOptions, SubAgentTask
from .common import ROOT, make_agent


@dataclass
class ScenarioPaths:

    run_root: Path
    fixture_root: Path
    config: Path
    summary_json: Path
    summary_md: Path

def create_scenario_workspace(args) -> ScenarioPaths:

    parent = (
        Path(args.workspace).expanduser().resolve()
        if args.workspace
        else Path(tempfile.gettempdir()) / "my-agent-scenarios"
    )
    parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_root = parent / f"scenario-{stamp}-{uuid.uuid4().hex[:6]}"
    fixture_root = run_root / "fixture_project"
    fixture_root.mkdir(parents=True, exist_ok=True)
    write_scenario_fixture(fixture_root)
    config_path = run_root / "scenario_agent_config.yaml"
    write_scenario_config(
        source_config=Path(args.config),
        target_config=config_path,
        fixture_root=fixture_root,
        request_timeout=args.timeout,
        max_subagents=max(args.count, 1),
    )
    return ScenarioPaths(
        run_root=run_root,
        fixture_root=fixture_root,
        config=config_path,
        summary_json=run_root / "scenario_summary.json",
        summary_md=run_root / "SCENARIO_SUMMARY.md",
    )


def write_scenario_fixture(fixture_root: Path) -> None:

    (fixture_root / "README.md").write_text(
        "\n".join(
            [
                "# My Agent Scenario Fixture",
                "",
                "这是 my-agent 隔离全流程测试用的小项目。",
                "所有 runner 只能在这个目录里读写文件。",
                "",
                "## 验收目标",
                "",
                "- 子代理必须读取本 README。",
                "- 子代理必须在自己的 task_dir/scenario_outputs/ 里写入报告。",
                "- 父代理必须完成 runner 调度和验收闭环。",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (fixture_root / "notes").mkdir(parents=True, exist_ok=True)
    (fixture_root / "notes" / "input_a.md").write_text(
        "A 组素材：检查 fixture 的 README，并说明读写工具是否可用。\n",
        encoding="utf-8",
    )
    (fixture_root / "notes" / "input_b.md").write_text(
        "B 组素材：输出一份简短证据报告，证明任务只在隔离目录内运行。\n",
        encoding="utf-8",
    )
    (fixture_root / "scenario_outputs").mkdir(parents=True, exist_ok=True)


def write_scenario_config(
    *,
    source_config: Path,
    target_config: Path,
    fixture_root: Path,
    request_timeout: float,
    max_subagents: int,
) -> None:

    base = source_config.read_text(encoding="utf-8")
    fixture = str(fixture_root).replace("\\", "/")
    overrides = f"""

# scenario-test isolation overrides
workspace_root: "{fixture}"
prompt_files:
memory_path: ".my_agent/memory.jsonl"
subagent_workspace: ".my_agent/subagents"
gateway_workspace: ".my_agent/gateway"
max_subagents: {max_subagents}
gateway_request_timeout: {int(request_timeout)}
gateway_request_poll_interval: 1
daemon_planner: false
daemon_apply: false
daemon_execute_runners: false
daemon_max_runners: 0
daemon_interval: 1
runner_failure_policy: "auto"
max_tool_rounds: 8
"""
    target_config.write_text(base + overrides, encoding="utf-8")


def load_scenario_agent(config_path: Path) -> SimpleAgent:

    class Args:
        config = str(config_path)

    return make_agent(Args())


def install_scenario_backend(agent: SimpleAgent, backend: object) -> None:

    agent.backend = backend
    agent._subagent_worker_backend_override = backend


def build_scenario_prompt(count: int) -> str:

    return (
        "这是 my-agent 隔离全流程场景测试。你必须通过工具创建子代理工单，"
        "不要自己直接完成任务。\n\n"
        "本阶段只允许创建工单和查看看板；禁止调用 dispatch_subagents，禁止 execute_runners，"
        "不要启动 runner，runner 会由下一阶段父代理调度。\n\n"
        "请只调用一次 create_subagents，参数必须满足：\n"
        f"- count: {count}\n"
        "- tool_preset: coding\n"
        "- goal: 在隔离 fixture 项目中读取 README.md，并在子代理 task_dir/scenario_outputs/ 写入自己的证据报告\n"
        "- acceptance_checks: 必须有 read_file 证据；必须有 write_file 证据；必须等待父代理验收\n"
        "- plan: 读取 README.md；从执行上下文读取 task_dir；写入 task_dir/scenario_outputs/<run_id>.md；"
        "输出 SUBAGENT_RESULT；等待验收\n\n"
        "创建后可以调用 subagent_board 看一眼状态，然后用一句话汇报创建了几个子代理。"
    )


def build_scenario_runner_instruction() -> str:

    return (
        "这是隔离全流程测试的 runner 阶段。你只能在当前 fixture 工作区内操作。\n"
        "必须严格按顺序完成，不允许跳步，也不允许用文字声称已经调用工具。\n"
        "1. 第一轮回复只能是下面这个工具调用，不要输出 SUBAGENT_RESULT、解释或 Markdown：\n"
        "[TOOL_CALL]\n"
        "{\"tool\":\"read_file\",\"path\":\"README.md\"}\n"
        "[/TOOL_CALL]\n"
        "2. 收到 read_file 成功结果后，从执行上下文 JSON 找到自己的 run_id、task_dir 和 allowed_write_roots。\n"
        "3. 第二轮回复只能调用 write_file，path 必须落在 allowed_write_roots 里面，推荐使用 "
        ".my_agent/subagents/<run_id>/scenario_outputs/<run_id>.md；content 写一份 3-6 行中文报告，"
        "说明已读取 README.md，并注明这是隔离测试和 task_dir 内产物。\n"
        "4. 只有在你已经看到 write_file 成功结果后，才允许输出最终 [SUBAGENT_RESULT]。\n"
        "5. 最终回复只能包含一个 [SUBAGENT_RESULT] JSON 结果块，不要输出 Markdown 代码围栏。\n"
        "JSON 必须包含：status=AWAITING_ACCEPTANCE；summary；used_tools 至少包含 read_file 和 write_file；"
        "evidence 至少两条，分别证明 README.md 已读取、task_dir/scenario_outputs/<run_id>.md 已写入；"
        "tests 至少一条 ok=true；artifacts 包含写入的报告路径；patches 为空数组。"
    )


def scenario_command(paths: ScenarioPaths, *parts: str) -> list[str]:

    return [sys.executable, "-m", "agent_py_agent", "--config", str(paths.config), *parts]


def run_scenario_gateway_ask(
    paths: ScenarioPaths,
    prompt: str,
    *,
    timeout: float,
    save: bool = False,
) -> dict[str, object]:

    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    start = run_scenario_subprocess(scenario_command(paths, "gateway", "start", "--force"), env=env, timeout=60)
    if start.returncode != 0:
        return {"ok": False, "error": "gateway start failed", "stdout": start.stdout, "stderr": start.stderr}
    try:
        ask_command = scenario_command(paths, "gateway", "ask", prompt, "--timeout", str(timeout), "--json")
        if not save:
            ask_command.append("--no-save")
        ask = run_scenario_subprocess(
            ask_command,
            env=env,
            timeout=timeout + 30,
        )
        if ask.returncode != 0:
            return {"ok": False, "error": "gateway ask failed", "stdout": ask.stdout, "stderr": ask.stderr}
        try:
            payload = json.loads(ask.stdout)
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"gateway response was not JSON: {exc}", "stdout": ask.stdout}
        return payload
    finally:
        run_scenario_subprocess(
            scenario_command(paths, "gateway", "stop", "--timeout", "10", "--kill", "--reason", "scenario-test done"),
            env=env,
            timeout=30,
        )


def run_scenario_subprocess(cmd: list[str], *, env: dict[str, str], timeout: float) -> subprocess.CompletedProcess:

    print("$", " ".join(cmd))
    completed = subprocess.run(
        cmd,
        cwd=ROOT.parent,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=env,
        timeout=timeout,
    )
    if completed.stdout:
        print(completed.stdout)
    if completed.stderr:
        print(completed.stderr)
    return completed


def print_scenario_step(index: int, title: str) -> None:
    print(f"\n== {index}. {title} ==")


def print_scenario_board(agent: SimpleAgent, *, limit: int) -> None:

    board = agent.subagents.write_board(options=SubAgentBoardOptions(recent_limit=limit))
    print("board_summary=" + json.dumps(board.summary, ensure_ascii=False, sort_keys=True))
    for item in board.items[:limit]:
        print(
            f"- {item.id} status={item.status} verify={item.verification_status} "
            f"tools_evidence={item.evidence_count} flags={','.join(item.risk_flags) or 'ok'} :: {item.goal}"
        )
    print(f"board_json={agent.subagents.workspace / 'subagent_board.json'}")
    print(f"board_md={agent.subagents.workspace / 'SUBAGENT_BOARD.md'}")


def scenario_tasks_verified(agent: SimpleAgent, expected_count: int) -> bool:
    tasks = agent.subagents.list_runs()
    if len(tasks) < expected_count:
        return False
    return all(
        task.status == "DONE" and task.verification_status == "VERIFIED"
        for task in tasks[:expected_count]
    )


def collect_scenario_report_files(agent: SimpleAgent, fixture_root: Path, expected_count: int) -> list[Path]:

    report_files: list[Path] = []
    seen: set[Path] = set()
    for candidate in _scenario_report_candidates(agent.subagents.list_runs()[:expected_count], fixture_root):
        resolved = candidate.resolve(strict=False)
        if resolved in seen or not candidate.exists():
            continue
        seen.add(resolved)
        report_files.append(candidate)
    return sorted(report_files)


def _scenario_report_candidates(tasks: list[SubAgentTask], fixture_root: Path) -> list[Path]:

    candidates = list((fixture_root / "scenario_outputs").glob("*.md"))
    for task in tasks:
        task_dir = Path(task.task_dir)
        candidates.extend((task_dir / "scenario_outputs").glob("*.md"))
    return candidates


def write_scenario_summary(
    paths: ScenarioPaths,
    *,
    ok: bool,
    reason: str,
    extra: dict[str, object] | None = None,
) -> None:

    payload = {
        "ok": ok,
        "reason": reason,
        "run_root": str(paths.run_root),
        "fixture_root": str(paths.fixture_root),
        "config": str(paths.config),
        "summary_json": str(paths.summary_json),
        "summary_md": str(paths.summary_md),
        **(extra or {}),
    }
    paths.summary_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    lines = [
        "# Scenario Test Summary",
        "",
        f"- ok: {ok}",
        f"- reason: {reason}",
        f"- run_root: {paths.run_root}",
        f"- fixture_root: {paths.fixture_root}",
        f"- config: {paths.config}",
    ]
    if extra:
        lines.extend(["", "## Extra", "", "```json", json.dumps(extra, ensure_ascii=False, indent=2), "```"])
    paths.summary_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
