"""包级动态建议的配置、权限、纯投影与零网络出站验证；不证明真实模型自然采用。"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
from agent_py_agent.agent.agent_core.runtime.loop_models import (
    RuntimeContextRequest,
    RuntimeLoopParams,
)
from agent_py_agent.agent.agent_core.runtime.loop_support import (
    ToolSectionsRequest,
    _required_action_contract_snapshot,
    _resolve_tool_sections,
    _tool_snapshots_for_run,
)
from agent_py_agent.agent.agent_core.tool_ir_history import (
    project_native_prompt_history,
    project_native_provider_messages,
)
from agent_py_agent.agent.backends import ModelResponse, gateway_helpers
from agent_py_agent.agent.backends.anthropic import AnthropicCompatibleBackend
from agent_py_agent.agent.backends.base import BackendOptions
from agent_py_agent.agent.backends.tool_ir import UserTurn
from agent_py_agent.agent.backends.typesafe_decision import TypesafeDecisionBackend
from agent_py_agent.agent.backends.typesafe_decision_wire import (
    parse_typesafe_response,
    typesafe_payload,
)
from agent_py_agent.agent.capability.config import CapabilityConfig, load_capability_config
from agent_py_agent.agent.capability.decision_recommendation import (
    CapabilityPresentation,
    recommend_capabilities,
)
from agent_py_agent.agent.capability.package_snapshot import CapabilityPackageSnapshot
from agent_py_agent.agent.capability.router import CapabilityRouter
from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshot
from agent_py_agent.agent.conversation.compact_provider_surface import (
    ConversationCompactModelSurface,
    prepare_conversation_compact_provider_surface,
)
from agent_py_agent.agent.prompting_parts.builder import (
    PromptBuildRequest,
    ToolSections,
    render_prepared_prompt,
)
from agent_py_agent.agent.prompting_parts.cache_layout import prompt_cache_layout
from agent_py_agent.agent.runtime_context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.tests.test_agent.backends import _TestNativeBackend
from agent_py_agent.tests.test_capability_package_discovery import package_fixture
from agent_py_agent.tests.test_capability_package_task_refs import _agent, _bind_main_task
from agent_py_agent.tests.test_decision_model_profiles import decision
from agent_py_agent.tests.test_decision_settings import patch

_QUERY = "把故事整理成分镜"
_TITLE = "# 本轮能力包候选"


# LLM: 只构造不可变元数据视图；reader 仍为原包测试替身，调用次数可观察，不能授权正文访问。
# 函数用途: 在不初始化宿主的测试中装配任意规模的包目录。
def _router(packages, *, limit=5):
    snapshot = SkillSnapshot((), (), "fixture", "local/main", "/unused", packages=tuple(packages))
    return CapabilityRouter(config=CapabilityConfig(capability_candidate_limit=limit), skill_snapshot=snapshot)


# LLM: 只解析测试约定的机器字段，不据提示文案推权限或验收业务结果。
# 函数用途: 检查每张动态卡都是完整 JSON，读取参数未被预算裁剪。
def _cards(text):
    return [json.loads(line[2:]) for line in text.splitlines() if line.startswith('- {"package_id"')]


# LLM: 测试配置沿真实 capability_config_path 加载，不给 Router 塞主配置或创建第二套开关。
# 函数用途: 在 pytest 临时目录写本轮明确配置，并清原配置缓存以模拟新工作片。
def _configure(agent, *, enabled=True, limit=5):
    path = agent.root / "capability.yaml"
    path.write_text(f"enable_capability_package_recommendations: {str(enabled).lower()}\n"
                    f"capability_candidate_limit: {limit}\n", encoding="utf-8")
    agent.capability_config_path = path
    agent._capability_config_runtime_snapshot = None


# LLM: 使用原工具注册表冻结授权；范围和选中集合只传本次请求，不改共享 Router。
# 函数用途: 调用生产推荐接缝，返回原工具目录与动态推荐字符串。
def _sections(agent, *, scope="default", selected=None, protocol="native", allowed=("skill_search",), snapshot=None):
    names = list(allowed)
    runtime = snapshot if snapshot is not None else agent.tools.runtime_snapshot(allowed_tools=names)
    return _resolve_tool_sections(ToolSectionsRequest(
        agent, _QUERY, names, runtime, SimpleNamespace(source_protocol=protocol),
        context_scope=scope, selected_skill_ids=selected,
    ))


# LLM: 真正的宿主准备只做一次；后台/容量/恢复测试随后仅使用冻结值，不复查目录或配置。
# 函数用途: 沿原 Builder 生成文本或原生提示材料，固定工作区避免时钟影响字节比较。
def _prepared(agent, *, protocol="native", selected=None, scope="default"):
    # 运行时工具协议仍只有 native；text 只覆盖既有纯文本提示及 provider 装配，不复活旧文本调用协议。
    catalog, recommendations = _sections(agent, selected=selected, scope=scope)
    return agent.prompts.prepare_render_input(PromptBuildRequest(
        _QUERY, [], context_scope=scope, workspace_context_override="固定工作区",
        tools=ToolSections(tool_catalog_section=catalog, tool_recommendations_section=recommendations,
                           native_tool_use=protocol == "native", selected_skill_ids=selected),
    ))


def test_package_recommendation_default_matches_shipped_yaml():
    path = Path(__file__).parents[1] / "config" / "capability_config.yaml"
    assert CapabilityConfig().enable_capability_package_recommendations is True
    assert load_capability_config(path).enable_capability_package_recommendations is True


@pytest.mark.parametrize("value,expected", [('"false"', False), ("true", True), ("false", False)])
def test_package_recommendation_flag_loads_real_yaml(tmp_path, value, expected):
    path = tmp_path / "capability.yaml"
    path.write_text(f"enable_capability_package_recommendations: {value}\n", encoding="utf-8")
    assert load_capability_config(path).enable_capability_package_recommendations is expected


def test_metadata_candidates_reuse_soft_order_and_keep_exact_read_generation():
    reads = []
    first, second = package_fixture("alpha", reads=reads), package_fixture("beta", reads=reads)
    router = _router([first, second], limit=1)
    expected = router.search(_QUERY, kinds={"capability_package"})
    text = router.render_package_recommendations(_QUERY)
    rows = _cards(text)
    assert [row["package_id"] for row in rows] == [hit.card.name for hit in expected]
    assert rows[0]["next_read"] == {"action": "get", "package_id": first.package_id,
                                    "expected_package_sha256": first.package_sha256,
                                    "expected_activation_id": first.activation_id}
    assert "description" in rows[0] and "skill_id" not in rows[0]["next_read"]
    assert "能力包使用规则" not in text and "不增加授权" in text
    assert all(secret not in text for secret in ("私有独门", "PRIVATE-METHOD-BODY", "CAPABILITY.md", "scripts/"))
    assert router.render_package_recommendations("私有独门") == "" and reads == []
    assert len(router.cards(kinds={"skill"})) == 0


@pytest.mark.parametrize("selected,expected", [(None, ["alpha"]), ((), []),
                                              (("capability:beta",), ["beta"]), (("beta",), [])])
def test_selection_filters_before_limit_without_reintroducing_unselected_packages(selected, expected):
    router = _router([package_fixture("alpha"), package_fixture("beta")], limit=1)
    text = router.render_package_recommendations(_QUERY, selected_skill_ids=selected)
    assert [row["package_id"] for row in _cards(text)] == expected
    assert len(router.cards(kinds={"capability_package"})) == 2


def test_large_directory_obeys_existing_budget_without_partial_json_or_private_resources():
    packages = [replace(package_fixture(f"pack-{index:04d}"), description="分镜资料" * 800) for index in range(1000)]
    router = _router(packages, limit=0)
    text = router.render_package_recommendations(_QUERY, context_window_tokens=20_000)
    rows = _cards(text)
    assert rows and len(rows) < 1000
    assert (len(text.encode()) + 3) // 4 <= 400
    assert all(len(row["next_read"]["expected_activation_id"]) == 64 for row in rows)
    assert router.render_package_recommendations(_QUERY, context_window_tokens=1) == ""
    assert len(router.render_package_recommendations(_QUERY)) <= 8000


@pytest.mark.parametrize("condition", ["off", "no_packages", "isolated", "control_plane", "task_local", "no_permission", "hidden", "unavailable"])
def test_ineligible_run_preserves_original_tool_sections_bytes(tmp_path, condition):
    agent, _store, _entries = _agent(tmp_path)
    _configure(agent, enabled=condition != "off")
    if condition == "no_packages":
        agent.skills_service.package_provider = lambda: ()
    allowed = ["read_file"] if condition == "no_permission" else ["skill_search"]
    snapshot = agent.tools.runtime_snapshot(allowed_tools=allowed)
    if condition in {"hidden", "unavailable"}:
        runtime = snapshot.runtime("skill_search")
        runtime = (replace(runtime, exposure=replace(runtime.exposure, model_visible=False)) if condition == "hidden"
                   else replace(runtime, availability=replace(runtime.availability, available=False)))
        snapshot = replace(snapshot, runtimes=(runtime,), snapshot_hash="")
    expected = (
        agent.tools.render_catalog_section(allowed_tools=allowed, tool_protocol="native", runtime_snapshot=snapshot),
        agent.tools.render_recommended_tools_section(_QUERY, allowed_tools=allowed, tool_protocol="native", runtime_snapshot=snapshot),
    )
    actual = _sections(agent, allowed=allowed, scope=condition, snapshot=snapshot)
    assert actual == expected
    if condition == "off":
        assert "package_id: story-a" in agent.capability_router.render_skill_metadata_index()


@pytest.mark.parametrize("protocol", ["native", "text"])
def test_task_candidates_enter_existing_dynamic_segment_and_off_keeps_static_directory(tmp_path, protocol):
    agent, _store, _entries = _agent(tmp_path)
    _configure(agent, limit=1)
    enabled = render_prepared_prompt(_prepared(agent, protocol=protocol))
    assert enabled.count(_TITLE) == 1 and enabled.count("### 能力包使用规则") == 1
    assert len(_cards(enabled)) == 1
    _configure(agent, enabled=False)
    disabled = render_prepared_prompt(_prepared(agent, protocol=protocol))
    assert _TITLE not in disabled and "package_id: story-a" in disabled
    if protocol == "native":
        enabled_layout, disabled_layout = prompt_cache_layout(enabled), prompt_cache_layout(disabled)
        assert enabled_layout.stable_prefix == disabled_layout.stable_prefix
        assert _TITLE in dict(enabled_layout.volatile_sections)["prompt.tool_recommendations"]
        assert _TITLE not in enabled_layout.stable_prefix


def test_frozen_background_resume_projection_never_requeries_or_mutates_pins(tmp_path, monkeypatch):
    agent, _store, _entries = _agent(tmp_path)
    _configure(agent)
    _bind_main_task(agent)
    prepared = _prepared(agent)
    expected = render_prepared_prompt(prepared)
    original = agent.conversation_store.tasks.load("main-task")

    def forbidden(*args, **kwargs):
        raise AssertionError("冻结投影不得读取配置、包或宿主状态")

    with monkeypatch.context() as pure:
        pure.setattr(CapabilityRouter, "render_package_recommendations", forbidden)
        pure.setattr(CapabilityPackageSnapshot, "read", forbidden)
        pure.setattr(Path, "read_text", forbidden)
        for injection in (("后台续跑事实",), ("Compact 恢复事实",)):
            restored = replace(prepared, injection_fragments=injection)
            prompt = render_prepared_prompt(restored)
            assert _cards(prompt) == _cards(expected)
            assert prompt_cache_layout(prompt).stable_prefix == prompt_cache_layout(expected).stable_prefix
            assert injection[0] in prompt
        assert render_prepared_prompt(prepared) == expected
    assert agent.conversation_store.tasks.load("main-task").skill_snapshot_refs == original.skill_snapshot_refs == ()


def test_task_local_recommendations_only_use_canonical_child_grants(tmp_path):
    agent, _store, _entries = _agent(tmp_path)
    _configure(agent)
    outcome = CreateSubagentsTool(agent).execute({"goal": _QUERY, "allowed_skills": ["capability:story-a"],
                                                "allowed_tools": ["skill_search"], "defer_start": True})
    assert outcome.ok, outcome.output
    child = agent.subagents.list_runs()[0]
    previous = set_current_subagent_context(agent, run_id=child.id, task_attributes=child.attributes)
    try:
        before = agent.current_skill_snapshot()
        _catalog, text = _sections(agent, scope="task_local", selected=("capability:story-a", "capability:story-b"))
        assert [row["package_id"] for row in _cards(text)] == ["story-a"]
        assert agent.current_skill_snapshot().fingerprint == before.fingerprint
        current = agent.subagents.load(child.id)
        assert current.allowed_skills == child.allowed_skills
        assert current.attributes["skill_snapshot_refs"] == child.attributes["skill_snapshot_refs"]
    finally:
        restore_current_subagent_context(agent, previous)


@pytest.mark.parametrize("selected", [None, (), ("capability:story-b",)])
def test_native_loop_carries_selection_and_adds_no_model_call_or_tool_operation(tmp_path, monkeypatch, selected):
    agent, _store, _entries = _agent(tmp_path)
    _configure(agent)
    seen = []

    def forbidden(*args, **kwargs):
        raise AssertionError("候选展示不得读取正文或固定任务版本")

    monkeypatch.setattr(CapabilityPackageSnapshot, "read", forbidden)
    monkeypatch.setattr("agent_py_agent.agent.capability.task_references.pin_package_reference", forbidden)

    class Backend(_TestNativeBackend):
        def generate(self, prompt, **kwargs):
            dynamic = [block["text"] for message in kwargs.get("messages", [])
                       for block in message["content"] if isinstance(block, dict) and block.get("type") == "text"]
            seen.append("\n".join([str(prompt), *dynamic]))
            return ModelResponse(text="已收到。", backend="recommendation-fixture")

    if selected is not None:
        monkeypatch.setattr("agent_py_agent.agent.capability.decision_recommendation.recommend_capabilities",
                            lambda _agent, _params, snapshot, _contract: CapabilityPresentation(snapshot, selected_skill_ids=selected))
    agent.backend = Backend()
    result = agent.run(_QUERY, save=False, allowed_tools=["skill_search"], context_scope="conversation")
    assert result.response == "已收到。" and len(seen) == 1
    rows = _cards(seen[0])
    assert [row["package_id"] for row in rows] == (["story-a", "story-b"] if selected is None else
                                                 ["story-b"] if selected else [])
    assert result.operation_verification.get("operations", []) == []


@pytest.mark.parametrize("selected,scope", [(("capability:story-b",), "default"), ((), "default"),
                                            (("capability:story-b",), "isolated"), (("capability:story-b",), "control_plane")])
def test_real_compact_preparation_passes_revalidated_selection_and_model_scope(tmp_path, monkeypatch, selected, scope):
    agent, _store, _entries = _agent(tmp_path)
    _configure(agent)
    attrs = _bind_main_task(agent)
    agent.backend = _TestNativeBackend()
    profile, _ = decision(agent)
    patch(agent, {"enabled": True, "profile_id": profile, "points.skill_tool.mode": "apply"})
    calls = []

    def choose(backend, request, *, deadline):
        calls.append(request)
        questions = typesafe_payload(request, backend.model_name)["questions"]
        answers = {}
        for key, question in questions.items():
            choice = "include" if question["instructions"]["candidate"]["ref"] in selected else "not_needed"
            answers[key] = {"type": "choice", "choice": choice, "confidence": 1.0,
                            "probabilities": {candidate: float(candidate == choice) for candidate in question["criteria"]}}
        return parse_typesafe_response(request, backend.model_name, {"model": "decision-fixture", "answers": answers})

    monkeypatch.setattr(TypesafeDecisionBackend, "decide", choose)
    context = RuntimeContextRequest(_QUERY, [], False, "default", allowed_tools=["skill_search"],
                                    request_id="compact-request", run_id="compact-run", task_id="main-task", task_attributes=attrs)
    runtime, protocol = _tool_snapshots_for_run(agent, context)
    params = RuntimeLoopParams(_QUERY, _QUERY, [], [], None, "", request_id=context.request_id,
                               run_id=context.run_id, task_id=context.task_id, task_attributes=attrs,
                               allowed_tools=context.allowed_tools, tool_runtime_snapshot=runtime, tool_protocol_snapshot=protocol,
                               capability_presentation_turn_id="same-turn")
    contract = _required_action_contract_snapshot(agent, params, runtime)
    chosen = recommend_capabilities(agent, params, runtime, contract)
    assert chosen.selection is not None, chosen.finding
    assert chosen.selected_skill_ids == selected
    observed, requests = [], []

    def resolve(request):
        requests.append(request)
        return _resolve_tool_sections(request)

    monkeypatch.setattr("agent_py_agent.agent.agent_core.runtime.loop_support._resolve_tool_sections", resolve)
    requested = ConversationCompactModelSurface(
        allowed_tools=("skill_search",), context_scope=scope, presentation_context=context,
        capability_presentation=chosen.selection, capability_presentation_turn_id="same-turn",
        capability_presentation_callback=observed.append,
    )
    prepared = prepare_conversation_compact_provider_surface(agent, requested, run_id="summary-operation")
    assert len(calls) == 1 and len(requests) == 1
    assert requests[0].context_scope == scope
    expected_selection = selected if scope == "default" else None
    assert requests[0].selected_skill_ids == expected_selection
    if scope == "default":
        assert observed == [chosen.selection]
        dynamic = dict(prepared.volatile_sections)["prompt.tool_recommendations"]
        assert [row["package_id"] for row in _cards(dynamic)] == (["story-b"] if selected else [])
    else:
        assert observed == [None] and prepared.volatile_sections == ()


@pytest.mark.parametrize("protocol", ["native", "text"])
@pytest.mark.parametrize("stream,cache", [(False, False), (False, True), (True, False), (True, True)])
def test_actual_anthropic_http_serialization_carries_bounded_recommendations(tmp_path, monkeypatch, protocol, stream, cache):
    agent, _store, _entries = _agent(tmp_path)
    _configure(agent)
    prompt = render_prepared_prompt(_prepared(agent, protocol=protocol, selected=("capability:story-b",)))
    messages = None
    if protocol == "native":
        prompt, history = project_native_prompt_history(SimpleNamespace(tool_ir_history=[UserTurn(_QUERY)]), prompt,
                                                       conversation_state="")
        messages = project_native_provider_messages(history)
    captured = []

    class CapturedBeforeNetwork(BaseException):
        pass

    def capture(req, _request):
        captured.append(json.loads(req.data.decode()))
        raise CapturedBeforeNetwork

    monkeypatch.setattr(gateway_helpers, "_gateway_urlopen", capture)
    backend = AnthropicCompatibleBackend(BackendOptions(
        api_base="https://example.invalid/anthropic", api_key="test-only", model_name="fixture",
        stream_enabled=stream, prompt_cache_enabled=cache,
    ))
    with pytest.raises(CapturedBeforeNetwork):
        backend.generate(prompt, messages=messages)
    assert len(captured) == 1
    body = json.dumps(captured[0], ensure_ascii=False)
    assert body.count(_TITLE) == 1 and "expected_package_sha256" in body and "expected_activation_id" in body
    assert "story-b" in body and "story-a" not in body
    assert "PRIVATE-METHOD-BODY" not in body and "methods/SKILL.md" not in body
    if protocol == "native":
        assert _TITLE not in json.dumps(captured[0].get("system", []), ensure_ascii=False)
        assert _TITLE in json.dumps(captured[0]["messages"], ensure_ascii=False)
