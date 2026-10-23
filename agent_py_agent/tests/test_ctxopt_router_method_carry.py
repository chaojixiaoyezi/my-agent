# LLM: 只用离线卡片和真实方法沿用接缝；不把局部预算或模拟器结果外推为供应商验收。
# 模块用途: 回归完整目录预算、中文泛词降噪及扫描/正文完整性，守住原公开返回类型。
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability import method_carry
from agent_py_agent.agent.capability import router as module
from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
from agent_py_agent.agent.memory_archive import estimate_tokens
from agent_py_agent.tests.test_conversation_method_carry import method_fixture
from agent_py_agent.tests.test_conversation_method_restore import carry, compact


# LLM: 卡片身份和读取选择器保持真实格式，不用超短假编号掩盖包装开销。
# 函数用途: 建立足够长的可选目录，让渲染必须实际裁剪。
def cards(kind, count=40):
    return [module.CapabilityCard(
        id=f"{kind}:{index}", kind=kind, name=f"method-{index:03d}",
        description="核对资料、验证输入并保留完整稳定引用。" * 40,
        metadata={"stable_id": f"{'capability' if kind == 'capability_package' else 'owner'}:method-{index:03d}",
                  "package_id": f"method-{index:03d}"},
    ) for index in range(count)]


@pytest.mark.parametrize("kind", ["skill", "capability_package", "mixed"])
@pytest.mark.parametrize("window", [None, 200_000])
@pytest.mark.parametrize("selected", [False, True])
def test_complete_catalog_envelope_fits_budget(kind, window, selected):
    available = []
    if kind != "capability_package":
        available.extend(cards("skill"))
    if kind != "skill":
        available.extend(cards("capability_package"))
    router = module.CapabilityRouter(extra_cards=available)
    text = router.render_skill_metadata_index(
        context_window_tokens=window,
        selected_skill_ids=tuple(card.metadata["stable_id"] for card in available) if selected else None,
    )
    budget = module._skill_metadata_budget(window)
    assert budget.cost(text) <= budget.limit
    assert "skill_id:" in text if kind == "skill" else "package_id:" in text
    if kind != "skill":
        assert module._CAPABILITY_PACKAGE_USAGE_INSTRUCTIONS in text
    if kind != "capability_package" and not selected:
        assert module._SKILL_USAGE_INSTRUCTIONS in text


def test_package_reports_actual_envelope_cost():
    budget = module._SkillMetadataBudget(4000, True)
    text, used = module._render_package_metadata(cards("capability_package"), budget, None, ())
    assert used == budget.cost(text)
    assert used <= budget.limit


@pytest.mark.parametrize("kind", ["skill", "capability_package"])
@pytest.mark.parametrize("token_based", [False, True])
def test_exact_envelope_floor_is_not_treated_as_tiny_synthetic_budget(kind, token_based):
    available = cards(kind, 1)
    measure = module._SkillMetadataBudget(0, token_based)
    header = module._SKILL_METADATA_HEADER if kind == "skill" else module._PACKAGE_METADATA_HEADER
    usage = module._SKILL_USAGE_INSTRUCTIONS if kind == "skill" else module._CAPABILITY_PACKAGE_USAGE_INSTRUCTIONS
    floor = measure.cost("\n".join([*header, module._skill_line(available[0], ""), usage]))
    budget = module._SkillMetadataBudget(floor, token_based)
    text = module._render_public_skill_metadata(available, budget) if kind == "skill" else module._render_package_metadata(available, budget, None, ())[0]
    assert budget.cost(text) <= floor
    assert module._skill_line(available[0], "") in text


def test_mixed_directory_exact_token_floor_includes_intersection_and_separator():
    skill, package = cards("skill", 1)[0], cards("capability_package", 1)[0]
    measure = module._SkillMetadataBudget(0, True)
    skill_text = "\n".join([*module._SKILL_METADATA_HEADER, module._skill_line(skill, ""), module._SKILL_USAGE_INSTRUCTIONS])
    package_text = "\n".join([*module._PACKAGE_METADATA_HEADER, module._skill_line(package, ""), module._CAPABILITY_PACKAGE_USAGE_INSTRUCTIONS])
    floor = measure.cost(package_text + "\n\n" + skill_text)
    text = module.CapabilityRouter(extra_cards=[skill, package]).render_skill_metadata_index(context_window_tokens=floor * 50)
    assert measure.cost(text) <= floor
    assert module._skill_line(skill, "") in text and module._skill_line(package, "") in text


@pytest.mark.parametrize("selected", [None, ()])
def test_tiny_budget_preserves_authorized_required_and_in_use_ids(selected):
    skill, package = cards("skill", 1)[0], cards("capability_package", 1)[0]
    required = (skill.metadata["stable_id"], package.metadata["stable_id"])
    router = module.CapabilityRouter(extra_cards=[skill, package])
    for in_use in ((), required):
        text = router.render_skill_metadata_index(context_window_tokens=1, selected_skill_ids=selected,
            required_skill_ids=required, in_use_method_ids=in_use)
        assert "skill_id: owner:method-000" in text and "package_id: method-000" in text
        assert "（本会话在用）" in text if in_use else "（本会话在用）" not in text
    assert router.render_skill_metadata_index(context_window_tokens=1, selected_skill_ids=(),
        required_skill_ids=("owner:missing",)).count("owner:missing") == 0


