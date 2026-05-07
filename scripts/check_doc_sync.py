# LLM: Documentation-sync guard; keep module rules and git-diff evaluation stable for CI.
# 模块用途: 检查代码、模块文档和注释是否一起更新，避免实现和说明脱节。

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


# LLM: ModuleDocRule 描述模块同步规则；新增模块时要补齐 code 和 docs 前缀。
# 类用途: 绑定一个模块的代码路径和必须同步更新的文档路径。
@dataclass(frozen=True)
class ModuleDocRule:
    """Describes one module's code paths and the docs that must move with them.

    New programmer note:
    A rule is a small checklist. If changed files live under code_prefixes, then
    required_docs must also appear in the same git diff."""

    name: str
    code_prefixes: tuple[str, ...]
    required_docs: tuple[str, ...]


COMMENT_SYNC_MARKERS = ("LLM:", "模块用途:", "函数用途:", "类用途:", "新手说明:", "参数说明:", "返回说明:")

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
            "agent_py_agent/cli/memory_commands.py",
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


# LLM: _run_git 是本守卫唯一 git 调用点；异常会直接让检查失败。
# 函数用途: 执行 git diff/status 类命令并返回 UTF-8 文本输出。
def _run_git(args: list[str]) -> str:
    return subprocess.check_output(["git", *args], text=True, encoding="utf-8")


# LLM: normalize_path 统一路径格式；规则匹配全部依赖正斜杠口径。
# 函数用途: 去掉路径首尾空白，并把 Windows 分隔符转换成仓库路径格式。
def normalize_path(path: str) -> str:
    return path.replace("\\", "/").strip()


# LLM: changed_paths 收集待评估文件；staged/base 参数决定检查范围。
# 函数用途: 调用 git diff --name-only，返回本次变更涉及的规范化路径。
def changed_paths(*, staged: bool = False, base: str = "HEAD") -> list[str]:
    args = ["diff", "--name-only"]
    if staged:
        args.append("--cached")
    else:
        args.append(base)
    return [normalize_path(line) for line in _run_git(args).splitlines() if line.strip()]


# LLM: changed_diff 提供逐文件新增行；注释同步判断依赖 unified=0 输出。
# 函数用途: 读取单个文件的零上下文 diff，供新增代码和注释检测使用。
def changed_diff(path: str, *, staged: bool = False, base: str = "HEAD") -> str:
    args = ["diff", "--unified=0"]
    if staged:
        args.append("--cached")
    else:
        args.append(base)
    args.extend(["--", path])
    return _run_git(args)


# LLM: matching_rule 决定变更归属模块；前缀匹配顺序要保持清晰。
# 函数用途: 根据路径找到对应 ModuleDocRule，找不到则不纳入模块同步检查。
def matching_rule(path: str) -> ModuleDocRule | None:
    normalized = normalize_path(path)
    for rule in MODULE_RULES:
        if any(normalized.startswith(prefix) or normalized == prefix for prefix in rule.code_prefixes):
            return rule
    return None


# LLM: is_python_implementation 过滤实现文件；测试文件不触发 same-file 注释要求。
# 函数用途: 判断路径是否是受模块规则约束的 Python 实现文件。
def is_python_implementation(path: str) -> bool:
    normalized = normalize_path(path)
    if not normalized.endswith(".py"):
        return False
    if normalized.startswith("agent_py_agent/tests/"):
        return False
    if "/tests/" in normalized:
        return False
    return matching_rule(normalized) is not None


# LLM: _module_docs_changed_for_path 是 code-size 清理豁免条件的一部分。
# 函数用途: 判断某个实现文件所属模块的必需文档是否全部在本次 diff 中更新。
def _module_docs_changed_for_path(path: str, changes: set[str]) -> bool:
    rule = matching_rule(path)
    return bool(rule and all(doc in changes for doc in rule.required_docs))


# LLM: _is_code_size_cleanup 识别规模治理清理；只看 CODE_SIZE_REPORT.md 是否变更。
# 函数用途: 判断本次变更是否包含 code-size 报告更新。
def _is_code_size_cleanup(changes: set[str]) -> bool:
    return "CODE_SIZE_REPORT.md" in changes


# LLM: _added_lines 抽取新增内容；后续逻辑只看真实新增行。
# 函数用途: 从 unified diff 中取出新增行，并排除 +++ 文件头。
def _added_lines(diff_text: str) -> list[str]:
    return [
        line[1:]
        for line in diff_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]


# LLM: _is_added_code_line 区分代码和说明；空行、注释、docstring 不算实现。
# 函数用途: 判断新增行是否是需要配套注释或文档的实现代码。
def _is_added_code_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith("#"):
        return False
    if stripped.startswith(('"""', "'''")):
        return False
    return True


# LLM: _has_added_comment_or_doc 识别同文件说明更新；marker 列表是契约。
# 函数用途: 判断新增行中是否包含注释、模块用途、函数用途或类用途说明。
def _has_added_comment_or_doc(lines: list[str]) -> bool:
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            return True
        if any(marker in line for marker in COMMENT_SYNC_MARKERS):
            return True
    return False


# LLM: evaluate_sync 是纯判断核心；测试直接传入 paths 和 diff 文本。
# 函数用途: 生成缺失模块文档或缺少同文件说明的可读问题列表。
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
                f"(expected one of {', '.join(COMMENT_SYNC_MARKERS)} or a # comment)."
            )

    return problems


# LLM: main 是文档同步守卫 CLI；输出 DOC_SYNC_PASS/FAIL 供 CI 读取。
# 函数用途: 解析检查范围，收集 diff，打印同步结果并返回退出码。
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
