# LLM: 只用公开合成项目验证 B 包 0.3.3 的两条新硬要求（出镜人物缺参考升级为错误；被镜头引用且仍 planned/missing 的
#   参考必须在 handoff 登记）和对应 hint。失败形状按上一轮 B 类重跑（B05-t401/t402、B10-t401/t402）的失败方式自行构造，
#   不拷贝审阅证据里的产物正文。不读保留集或真实任务产物。
# 模块用途: 证明 0.3.3 的接受集合变化按结构化字段判定、正反例齐全，并钉住所有带 hint 的错误码不漏出 {占位符}。

from __future__ import annotations

import copy
import runpy

import pytest

from agent_py_agent.tests.test_capability_package_drama_workflow_handoff import SCRIPT
from agent_py_agent.tests.test_capability_package_drama_workflow_v03 import (
    BASELINE,
    _beat,
    _found,
    _handoff,
    _project,
    _row,
    _run,
)


# 函数用途: 每例重新加载正式脚本，只读取纯函数，不执行 CLI 或启动真实业务。
@pytest.fixture
def checker():
    return runpy.run_path(str(SCRIPT))


# LLM: hint 是宿主转给模型的唯一一句话（最多 200 字符）；任何残留的 {占位符} 都会把没替换的模板交给模型。
#   遍历 GENERIC_HINTS 的全部码和 error_hint 里按结构化字段现算的全部分支，两个方向都不许出现花括号。
# 函数用途: 钉住所有带 hint 的错误码都不会漏出 {占位符}。
def test_every_hint_code_leaves_no_placeholder_braces(checker):
    hints = checker["GENERIC_HINTS"]
    assert hints, "通用改法表为空时这条守卫失去意义"
    for code in sorted(hints):
        hint = checker["error_hint"](code, f"样例.{code}", {})
        assert hint, code
        assert "{" not in hint and "}" not in hint, (code, hint)
    computed = [
        ("object_id_mismatch", "object_mappings[0].target",
         {"pointer": "/shots/0", "expected_object_id": "SH01", "actual_object_id": "reference_ids"}),
        ("bounded_references_required", "unresolved_differences[0].refs", {"count": 0}),
        ("shot_character_reference_missing", "SH03", {"character_id": "C02"}),
        ("handoff_missing_planned_reference_entry", "REF-C02", {"reference_id": "REF-C02"}),
    ]
    for code, path, row in computed:
        hint = checker["error_hint"](code, path, row)
        assert hint, code
        assert "{" not in hint and "}" not in hint, (code, hint)
    assert "C02" in checker["error_hint"]("shot_character_reference_missing", "SH03", {"character_id": "C02"})
    assert "REF-C02" in checker["error_hint"]("handoff_missing_planned_reference_entry", "REF-C02",
                                              {"reference_id": "REF-C02"})


# 函数用途: 只登记指定 ID 的计划中参考（其余故意漏登记），下标按 references 数组算。
def _planned_additions_for(before: dict, identifiers: set[str]) -> list[dict]:
    return [{"stage_id": "ST1", "target": {"file_id": "F2", "pointer": f"/references/{index}"},
             "reason": f"{row['id']} 尚未制作，登记为计划项"}
            for index, row in enumerate(before["references"])
            if row.get("state") == "planned" and row["id"] in identifiers]


# ---- shot_character_reference_missing 的正反例与纯环境镜头 ----

# 函数用途: 出镜角色没连参考时报错（反例）；把已有参考连回镜头后结构通过（正例）。
def test_missing_shot_character_reference_error_clears_when_reference_is_linked(tmp_path):
    project = _project()
    _row(project, "shots", "SH03")["reference_ids"] = ["REF-L01"]
    broken = _run(tmp_path, {"p.json": project}, "--project", "p.json")
    assert not broken["structure_valid"]
    assert [(item["path"], item["character_id"])
            for item in _found(broken, "errors", "shot_character_reference_missing")] == [("SH03", "C02")]
    _row(project, "shots", "SH03")["reference_ids"] = ["REF-C02", "REF-L01"]
    fixed = _run(tmp_path, {"p.json": project}, "--project", "p.json")
    assert fixed["structure_valid"], fixed["errors"]
    assert not _found(fixed, "errors", "shot_character_reference_missing")


