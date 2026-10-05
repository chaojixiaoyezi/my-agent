# LLM: 守卫必须从检查器的结构化 hint-code registry 取码，并以真实交付逐项触发；不完整的样例、遗漏 registry 项或宿主投影丢 hint 都要失败。
#   Unicode 类别 C 全部禁止；动态段落/人物/道具 ID 需过滤控制字符并限长，保证原报告与宿主可见 hint 一致。
# 模块用途: 只用公开合成资料验证检查器每个真实 hint 的成品性，不评价提示文案语义。

from __future__ import annotations

import ast
import hashlib
import json
import re
import runpy
import unicodedata
from itertools import chain

from agent_py_agent.tests.test_capability_package_drama_text_basis import PACKAGE, _invoke
from agent_py_agent.tests.test_capability_package_drama_text_lines import _delivery

PLACEHOLDER = re.compile(r"\{[A-Za-z_]+\}")
MAX_HINT_CHARS = 200
MAX_HINT_FRAGMENT_CHARS = 20


# LLM: 通过 run_path 加载包内单文件检查器，读取运行时 hint registry 和投影函数；不导入、不执行宿主流程。
# 函数用途: 为守卫测试取得检查器的结构化声明与函数。
def _checker() -> dict:
    return runpy.run_path(str(PACKAGE / "scripts/check_delivery.py"))


# LLM: AST allowlist checks the value expression, not its evaluated text, so replacing a registry lookup with an equal constant is detected.
# 函数用途: 判断 hint 字段是否通过唯一允许的模板登记或动态构造器取值。
def _is_allowed_hint_source(value: ast.expr) -> bool:
    registry_lookup = (isinstance(value, ast.Subscript) and isinstance(value.value, ast.Name)
                       and value.value.id == "HINT_CODE_TEMPLATES")
    dynamic_builder = (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                       and value.func.id == "_dynamic_hint")
    return registry_lookup or dynamic_builder


# LLM: 只收集固定 hint 文本常量；动态发射码由 HINT_CODE_TEMPLATES 另行覆盖，不能用此列表代替码清单。
# 函数用途: 读取常量提示，检查静态文案自身也没有占位符或控制字符。
def _hint_constants(checker: dict) -> dict[str, str]:
    return {name: value for name, value in checker.items() if name.endswith("_HINT") and isinstance(value, str)}


# LLM: 同时守住宿主返工提示的 200 字合同和 Unicode 所有 Other 类别；C0/C1、格式/双向、代理、私用及未分配码点均拒绝。
# 函数用途: 断言传出的 hint 是完整、短小、可安全显示的单行文本。
def _assert_finished_hint(where: str, hint: str) -> None:
    assert isinstance(hint, str) and hint.strip(), f"{where}: hint 不能为空"
    leftovers = PLACEHOLDER.findall(hint)
    assert not leftovers, f"{where}: hint 残留未替换的占位符 {leftovers}"
    assert len(hint) <= MAX_HINT_CHARS, f"{where}: hint 长度 {len(hint)} 超过 {MAX_HINT_CHARS}"
    controls = [char for char in hint if unicodedata.category(char).startswith("C")]
    assert not controls, f"{where}: hint 含 Unicode C 类字符 {[hex(ord(c)) for c in controls]}"


# LLM: 仅抽取检查器真实报告与 host_result 的 hint 条目，比较结构化 code/hint，不按自然语言判断覆盖。
# 函数用途: 让测试复核真实产出没有在宿主投影时被静默丢掉。
def _hinted_items(checker: dict, report: dict) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    raw = [(row["code"], row["hint"]) for row in report["errors"] + report["warnings"] if "hint" in row]
    host = checker["host_result"](report)
    projected = [(row["code"], row["hint"]) for row in host["errors"] + host["warnings"] if "hint" in row]
    return raw, projected


