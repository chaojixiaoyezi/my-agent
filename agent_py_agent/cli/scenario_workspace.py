
from __future__ import annotations

"""writes scenario fixture projects and isolated scenario config overrides.

给人看的解释：
scenario_utils 负责流程编排；这里单独承接磁盘 fixture/config 写入，避免场景入口文件继续变胖。
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ScenarioConfigRequest:
    source_config: Path
    target_config: Path
    fixture_root: Path
    request_timeout: float
    max_subagents: int
    runner_concurrency: object | None = None
    runner_start_rate: object | None = None
    model_request_timeout: object | None = None


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
                "- 父代理必须完成 runner 调度和收口闭环。",
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


def write_scenario_config(request: ScenarioConfigRequest) -> None:
    base = request.source_config.read_text(encoding="utf-8")
    fixture = str(request.fixture_root).replace("\\", "/")
    overrides = f"""

# scenario-test isolation overrides
workspace_root: "{fixture}"
prompt_files:
memory_path: ".my_agent/memory.jsonl"
subagent_workspace: ".my_agent/subagents"
gateway_workspace: ".my_agent/gateway"
max_subagents: {request.max_subagents}
gateway_request_timeout: {int(request.request_timeout)}
gateway_request_poll_interval: 1
daemon_planner: false
daemon_mutate_state: false
daemon_start_runners: false
daemon_max_runners: 0
daemon_interval: 1
runner_failure_policy: "auto"
max_tool_rounds: 0
"""
    if request.runner_concurrency is not None:
        overrides += f'runner_concurrency: "{request.runner_concurrency}"\n'
    if request.runner_start_rate is not None:
        overrides += f'runner_start_rate: "{request.runner_start_rate}"\n'
    if request.model_request_timeout is not None:
        overrides += f"request_timeout: {int(float(request.model_request_timeout))}\n"
    request.target_config.write_text(base + overrides, encoding="utf-8")
