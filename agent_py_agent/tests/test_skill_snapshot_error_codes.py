"""SkillSnapshotError 整族的结构化错误码。

error_code 是唯一的机器可读码，消息保持"码 + 可选明细"的原格式只给人看。产品代码（整个 agent_py_agent，排除 tests）
里每个抛出点的码参数（第一个位置参数或 error_code= 关键字）必须是码常量或上游异常的结构化码属性；别名导入和子类
并入族名一起扫。运行时构造再按同一形状规则核一次，不合格回落到固定码。调用方与后台失败分类都只读 error_code。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from agent_py_agent.agent.capability.skill_snapshot import (
    SKILL_SNAPSHOT_ERROR_CODE_INVALID,
    SkillSnapshotError,
)
from agent_py_agent.agent.capability.task_references import (
    SkillReferenceError,
    normalize_skill_reference,
    task_skill_references,
)
from agent_py_agent.agent.runtime_errors import is_structured_error_code

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
BASE_FAMILY = frozenset({"SkillSnapshotError", "SkillReferenceError"})
STRUCTURED_CODE_ATTRIBUTES = {"error_code"}
# 唯一的变量码：read_in_package 从上游异常的结构化 code 属性取码，缺省为固定常量。收窄到（文件，函数，变量）。
ALLOWED_CODE_VARIABLES = {("agent/capability/skill_snapshot.py", "read_in_package", "code")}


def _product_sources() -> list[tuple[str, ast.Module]]:
    sources = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        if not (relative.startswith("tests/") or "/tests/" in relative):
            sources.append((relative, ast.parse(path.read_text(encoding="utf-8"))))
    return sources


def _simple_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    return node.attr if isinstance(node, ast.Attribute) else ""


def _alias_names(tree: ast.Module, names: set[str]) -> set[str]:
    imports = (node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom)))
    return {alias.asname for node in imports for alias in node.names
            if alias.asname and alias.name.split(".")[-1] in names}


def _subclass_names(tree: ast.Module, names: set[str]) -> set[str]:
    return {node.name for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and any(_simple_name(base) in names for base in node.bases)}


def _extensions(tree: ast.Module, names: set[str]) -> set[str]:
    return _alias_names(tree, names) | _subclass_names(tree, names)


def _family(trees: list[ast.Module]) -> set[str]:
    """族名 = 两个基类 + 产品代码里对它们的别名导入与子类，迭代到不再增加。"""
    names = set(BASE_FAMILY)
    while True:
        grown = names.union(*(_extensions(tree, names) for tree in trees))
        if grown == names:
            return names
        names = grown


def _scoped_calls(tree: ast.Module, family: set[str]) -> list[tuple[str, ast.Call]]:
    scope: dict[int, str] = {}
    for function in (node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
        scope.update((id(node), function.name) for node in ast.walk(function) if isinstance(node, ast.Call))
    return [(scope.get(id(node), ""), node) for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _simple_name(node.func) in family]


def _code_argument(node: ast.Call) -> ast.AST | None:
    if node.args:
        return node.args[0]
    return next((keyword.value for keyword in node.keywords if keyword.arg == "error_code"), None)


def _code_argument_violation(node: ast.Call, where: tuple[str, str]) -> str:
    code = _code_argument(node)
    if code is None:
        return "没有错误码参数"
    if isinstance(code, ast.Constant) and is_structured_error_code(code.value):
        return ""
    if isinstance(code, ast.Attribute) and code.attr in STRUCTURED_CODE_ATTRIBUTES:
        return ""
    if isinstance(code, ast.Name) and (*where, code.id) in ALLOWED_CODE_VARIABLES:
        return ""
    return f"码参数不是结构化错误码：{ast.unparse(code)}"


def _violations(trees: list[tuple[str, ast.Module]]) -> tuple[list[str], int]:
    family = _family([tree for _relative, tree in trees])
    calls = [(relative, function, node) for relative, tree in trees for function, node in _scoped_calls(tree, family)]
    checked = ((relative, node, _code_argument_violation(node, (relative, function))) for relative, function, node in calls)
    return [f"{relative}:{node.lineno} {problem}" for relative, node, problem in checked if problem], len(calls)


def _sample(source: str, relative: str = "agent/capability/example.py") -> list[str]:
    return _violations([(relative, ast.parse(source))])[0]


def test_every_product_raise_site_passes_a_structured_code():
    violations, sites = _violations(_product_sources())
    assert violations == []
    assert sites > 30  # 守卫确实扫到了整族抛出点，不是空跑


@pytest.mark.parametrize("source", [
    'raise SkillSnapshotError(f"SKILL_NOT_AVAILABLE reference={x}")',
    "raise SkillSnapshotError(str(exc))",
    'raise SkillSnapshotError("skill not available")',
    "raise SkillSnapshotError()",
    "raise SkillReferenceError(message)",
    'raise SkillSnapshotError(error_code=f"X {y}")',
    "from agent.capability.skill_snapshot import SkillSnapshotError as SnapErr\nraise SnapErr(str(exc))",
    "class PackageSnapshotError(SkillSnapshotError):\n    pass\nraise PackageSnapshotError(message)",
    "def other():\n    raise SkillSnapshotError(code)",
])
def test_guard_rejects_message_shaped_codes_aliases_and_subclasses(source):
    assert _sample(source)


@pytest.mark.parametrize("source", [
    'raise SkillSnapshotError("SKILL_NOT_AVAILABLE", f"reference={x}")',
    "raise SkillSnapshotError(exc.error_code) from exc",
    'raise SkillReferenceError("SKILL_REFERENCE_INVALID")',
    'raise SkillSnapshotError(error_code="SKILL_SNAPSHOT_STALE")',
])
def test_guard_accepts_structured_codes(source):
    assert _sample(source) == []


def test_allowed_variable_is_scoped_to_its_file_and_function():
    source = "def read_in_package():\n    raise SkillSnapshotError(code)"
    assert _sample(source, "agent/capability/skill_snapshot.py") == []
    assert _sample(source, "agent/capability/other.py")
    other_function = "def read_body():\n    raise SkillSnapshotError(code)"
    assert _sample(other_function, "agent/capability/skill_snapshot.py")


@pytest.mark.parametrize("bad", ["skill not available", "lower_case", " ", "", "SKILL STALE", "1SKILL"])
def test_runtime_rejects_malformed_codes_to_a_fixed_code(bad):
    error = SkillSnapshotError(bad, "package=p")
    assert error.error_code == SKILL_SNAPSHOT_ERROR_CODE_INVALID
    assert str(error).startswith(SKILL_SNAPSHOT_ERROR_CODE_INVALID) and str(error).endswith("package=p")


def test_skill_search_get_failure_carries_the_structured_code(tmp_path, skill_catalog_factory):
    from agent_py_agent.tests.test_capability_package_discovery import discovery_fixture

    catalog, snapshot, _agent, tool = discovery_fixture(tmp_path, skill_catalog_factory, [])
    entry = snapshot.resolve("story-content")
    (catalog.home.owner_home_dir / "skills" / "story-content" / "SKILL.md").write_text("changed", encoding="utf-8")
    outcome = tool.execute({"action": "get", "skill_id": entry.stable_id})
    payload = json.loads(outcome.output)
    assert (outcome.ok, outcome.error_code) == (False, "SKILL_SNAPSHOT_UNAVAILABLE")
    assert payload["details"] == {"error_code": "SKILL_SNAPSHOT_STALE"} and payload["skill_id"] == entry.stable_id


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
