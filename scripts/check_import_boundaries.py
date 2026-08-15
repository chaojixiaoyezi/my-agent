#!/usr/bin/env python3

from __future__ import annotations

"""Enforce one-way runtime imports and keep developer harnesses off the hot path."""

import argparse
import ast
import importlib.util
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from package_boundary_policy import is_dev_only_module

SOURCE_ROOTS = (ROOT / "agent_py_agent" / "agent", ROOT / "agent_py_agent" / "cli")


@dataclass(frozen=True)
class LayerRule:
    source_prefix: str
    forbidden_prefixes: tuple[str, ...]


LAYER_RULES = (
    LayerRule("agent_py_agent.cli.", ("agent_py_agent.agent.memory_store", "agent_py_agent.agent.tooling", "agent_py_agent.agent.subagents")),
    LayerRule("agent_py_agent.agent.agent_core.", ("agent_py_agent.cli", "agent_py_agent.agent.adapter")),
    LayerRule("agent_py_agent.agent.subagents.", ("agent_py_agent.cli", "agent_py_agent.agent.agent_core", "agent_py_agent.agent.gateway_parts")),
    LayerRule("agent_py_agent.agent.gateway_parts.", ("agent_py_agent.cli", "agent_py_agent.agent.agent_core", "agent_py_agent.agent.subagents")),
    LayerRule("agent_py_agent.agent.memory_store.", ("agent_py_agent.agent.memory_routing", "agent_py_agent.agent.memory_archive")),
    LayerRule("agent_py_agent.agent.memory_routing.", ("agent_py_agent.agent.subagents", "agent_py_agent.agent.agent_core")),
    LayerRule("agent_py_agent.agent.memory_archive.", ("agent_py_agent.agent.subagents", "agent_py_agent.agent.agent_core")),
    LayerRule("agent_py_agent.agent.tooling.", ("agent_py_agent.agent.agent_core", "agent_py_agent.agent.subagents")),
    LayerRule("agent_py_agent.agent.adapter.", ("agent_py_agent.agent.agent_core", "agent_py_agent.agent.subagents")),
    LayerRule("agent_py_agent.agent.extensions.", ("agent_py_agent.agent.agent_core", "agent_py_agent.agent.subagents")),
)

# Exact historical debts from the pre-P1 codebase. The boundary is enforced as
# "no new debt": changing either endpoint stops matching and fails CI. Removal
# only shrinks this list; broad wildcard exemptions are intentionally forbidden.
LEGACY_LAYER_EXCEPTIONS = frozenset(
    {
        ("agent_py_agent.cli._actions", "agent_py_agent.agent.subagents.models"),
        ("agent_py_agent.cli._actions", "agent_py_agent.agent.subagents.services.actions"),
        ("agent_py_agent.cli._board", "agent_py_agent.agent.subagents.models"),
        ("agent_py_agent.cli._hierarchy", "agent_py_agent.agent.subagents.services.hierarchy.recovery"),
        ("agent_py_agent.cli._hierarchy", "agent_py_agent.agent.subagents.services.hierarchy.scheduler"),
        ("agent_py_agent.cli._inspection", "agent_py_agent.agent.subagents.models"),
        ("agent_py_agent.cli._inspection", "agent_py_agent.agent.subagents.run_budget"),
        ("agent_py_agent.cli._leadership", "agent_py_agent.agent.subagents.models"),
        ("agent_py_agent.cli._review", "agent_py_agent.agent.subagents.patch"),
        ("agent_py_agent.cli._review_tests", "agent_py_agent.agent.subagents.execution.executor"),
        ("agent_py_agent.cli._review_tests", "agent_py_agent.agent.subagents.execution.report"),
        ("agent_py_agent.cli._review_tests", "agent_py_agent.agent.subagents.execution.test_items"),
        ("agent_py_agent.cli._review_tests", "agent_py_agent.agent.subagents.test_failure_classification"),
        ("agent_py_agent.cli.dispatch_background", "agent_py_agent.agent.subagents.process_control"),
        ("agent_py_agent.cli.local_commands", "agent_py_agent.agent.subagents.models"),
        ("agent_py_agent.cli.scenario_utils", "agent_py_agent.agent.subagents.models"),
        ("agent_py_agent.cli.task_commands", "agent_py_agent.agent.subagents.models"),
        ("agent_py_agent.agent.tooling.controlled_exec", "agent_py_agent.agent.subagents.controlled_exec_gateway"),
        ("agent_py_agent.agent.tooling.controlled_exec", "agent_py_agent.agent.subagents.models"),
        ("agent_py_agent.agent.tooling.controlled_exec", "agent_py_agent.agent.subagents.shell_gateway"),
        ("agent_py_agent.agent.tooling.controlled_exec", "agent_py_agent.agent.subagents.shell_gateway_execution"),
        ("agent_py_agent.agent.tooling.controlled_exec", "agent_py_agent.agent.subagents.task_trash"),
        ("agent_py_agent.agent.tooling.web_project_integrity", "agent_py_agent.agent.subagents.static_site"),
        ("agent_py_agent.agent.gateway_parts.request_execution", "agent_py_agent.agent.agent_core.runtime_mixin"),
        ("agent_py_agent.agent.memory_archive.compact_runtime_handoff", "agent_py_agent.agent.subagents.models"),
        ("agent_py_agent.agent.memory_archive.compact_work_state.sources", "agent_py_agent.agent.subagents.services.agent_run_state"),
        ("agent_py_agent.agent.memory_archive.compact_work_state.run_intent_sources", "agent_py_agent.agent.subagents.services.agent_run_state"),
        ("agent_py_agent.agent.subagents.services.workflow", "agent_py_agent.agent.agent_core.orchestration.tool_grants"),
    }
)


