# LLM: 只用公开合成资料验证 A 包 0.5.0 新增的 7 项确定性检查和 --host-json 宿主核验输出（能力包 v2 块 7）；
#   每项一个正例一个反例，对应 capability-packs-v2-design/attribution.md 的 K/P 类失败。不读保留集或真实任务产物。
# 模块用途: 证明新检查只看作者写成结构化字段的内容（占位、逐字、原文引用、道具、画外点名、秒数下限），
#   并且宿主模式只输出 pack_verifier_result.v1 的结构化字段。

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "examples/capability-packages/drama-text-a"


# LLM: 公开示例的独立副本，含 0.5.0 的对象式改编条目和道具 origin。
# 函数用途: 准备一份能通过检查的合成交付。
def _delivery() -> dict:
    return json.loads((PACKAGE / "resources/example-delivery.json").read_text(encoding="utf-8"))


# LLM: 只在 pytest 临时目录写明确输入，用隔离解释器跑包内同一脚本；extra 是附加命令行参数。
# 函数用途: 跑原检查器并返回（解析后的输出, 退出码）。
def _run(tmp_path: Path, delivery: dict | bytes, extra: tuple[str, ...] = ()) -> tuple[dict, int]:
    source = (PACKAGE / "resources/example-source.json").read_bytes()
    if isinstance(delivery, dict):
        delivery["source_sha256"] = hashlib.sha256(source).hexdigest()
        delivery = (json.dumps(delivery, ensure_ascii=False) + "\n").encode()
    (tmp_path / "source.json").write_bytes(source)
    (tmp_path / "delivery.json").write_bytes(delivery)
    environment = {"PATH": os.defpath}
    if "SYSTEMROOT" in os.environ:
        environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    process = subprocess.run(
        [sys.executable, "-I", "-B", "-X", "utf8", str(PACKAGE / "scripts/check_delivery.py"),
         "--source", "source.json", "--delivery", "delivery.json", *extra], cwd=tmp_path, env=environment,
        capture_output=True, text=True, encoding="utf-8", timeout=15,
    )
    assert not process.stderr, process.stderr
    return json.loads(process.stdout), process.returncode


# 函数用途: 只取错误码集合。
def _errors(report: dict) -> set[str]:
    return {item["code"] for item in report["errors"]}


# 函数用途: 取某一类（errors 或 warnings）里某个码的全部条目，测试据此核对位置。
def _found(report: dict, kind: str, code: str) -> list[dict]:
    return [item for item in report[kind] if item["code"] == code]


# 函数用途: 按 ID 取可变的镜头行。
def _shot(delivery: dict, identifier: str) -> dict:
    return next(row for row in delivery["shots"] if row["id"] == identifier)


NEW_CODES = {"placeholder_text", "embedded_quote_not_verbatim", "embedded_quote_not_in_line",
             "adaptation_original_not_in_source", "prop_states_missing", "prop_origin_unstated",
             "named_character_offscreen", "shot_too_short"}


def test_example_passes_without_any_new_finding(tmp_path):
    report, code = _run(tmp_path, _delivery())
    assert report["structure_valid"] and code == 0, report["errors"]
    assert report["checker"]["package_version"] == "0.5.0"
    assert not NEW_CODES & {item["code"] for item in report["errors"] + report["warnings"]}


# ---- placeholder_text（A10-t4：13 镜起止状态全是字面 "{}"）----

@pytest.mark.parametrize("value", ["{}", "[]", "TODO", "todo: 补动作", "TBD", "……", "——", "<这一镜第一帧能看到的状态>",
                                   "待定", "同上"])
@pytest.mark.parametrize("field", ["start_state", "action", "end_state"])
def test_placeholder_shot_text_is_an_error(tmp_path, field, value):
    delivery = _delivery()
    _shot(delivery, "SH01")[field] = value
    report, _ = _run(tmp_path, delivery)
    assert [item["path"] for item in _found(report, "errors", "placeholder_text")] == [f"SH01.{field}"]


@pytest.mark.parametrize("value", ["Todos los días 店员都会开门。", "门口旧书无人触碰。", "旧书（淋湿）放在门口。"])
def test_real_text_with_punctuation_or_todo_like_words_is_not_a_placeholder(tmp_path, value):
    delivery = _delivery()
    _shot(delivery, "SH01")["start_state"] = value
    report, _ = _run(tmp_path, delivery)
    assert "placeholder_text" not in _errors(report)


def test_placeholder_in_summary_line_and_prop_label_is_an_error(tmp_path):
    delivery = _delivery()
    delivery["scenes"][0]["summary"] = "{}"
    _shot(delivery, "SH01")["lines"][0]["text"] = "<台词或字幕>"
    _shot(delivery, "SH01")["prop_states"]["start"][0]["state"] = "<短标签，例：淋湿>"
    report, _ = _run(tmp_path, delivery)
    assert {item["path"] for item in _found(report, "errors", "placeholder_text")} == {
        "S01.summary", "SH01.lines[0].text", "SH01.prop_states.start[0].state"}


