# LLM: 只用公开合成资料验证 A 包 0.4.0 的可选结构化字段（台词、逐字引用、道具状态）和检查器身份；不读保留集或真实任务产物。
# 模块用途: 证明这些字段写了才查、查的是作者自己的结构化声明，逐字只按子串比，提醒不改变结构结论。

from __future__ import annotations

import hashlib
import json

import pytest

from agent_py_agent.tests.test_capability_package_drama_text_basis import PACKAGE, _check
from agent_py_agent.tests.test_capability_package_drama_text_basis import (
    _delivery as _legacy_delivery,
)


# LLM: 在共用的合成资料上补回公开示例里的 0.4.0 可选字段，其它字段沿共用夹具。
# 函数用途: 准备带台词、原文引用和道具状态的合成交付。
def _delivery() -> dict:
    delivery = _legacy_delivery()
    example = json.loads((PACKAGE / "resources/example-delivery.json").read_text(encoding="utf-8"))
    delivery["props"] = example["props"]
    extras = {row["id"]: row for row in example["shots"]}
    for shot in delivery["shots"]:
        shot.update({key: extras[shot["id"]][key] for key in ("lines", "source_quotes", "prop_states")})
        # 0.5.3 起“未标改编”规则要求新增台词有 adaptations 兜底；示例本来就带这组字段，一起拷过来，
        # 否则夹具会造出“有台词但没标改编”的交付，被新规则正确拦下（那不是用例想测的场景）。
        if "adaptations" in extras[shot["id"]]:
            shot["adaptations"] = extras[shot["id"]]["adaptations"]
    return delivery


# LLM: 测试只读结构化错误码集合，不根据说明文字判定。
# 函数用途: 取报告里的错误码集合。
def _errors(report: dict) -> set[str]:
    return {item["code"] for item in report["errors"]}


# LLM: 同上，只取 warning 码。
# 函数用途: 取报告里的警告码列表。
def _warnings(report: dict) -> list[str]:
    return [item["code"] for item in report["warnings"]]


# LLM: 按镜头 ID 返回可变的镜头行，测试据此改单个字段。
# 函数用途: 便于按 ID 修改合成资料中的镜头。
def _shot(delivery: dict, identifier: str) -> dict:
    return next(row for row in delivery["shots"] if row["id"] == identifier)


def test_example_with_structured_fields_passes_and_reports_counts(tmp_path):
    report = _check(tmp_path, _delivery())
    assert report["structure_valid"], report["errors"]
    metrics = report["metrics"]
    assert metrics["dialogue_lines"] == 2 and metrics["dialogue_lines_by_speaker"] == {"C01": 1, "C02": 1}
    assert metrics["shots_with_structured_lines"] == 3 and metrics["shots_with_lines"] == 2
    assert metrics["source_quotes"] == 2 and metrics["props"] == 1 and metrics["prop_state_pairs_checked"] == 2
    assert "dialogue_not_structured" not in _warnings(report) and "no_dialogue_lines" not in _warnings(report)


def test_missing_optional_fields_stay_valid_but_make_unchecked_dialogue_visible(tmp_path):
    delivery = _delivery()
    delivery.pop("props")
    for shot in delivery["shots"]:
        for key in ("lines", "source_quotes", "prop_states"):
            shot.pop(key)
    report = _check(tmp_path, delivery)
    assert report["structure_valid"], report["errors"]
    assert "dialogue_not_structured" in _warnings(report)
    assert report["metrics"]["dialogue_lines"] == 0 and report["metrics"]["prop_state_pairs_checked"] == 0


def test_empty_lines_everywhere_warns_no_dialogue_without_failing(tmp_path):
    delivery = _delivery()
    for shot in delivery["shots"]:
        shot["lines"] = []
    report = _check(tmp_path, delivery)
    assert report["structure_valid"] and "no_dialogue_lines" in _warnings(report)


@pytest.mark.parametrize("line,code", [
    ({"speaker_id": "C01", "text": ""}, "line_text_required"),
    ({"speaker_id": "C01"}, "line_text_required"),
    ("C01: 台词", "line_text_required"),
    ({"speaker_id": "C99", "text": "台词"}, "unknown_line_speaker"),
    ({"speaker_id": "C02", "text": "台词"}, "line_speaker_undeclared"),
])
def test_line_structure_and_speaker_declaration_errors(tmp_path, line, code):
    delivery = _delivery()
    _shot(delivery, "SH01")["lines"] = [line]
    report = _check(tmp_path, delivery)
    assert not report["structure_valid"] and code in _errors(report), report["errors"]


def test_narration_line_without_speaker_is_counted_separately(tmp_path):
    delivery = _delivery()
    _shot(delivery, "SH01")["lines"].append({"speaker_id": None, "text": "雨还在下。"})
    report = _check(tmp_path, delivery)
    assert report["structure_valid"] and report["metrics"]["dialogue_lines_by_speaker"]["_narration"] == 1


def test_verbatim_line_must_be_exact_substring_of_its_cited_passage(tmp_path):
    delivery = _delivery()
    line = _shot(delivery, "SH02")["lines"][0]
    line["text"] = line["text"].replace("写给", "寄给")
    report = _check(tmp_path, delivery)
    item = next(row for row in report["errors"] if row["code"] == "quote_not_verbatim")
    assert item["path"] == "SH02.lines[0]" and item["source_id"] == "P02" and item["found_in_source_ids"] == []