# LLM: 测试只读取随包公开合成原文，并复制成可变对象；不接触真实业务产物。
# 函数用途: 为每次检查建立独立原文输入。
def _source() -> dict:
    return json.loads((PACKAGE / "resources/example-source.json").read_text(encoding="utf-8"))


# LLM: 按稳定 ID 选择当前合成交付镜头，修改字段后由检查器实际判定。
# 函数用途: 避免守卫样例依赖数组位置。
def _shot(delivery: dict, identifier: str) -> dict:
    return next(row for row in delivery["shots"] if row["id"] == identifier)


# LLM: 用变更后的合成原文字节重算 source_sha256，再经隔离 subprocess 跑包内原检查器，禁止手工拼装报告。
# 函数用途: 对自造输入执行真实 CLI 并返回报告。
def _check_with_source(tmp_path, delivery: dict, source: dict) -> dict:
    source_bytes = (json.dumps(source, ensure_ascii=False) + "\n").encode()
    delivery["source_sha256"] = hashlib.sha256(source_bytes).hexdigest()
    inputs = {"source.json": source_bytes,
              "delivery.json": (json.dumps(delivery, ensure_ascii=False) + "\n").encode()}
    return _invoke(tmp_path, inputs)


# LLM: 只替换 JSON 结构中与既有 ID 完全相等的字符串值，保持输入间引用一致，不改自由文本里的相似片段。
# 函数用途: 为控制字符和超长 ID 反例构造内部一致的合成资料。
def _replace_exact_value(value, before: str, after: str):
    if isinstance(value, dict):
        return {key: _replace_exact_value(item, before, after) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_exact_value(item, before, after) for item in value]
    return after if value == before else value


# LLM: 同时触发 HINT_CODE_TEMPLATES 声明的错误与警告；故意让来源、道具、人物与对白分别进入各自真实检查分支。
# 函数用途: 构造一份多缺陷但基础结构有效的合成交付，确保全部 hint 发射点真实执行。
def _all_hint_delivery() -> tuple[dict, dict]:
    delivery, source = _delivery(), _source()
    passage = next(row for row in source["passages"] if row["id"] == "P01")
    passage["text"] = "合成叙述句在前，旧书在桌上。他说：“合成引号内的一句话。”"
    first, second, third = (_shot(delivery, key) for key in ("SH01", "SH02", "SH03"))
    first["adaptations"] = [{"text": "原文未出现“旧书”", "original_quote": ""}]
    first["lines"] = [{"speaker_id": None, "text": "合成叙述句在前", "verbatim_source_id": "P01"}]
    first["action"] = "店员拾起旧书。"
    first["prop_states"] = {
        "start": [{"prop_id": "PR06", "holder_id": "C01", "state": "起点"},
                  {"prop_id": "PR07", "holder_id": "C01", "state": "桌面"}],
        "end": [{"prop_id": "PR06", "holder_id": "C01", "state": "终点"},
                {"prop_id": "PR07", "holder_id": "C02", "state": "手持"}],
    }
    second["visible_character_ids"] = ["C01"]
    second["action"] = "赶车人站在门口。"
    second["prop_states"]["start"].append({"prop_id": "PR06", "holder_id": "C02", "state": "门口"})
    second["prop_states"]["end"].append({"prop_id": "PR06", "holder_id": "C02", "state": "门口"})
    third["adaptations"] = []
    third["lines"] = [{"speaker_id": None, "text": "合成守卫专用未标注台词"}]
    delivery["props"].extend([
        {"id": "PR02", "name": "合成来源未知道具", "origin": {"kind": "source", "source_id": "MISSING", "quote": "不存在"}},
        {"id": "PR03", "name": "合成非逐字道具", "origin": {"kind": "source", "source_id": "P01", "quote": "不在原文的引文"}},
        {"id": "PR04", "name": "合成形状错误道具", "origin": {"kind": "other", "note": "合成错误形状"}},
        {"id": "PR05", "name": "旧书", "origin": {"kind": "adaptation", "text": "合成新增说明"}},
        {"id": "PR06", "name": "合成未声明来源道具"},
        {"id": "PR07", "name": "合成镜内交接道具", "origin": {"kind": "adaptation", "text": "合成新增说明"}},
    ])
    return delivery, source


