# LLM: 只用公开合成资料验证 A 包 0.5.3 的来源与改编标注规则（道具来源标错、未标改编、说明与原文矛盾）；
#   不读保留集或真实任务产物，也不抄任何用例原文。夹具从共用合成交付出发，只改结构字段。
# 模块用途: 证明三条新规则各自的报错条件、合法写法与合理豁免，以及每条错误都带可照做的 hint。

from __future__ import annotations

import json

from agent_py_agent.tests.test_capability_package_drama_text_basis import PACKAGE, _check
from agent_py_agent.tests.test_capability_package_drama_text_lines import _delivery


# 函数用途: 取报告里的错误码集合。
def _errors(report: dict) -> set[str]:
    return {item["code"] for item in report["errors"]}


# 函数用途: 取某个错误码的完整条目，便于核对 hint 和结构化字段。
def _entry(report: dict, code: str) -> dict:
    return next(item for item in report["errors"] if item["code"] == code)


# 函数用途: 按 ID 取可变的镜头行。
def _shot(delivery: dict, identifier: str) -> dict:
    return next(row for row in delivery["shots"] if row["id"] == identifier)


# 函数用途: 取可变的道具行。
def _prop(delivery: dict, identifier: str) -> dict:
    return next(row for row in delivery["props"] if row["id"] == identifier)


# ---- 规则 1：道具来源标成新增，但道具名逐字出现在原文 ----


def test_prop_marked_new_while_name_is_verbatim_in_source_is_an_error(tmp_path):
    delivery = _delivery()
    name = _prop(delivery, "PR01")["name"]
    origin = json.loads((PACKAGE / "resources/example-source.json").read_text(encoding="utf-8"))
    text = next(row["text"] for row in origin["passages"] if name in row["text"])
    _prop(delivery, "PR01")["origin"] = {"kind": "adaptation", "text": "本次新增的道具"}
    report = _check(tmp_path, delivery)
    assert "prop_origin_marked_new_but_in_source" in _errors(report)
    entry = _entry(report, "prop_origin_marked_new_but_in_source")
    assert entry["path"] == "PR01.origin"
    assert entry["name"] == name
    assert entry["found_in_source_ids"], "要给出道具名出现的段落 ID"
    assert any(text in row["text"] for row in origin["passages"] if row["id"] in entry["found_in_source_ids"])
    assert name in entry["hint"] or entry["hint"]
    assert len(entry["hint"]) <= 200


def test_prop_marked_new_with_a_name_absent_from_source_passes(tmp_path):
    delivery = _delivery()
    _prop(delivery, "PR01")["origin"] = {"kind": "adaptation", "text": "本次新增的道具"}
    _prop(delivery, "PR01")["name"] = "合成测试专用名A"
    assert _check(tmp_path, delivery)["structure_valid"], _errors(_check(tmp_path, delivery))


def test_prop_name_sharing_one_char_with_source_but_not_present_whole_passes(tmp_path):
    """盲区用例：名字里含原文出现过的单字，但整名不在原文里——不能报（规则只做整名逐字子串）。"""
    delivery = _delivery()
    prop = _prop(delivery, "PR01")
    source = json.loads((PACKAGE / "resources/example-source.json").read_text(encoding="utf-8"))
    full = prop["name"]
    assert len(full) >= 2
    single = full[0]
    assert any(single in row["text"] for row in source["passages"]), "前提：这个单字确实在原文里"
    prop["origin"] = {"kind": "adaptation", "text": "本次新增"}
    prop["name"] = single + "合成测试余下部分"
    report = _check(tmp_path, delivery)
    assert "prop_origin_marked_new_but_in_source" not in _errors(report), report["errors"]


