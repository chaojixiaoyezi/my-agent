
from __future__ import annotations

"""Live Lab runner for command execution and suite orchestration.

这个文件是测试台执行入口。隔离目录和日志分别在 session/reporter 模块里，
这里保留命令执行和 case 调度。
"""

import argparse
import json
import shlex
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .compact_observations import (
    LiveLabError,
    assert_isolated_home_path,
    collect_bg_process_census,
    diff_bg_process_census,
    prepare_isolated_subprocess_env,
)
from .constants import REAL_CASES, REPO_ROOT, SUITES
from .reporter import LabReporter
from .session import LabSessionManager


@dataclass
class CommandResult:
    """immutable result record for one child process invocation.

    每跑一条命令，我们都把命令、退出码、输出和耗时收起来。
    后面写 summary 时，就不用靠肉眼回翻终端。"""

    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    elapsed_seconds: float


class LabRunner:
    """owns subprocess execution, prompt recording, and suite dispatch."""

    def __init__(self, session: LabSessionManager, reporter: LabReporter, args: argparse.Namespace) -> None:
        self._session = session
        self._reporter = reporter
        self.args = args
        self._results: list[dict[str, object]] = []
        self._home_isolation_provenance: dict[str, Any] = {}
        self._bg_census_pre: dict[str, Any] | None = None
        self._bg_census_post: dict[str, Any] | None = None
        self._bg_census_diff: dict[str, Any] | None = None
        self._case_telemetry: dict[str, dict[str, Any]] = {}
        self._enforce_session_home_isolation()

    def _enforce_session_home_isolation(self) -> None:
        assert_isolated_home_path(self._session.run_root, label="run_root")
        isolated_home = getattr(self._session, "isolated_home", None) or (self._session.run_root / "home")
        assert_isolated_home_path(isolated_home, label="isolated_home")

    @property
    def results(self) -> list[dict[str, object]]:
        return self._results

    def record_case_telemetry(self, case_name: str, telemetry: Mapping[str, Any]) -> None:
        """Store structured case telemetry for inclusion in live_lab_summary.json."""
        self._case_telemetry[str(case_name)] = dict(telemetry)

    def agent_command(self, *parts: str) -> list[str]:
        """builds a Python module invocation against the isolated config."""
        return [sys.executable, "-m", "agent_py_agent", "--config", str(self._session.config_path), *parts]

    def run_command(
        self,
        command: list[str],
        *,
        timeout: float | None = None,
        allow_fail: bool = False,
        input_text: str | None = None,
    ) -> CommandResult:
        """executes a subprocess, captures output, enforces isolation, and tracks turn usage."""
        timeout = timeout if timeout is not None else self.args.timeout
        child_env, env_prov = _isolated_subprocess_env(self._session)
        self._home_isolation_provenance = env_prov

        self._log("```bash")
        self._log("$ " + shlex.join(command))
        self._log("```")
        track_usage = (
            hasattr(self._session, "begin_turn_usage")
            and hasattr(self._session, "end_turn_usage")
            and len(command) >= 3
            and command[1:3] == ["-m", "agent_py_agent"]
        )
        turn_label = " ".join(command[3:5]) if len(command) >= 4 else "agent"
        turn_token = self._session.begin_turn_usage(turn_label) if track_usage else None
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=REPO_ROOT,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                input=input_text,
                env=child_env,
                timeout=timeout,
            )
        finally:
            if track_usage:
                self._session.end_turn_usage(turn_token, turn_label=turn_label)
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
        """persists and prints the exact prompt used by a case."""
        prompt_path = self._session.prompts_dir / f"{case_name}.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        self._log(f"{label}:")
        self._log("```text")
        self._log(prompt)
        self._log("```")
        self._log(f"prompt_file={prompt_path}")
        return prompt_path

    def should_stop(self) -> bool:
        """checks the cooperative stop file before starting each case."""
        if self._session.stop_file.exists():
            self._log(f"STOP 文件已存在，停止后续 case: {self._session.stop_file}")
            return True
        return False

    def run_suite(self, run_case) -> int:
        """executes selected cases, captures pre/post bg-* census, and writes summary."""
        if self.args.suite in {"real", "all"} and not self.args.real_llm:
            self._log("suite 包含真实 LLM case，但没有传 `--real-llm`。")
            self._log("请显式运行：`scripts/open_live_lab.sh --suite real --real-llm`")
            return 2

        store_root = getattr(self._session, "process_store_root", None)
        self._bg_census_pre = collect_bg_process_census(store_root)
        try:
            for case_name in SUITES[self.args.suite]:
                if self.should_stop():
                    self._results.append({"case": case_name, "status": "skip", "reason": "stop file exists"})
                    break
                if self._skip_real_case(case_name):
                    continue
                if not self.run_one_case(case_name, run_case):
                    break
        finally:
            self._bg_census_post = collect_bg_process_census(store_root)
            self._bg_census_diff = diff_bg_process_census(self._bg_census_pre, self._bg_census_post)

        return self._write_enriched_summary()

    def _write_enriched_summary(self) -> int:
        exit_code = self._reporter.write_summary(self._results)
        self._augment_summary_file()
        return exit_code

    def _augment_summary_file(self) -> None:
        summary_path = getattr(self._session, "summary_path", None)
        if not isinstance(summary_path, Path) or not summary_path.is_file():
            return
        try:
            summary_data = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(summary_data, dict):
            return
        isolated_home = getattr(self._session, "isolated_home", None) or (self._session.run_root / "home")
        if not self._home_isolation_provenance:
            _, self._home_isolation_provenance = _isolated_subprocess_env(self._session)
        summary_data["isolated_home"] = str(isolated_home)
        summary_data["home_isolation"] = self._home_isolation_provenance
        summary_data["turn_usage"] = list(getattr(self._session, "turn_usage_records", ()))
        if self._bg_census_pre is not None and self._bg_census_post is not None:
            summary_data["bg_process_census"] = {
                "pre": self._bg_census_pre,
                "post": self._bg_census_post,
                "diff": self._bg_census_diff,
            }
        if self._case_telemetry:
            summary_data["case_telemetry"] = dict(self._case_telemetry)
        summary_path.write_text(json.dumps(summary_data, ensure_ascii=False, indent=2), encoding="utf-8")

    def run_one_case(self, case_name: str, run_case) -> bool:
        """runs one case and records its terminal status."""
        started = time.monotonic()
        try:
            run_case(self, case_name)
        except Exception as exc:
            elapsed = time.monotonic() - started
            self._results.append({"case": case_name, "status": "fail", "error": str(exc), "elapsed_seconds": elapsed})
            self._log(f"CASE_FAIL case={case_name} error={exc}")
            return bool(self.args.keep_going)
        elapsed = time.monotonic() - started
        self._results.append({"case": case_name, "status": "pass", "elapsed_seconds": elapsed})
        self._log(f"CASE_PASS case={case_name} elapsed={elapsed:.2f}s")
        return True

    def _skip_real_case(self, case_name: str) -> bool:
        if case_name not in REAL_CASES or self.args.real_llm:
            return False
        self._reporter.section(f"CASE {case_name} SKIPPED")
        self._log("这个 case 会调用真实模型。需要传 `--real-llm` 才会执行。")
        self._results.append({"case": case_name, "status": "skip", "reason": "real llm not enabled"})
        return True

    def _log(self, message: str = "") -> None:
        self._reporter.log(message)


