
from __future__ import annotations

"""constants and finding model for the code-size governance checker."""

from dataclasses import dataclass

SOURCE_ROOTS = ["agent_py_agent", "scripts"]

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
    "frontend/",
}
EXCLUDE_PREFIXES = ("._",)
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}
EXCLUDE_NAMES = {".DS_Store", ".AppleDouble", ".LSOverride"}


def is_test_path(path: str) -> bool:
    normalized = str(path or "").replace("\\", "/").strip("/")
    return normalized == "tests" or normalized.startswith("tests/") or "/tests/" in f"/{normalized}/"


@dataclass
class Finding:
    kind: str
    path: str
    name: str
    value: int
    limit: int
    severity: str
    message: str

    def identity(self) -> str:
        return f"{self.kind}:{self.path}:{self.name}"