# LLM: 保持字典 AST 遍历为扁平流程，避免来源合同守卫本身增加嵌套复杂度。
# 函数用途: 列出一个字典节点的源码行、键和值。
def _dict_entries(node: ast.Dict) -> list[tuple[int, ast.expr | None, ast.expr]]:
    return [(node.lineno, key, value) for key, value in zip(node.keys, node.values)]


# LLM: 逐个 AST 字典字面量展开条目，再由来源守卫检查每个 hint 值的表达式来源。
# 函数用途: 提取检查器源码中的所有字典条目供 hint 来源守卫逐项验证。
def _all_dict_entries(tree: ast.AST) -> list[tuple[int, ast.expr | None, ast.expr]]:
    dictionaries = (node for node in ast.walk(tree) if isinstance(node, ast.Dict))
    return list(chain.from_iterable(map(_dict_entries, dictionaries)))


# LLM: 检查器源码中的每个 hint 字典值必须显式来自 registry 或动态构造器；不接受值相同的硬编码常量。
# 函数用途: 防止 hint 字典在登记表之外私自维护提示来源。
def test_every_hint_dictionary_uses_registered_source_or_dynamic_builder():
    script = PACKAGE / "scripts/check_delivery.py"
    tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
    unsupported = [
        f"line {line}: {ast.dump(value, include_attributes=False)}"
        for line, key, value in _all_dict_entries(tree)
        if isinstance(key, ast.Constant) and key.value == "hint" and not _is_allowed_hint_source(value)
    ]
    assert not unsupported, "hint 字典必须使用 HINT_CODE_TEMPLATES[...] 或 _dynamic_hint(...): " + "; ".join(unsupported)


def test_every_hint_constant_is_a_finished_string():
    constants = _hint_constants(_checker())
    assert constants, "检查器应暴露 hint 常量"
    for name, hint in sorted(constants.items()):
        _assert_finished_hint(name, hint)


def test_real_trigger_covers_every_registered_hint_code(tmp_path):
    checker = _checker()
    registry = checker.get("HINT_CODE_TEMPLATES")
    assert isinstance(registry, dict) and registry, "检查器必须结构化登记全部 hint 发射码"
    delivery, source = _all_hint_delivery()
    report = _check_with_source(tmp_path, delivery, source)
    raw, projected = _hinted_items(checker, report)
    observed = {code for code, _ in raw}
    expected = set(registry)
    assert expected <= observed, f"未真实触发的 hint 码：{sorted(expected - observed)}"
    assert observed <= expected, f"hint code 未登记：{sorted(observed - expected)}"
    assert sorted(projected) == sorted(raw), "host_result 不得静默丢掉或改写任何 hint"
    for code, hint in raw:
        _assert_finished_hint(code, hint)
    for code, hint in projected:
        _assert_finished_hint(code, hint)


# LLM: 审核动态段落 ID 的真实 report/host hint，逐码检查字符、长度、片段界限与宿主保留。
# 函数用途: 将来源 ID 的恶意字符和过长输入用例统一核验，避免主测试嵌套过深。
def _assert_dynamic_source_case(checker: dict, report: dict, case: dict) -> None:
    raw, projected = _hinted_items(checker, report)
    target_codes = {"adaptation_claim_contradicts_source", "prop_origin_marked_new_but_in_source"}
    by_code = {code: hint for code, hint in raw if code in target_codes}
    assert set(by_code) == target_codes, by_code
    for code, hint in by_code.items():
        _assert_finished_hint(code, hint)
        assert case["expected_fragment"] in hint
        assert case["identifier"] not in hint
        if case["is_long"]:
            assert hint.count("z") == MAX_HINT_FRAGMENT_CHARS - 1, "长段落 ID 必须按片段上限截短"
    assert target_codes <= {code for code, _ in projected}, "短 hint 应保留在宿主投影"


