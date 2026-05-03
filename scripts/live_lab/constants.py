from __future__ import annotations

"""LLM: shared constants for the Live Lab CLI, runner, and cases.

给人看的解释：
这些是测试台所有模块都会用到的固定路径和 suite 定义。
集中放这里，避免每个文件自己猜项目根目录或 case 名字。
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "agent_py_agent" / "config" / "agent_config.yaml"
DEFAULT_RUNS_DIR = REPO_ROOT / "validation" / "live_lab"

SUITES = {
    "health": ["health"],
    "bad-weather": ["bad_weather"],
    "log-analysis": ["log_analysis_replay"],
    "smoke": ["health", "bad_weather"],
    "real": ["health", "gateway_ask", "long_subagent"],
    "all": ["health", "bad_weather", "log_analysis_replay", "gateway_ask", "long_subagent"],
}

REAL_CASES = {"gateway_ask", "long_subagent"}
