# LLM: Classify result check execution outcomes into compact rescue-routing categories.
# 模块用途: 根据 test_execution.json 的摘要判断失败类型，写 refs-only 分类报告，不展开大输出正文。

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from .execution_report import TestExecutionReport


# LLM: TestFailureClassificationRequest bundles test report and optional runner output metadata.
# 类用途: 保存一次测试失败分类所需输入；新增父级 oracle 字段时扩展这个 bundle。
@dataclass(frozen=True)
class TestFailureClassificationRequest:
    """Input bundle for classifying a test execution report."""

    __test__: ClassVar[bool] = False

    report: TestExecutionReport
    output: dict[str, object] = field(default_factory=dict)
    reserved: dict[str, Any] = field(default_factory=dict)


# LLM: TestFailureClassificationItem is one compact record-level diagnosis.
# 类用途: 保存单条测试记录的分类、建议动作和短摘要；不会复制完整 stdout/stderr。
@dataclass(frozen=True)
class TestFailureClassificationItem:
    """Classification for one test execution record."""

    __test__: ClassVar[bool] = False

    test_name: str
    category: str
    recommended_action: str
    command: str = ""
    summary: str = ""


# LLM: TestFailureClassificationReport is the persisted facts source for repair/rescue planning.
# 类用途: 汇总测试报告分类结果；后续 rescue/repair 只读这个摘要和 test_execution ref。
@dataclass(frozen=True)
class TestFailureClassificationReport:
    """Compact classification report over a test execution report."""

    __test__: ClassVar[bool] = False

    overall_status: str
    primary_category: str
    recommended_action: str
    counts: dict[str, int]
    items: list[TestFailureClassificationItem] = field(default_factory=list)
    test_execution_ref: str = ""
    reserved: dict[str, Any] = field(default_factory=dict)

    # LLM: to_dict keeps the JSON report schema stable and path/ref oriented.
    # 函数用途: 转换成可写入 JSON 的结构；不包含完整执行输出。
    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema"] = "test_failure_classification.v1"
        return payload


# LLM: classify_test_execution_report converts execution evidence into rescue-routing categories.
# 函数用途: 把测试报告归类为通过、runner 输出缺口、命令问题、断言失败、语法/导入问题等。
def classify_test_execution_report(
    request: TestFailureClassificationRequest,
) -> TestFailureClassificationReport:
    """Classify a test execution report for parent repair/rescue routing."""

    report = request.report
    if report.total_tests <= 0:
        return _zero_tests_report(request)
    items = [_classify_record(record) for record in report.records]
    counts = _counts(items)
    primary = _primary_category(counts)
    return TestFailureClassificationReport(
        overall_status="passed" if report.failed == 0 else "failed",
        primary_category=primary,
        recommended_action=_recommended_action(primary),
        counts=counts,
        items=items,
        test_execution_ref=str(report.json_path),
        reserved=_reserved(),
    )


# LLM: write_test_failure_classification persists the compact report next to test_execution.json.
# 函数用途: 写入 `test_failure_classification.json`，给后续 follow-up、repair 和真实 E2E 复盘引用。
def write_test_failure_classification(
    output_dir: str | Path,
    request: TestFailureClassificationRequest,
) -> Path:
    """Write a compact classification report and return its path."""

    report = classify_test_execution_report(request)
    path = Path(output_dir) / "test_failure_classification.json"
    path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


# LLM: _zero_tests_report distinguishes missing structured tests from actual test failures.
# 函数用途: 没有测试记录时生成 runner 输出缺口分类，避免进入“修业务代码”的错误路径。
def _zero_tests_report(request: TestFailureClassificationRequest) -> TestFailureClassificationReport:
    return TestFailureClassificationReport(
        overall_status="blocked",
        primary_category="runner_output_missing_tests",
        recommended_action="fix_runner_output",
        counts={"runner_output_missing_tests": 1},
        test_execution_ref=str(request.report.json_path),
        reserved=_reserved(),
    )


# LLM: _classify_record is intentionally heuristic and conservative for command/file/content records.
# 函数用途: 根据单条测试记录的执行状态、错误、退出码和短输出判断失败类型。
def _classify_record(record) -> TestFailureClassificationItem:
    category = _record_category(record)
    return TestFailureClassificationItem(
        test_name=record.test_name,
        category=category,
        recommended_action=_recommended_action(category),
        command=record.command,
        summary=_short_summary(record),
    )


