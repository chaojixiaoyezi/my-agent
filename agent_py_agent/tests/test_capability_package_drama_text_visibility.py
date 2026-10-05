# LLM: 只验证 A0.3.0 公开合成资料的字面与声明合同，沿同一真实 CLI；不得使用旧业务或保留集推导新产品行为。
# 模块用途: 区分人物引用、歧义、字界、完整扫描与未检查，组件结果不证明模型采用或剧情真实性。

from __future__ import annotations

import pytest

from agent_py_agent.tests.test_capability_package_drama_text_basis import _check, _delivery


# LLM: 复用合法公开时长/来源资料，仅替换可读镜头与显式代称；不改磁盘夹具或真实任务文件。
# 函数用途: 准备一处可定位的名字正文，避免其它字段的正常人名影响本例计数。
def _document(text: str, names: tuple[list[str], list[str]] | None = None) -> dict:
    delivery = _delivery()
    for row, terms in zip(delivery["cast"], names or (["小明"], ["小红"]), strict=True):
        row["text_names"] = terms
    for shot in delivery["shots"]:
        for key in ("start_state", "action", "end_state"):
            shot[key] = "空镜。"
        shot["visible_character_ids"] = []
        shot["offscreen_character_ids"] = []
    delivery["shots"][0]["action"] = text
    return delivery


# LLM: 只选名字扫描的两类警告，改编与其他原检查警告仍留在完整报告，不能改变 structure_valid。
# 函数用途: 取得需要核对字面位置与角色候选的诊断条目。
def _name_warnings(report: dict) -> list[dict]:
    return [row for row in report["warnings"]
            if row["code"] in {"named_character_unaccounted", "ambiguous_character_name"}]


def test_unaccounted_unique_name_is_error_with_original_unicode_span(tmp_path):
    text = "🙂小明在镜头描述中出现。"
    report = _check(tmp_path, _document(text))
    assert not report["structure_valid"]
    error, = [row for row in report["errors"] if row["code"] == "named_character_unaccounted"]
    assert (error["path"], error["start"], error["end"]) == ("SH01.action", 1, 3)
    assert error["candidate_character_ids"] == ["C01"] and error["declared_candidate_ids"] == []
    assert error["hint"] and len(error["hint"]) <= 200
    assert text[error["start"]:error["end"]] == "小明"
    assert report["name_diagnostics"]["status"] == "complete"
    assert report["name_diagnostics"]["match_count"] == 1
    assert report["name_diagnostics"]["error_count"] == 1


@pytest.mark.parametrize("text", ["没有小明。", "屏幕写着小明。", "她说：‘小明的包。’"])
def test_mentions_negation_and_quotes_do_not_override_missing_classification(tmp_path, text):
    report = _check(tmp_path, _document(text))
    assert not report["structure_valid"]
    assert len([row for row in report["errors"] if row["code"] == "named_character_unaccounted"]) == 1


@pytest.mark.parametrize("field", ["visible_character_ids", "offscreen_character_ids"])
def test_either_explicit_declaration_accounts_for_unique_literal_name(tmp_path, field):
    delivery = _document("小明先在画外说话，随后入画。")
    delivery["shots"][0][field] = ["C01"]
    report = _check(tmp_path, delivery)
    assert report["structure_valid"]
    assert _name_warnings(report) == []
    assert report["name_diagnostics"]["match_count"] == 1
    # 作者须按整镜选 visible；工具不把这段文字解释成 offscreen 声明真假。


def test_offscreen_may_reference_cast_outside_current_scene(tmp_path):
    delivery = _document("只提到小红的来信。")
    delivery["shots"][0]["offscreen_character_ids"] = ["C02"]
    report = _check(tmp_path, delivery)
    assert report["structure_valid"]
    assert report["name_diagnostics"]["warning_count"] == 0


@pytest.mark.parametrize("field,value,code", [
    ("visible_character_ids", None, "references_required"),
    ("offscreen_character_ids", None, "references_required"),
    ("offscreen_character_ids", ["unknown"], "unknown_reference"),
    ("offscreen_character_ids", [["C01"]], "unknown_reference"),
    ("offscreen_character_ids", ["C01", "C01"], "duplicate_character_reference"),
    ("visible_character_ids", ["C01", "C01"], "duplicate_character_reference"),
    ("visible_character_ids", ["C02"], "unknown_reference"),
])
def test_character_lists_keep_explicit_scope_and_unique_references(tmp_path, field, value, code):
    delivery = _document("小明。")
    delivery["shots"][0][field] = value
    report = _check(tmp_path, delivery)
    assert not report["structure_valid"]
    assert code in {row["code"] for row in report["errors"]}
    assert report["name_diagnostics"]["status"] == "not_checked"
    assert report["name_diagnostics"]["match_count"] is None


