from __future__ import annotations

"""LLM: Live Lab runner for isolated config, logging, subprocesses, and summaries.

给人看的解释：
这个文件是测试台"总控室"。
它负责准备隔离工作区、打印和保存日志、执行命令、记录每个 case 的通过/失败。
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

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


# ─── Session Manager ───────────────────────────────────────────────────────────


class LabSessionManager:
    """LLM: owns workspace setup, config writing, and header logging.

    给人看的解释：
    测试前先建目录、写 fixture 小项目、写一份临时配置。
    这份配置会把 memory、gateway、subagent、LocalStore 都关进测试目录。
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

    def setup(self) -> None:
        """LLM: creates the isolated live-lab workspace and config."""
        if not self.source_config.exists():
            raise FileNotFoundError(f"配置文件不存在: {self.source_config}")
        self.fixture_root.mkdir(parents=True, exist_ok=True)
        self.prompts_dir.mkdir(parents=True, exist_ok=True)
        self.responses_dir.mkdir(parents=True, exist_ok=True)
        self.write_fixture()
        self.write_config()
        self.prepare_runtime_dirs()

    def log_header(self) -> None:
        """LLM: prints the run metadata that a human needs at the top of the terminal."""
        self._log("# MY-AGENT LIVE LAB")
        self._log("")
        self._log(f"- run_root: {self.run_root}")
        self._log(f"- fixture_root: {self.fixture_root}")
        self._log(f"- config: {self.config_path}")
        self._log(f"- transcript: {self.transcript_path}")
        self._log(f"- stop_file: {self.stop_file}")
        self._log(f"- suite: {self.args.suite}")
        self._log(f"- real_llm: {self.args.real_llm}")
        self._log("")
        self._log("如果要中途停止，在另一个终端执行：")
        self._log(f"`touch {shlex.quote(str(self.stop_file))}`")
        self._log("")

    def write_fixture(self) -> None:
        """LLM: writes a tiny project that real agents can safely read and modify."""
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
        """LLM: appends isolation overrides while preserving model/API settings."""
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
        """LLM: pre-creates runtime files that doctor expects in a fresh workspace."""
        event_path = self.fixture_root / ".my_agent" / "local_store" / "events.jsonl"
        event_path.parent.mkdir(parents=True, exist_ok=True)
        event_path.touch(exist_ok=True)


# ─── Reporter ──────────────────────────────────────────────────────────────────


class LabReporter:
    """LLM: owns output logging, model preflight, and summary writing."""

    def __init__(self, session: LabSessionManager, args: argparse.Namespace) -> None:
        self._session = session
        self.args = args

    def log_model_preflight(self) -> None:
        """LLM: logs backend/key presence without exposing secrets."""
        sys.path.insert(0, str(REPO_ROOT))
        from agent_py_agent.agent.config import load_config

        config = load_config(self._session.config_path)
        self._log("## Config Preflight")
        self._log("")
        self._log(f"- backend: {config.model_backend}")
        self._log(f"- model: {config.model_name}")
        self._log(f"- api_base: {config.api_base}")
        self._log(f"- api_key_env: {config.api_key_env}")
        self._log(f"- api_key_present: {bool(config.api_key)}")
        if self.args.real_llm and config.model_backend == "echo":
            self._log("- warning: `--real-llm` 已打开，但当前 backend 仍是 echo。")
        if self.args.real_llm and config.model_backend != "echo" and not config.api_key:
            self._log("- warning: 真实模型后端未读到 API key，本轮真实 case 可能会失败。")
        self._log("")

    def log_output(self, label: str, value: str) -> None:
        """LLM: prints a captured output block only when it is non-empty."""
        if not value:
            return
        self._log("")
        self._log(f"{label}:")
        self._log("```text")
        self._log(value.rstrip())
        self._log("```")

    def write_summary(self, results: list[dict[str, object]]) -> int:
        """LLM: writes final machine-readable run summary and returns process code."""
        failed = [item for item in results if item["status"] == "fail"]
        payload = {
            "ok": not failed,
            "suite": self.args.suite,
            "real_llm": self.args.real_llm,
            "run_root": str(self._session.run_root),
            "fixture_root": str(self._session.fixture_root),
            "config": str(self._session.config_path),
            "transcript": str(self._session.transcript_path),
            "stop_file": str(self._session.stop_file),
            "results": results,
        }
        self._session.summary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.section("SUMMARY")
        self._log(json.dumps(payload, ensure_ascii=False, indent=2))
        self._log("")
        self._log("LIVE_LAB_PASS" if payload["ok"] else "LIVE_LAB_FAIL")
        return 0 if payload["ok"] else 2

    def section(self, title: str) -> None:
        """LLM: renders a visible case boundary."""
        self._log("")
        self._log(f"## {title}")
        self._log("")

    # Internal helpers -----------------------------------------------------------

    def _log(self, message: str = "") -> None:
        """Write to transcript and terminal."""
        if hasattr(self._session, "_created") and self._session._created:
            with self._session.transcript_path.open("a", encoding="utf-8") as handle:
                handle.write(message + "\n")
        print(message, flush=True)


