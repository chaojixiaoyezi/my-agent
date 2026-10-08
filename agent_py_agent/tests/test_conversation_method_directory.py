# LLM: 用当前受限快照和真实 run scope 验证目录缓存边界，不借全局注册表扩大 required。
# 模块用途: 锁定首次使用顺序、必显、失效过滤及 run 内冻结，原空名单字节必须保持。
from dataclasses import replace

from agent_py_agent.agent.agent_core.runtime_mixin import current_prompt_scope
from agent_py_agent.agent.capability.method_carry import prompt_method_ids
from agent_py_agent.agent.capability.router import CapabilityRouter
from agent_py_agent.tests.test_conversation_method_carry import (
    method_fixture,
    read_package,
    records,
    switch,
)


def render(fixture, **options):
    return CapabilityRouter(skill_snapshot=fixture.agent.current_skill_snapshot()).render_skill_metadata_index(**options)


def ids(fixture):
    return tuple(row["stable_id"] for row in sorted(records(fixture), key=lambda row: row["first_used_at"]))


def test_in_use_packages_first_marked_and_rule_only_with_valid_used_packages(tmp_path):
    fixture = method_fixture(tmp_path, count=3)
    assert read_package(fixture, 2).ok and read_package(fixture, 1).ok
    text = render(fixture, in_use_method_ids=ids(fixture))
    assert text.index("- entry-2") < text.index("- entry-1") < text.index("- entry-0")
    assert "entry-2（本会话在用）" in text
    assert "接着做同类的事，继续照在用包的方法和你走到的那一步；不相关的事照常处理。" in text
    assert render(fixture, in_use_method_ids=()) == render(fixture)


def test_in_use_required_survives_tiny_budget_and_empty_selection(tmp_path):
    fixture = method_fixture(tmp_path)
    skill = fixture.scope.skills.enabled_entries()[0]
    used = ("capability:entry-0", skill.stable_id)
    text = render(fixture, in_use_method_ids=used, selected_skill_ids=(), context_window_tokens=1)
    assert "entry-0（本会话在用）" in text and f"{skill.name}（本会话在用）" in text
    assert skill.stable_id in text


def test_removed_methods_not_marked_or_added_from_unscoped_catalog(tmp_path):
    fixture = method_fixture(tmp_path, count=2)
    skill = fixture.scope.skills.enabled_entries()[0]
    before = render(fixture)
    assert read_package(fixture).ok
    fixture.agent._current_skill_snapshot = replace(fixture.scope.skills, packages=(), entries=())
    assert render(fixture, in_use_method_ids=("capability:entry-0", skill.stable_id)) == ""
    fixture.agent._current_skill_snapshot = fixture.scope.skills
    assert render(fixture, in_use_method_ids=("capability:missing", "missing:skill")) == before


def test_display_order_does_not_follow_recent_usage(tmp_path):
    fixture = method_fixture(tmp_path, count=3)
    for n in (2, 1, 0):
        assert read_package(fixture, n).ok
    before = render(fixture, in_use_method_ids=ids(fixture))
    assert read_package(fixture, 1).ok
    assert render(fixture, in_use_method_ids=ids(fixture)) == before


def test_run_freezes_methods_once_midrun_get_only_changes_next_run(tmp_path):
    fixture = method_fixture(tmp_path, count=2)
    assert read_package(fixture).ok
    with current_prompt_scope(fixture.agent, "继续", fixture.params):
        assert prompt_method_ids(fixture.agent) == ("capability:entry-0",)
        before = render(fixture, in_use_method_ids=prompt_method_ids(fixture.agent))
        assert read_package(fixture, 1).ok
        assert prompt_method_ids(fixture.agent) == ("capability:entry-0",)
        assert render(fixture, in_use_method_ids=prompt_method_ids(fixture.agent)) == before
    with current_prompt_scope(fixture.agent, "再继续", replace_params(fixture.params)):
        assert prompt_method_ids(fixture.agent) == ("capability:entry-0", "capability:entry-1")


def replace_params(params):
    from types import SimpleNamespace
    return SimpleNamespace(**{key: value for key, value in vars(params).items() if key != "conversation_methods"})


def test_off_switch_blocks_run_metadata_without_deleting_records(tmp_path):
    fixture = method_fixture(tmp_path)
    assert read_package(fixture).ok
    switch(fixture, False)
    with current_prompt_scope(fixture.agent, "继续", fixture.params):
        assert prompt_method_ids(fixture.agent) == ()
        assert render(fixture, in_use_method_ids=prompt_method_ids(fixture.agent)) == render(fixture)
    assert len(records(fixture)) == 1


# 3a 集成复核（10-08）：平常没有 Jev 选择时，有 skill 在用也必须保持普通目录格式（标题与使用规则不丢），在用的排最前并标注。
def test_in_use_skill_keeps_public_directory_format_and_usage_rules(tmp_path):
    from agent_py_agent.agent.capability.router import _SKILL_USAGE_INSTRUCTIONS

    fixture = method_fixture(tmp_path)
    skills = fixture.scope.skills.enabled_entries()
    used = skills[-1]
    text = render(fixture, in_use_method_ids=(used.stable_id,))
    assert "# Available Skills" in text and "# Selected Skills" not in text
    assert _SKILL_USAGE_INSTRUCTIONS in text
    assert f"- {used.name}（本会话在用）" in text
    section = text[text.index("# Available Skills"):]
    first_skill_line = next(line for line in section.splitlines() if line.startswith("- "))
    assert first_skill_line.startswith(f"- {used.name}（本会话在用）")


# 3a 集成复核（10-08）：普通目录在极小预算下，在用 skill 仍显示，其余照原规则省略并报告。
def test_in_use_skill_survives_tiny_budget_in_public_directory(tmp_path):
    fixture = method_fixture(tmp_path)
    used = fixture.scope.skills.enabled_entries()[-1]
    text = render(fixture, in_use_method_ids=(used.stable_id,), context_window_tokens=1)
    assert f"{used.name}（本会话在用）" in text and used.stable_id in text
    assert "# Selected Skills" not in text
