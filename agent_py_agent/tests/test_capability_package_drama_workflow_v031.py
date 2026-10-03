# LLM: 只用公开合成项目验证 B 包 0.3.1 的动作节拍字段名收紧；对应能力包 v2 块 8 冻结重跑里“单数 character_id”的归因。
#   正例、反例各一：单数升级为错误，纯缺失仍是提醒，空值与对白字段不受影响。不读保留集或真实任务产物。
#   另含一条核对方法守卫：review.md 必须要求“缺什么”先回产物逐项复核、表格与结论一致（防规则被误删）。
# 模块用途: 证明 beat_character_id_singular 只在“缺 character_ids 且写了非空单数 character_id”时触发，其余写法保持 0.3.0 语义。

from __future__ import annotations

from agent_py_agent.tests.test_capability_package_drama_workflow_handoff import PACKAGE
from agent_py_agent.tests.test_capability_package_drama_workflow_v03 import (
    _beat,
    _found,
    _project,
    _run,
)


def test_singular_character_id_on_action_beat_is_an_error(tmp_path):
    project = _project()
    beat = _beat(project, "B01")
    beat.pop("character_ids")
    beat["character_id"] = "C01"
    report = _run(tmp_path, {"p.json": project}, "--project", "p.json")
    assert not report["structure_valid"]
    assert [item["path"] for item in _found(report, "errors", "beat_character_id_singular")] == ["B01.character_id"]
    assert not _found(report, "warnings", "beat_character_missing")


def test_singular_character_id_with_empty_list_is_an_error(tmp_path):
    project = _project()
    beat = _beat(project, "B03")
    beat["character_ids"] = []
    beat["character_id"] = "C02"
    report = _run(tmp_path, {"p.json": project}, "--project", "p.json")
    assert [item["path"] for item in _found(report, "errors", "beat_character_id_singular")] == ["B03.character_id"]


def test_missing_characters_still_only_warn(tmp_path):
    project = _project()
    _beat(project, "B01").pop("character_ids")
    report = _run(tmp_path, {"p.json": project}, "--project", "p.json")
    assert report["structure_valid"]
    assert [item["path"] for item in _found(report, "warnings", "beat_character_missing")] == ["B01"]
    assert not _found(report, "errors", "beat_character_id_singular")


def test_empty_singular_value_does_not_trigger_the_new_error(tmp_path):
    project = _project()
    beat = _beat(project, "B01")
    beat.pop("character_ids")
    beat["character_id"] = ""
    report = _run(tmp_path, {"p.json": project}, "--project", "p.json")
    assert report["structure_valid"]
    assert [item["path"] for item in _found(report, "warnings", "beat_character_missing")] == ["B01"]
    assert not _found(report, "errors", "beat_character_id_singular")


def test_clean_example_has_no_new_error_and_reports_031(tmp_path):
    report = _run(tmp_path, {"p.json": _project()}, "--project", "p.json")
    assert report["structure_valid"]
    assert report["checker"]["package_version"] == "0.3.1"
    assert not _found(report, "errors", "beat_character_id_singular")


# 函数用途: 合法非空列表旁残留一个非空单数时，列表已经写对，不报新错误（只按列表核对角色）。
def test_singular_leftover_alongside_non_empty_list_is_not_the_new_error(tmp_path):
    project = _project()
    beat = _beat(project, "B01")
    beat["character_ids"] = ["C01"]
    beat["character_id"] = "C01"
    report = _run(tmp_path, {"p.json": project}, "--project", "p.json")
    assert report["structure_valid"]
    assert not _found(report, "errors", "beat_character_id_singular")
    assert not _found(report, "warnings", "beat_character_missing")


# 函数用途: 单数值不是字符串时不升级为字段名错误——仍按“没写角色字段”给原来的 beat_character_missing 提醒。
def test_non_string_singular_value_keeps_the_original_warning(tmp_path):
    project = _project()
    beat = _beat(project, "B01")
    beat.pop("character_ids")
    beat["character_id"] = 7
    report = _run(tmp_path, {"p.json": project}, "--project", "p.json")
    assert report["structure_valid"]
    assert [item["path"] for item in _found(report, "warnings", "beat_character_missing")] == ["B01"]
    assert not _found(report, "errors", "beat_character_id_singular")


# 函数用途: 核对方法必须要求“缺什么”先回产物逐项复核、表格与结论一致（防这条规则被误删）。
def test_review_method_requires_missing_items_rechecked_against_the_product():
    text = (PACKAGE / "methods" / "review.md").read_text(encoding="utf-8")
    assert "回产物逐项复核" in text
    assert "表格、清单和结论必须一致" in text