# ─── Runner ───────────────────────────────────────────────────────────────────


class LabRunner:
    """LLM: owns subprocess execution, prompt recording, and suite dispatch."""

    def __init__(self, session: LabSessionManager, reporter: LabReporter, args: argparse.Namespace) -> None:
        self._session = session
        self._reporter = reporter
        self.args = args
        self._results: list[dict[str, object]] = []

    @property
    def results(self) -> list[dict[str, object]]:
        return self._results

    def agent_command(self, *parts: str) -> list[str]:
        """LLM: builds a Python module invocation against the isolated config."""
        return [sys.executable, "-m", "agent_py_agent", "--config", str(self._session.config_path), *parts]

    def run_command(
        self,
        command: list[str],
        *,
        timeout: float | None = None,
        allow_fail: bool = False,
        input_text: str | None = None,
    ) -> CommandResult:
        """LLM: executes a subprocess, captures output, and enforces failures."""
        timeout = timeout if timeout is not None else self.args.timeout
        self._log("```bash")
        self._log("$ " + shlex.join(command))
        self._log("```")
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
        self._log(f"exit_code={result.returncode} elapsed={result.elapsed_seconds:.2f}s")
        self._reporter.log_output("stdout", result.stdout)
        self._reporter.log_output("stderr", result.stderr)
        if result.returncode != 0 and not allow_fail:
            raise LiveLabError(f"命令失败 exit_code={result.returncode}: {shlex.join(command)}")
        return result

    def record_prompt(self, case_name: str, prompt: str, *, label: str = "PROMPT SENT TO MY-AGENT") -> Path:
        """LLM: persists and prints the exact prompt used by a case."""
        prompt_path = self._session.prompts_dir / f"{case_name}.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        self._log(f"{label}:")
        self._log("```text")
        self._log(prompt)
        self._log("```")
        self._log(f"prompt_file={prompt_path}")
        return prompt_path

    def should_stop(self) -> bool:
        """LLM: checks the cooperative stop file before starting each case."""
        if self._session.stop_file.exists():
            self._log(f"STOP 文件已存在，停止后续 case: {self._session.stop_file}")
            return True
        return False

    def run_suite(self, run_case) -> int:
        """LLM: executes selected cases, records pass/fail/skip, and writes summary."""
        if self.args.suite in {"real", "all"} and not self.args.real_llm:
            self._log("suite 包含真实 LLM case，但没有传 `--real-llm`。")
            self._log("请显式运行：`scripts/open_live_lab.sh --suite real --real-llm`")
            return 2

        for case_name in SUITES[self.args.suite]:
            if self.should_stop():
                self._results.append({"case": case_name, "status": "skip", "reason": "stop file exists"})
                break
            if case_name in REAL_CASES and not self.args.real_llm:
                self._reporter.section(f"CASE {case_name} SKIPPED")
                self._log("这个 case 会调用真实模型。需要传 `--real-llm` 才会执行。")
                self._results.append({"case": case_name, "status": "skip", "reason": "real llm not enabled"})
                continue
            if not self.run_one_case(case_name, run_case):
                break
        return self._reporter.write_summary(self._results)

    def run_one_case(self, case_name: str, run_case) -> bool:
        """LLM: runs one case and records its terminal status."""
        started = time.monotonic()
        try:
            run_case(self, case_name)
        except Exception as exc:
            elapsed = time.monotonic() - started
            self._results.append({"case": case_name, "status": "fail", "error": str(exc), "elapsed_seconds": elapsed})
            self._log(f"CASE_FAIL case={case_name} error={exc}")
            return bool(self.args.keep_going)
        else:
            elapsed = time.monotonic() - started
            self._results.append({"case": case_name, "status": "pass", "elapsed_seconds": elapsed})
            self._log(f"CASE_PASS case={case_name} elapsed={elapsed:.2f}s")
            return True

    # Internal helpers -----------------------------------------------------------

    def _log(self, message: str = "") -> None:
        """Write to transcript and terminal."""
        if hasattr(self._session, "_created") and self._session._created:
            with self._session.transcript_path.open("a", encoding="utf-8") as handle:
                handle.write(message + "\n")
        print(message, flush=True)


# ─── Orchestrator ───────────────────────────────────────────────────────────────


