from __future__ import annotations

"""LLM: freezes architecture debt baselines and blocks new drift.

给人看的解释：
这些测试不是业务功能测试，而是工程治理护栏。它们允许历史债务存在，
但要求后续改动不能继续增加运行产物、星号导入、大入口文件和垃圾命名。
"""

import ast
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

STAR_IMPORT_BASELINE: dict[str, int] = {}

ENTRYPOINT_LINE_LIMITS = {
    "agent_py_agent/cli/parser.py": 120,
    "agent_py_agent/cli/chat.py": 1017,
    "agent_py_agent/agent/agent_core/dispatch_mixin.py": 895,
    "agent_py_agent/agent/subagents/manager_base.py": 751,
    "agent_py_agent/agent/subagents/manager_patch.py": 794,
    "agent_py_agent/agent/settings/config.py": 751,
    "agent_py_agent/agent/memory_archive/query.py": 839,
    "agent_py_agent/agent/log_analysis/analytics/detectors/rules.py": 747,
}

JUNK_NAME_BASELINE = {
    "agent_py_agent/agent/log_analysis/analytics/detectors/helpers.py",
    "agent_py_agent/agent/log_analysis/parsers/common.py",
    "agent_py_agent/agent/subagents/utils.py",
    "agent_py_agent/cli/common.py",
}

RUNTIME_ARTIFACT_NAMES = {
    ".DS_Store",
    ".coverage",
    ".pytest_cache",
    "__pycache__",
    "MagicMock",
    "mutation_test_report.json",
}
RUNTIME_ARTIFACT_SUFFIXES = {".pyc", ".pyo"}
JUNK_FILE_NAMES = {
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


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _python_source_files() -> list[Path]:
    files: list[Path] = []
    for package in ("agent_py_agent", "scripts"):
        root = REPO_ROOT / package
        if root.exists():
            files.extend(path for path in root.rglob("*.py") if ".git" not in path.parts)
    return files


def test_no_new_star_imports() -> None:
    """Existing star imports are debt; new ones must not appear."""

    counts: dict[str, int] = {}
    for path in _python_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
                relative_path = path.relative_to(REPO_ROOT).as_posix()
                counts[relative_path] = counts.get(relative_path, 0) + 1

    assert counts == STAR_IMPORT_BASELINE


def test_runtime_artifacts_are_not_present_in_tracked_files() -> None:
    """Generated local state must stay out of the repository tree."""

    offenders: list[str] = []
    for relative_path in _tracked_files():
        path = REPO_ROOT / relative_path
        if not path.exists():
            continue
        if any(part in RUNTIME_ARTIFACT_NAMES for part in path.parts):
            offenders.append(relative_path)
            continue
        if path.suffix in RUNTIME_ARTIFACT_SUFFIXES:
            offenders.append(relative_path)

    assert offenders == []


def test_large_entrypoints_do_not_grow_past_baseline() -> None:
    """Known-large files need gradual extraction, not further growth."""

    offenders = []
    for relative_path, max_lines in ENTRYPOINT_LINE_LIMITS.items():
        path = REPO_ROOT / relative_path
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        if line_count > max_lines:
            offenders.append(f"{relative_path}: {line_count} > {max_lines}")

    assert offenders == []


def test_no_new_junk_filenames() -> None:
    """New modules need specific names that communicate ownership."""

    offenders = []
    for relative_path in _tracked_files():
        path = Path(relative_path)
        if path.name not in JUNK_FILE_NAMES:
            continue
        if relative_path in JUNK_NAME_BASELINE:
            continue
        offenders.append(relative_path)

    assert offenders == []


def test_compileall_succeeds() -> None:
    """All Python source must compile without syntax errors."""

    import compileall

    result = compileall.compile_dir(
        str(REPO_ROOT / "agent_py_agent"),
        quiet=2,
        force=True,
    )
    assert result is not None


def test_no_new_forbidden_globals() -> None:
    """No new files may define forbidden global patterns like AgentManager, TaskManager."""

    FORBIDDEN_CLASS_NAMES = {
        "AgentManager",
        "TaskManager",
        "ServiceManager",
        "RuntimeEverything",
        "CommonHelper",
    }
    BASELINE = set()

    offenders = []
    for path in _python_source_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in FORBIDDEN_CLASS_NAMES:
                rel = path.relative_to(REPO_ROOT).as_posix()
                key = f"{rel}:{node.name}"
                if key not in BASELINE:
                    offenders.append(key)

    assert offenders == []
