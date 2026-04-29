from __future__ import annotations

"""LLM: Live Lab runner for isolated config, logging, subprocesses, and summaries.

给人看的解释：
这个文件是测试台“总控室”。
它负责准备隔离工作区、打印和保存日志、执行命令、记录每个 case 的通过/失败。
"""

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import uuid

from .constants import REAL_CASES, REPO_ROOT, SUITES


@dataclass
class CommandResult:
    """LLM: immutable result record for one child process invocation.

    给人看的解释：
    每跑一条命令，我们都把命令、退出码、输出和耗时收起来。
    后面写 summary 时，就不用靠肉眼回翻终端。
    """

    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    elapsed_seconds: float


class LiveLabError(RuntimeError):
    """LLM: raised when a live lab command fails and the suite should stop.

    给人看的解释：
    这是测试台自己的错误类型。
    比如 gateway 启动失败、场景测试没过，就会抛这个错误，最后汇总里会标红。
    """


class LiveLab:
    """LLM: coordinates isolated config, visible execution, transcript, and summaries.

    给人看的解释：
    你可以把它理解成一个测试主持人：
    先布置一个不会污染仓库的小房间，然后按顺序把任务交给 my-agent，
    同时把“交了什么、怎么跑的、跑成什么样”全部记下来。
    """

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.source_config = Path(args.config).expanduser().resolve()
        self.runs_dir = Path(args.runs_dir).expanduser().resolve()
        stamp = args.run_id or time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.run_root = self.runs_dir / stamp
        self.fixture_root = self.run_root / "fixture_project"
        self.prompts_dir = self.run_root / "prompts"
        self.responses_dir = self.run_root / "responses"
        self.config_path = self.run_root / "live_agent_config.yaml"
        self.transcript_path = self.run_root / "TRANSCRIPT.md"
        self.summary_path = self.run_root / "live_lab_summary.json"
        self.stop_file = self.run_root / "STOP"
        self.results: list[dict[str, object]] = []
        self.created = False

    def setup(self) -> None:
        """LLM: creates the isolated live-lab workspace and config.

        给人看的解释：
        测试前先建目录、写 fixture 小项目、写一份临时配置。
        这份配置会把 memory、gateway、subagent、LocalStore 都关进测试目录。
        """

        if not self.source_config.exists():
            raise FileNotFoundError(f"配置文件不存在: {self.source_config}")
        self.fixture_root.mkdir(parents=True, exist_ok=True)
        self.prompts_dir.mkdir(parents=True, exist_ok=True)
        self.responses_dir.mkdir(parents=True, exist_ok=True)
        self.write_fixture()
        self.write_config()
        self.prepare_runtime_dirs()
        self.created = True
        self.log_header()
        self.log_model_preflight()

    def log_header(self) -> None:
        """LLM: prints the run metadata that a human needs at the top of the terminal.

        给人看的解释：
        一开始先告诉你本轮目录、配置、transcript 和停止文件在哪。
        长任务跑起来之后，这些路径就是我们排查问题的入口。
        """

        self.log("# MY-AGENT LIVE LAB")
        self.log("")
        self.log(f"- run_root: {self.run_root}")
        self.log(f"- fixture_root: {self.fixture_root}")
        self.log(f"- config: {self.config_path}")
        self.log(f"- transcript: {self.transcript_path}")
        self.log(f"- stop_file: {self.stop_file}")
        self.log(f"- suite: {self.args.suite}")
        self.log(f"- real_llm: {self.args.real_llm}")
        self.log("")
        self.log("如果要中途停止，在另一个终端执行：")
        self.log(f"`touch {shlex.quote(str(self.stop_file))}`")
        self.log("")

    def write_fixture(self) -> None:
        """LLM: writes a tiny project that real agents can safely read and modify.

        给人看的解释：
        真实 runner 需要一个可以动手的项目。
        这里准备 README、输入素材和输出目录，所有读写都限制在这个小项目里。
        """

        (self.fixture_root / "README.md").write_text(
            "\n".join(
                [
                    "# Live Agent Lab Fixture",
                    "",
                    "这是 my-agent Live Lab 的隔离项目。",
                    "真实模型可以读取这里的文件，也可以把测试报告写到 lab_outputs/。",
                    "",
                    "## 任务素材",
                    "",
                    "- 你需要证明自己读到了 README。",
                    "- 你需要把输出留在 lab_outputs/。",
                    "- 不要读写 fixture_project 之外的文件。",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (self.fixture_root / "notes").mkdir(parents=True, exist_ok=True)
        (self.fixture_root / "notes" / "small_task.md").write_text(
            "小任务素材：总结 README 的目标，并说明输出文件路径。\n",
            encoding="utf-8",
        )
        (self.fixture_root / "notes" / "problem_task.md").write_text(
            "问题任务素材：如果证据不足，应明确说缺什么，不要假装完成。\n",
            encoding="utf-8",
        )
        (self.fixture_root / "lab_outputs").mkdir(parents=True, exist_ok=True)

    def write_config(self) -> None:
        """LLM: appends isolation overrides while preserving model/API settings.

        给人看的解释：
        我们复用你当前的模型配置，但把所有数据路径改到 Live Lab 目录。
        如果没显式加 `--real-llm`，还会把模型后端临时改成 echo，避免误烧真实 API。
        """

        base = self.source_config.read_text(encoding="utf-8")
        fixture = str(self.fixture_root).replace("\\", "/")
        backend_override = "" if self.args.real_llm else '\nmodel_backend: "echo"\n'
        overrides = f"""

# live-agent-lab isolation overrides
workspace_root: "{fixture}"
prompt_files:
memory_path: ".my_agent/memory.jsonl"
local_store_path: ".my_agent/local_store/local.db"
local_store_files_dir: ".my_agent/local_store/files"
local_store_events_path: ".my_agent/local_store/events.jsonl"
subagent_workspace: ".my_agent/subagents"
gateway_workspace: ".my_agent/gateway"
adapter_workspace: ".my_agent/adapters/file"
max_subagents: {max(self.args.count, 1)}
gateway_request_timeout: {int(self.args.timeout)}
gateway_request_poll_interval: 1
gateway_request_workers: 1
gateway_processing_timeout_seconds: {max(int(self.args.timeout) + 120, 180)}
gateway_request_max_attempts: 2
daemon_planner: false
daemon_apply: false
daemon_execute_runners: false
daemon_max_runners: 0
daemon_interval: 1
runner_failure_policy: "auto"
max_tool_rounds: 8
request_timeout: {max(30, int(self.args.timeout))}
{backend_override}"""
        self.config_path.write_text(base + overrides, encoding="utf-8")

    def prepare_runtime_dirs(self) -> None:
        """LLM: pre-creates runtime files that doctor expects in a fresh workspace.

        给人看的解释：
        全新的测试工作区没有任何历史事件。
        为了让健康检查表达“这是空账本”而不是“文件缺失”，这里会先创建空的审计流水文件。
        """

        event_path = self.fixture_root / ".my_agent" / "local_store" / "events.jsonl"
        event_path.parent.mkdir(parents=True, exist_ok=True)
        event_path.touch(exist_ok=True)

    def log_model_preflight(self) -> None:
        """LLM: logs backend/key presence without exposing secrets.

        给人看的解释：
        跑真实模型前，最常见的问题是 key 没配或后端还是 echo。
        这里会打印“有没有 key”，但不会把 key 内容泄露出来。
        """

        sys.path.insert(0, str(REPO_ROOT))
        from agent_py_agent.agent.config import load_config

        config = load_config(self.config_path)
        self.log("## Config Preflight")
        self.log("")
        self.log(f"- backend: {config.model_backend}")
        self.log(f"- model: {config.model_name}")
        self.log(f"- api_base: {config.api_base}")
        self.log(f"- api_key_env: {config.api_key_env}")
        self.log(f"- api_key_present: {bool(config.api_key)}")
        if self.args.real_llm and config.model_backend == "echo":
            self.log("- warning: `--real-llm` 已打开，但当前 backend 仍是 echo。")
        if self.args.real_llm and config.model_backend != "echo" and not config.api_key:
            self.log("- warning: 真实模型后端未读到 API key，本轮真实 case 可能会失败。")
        self.log("")

    def log(self, message: str = "") -> None:
        """LLM: writes one line to both terminal and transcript.

        给人看的解释：
        终端里看到什么，transcript 里也会尽量留下。
        这样测试跑完后，不用靠记忆复盘。
        """

        if self.created:
            with self.transcript_path.open("a", encoding="utf-8") as handle:
                handle.write(message + "\n")
        print(message, flush=True)

    def section(self, title: str) -> None:
        """LLM: renders a visible case boundary.

        给人看的解释：
        每个测试 case 开始时打一条醒目的分隔线，方便你盯终端时知道现在跑到哪一步。
        """

        self.log("")
        self.log(f"## {title}")
        self.log("")

    def agent_command(self, *parts: str) -> list[str]:
        """LLM: builds a Python module invocation against the isolated config.

        给人看的解释：
        所有 my-agent 命令都从这里拼，确保每条命令都用 Live Lab 的临时配置。
        """

        return [sys.executable, "-m", "agent_py_agent", "--config", str(self.config_path), *parts]

    def run_command(
        self,
        command: list[str],
        *,
        timeout: float | None = None,
        allow_fail: bool = False,
        input_text: str | None = None,
    ) -> CommandResult:
        """LLM: executes a subprocess, captures output, and enforces failures.

        给人看的解释：
        这里真正跑命令。
        每条命令会打印 `$ ...`、耗时、退出码、stdout、stderr；失败时默认停止本轮测试。
        """

        timeout = timeout if timeout is not None else self.args.timeout
        self.log("```bash")
        self.log("$ " + shlex.join(command))
        self.log("```")
        started = time.monotonic()
        env = os.environ.copy()
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            input=input_text,
            env=env,
            timeout=timeout,
        )
        result = CommandResult(
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            elapsed_seconds=time.monotonic() - started,
        )
        self.log(f"exit_code={result.returncode} elapsed={result.elapsed_seconds:.2f}s")
        self.log_output("stdout", result.stdout)
        self.log_output("stderr", result.stderr)
        if result.returncode != 0 and not allow_fail:
            raise LiveLabError(f"命令失败 exit_code={result.returncode}: {shlex.join(command)}")
        return result

    def log_output(self, label: str, value: str) -> None:
        """LLM: prints a captured output block only when it is non-empty.

        给人看的解释：
        stdout/stderr 为空时就不刷屏。
        有内容时用代码块包起来，方便 transcript 后续阅读。
        """

        if not value:
            return
        self.log("")
        self.log(f"{label}:")
        self.log("```text")
        self.log(value.rstrip())
        self.log("```")

    def record_prompt(self, case_name: str, prompt: str, *, label: str = "PROMPT SENT TO MY-AGENT") -> Path:
        """LLM: persists and prints the exact prompt used by a case.

        给人看的解释：
        你关心“到底给智能体发了什么”。
        这个函数会把 prompt 原文写成文件，也会在终端里完整打印出来。
        """

        prompt_path = self.prompts_dir / f"{case_name}.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        self.log(f"{label}:")
        self.log("```text")
        self.log(prompt)
        self.log("```")
        self.log(f"prompt_file={prompt_path}")
        return prompt_path

    def should_stop(self) -> bool:
        """LLM: checks the cooperative stop file before starting each case.

        给人看的解释：
        真实长任务可能跑很久。
        你只要 touch 这个 STOP 文件，下一段 case 开始前就会停下。
        """

        if self.stop_file.exists():
            self.log(f"STOP 文件已存在，停止后续 case: {self.stop_file}")
            return True
        return False

    def run_suite(self) -> int:
        """LLM: executes selected cases, records pass/fail/skip, and writes summary.

        给人看的解释：
        这里是总调度：按 suite 顺序一个个跑。
        每个 case 结束都会记结果；如果失败，默认停住，除非你传了 `--keep-going`。
        """

        if self.args.suite in {"real", "all"} and not self.args.real_llm:
            self.log("suite 包含真实 LLM case，但没有传 `--real-llm`。")
            self.log("请显式运行：`scripts/open_live_lab.sh --suite real --real-llm`")
            return 2

        from .cases import run_case

        for case_name in SUITES[self.args.suite]:
            if self.should_stop():
                self.results.append({"case": case_name, "status": "skip", "reason": "stop file exists"})
                break
            if case_name in REAL_CASES and not self.args.real_llm:
                self.section(f"CASE {case_name} SKIPPED")
                self.log("这个 case 会调用真实模型。需要传 `--real-llm` 才会执行。")
                self.results.append({"case": case_name, "status": "skip", "reason": "real llm not enabled"})
                continue
            if not self.run_one_case(case_name, run_case):
                break
        return self.write_summary()

    def run_one_case(self, case_name: str, run_case) -> bool:
        """LLM: runs one case and records its terminal status.

        给人看的解释：
        单个 case 的 try/except 放这里，避免总循环里塞太多细节。
        失败时是否继续，由 `--keep-going` 控制。
        """

        started = time.monotonic()
        try:
            run_case(self, case_name)
        except Exception as exc:
            elapsed = time.monotonic() - started
            self.results.append({"case": case_name, "status": "fail", "error": str(exc), "elapsed_seconds": elapsed})
            self.log(f"CASE_FAIL case={case_name} error={exc}")
            return bool(self.args.keep_going)
        else:
            elapsed = time.monotonic() - started
            self.results.append({"case": case_name, "status": "pass", "elapsed_seconds": elapsed})
            self.log(f"CASE_PASS case={case_name} elapsed={elapsed:.2f}s")
            return True

    def write_summary(self) -> int:
        """LLM: writes final machine-readable run summary and returns process code.

        给人看的解释：
        最后收口成一个 JSON：哪些 case 过了，哪些失败，证据在哪。
        脚本退出码也从这里决定，方便后续接 CI 或自动回归。
        """

        failed = [item for item in self.results if item["status"] == "fail"]
        payload = {
            "ok": not failed,
            "suite": self.args.suite,
            "real_llm": self.args.real_llm,
            "run_root": str(self.run_root),
            "fixture_root": str(self.fixture_root),
            "config": str(self.config_path),
            "transcript": str(self.transcript_path),
            "stop_file": str(self.stop_file),
            "results": self.results,
        }
        self.summary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.section("SUMMARY")
        self.log(json.dumps(payload, ensure_ascii=False, indent=2))
        self.log("")
        self.log("LIVE_LAB_PASS" if payload["ok"] else "LIVE_LAB_FAIL")
        return 0 if payload["ok"] else 2