@dataclass(frozen=True)
class ImportBoundaryFinding:
    code: str
    source: str
    target: str
    line: int


def check_import_boundaries(root: Path = ROOT) -> list[ImportBoundaryFinding]:
    findings: list[ImportBoundaryFinding] = []
    for path in _production_python_files(root):
        findings.extend(_findings_for_file(root, path))
    return sorted(findings, key=lambda item: (item.source, item.line, item.target))


def _findings_for_file(root: Path, path: Path) -> list[ImportBoundaryFinding]:
    source = _module_name(root, path)
    if is_dev_only_module(source):
        return []
    findings: list[ImportBoundaryFinding] = []
    for target, line in _imports_for(path, source):
        code = _boundary_code(source, target)
        if code and (source, target) not in LEGACY_LAYER_EXCEPTIONS:
            findings.append(ImportBoundaryFinding(code, path.relative_to(root).as_posix(), target, line))
    return findings


def _production_python_files(root: Path) -> list[Path]:
    roots = (root / "agent_py_agent" / "agent", root / "agent_py_agent" / "cli")
    return sorted(path for source_root in roots for path in source_root.rglob("*.py"))


def _module_name(root: Path, path: Path) -> str:
    return ".".join(path.relative_to(root).with_suffix("").parts)


def _imports_for(path: Path, source_module: str) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[tuple[str, int]] = []
    source_package = source_module.rsplit(".", 1)[0]
    for node in ast.walk(tree):
        imports.extend(_node_imports(node, source_package))
    return imports


def _node_imports(node: ast.AST, source_package: str) -> list[tuple[str, int]]:
    if isinstance(node, ast.Import):
        return [(alias.name, node.lineno) for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        target = _resolve_import_from(source_package, node)
        return [(target, node.lineno)] if target else []
    return []


def _resolve_import_from(source_package: str, node: ast.ImportFrom) -> str:
    module = str(node.module or "")
    if node.level <= 0:
        return module
    relative = "." * node.level + module
    try:
        return importlib.util.resolve_name(relative, source_package)
    except (ImportError, ValueError):
        return ""


def _boundary_code(source: str, target: str) -> str:
    if target.startswith("agent_py_agent.tests") or target == "scripts" or target.startswith("scripts."):
        return "PRODUCTION_IMPORTS_TEST_OR_SCRIPT"
    if is_dev_only_module(target):
        return "PRODUCTION_IMPORTS_DEV_HARNESS"
    if source.startswith("agent_py_agent.agent.") and target.startswith("agent_py_agent.cli."):
        return "RUNTIME_IMPORTS_CLI"
    for rule in LAYER_RULES:
        if not source.startswith(rule.source_prefix):
            continue
        if any(target == prefix or target.startswith(prefix + ".") for prefix in rule.forbidden_prefixes):
            return "LAYER_BOUNDARY_FORBIDDEN"
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check production import boundaries.")
    parser.add_argument("--repo-root", default=str(ROOT))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    findings = check_import_boundaries(Path(args.repo_root).resolve())
    if args.json:
        print(json.dumps([asdict(item) for item in findings], ensure_ascii=False, indent=2))
    else:
        print(f"IMPORT_BOUNDARIES findings={len(findings)}")
        for item in findings:
            print(f"- {item.code}: {item.source}:{item.line} -> {item.target}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