def test_dynamic_source_id_hints_filter_controls_and_bound_length(tmp_path):
    checker = _checker()
    control_id = "P\x01\x7f\x85\u200b\u202e{reference_id}01"
    long_id = "P" + "z" * 500
    cases = [
        {"identifier": control_id, "expected_fragment": "P｛reference_id｝01", "is_long": False},
        {"identifier": long_id, "expected_fragment": "P" + "z" * (MAX_HINT_FRAGMENT_CHARS - 1), "is_long": True},
    ]
    for case in cases:
        delivery, source = _delivery(), _source()
        source["passages"][0]["id"] = case["identifier"]
        delivery = _replace_exact_value(delivery, "P01", case["identifier"])
        _shot(delivery, "SH01")["adaptations"] = [{"text": "原文未出现“旧书”", "original_quote": ""}]
        delivery["props"][0]["origin"] = {"kind": "adaptation", "text": "合成新增说明"}
        report = _check_with_source(tmp_path, delivery, source)
        _assert_dynamic_source_case(checker, report, case)


def test_dynamic_holder_ids_filter_controls_and_bound_length(tmp_path):
    checker = _checker()
    id_limit = MAX_HINT_FRAGMENT_CHARS
    for from_id, to_id in (("C01\x85\u200b\u202e", "C02\x7f\x9f"),
                           ("C01" + "q" * 500, "C02" + "v" * 500)):
        delivery = _delivery()
        delivery = _replace_exact_value(delivery, "C01", from_id)
        delivery = _replace_exact_value(delivery, "C02", to_id)
        first = _shot(delivery, "SH01")
        first["prop_states"]["start"] = [{"prop_id": "PR01", "holder_id": from_id, "state": "合成起点"}]
        first["prop_states"]["end"] = [{"prop_id": "PR01", "holder_id": to_id, "state": "合成终点"}]
        first.pop("prop_handoffs", None)
        report = _check_with_source(tmp_path, delivery, _source())
        raw, projected = _hinted_items(checker, report)
        hint = next(hint for code, hint in raw if code == "intra_shot_handoff_unstated")
        _assert_finished_hint("intra_shot_handoff_unstated", hint)
        expected_from = "C01" + "q" * (id_limit - 3) if len(from_id) > id_limit else "C01"
        assert expected_from in hint
        expected_to = "C02" + "v" * (id_limit - 3) if len(to_id) > id_limit else "C02"
        assert expected_to in hint
        if len(from_id) > id_limit:
            assert hint.count("q") == id_limit - 3 and hint.count("v") == id_limit - 3
        assert from_id not in hint and to_id not in hint
        assert any(code == "intra_shot_handoff_unstated" and item == hint for code, item in projected)


def test_guard_rejects_unicode_controls_and_accepts_json_example_braces():
    for char in ("\x01", "\x85", "\x7f", "\u200b", "\u202e", "\ue000", "\ufdd0", "\ud800"):
        try:
            _assert_finished_hint("坏样本", "合成提示" + char)
        except AssertionError as exc:
            assert "Unicode C 类" in str(exc)
        else:
            raise AssertionError(f"守卫漏掉控制字符 U+{ord(char):04X}")
    _assert_finished_hint("合法 JSON 示例", '格式示例 {"id": "P01"}')


def test_guard_catches_an_unreplaced_placeholder_in_a_hint_constant():
    constants = _hint_constants(_checker())
    name = sorted(constants)[0]
    broken = constants[name] + " 请填 {reference_id}"
    try:
        _assert_finished_hint(f"{name}(坏样本)", broken)
    except AssertionError as exc:
        assert "{reference_id}" in str(exc)
    else:
        raise AssertionError("守卫没能拦下未替换的占位符")