class LiveLab:
    """coordinates isolated config, visible execution, transcript, and summaries.

    你可以把它理解成一个测试主持人：先布置一个不会污染仓库的小房间，
    然后按顺序把任务交给 my-agent，并把过程全部记下来。"""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self._session = LabSessionManager(args)
        self._reporter = LabReporter(self._session, args)
        self._runner = LabRunner(self._session, self._reporter, args)
        self.results: list[dict[str, object]] = []

    def setup(self) -> None:
        """creates the isolated live-lab workspace and config."""
        self._session.setup()
        self._reporter.log_header()
        self._reporter.log_model_preflight()

    @property
    def created(self) -> bool:
        return bool(getattr(self._session, "created", False))

    @property
    def run_root(self) -> Path:
        return self._session.run_root

    @property
    def prompts_dir(self) -> Path:
        return self._session.prompts_dir

    @property
    def responses_dir(self) -> Path:
        return self._session.responses_dir

    @property
    def summary_path(self) -> Path:
        return self._session.summary_path

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
    def gateway_wait_timeout(self) -> int:
        return self._session.gateway_wait_timeout

    @property
    def stop_file(self) -> Path:
        return self._session.stop_file

    @property
    def isolated_home(self) -> Path:
        return getattr(self._session, "isolated_home", self._session.run_root / "home")

    @property
    def owner_home_dir(self) -> Path | None:
        return getattr(self._session, "owner_home_dir", None)

    @property
    def owner_compact_dir(self) -> Path | None:
        return getattr(self._session, "owner_compact_dir", None)

    @property
    def owner_tool_policy_json(self) -> Path | None:
        return getattr(self._session, "owner_tool_policy_json", None)

    @property
    def process_store_root(self) -> Path | None:
        return getattr(self._session, "process_store_root", None)

    @property
    def turn_usage_records(self) -> list[dict[str, Any]]:
        return list(getattr(self._session, "turn_usage_records", ()))

    def log(self, message: str = "") -> None:
        """writes one line to both terminal and transcript."""
        self._reporter.log(message)

    def section(self, title: str) -> None:
        """renders a visible case boundary."""
        self._reporter.section(title)

    def agent_command(self, *parts: str) -> list[str]:
        """builds a Python module invocation against the isolated config."""
        return self._runner.agent_command(*parts)

    def run_command(self, command: list[str], *, timeout=None, allow_fail=False, input_text=None) -> CommandResult:
        """executes a subprocess, captures output, and enforces failures."""
        return self._runner.run_command(command, timeout=timeout, allow_fail=allow_fail, input_text=input_text)

    def record_case_telemetry(self, case_name: str, telemetry: Mapping[str, Any]) -> None:
        self._runner.record_case_telemetry(case_name, telemetry)

    def log_output(self, label: str, value: str) -> None:
        """prints a captured output block only when it is non-empty."""
        self._reporter.log_output(label, value)

    def record_prompt(self, case_name: str, prompt: str, *, label: str = "PROMPT SENT TO MY-AGENT") -> Path:
        """persists and prints the exact prompt used by a case."""
        return self._runner.record_prompt(case_name, prompt, label=label)

    def should_stop(self) -> bool:
        """checks the cooperative stop file before starting each case."""
        return self._runner.should_stop()

    def run_suite(self) -> int:
        """executes selected cases, records pass/fail/skip, and writes summary."""
        return self._runner.run_suite(run_case=_run_case_wrapper)

    def run_one_case(self, case_name: str, run_case) -> bool:
        """runs one case and records its terminal status."""
        return self._runner.run_one_case(case_name, run_case)

    def write_summary(self) -> int:
        """writes final machine-readable run summary and returns process code."""
        return self._runner._write_enriched_summary()


