# LLM: 只用公开合成资料验证 A 包 0.5.4 的两条新检查（台词逐字但落在段落引号外、镜内道具交接无结构化记录）；
#   样例形状按 0.5.2 三批业务审阅里的失败形状**自己构造**，不拷贝任何产物正文。不读保留集。
# 模块用途: 证明两条新规则的报错/告警条件、合法退出与低误报边界，并断言新 hint 合格。

from __future__ import annotations

import hashlib
import json
import runpy

from agent_py_agent.tests.test_capability_package_drama_text_basis import PACKAGE, _check, _invoke
from agent_py_agent.tests.test_capability_package_drama_text_lines import _delivery


# 函数用途: 用**自己构造的**原文跑一次检查；basis 的 _check 固定读磁盘上的示例原文，改不动段落，这里直接传字节。
def _check_with_source(tmp_path, delivery: dict, source: dict) -> dict:
    raw = (json.dumps(source, ensure_ascii=False) + "\n").encode()
    delivery["source_sha256"] = hashlib.sha256(raw).hexdigest()
    return _invoke(tmp_path, {"source.json": raw,
                              "delivery.json": (json.dumps(delivery, ensure_ascii=False) + "\n").encode()})


# 函数用途: 取示例原文的可变副本（段落文本会被测试改写）。
def _source() -> dict:
    return json.loads((PACKAGE / "resources/example-source.json").read_text(encoding="utf-8"))


# 函数用途: 取错误码集合。
def _errors(report: dict) -> set[str]:
    return {item["code"] for item in report["errors"]}


# 函数用途: 取警告码集合。
def _warnings(report: dict) -> set[str]:
    return {item["code"] for item in report["warnings"]}


# 函数用途: 按 ID 取可变镜头行。
def _shot(delivery: dict, identifier: str) -> dict:
    return next(row for row in delivery["shots"] if row["id"] == identifier)


# ---- 规则 a：标了 verbatim_source_id，但台词不在段落引号内 ----


def test_verbatim_line_outside_paragraph_quotes_is_an_error(tmp_path):
    """失败形状：把第三人称叙述句当成角色的逐字台词（P02 类段落里没有引号时不会误报，这里造一个带引号的段落）。"""
    delivery = _delivery()
    shot = _shot(delivery, "SH01")
    source = _source()
    passage = next(row for row in source["passages"] if row["id"] in shot["source_ids"])
    passage["text"] = "合成叙述句在前。他说：“合成引号内的一句话。”合成叙述句在后。"
    shot["source_ids"] = [passage["id"]]
    # 台词本身逐字来自该段落（在引号**外**的那半句）；改段落会连带触发别的既有检查，所以只断言目标错误码。
    said = "合成叙述句在前"
    shot["lines"] = [{"speaker_id": None, "text": said, "verbatim_source_id": passage["id"]}]
    report = _check_with_source(tmp_path, delivery, source)
    assert "verbatim_not_quoted" in _errors(report), report["errors"]


def test_verbatim_line_inside_paragraph_quotes_passes(tmp_path):
    delivery = _delivery()
    shot = _shot(delivery, "SH01")
    source = _source()
    passage = next(row for row in source["passages"] if row["id"] in shot["source_ids"])
    said = "合成引号内的一句话"
    passage["text"] = "他说：“" + said + "。”"
    shot["source_ids"] = [passage["id"]]
    shot["lines"] = [{"speaker_id": None, "text": said, "verbatim_source_id": passage["id"]}]
    report = _check_with_source(tmp_path, delivery, source)
    assert "verbatim_not_quoted" not in _errors(report), report["errors"]


# LLM: 该用例仅固定已知边界：段落无引号时规则不触发，不得把通过解释为叙述确实是人物台词。
# 函数用途: 保留 0.5.4 的无引号早退行为，同时明确语义风险仍需人工判断。
def test_known_boundary_unquoted_paragraph_does_not_trigger_verbatim_error(tmp_path):
    """已知边界而非合规样例：无引号散文的 verbatim 标注仍未由本规则验证。"""
    delivery = _delivery()
    shot = _shot(delivery, "SH01")
    source = _source()
    passage = next(row for row in source["passages"] if row["id"] in shot["source_ids"])
    passage["text"] = "合成段落通篇不使用引号的一段连续文字。"
    shot["source_ids"] = [passage["id"]]
    shot["lines"] = [{"speaker_id": None, "text": "合成段落通篇不", "verbatim_source_id": passage["id"]}]
    report = _check_with_source(tmp_path, delivery, source)
    assert "verbatim_not_quoted" not in _errors(report), report["errors"]


