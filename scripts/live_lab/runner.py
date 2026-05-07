# LLM: Live Lab validation script; keep CLI flags, artifact paths, and replay outputs stable for scenario tests.
# 模块用途: 支撑可见验收和回放场景，负责启动案例、整理输出或生成报告。

from __future__ import annotations

"""Live Lab runner for command execution and suite orchestration.

给人看的解释：
这个文件是测试台入口门面。隔离目录和日志分别在 session/reporter 模块里，
这里保留命令执行、case 调度和对旧 LiveLab 接口的兼容代理。
"""

import argparse
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .constants import REAL_CASES, REPO_ROOT, SUITES
from .reporter import LabReporter
from .session import LabSessionManager


# LLM: CommandResult 是Live Lab 验收的数据契约；字段名会被调用方和测试读取。
# 类用途: 定义本模块对外传递的数据字段，字段名需要和调用方保持一致。
@dataclass
class CommandResult:
    """immutable result record for one child process invocation.

    给人看的解释：
    每跑一条命令，我们都把命令、退出码、输出和耗时收起来。
    后面写 summary 时，就不用靠肉眼回翻终端。"""

    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    elapsed_seconds: float


# LLM: LiveLabError 是Live Lab 验收的数据契约；字段名会被调用方和测试读取。
# 类用途: 标记本流程的专用异常，方便入口层给出明确失败信息。
class LiveLabError(RuntimeError):
    """raised when a live lab command fails and the suite should stop.

    给人看的解释：
    这是测试台自己的错误类型。比如 gateway 启动失败、场景测试没过，就会抛出它。"""


# LLM: LabRunner 是Live Lab 验收的数据契约；字段名会被调用方和测试读取。
# 类用途: 封装验收运行环境、命令执行和结果记录。
class LabRunner:
    """owns subprocess execution, prompt recording, and suite dispatch."""

    # LLM: __init__ 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def __init__(self, session: LabSessionManager, reporter: LabReporter, args: argparse.Namespace) -> None:
        self._session = session
        self._reporter = reporter
        self.args = args
        self._results: list[dict[str, object]] = []

    # LLM: results 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    @property
    def results(self) -> list[dict[str, object]]:
        return self._results

    # LLM: agent_command 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def agent_command(self, *parts: str) -> list[str]:
        """builds a Python module invocation against the isolated config."""
        return [sys.executable, "-m", "agent_py_agent", "--config", str(self._session.config_path), *parts]

    # LLM: run_command 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 执行对应流程阶段，并把成功、失败和产物写入汇总状态。
    def run_command(
        self,
        command: list[str],
        *,
        timeout: float | None = None,
        allow_fail: bool = False,
        input_text: str | None = None,
    ) -> CommandResult:
        """executes a subprocess, captures output, and enforces failures."""
        timeout = timeout if timeout is not None else self.args.timeout
        self._log("```bash")
        self._log("$ " + shlex.join(command))
        self._log("```")
        started = time.monotonic()
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            input=input_text,
            env=_utf8_env(),
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

    # LLM: record_prompt 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 记录运行结果、失败原因或会话痕迹，供报告和诊断读取。
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

    # LLM: should_stop 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 判断输入或环境是否满足规则，结果会影响分支、告警或阻断。
    def should_stop(self) -> bool:
        """checks the cooperative stop file before starting each case."""
        if self._session.stop_file.exists():
            self._log(f"STOP 文件已存在，停止后续 case: {self._session.stop_file}")
            return True
        return False

    # LLM: run_suite 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 执行对应流程阶段，并把成功、失败和产物写入汇总状态。
    def run_suite(self, run_case) -> int:
        """executes selected cases, records pass/fail/skip, and writes summary."""
        if self.args.suite in {"real", "all"} and not self.args.real_llm:
            self._log("suite 包含真实 LLM case，但没有传 `--real-llm`。")
            self._log("请显式运行：`scripts/open_live_lab.sh --suite real --real-llm`")
            return 2
        for case_name in SUITES[self.args.suite]:
            if self.should_stop():
                self._results.append({"case": case_name, "status": "skip", "reason": "stop file exists"})
                break
            if self._skip_real_case(case_name):
                continue
            if not self.run_one_case(case_name, run_case):
                break
        return self._reporter.write_summary(self._results)

    # LLM: run_one_case 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 执行对应流程阶段，并把成功、失败和产物写入汇总状态。
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

    # LLM: _skip_real_case 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def _skip_real_case(self, case_name: str) -> bool:
        if case_name not in REAL_CASES or self.args.real_llm:
            return False
        self._reporter.section(f"CASE {case_name} SKIPPED")
        self._log("这个 case 会调用真实模型。需要传 `--real-llm` 才会执行。")
        self._results.append({"case": case_name, "status": "skip", "reason": "real llm not enabled"})
        return True

    # LLM: _log 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def _log(self, message: str = "") -> None:
        self._reporter.log(message)


