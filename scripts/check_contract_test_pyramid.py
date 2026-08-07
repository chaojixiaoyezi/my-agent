#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

REQUIRED_TEST_DIRS = (
    "agent_py_agent/tests/contracts",
    "agent_py_agent/tests/fake_tools",
    "agent_py_agent/tests/fake_llm",
    "agent_py_agent/tests/replay",
    "agent_py_agent/tests/scenario_packs",
)

REFERENCE_PROJECTS = {
    "agentscope-main": "AgentScope",
    "claude-code": "Claude Code",
    "claw-code-main": "Claw Code",
    "codex-main": "Codex",
    "free-code-main": "Free Code",
    "hermes-agent-main": "Hermes",
    "langchain-master": "LangChain",
    "langgraph-main": "LangGraph",
    "openai-agents-python-main": "OpenAI Agents SDK",
    "openclaude-main": "OpenClaude",
    "openclaw-main": "OpenClaw",
    "openhuman-main": "OpenHuman",
    "my-agent-architecture-review-20260519-clean": "my-agent-architecture-review",
    "my-agent-feature-card-message-runtime": "my-agent-feature-card-message-runtime",
}

PRODUCTION_TASK_SPECIFIC_NEEDLES = (
    # 注意:本名单只放【测试任务特有的内容】(任务名/任务主题),不放通用术语。
    # deepseek 曾入名单(2026-05 防测试任务把模型名写进生产代码),其污染源已删;
    # 但 "DeepSeek" 是通用模型名,生产代码注释/名单里引用模型名是正当工程记录,
    # 子串匹配会反复误报(2026-08-08 completion.py 真机观察注释中招),故移除。
    "论文翻译",
    "github升星",
    "github star",
    "购物网站",
    "最热文章",
    "2026年后每周",
)


@dataclass(frozen=True)
class ContractPyramidReport:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, str], ...]
    reference_projects_checked: tuple[str, ...]


def check_contract_test_pyramid(repo_root: Path) -> ContractPyramidReport:
    root = Path(repo_root)
    findings: list[dict[str, str]] = []
    checked: list[str] = []
    _check_required_test_dirs(root, findings)
    checked.extend(_check_reference_map(root, findings))
    _check_production_needles(root, findings)
    return ContractPyramidReport(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(item["code"] for item in findings)),
        findings=tuple(findings),
        reference_projects_checked=tuple(checked),
    )


def _check_required_test_dirs(root: Path, findings: list[dict[str, str]]) -> None:
    for rel in REQUIRED_TEST_DIRS:
        path = root / rel
        if not path.is_dir():
            findings.append(_finding("CONTRACT_TEST_DIR_MISSING", rel, "required test pyramid directory is missing"))
            continue
        if not any(child.is_file() for child in path.iterdir()):
            findings.append(_finding("CONTRACT_TEST_DIR_EMPTY", rel, "required test pyramid directory has no fixtures"))


def _check_reference_map(root: Path, findings: list[dict[str, str]]) -> list[str]:
    path = root / "docs/design/main-agent-contract-testing.md"
    if not path.exists():
        findings.append(_finding("REFERENCE_MAP_MISSING", str(path), "reference map document is missing"))
        return []
    text = path.read_text(encoding="utf-8", errors="replace").lower()
    checked: list[str] = []
    for project, marker in REFERENCE_PROJECTS.items():
        if marker.lower() in text or project.lower() in text:
            checked.append(project)
        else:
            findings.append(_finding("REFERENCE_PROJECT_MISSING", project, "reference project is not mapped"))
    return checked


def _check_production_needles(root: Path, findings: list[dict[str, str]]) -> None:
    production_root = root / "agent_py_agent" / "agent"
    if not production_root.exists():
        findings.append(_finding("PRODUCTION_ROOT_MISSING", str(production_root), "production agent root is missing"))
        return
    for path in production_root.rglob("*.py"):
        findings.extend(_production_task_specific_findings(root, path))


def _production_task_specific_findings(root: Path, path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8", errors="replace").lower()
    return [
        _finding("PRODUCTION_TASK_SPECIFIC_CONTRACT", str(path.relative_to(root)), needle)
        for needle in PRODUCTION_TASK_SPECIFIC_NEEDLES
        if needle in text
    ]


def _finding(code: str, location: str, detail: str) -> dict[str, str]:
    return {"code": code, "location": location, "detail": detail}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate contract test pyramid coverage and production contract hygiene.")
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository root to validate. Defaults to the current working directory.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full report as JSON.",
    )
    args = parser.parse_args(argv)
    report = check_contract_test_pyramid(Path(args.repo_root).resolve())
    payload = {
        "ok": report.ok,
        "error_codes": list(report.error_codes),
        "findings": list(report.findings),
        "reference_projects_checked": list(report.reference_projects_checked),
    }
    _print_report(payload, report, json_output=bool(args.json))
    return 0 if report.ok else 1


def _print_report(payload: dict[str, object], report: ContractPyramidReport, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if report.ok:
        checked = ", ".join(report.reference_projects_checked)
        print(f"contract test pyramid ok; checked references: {checked}")
        return
    print("contract test pyramid failed")
    for item in report.findings:
        print(f"- {item['code']}: {item['location']} :: {item['detail']}")


__all__ = ["ContractPyramidReport", "check_contract_test_pyramid", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