@pytest.mark.parametrize("query", ["你好", "只回", "回复", "收到", "请问", "帮我", "一下", "可以", "什么", "怎么", "你好，只回复收到"])
def test_generic_chinese_request_terms_do_not_recall_unrelated_cards(query):
    card = module.CapabilityCard(id="skill:noise", kind="skill", name="unrelated",
        description="你好 只回 回复 收到 请问 帮我 一下 可以 什么 怎么", keywords=["帮我"])
    assert module.CapabilityRouter(extra_cards=[card]).search(query, kinds={"skill"}, limit=0) == []


@pytest.mark.parametrize("query", ["回复", "请使用回复", "帮我把论文翻译成中文PDF", "请问怎么调试Python异常"])
def test_exact_generic_name_and_meaningful_requests_still_recall(query):
    name = "回复" if "回复" in query else "论文翻译" if "论文" in query else "Python调试"
    card = module.CapabilityCard(id="skill:meaning", kind="skill", name=name,
        description="论文翻译中文PDF，Python异常调试")
    assert module.CapabilityRouter(extra_cards=[card]).search(query, kinds={"skill"}, limit=0)


# LLM: 当前基线从结构化会话账本取方法，并无旧版工具历史扫描；新增有界接口不得改旧 tuple 接口。
# 函数用途: 生成可识别的账本行，包含坏行和重复时仍由原解析规则处理。
def ledger(count):
    return [{"kind": "skill", "stable_id": f"owner:method-{index}",
             "first_used_at": index, "last_used_at": index} for index in range(count)]


@pytest.mark.parametrize("count,limit,inspected,truncated", [(0, 0, 0, False), (2, 2, 2, False), (3, 2, 2, True), (3, 0, 0, True)])
def test_bounded_method_scan_exposes_structured_completeness(count, limit, inspected, truncated):
    rows = ledger(count)
    result = method_carry.scan_methods(rows, scan_limit=limit)
    assert result.inspected_records == inspected and result.total_records == count
    assert result.truncated is truncated and result.complete is (not truncated)
    assert result.methods == method_carry.methods_from(rows[-limit:] if limit else [])
    assert type(method_carry.methods_from(rows)) is tuple


def test_unbounded_scan_keeps_original_deduplication_and_capacity():
    rows = [*ledger(9), None, {"kind": "wrong"}, {"kind": "skill", "stable_id": "owner:method-0", "last_used_at": 99}]
    result = method_carry.scan_methods(rows)
    assert result.complete is True and result.truncated is False
    assert result.methods == method_carry.methods_from(rows)
    assert tuple(row["stable_id"] for row in result.methods) == (
        "owner:method-5", "owner:method-6", "owner:method-7", "owner:method-8", "owner:method-0")


@pytest.mark.parametrize("body,allowance", [("短正文", 100), ("长正文。" * 1000, 100), ("长正文", 0), ("", 100)])
def test_skill_prefix_result_reports_body_truncation_without_changing_string_api(body, allowance):
    prefix = "Skill 参考（skill_id: owner:method）\n"
    result = method_carry.fit_skill_prefix_result(prefix, body, allowance)
    assert type(method_carry._fit_skill_prefix(prefix, body, allowance)) is str
    assert result.text == method_carry._fit_skill_prefix(prefix, body, allowance)
    assert result.total_chars == len(body)
    assert result.included_chars == (len(result.text) - len(prefix) if result.text else 0)
    assert result.complete is (result.included_chars == len(body))
    assert result.truncated is (not result.complete)
    # 空串没有投递内容；原estimate_tokens对空串返回最小计费1，不能当作零预算下的正文。
    assert (estimate_tokens(result.text) if result.text else 0) <= allowance
    if allowance == 0:
        assert result.text == "" and result.included_chars == 0 and result.truncated


def test_skill_carry_contains_budgeted_structured_truncation_status(tmp_path, monkeypatch):
    fixture = method_fixture(tmp_path)
    skill = fixture.scope.skills.enabled_entries()[0]
    monkeypatch.setattr(type(fixture.scope.skills), "read_body", lambda *_args: "长正文。" * 1000)
    request = SimpleNamespace(agent=fixture.agent, params=fixture.params, scope=fixture.scope,
        authority=SimpleNamespace(check=lambda: None), thread_id=fixture.link.thread_id, generation=1)
    text = method_carry._prepare_skill_method(request, {"stable_id": skill.stable_id}, 200)
    assert '"complete":false' in text and '"truncated":true' in text
    assert skill.stable_id in text and estimate_tokens(text) <= 200


@pytest.mark.parametrize("long_body", [False, True])
def test_real_skill_get_compact_and_carry_publish_structured_body_status(tmp_path, monkeypatch, long_body):
    fixture = method_fixture(tmp_path)
    skill = fixture.scope.skills.enabled_entries()[0]
    monkeypatch.setattr(type(fixture.scope.skills), "read_body", lambda *_args: "方法正文。" * (1000 if long_body else 1))
    assert SkillSearchTool(fixture.agent).execute({"action": "get", "skill_id": skill.stable_id}).ok
    compact(fixture)
    turns = carry(fixture, 300)
    assert len(turns) == 1 and estimate_tokens(turns[0].text) <= 300
    status = json.loads(next(line for line in turns[0].text.splitlines() if line.startswith('{"complete":')))
    assert status["truncated"] is long_body and status["complete"] is (not long_body)
    assert status["next_read"] == {"action": "get", "skill_id": skill.stable_id}