# LLM: LiveLab 是Live Lab 验收的数据契约；字段名会被调用方和测试读取。
# 类用途: 封装验收运行环境、命令执行和结果记录。
class LiveLab:
    """coordinates isolated config, visible execution, transcript, and summaries.

    给人看的解释：
    你可以把它理解成一个测试主持人：先布置一个不会污染仓库的小房间，
    然后按顺序把任务交给 my-agent，并把过程全部记下来。"""

    # LLM: __init__ 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self._session = LabSessionManager(args)
        self._reporter = LabReporter(self._session, args)
        self._runner = LabRunner(self._session, self._reporter, args)
        self.results: list[dict[str, object]] = []

    # LLM: setup 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def setup(self) -> None:
        """creates the isolated live-lab workspace and config."""
        self._session.setup()
        self._reporter.log_header()
        self._reporter.log_model_preflight()

    # LLM: transcript_path 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    @property
    def transcript_path(self) -> Path:
        return self._session.transcript_path

    # LLM: fixture_root 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    @property
    def fixture_root(self) -> Path:
        return self._session.fixture_root

    # LLM: config_path 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    @property
    def config_path(self) -> Path:
        return self._session.config_path

    # LLM: stop_file 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    @property
    def stop_file(self) -> Path:
        return self._session.stop_file

    # LLM: log 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def log(self, message: str = "") -> None:
        """writes one line to both terminal and transcript."""
        self._reporter.log(message)

    # LLM: section 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def section(self, title: str) -> None:
        """renders a visible case boundary."""
        self._reporter.section(title)

    # LLM: agent_command 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def agent_command(self, *parts: str) -> list[str]:
        """builds a Python module invocation against the isolated config."""
        return self._runner.agent_command(*parts)

    # LLM: run_command 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 执行对应流程阶段，并把成功、失败和产物写入汇总状态。
    def run_command(self, command: list[str], *, timeout=None, allow_fail=False, input_text=None) -> CommandResult:
        """executes a subprocess, captures output, and enforces failures."""
        return self._runner.run_command(command, timeout=timeout, allow_fail=allow_fail, input_text=input_text)

    # LLM: log_output 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def log_output(self, label: str, value: str) -> None:
        """prints a captured output block only when it is non-empty."""
        self._reporter.log_output(label, value)

    # LLM: record_prompt 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 记录运行结果、失败原因或会话痕迹，供报告和诊断读取。
    def record_prompt(self, case_name: str, prompt: str, *, label: str = "PROMPT SENT TO MY-AGENT") -> Path:
        """persists and prints the exact prompt used by a case."""
        return self._runner.record_prompt(case_name, prompt, label=label)

    # LLM: should_stop 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 判断输入或环境是否满足规则，结果会影响分支、告警或阻断。
    def should_stop(self) -> bool:
        """checks the cooperative stop file before starting each case."""
        return self._runner.should_stop()

    # LLM: run_suite 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 执行对应流程阶段，并把成功、失败和产物写入汇总状态。
    def run_suite(self) -> int:
        """executes selected cases, records pass/fail/skip, and writes summary."""
        return self._runner.run_suite(run_case=_run_case_wrapper)

    # LLM: run_one_case 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 执行对应流程阶段，并把成功、失败和产物写入汇总状态。
    def run_one_case(self, case_name: str, run_case) -> bool:
        """runs one case and records its terminal status."""
        return self._runner.run_one_case(case_name, run_case)

    # LLM: write_summary 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 把报告、摘要或状态写入磁盘，保持输出路径和 JSON 字段稳定。
    def write_summary(self) -> int:
        """writes final machine-readable run summary and returns process code."""
        return self._reporter.write_summary(self._runner.results)


# LLM: _utf8_env 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _utf8_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


# LLM: _run_case_wrapper 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
# 函数用途: 执行对应流程阶段，并把成功、失败和产物写入汇总状态。
def _run_case_wrapper(runner: LabRunner, case_name: str) -> None:
    """Bridge that exposes the old LiveLab surface to scripts.live_lab.cases."""
    from .cases import run_case as _orig_run_case

    _orig_run_case(_LabInterface(runner), case_name)


# LLM: _LabInterface 是Live Lab 验收的数据契约；字段名会被调用方和测试读取。
# 类用途: 定义本模块对外传递的数据字段，字段名需要和调用方保持一致。
class _LabInterface:
    """Thin adapter that exposes LabRunner as the older LiveLab case surface."""

    # LLM: __init__ 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def __init__(self, runner: LabRunner) -> None:
        self._runner = runner
        self._reporter = runner._reporter
        self._session = runner._session
        self.args = runner.args

    # LLM: transcript_path 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    @property
    def transcript_path(self) -> Path:
        return self._session.transcript_path

    # LLM: fixture_root 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    @property
    def fixture_root(self) -> Path:
        return self._session.fixture_root

    # LLM: config_path 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    @property
    def config_path(self) -> Path:
        return self._session.config_path

    # LLM: stop_file 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    @property
    def stop_file(self) -> Path:
        return self._session.stop_file

    # LLM: log 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def log(self, message: str = "") -> None:
        self._reporter.log(message)

    # LLM: section 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def section(self, title: str) -> None:
        self._reporter.section(title)

    # LLM: agent_command 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def agent_command(self, *parts: str) -> list[str]:
        return self._runner.agent_command(*parts)

    # LLM: run_command 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 执行对应流程阶段，并把成功、失败和产物写入汇总状态。
    def run_command(self, command: list[str], *, timeout=None, allow_fail=False, input_text=None) -> CommandResult:
        return self._runner.run_command(command, timeout=timeout, allow_fail=allow_fail, input_text=input_text)

    # LLM: log_output 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def log_output(self, label: str, value: str) -> None:
        self._reporter.log_output(label, value)

    # LLM: record_prompt 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 记录运行结果、失败原因或会话痕迹，供报告和诊断读取。
    def record_prompt(self, case_name: str, prompt: str, *, label: str = "PROMPT SENT TO MY-AGENT") -> Path:
        return self._runner.record_prompt(case_name, prompt, label=label)

    # LLM: should_stop 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 判断输入或环境是否满足规则，结果会影响分支、告警或阻断。
    def should_stop(self) -> bool:
        return self._runner.should_stop()