def test_unfilled_template_hint_is_caught_when_copied_into_a_delivery(tmp_path):
    template = json.loads((PACKAGE / "templates/delivery.json").read_text(encoding="utf-8"))
    delivery = _delivery()
    _shot(delivery, "SH02")["end_state"] = template["shots"][0]["end_state"]
    report, _ = _run(tmp_path, delivery)
    assert [item["path"] for item in _found(report, "errors", "placeholder_text")] == ["SH02.end_state"]


# ---- embedded_quote_not_verbatim（A05-t4、A08-t6：台词内嵌引文没被核对）----

def test_declared_embedded_quote_that_is_verbatim_passes(tmp_path):
    delivery = _delivery()
    line = _shot(delivery, "SH01")["lines"][0]
    line["text"] = "谁把被雨淋湿的旧书落在门口了？"
    line["embedded_quotes"] = [{"source_id": "P01", "text": "被雨淋湿的旧书"}]
    report, _ = _run(tmp_path, delivery)
    assert report["structure_valid"], report["errors"]


@pytest.mark.parametrize("case", [
    ("谁把被雨淋坏的旧书落在门口了？", "被雨淋坏的旧书", "P01", "embedded_quote_not_verbatim"),
    ("谁把书落在门口了？", "被雨淋湿的旧书", "P01", "embedded_quote_not_in_line"),
    ("谁把被雨淋湿的旧书落在门口了？", "被雨淋湿的旧书", "P02", "quote_source_not_in_shot"),
])
def test_embedded_quote_must_be_in_the_line_and_verbatim_in_the_cited_passage(tmp_path, case):
    text, quote, source_id, code = case
    delivery = _delivery()
    line = _shot(delivery, "SH01")["lines"][0]
    line["text"] = text
    line["embedded_quotes"] = [{"source_id": source_id, "text": quote}]
    report, _ = _run(tmp_path, delivery)
    assert [item["path"] for item in _found(report, "errors", code)] == ["SH01.lines[0].embedded_quotes[0]"]


# ---- adaptation_original_not_in_source（A05-t4、A10-t5：伪引原文“匆匆”）----

def test_adaptation_original_quote_from_any_passage_passes_and_empty_means_pure_addition(tmp_path):
    delivery = _delivery()
    _shot(delivery, "SH01")["adaptations"] = [{"text": "把装袋提前暗示。", "original_quote": "把书装进防水袋"},
                                               {"text": "补写拾书动作。", "original_quote": ""}]
    report, _ = _run(tmp_path, delivery)
    assert report["structure_valid"], report["errors"]


def test_adaptation_original_quote_not_in_source_is_an_error(tmp_path):
    delivery = _delivery()
    _shot(delivery, "SH02")["adaptations"][0]["original_quote"] = "匆匆"
    report, _ = _run(tmp_path, delivery)
    assert [item["path"] for item in _found(report, "errors", "adaptation_original_not_in_source")] == [
        "SH02.adaptations[0].original_quote"]


@pytest.mark.parametrize("entry", [{"text": "补写。", "original_quote": 5}, {"original_quote": "被雨淋湿的旧书"}, {"text": " "}])
def test_adaptation_object_needs_text_and_string_quote(tmp_path, entry):
    delivery = _delivery()
    _shot(delivery, "SH01")["adaptations"] = [entry]
    report, _ = _run(tmp_path, delivery)
    assert "SH01.adaptations[0]" in {item["path"] for item in _found(report, "errors", "nonempty_text_required")}


# ---- prop_states_missing（A08-t6：前两镜没有 prop_states）----

def test_prop_named_in_action_without_state_warns_and_declared_state_does_not(tmp_path):
    delivery = _delivery()
    shot = _shot(delivery, "SH02")
    shot["action"] = "赶车人翻开旧书，指出夹在书页中的便条。"
    declared, _ = _run(tmp_path, json.loads(json.dumps(delivery)))
    assert not _found(declared, "warnings", "prop_states_missing")
    shot.pop("prop_states")
    report, _ = _run(tmp_path, delivery)
    assert [(item["path"], item["prop_id"]) for item in _found(report, "warnings", "prop_states_missing")] == [
        ("SH02.prop_states", "PR01")]
    assert report["structure_valid"]


# ---- prop_origin_unstated（A08-t5：电池灯从哪来、谁交给谁没写）----

@pytest.mark.parametrize("change,where", [("held_at_start", "SH01.prop_states.start"),
                                          ("appears_at_end", "SH01.prop_states.end")])
def test_prop_first_seen_already_held_needs_an_origin(tmp_path, change, where):
    delivery = _delivery()
    states = _shot(delivery, "SH01")["prop_states"]
    if change == "held_at_start":
        states["start"][0]["holder_id"] = "C01"
    else:
        states["start"] = []
    delivery["props"][0].pop("origin")
    report, _ = _run(tmp_path, delivery)
    assert [(item["path"], item["prop_id"]) for item in _found(report, "warnings", "prop_origin_unstated")] == [
        (where, "PR01")]
    delivery["props"][0]["origin"] = "店员从柜台下取出。"
    stated, _ = _run(tmp_path, delivery)
    assert not _found(stated, "warnings", "prop_origin_unstated")