def test_verbatim_text_found_elsewhere_points_to_the_right_passage(tmp_path):
    delivery = _delivery()
    shot = _shot(delivery, "SH03")
    shot["source_quotes"] = [{"source_id": "P03", "text": "被雨淋湿的旧书"}]
    report = _check(tmp_path, delivery)
    item = next(row for row in report["errors"] if row["code"] == "quote_not_verbatim")
    assert item["found_in_source_ids"] == ["P01"]


@pytest.mark.parametrize("quote,code", [
    ({"source_id": "P01", "text": "打烊前"}, "quote_source_not_in_shot"),
    ({"source_id": "P99", "text": "店员"}, "quote_source_not_in_shot"),
    ({"source_id": "P02", "text": "书"}, "quote_too_short"),
    ({"source_id": "P02", "text": ""}, "quote_text_required"),
])
def test_source_quote_must_cite_this_shot_and_be_long_enough(tmp_path, quote, code):
    delivery = _delivery()
    _shot(delivery, "SH02")["source_quotes"] = [quote]
    report = _check(tmp_path, delivery)
    assert code in _errors(report), report["errors"]


def test_no_normalization_of_punctuation_or_spacing_in_quotes(tmp_path):
    delivery = _delivery()
    _shot(delivery, "SH01")["source_quotes"] = [{"source_id": "P01", "text": "打烊前,店员发现"}]
    report = _check(tmp_path, delivery)
    assert "quote_not_verbatim" in _errors(report)


def test_adjacent_prop_state_mismatch_is_an_error_unless_a_break_is_declared(tmp_path):
    delivery = _delivery()
    _shot(delivery, "SH02")["prop_states"]["start"] = [{"prop_id": "PR01", "holder_id": "C01", "state": "状态乙"}]
    report = _check(tmp_path, delivery)
    assert not report["structure_valid"]
    item, = [row for row in report["errors"] if row["code"] == "prop_state_discontinuity"]
    assert (item["path"], item["previous_shot"], item["prop_id"]) == ("SH02", "SH01", "PR01")
    assert item["hint"] and len(item["hint"]) <= 200
    _shot(delivery, "SH02")["continuity_break"] = "明确的有意跳接"
    legal_exit = _check(tmp_path, delivery)
    assert legal_exit["structure_valid"] and not [row for row in legal_exit["errors"]
                                                   if row["code"] == "prop_state_discontinuity"]


def test_matching_adjacent_prop_states_pass(tmp_path):
    report = _check(tmp_path, _delivery())
    assert report["structure_valid"]
    assert not [row for row in report["errors"] if row["code"] == "prop_state_discontinuity"]


def test_prop_holder_must_be_declared_in_the_shot_and_offscreen_holder_warns(tmp_path):
    delivery = _delivery()
    shot = _shot(delivery, "SH01")
    shot["prop_states"]["end"] = [{"prop_id": "PR01", "holder_id": "C02", "state": "淋湿"}]
    assert "prop_holder_undeclared" in _errors(_check(tmp_path, delivery))
    shot["offscreen_character_ids"] = ["C02"]
    report = _check(tmp_path, delivery)
    assert "prop_holder_undeclared" not in _errors(report) and "prop_holder_offscreen" in _warnings(report)


@pytest.mark.parametrize("states,code", [
    ({"start": [{"prop_id": "PR09", "holder_id": None, "state": "x"}]}, "invalid_or_duplicate_prop_state"),
    ({"start": [{"prop_id": "PR01", "holder_id": None, "state": "x"},
                {"prop_id": "PR01", "holder_id": None, "state": "y"}]}, "invalid_or_duplicate_prop_state"),
    ({"start": [{"prop_id": "PR01", "holder_id": "C99", "state": "x"}]}, "unknown_reference"),
    ({"start": [{"prop_id": "PR01", "holder_id": None, "state": " "}]}, "prop_state_required"),
    ({"middle": []}, "prop_states_shape"),
    ([], "prop_states_shape"),
])
def test_prop_state_structure_errors(tmp_path, states, code):
    delivery = _delivery()
    _shot(delivery, "SH01")["prop_states"] = states
    assert code in _errors(_check(tmp_path, delivery))


def test_continuity_break_and_props_types_are_checked(tmp_path):
    delivery = _delivery()
    _shot(delivery, "SH02")["continuity_break"] = True
    delivery["props"].append({"id": "PR02", "name": ""})
    codes = _errors(_check(tmp_path, delivery))
    assert {"continuity_break_type", "prop_name_required"} <= codes


def test_report_carries_checker_identity_matching_script_bytes_and_declaration(tmp_path):
    report = _check(tmp_path, _delivery())
    script = (PACKAGE / "scripts/check_delivery.py").read_bytes()
    declaration = json.loads((PACKAGE / "declaration.json").read_text(encoding="utf-8"))
    assert report["checker"] == {"package_id": declaration["plugin_id"], "package_version": declaration["version"],
                                 "script_sha256": hashlib.sha256(script).hexdigest()}
    source = (PACKAGE / "resources/example-source.json").read_bytes()
    assert report["metrics"]["source_sha256_actual"] == hashlib.sha256(source).hexdigest()
