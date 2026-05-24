# LLM: Live Lab validation script; keep CLI flags, artifact paths, and replay outputs stable for scenario tests.
# 模块用途: 支撑可见验收和回放场景，负责启动案例、整理输出或生成报告。

from __future__ import annotations

"""shared constants for the Live Lab CLI, runner, and cases.

给人看的解释：
这些是测试台所有模块都会用到的固定路径和 suite 定义。
集中放这里，避免每个文件自己猜项目根目录或 case 名字。
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "agent_py_agent" / "config" / "agent_config.yaml"
DEFAULT_RUNS_DIR = REPO_ROOT / "validation" / "live_lab"

# Suite map is part of the public Live Lab CLI contract; keep docs/modules/live-lab in sync when it changes.
# LLM: web, file, and document repair suites validate seeded failure recovery through the same refs-first repair contract.
# 2026-05-18: main-complex is the root-agent-only complex task suite; it disables subagents in its case module.
# 2026-05-18: main-artifact is a fast real-LLM slice for long output/artifact readback without rerunning every complex case.
SUITES = {
    "health": ["health"],
    "bad-weather": ["bad_weather"],
    "log-analysis": ["log_analysis_replay"],
    "smoke": ["health", "bad_weather"],
    "main-artifact": ["health", "main_artifact_readback", "main_compact_resume_roundtrip"],
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
        "log_analysis_replay",
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
    "main_large_log_audit",
}