# 函数用途: 纯环境镜头（引用的节拍没有角色字段）不要求人物参考，不报新错误。
def test_pure_environment_shot_is_not_flagged(tmp_path):
    project = _project()
    _beat(project, "B01").pop("character_ids")
    _row(project, "shots", "SH01")["reference_ids"] = ["REF-L01"]
    report = _run(tmp_path, {"p.json": project}, "--project", "p.json")
    assert not _found(report, "errors", "shot_character_reference_missing")
    assert report["structure_valid"], report["errors"]


# ---- handoff_missing_planned_reference_entry：人物 / 地点 / 道具三类缺登记各一 ----

# 函数用途: 人物参考 REF-C01 被镜头引用却没在交接里登记，只报漏掉的那一条。
def test_planned_character_reference_missing_from_handoff_is_reported(tmp_path):
    before = _project()
    handoff = _handoff(tmp_path, before, _project(), additions=_planned_additions_for(before, {"REF-C02", "REF-L01"}))
    report = _run(tmp_path, {"h.json": handoff}, *BASELINE, "--handoff", "h.json", "--host-json")
    assert [(item["code"], item["location"]) for item in report["errors"]] == [
        ("handoff_missing_planned_reference_entry", "REF-C01")]
    assert "REF-C01" in report["errors"][0]["hint"]


# 函数用途: 地点参考 REF-L01 被镜头引用却没在交接里登记，只报漏掉的那一条。
def test_planned_location_reference_missing_from_handoff_is_reported(tmp_path):
    before = _project()
    handoff = _handoff(tmp_path, before, _project(), additions=_planned_additions_for(before, {"REF-C01", "REF-C02"}))
    report = _run(tmp_path, {"h.json": handoff}, *BASELINE, "--handoff", "h.json", "--host-json")
    assert [(item["code"], item["location"]) for item in report["errors"]] == [
        ("handoff_missing_planned_reference_entry", "REF-L01")]


# LLM: 上一轮 B10-t401 形状：关键道具的缺项记录不完整。示例项目没有道具参考，这里补一条被镜头引用的 planned 道具参考，
#   交接登记了人物和地点却漏了它。
# 函数用途: 道具参考 REF-P01 被镜头引用却没在交接里登记，只报漏掉的那一条。
def test_planned_prop_reference_missing_from_handoff_is_reported(tmp_path):
    before = _project()
    before["references"].append({"id": "REF-P01", "kind": "prop", "subject_id": "P01", "state": "planned"})
    _row(before, "shots", "SH01")["reference_ids"] = ["REF-C01", "REF-P01", "REF-L01"]
    handoff = _handoff(tmp_path, before, copy.deepcopy(before),
                       additions=_planned_additions_for(before, {"REF-C01", "REF-C02", "REF-L01"}))
    report = _run(tmp_path, {"h.json": handoff}, *BASELINE, "--handoff", "h.json", "--host-json")
    assert [(item["code"], item["location"]) for item in report["errors"]] == [
        ("handoff_missing_planned_reference_entry", "REF-P01")]


# ---- 上一轮四例的典型错误形状各一 ----

# LLM: B05-t401 形状：镜头里出现角色 C02 但没连它的参考，同时交接只登记了部分计划中参考——镜头关系与缺项记录都不完整。
# 函数用途: 钉住这两个缺口在同一次检查里都能报出来（出镜缺参考 + 缺登记）。
def test_b05_t401_shape_shot_link_and_handoff_record_are_both_incomplete(tmp_path):
    before = _project()
    after = copy.deepcopy(before)
    _row(after, "shots", "SH03")["reference_ids"] = ["REF-L01"]
    handoff = _handoff(tmp_path, before, after, additions=_planned_additions_for(before, {"REF-C01", "REF-L01"}))
    report = _run(tmp_path, {"h.json": handoff}, *BASELINE, "--handoff", "h.json", "--host-json")
    shot_error = next(item for item in report["errors"] if item["code"] == "shot_character_reference_missing")
    assert shot_error["location"] == "SH03"
    assert "SH03" in shot_error["hint"] and "C02" in shot_error["hint"]
    assert [item["location"] for item in report["errors"]
            if item["code"] == "handoff_missing_planned_reference_entry"] == ["REF-C02"]


