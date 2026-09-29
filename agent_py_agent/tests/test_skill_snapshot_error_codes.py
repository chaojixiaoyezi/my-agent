"""SkillSnapshotError 整族的结构化错误码。

error_code 是唯一的机器可读码，消息保持"码 + 可选明细"的原格式只给人看。产品代码里每个抛出点的第一个参数
必须是码常量或上游异常的结构化码属性（AST 守卫），调用方与后台失败分类都只读 error_code，不解析消息。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshotError
from agent_py_agent.agent.capability.task_references import (
    SkillReferenceError,
    normalize_skill_reference,
    task_skill_references,
)

PRODUCT_ROOT = Path(__file__).resolve().parents[1] / "agent"
CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]+$")
STRUCTURED_CODE_ATTRIBUTES = {"error_code"}
# 唯一的变量码：read_in_package 从上游异常的结构化 code 属性取码，缺省为固定常量。
ALLOWED_CODE_VARIABLES = {("capability/skill_snapshot.py", "code")}
FAMILY = {"SkillSnapshotError", "SkillReferenceError"}


def _called_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else ""


def _code_argument_violation(node: ast.Call, relative: str) -> str:
    if not node.args:
        return "没有错误码参数"
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str) and CODE_PATTERN.fullmatch(first.value):
        return ""
    if isinstance(first, ast.Attribute) and first.attr in STRUCTURED_CODE_ATTRIBUTES:
        return ""
    if isinstance(first, ast.Name) and (relative, first.id) in ALLOWED_CODE_VARIABLES:
        return ""
    return f"第一个参数不是结构化错误码：{ast.unparse(first)}"


def _violations(source: str, relative: str) -> list[str]:
    calls = [node for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.Call) and _called_name(node) in FAMILY]
    checked = ((node, _code_argument_violation(node, relative)) for node in calls)
    return [f"{relative}:{node.lineno} {problem}" for node, problem in checked if problem]


def test_every_product_raise_site_passes_a_structured_code():
    violations: list[str] = []
    sites = 0
    for path in sorted(PRODUCT_ROOT.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if not any(name in source for name in FAMILY):
            continue
        relative = path.relative_to(PRODUCT_ROOT).as_posix()
        sites += sum(source.count(f"{name}(") for name in FAMILY)
        violations.extend(_violations(source, relative))
    assert violations == []
    assert sites > 30  # 守卫确实扫到了整族抛出点，不是空跑


@pytest.mark.parametrize("source", [
    'raise SkillSnapshotError(f"SKILL_NOT_AVAILABLE reference={x}")',
    "raise SkillSnapshotError(str(exc))",
    'raise SkillSnapshotError("skill not available")',
    "raise SkillSnapshotError()",
    "raise SkillReferenceError(message)",
])
def test_guard_rejects_message_shaped_codes(source):
    assert _violations(source, "capability/example.py")


@pytest.mark.parametrize("source", [
    'raise SkillSnapshotError("SKILL_NOT_AVAILABLE", f"reference={x}")',
    "raise SkillSnapshotError(exc.error_code) from exc",
    'raise SkillReferenceError("SKILL_REFERENCE_INVALID")',
])
def test_guard_accepts_structured_codes(source):
    assert _violations(source, "capability/example.py") == []


def test_error_code_is_separate_from_the_human_message():
    error = SkillSnapshotError("SKILL_SNAPSHOT_STALE", "skill=builtin:x", reason="activation_changed_during_read")
    assert (error.error_code, str(error), error.reason) == (
        "SKILL_SNAPSHOT_STALE", "SKILL_SNAPSHOT_STALE skill=builtin:x", "activation_changed_during_read")
    bare = SkillSnapshotError("SKILL_TASK_BINDING_INVALID")
    assert (bare.error_code, str(bare), bare.reason) == ("SKILL_TASK_BINDING_INVALID", "SKILL_TASK_BINDING_INVALID", "")


@pytest.mark.parametrize(("row", "code"), [
    ({}, "SKILL_REFERENCE_INVALID"),
    ({"stable_id": "capability:pkg", "content_sha256": "a" * 64, "kind": "capability_package"},
     "SKILL_PACKAGE_REFERENCE_INVALID"),
])
def test_reference_shape_errors_carry_codes_and_stay_value_errors(row, code):
    with pytest.raises(SkillReferenceError) as raised:
        normalize_skill_reference(row)
    assert raised.value.error_code == code
    assert isinstance(raised.value, ValueError)


def test_wrapped_reference_error_keeps_the_structured_code():
    task = type("Task", (), {"attributes": {"skill_snapshot_refs": [
        {"stable_id": "capability:pkg", "content_sha256": "a" * 64, "kind": "capability_package"},
    ]}, "capability_grants": ()})()
    with pytest.raises(SkillSnapshotError) as raised:
        task_skill_references(task)
    assert raised.value.error_code == "SKILL_PACKAGE_REFERENCE_INVALID"
    assert isinstance(raised.value.__cause__, SkillReferenceError)