class LiveLab:
    """LLM: coordinates isolated config, visible execution, transcript, and summaries.

    给人看的解释：
    你可以把它理解成一个测试主持人：
    先布置一个不会污染仓库的小房间，然后按顺序把任务交给 my-agent，
    同时把"交了什么、怎么跑的、跑成什么样"全部记下来。
    """

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self._session = LabSessionManager(args)
        self._reporter = LabReporter(self._session, args)
        self._runner = LabRunner(self._session, self._reporter, args)
        self.results: list[dict[str, object]] = []
        self._created = False

    def setup(self) -> None:
        """LLM: creates the isolated live-lab workspace and config."""
        self._session.setup()
        self._created = True
        self._reporter.log_header()
        self._reporter.log_model_preflight()

    @property
    def transcript_path(self) -> Path:
        return self._session.transcript_path

    @property
    def fixture_root(self) -> Path:
        return self._session.fixture_root

    @property
    def config_path(self) -> Path:
        return self._session.config_path

    @property
    def stop_file(self) -> Path:
        return self._session.stop_file

    def log(self, message: str = "") -> None:
        """LLM: writes one line to both terminal and transcript."""
        if self._created:
            with self._session.transcript_path.open("a", encoding="utf-8") as handle:
                handle.write(message + "\n")
        print(message, flush=True)

    def section(self, title: str) -> None:
        """LLM: renders a visible case boundary."""
        self.log("")
        self.log(f"## {title}")
        self.log("")

    def agent_command(self, *parts: str) -> list[str]:
        """LLM: builds a Python module invocation against the isolated config."""
        return self._runner.agent_command(*parts)

    def run_command(
        self,
        command: list[str],
        *,
        timeout: float | None = None,
        allow_fail: bool = False,
        input_text: str | None = None,
    ) -> CommandResult:
        """LLM: executes a subprocess, captures output, and enforces failures."""
        return self._runner.run_command(command, timeout=timeout, allow_fail=allow_fail, input_text=input_text)

    def log_output(self, label: str, value: str) -> None:
        """LLM: prints a captured output block only when it is non-empty."""
        self._reporter.log_output(label, value)

    def record_prompt(self, case_name: str, prompt: str, *, label: str = "PROMPT SENT TO MY-AGENT") -> Path:
        """LLM: persists and prints the exact prompt used by a case."""
        return self._runner.record_prompt(case_name, prompt, label=label)

    def should_stop(self) -> bool:
        """LLM: checks the cooperative stop file before starting each case."""
        return self._runner.should_stop()

    def run_suite(self) -> int:
        """LLM: executes selected cases, records pass/fail/skip, and writes summary."""
        return self._runner.run_suite(run_case=_run_case_wrapper)

    def run_one_case(self, case_name: str, run_case) -> bool:
        """LLM: runs one case and records its terminal status."""
        return self._runner.run_one_case(case_name, run_case)

    def write_summary(self) -> int:
        """LLM: writes final machine-readable run summary and returns process code."""
        return self._reporter.write_summary(self._runner.results)


def _run_case_wrapper(lab: LabRunner, case_name: str) -> None:
    """Bridge that unpacks the LiveLab-style interface for run_case."""
    from .cases import run_case as _orig_run_case

    class _LabInterface:
        """Thin adapter that exposes the LabRunner surface as the old LiveLab surface."""

        def __init__(self, runner: LabRunner, reporter: LabReporter, session: LabSessionManager):
            self._runner = runner
            self._reporter = reporter
            self._session = session

        @property
        def args(self):
            return self._runner.args

        @property
        def transcript_path(self):
            return self._session.transcript_path

        @property
        def fixture_root(self):
            return self._session.fixture_root

        @property
        def config_path(self):
            return self._session.config_path

        @property
        def stop_file(self):
            return self._session.stop_file

        def log(self, message: str = "") -> None:
            self._runner._log(message)

        def section(self, title: str) -> None:
            self._reporter.section(title)

        def agent_command(self, *parts: str) -> list[str]:
            return self._runner.agent_command(*parts)

        def run_command(self, command: list[str], *, timeout=None, allow_fail=False, input_text=None):
            return self._runner.run_command(command, timeout=timeout, allow_fail=allow_fail, input_text=input_text)

        def log_output(self, label: str, value: str) -> None:
            self._reporter.log_output(label, value)

        def record_prompt(self, case_name: str, prompt: str, *, label: str = "PROMPT SENT TO MY-AGENT"):
            return self._runner.record_prompt(case_name, prompt, label=label)

        def should_stop(self) -> bool:
            return self._runner.should_stop()

    from .constants import REAL_CASES, SUITES

    _lab_interface = _LabInterface(lab._runner, lab._reporter, lab._session)
    _orig_run_case(_lab_interface, case_name)
