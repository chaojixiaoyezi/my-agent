"""临时 owner 的真实 PromptBuilder 跨回合字节合同，不访问生产 persona。"""
import json
import logging
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.persona_repository import PersonaRepository
from agent_py_agent.agent.prompting_parts.builder import (
    PromptBuilder,
    PromptBuildRequest,
    ToolSections,
)
from agent_py_agent.agent.prompting_parts.cache_layout import prompt_cache_layout
from agent_py_agent.agent.settings import AgentConfig


@pytest.fixture
def scene(tmp_path):
    home = tmp_path / "owner"
    home.mkdir()
    paths = {target: home / name for target, name in
             (("agents", "AGENTS.md"), ("soul", "SOUL.md"), ("user", "USER.md"))}
    for path in paths.values():
        path.write_text("# 初始\n- 原有行\n", encoding="utf-8")
    repo = PersonaRepository(owner_home=home, agents_path=paths["agents"],
                             soul_path=paths["soul"], user_path=paths["user"])
    config = AgentConfig(prompt_files=[])
    home_paths = SimpleNamespace(owner_home_dir=home)
    builder = PromptBuilder(config, tmp_path, home_paths=home_paths, persona_repository=repo)
    return builder, paths, home


def request(generation=0, profile="m1", thread="thread-one", protocol="responses"):
    value = PromptBuildRequest("继续", [], tools=ToolSections(native_tool_use=True), workspace_context_override="固定工作区")
    # 先用结构相同的轻量事实对象，让未实现版本也能运行到真实字节断言。
    value.thread_persona = SimpleNamespace(thread_id=thread, compact_generation=generation,
                                          model_profile_id=profile, protocol=(protocol, "native"))
    return value


def prefix_and_updates(builder, value):
    layout = prompt_cache_layout(builder.build(request=value))
    return layout.stable_prefix, dict(layout.volatile_sections).get("prompt.persona_updates", "")


def test_append_keeps_prefix_and_adds_typed_tail(scene):
    builder, paths, _ = scene
    first, _ = prefix_and_updates(builder, request())
    paths["agents"].write_text("# 初始\n- 原有行\n- 新增规则\n", encoding="utf-8")
    second, updates = prefix_and_updates(builder, request())
    assert second == first
    assert "新增规则" in updates
    assert "agents" in updates


@pytest.mark.parametrize("changed", [request(1), request(profile="m2"), request(protocol="anthropic")])
def test_committed_epoch_refreshes_and_clears_tail(scene, changed):
    builder, paths, _ = scene
    first, _ = prefix_and_updates(builder, request())
    paths["agents"].write_text("- 新增规则\n", encoding="utf-8")
    stable, updates = prefix_and_updates(builder, request())
    assert stable == first and updates
    refreshed, updates = prefix_and_updates(builder, changed)
    assert "新增规则" in refreshed and not updates


def test_disabled_and_threadless_keep_live_behavior(scene):
    builder, paths, home = scene
    builder.config.thread_prompt_prefix_freeze_enabled = False
    first, _ = prefix_and_updates(builder, request())
    paths["agents"].write_text("- 现读\n", encoding="utf-8")
    second, updates = prefix_and_updates(builder, request())
    assert second != first and not updates
    assert not (home / "persona" / "thread_prefix").exists()
    builder.config.thread_prompt_prefix_freeze_enabled = True
    value = request(thread="")
    first, _ = prefix_and_updates(builder, value)
    paths["agents"].write_text("- 无线程继续现读\n", encoding="utf-8")
    second, updates = prefix_and_updates(builder, value)
    assert first != second and not updates


def test_long_agents_and_restart_keep_exact_frozen_bytes(scene):
    builder, paths, _ = scene
    paths["agents"].write_text("- 很长的规则\n" * 5000, encoding="utf-8")
    first, _ = prefix_and_updates(builder, request())
    with paths["agents"].open("a", encoding="utf-8") as file:
        file.write("- 新增尾行\n")
    restarted = PromptBuilder(builder.config, builder.root, home_paths=builder.home_paths,
                              persona_repository=builder.persona_repository)
    second, updates = prefix_and_updates(restarted, request())
    assert first == second
    assert "新增尾行" in updates