def test_missing_offscreen_field_is_not_silently_filled(tmp_path):
    delivery = _document("空镜。")
    del delivery["shots"][0]["offscreen_character_ids"]
    report = _check(tmp_path, delivery)
    assert {"code": "references_required", "path": "SH01.offscreen_character_ids"} in report["errors"]


def test_character_cannot_be_visible_and_offscreen_in_same_whole_shot(tmp_path):
    delivery = _document("小明入画。")
    delivery["shots"][0]["visible_character_ids"] = ["C01"]
    delivery["shots"][0]["offscreen_character_ids"] = ["C01"]
    report = _check(tmp_path, delivery)
    assert {"code": "character_visibility_overlap", "path": "SH01", "character_ids": ["C01"]} in report["errors"]


@pytest.mark.parametrize("names,code", [
    (None, "text_names_required"), ("小明", "text_names_required"),
    (["明"], "invalid_text_name"), (["  "], "invalid_text_name"),
    (["小明 "], "invalid_text_name"), (["Mary\nJane"], "invalid_text_name"),
    (["ab\x7f"], "invalid_text_name"), ([{}], "invalid_text_name"),
    (["小明", "小明"], "duplicate_text_name"), ([], "text_skip_reason_required"),
])
def test_invalid_name_declarations_are_not_silently_skipped(tmp_path, names, code):
    delivery = _document("小明。")
    delivery["cast"][0]["text_names"] = names
    report = _check(tmp_path, delivery)
    assert not report["structure_valid"]
    assert code in {row["code"] for row in report["errors"]}
    assert report["name_diagnostics"]["warning_count"] is None
    assert _name_warnings(report) == []


def test_no_implicit_display_name_or_missing_name_declaration(tmp_path):
    delivery = _document("店员经过。")
    report = _check(tmp_path, delivery)
    assert report["structure_valid"] and report["name_diagnostics"]["match_count"] == 0
    del delivery["cast"][0]["text_names"]
    report = _check(tmp_path, delivery)
    assert {"code": "text_names_required", "path": "C01.text_names"} in report["errors"]


@pytest.mark.parametrize("field,value,code", [
    ("name", "", "character_name_required"),
    ("text_match_skip_reason", None, "text_skip_reason_type"),
    ("text_match_skip_reason", "退出诊断", "conflicting_text_match_declaration"),
])
def test_display_and_skip_declarations_are_consistent(tmp_path, field, value, code):
    delivery = _document("小明。")
    delivery["cast"][0][field] = value
    report = _check(tmp_path, delivery)
    assert code in {row["code"] for row in report["errors"]}


def test_explicit_opt_out_is_a_legal_exit_and_other_unclassified_name_fails(tmp_path):
    delivery = _document("小明和小红。")
    delivery["cast"][0].update(text_names=[], text_match_skip_reason="该姓名本次不参与字面匹配。")
    report = _check(tmp_path, delivery)
    assert not report["structure_valid"]
    assert report["name_diagnostics"]["skipped_character_ids"] == ["C01"]
    assert report["name_diagnostics"]["match_count"] == 1
    item, = [row for row in report["errors"] if row["code"] == "named_character_unaccounted"]
    assert item["candidate_character_ids"] == ["C02"]
    assert {"code": "character_text_match_disabled", "character_ids": ["C01"]} in report["warnings"]

    delivery["cast"][1].update(text_names=[], text_match_skip_reason="该姓名本次不参与字面匹配。")
    exited = _check(tmp_path, delivery)
    assert not [row for row in exited["errors"] if row["code"] == "named_character_unaccounted"]


def test_all_names_opted_out_is_not_a_zero_match_success(tmp_path):
    delivery = _document("小明和小红。")
    for row in delivery["cast"]:
        row.update(text_names=[], text_match_skip_reason="本次明确不做名字匹配。")
    report = _check(tmp_path, delivery)
    assert report["structure_valid"]
    diagnostics = report["name_diagnostics"]
    assert diagnostics["status"] == "not_checked" and diagnostics["reason"] == "no_matchable_names"
    assert diagnostics["match_count"] is diagnostics["warning_count"] is None


@pytest.mark.parametrize("reverse", [False, True])
def test_shared_name_is_ambiguous_regardless_of_cast_order(tmp_path, reverse):
    delivery = _document("小明到场。", (["小明"], ["小明"]))
    delivery["shots"][0]["visible_character_ids"] = ["C01"]
    if reverse:
        delivery["cast"].reverse()
    report = _check(tmp_path, delivery)
    warning, = _name_warnings(report)
    assert warning["code"] == "ambiguous_character_name"
    assert warning["candidate_character_ids"] == ["C01", "C02"]
    assert warning["declared_candidate_ids"] == ["C01"]
    assert report["name_diagnostics"]["match_count"] == 1


