
from __future__ import annotations

"""shared constants for the Live Lab CLI, runner, and cases.

这些是测试台所有模块都会用到的固定路径和 suite 定义。
集中放这里，避免每个文件自己猜项目根目录或 case 名字。
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "agent_py_agent" / "config" / "agent_config.yaml"
DEFAULT_RUNS_DIR = REPO_ROOT / "validation" / "live_lab"

# Suite map is part of the public Live Lab CLI contract; keep docs/modules/live-lab in sync when it changes.
# 2026-05-18: main-complex is the root-agent-only complex task suite; it disables subagents in its case module.
# 2026-05-18: main-artifact is a fast real-LLM slice for long output/artifact readback without rerunning every complex case.
SUITES = {
    "health": ["health"],
    "bad-weather": ["bad_weather"],
    "smoke": ["health", "bad_weather"],
    "main-artifact": ["health", "main_artifact_readback", "main_compact_resume_roundtrip"],
    "compact-stress": ["health", "main_compact_stress_long_read"],
    "main-complex": [
        "health",
        "main_tool_failure_recovery",
        "main_artifact_readback",
        "main_compact_resume_roundtrip",
        "main_large_log_audit",
    ],
    "real": ["health", "gateway_ask", "long_subagent"],
    "all": [
        "health",
        "bad_weather",
        "gateway_ask",
        "long_subagent",
    ],
}

REAL_CASES = {
    "gateway_ask",
    "long_subagent",
    "main_tool_failure_recovery",
    "main_artifact_readback",
    "main_compact_resume_roundtrip",
    "main_compact_stress_long_read",
    "main_large_log_audit",
}
