# LLM: Live Lab validation script; keep CLI flags, artifact paths, and replay outputs stable for scenario tests.
# 模块用途: 支撑可见验收和回放场景，负责启动案例、整理输出或生成报告。

from __future__ import annotations

"""Live Lab session workspace and isolated config writer.

给人看的解释：
这个模块只负责创建隔离测试目录、fixture 小项目和临时配置，避免 Live Lab 污染开发仓库。
"""

import argparse
import time
import uuid
from pathlib import Path


# LLM: LabSessionManager 是Live Lab 验收的数据契约；字段名会被调用方和测试读取。
# 类用途: 定义本模块对外传递的数据字段，字段名需要和调用方保持一致。
class LabSessionManager:
    """owns workspace setup and config writing.

    给人看的解释：
    测试前先建目录、写 fixture 小项目、写一份临时配置。
    这份配置会把 memory、gateway、subagent、LocalStore 都关进测试目录。"""

    # LLM: __init__ 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.source_config = Path(args.config).expanduser().resolve()
        self.source_capability_config = Path(args.capability_config).expanduser().resolve()
        self.runs_dir = Path(args.runs_dir).expanduser().resolve()
        stamp = args.run_id or time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.run_root = self.runs_dir / stamp
        self.fixture_root = self.run_root / "fixture_project"
        self.prompts_dir = self.run_root / "prompts"
        self.responses_dir = self.run_root / "responses"
        self.config_path = self.run_root / "live_agent_config.yaml"
        self.capability_config_path = self.run_root / "live_capability_config.yaml"
        self.transcript_path = self.run_root / "TRANSCRIPT.md"
        self.summary_path = self.run_root / "live_lab_summary.json"
        self.stop_file = self.run_root / "STOP"
        self.created = False

    # LLM: setup 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def setup(self) -> None:
        """creates the isolated live-lab workspace and config."""
        if not self.source_config.exists():
            raise FileNotFoundError(f"配置文件不存在: {self.source_config}")
        self.fixture_root.mkdir(parents=True, exist_ok=True)
        self.prompts_dir.mkdir(parents=True, exist_ok=True)
        self.responses_dir.mkdir(parents=True, exist_ok=True)
        self.write_fixture()
        self.write_config()
        self.write_capability_config()
        self.prepare_runtime_dirs()
        self.created = True

    # LLM: write_fixture 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 把报告、摘要或状态写入磁盘，保持输出路径和 JSON 字段稳定。
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

    # LLM: write_config 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 把报告、摘要或状态写入磁盘，保持输出路径和 JSON 字段稳定。
    def write_config(self) -> None:
        """appends isolation overrides while preserving model/API settings."""
        base = self.source_config.read_text(encoding="utf-8")
        fixture = str(self.fixture_root).replace("\\", "/")
        # LLM: Live Lab must isolate the owner home too, or global daily memory can rewrite the next case.
        isolated_home = str((self.fixture_root / ".my_agent" / "home").resolve()).replace("\\", "/")
        backend_override = "" if self.args.real_llm else '\nmodel_backend: "echo"\n'
        max_subagents = _live_lab_max_subagents(self.args)
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
max_subagents: {max_subagents}
gateway_request_timeout: {int(self.args.timeout)}
gateway_request_poll_interval: 1
gateway_request_workers: {max(2, int(getattr(self.args, "max_runners", 1) or 1) + 2)}
gateway_foreground_reserved_workers: 1
gateway_background_model_request_timeout: {min(3600, max(600, int(self.args.timeout) * 5))}
gateway_processing_timeout_seconds: {max(int(self.args.timeout) + 120, 180)}
gateway_request_max_attempts: 2
gateway_port: 0
daemon_planner: false
daemon_apply: true
daemon_execute_runners: true
daemon_max_runners: {max(1, int(getattr(self.args, "max_runners", 1) or 1))}
daemon_interval: 1
runner_failure_policy: "auto"
runner_timeout_seconds: {max(60, int(self.args.timeout))}
# live lab keeps runner tool rounds unlimited unless a specific stress case overrides it
max_tool_rounds: 0
request_timeout: {max(30, int(self.args.timeout))}
{backend_override}"""
        self.config_path.write_text(base + overrides, encoding="utf-8")

    # LLM: write_capability_config keeps real Live Lab recovery thresholds isolated from developer defaults.
    # 函数用途: 复制本轮能力配置并写入真实测试专用的子代理失联/运行超时阈值。
    def write_capability_config(self) -> None:
        """writes per-run capability config so stale real runners can be recovered."""
        if self.source_capability_config.exists():
            base = self.source_capability_config.read_text(encoding="utf-8")
        else:
            base = "enable_capability_routing: true\ncapability_grant_expires_after_task: true\n"
        timeout = int(self.args.timeout)
        heartbeat_timeout = max(60, min(timeout // 3, 180))
        run_timeout = max(timeout * 3, heartbeat_timeout * 3)
        overrides = f"""

# live-agent-lab recovery overrides
subagent_heartbeat_timeout: {heartbeat_timeout}
subagent_run_timeout: {run_timeout}
subagent_min_evidence_for_done: 1
subagent_no_progress_attempt_limit: 3
"""
        self.capability_config_path.write_text(base + overrides, encoding="utf-8")

    # LLM: prepare_runtime_dirs 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def prepare_runtime_dirs(self) -> None:
        """pre-creates runtime files that doctor expects in a fresh workspace."""
        event_path = self.fixture_root / ".my_agent" / "local_store" / "events.jsonl"
        event_path.parent.mkdir(parents=True, exist_ok=True)
        event_path.touch(exist_ok=True)


# LLM: Live Lab fan-out must cover multi-request and bundle scenarios, not only the legacy count flag.
# 函数用途: 生成真实测试专用的子代理上限，避免复杂场景被隔离配置误截断。
def _live_lab_max_subagents(args: argparse.Namespace) -> int:
    count = max(1, int(getattr(args, "count", 1) or 1))
    runners = max(1, int(getattr(args, "max_runners", 1) or 1))
    cycles = max(1, int(getattr(args, "max_cycles", 2) or 2))
    return max(count, runners * cycles * 4, 8)