# LLM: 原句可在叙述与引号对白中重复出现时，按任意完整引号内匹配确认来源，不依赖第一次出现的位置。
# 函数用途: 防止未引用的先行复述掩盖后续合法引号内台词。
def test_verbatim_uses_later_quoted_occurrence_after_unquoted_text(tmp_path):
    delivery = _delivery()
    shot = _shot(delivery, "SH01")
    source = _source()
    passage = next(row for row in source["passages"] if row["id"] in shot["source_ids"])
    said = "合成灯句"
    passage["text"] += " 旁白先提到" + said + "。后来他说：“" + said + "。”"
    shot["lines"] = [{"speaker_id": None, "text": said, "verbatim_source_id": passage["id"]}]
    report = _check_with_source(tmp_path, delivery, source)
    assert "verbatim_not_quoted" not in _errors(report), report["errors"]


# LLM: 跨过闭引号的台词片段不得借相邻字符伪装成引号内部匹配。
# 函数用途: 锁住引用区间的右边界，避免跨出对白后仍按逐字台词放行。
def test_verbatim_text_crossing_quote_boundary_is_rejected(tmp_path):
    delivery = _delivery()
    shot = _shot(delivery, "SH01")
    source = _source()
    passage = next(row for row in source["passages"] if row["id"] in shot["source_ids"])
    passage["text"] = "他说：「合成句子。」后续旁白"
    shot["lines"] = [{"speaker_id": None, "text": "合成句子。」后续", "verbatim_source_id": passage["id"]}]
    report = _check_with_source(tmp_path, delivery, source)
    assert "verbatim_not_quoted" in _errors(report), report["errors"]


# LLM: QUOTED_SPAN 与 quoted_spans 必须使用同一 quote pair 定义，以免改编引文与逐字台词判断分叉。
# 函数用途: 用半角双引号核对正则提取和区间提取保持一致。
def test_quote_regex_and_span_parser_share_ascii_double_quotes():
    checker = runpy.run_path(str(PACKAGE / "scripts/check_delivery.py"))
    passage = 'He said "first line" and then "synthetic line."'
    regex_quotes = [next(group for group in match.groups() if group)
                    for match in checker["QUOTED_SPAN"].finditer(passage)]
    spans = checker["quoted_spans"](passage)
    span_quotes = [passage[start:end] for start, end in spans]
    assert regex_quotes == ["first line", "synthetic line."]
    assert span_quotes == regex_quotes


def test_verbatim_line_in_corner_brackets_passes(tmp_path):
    """引号集合与 QUOTED_SPAN 一致：直角引号 `「」` 里的台词同样算原话（ds6 初审指出旧版只认弯引号）。"""
    delivery = _delivery()
    shot = _shot(delivery, "SH01")
    source = _source()
    passage = next(row for row in source["passages"] if row["id"] in shot["source_ids"])
    said = "合成直角引号内的一句"
    passage["text"] = "他说：「" + said + "」"
    shot["source_ids"] = [passage["id"]]
    shot["lines"] = [{"speaker_id": None, "text": said, "verbatim_source_id": passage["id"]}]
    report = _check_with_source(tmp_path, delivery, source)
    assert "verbatim_not_quoted" not in _errors(report), report["errors"]


def test_verbatim_line_outside_corner_brackets_is_an_error(tmp_path):
    delivery = _delivery()
    shot = _shot(delivery, "SH01")
    source = _source()
    passage = next(row for row in source["passages"] if row["id"] in shot["source_ids"])
    passage["text"] = "合成叙述在前。他说：「合成直角引号内的文本。」合成叙述在后。"
    shot["source_ids"] = [passage["id"]]
    shot["lines"] = [{"speaker_id": None, "text": "合成叙述在前", "verbatim_source_id": passage["id"]}]
    report = _check_with_source(tmp_path, delivery, source)
    assert "verbatim_not_quoted" in _errors(report), report["errors"]


# ---- 规则 b：镜内道具持有人变化没有结构化交接记录（提醒） ----