def test_prop_first_seen_unheld_needs_no_origin(tmp_path):
    delivery = _delivery()
    delivery["props"][0].pop("origin")
    report, _ = _run(tmp_path, delivery)
    assert not _found(report, "warnings", "prop_origin_unstated")


# ---- named_character_offscreen（A10-t6、A08-t4、A10-t4：动作点名的人物被列为画外）----

def test_character_named_in_action_but_declared_offscreen_warns(tmp_path):
    delivery = _delivery()
    shot = _shot(delivery, "SH01")
    shot["action"] = "店员拾起旧书，赶车人在门外喊了一声。"
    shot["offscreen_character_ids"] = ["C02"]
    report, _ = _run(tmp_path, delivery)
    assert report["structure_valid"], report["errors"]
    assert [(item["path"], item["character_id"]) for item in _found(report, "warnings", "named_character_offscreen")] == [
        ("SH01.action", "C02")]


def test_visible_named_character_does_not_warn_offscreen(tmp_path):
    delivery = _delivery()
    shot = _shot(delivery, "SH01")
    shot["action"] = "店员拾起旧书，赶车人走进门来。"
    shot["visible_character_ids"] = ["C01", "C02"]
    delivery["scenes"][0]["character_ids"] = ["C01", "C02"]
    report, _ = _run(tmp_path, delivery)
    assert not _found(report, "warnings", "named_character_offscreen")


# ---- shot_too_short（A08-t5：1 秒镜头要完成 4 个动作）----

def test_shot_below_the_minimum_seconds_warns_and_minimum_is_configurable(tmp_path):
    report, _ = _run(tmp_path, _delivery())
    assert not _found(report, "warnings", "shot_too_short")
    strict, code = _run(tmp_path, _delivery(), ("--min-shot-seconds", "25"))
    assert code == 0 and strict["structure_valid"]
    assert [item["path"] for item in _found(strict, "warnings", "shot_too_short")] == [
        "SH01.seconds", "SH02.seconds", "SH03.seconds"]
    assert {item["min_seconds"] for item in _found(strict, "warnings", "shot_too_short")} == {25.0}


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "abc"])
def test_invalid_minimum_seconds_is_an_argument_error(tmp_path, value):
    source = (PACKAGE / "resources/example-source.json").read_bytes()
    (tmp_path / "source.json").write_bytes(source)
    (tmp_path / "delivery.json").write_text("{}", encoding="utf-8")
    process = subprocess.run(
        [sys.executable, "-I", "-B", "-X", "utf8", str(PACKAGE / "scripts/check_delivery.py"),
         "--source", "source.json", "--delivery", "delivery.json", "--min-shot-seconds", value],
        cwd=tmp_path, env={"PATH": os.defpath}, capture_output=True, text=True, encoding="utf-8", timeout=15,
    )
    assert process.returncode == 2 and not process.stdout


# ---- --host-json：pack_verifier_result.v1 ----

def test_host_json_outputs_only_structured_v1_fields(tmp_path):
    report, code = _run(tmp_path, _delivery(), ("--host-json",))
    assert code == 0
    assert set(report) == {"schema", "valid", "errors", "warnings", "metrics"}
    assert report["schema"] == "pack_verifier_result.v1" and report["valid"] is True and report["errors"] == []
    assert all(set(item) == {"code", "location"} for item in report["warnings"])
    assert {"code": "shot_adaptations_need_review", "location": "SH01.adaptations"} in report["warnings"]
    assert len(report["metrics"]) <= 16
    assert all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in report["metrics"].values())
    assert report["metrics"]["shots"] == 3 and "scene_shot_seconds" not in report["metrics"]


def test_host_json_invalid_delivery_has_errors_and_still_exits_zero(tmp_path):
    delivery = _delivery()
    _shot(delivery, "SH01")["start_state"] = "{}"
    report, code = _run(tmp_path, delivery, ("--host-json",))
    assert code == 0 and report["valid"] is False
    assert {"code": "placeholder_text", "location": "SH01.start_state"} in report["errors"]
    plain, plain_code = _run(tmp_path, _delivery() | {"shots": delivery["shots"]})
    assert plain_code == 1 and plain["schema"] == "drama_text_check.v3", "不加参数时原报告和退出码不变"


def test_host_json_unreadable_target_is_a_structured_error_without_message(tmp_path):
    report, code = _run(tmp_path, b"{not json", ("--host-json",))
    assert code == 0
    assert report == {"schema": "pack_verifier_result.v1", "valid": False,
                      "errors": [{"code": "target_unreadable", "location": "$"}], "warnings": [], "metrics": {}}
