# LLM: Runtime fact source writes explicit run-local facts for later compact work-state recovery.
# 模块用途: 在真实 run 保存时写入可审计的 task.json 事实源，让 compact apply 读取明确验收/约束/测试。

from __future__ import annotations

"""run-local fact source writer for compact/resume."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# LLM: RuntimeFactSourceRequest bundles the real run facts that are safe to persist for compact.
# 类用途: 描述一次真实 run 可写入事实源的目标、下一步、工具记录和运行状态。
@dataclass(frozen=True)
class RuntimeFactSourceRequest:
    root: Path
    request_id: str
    user_prompt: str
    response_text: str
    backend: str
    status: str
    next_actions: list[str]
    archive_tool_calls: list[Any]


# LLM: ApprovedRuntimeFactSourceRequest carries user-approved compact completion facts without parsing prose.
# 类用途: 描述手动补齐 compact resume 缺失字段时要写入的明确验收、约束和测试事实。
@dataclass(frozen=True)
class ApprovedRuntimeFactSourceRequest:
    root: Path
    fact_id: str
    goal: str
    next_actions: list[str]
    acceptance: list[str]
    constraints: list[str]
    latest_tests: list[str]
    source_apply_id: str = ""


# LLM: write_runtime_fact_source writes explicit facts only and returns a directory ref for recovery snapshots.
# 函数用途: 写入 memory_archive/runtime_facts/<request_id>/task.json，供 compact work_state 扫描。
def write_runtime_fact_source(request: RuntimeFactSourceRequest) -> str:
    if not request.request_id:
        return ""
    root = request.root / "memory_archive" / "runtime_facts" / _safe_id(request.request_id)
    root.mkdir(parents=True, exist_ok=True)
    payload = _runtime_fact_payload(request)
    (root / "task.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return str(root)


# LLM: write_approved_runtime_fact_source is the manual bridge from completion prompt to compact facts.
# 函数用途: 把用户确认过的补全字段写成 runtime_facts/<fact_id>/task.json，供下一次 compact apply 读取。
def write_approved_runtime_fact_source(request: ApprovedRuntimeFactSourceRequest) -> str:
    if not request.fact_id:
        return ""
    root = request.root / "memory_archive" / "runtime_facts" / _safe_id(request.fact_id)
    root.mkdir(parents=True, exist_ok=True)
    payload = _approved_fact_payload(request)
    (root / "task.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return str(root)


# LLM: _runtime_fact_payload keeps explicit user-authored fields separate from run status fields.
# 函数用途: 生成 task.json；验收/约束只来自明确标题或标签，测试来自明确测试条目或实际工具命令。
def _runtime_fact_payload(request: RuntimeFactSourceRequest) -> dict[str, Any]:
    sections = _explicit_sections(request.user_prompt)
    latest_tests = _dedupe([*sections.tests, *_tool_test_items(request.archive_tool_calls)])
    return {
        "version": 1,
        "source": "runtime_fact_source",
        "request_id": request.request_id,
        "goal": request.user_prompt.strip(),
        "next_actions": list(request.next_actions),
        "acceptance": sections.acceptance,
        "constraints": sections.constraints,
        "latest_tests": latest_tests,
        "run_status": {
            "status": request.status,
            "backend": request.backend,
            "response_present": bool(request.response_text.strip()),
        },
    }


# LLM: _approved_fact_payload keeps manual completion facts auditable and schema-compatible with task.json readers.
# 函数用途: 生成手动补全事实源 payload，保留 source_apply_id 方便追溯是哪次 compact 被补齐。
def _approved_fact_payload(request: ApprovedRuntimeFactSourceRequest) -> dict[str, Any]:
    return {
        "version": 1,
        "source": "approved_runtime_fact_source",
        "fact_id": request.fact_id,
        "source_apply_id": request.source_apply_id,
        "goal": request.goal.strip(),
        "next_actions": _dedupe(request.next_actions),
        "acceptance": _dedupe(request.acceptance),
        "constraints": _dedupe(request.constraints),
        "latest_tests": _dedupe(request.latest_tests),
        "run_status": {
            "status": "approved_manual_completion",
            "backend": "manual",
            "response_present": False,
        },
    }


# LLM: _ExplicitSections carries conservative parser output from the user's prompt.
# 类用途: 保存用户 prompt 中明确标注的验收、约束和测试条目；没有明确标注则保持空。
@dataclass(frozen=True)
class _ExplicitSections:
    acceptance: list[str]
    constraints: list[str]
    tests: list[str]


# LLM: _explicit_sections parses only labeled sections and inline labels, never free-form assistant prose.
# 函数用途: 从用户原始 prompt 中提取“验收/约束/测试”显式条目，避免把普通描述当事实。
def _explicit_sections(text: str) -> _ExplicitSections:
    lines = text.splitlines()
    buckets = {"acceptance": [], "constraints": [], "tests": []}
    active = ""
    for line in lines:
        label, inline = _section_heading(line)
        if label:
            active = label
            buckets[label].extend(_inline_items(inline))
            continue
        if _looks_like_unmatched_heading(line):
            active = ""
            continue
        if active:
            buckets[active].extend(_line_items(line))
    return _ExplicitSections(
        acceptance=_dedupe(buckets["acceptance"]),
        constraints=_dedupe(buckets["constraints"]),
        tests=_dedupe(buckets["tests"]),
    )


# LLM: _section_heading recognizes explicit Chinese/English section labels with optional inline content.
# 函数用途: 判断一行是否是验收、约束或测试标题，并返回标题后的内联条目。
def _section_heading(line: str) -> tuple[str, str]:
    text = line.strip().lstrip("-*# ").strip()
    match = re.match(r"^(验收条件|验收|acceptance|constraints?|约束|限制|tests?|测试|最近测试)\s*[:：]\s*(.*)$", text, re.I)
    if not match:
        return "", ""
    return _label_key(match.group(1)), match.group(2).strip()


# LLM: _looks_like_unmatched_heading prevents unrelated labeled sections from leaking into active fact buckets.
# 函数用途: 识别未知标题或标签行，一旦出现就停止继续收集上一段验收/约束/测试事实。
def _looks_like_unmatched_heading(line: str) -> bool:
    text = line.strip().lstrip("-*# ").strip()
    if not text:
        return False
    if re.match(r"^[^:：]{1,40}\s*[:：]\s*$", text):
        return True
    return bool(re.match(r"^[^:：]{1,40}\s*[:：]\s+.+$", text))


# LLM: _label_key maps human labels to the three compact work-state fields.
# 函数用途: 将中英文标题归一化为 acceptance、constraints 或 tests。
def _label_key(label: str) -> str:
    lower = label.lower()
    if lower in {"acceptance", "验收条件", "验收"}:
        return "acceptance"
    if lower in {"constraint", "constraints", "约束", "限制"}:
        return "constraints"
    return "tests"


# LLM: _line_items accepts only bullets/checklists inside an explicit section.
# 函数用途: 从已进入标题范围的行提取条目；遇到普通段落则不扩写成事实。
def _line_items(line: str) -> list[str]:
    text = line.strip()
    for prefix in ("- [x]", "- [X]", "- [ ]", "- ", "* "):
        if text.startswith(prefix):
            item = text[len(prefix) :].strip()
            return [item] if item else []
    return []


# LLM: _inline_items splits explicit inline label content into short fact items.
# 函数用途: 支持“验收: A；B”这类紧凑写法，不处理空内容。
def _inline_items(text: str) -> list[str]:
    if not text:
        return []
    return [item.strip() for item in re.split(r"[;；]", text) if item.strip()]


# LLM: _tool_test_items records actual test-like tool commands as latest_tests facts.
# 函数用途: 从工具归档中提取 pytest/ruff/unittest 等测试命令；普通工具调用不会写成测试状态。
def _tool_test_items(tool_calls: list[Any]) -> list[str]:
    return [item for call in tool_calls if (item := _tool_test_item(call))]


# LLM: _tool_test_item keeps latest_tests tied to explicit test commands rather than model claims.
# 函数用途: 读取单个工具调用记录，命中测试命令时返回一行可审计测试状态。
def _tool_test_item(call: Any) -> str:
    text = json.dumps(call, ensure_ascii=False, sort_keys=True) if isinstance(call, dict) else str(call)
    if not _looks_like_test_command(text):
        return ""
    return text[:240]


# LLM: _looks_like_test_command uses conservative keyword matching for known test/lint commands.
# 函数用途: 判断工具文本是否包含真实测试命令，避免所有工具调用都变成 latest_tests。
def _looks_like_test_command(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in ("pytest", "unittest", "ruff check", "npm test", "cargo test"))


# LLM: _safe_id keeps runtime fact directories filesystem-safe without changing request identity meaning.
# 函数用途: 将 request_id 转成目录名，避免路径分隔符或空白影响落盘。
def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "run"


# LLM: _dedupe preserves prompt order while removing duplicate explicit fact items.
# 函数用途: 对验收、约束、测试条目去重，保持用户原始顺序。
def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


__all__ = [
    "ApprovedRuntimeFactSourceRequest",
    "RuntimeFactSourceRequest",
    "write_approved_runtime_fact_source",
    "write_runtime_fact_source",
]