def test_prop_origin_hint_carries_the_matching_passage_ids(tmp_path):
    """盲区用例：报错 hint 必须写明这个名字出现在哪个段落，不能只报个通用错误码。"""
    delivery = _delivery()
    prop = _prop(delivery, "PR01")
    source = json.loads((PACKAGE / "resources/example-source.json").read_text(encoding="utf-8"))
    ids = [row["id"] for row in source["passages"] if prop["name"] in row["text"]]
    assert ids, "前提：示例道具名确实出现在原文里"
    prop["origin"] = {"kind": "adaptation", "text": "本次新增"}
    report = _check(tmp_path, delivery)
    entry = _entry(report, "prop_origin_marked_new_but_in_source")
    assert entry["found_in_source_ids"] == sorted(ids)
    for identifier in ids:
        assert identifier in entry["hint"], "hint 里必须出现具体段落 ID"


def test_prop_marked_source_is_the_legal_exit_that_also_passes(tmp_path):
    delivery = _delivery()
    origin = _prop(delivery, "PR01")["origin"]
    assert origin["kind"] == "source", "示例本来就按原文来源声明"
    assert _check(tmp_path, delivery)["structure_valid"]


# ---- 规则 2：新增台词/字幕没有改编标注 ----


def test_line_neither_verbatim_nor_adapted_is_reported(tmp_path):
    delivery = _delivery()
    shot = _shot(delivery, "SH01")
    shot["adaptations"] = []
    shot["lines"] = [{"speaker_id": None, "text": "合成测试用的一句全新字幕文字"}]
    report = _check(tmp_path, delivery)
    assert "adaptation_unmarked" in _errors(report)
    entry = _entry(report, "adaptation_unmarked")
    assert entry["path"] == "SH01.lines[0].text"
    assert len(entry["hint"]) <= 200


def test_line_covered_by_adaptations_passes(tmp_path):
    delivery = _delivery()
    shot = _shot(delivery, "SH01")
    shot["adaptations"] = [{"text": "本镜台词为合成改写说明", "original_quote": ""}]
    shot["lines"] = [{"speaker_id": None, "text": "合成测试用的一句全新字幕文字"}]
    assert _check(tmp_path, delivery)["structure_valid"], _errors(_check(tmp_path, delivery))


def test_line_that_is_verbatim_source_text_passes(tmp_path):
    delivery = _delivery()
    shot = _shot(delivery, "SH01")
    shot["adaptations"] = []
    source = json.loads((PACKAGE / "resources/example-source.json").read_text(encoding="utf-8"))
    passage = next(row for row in source["passages"] if row["id"] in shot["source_ids"])
    shot["lines"] = [{"speaker_id": None, "text": passage["text"]}]
    assert _check(tmp_path, delivery)["structure_valid"], _errors(_check(tmp_path, delivery))


def test_line_covered_by_embedded_quote_passes(tmp_path):
    delivery = _delivery()
    shot = _shot(delivery, "SH01")
    shot["adaptations"] = []
    source = json.loads((PACKAGE / "resources/example-source.json").read_text(encoding="utf-8"))
    passage = next(row for row in source["passages"] if row["id"] in shot["source_ids"])
    quote = passage["text"][:6]
    shot["lines"] = [{"speaker_id": None, "text": f"合成前缀{quote}合成后缀",
                      "embedded_quotes": [{"source_id": passage["id"], "text": quote}]}]
    assert _check(tmp_path, delivery)["structure_valid"], _errors(_check(tmp_path, delivery))


def test_short_interjection_and_pure_punctuation_are_whitelisted(tmp_path):
    delivery = _delivery()
    shot = _shot(delivery, "SH01")
    shot["adaptations"] = []
    # 只用极短语气词与纯标点两类；纯标点单独一条也会被既有 placeholder_text 规则拦下，那不属于本规则的范围。
    shot["lines"] = [{"speaker_id": None, "text": "嗯"}, {"speaker_id": None, "text": "啊？"}]
    assert _check(tmp_path, delivery)["structure_valid"], _errors(_check(tmp_path, delivery))


def test_placeholder_line_is_whitelisted_by_the_placeholder_rule(tmp_path):
    delivery = _delivery()
    shot = _shot(delivery, "SH01")
    shot["adaptations"] = []
    shot["lines"] = [{"speaker_id": None, "text": "TODO"}]
    report = _check(tmp_path, delivery)
    assert "adaptation_unmarked" not in _errors(report)