def test_truncated_agents_append_only_reports_two_new_lines_not_window_evictions(scene):
    builder, paths, _ = scene
    original = [f"规则{i:04d}" + "." * 80 for i in range(600)]
    additions = [f"新增{i:04d}" + "." * 80 for i in range(600, 602)]
    paths["agents"].write_text("\n".join(original) + "\n", encoding="utf-8")
    assert builder.persona_repository.snapshot()["agents"].diagnostic.truncated
    first, _ = prefix_and_updates(builder, request())
    with paths["agents"].open("a", encoding="utf-8") as file:
        file.write("\n".join(additions) + "\n")
    stable, updates = prefix_and_updates(builder, request())
    assert stable == first
    assert not any(line.startswith("- ") for line in updates.splitlines()), updates
    assert [line[2:] for line in updates.splitlines() if line.startswith("+ ")] == additions


def test_untruncated_agents_still_reports_real_deleted_line(scene):
    builder, paths, _ = scene
    first, _ = prefix_and_updates(builder, request())
    paths["agents"].write_text("# 初始\n", encoding="utf-8")
    assert not builder.persona_repository.snapshot()["agents"].diagnostic.truncated
    stable, updates = prefix_and_updates(builder, request())
    assert stable == first
    assert "- - 原有行" in updates.splitlines()


def test_truncated_target_overflow_counts_additions_only(scene, monkeypatch):
    from agent_py_agent.agent.prompting_parts import thread_persona

    builder, paths, _ = scene
    paths["agents"].write_text("\n".join(f"规则{i:04d}" + "." * 80 for i in range(600)) + "\n", encoding="utf-8")
    prefix_and_updates(builder, request())
    with paths["agents"].open("a", encoding="utf-8") as file:
        file.write("\n".join(f"新增{i:04d}" + "." * 80 for i in range(600, 602)) + "\n")
    monkeypatch.setattr(thread_persona, "PERSONA_UPDATES_MAX_CHARS", 170)
    _, updates = prefix_and_updates(builder, request())
    assert "agents: 新增 2 行" in updates
    assert "删除" not in updates
    assert "下次压缩后整体生效" in updates
    assert len(updates) <= 170


def test_delete_replace_and_overflow_are_grouped_bounded(scene):
    builder, paths, _ = scene
    prefix_and_updates(builder, request())
    paths["user"].write_text("# 初始\n- 修改行\n", encoding="utf-8")
    _, updates = prefix_and_updates(builder, request())
    assert "user" in updates and "- 原有行" in updates and "- 修改行" in updates
    paths["agents"].write_text("- 新规则" + "长" * 5000, encoding="utf-8")
    _, updates = prefix_and_updates(builder, request())
    assert len(updates) <= 4000
    assert "下次压缩后整体生效" in updates
    assert "长" * 100 not in updates


def test_threads_do_not_share_snapshots(scene):
    builder, paths, _ = scene
    first, _ = prefix_and_updates(builder, request())
    paths["agents"].write_text("- 新线程规则\n", encoding="utf-8")
    other, updates = prefix_and_updates(builder, request(thread="thread-two"))
    assert first != other and not updates
    same, updates = prefix_and_updates(builder, request())
    assert same == first and "新线程规则" in updates


def test_config_quoted_false_and_catalog(tmp_path):
    from agent_py_agent.agent.settings.config import load_config
    from agent_py_agent.agent.settings.parameter_registry import parameter_registry

    path = tmp_path / "config.yaml"
    path.write_text('thread_prompt_prefix_freeze_enabled: "false"\n', encoding="utf-8")
    assert load_config(path).thread_prompt_prefix_freeze_enabled is False
    spec = parameter_registry()["thread_prompt_prefix_freeze_enabled"]
    assert spec.default is True and spec.value_type == "bool"
    assert "AGENTS/SOUL/USER" in spec.description