def _isolated_subprocess_env(
    session: Any = None,
    *,
    base_env: Mapping[str, str] | None = None,
    refuse_host_env: bool = False,
) -> tuple[dict[str, str], dict[str, Any]]:
    isolated_home: Path | None = None
    run_root: Path | None = None
    if session is not None:
        run_root = Path(session.run_root)
        isolated_home = Path(getattr(session, "isolated_home", None) or (run_root / "home"))
    return prepare_isolated_subprocess_env(
        isolated_home=isolated_home,
        run_root=run_root,
        base_env=base_env,
        refuse_host_env=refuse_host_env,
    )


def _utf8_env(session: Any = None, *, refuse_host_env: bool = False) -> dict[str, str]:
    env, _prov = _isolated_subprocess_env(session, refuse_host_env=refuse_host_env)
    return env


def _run_case_wrapper(runner: LabRunner, case_name: str) -> None:
    """Bridge that exposes the old LiveLab surface to scripts.live_lab.cases."""
    from .cases import run_case as _orig_run_case

    _orig_run_case(_LabInterface(runner), case_name)


class _LabInterface:
    """Thin adapter that exposes LabRunner as the older LiveLab case surface."""

    def __init__(self, runner: LabRunner) -> None:
        self._runner = runner
        self._reporter = runner._reporter
        self._session = runner._session
        self.args = runner.args

    @property
    def created(self) -> bool:
        return bool(getattr(self._session, "created", False))

    @property
    def run_root(self) -> Path:
        return self._session.run_root

    @property
    def prompts_dir(self) -> Path:
        return self._session.prompts_dir

    @property
    def responses_dir(self) -> Path:
        return self._session.responses_dir

    @property
    def summary_path(self) -> Path:
        return self._session.summary_path

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
    def gateway_wait_timeout(self) -> int:
        return self._session.gateway_wait_timeout

    @property
    def stop_file(self) -> Path:
        return self._session.stop_file

    @property
    def isolated_home(self) -> Path:
        return getattr(self._session, "isolated_home", self._session.run_root / "home")

    @property
    def owner_home_dir(self) -> Path | None:
        return getattr(self._session, "owner_home_dir", None)

    @property
    def owner_compact_dir(self) -> Path | None:
        return getattr(self._session, "owner_compact_dir", None)

    @property
    def owner_tool_policy_json(self) -> Path | None:
        return getattr(self._session, "owner_tool_policy_json", None)

    @property
    def process_store_root(self) -> Path | None:
        return getattr(self._session, "process_store_root", None)

    @property
    def turn_usage_records(self) -> list[dict[str, Any]]:
        return list(getattr(self._session, "turn_usage_records", ()))

    def log(self, message: str = "") -> None:
        self._reporter.log(message)

    def section(self, title: str) -> None:
        self._reporter.section(title)

    def agent_command(self, *parts: str) -> list[str]:
        return self._runner.agent_command(*parts)

    def run_command(self, command: list[str], *, timeout=None, allow_fail=False, input_text=None) -> CommandResult:
        return self._runner.run_command(command, timeout=timeout, allow_fail=allow_fail, input_text=input_text)

    def record_case_telemetry(self, case_name: str, telemetry: Mapping[str, Any]) -> None:
        if hasattr(self._runner, "record_case_telemetry"):
            self._runner.record_case_telemetry(case_name, telemetry)

    def log_output(self, label: str, value: str) -> None:
        self._reporter.log_output(label, value)

    def record_prompt(self, case_name: str, prompt: str, *, label: str = "PROMPT SENT TO MY-AGENT") -> Path:
        return self._runner.record_prompt(case_name, prompt, label=label)

    def should_stop(self) -> bool:
        return self._runner.should_stop()
