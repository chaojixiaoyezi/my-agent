#!/usr/bin/env python3
# LLM: Contract-test pyramid checks keep production code generic while requiring local fast-test layers to exist.
# 模块用途: 审核合同测试目录、参考项目映射和生产代码专项词，防止真实任务专项逻辑重新渗回生产层。

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
    "deepseek",
    "论文翻译",
    "github升星",
    "github star",
    "购物网站",
    "最热文章",
    "2026年后每周",
)


# LLM: ContractPyramidReport is the stable machine result for pyramid coverage and hygiene checks.
# 类用途: 表示合同测试金字塔检查结果，包含错误码、finding 列表和已映射参考项目。
@dataclass(frozen=True)
class ContractPyramidReport:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, str], ...]
    reference_projects_checked: tuple[str, ...]


# LLM: check_contract_test_pyramid is the public gate that validates local fast-test layers and production hygiene.
# 函数用途: 检查合同测试目录是否齐全、文档是否映射参考项目，以及生产代码中是否混入专项任务词。
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


# LLM: _check_required_test_dirs verifies the local fast-test pyramid exists before real-environment validation.
# 函数用途: 校验 contracts/fake_tools/fake_llm/replay/scenario_packs 目录存在且不为空。
def _check_required_test_dirs(root: Path, findings: list[dict[str, str]]) -> None:
    for rel in REQUIRED_TEST_DIRS:
        path = root / rel
        if not path.is_dir():
            findings.append(_finding("CONTRACT_TEST_DIR_MISSING", rel, "required test pyramid directory is missing"))
            continue
        if not any(child.is_file() for child in path.iterdir()):
            findings.append(_finding("CONTRACT_TEST_DIR_EMPTY", rel, "required test pyramid directory has no fixtures"))


# LLM: _check_reference_map ensures the contract-testing design doc still cites the chosen external reference repos.
# 函数用途: 检查设计文档是否覆盖既定参考项目，避免后续开发脱离对照来源。
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


# LLM: _check_production_needles guards against task-specific words leaking into production contract code.
# 函数用途: 扫描生产 agent 代码中的专项任务词，提示需要继续去专项化的实现。
def _check_production_needles(root: Path, findings: list[dict[str, str]]) -> None:
    production_root = root / "agent_py_agent" / "agent"
    if not production_root.exists():
        findings.append(_finding("PRODUCTION_ROOT_MISSING", str(production_root), "production agent root is missing"))
        return
    for path in production_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        for needle in PRODUCTION_TASK_SPECIFIC_NEEDLES:
            if needle in text:
                findings.append(
                    _finding(
                        "PRODUCTION_TASK_SPECIFIC_CONTRACT",
                        str(path.relative_to(root)),
                        needle,
                    )
                )


# LLM: _finding keeps pyramid gate failures compact, stable, and easy to diff in CI.
# 函数用途: 统一生成合同测试金字塔检查 finding，保持 code/location/detail 结构稳定。
def _finding(code: str, location: str, detail: str) -> dict[str, str]:
    return {"code": code, "location": location, "detail": detail}


# LLM: main exposes the pyramid gate as a small CLI for CI and local verification.
# 函数用途: 解析命令行参数并输出合同测试金字塔检查结果，供脚本或 CI 直接调用。
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
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif report.ok:
        checked = ", ".join(report.reference_projects_checked)
        print(f"contract test pyramid ok; checked references: {checked}")
    else:
        print("contract test pyramid failed")
        for item in report.findings:
            print(f"- {item['code']}: {item['location']} :: {item['detail']}")
    return 0 if report.ok else 1


__all__ = ["ContractPyramidReport", "check_contract_test_pyramid", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
