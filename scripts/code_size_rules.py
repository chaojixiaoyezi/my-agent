# LLM: Code-size governance helper; keep report identities, thresholds, and baseline behavior stable.
# 模块用途: 支撑代码规模守卫，统计文件/函数/类大小并生成可审查的报告。

from __future__ import annotations

"""constants and finding model for the code-size governance checker."""

from dataclasses import dataclass

SOURCE_ROOTS = ["agent_py_agent", "scripts"]

FILE_SOFT_LIMIT = 400
FILE_HARD_LIMIT = 600
TEST_SOFT_LIMIT = 700
TEST_HARD_LIMIT = 900
FUNCTION_SOFT_LIMIT = 60
FUNCTION_HARD_LIMIT = 100
CLASS_SOFT_LIMIT = 250
CLASS_HARD_LIMIT = 350
MIXIN_SOFT_LIMIT = 200
MIXIN_HARD_LIMIT = 250
PARAM_SOFT_LIMIT = 6
PARAM_HARD_LIMIT = 8
NESTING_SOFT_LIMIT = 3
NESTING_HARD_LIMIT = 4
NEAR_SOFT_RATIO = 0.8

JUNK_NAMES = {
    "common.py",
    "final.py",
    "final2.py",
    "helper.py",
    "helpers.py",
    "manager2.py",
    "manager_extra.py",
    "misc.py",
    "new.py",
    "old.py",
    "temp.py",
    "tmp.py",
    "utils.py",
}
JUNK_NAME_BASELINE = {
    "agent_py_agent/agent/log_analysis/analytics/detectors/helpers.py",
    "agent_py_agent/agent/log_analysis/parsers/common.py",
    "agent_py_agent/agent/subagents/utils.py",
    "agent_py_agent/cli/common.py",
}

# High-risk files frozen by architecture guardrails.
HIGH_RISK_FILES: dict[str, int] = {
    "agent_py_agent/cli/chat.py": 1017,
    "agent_py_agent/agent/agent_core/dispatch_mixin.py": 895,
    "agent_py_agent/agent/memory_archive/query.py": 839,
    "agent_py_agent/agent/subagents/manager_patch.py": 794,
    "agent_py_agent/agent/settings/config.py": 751,
    "agent_py_agent/agent/subagents/manager_base.py": 751,
    "agent_py_agent/agent/log_analysis/analytics/detectors/rules.py": 747,
    "agent_py_agent/agent/log_analysis/tools.py": 672,
    "agent_py_agent/agent/memory_archive/runtime.py": 657,
    "agent_py_agent/agent/adapter/qq.py": 613,
    "agent_py_agent/cli/memory_commands.py": 609,
}

EXCLUDE_PARTS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "htmlcov",
    ".coverage",
}
EXCLUDE_PATH_PREFIXES = {
    "agent_py_agent/data/",
}
EXCLUDE_PREFIXES = ("._",)
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}
EXCLUDE_NAMES = {".DS_Store", ".AppleDouble", ".LSOverride"}


# LLM: Finding 是 code-size finding 的数据契约。
# 类用途: 表示一条规模检查结果，统一报告、baseline 和 strict 阻断字段。
@dataclass
class Finding:
    kind: str
    path: str
    name: str
    value: int
    limit: int
    severity: str
    message: str

    # LLM: Finding.identity 生成 baseline key；字段顺序不能随意调整。
    # 函数用途: 用 kind、path 和 name 组合稳定身份，供 strict baseline 比对。
    def identity(self) -> str:
        return f"{self.kind}:{self.path}:{self.name}"
