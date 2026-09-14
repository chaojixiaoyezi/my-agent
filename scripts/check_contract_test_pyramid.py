#!/usr/bin/env python3
# LLM: 开发期测试分层守卫；不参与运行时任务判定。
# 模块用途: 检查测试组织、文档及生产代码卫生，输出可定位的问题。

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

REQUIRED_TEST_LAYERS = ("contract", "fake-tool", "fake-llm", "replay", "real-tui")

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


# LLM: 守卫结果结构；字段变更需同步 CLI JSON 和测试。
# 类用途: 汇总检查层次及缺失项，不修改仓库。
@dataclass(frozen=True)
class ContractPyramidReport:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, str], ...]
    test_layers_checked: tuple[str, ...]


# LLM: 开发检查总入口，保持失败码稳定并同步 gate 测试。
# 函数用途: 只读检查测试分层、文档和专项代码污染。
def check_contract_test_pyramid(repo_root: Path) -> ContractPyramidReport:
    root = Path(repo_root)
    findings: list[dict[str, str]] = []
    checked: list[str] = []
    _check_required_test_dirs(root, findings)
    checked.extend(_check_test_strategy(root, findings))
    _check_production_needles(root, findings)
    return ContractPyramidReport(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(item["code"] for item in findings)),
        findings=tuple(findings),
        test_layers_checked=tuple(checked),
    )


# LLM: 目录路径与 REQUIRED_TEST_DIRS 同源。
# 函数用途: 记录缺失或空的测试目录，不创建占位文件。
def _check_required_test_dirs(root: Path, findings: list[dict[str, str]]) -> None:
    for rel in REQUIRED_TEST_DIRS:
        path = root / rel
        if not path.is_dir():
            findings.append(_finding("CONTRACT_TEST_DIR_MISSING", rel, "required test pyramid directory is missing"))
            continue
        if not any(child.is_file() for child in path.iterdir()):
            findings.append(_finding("CONTRACT_TEST_DIR_EMPTY", rel, "required test pyramid directory has no fixtures"))


# LLM: 检查稳定的测试层次标题，不依赖外部产品名称。
# 函数用途: 将策略文档缺失的测试层次加入诊断。
def _check_test_strategy(root: Path, findings: list[dict[str, str]]) -> list[str]:
    path = root / "docs/design/main-agent-contract-testing.md"
    if not path.exists():
        findings.append(_finding("TEST_STRATEGY_MISSING", str(path), "测试策略文档缺失"))
        return []
    text = path.read_text(encoding="utf-8", errors="replace").lower()
    checked: list[str] = []
    headings = {line.strip() for line in text.splitlines() if line.startswith("## ")}
    for layer in REQUIRED_TEST_LAYERS:
        if f"## {layer}" in headings:
            checked.append(layer)
        else:
            findings.append(_finding("TEST_LAYER_UNDOCUMENTED", layer, "测试层次缺少说明"))
    return checked


# LLM: 仅开发期扫描，不连接真实任务状态机。
# 函数用途: 遍历生产模块，记录误放入的测试任务专用文本。
def _check_production_needles(root: Path, findings: list[dict[str, str]]) -> None:
    production_root = root / "agent_py_agent" / "agent"
    if not production_root.exists():
        findings.append(_finding("PRODUCTION_ROOT_MISSING", str(production_root), "production agent root is missing"))
        return
    for path in production_root.rglob("*.py"):
        findings.extend(_production_task_specific_findings(root, path))


# LLM: 名单只包含测试任务专属词，不限制普通业务术语。
# 函数用途: 读取单文件并返回匹配位置，不修改内容。
def _production_task_specific_findings(root: Path, path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8", errors="replace").lower()
    return [
        _finding("PRODUCTION_TASK_SPECIFIC_CONTRACT", str(path.relative_to(root)), needle)
        for needle in PRODUCTION_TASK_SPECIFIC_NEEDLES
        if needle in text
    ]


# LLM: 开发诊断统一结构，调用方可按 code 定位。
# 函数用途: 把代码、位置和原因组成结果项。
def _finding(code: str, location: str, detail: str) -> dict[str, str]:
    return {"code": code, "location": location, "detail": detail}


# LLM: 命令行只读检查入口，失败返回非零码。
# 函数用途: 提供本地与 CI 共用的检查命令。
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
        "test_layers_checked": list(report.test_layers_checked),
    }
    _print_report(payload, report, json_output=bool(args.json))
    return 0 if report.ok else 1


# LLM: 展示层不改变检查结果，JSON 与数据类同步。
# 函数用途: 将检查结果输出为文本或 JSON。
def _print_report(payload: dict[str, object], report: ContractPyramidReport, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if report.ok:
        checked = ", ".join(report.test_layers_checked)
        print(f"contract test pyramid ok; checked layers: {checked}")
        return
    print("contract test pyramid failed")
    for item in report.findings:
        print(f"- {item['code']}: {item['location']} :: {item['detail']}")


__all__ = ["ContractPyramidReport", "check_contract_test_pyramid", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
