
from __future__ import annotations

"""Live Lab session workspace and isolated config writer.

这个模块只负责创建隔离测试目录、fixture 小项目和临时配置，避免 Live Lab 污染开发仓库。
"""

import argparse
import time
import uuid
from pathlib import Path


def live_lab_model_request_timeout(args: argparse.Namespace) -> int:
    """Return the per-model-call timeout used by the isolated Live Lab config."""

    return max(30, int(getattr(args, "timeout", 300) or 300))


def live_lab_gateway_wait_timeout(args: argparse.Namespace) -> int:
    """Return the total wait budget for a gateway request.

    A single user request can include several model calls: the root turn,
    tool-result follow-up turns, child runner turns, and a final response. Keep
    this budget separate from the per-call model timeout so the harness does not
    kill valid multi-step work before the model request budget has even elapsed.
    """

    per_call = live_lab_model_request_timeout(args)
    max_cycles = max(1, int(getattr(args, "max_cycles", 1) or 1))
    estimated_model_calls = max(4, max_cycles + 2)
    return per_call * estimated_model_calls + 120


def live_lab_gateway_processing_timeout(args: argparse.Namespace) -> int:
    """Return the stale-processing timeout for the isolated gateway."""

    return max(live_lab_gateway_wait_timeout(args) + 120, 180)


class LabSessionManager:
    """owns workspace setup and config writing.

    测试前先建目录、写 fixture 小项目、写一份临时配置。
    这份配置会把 memory、gateway、subagent、LocalStore 都关进测试目录。"""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.source_config = Path(args.config).expanduser().resolve()
        self.runs_dir = Path(args.runs_dir).expanduser().resolve()
        stamp = args.run_id or time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.run_root = self.runs_dir / stamp
        self.fixture_root = self.run_root / "fixture_project"
        # 宿主数据根不能嵌在业务工作区内；两个目录同属本轮，仍保持完全隔离。
        self.isolated_home = self.run_root / "home"
        self.prompts_dir = self.run_root / "prompts"
        self.responses_dir = self.run_root / "responses"
        self.config_path = self.run_root / "live_agent_config.yaml"
        self.transcript_path = self.run_root / "TRANSCRIPT.md"
        self.summary_path = self.run_root / "live_lab_summary.json"
        self.stop_file = self.run_root / "STOP"
        self.created = False

    def setup(self) -> None:
        """creates the isolated live-lab workspace and config."""
        if not self.source_config.exists():
            raise FileNotFoundError(f"配置文件不存在: {self.source_config}")
        self.fixture_root.mkdir(parents=True, exist_ok=True)
        self.prompts_dir.mkdir(parents=True, exist_ok=True)
        self.responses_dir.mkdir(parents=True, exist_ok=True)
        self.write_fixture()
        self.write_config()
        self.prepare_runtime_dirs()
        self.created = True

    @property
    def model_request_timeout(self) -> int:
        """Per-model-call timeout configured for this isolated run."""

        return live_lab_model_request_timeout(self.args)

    @property
    def gateway_wait_timeout(self) -> int:
        """Total wait budget for one gateway ask in this isolated run."""

        return live_lab_gateway_wait_timeout(self.args)

    def write_fixture(self) -> None:
        """writes a tiny project that real agents can safely read and modify."""
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
        """appends isolation overrides while preserving model/API settings."""
        base = self.source_config.read_text(encoding="utf-8")
        fixture = str(self.fixture_root).replace("\\", "/")
        isolated_home = str(self.isolated_home.resolve()).replace("\\", "/")
        backend_override = "" if self.args.real_llm else '\nmodel_backend: "echo"\n'
        request_timeout = live_lab_model_request_timeout(self.args)
        gateway_timeout = live_lab_gateway_wait_timeout(self.args)
        processing_timeout = live_lab_gateway_processing_timeout(self.args)
        overrides = f"""

# live-agent-lab isolation overrides
workspace_root: "{fixture}"
my_agent_home: "{isolated_home}"
prompt_files:
memory_path: ".my_agent/memory.jsonl"
local_store_path: ".my_agent/local_store/local.db"
local_store_files_dir: ".my_agent/local_store/files"
local_store_events_path: ".my_agent/local_store/events.jsonl"
subagent_workspace: ".my_agent/subagents"
gateway_workspace: ".my_agent/gateway"
adapter_workspace: ".my_agent/adapters/file"
max_subagents: {max(self.args.count, 1)}
gateway_request_timeout: {gateway_timeout}
gateway_processing_timeout_seconds: {processing_timeout}
gateway_request_max_attempts: 2
daemon_apply: false
daemon_execute_runners: false
# live lab keeps runner tool rounds unlimited unless a specific stress case overrides it
max_tool_rounds: 0
request_timeout: {request_timeout}
{backend_override}"""
        self.config_path.write_text(base + overrides, encoding="utf-8")

    def prepare_runtime_dirs(self) -> None:
        """沿正式 CLI 解析结果准备本轮运行文件和空记忆的派生索引。"""
        from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
        from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
        from agent_py_agent.cli.common import make_lightweight_cli_agent_context

        context = make_lightweight_cli_agent_context(argparse.Namespace(config=str(self.config_path)))
        ensure_my_agent_home(context.home_paths.root)
        # 项目路径与 owner 固定服务路径的相对基准不同，不能手拼 fixture 子目录。
        event_path = context.runtime_paths["local_store_events_path"]
        event_path.parent.mkdir(parents=True, exist_ok=True)
        event_path.touch(exist_ok=True)
        memory = JsonlMemory(context.runtime_paths["memory_path"])
        memory.migrate_tiers()
        memory.rebuild_read_index()