# LLM: _record_category keeps the public item builder flat for code-size guardrails.
# 函数用途: 返回单条测试记录的分类，避免分类对象构造函数里继续堆嵌套判断。
def _record_category(record) -> str:
    if record.passed:
        return "passed"
    if not record.executed:
        return _not_executed_category(record)
    return _executed_failure_category(record)


# LLM: _not_executed_category distinguishes safety rejections from missing execution facts.
# 函数用途: 判断未执行测试是命令被安全拦截，还是 runner/父级没有真正启动测试。
def _not_executed_category(record) -> str:
    return "command_rejected" if _is_command_rejected(record) else "not_executed"


# LLM: _executed_failure_category separates code/test failures from import/syntax collection failures.
# 函数用途: 对已执行但失败的记录做更细分类，便于 repair 子代理拿到正确入口。
def _executed_failure_category(record) -> str:
    text = _record_text(record)
    if any(token in text for token in ("SyntaxError", "IndentationError", "ImportError", "ModuleNotFoundError")):
        return "syntax_or_import_error"
    if any(token in text for token in ("AssertionError", "assert ", "E       assert")):
        return "assertion_failure"
    if "timed out" in text.lower() or "timeout" in str(record.error).lower():
        return "timeout"
    return "runtime_failure"


# LLM: _is_command_rejected recognizes executor safety rejections without depending on localized wording only.
# 函数用途: 判断失败是否来自安全拦截，而不是业务测试真的跑失败。
def _is_command_rejected(record) -> bool:
    reason = str(record.validation_result.get("reason") or "")
    text = _record_text(record)
    return reason == "command_rejected" or "高风险 shell 字符" in text or "command_rejected" in text


# LLM: _primary_category picks the most actionable non-passed category.
# 函数用途: 从分类计数中选择主因；顺序代表当前 rescue 优先级。
def _primary_category(counts: dict[str, int]) -> str:
    for category in (
        "assertion_failure",
        "syntax_or_import_error",
        "runtime_failure",
        "command_rejected",
        "not_executed",
        "timeout",
        "runner_output_missing_tests",
    ):
        if counts.get(category):
            return category
    return "passed"


# LLM: _recommended_action maps categories into coarse parent workflow actions.
# 函数用途: 给后续 repair/rescue 流程提供稳定动作词，而不是直接解析文本。
def _recommended_action(category: str) -> str:
    if category == "passed":
        return "summarize_or_deliver"
    if category in {"assertion_failure", "syntax_or_import_error", "runtime_failure"}:
        return "repair_code"
    if category in {"command_rejected", "not_executed", "runner_output_missing_tests"}:
        return "fix_runner_output"
    if category == "timeout":
        return "takeover_or_reassign"
    return "manual_review"


# LLM: _counts keeps summary computation deterministic for JSON and CLI displays.
# 函数用途: 统计每个分类出现次数；没有 records 时由 zero-tests 分支处理。
def _counts(items: list[TestFailureClassificationItem]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.category] = counts.get(item.category, 0) + 1
    return counts


# LLM: _short_summary trims noisy process output to a human-useful failure hint.
# 函数用途: 从错误/stdout/stderr 中抽取短摘要，避免分类报告复制大输出。
def _short_summary(record) -> str:
    text = _record_text(record)
    if not text:
        return ""
    return text[-240:]


# LLM: _record_text normalizes all small diagnostic channels into one string for heuristics.
# 函数用途: 拼接 error/stdout/stderr，但只给分类和短摘要使用，不写完整正文。
def _record_text(record) -> str:
    return "\n".join(str(item or "") for item in (record.error, record.stdout, record.stderr))


# LLM: _reserved records the current non-mutating and refs-only contract for future automation.
# 函数用途: 固定分类报告的安全边界，后续自动化不能把它误当作已执行修复。
def _reserved() -> dict[str, Any]:
    return {
        "refs_only": True,
        "reads_artifact_bodies": False,
        "mutates_task_state": False,
        "auto_repairs": False,
    }