@pytest.mark.parametrize("source", ["gateway", "subagent", "background"])
def test_shared_runtime_entry_and_projection_have_same_tail(scene, source):
    from agent_py_agent.agent.agent_core.tool_request_projection import (
        ToolLoopRequestInput,
        project_tool_loop_request,
    )
    from agent_py_agent.agent.model_request_selection import render_selected_request
    from agent_py_agent.agent.prompting_parts.builder import render_prepared_prompt
    from agent_py_agent.agent.tooling.runtime_contracts import (
        ProviderToolCapability,
        ToolChoice,
        ToolProtocolSnapshot,
    )

    builder, paths, _ = scene
    thread = SimpleNamespace(compact_generation=0, model_profile_id="m1")
    store = SimpleNamespace(threads=SimpleNamespace(load=lambda _id: thread))
    agent = SimpleNamespace(config=builder.config, prompts=builder, conversation_store=store)
    params = SimpleNamespace(task_attributes={"conversation_thread_id": "thread-one"}, source=source,
                             tool_protocol_snapshot=SimpleNamespace(source_protocol="native"))
    value = request()
    first = render_selected_request(agent, params, value)
    paths["agents"].write_text("- 当轮新规则\n", encoding="utf-8")
    second = render_selected_request(agent, params, value)
    assert prompt_cache_layout(first).stable_prefix == prompt_cache_layout(second).stable_prefix
    assert "当轮新规则" in dict(prompt_cache_layout(second).volatile_sections)["prompt.persona_updates"]
    prepared = builder.prepare_render_input(value)
    # 完整工具请求投影只消费同次材料，不重新读人格、线程或候选配置。
    paths["agents"].write_text("- 投影期间再变化\n", encoding="utf-8")
    capability = ProviderToolCapability("test", "https://invalid.test", "test", False, True, "fixture")
    native = ToolProtocolSnapshot(run_id="run-test", capability=capability, source_protocol="native")
    projection = project_tool_loop_request(ToolLoopRequestInput(
        prompt_input=prepared, system_instruction="host", tool_protocol_snapshot=native,
        tool_choice=ToolChoice(), native_tools=(), tool_ir_history=(), provider_history_messages=(),
        tool_context=(), forwarded_guidance=frozenset(), conversation_state=""))
    assert projection.status == "ready"
    assert projection.prompt == render_prepared_prompt(prepared)
    assert "投影期间再变化" not in projection.prompt
    thread.compact_generation = 1
    refreshed = render_selected_request(agent, params, value)
    assert "投影期间再变化" in prompt_cache_layout(refreshed).stable_prefix
    assert "prompt.persona_updates" not in dict(prompt_cache_layout(refreshed).volatile_sections)


@pytest.mark.parametrize("damage", ["json", "schema", "sections", "oversized", "utf8", "value_type"])
def test_invalid_snapshot_is_rebuilt_without_failing_turn_or_logging_content(scene, caplog, damage):
    builder, paths, home = scene
    prefix_and_updates(builder, request())
    path = home / "persona" / "thread_prefix" / "thread-one.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    paths["agents"].write_text("- 当前渲染段DO_NOT_LOG_ME\n", encoding="utf-8")
    corruptions = {
        "json": b'{"DO_NOT_LOG_ME":',
        "schema": json.dumps(saved | {"schema_version": 999}).encode(),
        "sections": json.dumps(saved | {"sections": {"agents": "DO_NOT_LOG_ME"}}).encode(),
        "oversized": b"DO_NOT_LOG_ME" + b" " * (1024 * 1024),
        "utf8": b"DO_NOT_LOG_ME\xff",
        "value_type": json.dumps(saved | {"sections": saved["sections"] | {"agents": 7}}).encode(),
    }
    path.write_bytes(corruptions[damage])
    caplog.set_level(logging.WARNING)
    result, failure = None, None
    try:
        result = prefix_and_updates(builder, request())
    except ValueError as exc:
        failure = exc
    assert failure is None, f"缓存内容损坏不应阻断回合: {failure}"
    stable, updates = result
    assert "当前渲染段DO_NOT_LOG_ME" in stable and not updates
    repaired = json.loads(path.read_text(encoding="utf-8"))
    assert repaired["schema_version"] == 1
    assert "当前渲染段DO_NOT_LOG_ME" in repaired["sections"]["agents"]
    assert path.stat().st_mode & 0o777 == 0o600
    warnings = [row for row in caplog.records if row.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].getMessage() == "thread_id=thread-one reason_code=PERSONA_PREFIX_INVALID"
    assert warnings[0].exc_info is None
    assert "DO_NOT_LOG_ME" not in caplog.text
    assert str(home) not in caplog.text
    caplog.clear()
    assert prefix_and_updates(builder, request()) == result
    assert not caplog.records


