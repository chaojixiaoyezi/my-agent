"""能力包软使用合同进入真实提示布局；不把 fake 模型响应当成自然召回验收。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.capability.router import CapabilityRouter
from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshot, SkillSnapshotEntry
from agent_py_agent.agent.prompting_parts.builder import PromptBuilder
from agent_py_agent.agent.prompting_parts.cache_layout import prompt_cache_layout
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_agent.backends import _TestNativeBackend
from agent_py_agent.tests.test_capability_package_discovery import (
    discovery_fixture,
    package_fixture,
)
from agent_py_agent.tests.test_capability_package_task_refs import _agent
from agent_py_agent.tests.test_decision_skill_projection import build


# LLM: 这些检查约束提示合同在所有宿主布局中可见；不能证明模型自然选择了包或替代真实验收。
# 函数用途: 核对采用、完整读取、资源复制、派工和权限说明未在摘要或选中视图里漏失。
def assert_package_guidance(text):
    assert text.count("### 能力包使用规则") == 1
    assert "任务明确匹配包摘要" in text
    assert "skill_search(action=get, package_id=" in text
    assert "resource_path" in text and "continuation" in text and "has_more=true" in text
    assert "has_more_after=true" in text and "next_read" in text and "next_offset" in text
    assert "source_ref" in text and "原样" in text
    assert "allowed_skills" in text and "capability:<package_id>" in text
    assert "宿主" in text and "固定" in text
    assert "不能改写或削弱原核验器" in text
    assert "不增加工具、路径或网络权限" in text


@pytest.mark.parametrize("count", [1, 3])
def test_package_only_and_mixed_indexes_include_one_usage_contract_without_reading(tmp_path, skill_catalog_factory, count):
    reads = []
    packages = [package_fixture(f"resource-pack-{index}", reads=reads) for index in range(count)]
    _, snapshot, agent, _ = discovery_fixture(tmp_path, skill_catalog_factory, packages)
    for scoped in (snapshot, replace(snapshot, entries=())):
        router = CapabilityRouter(skill_snapshot=scoped)
        prompt = router.render_skill_metadata_index()
        assert_package_guidance(prompt)
        assert prompt.count("package_id:") == count
        assert "私有独门" not in prompt and "PRIVATE-METHOD-BODY" not in prompt
        assert len(router.cards(kinds={"skill"})) == len(scoped.entries)
    assert agent.current_skill_snapshot() is snapshot and reads == []


@pytest.mark.parametrize("selected", [None, (), ("capability:resource-pack",)])
def test_selected_empty_and_full_prompt_keep_scoped_discovery_and_cache_layout(tmp_path, skill_catalog_factory, selected):
    reads = []
    package = package_fixture("resource-pack", reads=reads)
    _, snapshot, agent, tool = discovery_fixture(tmp_path, skill_catalog_factory, [package])
    builder = PromptBuilder(AgentConfig(prompt_files=[]), tmp_path)
    builder.capability_router = agent.capability_router
    prompt = build(builder, selected=selected)
    assert_package_guidance(str(prompt))
    layout = prompt_cache_layout(prompt)
    surface = layout.stable_prefix if selected is None else dict(layout.volatile_sections)["prompt.tool_recommendations"]
    assert "### 能力包使用规则" in surface
    if selected == ():
        assert "1 个能力包未展示" in prompt and "package_id: resource-pack" not in prompt
        search = tool.execute({"action": "search", "query": "resource-pack"})
        assert search.ok and any(row.get("package_id") == package.package_id for row in json.loads(search.output)["matches"])
    assert agent.current_skill_snapshot() is snapshot and reads == []


def test_budget_omission_keeps_generic_guidance_once_and_searchable_packages(tmp_path, skill_catalog_factory):
    reads = []
    packages = [package_fixture(f"resource-pack-{index:04}", reads=reads) for index in range(1000)]
    _, snapshot, agent, tool = discovery_fixture(tmp_path, skill_catalog_factory, packages)
    prompt = agent.capability_router.render_skill_metadata_index(context_window_tokens=100)
    assert_package_guidance(prompt)
    assert "1000 个能力包未展示" in prompt
    assert len(prompt) < 3000 and "PRIVATE-METHOD-BODY" not in prompt
    match = json.loads(tool.execute({"action": "search", "query": "resource-pack-0999"}).output)
    assert any(row.get("package_id") == "resource-pack-0999" for row in match["matches"])
    assert len(snapshot.entries) == 1 and len(snapshot.packages) == 1000 and reads == []


@pytest.mark.parametrize("selected,digest", [
    (None, "630ed877495e944f907937c93ed96a72af7b34f796748a72f34b8f25159ae222"),
    ((), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
    (("builtin:ordinary",), "df75146213b83f696b710502c14c1c1bdcf378ff530984dca90f0ff1ebd53597"),
])
def test_no_package_keeps_frozen_ordinary_skill_prompt_bytes(selected, digest):
    # 此字节基线采自修改前的 36439d633，覆盖默认、空选择和显式选择三个公开 Skill 视图。
    entry = SkillSnapshotEntry("builtin:ordinary", "ordinary", "核对资料与保留来源", "/unused/ordinary/SKILL.md",
                               "builtin", "general", "a" * 64)
    router = CapabilityRouter(skill_snapshot=SkillSnapshot((entry,), (), "fixture", "local/main", "/unused"))
    prompt = router.render_skill_metadata_index(context_window_tokens=200000, selected_skill_ids=selected)
    assert hashlib.sha256(prompt.encode()).hexdigest() == digest


def test_task_local_scope_never_exposes_ungranted_package_or_private_members(tmp_path, skill_catalog_factory):
    first, second = package_fixture("first"), package_fixture("second")
    _, snapshot, _, _ = discovery_fixture(tmp_path, skill_catalog_factory, [first, second])
    scoped = snapshot.restricted([first.stable_id], expected_refs=[first.to_ref()])
    builder = PromptBuilder(AgentConfig(prompt_files=[]), tmp_path)
    builder.capability_router = CapabilityRouter(skill_snapshot=scoped)
    prompt = build(builder, selected=(first.stable_id, second.stable_id), required=(first.stable_id,), scope="task_local")
    assert_package_guidance(str(prompt))
    assert "package_id: first" in prompt and "package_id: second" not in prompt
    assert "PRIVATE-METHOD-BODY" not in prompt and "私有独门" not in prompt
    assert scoped.packages == (first,) and scoped.entries == ()


def test_native_fake_reply_receives_usage_contract_without_implicit_reads_or_extra_model_calls(tmp_path):
    agent, _store, _entries = _agent(tmp_path)

    class Backend(_TestNativeBackend):
        name = "package-guidance-fixture"

        def __init__(self):
            self.calls = 0

        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls += 1
            assert_package_guidance(str(prompt))
            assert "package_id: story-a" in prompt and "package_id: story-b" in prompt
            assert "methods/SKILL.md" not in prompt
            return ModelResponse(text="已收到。", backend=self.name)

    backend = Backend()
    agent.backend = backend
    result = agent.run("先说说如何整理这份资料。", save=False, allowed_tools=["skill_search", "write_file"],
                       context_scope="conversation")
    assert result.response == "已收到。" and backend.calls == 1
    assert result.operation_verification.get("operations", []) == []
