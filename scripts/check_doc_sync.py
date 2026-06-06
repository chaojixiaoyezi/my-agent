
from __future__ import annotations

"""Check that module code changes move docs and teaching comments together.

This script is intentionally small and local: it looks at the current git diff
and fails when a known module's implementation changes without its module docs
or same-file comments being updated.
"""

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModuleDocRule:
    """Describes one module's code paths and the docs that must move with them.

    New programmer note:
    A rule is a small checklist. If changed files live under code_prefixes, then
    required_docs must also appear in the same git diff."""

    name: str
    code_prefixes: tuple[str, ...]
    required_docs: tuple[str, ...]


MODULE_RULES = (
    ModuleDocRule(
        name="log-analysis",
        code_prefixes=(
            "agent_py_agent/agent/log_analysis/",
            "agent_py_agent/cli/logs.py",
            "scripts/live_lab/log_analysis_replay.py",
        ),
        required_docs=(
            "docs/modules/log-analysis/02-progress.md",
            "docs/modules/log-analysis/04-structure.md",
        ),
    ),
    ModuleDocRule(
        name="subagent",
        code_prefixes=(
            "agent_py_agent/agent/subagent.py",
            "agent_py_agent/agent/subagents/",
            "agent_py_agent/agent/subagent_workflows/",
            "agent_py_agent/cli/subagents.py",
        ),
        required_docs=(
            "docs/modules/subagent/02-progress.md",
            "docs/modules/subagent/04-structure.md",
        ),
    ),
    ModuleDocRule(
        name="memory",
        code_prefixes=(
            "agent_py_agent/agent/memory.py",
            "agent_py_agent/agent/memory_settings.py",
            "agent_py_agent/agent/settings/memory.py",
            "agent_py_agent/agent/memory_store/",
            "agent_py_agent/agent/memory_archive/",
            "agent_py_agent/agent/memory_routing/",
            "agent_py_agent/cli/memory_commands/",
            "agent_py_agent/cli/memory_archive_commands.py",
            "agent_py_agent/cli/memory_compact_commands.py",
        ),
        required_docs=(
            "docs/modules/memory/02-progress.md",
            "docs/modules/memory/04-structure.md",
        ),
    ),
    ModuleDocRule(
        name="gateway",
        code_prefixes=(
            "agent_py_agent/agent/gateway.py",
            "agent_py_agent/agent/gateway_parts/",
            "agent_py_agent/cli/gateway_process.py",
            "agent_py_agent/cli/gateway_client.py",
            "agent_py_agent/cli/adapter.py",
        ),
        required_docs=(
            "docs/modules/gateway/02-progress.md",
            "docs/modules/gateway/04-structure.md",
        ),
    ),
    ModuleDocRule(
        name="live-lab",
        code_prefixes=(
            "scripts/live_agent_lab.py",
            "scripts/live_lab/",
            "scripts/open_live_lab.sh",
        ),
        required_docs=(
            "docs/modules/live-lab/02-progress.md",
            "docs/modules/live-lab/04-structure.md",
        ),
    ),
)


def _run_git(args: list[str]) -> str:
    return subprocess.check_output(["git", *args], text=True, encoding="utf-8")


def normalize_path(path: str) -> str:
    return path.replace("\\", "/").strip()


def changed_paths(*, staged: bool = False, base: str = "HEAD") -> list[str]:
    args = ["diff", "--name-only"]
    if staged:
        args.append("--cached")
    else:
        args.append(base)
    return [normalize_path(line) for line in _run_git(args).splitlines() if line.strip()]


def changed_diff(path: str, *, staged: bool = False, base: str = "HEAD") -> str:
    args = ["diff", "--unified=0"]
    if staged:
        args.append("--cached")
    else:
        args.append(base)
    args.extend(["--", path])
    return _run_git(args)


def matching_rule(path: str) -> ModuleDocRule | None:
    normalized = normalize_path(path)
    for rule in MODULE_RULES:
        if any(normalized.startswith(prefix) or normalized == prefix for prefix in rule.code_prefixes):
            return rule
    return None


def is_python_implementation(path: str) -> bool:
    normalized = normalize_path(path)
    if not normalized.endswith(".py"):
        return False
    if normalized.startswith("agent_py_agent/tests/"):
        return False
    if "/tests/" in normalized:
        return False
    return matching_rule(normalized) is not None


def _module_docs_changed_for_path(path: str, changes: set[str]) -> bool:
    rule = matching_rule(path)
    return bool(rule and all(doc in changes for doc in rule.required_docs))


def _is_code_size_cleanup(changes: set[str]) -> bool:
    return "CODE_SIZE_REPORT.md" in changes


def _added_lines(diff_text: str) -> list[str]:
    return [
        line[1:]
        for line in diff_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]


def _changed_code_lines(diff_text: str) -> list[str]:
    lines: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if not line.startswith(("+", "-")):
            continue
        payload = line[1:]
        if _is_added_code_line(payload):
            lines.append(payload)
    return lines


def _is_added_code_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith("#"):
        return False
    if stripped.startswith(('"""', "'''")):
        return False
    return True


def _has_added_comment_or_doc(lines: list[str]) -> bool:
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            return True
        if stripped.startswith(('"""', "'''")):
            return True
    return False


def evaluate_sync(changes: list[str], diffs_by_path: dict[str, str]) -> list[str]:
    """Pure evaluation helper used by tests and by the CLI wrapper.

    New programmer note:
    Give this function a list of changed paths and each path's diff text. It
    returns human-readable problems. An empty list means the sync gate passes."""

    normalized_changes = {normalize_path(path) for path in changes}
    problems: list[str] = []

    for rule in MODULE_RULES:
        changed_module_files = [
            path
            for path in sorted(normalized_changes)
            if any(path.startswith(prefix) or path == prefix for prefix in rule.code_prefixes)
            and _changed_code_lines(diffs_by_path.get(path, ""))
        ]
        if not changed_module_files:
            continue
        missing_docs = [doc for doc in rule.required_docs if doc not in normalized_changes]
        if missing_docs:
            problems.append(
                f"{rule.name}: code changed ({', '.join(changed_module_files)}) "
                f"but required docs were not updated: {', '.join(missing_docs)}"
            )

    for path in sorted(normalized_changes):
        if not is_python_implementation(path):
            continue
        added = _added_lines(diffs_by_path.get(path, ""))
        has_code_size_module_docs = _is_code_size_cleanup(normalized_changes) and _module_docs_changed_for_path(
            path,
            normalized_changes,
        )
        if (
            any(_is_added_code_line(line) for line in added)
            and not _has_added_comment_or_doc(added)
            and not has_code_size_module_docs
        ):
            problems.append(
                f"{path}: implementation code changed, but no same-file comment/doc update was added "
                "(expected a useful comment, docstring, or matching module doc update)."
            )

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check code/docs/comment sync for known modules.")
    parser.add_argument("--staged", action="store_true", help="Check only staged changes.")
    parser.add_argument("--base", default="HEAD", help="Git base for unstaged+staged diff checks.")
    args = parser.parse_args(argv)

    changes = changed_paths(staged=args.staged, base=args.base)
    diffs = {path: changed_diff(path, staged=args.staged, base=args.base) for path in changes}
    problems = evaluate_sync(changes, diffs)
    if problems:
        print("DOC_SYNC_FAIL")
        for problem in problems:
            print(f"- {problem}")
        return 1
    print("DOC_SYNC_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
