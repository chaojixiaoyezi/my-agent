
from __future__ import annotations

"""Live Lab transcript, preflight, and summary reporter.

这里统一处理终端输出和 TRANSCRIPT.md 记录，避免 runner 里散落重复日志逻辑。
"""

import argparse
import json
import shlex
import sys

from .constants import REPO_ROOT
from .session import LabSessionManager


class LabReporter:
    """owns output logging, model preflight, and summary writing."""

    def __init__(self, session: LabSessionManager, args: argparse.Namespace) -> None:
        self._session = session
        self.args = args

    def log_header(self) -> None:
        """prints the run metadata that a human needs at the top of the terminal."""
        self.log("# MY-AGENT LIVE LAB")
        self.log("")
        self.log(f"- run_root: {self._session.run_root}")
        self.log(f"- fixture_root: {self._session.fixture_root}")
        self.log(f"- config: {self._session.config_path}")
        self.log(f"- transcript: {self._session.transcript_path}")
        self.log(f"- stop_file: {self._session.stop_file}")
        self.log(f"- suite: {self.args.suite}")
        self.log(f"- real_llm: {self.args.real_llm}")
        self.log("")
        self.log("如果要中途停止，在另一个终端执行：")
        self.log(f"`touch {shlex.quote(str(self._session.stop_file))}`")
        self.log("")

    def log_model_preflight(self) -> None:
        """logs backend/key presence without exposing secrets."""
        sys.path.insert(0, str(REPO_ROOT))
        from agent_py_agent.agent.config import load_config

        config = load_config(self._session.config_path)
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

    def log_output(self, label: str, value: str) -> None:
        """prints a captured output block only when it is non-empty."""
        if not value:
            return
        self.log("")
        self.log(f"{label}:")
        self.log("```text")
        self.log(value.rstrip())
        self.log("```")

    def write_summary(self, results: list[dict[str, object]]) -> int:
        """writes final machine-readable run summary and returns process code."""
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
        self._session.summary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.section("SUMMARY")
        self.log(json.dumps(payload, ensure_ascii=False, indent=2))
        self.log("")
        self.log("LIVE_LAB_PASS" if payload["ok"] else "LIVE_LAB_FAIL")
        return 0 if payload["ok"] else 2

    def section(self, title: str) -> None:
        """renders a visible case boundary."""
        self.log("")
        self.log(f"## {title}")
        self.log("")

    def log(self, message: str = "") -> None:
        """Write to transcript and terminal."""
        if self._session.created:
            with self._session.transcript_path.open("a", encoding="utf-8") as handle:
                handle.write(message + "\n")
        print(message, flush=True)