def test_snapshot_has_private_mode_and_rejects_symlink(scene):
    builder, paths, home = scene
    prefix_and_updates(builder, request())
    path = home / "persona" / "thread_prefix" / "thread-one.json"
    assert path.stat().st_mode & 0o777 == 0o600
    linked = path.parent / "thread-link.json"
    linked.symlink_to(paths["agents"])
    with pytest.raises(ValueError, match="PERSONA_PREFIX_SYMLINK"):
        prefix_and_updates(builder, request(thread="thread-link"))


def test_compact_preparation_uses_same_frozen_persona_and_tail(scene, monkeypatch):
    from agent_py_agent.agent.agent_core.runtime import loop_support
    from agent_py_agent.agent.conversation import compact_provider_surface as surface

    builder, paths, _ = scene
    builder.config.model_backend = "responses"
    first, _ = prefix_and_updates(builder, request())
    paths["agents"].write_text("- 压缩前新规则\n", encoding="utf-8")
    thread = SimpleNamespace(compact_generation=0, model_profile_id="m1")
    agent = SimpleNamespace(config=builder.config, prompts=builder, tools=SimpleNamespace(), backend=None,
                            conversation_store=SimpleNamespace(threads=SimpleNamespace(load=lambda _id: thread)))
    monkeypatch.setattr(loop_support, "_tool_snapshots_for_run",
                        lambda *_: (None, SimpleNamespace(source_protocol="native")))
    monkeypatch.setattr(loop_support, "_resolve_tool_sections",
                        lambda *_: ("# Tools\n（当前未启用工具）", "推荐"))
    monkeypatch.setattr(surface, "resolve_native_tools", lambda *_: [])
    monkeypatch.setattr(surface, "_compact_capability_presentation", lambda *_: SimpleNamespace(
        tool_snapshot=None, selection=None, selected_skill_ids=None, required_skill_ids=()))
    model = surface.ConversationCompactModelSurface(thread_id="thread-one")
    prepared = surface.prepare_conversation_compact_provider_surface(agent, model, run_id="compact-one")
    assert prepared.stable_prompt_prefix == first
    assert "压缩前新规则" in dict(prepared.volatile_sections)["prompt.persona_updates"]


def test_compact_run_binds_canonical_thread_not_stale_surface(monkeypatch):
    from agent_py_agent.agent.conversation import compact
    from agent_py_agent.agent.conversation.compact_provider_surface import (
        ConversationCompactModelSurface,
    )

    original = ConversationCompactModelSurface(thread_id="stale-thread")
    request = SimpleNamespace(provider_surface=None, model_surface=original, agent=object(), run_id="compact-one",
                              thread=SimpleNamespace(thread_id="canonical-thread"))
    captured = []
    monkeypatch.setattr(compact, "prepare_conversation_compact_provider_surface",
                        lambda agent, surface, *, run_id: captured.append(surface) or "prepared")
    assert compact._resolved_provider_surface(request) == "prepared"
    assert captured[0].thread_id == "canonical-thread"
    assert original.thread_id == "stale-thread"