# LLM: B10-t402 形状：新参考维持在 planned、镜头与新增角色参考没有关联（参考表里有 REF-C02，镜头却只连了地点）。
# 函数用途: 钉住“参考存在但没连进镜头”同样报出镜缺参考。
def test_b10_t402_shape_planned_reference_exists_but_is_not_linked_to_the_shot(tmp_path):
    before = _project()
    before["references"] = [row for row in before["references"] if row["id"] != "REF-C02"]
    for shot in before["shots"]:
        shot["reference_ids"] = [value for value in shot["reference_ids"] if value != "REF-C02"]
    after = copy.deepcopy(before)
    after["references"].append({"id": "REF-C02", "kind": "character", "subject_id": "C02", "state": "planned"})
    report = _run(tmp_path, {"p.json": after, "b.json": before}, *BASELINE)
    assert {item["code"] for item in report["errors"]} == {"shot_character_reference_missing"}
    assert [(item["path"], item["character_id"])
            for item in _found(report, "errors", "shot_character_reference_missing")] == [("SH03", "C02")]


# LLM: B05-t402 形状的通过样例（上一轮重跑里唯一在镜头关系与缺项记录上合格的一次）：给缺参考的角色和道具各新建一条
#   planned 参考、连进镜头、逐条登记进 additions，并把镜头的对应关系变化写进 object_mappings；结构检查通过。
# 函数用途: 钉住“新建 planned 参考 + 连镜头 + 登记”的完整正确写法。
def test_b05_t402_shape_new_planned_references_linked_and_registered_pass(tmp_path):
    before = _project()
    before["references"] = [row for row in before["references"] if row["id"] != "REF-C02"]
    _row(before, "shots", "SH03")["reference_ids"] = ["REF-L01"]
    after = copy.deepcopy(before)
    after["references"].append({"id": "REF-C02", "kind": "character", "subject_id": "C02", "state": "planned"})
    after["references"].append({"id": "REF-P01", "kind": "prop", "subject_id": "P01", "state": "planned"})
    _row(after, "shots", "SH03")["reference_ids"] = ["REF-C02", "REF-P01", "REF-L01"]
    mapping = {"stage_id": "ST1", "source": {"file_id": "F1", "pointer": "/shots/2"},
               "target": {"file_id": "F2", "pointer": "/shots/2"}, "reason": "给 SH03 连上 C02、P01 的新参考"}
    handoff = _handoff(tmp_path, before, after, object_mappings=[mapping],
                       additions=_planned_additions_for(after, {"REF-C01", "REF-C02", "REF-L01", "REF-P01"}))
    report = _run(tmp_path, {"h.json": handoff}, *BASELINE, "--handoff", "h.json", "--host-json")
    assert report["valid"], report["errors"]


# ---- 0.3.3 初审（ds6 pb33r）补的两条：豁免要有用例守着，已知边界要钉住 ----

# LLM: “没被任何镜头引用的 planned 参考不强制登记”是 check_handoff_reference_coverage 里 identifier in referenced 过滤的
#   豁免，CAPABILITY/workflow/PROVENANCE 三处都写了；删掉这个过滤，交付会被要求登记一堆无关参考，这条用例必须变红。
# 函数用途: 给项目加一条没被任何镜头引用的 planned 道具参考，交接不登记它，断言不报缺登记。
def test_unreferenced_planned_reference_needs_no_handoff_entry(tmp_path):
    before = _project()
    before["references"].append({"id": "REF-P09", "kind": "prop", "subject_id": "P09", "state": "planned"})
    handoff = _handoff(tmp_path, before, copy.deepcopy(before),
                       additions=_planned_additions_for(before, {"REF-C01", "REF-C02", "REF-L01"}))
    report = _run(tmp_path, {"h.json": handoff}, *BASELINE, "--handoff", "h.json", "--host-json")
    assert not [item for item in report["errors"] if item["code"] == "handoff_missing_planned_reference_entry"], \
        report["errors"]


# LLM: 已知边界（3a 10-04 裁定，写进 PROVENANCE 0.3.3 节）：动作节拍只写了空字符串的单数 character_id 时，沿用 0.3.1 起
#   “空字符串表示没写角色”的口径——不报字段名错误，也不要求人物参考。本用例钉住现状，免得以后被无意改掉；
#   若重跑里真出现这种写法、决定收紧，要同时改这条用例和 PROVENANCE。
# 函数用途: 钉住空字符串 character_id 既不报 beat_character_id_singular，也不报 shot_character_reference_missing。
def test_empty_singular_character_id_is_a_known_boundary(tmp_path):
    project = _project()
    beat = _beat(project, "B03")
    beat.pop("character_ids", None)
    beat["character_id"] = ""
    report = _run(tmp_path, {"p.json": project}, "--project", "p.json")
    codes = {item["code"] for item in report["errors"]}
    assert "beat_character_id_singular" not in codes
    assert "shot_character_reference_missing" not in codes
