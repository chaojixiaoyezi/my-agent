"""NW3 W6 verification: INIT-CTX A2/A5/W3-dup, 2B lesson-<hex> filter, 1B signal-driven mount, 3B stopwords."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import WorkflowDefinition, WorkflowStore
from agent_py_agent.agent.capability.method_tree import (
    is_opaque_undescribed_lesson_skill,
    render_method_catalog,
)
from agent_py_agent.agent.capability.router import (
    _SKILL_USAGE_INSTRUCTIONS,
    CapabilityCard,
    CapabilityRouter,
)
from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshot, SkillSnapshotEntry
from agent_py_agent.agent.capability.turn_recommendations import (
    _english_stopwords as REC_STOPWORDS,
)
from agent_py_agent.agent.capability.turn_recommendations import (
    build_recommendation_index,
    render_turn_recommendations,
)
from agent_py_agent.agent.conversation.tool_discovery import mount_thread_asset_tools
from agent_py_agent.agent.tooling.models import (
    KeywordToolSearchProvider,
    ToolModelHints,
)
from agent_py_agent.agent.tooling.models import (
    _english_stopwords as TOOL_STOPWORDS,
)
from agent_py_agent.agent.tooling.registry import MIN_RECOMMENDED_TOOL_SCORE
from agent_py_agent.tests.test_compactcall_websocket import _setup
from agent_py_agent.tests.test_tool_registry import make_test_model_spec


def _skill_desc(name: str, *, source: str = "owner", summary: str = "", stable_id: str = "", refs: dict | None = None) -> dict:
    sid = stable_id or f"{source}:{name}"
    return {
        "kind": "skill",
        "id": name,
        "name": name,
        "summary": summary,
        "enabled": True,
        "source_scope": source,
        "refs": refs or {},
        "metadata": {"stable_id": sid, "source": source},
    }


def _script_desc(asset_id: str, *, name: str = "", summary: str = "脚本说明", metadata: dict | None = None) -> dict:
    return {
        "kind": "script",
        "id": asset_id,
        "name": name or asset_id,
        "summary": summary,
        "enabled": True,
        "source_scope": "owner",
        "refs": {},
        "metadata": dict(metadata or {}),
    }


def _workflow_desc(asset_id: str, *, name: str = "", summary: str = "工作流说明", refs: dict | None = None, metadata: dict | None = None) -> dict:
    return {
        "kind": "workflow",
        "id": asset_id,
        "name": name or asset_id,
        "summary": summary,
        "enabled": True,
        "source_scope": "owner",
        "refs": dict(refs or {}),
        "metadata": dict(metadata or {}),
    }


# ---------------------------------------------------------------------------
# INIT-CTX A5: Omit empty-list fields from serialized recommendation JSON
# ---------------------------------------------------------------------------

def test_a5_omits_empty_lists_in_serialized_recommendations_without_mutating_cache(tmp_path):
    rows = [
        _script_desc("check-dns", name="DNS 核对"),
        _workflow_desc("triage-flow", name="分诊流程", refs={"scripts": ["check-dns"]}),
    ]
    index = build_recommendation_index(rows)
    before_match = index.match("check-dns")
    assert len(before_match) == 1
    assert before_match[0]["bound_methods"] == []
    assert before_match[0]["monitor_aliases"] == []

    rendered = render_turn_recommendations("check-dns", rows, owner_home=tmp_path)
    json_lines = [json.loads(line[2:]) for line in rendered.splitlines() if line.startswith("- {")]
    assert len(json_lines) == 1
    card = json_lines[0]
    for list_field in ("inputs", "outputs", "known_pitfalls", "bound_methods", "monitor_aliases"):
        assert list_field not in card

    # Cached RecommendationIndex entries remain completely intact after rendering.
    after_match = index.match("check-dns")
    assert after_match[0]["bound_methods"] == []
    assert after_match[0]["monitor_aliases"] == []

    # Non-empty lists and monitor_aliases badge lines are preserved.
    rendered_wf = render_turn_recommendations("triage-flow", rows, owner_home=tmp_path)
    wf_lines = [json.loads(line[2:]) for line in rendered_wf.splitlines() if line.startswith("- {")]
    assert wf_lines[0]["bound_methods"] == ["script:check-dns"]

    rows_with_monitor = [
        *rows,
        {
            "kind": "monitor",
            "id": "dns-mon",
            "name": "DNS 告警",
            "enabled": True,
            "metadata": {"alias": "dns-alert"},
            "refs": [{"kind": "script", "id": "check-dns"}],
        },
    ]
    rendered_mon = render_turn_recommendations("dns-alert", rows_with_monitor, owner_home=tmp_path)
    mon_lines = [json.loads(line[2:]) for line in rendered_mon.splitlines() if line.startswith("- {")]
    assert mon_lines[0]["monitor_aliases"] == ["dns-alert"]
    assert "⏰ dns-alert" in rendered_mon


# ---------------------------------------------------------------------------
# PD-IC5 / 3B: English stop-word filter in turn_recommendations & tooling
# ---------------------------------------------------------------------------

def test_3b_turn_recommendations_ignores_english_stopwords_and_keeps_exact_matches():
    assert {"the", "in", "for", "with", "use", "using", "you", "your"} <= REC_STOPWORDS
    rows = [
        _script_desc(
            "use-for-log",
            name="The Tool In Use",
            metadata={"trigger_matchers": {"intent_keywords": ["for", "the", "with"]}},
        ),
        _script_desc(
            "how-to",
            name="how-to",
            metadata={"trigger_matchers": {"intent_keywords": ["in", "troubleshoot"]}},
        ),
    ]
    index = build_recommendation_index(rows)
    assert index.match("Hello, how are you? What can I use with this for our work in the chat?") == []
    assert render_turn_recommendations("I am using this for my test in the box", rows) == ""

    # Exact composite identifier and non-stopword keyword/token still match cleanly.
    hit_composite = index.match("please run how-to")
    assert [row["id"] for row in hit_composite] == ["how-to"]

    hit_non_stopword = index.match("need to troubleshoot log issue")
    assert {row["id"] for row in hit_non_stopword} == {"use-for-log", "how-to"}


def test_3b_tooling_search_blocks_english_stopword_prose_hits_and_keeps_exact_matches():
    assert {"the", "in", "for", "with", "use", "using", "is", "are"} <= TOOL_STOPWORDS
    provider = KeywordToolSearchProvider()
    noise_spec = make_test_model_spec(
        "cancel_session_task",
        description="Use this tool for checking what is in the session with your account.",
        category="orchestration",
    )
    user_cfg_spec = make_test_model_spec(
        "user_config",
        description="Manage configuration settings for you and your team.",
        category="system",
    )
    use_named_spec = make_test_model_spec(
        "with",
        description="Exact literal tool name probe",
        category="for",
    )

    explicit_kw_spec = replace(
        make_test_model_spec("probe_kw", description="Generic", category="testing"),
        hints=ToolModelHints(category="testing", keywords=("alpha", "using"), use_cases=("Use this for testing.",)),
    )

    specs = (noise_spec, user_cfg_spec, explicit_kw_spec)
    hits = provider.search("What can I use for this in our work with you?", specs, 10)
    assert all(hit.score < MIN_RECOMMENDED_TOOL_SCORE for hit in hits)

    # Exact non-stopword tool name still scores well above MIN_RECOMMENDED_TOOL_SCORE.
    exact_hits = provider.search("user_config", specs, 10)
    assert exact_hits and exact_hits[0].name == "user_config" and exact_hits[0].score >= 6.0

    # Explicit non-prose stopword as exact tool name or exact explicit keyword still scores.
    exact_stopword_tool = provider.search("with", (use_named_spec,), 10)
    assert exact_stopword_tool and exact_stopword_tool[0].score >= 6.0

    exact_kw_hit = provider.search("using", (explicit_kw_spec,), 10)
    assert exact_kw_hit and exact_kw_hit[0].score >= 4.0


# ---------------------------------------------------------------------------
# INIT-CTX A2 + W3-dup + 2B: Level-0 catalog slimming & lesson-<hex> filter
# ---------------------------------------------------------------------------

def test_a2_omits_redundant_skill_id_when_unique_and_keeps_on_collision_or_noncanonical():
    rows = [
        _skill_desc("unique-skill", source="owner", summary="唯一技能说明"),
        _skill_desc("shared-name", source="owner", summary="Owner 版本"),
        _skill_desc("shared-name", source="builtin", summary="Builtin 版本"),
        _skill_desc("custom-id", source="workspace", summary="自定义 ID", stable_id="workspace:alias-id"),
        _workflow_desc("flow-1"),
    ]
    text = render_method_catalog(rows, skill_usage_instructions=_SKILL_USAGE_INSTRUCTIONS)["text"]
    assert "- unique-skill: 唯一技能说明\n" in text
    assert "(skill_id: owner:unique-skill)" not in text
    assert "- shared-name: Owner 版本 (skill_id: owner:shared-name)" in text
    assert "- shared-name: Builtin 版本 (skill_id: builtin:shared-name)" in text
    assert "- custom-id: 自定义 ID (skill_id: workspace:alias-id)" in text


def test_w3_dup_usage_instructions_removes_duplicate_discovery_line_and_search_get_accepts_name(tmp_path):
    assert "- 发现：上面列的是当前轮可用 Skill 的名称、说明和稳定 skill_id" not in _SKILL_USAGE_INSTRUCTIONS
    assert "skill_search(action=get" in _SKILL_USAGE_INSTRUCTIONS
    assert "<source>:<name>" in _SKILL_USAGE_INSTRUCTIONS

    skill_dir = tmp_path / "home" / "owners" / "local" / "main" / "skills" / "pdf-tool"
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text("---\nname: pdf-tool\ndescription: 处理 PDF\n---\nFULL-PDF-BODY\n", encoding="utf-8")

    entry = SkillSnapshotEntry(
        stable_id="owner:pdf-tool",
        name="pdf-tool",
        description="处理 PDF",
        path=str(skill_path),
        source="owner",
        category="owner",
        content_sha256=hashlib.sha256(skill_path.read_bytes()).hexdigest(),
    )
    snapshot = SkillSnapshot((entry,), (), "frozen", "local/main", str(tmp_path))
    router = CapabilityRouter(skill_snapshot=snapshot)
    tool = SkillSearchTool(SimpleNamespace(capability_router=router, current_skill_snapshot=lambda: snapshot))

    by_name = tool.execute({"action": "get", "skill_id": "pdf-tool"})
    by_stable = tool.execute({"action": "get", "skill_id": "owner:pdf-tool"})
    assert by_name.ok and "FULL-PDF-BODY" in by_name.output
    assert by_stable.ok and "FULL-PDF-BODY" in by_stable.output


def test_2b_excludes_opaque_undescribed_lesson_skills_from_level0_unless_pinned(tmp_path):
    assert is_opaque_undescribed_lesson_skill("lesson-abcdef123456", "")
    assert is_opaque_undescribed_lesson_skill("lesson-abcdef123456", "lesson-abcdef123456")
    assert not is_opaque_undescribed_lesson_skill("lesson-abcdef123456", "验证失败路径后再交付")
    assert not is_opaque_undescribed_lesson_skill("lesson-readable-slug", "")

    entries = []
    for name, desc in (
        ("lesson-deadbeef1234", ""),
        ("lesson-cafebabe5678", "lesson-cafebabe5678"),
        ("lesson-0123456789ab", "有明确描述的经验技能"),
        ("normal-skill", "普通技能"),
    ):
        sdir = tmp_path / "skills" / name
        sdir.mkdir(parents=True)
        spath = sdir / "SKILL.md"
        desc_line = f"description: {json.dumps(desc, ensure_ascii=False)}\n" if desc else ""
        spath.write_text(f"---\nname: {name}\n{desc_line}---\nBODY-{name}\n", encoding="utf-8")
        entries.append(SkillSnapshotEntry(
            stable_id=f"owner:{name}",
            name=name,
            description=desc,
            path=str(spath),
            source="owner",
            category="owner",
            content_sha256=hashlib.sha256(spath.read_bytes()).hexdigest(),
        ))

    snapshot = SkillSnapshot(tuple(entries), (), "frozen", "local/main", str(tmp_path))
    # Add a workflow card so both the method-tree path and non-method fallback path are verified.
    wf_card = CapabilityCard("workflow:owner:wf-1", "workflow", "wf-1", "工作流", source="owner", metadata={"stable_id": "workflow:owner:wf-1", "asset_id": "wf-1"})

    for extra in ((), (wf_card,)):
        router = CapabilityRouter(skill_snapshot=snapshot, extra_cards=list(extra))
        default_text = router.render_skill_metadata_index()
        assert "lesson-deadbeef1234" not in default_text
        assert "lesson-cafebabe5678" not in default_text
        assert "lesson-0123456789ab" in default_text
        assert "normal-skill" in default_text

        selected_text = router.render_skill_metadata_index(selected_skill_ids=("owner:lesson-deadbeef1234",))
        assert "lesson-deadbeef1234" in selected_text
        assert "lesson-cafebabe5678" not in selected_text

        required_text = router.render_skill_metadata_index(required_skill_ids=("owner:lesson-deadbeef1234",))
        assert "lesson-deadbeef1234" in required_text

        in_use_text = router.render_skill_metadata_index(in_use_method_ids=("owner:lesson-cafebabe5678",))
        assert "lesson-cafebabe5678" in in_use_text

    # Opaque lesson skills remain searchable and readable via skill_search.
    tool = SkillSearchTool(SimpleNamespace(capability_router=CapabilityRouter(skill_snapshot=snapshot), current_skill_snapshot=lambda: snapshot))
    search_res = tool.execute({"action": "search", "query": "lesson-deadbeef1234"})
    assert search_res.ok and "owner:lesson-deadbeef1234" in search_res.output
    get_res = tool.execute({"action": "get", "skill_id": "owner:lesson-deadbeef1234"})
    assert get_res.ok and "BODY-lesson-deadbeef1234" in get_res.output


# ---------------------------------------------------------------------------
# PD-IC1 / 1B: Signal-driven Turn-1 mount_thread_asset_tools
# ---------------------------------------------------------------------------

def _setup_asset_thread(tmp_path, monkeypatch, *, with_script: bool = True, with_workflow: bool = True):
    agent, thread, _prior, _records = _setup(tmp_path, monkeypatch, False)
    agent.tools.catalog_default_deferral_enabled = True
    home = agent.home_paths.owner_home_dir
    if with_script:
        ScriptStore(home).save_script(
            AssetDescriptor("fixed-query", "script", "固定脚本", metadata={"trigger_matchers": {"intent_keywords": ["脚本触发词"]}}),
            "raise AssertionError('never run')",
        )
    if with_workflow:
        WorkflowStore(home).save_workflow(
            WorkflowDefinition(
                AssetDescriptor("nightly-sync", "workflow", "夜间工作流", metadata={"trigger_matchers": {"intent_keywords": ["流程触发词"]}}),
                (),
                body_markdown="",
            )
        )
    snapshot = agent.tools.runtime_snapshot()
    return agent, thread, snapshot


def test_1b_turn1_plain_greeting_mounts_nothing_and_locks_midturn_prefix(tmp_path, monkeypatch):
    agent, thread, snapshot = _setup_asset_thread(tmp_path, monkeypatch)

    turn1 = SimpleNamespace(source="user", context_scope="conversation", allowed_tools=None,
                            task_attributes={"conversation_thread_id": thread.thread_id}, user_prompt="Hello, how are you today?")
    assert mount_thread_asset_tools(agent, turn1, runtime_snapshot=snapshot) == ()
    stored = agent.conversation_store.threads.require(thread.thread_id)
    assert stored.metadata.get("asset_mount_evaluated") is True
    assert stored.metadata.get("pinned_tool_names", []) == []

    # Turn 2 explicitly mentions assets, yet Turn-1 evaluated lock keeps prefix cache stable.
    turn2 = SimpleNamespace(source="user", context_scope="conversation", allowed_tools=None,
                            task_attributes={"conversation_thread_id": thread.thread_id}, user_prompt="请运行 nightly-sync 和 fixed-query")
    assert mount_thread_asset_tools(agent, turn2, runtime_snapshot=snapshot) == ()


def test_1b_preserves_existing_thread_pinned_tool_names(tmp_path, monkeypatch):
    agent, thread, snapshot = _setup_asset_thread(tmp_path, monkeypatch)

    def seed_old_pins(current):
        meta = dict(current.metadata)
        meta["pinned_tool_names"] = ["scripts", "workflows"]
        return replace(current, loaded_tool_names=("scripts", "workflows"), metadata=meta)

    agent.conversation_store.threads.update_atomic(thread.thread_id, seed_old_pins)
    turn = SimpleNamespace(source="user", context_scope="conversation", allowed_tools=None,
                           task_attributes={"conversation_thread_id": thread.thread_id}, user_prompt="继续")
    mounted = mount_thread_asset_tools(agent, turn, runtime_snapshot=snapshot)
    assert mounted == ("scripts", "workflows")
    stored = agent.conversation_store.threads.require(thread.thread_id)
    assert stored.metadata["pinned_tool_names"] == ["scripts", "workflows"]
    assert stored.metadata["asset_mount_evaluated"] is True


@pytest.mark.parametrize(
    "case,attrs,prompt,expected",
    [
        ("task_attr_workflow", {"workflow_id": "nightly-sync"}, "继续", ("workflows",)),
        ("task_attr_script", {"script_id": "fixed-query"}, "继续", ("scripts",)),
        ("task_attr_work_kind_wf", {"work_kind": "workflow_run"}, "继续", ("workflows",)),
        ("rec_keyword_wf", {}, "需要流程触发词排查", ("workflows",)),
        ("rec_keyword_sc", {}, "需要脚本触发词排查", ("scripts",)),
        ("prompt_mention_wf", {}, "请运行 nightly-sync", ("workflows",)),
        ("prompt_mention_sc", {}, "请运行 fixed-query", ("scripts",)),
        ("prompt_literal_tools", {}, "查看 workflows 和 scripts", ("scripts", "workflows")),
    ],
)
def test_1b_turn1_structured_signals_pin_only_matching_asset_tool(tmp_path, monkeypatch, case, attrs, prompt, expected):
    agent, thread, snapshot = _setup_asset_thread(tmp_path, monkeypatch)
    params = SimpleNamespace(
        source="user",
        context_scope="conversation",
        allowed_tools=None,
        task_attributes={"conversation_thread_id": thread.thread_id, **attrs},
        user_prompt=prompt,
    )
    mounted = mount_thread_asset_tools(agent, params, runtime_snapshot=snapshot)
    assert mounted == expected, f"case {case} expected {expected}, got {mounted}"
    stored = agent.conversation_store.threads.require(thread.thread_id)
    assert stored.metadata["pinned_tool_names"] == list(expected)
    assert stored.metadata["asset_mount_evaluated"] is True


def test_1b_turn1_in_use_skill_binding_pins_only_bound_kind(tmp_path, monkeypatch):
    agent, thread, snapshot = _setup_asset_thread(tmp_path, monkeypatch)
    sdir = agent.home_paths.owner_home_dir / "skills" / "wf-only-skill"
    sdir.mkdir(parents=True)
    spath = sdir / "SKILL.md"
    spath.write_text("---\nname: wf-only-skill\ndescription: 只绑定工作流\n---\nBODY\n", encoding="utf-8")
    entry = SkillSnapshotEntry(
        stable_id="owner:wf-only-skill",
        name="wf-only-skill",
        description="只绑定工作流",
        path=str(spath),
        source="owner",
        category="owner",
        content_sha256=hashlib.sha256(spath.read_bytes()).hexdigest(),
        refs={"workflows": ["nightly-sync"]},
    )
    agent._current_skill_snapshot = SkillSnapshot((entry,), (), "frozen", "local/main", str(tmp_path))
    agent.current_skill_snapshot = lambda: agent._current_skill_snapshot

    params = SimpleNamespace(
        source="user",
        context_scope="conversation",
        allowed_tools=None,
        task_attributes={"conversation_thread_id": thread.thread_id},
        user_prompt="按之前方式继续",
        in_use_method_ids=("owner:wf-only-skill",),
    )
    mounted = mount_thread_asset_tools(agent, params, runtime_snapshot=snapshot)
    assert mounted == ("workflows",)
