# LLM: 场景框架的通用底座；Gateway 子进程生命周期只在 run_scenario_gateway_ask 内闭合（start 之后必停），
#   停止只认场景 Gateway 工作区 pid 记录这一文件系统事实。改动 start/stop 顺序须同步 test_scenario_utils.py。
# 模块用途: 场景测试的临时工作区、固定 prompt、Gateway 子进程起停和 summary 输出。
from __future__ import annotations

"""provides scenario-test workspace setup, fixture writing, subprocess helpers, and summary output.

给人看的解释：
场景测试需要临时项目、隔离配置、固定 prompt、gateway 子进程和最终报告。
这些通用小工具都放这里，具体测试 case 只描述自己要验证的行为。
Gateway 一旦被场景启动，就绪超时、ask 失败或异常都会按 pid 把它停掉，不留后台进程。
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..agent.core import SimpleAgent
from ..agent.gateway_parts import gateway_paths, is_pid_alive, terminate_pid, wait_for_pid_exit
from ..agent.gateway_parts.daemon_control import get_running_pid
from ..agent.subagents.models import SubAgentBoardOptions, SubAgentTask, TaskStatus
from .common import ROOT, make_agent
from .scenario_workspace import ScenarioConfigRequest, write_scenario_config, write_scenario_fixture


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
    write_scenario_config(ScenarioConfigRequest(
        source_config=Path(args.config),
        target_config=config_path,
        fixture_root=fixture_root,
        request_timeout=args.timeout,
        max_subagents=max(args.count, 1),
        runner_concurrency=_optional_arg(args, "runner_concurrency"),
        runner_start_rate=_optional_arg(args, "runner_start_rate"),
        model_request_timeout=_optional_arg(args, "model_request_timeout"),
    ))
    return ScenarioPaths(
        run_root=run_root,
        fixture_root=fixture_root,
        config=config_path,
        summary_json=run_root / "scenario_summary.json",
        summary_md=run_root / "SCENARIO_SUMMARY.md",
    )


def _optional_arg(args: object, name: str) -> object | None:
    data = vars(args) if hasattr(args, "__dict__") else {}
    value = data.get(name)
    if value in {"", None}:
        return None
    return value


def load_scenario_agent(config_path: Path) -> SimpleAgent:

    class Args:
        config = str(config_path)

    return make_agent(Args())


def install_scenario_backend(agent: SimpleAgent, backend: object) -> None:

    agent.backend = backend
    agent._subagent_worker_backend_override = backend


def build_scenario_prompt(count: int) -> str:

    return (
        "这是一个隔离测试项目，请找几个帮手一起完成，最后由你检查他们交回来的结果。\n\n"
        f"请安排 {count} 个帮手分别去读当前项目里的 README.md。"
        "每个帮手都要在自己的任务目录里写一份 3-6 行中文证据报告，"
        "说明自己确实读到了 README.md，并写清楚报告文件放在哪里。\n\n"
        "你这一轮只负责把事情分配清楚，并简单看一下有没有成功分配出去；"
        "不要自己替他们读 README.md，也不要自己直接写最终报告。"
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
        "task_dir/scenario_outputs/<run_id>.md；content 写一份 3-6 行中文报告，"
        "说明已读取 README.md，并注明这是隔离测试和 task_dir 内产物。\n"
        "4. 只有在你已经看到 write_file 成功结果后，才允许输出最终 [SUBAGENT_RESULT]。\n"
        "5. 最终回复只能包含一个 [SUBAGENT_RESULT] JSON 结果块，不要输出 Markdown 代码围栏。\n"
        "JSON 必须包含：status=DONE；summary；used_tools 至少包含 read_file 和 write_file；"
        "evidence 至少两条，分别证明 README.md 已读取、task_dir/scenario_outputs/<run_id>.md 已写入；"
        "tests 至少一条 ok=true；artifacts 包含写入的报告路径；patches 为空数组。"
    )


def scenario_command(paths: ScenarioPaths, *parts: str) -> list[str]:

    return [sys.executable, "-m", "agent_py_agent", "--config", str(paths.config), *parts]


# 场景 Gateway 就绪等待预算：跟随本次 ask 超时换算，夹在 10-60 秒之间。全仓分片高负载时默认的 3 秒
# 会把“还在起来”的 Gateway 报成启动失败（exit 2）；这里只决定等多久，就绪判据仍由 gateway start 自己裁决。
_SCENARIO_GATEWAY_READY_MIN_SECONDS = 10.0
# 场景脚本等待 Gateway 就绪的预算上限 60 秒
_SCENARIO_GATEWAY_READY_MAX_SECONDS = 60.0
# --force 会先停旧实例再冷启动，start 子进程自身的时限要在就绪预算之外再留出停止等待的余量。
_SCENARIO_GATEWAY_START_EXTRA_SECONDS = 60.0
# 正式 stop 停不掉时按 pid 升级终止：SIGTERM 后等这么久，仍活着再 SIGKILL 并再等一次。
_SCENARIO_GATEWAY_STOP_GRACE_SECONDS = 5.0


def _scenario_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


# LLM: 预算只影响 gateway start 等多久，就绪判据不变；非法输入按 0 处理并落到下限。
# 函数用途: 按本次 ask 超时换算 Gateway 就绪等待秒数，夹在 10-60 秒之间。
def scenario_gateway_ready_budget_seconds(timeout: object) -> float:
    try:
        requested = float(timeout or 0)
    except (TypeError, ValueError):
        requested = 0.0
    return max(_SCENARIO_GATEWAY_READY_MIN_SECONDS, min(_SCENARIO_GATEWAY_READY_MAX_SECONDS, requested))


# LLM: Gateway 生命周期在本函数闭合：start 一经发出，就绪超时、ask 失败、子进程超时或任何异常都走 finally，
#   由 stop_scenario_gateway 按 pid 停掉本次启动的 Gateway；停止事实以结构化字段 gateway_stop 附在返回值里，
#   异常照常向上抛。调用方（scenario.py、gateway_cross_day_case.py）只看 ok/error/gateway_stop 字段。
# 函数用途: 起一个隔离的后台 Gateway、发一条 ask 拿回 JSON 结果，最后保证 Gateway 进程不残留。
def run_scenario_gateway_ask(
    paths: ScenarioPaths,
    prompt: str,
    *,
    timeout: float,
    save: bool = False,
) -> dict[str, object]:
    env = _scenario_subprocess_env()
    try:
        payload = _run_scenario_gateway_ask_unstopped(paths, _ScenarioAsk(prompt, timeout, save), env=env)
    finally:
        stop_facts = stop_scenario_gateway(paths, env=env)
    payload["gateway_stop"] = stop_facts
    return payload


# LLM: 一次场景 ask 的请求参数（原话、超时、是否保存会话）；只读值对象，由 run_scenario_gateway_ask 构造。
# 类用途: 把场景 ask 的三项参数收成一个对象，传给只负责 start -> ask 的内部函数。
@dataclass(frozen=True)
class _ScenarioAsk:
    prompt: str
    timeout: float
    save: bool


# LLM: 只负责 start -> ask -> 解析，不停 Gateway；停止由 run_scenario_gateway_ask 的 finally 兜底。
# 函数用途: 启动场景 Gateway 并发送一条 ask，把 CLI 的 JSON 回复原样返回。
def _run_scenario_gateway_ask_unstopped(
    paths: ScenarioPaths,
    ask_request: _ScenarioAsk,
    *,
    env: dict[str, str],
) -> dict[str, object]:
    prompt, timeout, save = ask_request.prompt, ask_request.timeout, ask_request.save
    ready_budget = scenario_gateway_ready_budget_seconds(timeout)
    start = run_scenario_subprocess(
        scenario_command(paths, "gateway", "start", "--force", "--ready-timeout", str(ready_budget)),
        env=env,
        timeout=ready_budget + _SCENARIO_GATEWAY_START_EXTRA_SECONDS,
    )
    if start.returncode != 0:
        return {"ok": False, "error": "gateway start failed", "stdout": start.stdout, "stderr": start.stderr}
    ask_command = scenario_command(paths, "gateway", "ask", prompt, "--timeout", str(timeout), "--json")
    if not save:
        ask_command.append("--no-save")
    ask = run_scenario_subprocess(ask_command, env=env, timeout=timeout + 30)
    if ask.returncode != 0:
        return {"ok": False, "error": "gateway ask failed", "stdout": ask.stdout, "stderr": ask.stderr}
    try:
        payload = json.loads(ask.stdout)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"gateway response was not JSON: {exc}", "stdout": ask.stdout}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "gateway response was not a JSON object", "stdout": ask.stdout}
    return payload


# LLM: 只认本次场景 Gateway 工作区 pid 记录里的活进程（文件系统事实，经 get_running_pid 核对代际），不解析 stdout。
#   先走正式 `gateway stop --kill`；进程仍在（还没发布 running 的 Gateway 可能不处理停止请求）就按 pid 整棵升级终止。
#   本函数不抛异常，所有结果作为结构化事实返回并打印一行 gateway_stop=…，供场景 summary 和人工核对。
# 函数用途: 场景结束（含失败/超时/异常）时把自己启动的 Gateway 停干净，并给出可核对的停止事实。
def stop_scenario_gateway(paths: ScenarioPaths, *, env: dict[str, str]) -> dict[str, object]:
    pid, pid_error = _scenario_gateway_pid(paths)
    facts: dict[str, object] = {
        "pid": pid,
        "stop_returncode": None,
        "stop_error": "",
        "terminated_by_pid": False,
        "alive_after_stop": False,
    }
    if pid_error:
        facts["pid_error"] = pid_error
    try:
        stop = run_scenario_subprocess(
            scenario_command(paths, "gateway", "stop", "--timeout", "10", "--kill", "--reason", "scenario-test done"),
            env=env,
            timeout=30,
        )
        facts["stop_returncode"] = stop.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        facts["stop_error"] = f"{type(exc).__name__}: {exc}"
    if pid and is_pid_alive(pid):
        facts["terminated_by_pid"] = True
        facts["termination"] = _terminate_scenario_gateway_pid(pid)
    facts["alive_after_stop"] = bool(pid) and is_pid_alive(pid)
    print("gateway_stop=" + json.dumps(facts, ensure_ascii=False, sort_keys=True, default=str))
    return facts


# LLM: cli 层不能引用 agent.subagents/agent.tooling 的进程树终止器（import 边界），这里只用 gateway_parts 的
#   进程原语：SIGTERM（Windows 即 TerminateProcess）等宽限，仍活着再 SIGKILL；只对本次记录的 pid 动手，不杀进程组。
# 函数用途: 正式 stop 停不掉时按 pid 升级终止场景 Gateway，返回结构化终止回执。
def _terminate_scenario_gateway_pid(pid: int) -> dict[str, object]:
    terminate_pid(pid)
    if wait_for_pid_exit(pid, _SCENARIO_GATEWAY_STOP_GRACE_SECONDS):
        return {"method": "SIGTERM", "exited": True}
    if os.name != "nt":
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return {"method": "SIGTERM->SIGKILL", "exited": wait_for_pid_exit(pid, _SCENARIO_GATEWAY_STOP_GRACE_SECONDS)}


# LLM: pid 只来自场景配置解析出的 Gateway 工作区 pid 记录；读不到或配置坏了只返回错误事实，停止流程继续。
# 函数用途: 找出本次场景启动的 Gateway 进程号。
def _scenario_gateway_pid(paths: ScenarioPaths) -> tuple[int | None, str]:
    try:
        agent = load_scenario_agent(paths.config)
        return get_running_pid(gateway_paths(agent).pid, cleanup_stale=False), ""
    except Exception as exc:  # 配置或工作区异常不能让停止兜底跳过；错误只作为结构化事实返回
        return None, f"{type(exc).__name__}: {exc}"


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

    board = agent.subagents.board.write_board(options=SubAgentBoardOptions(recent_limit=limit))
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


def scenario_tasks_active(agent: SimpleAgent, expected_count: int) -> bool:
    tasks = agent.subagents.list_runs()
    if len(tasks) < expected_count:
        return False
    active_statuses = {
        TaskStatus.PLANNING.value,
        TaskStatus.PENDING.value,
        TaskStatus.RUNNING.value,
    }
    return any(task.status in active_statuses for task in tasks[:expected_count])


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
        candidates.extend(_scenario_output_json_artifact_paths(task))
        if task.agent_run_workspace_dir:
            candidates.extend((Path(task.agent_run_workspace_dir) / "scenario_outputs").glob("*.md"))
        if task.task_workspace_dir:
            candidates.extend((Path(task.task_workspace_dir) / "work" / "agents" / task.id / "scenario_outputs").glob("*.md"))
    return candidates


def _scenario_output_json_artifact_paths(task: SubAgentTask) -> list[Path]:
    output_json = Path(task.output_json) if task.output_json else None
    if not output_json or not output_json.is_file():
        return []
    try:
        payload = json.loads(output_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        return []
    paths: list[Path] = []
    for item in artifacts:
        if not isinstance(item, dict):
            continue
        path_text = _scenario_artifact_path_text(item)
        if not path_text:
            continue
        candidate = Path(path_text)
        if candidate.suffix.lower() == ".md":
            paths.append(candidate)
    return paths


def _scenario_artifact_path_text(item: dict[str, object]) -> str:
    path_text = item.get("path")
    if isinstance(path_text, str) and path_text.strip():
        return path_text
    registry_ref = item.get("registry_ref")
    if not isinstance(registry_ref, dict):
        return ""
    path_text = registry_ref.get("path")
    return path_text if isinstance(path_text, str) and path_text.strip() else ""


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