def test_only_valid_longer_name_suppresses_a_fully_contained_short_name(tmp_path):
    report = _check(tmp_path, _document("甲乙丙，甲乙。", (["甲乙"], ["甲乙丙"])))
    assert {(row["text_name_preview"], row["start"], row["end"])
            for row in report["errors"] if row["code"] == "named_character_unaccounted"} == {
        ("甲乙丙", 0, 3), ("甲乙", 4, 6),
    }


def test_invalid_long_name_boundary_cannot_hide_valid_short_name(tmp_path):
    report = _check(tmp_path, _document("甲乙AB", (["甲乙"], ["甲乙A"])))
    error, = [row for row in report["errors"] if row["code"] == "named_character_unaccounted"]
    assert error["text_name_preview"] == "甲乙" and error["candidate_character_ids"] == ["C01"]


def test_partially_overlapping_unclassified_names_are_both_errors(tmp_path):
    report = _check(tmp_path, _document("甲乙丙", (["甲乙"], ["乙丙"])))
    assert {row["text_name_preview"] for row in report["errors"]
            if row["code"] == "named_character_unaccounted"} == {"甲乙", "乙丙"}


@pytest.mark.parametrize("text,count", [
    ("Tom", 1), ("Tom_1", 0), ("Tom-1", 0), ("Tom2", 0),
    ("xTom", 0), ("_Tom", 0), ("-Tom", 0), ("🙂Tom，", 1), ("中文Tom中文", 1),
])
def test_explicit_ascii_boundary_handles_words_identifiers_and_hyphens(tmp_path, text, count):
    report = _check(tmp_path, _document(text, (["Tom"], ["无人名"])))
    assert report["name_diagnostics"]["match_count"] == count


@pytest.mark.parametrize("names,text,count", [
    (["Mary Jane"], "Mary  Jane", 0), (["Mary Jane"], "mary jane", 0),
    (["Mary Jane"], "Mary Jane", 1), (["Café"], "Cafe\u0301", 0),
    (["Mary Jane", "mary jane"], "mary jane", 1), (["A.B"], "A.B", 1),
])
def test_names_keep_exact_case_spaces_unicode_and_literal_punctuation(tmp_path, names, text, count):
    report = _check(tmp_path, _document(text, (names, ["无人名"])))
    assert report["name_diagnostics"]["match_count"] == count


def test_scan_does_not_join_fields_or_use_source_summary_or_notes(tmp_path):
    delivery = _document("小")
    delivery["shots"][0]["end_state"] = "明"
    delivery["shots"][0]["adaptations"] = ["小明只是说明中提及。"]
    delivery["shots"][0]["unresolved"] = ["小红。"]
    delivery["scenes"][0]["summary"] = "小明。"
    delivery["brief"]["premise"] = "小明。"
    report = _check(tmp_path, delivery)
    assert report["name_diagnostics"]["checked_field_count"] == 9
    assert report["name_diagnostics"]["match_count"] == 0


def test_budget_skip_preserves_other_checks_and_nulls_match_counts(tmp_path):
    delivery = _document("甲" * 1000, (["甲" * 2500], ["小红"]))
    report = _check(tmp_path, delivery)
    assert report["structure_valid"] and report["metrics"]["shot_seconds"] == 60.0
    diagnostics = report["name_diagnostics"]
    assert diagnostics["reason"] == "scan_work_budget_exceeded"
    assert diagnostics["estimated_scan_work"] > diagnostics["scan_work_limit"]
    assert diagnostics["match_count"] is diagnostics["omitted_warning_count"] is None
    assert _name_warnings(report) == []


def test_error_output_cap_does_not_abort_scan_or_hide_exact_omitted_count(tmp_path):
    report = _check(tmp_path, _document("小明 " * 137))
    diagnostics = report["name_diagnostics"]
    assert diagnostics["status"] == "complete"
    assert diagnostics["match_count"] == diagnostics["error_count"] == 137
    emitted = [row for row in report["errors"] if row["code"] == "named_character_unaccounted"]
    assert diagnostics["emitted_error_count"] == len(emitted) == 100
    assert diagnostics["omitted_error_count"] == 37


def test_name_scan_cap_preserves_original_spans_for_unaccounted_name_errors(tmp_path):
    report = _check(tmp_path, _document("甲" * 4000, (["甲" * 400], ["小红"])))
    diagnostics = report["name_diagnostics"]
    assert diagnostics["status"] == "complete" and diagnostics["match_count"] == 3601
    error = next(row for row in report["errors"] if row["code"] == "named_character_unaccounted")
    assert error["text_name_length"] == error["end"] - error["start"] == 400
    assert len(error["text_name_preview"]) == 80