def test_intra_shot_holder_change_without_record_warns(tmp_path):
    delivery = _delivery()
    shot = _shot(delivery, "SH01")
    states = shot["prop_states"]
    states["start"] = [{"prop_id": "PR01", "holder_id": "C01", "state": "合成起始状态"}]
    states["end"] = [{"prop_id": "PR01", "holder_id": "C02", "state": "合成结束状态"}]
    shot["prop_states"] = states
    shot.pop("prop_handoffs", None)
    report = _check(tmp_path, delivery)
    assert "intra_shot_handoff_unstated" in _warnings(report), report["warnings"]
    entry = next(item for item in report["warnings"] if item["code"] == "intra_shot_handoff_unstated")
    assert entry["prop_id"] == "PR01" and entry["from_holder_id"] == "C01" and entry["to_holder_id"] == "C02"
    assert len(entry["hint"]) <= 200
    assert "intra_shot_handoff_unstated" not in _errors(report), "这是提醒，不进 errors"


def test_intra_shot_holder_change_with_record_passes(tmp_path):
    delivery = _delivery()
    shot = _shot(delivery, "SH01")
    states = shot["prop_states"]
    states["start"] = [{"prop_id": "PR01", "holder_id": "C01", "state": "合成起始状态"}]
    states["end"] = [{"prop_id": "PR01", "holder_id": "C02", "state": "合成结束状态"}]
    shot["prop_states"] = states
    shot["prop_handoffs"] = [{"prop_id": "PR01", "to_holder_id": "C02", "note": "合成交接说明"}]
    report = _check(tmp_path, delivery)
    assert "intra_shot_handoff_unstated" not in _warnings(report), report["warnings"]


def test_null_to_holder_is_not_an_intra_shot_handoff(tmp_path):
    """低误报边界：一端为 null（拿起/放下）不算镜内交接。"""
    delivery = _delivery()
    shot = _shot(delivery, "SH01")
    states = shot["prop_states"]
    states["start"] = [{"prop_id": "PR01", "holder_id": None, "state": "合成起始状态"}]
    states["end"] = [{"prop_id": "PR01", "holder_id": "C01", "state": "合成结束状态"}]
    shot["prop_states"] = states
    shot.pop("prop_handoffs", None)
    assert "intra_shot_handoff_unstated" not in _warnings(_check(tmp_path, delivery))


def test_same_holder_across_the_shot_is_not_a_handoff(tmp_path):
    delivery = _delivery()
    shot = _shot(delivery, "SH01")
    states = shot["prop_states"]
    states["start"] = [{"prop_id": "PR01", "holder_id": "C01", "state": "合成起始状态"}]
    states["end"] = [{"prop_id": "PR01", "holder_id": "C01", "state": "合成结束状态"}]
    shot["prop_states"] = states
    shot.pop("prop_handoffs", None)
    assert "intra_shot_handoff_unstated" not in _warnings(_check(tmp_path, delivery))


# ---- 两条新规则都带合格 hint ----


def test_new_rule_hints_are_bounded_and_finished(tmp_path):
    delivery = _delivery()
    shot = _shot(delivery, "SH01")
    source = _source()
    passage = next(row for row in source["passages"] if row["id"] in shot["source_ids"])
    passage["text"] = "合成叙述在前。他说：“合成引号内文本。”合成叙述在后。"
    shot["source_ids"] = [passage["id"]]
    shot["lines"] = [{"speaker_id": None, "text": "合成叙述在", "verbatim_source_id": passage["id"]}]
    states = shot["prop_states"]
    states["start"] = [{"prop_id": "PR01", "holder_id": "C01", "state": "合成起始"}]
    states["end"] = [{"prop_id": "PR01", "holder_id": "C02", "state": "合成结束"}]
    shot["prop_states"] = states
    report = _check_with_source(tmp_path, delivery, source)
    hits = [item for item in report["errors"] + report["warnings"]
            if item["code"] in ("verbatim_not_quoted", "intra_shot_handoff_unstated")]
    assert {item["code"] for item in hits} == {"verbatim_not_quoted", "intra_shot_handoff_unstated"}
    for item in hits:
        hint = item.get("hint")
        assert isinstance(hint, str) and 0 < len(hint) <= 200
        assert not any(char in hint for char in "{}")
        assert not any(ord(char) < 32 for char in hint)