# ---- 规则 3：说明声称“原文未出现”但原文里其实有 ----


def test_adaptation_claiming_absent_while_present_in_source_is_an_error(tmp_path):
    delivery = _delivery()
    source = json.loads((PACKAGE / "resources/example-source.json").read_text(encoding="utf-8"))
    passage = next(row for row in source["passages"] if row["id"] in _shot(delivery, "SH01")["source_ids"])
    quote = passage["text"][:8]
    # 被声称“不存在”的原句必须用引号括起来，规则只取引号内的内容去原文核对。
    _shot(delivery, "SH01")["adaptations"] = [{"text": f"原文未出现“{quote}”这一句，属于本次新增", "original_quote": ""}]
    report = _check(tmp_path, delivery)
    assert "adaptation_claim_contradicts_source" in _errors(report)
    entry = _entry(report, "adaptation_claim_contradicts_source")
    assert entry["found_in_source_ids"]
    assert len(entry["hint"]) <= 200


def test_adaptation_claiming_absent_with_genuinely_absent_text_passes(tmp_path):
    delivery = _delivery()
    _shot(delivery, "SH01")["adaptations"] = [
        {"text": "原文未出现“合成测试专用文案X”这句，属于本次新增", "original_quote": ""}]
    assert _check(tmp_path, delivery)["structure_valid"], _errors(_check(tmp_path, delivery))


def test_claimed_absent_quote_in_corner_brackets_is_also_checked(tmp_path):
    """引号集合要含直角引号：`「」`/`『』` 里的原句同样要回原文核对（ds6 初审指出旧版不认）。"""
    delivery = _delivery()
    source = json.loads((PACKAGE / "resources/example-source.json").read_text(encoding="utf-8"))
    passage = next(row for row in source["passages"] if row["id"] in _shot(delivery, "SH01")["source_ids"])
    quote = passage["text"][:8]
    _shot(delivery, "SH01")["adaptations"] = [
        {"text": f"原文未出现「{quote}」这一句，属于本次新增", "original_quote": ""}]
    report = _check(tmp_path, delivery)
    assert "adaptation_claim_contradicts_source" in _errors(report), report["errors"]


def test_adaptation_without_the_absent_claim_is_unaffected(tmp_path):
    delivery = _delivery()
    assert _check(tmp_path, delivery)["structure_valid"], _errors(_check(tmp_path, delivery))


# ---- 每条新错误都带 hint，且 hint 长度合规 ----


def test_every_new_rule_error_carries_a_bounded_hint(tmp_path):
    delivery = _delivery()
    _prop(delivery, "PR01")["origin"] = {"kind": "adaptation", "text": "合成新增说明"}
    source = json.loads((PACKAGE / "resources/example-source.json").read_text(encoding="utf-8"))
    passage = next(row for row in source["passages"] if row["id"] in _shot(delivery, "SH01")["source_ids"])
    # 规则 2 要求本镜没有 adaptations 才可能触发，规则 3 又要有 adaptations；两条规则分别落在不同镜头上，
    # 否则前者的合法退出会把后者挡掉。
    _shot(delivery, "SH01")["adaptations"] = [{"text": f"原文未出现“{passage['text'][:8]}”这一句", "original_quote": ""}]
    _shot(delivery, "SH02")["adaptations"] = []
    _shot(delivery, "SH02")["lines"] = [{"speaker_id": None, "text": "另一句合成测试专用文案Y"}]
    report = _check(tmp_path, delivery)
    assert {"prop_origin_marked_new_but_in_source", "adaptation_unmarked",
            "adaptation_claim_contradicts_source"} <= _errors(report)
    for code in ("prop_origin_marked_new_but_in_source", "adaptation_unmarked",
                 "adaptation_claim_contradicts_source"):
        hint = _entry(report, code)["hint"]
        assert isinstance(hint, str) and 0 < len(hint) <= 200
