"""Behavioral regression suite for selective USER prefix refresh after USER.md dreaming (NW3R1-P1-02)."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.agents_source_repository import AgentsSourceRepository
from agent_py_agent.agent.capability.agents_write_contract import (
    AgentsWriteAuthorization,
    AgentsWriteContext,
    write_intent_digest,
)
from agent_py_agent.agent.capability.persona_repository import PersonaRepository
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.agent.memory_store.user_profile_dream import run_user_profile_dream_if_due
from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.prompting_parts import builder as builder_mod
from agent_py_agent.agent.prompting_parts import thread_persona as thread_persona_mod
from agent_py_agent.agent.prompting_parts.builder import (
    PromptBuilder,
    PromptBuildRequest,
    ToolSections,
)
from agent_py_agent.agent.prompting_parts.cache_layout import prompt_cache_layout
from agent_py_agent.agent.prompting_parts.thread_persona import (
    PERSONA_UPDATES_MAX_CHARS,
    RenderedPersonaSections,
    ThreadPersonaScope,
    _read_persona_epoch,
    expire_thread_user_persona_prefixes,
    freeze_thread_persona,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaEnforcer


def _build_scene(tmp_path: Path, *, with_agents_source: bool = False):
    root = tmp_path / "host"
    owner_home = root / "owners" / "local" / "main"
    owner_home.mkdir(parents=True, exist_ok=True)
    agents_path = owner_home / "AGENTS.md"
    soul_path = owner_home / "SOUL.md"
    user_path = owner_home / "USER.md"
    agents_path.write_text("# AGENTS.md\n- FROZEN_AGENTS_LINE\n", encoding="utf-8")
    soul_path.write_text("# SOUL.md\n- FROZEN_SOUL_LINE\n", encoding="utf-8")
    user_path.write_text("", encoding="utf-8")

    quota = OwnerQuotaEnforcer(owner_home, max_bytes=1024 * 1024 * 1024)
    repo = PersonaRepository(
        owner_home=owner_home,
        soul_path=soul_path,
        user_path=user_path,
        agents_path=agents_path,
        quota_enforcer=quota,
    )
    source = None
    if with_agents_source:
        policy = PathAccessPolicy.from_values(mode="full_access", agent_home_root=root)
        source = AgentsSourceRepository(owner="local/main", owner_home=owner_home, path_policy=policy)
        repo.agents_source = source

    config = AgentConfig(prompt_files=[])
    home_paths = SimpleNamespace(
        owner_home_dir=owner_home,
        owner_agents_md=agents_path,
        owner_soul_md=soul_path,
        owner_user_md=user_path,
    )
    builder = PromptBuilder(config, tmp_path, home_paths=home_paths, persona_repository=repo)
    long_term = JsonlMemory(owner_home / "memory" / "long_term" / "memory.jsonl")
    return SimpleNamespace(
        root=root,
        owner_home=owner_home,
        agents_path=agents_path,
        soul_path=soul_path,
        user_path=user_path,
        repo=repo,
        source=source,
        builder=builder,
        long_term=long_term,
    )


def _prompt_request(thread_id: str = "thread-nw3-r1", *, generation: int = 0) -> PromptBuildRequest:
    req = PromptBuildRequest(
        "继续执行",
        [],
        tools=ToolSections(native_tool_use=True),
        workspace_context_override="固定工作区",
    )
    req.thread_persona = ThreadPersonaScope(
        thread_id=thread_id,
        compact_generation=generation,
        model_profile_id="m1",
        protocol=("responses", "native"),
    )
    return req


def _prefix_and_updates(builder: PromptBuilder, req: PromptBuildRequest) -> tuple[str, str]:
    layout = prompt_cache_layout(builder.build(request=req))
    return layout.stable_prefix, dict(layout.volatile_sections).get("prompt.persona_updates", "")


def _seed_large_user_profile(user_path: Path, *, core_count: int = 48, episodic_count: int = 28) -> list[str]:
    core_entries = [
        f"偏好与常用工程习惯 {idx:02d}: keep structured summaries concise with verified references and clear rollout notes"
        for idx in range(core_count)
    ]
    episodic_entries = [
        f"2026-11-10 临时部署调试记录 {idx:02d}: verified staging artifact hash {idx:02d}"
        for idx in range(episodic_count)
    ]
    lines = ["# USER.md", *[f"- {item}" for item in core_entries], *[f"- {item}" for item in episodic_entries]]
    user_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return core_entries


def _select_local_agents_source(source: AgentsSourceRepository, path: Path) -> None:
    value = {"kind": "local_path", "locator": str(path)}
    snap = source.configuration()
    content_sha = source.load().snapshot.content_sha256
    now = 1000.0
    auth = AgentsWriteAuthorization(
        1,
        "host-auth",
        source.owner,
        source.owner,
        "thread",
        "turn",
        "request",
        "set_source",
        snap.configuration_cas,
        content_sha,
        now - 1,
        now + 100,
        "host-request",
        "",
        write_intent_digest("set_source", value),
    )
    ctx = AgentsWriteContext(
        1,
        source.owner,
        source.owner,
        "thread",
        "turn",
        "request",
        "set_source",
        snap.configuration_cas,
        content_sha,
        now,
        "user_dialogue",
        auth,
        (),
        False,
        False,
        "",
        auth.intent_sha256,
    )
    source.set_source(value["kind"], value["locator"], authorization=auth, context=ctx)


def test_normal_scale_consolidated_user_over_4000_chars_refreshes_fixed_prefix(tmp_path: Path) -> None:
    scene = _build_scene(tmp_path, with_agents_source=True)
    core_entries = _seed_large_user_profile(scene.user_path, core_count=48, episodic_count=28)

    # Freeze an existing thread before dreaming.
    turn0_prefix, turn0_updates = _prefix_and_updates(scene.builder, _prompt_request())
    assert "FROZEN_AGENTS_LINE" in turn0_prefix
    assert "FROZEN_SOUL_LINE" in turn0_prefix
    assert turn0_updates == ""

    snap_path = scene.owner_home / "persona" / "thread_prefix" / "thread-nw3-r1.json"
    pre_dream_snap = json.loads(snap_path.read_text(encoding="utf-8"))
    assert "agents_source" in pre_dream_snap

    # Run periodic dream with default AgentConfig() (70 / 5000 thresholds) and 1 GiB quota.
    report = run_user_profile_dream_if_due(
        owner_home=scene.owner_home,
        persona_repository=scene.repo,
        long_term_store=scene.long_term,
        config=AgentConfig(),
        now=datetime(2026, 11, 15, 14, 0, tzinfo=timezone.utc),
    )
    assert report["status"] == "consolidated"
    assert report["expired_thread_prefixes"] == 1
    post_user = scene.repo.load("user").content
    assert len(post_user) > PERSONA_UPDATES_MAX_CHARS

    pending_snap = json.loads(snap_path.read_text(encoding="utf-8"))
    assert pending_snap["sections"]["user"] == ""
    assert pending_snap["user_refresh_pending"] is True
    assert pending_snap["sections"]["agents"] == pre_dream_snap["sections"]["agents"]
    assert pending_snap["sections"]["soul"] == pre_dream_snap["sections"]["soul"]
    assert pending_snap["agents_source"] == pre_dream_snap["agents_source"]

    # Mutate AGENTS.md and SOUL.md on disk after dream; they must stay frozen on same epoch.
    scene.agents_path.write_text("# AGENTS.md\n- FROZEN_AGENTS_LINE\n- AGENTS_DISK_EDIT_MARKER\n", encoding="utf-8")
    scene.soul_path.write_text("# SOUL.md\n- FROZEN_SOUL_LINE\n- SOUL_DISK_EDIT_MARKER\n", encoding="utf-8")

    # Render two consecutive turns on the same compact_generation / model_profile_id / protocol.
    turn1_prefix, turn1_updates = _prefix_and_updates(scene.builder, _prompt_request())
    turn2_prefix, turn2_updates = _prefix_and_updates(scene.builder, _prompt_request())

    assert turn1_prefix == turn2_prefix
    assert turn1_updates == turn2_updates
    for turn_prefix, turn_updates in ((turn1_prefix, turn1_updates), (turn2_prefix, turn2_updates)):
        for entry in core_entries:
            assert entry in turn_prefix
        assert "变化超过尾部预算" not in turn_updates
        assert "## user" not in turn_updates
        assert "FROZEN_AGENTS_LINE" in turn_prefix
        assert "FROZEN_SOUL_LINE" in turn_prefix
        assert "AGENTS_DISK_EDIT_MARKER" not in turn_prefix
        assert "SOUL_DISK_EDIT_MARKER" not in turn_prefix
        assert "AGENTS_DISK_EDIT_MARKER" in turn_updates
        assert "SOUL_DISK_EDIT_MARKER" in turn_updates

    refreshed_snap = json.loads(snap_path.read_text(encoding="utf-8"))
    assert "user_refresh_pending" not in refreshed_snap
    assert list(refreshed_snap["sections"]) == ["agents", "soul", "user", "diagnostics"]
    assert refreshed_snap["sections"]["agents"] == pre_dream_snap["sections"]["agents"]
    assert refreshed_snap["sections"]["soul"] == pre_dream_snap["sections"]["soul"]
    assert refreshed_snap["agents_source"] == pre_dream_snap["agents_source"]

    # Subsequent manual edit to USER.md emits only that small incremental diff against refreshed prefix.
    with scene.user_path.open("a", encoding="utf-8") as handle:
        handle.write("- 偏好: 新增一条增量偏好\n")
    turn3_prefix, turn3_updates = _prefix_and_updates(scene.builder, _prompt_request())
    assert turn3_prefix == turn2_prefix
    assert "新增一条增量偏好" in turn3_updates
    assert "变化超过尾部预算" not in turn3_updates


def test_stale_render_interleaving_event_controlled_never_consumes_refresh_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = _build_scene(tmp_path)
    core_entries = _seed_large_user_profile(scene.user_path, core_count=48, episodic_count=28)

    # Turn 0 initializes frozen snapshot at persona_epoch=0.
    _prefix_and_updates(scene.builder, _prompt_request(thread_id="thread-interleave"))
    snap_path = scene.owner_home / "persona" / "thread_prefix" / "thread-interleave.json"
    assert _read_persona_epoch(scene.owner_home) == 0

    render_ready = threading.Event()
    dream_done = threading.Event()
    stale_freeze_results: list[tuple[list[str], str]] = []
    stale_errors: list[BaseException] = []

    orig_render = builder_mod._render_persona_sections
    orig_freeze = builder_mod.freeze_thread_persona

    def _pausing_render(*args, **kwargs):
        rendered = orig_render(*args, **kwargs)
        render_ready.set()
        assert dream_done.wait(timeout=10), "timed out waiting for dream consolidation"
        return rendered

    def _capturing_freeze(*args, **kwargs):
        result = orig_freeze(*args, **kwargs)
        stale_freeze_results.append(result)
        return result

    monkeypatch.setattr(builder_mod, "_render_persona_sections", _pausing_render)
    monkeypatch.setattr(builder_mod, "freeze_thread_persona", _capturing_freeze)

    def _run_stale_turn() -> None:
        try:
            scene.builder.build(request=_prompt_request(thread_id="thread-interleave"))
        except BaseException as exc:  # pragma: no cover - defensive capture for thread assertion
            stale_errors.append(exc)

    worker = threading.Thread(target=_run_stale_turn, daemon=True)
    worker.start()
    assert render_ready.wait(timeout=10), "stale render worker never reached freeze barrier"

    # While pre-dream render is paused in-flight, run dream consolidation + expiry.
    report = run_user_profile_dream_if_due(
        owner_home=scene.owner_home,
        persona_repository=scene.repo,
        long_term_store=scene.long_term,
        config=AgentConfig(),
        now=datetime(2026, 11, 15, 14, 0, tzinfo=timezone.utc),
    )
    assert report["status"] == "consolidated"
    assert report["expired_thread_prefixes"] == 1
    assert _read_persona_epoch(scene.owner_home) == 1

    # Repeat expire call before refresh must be a no-op (returns 0).
    assert expire_thread_user_persona_prefixes(scene.owner_home) == 0

    dream_done.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert not stale_errors
    assert stale_freeze_results == [([], "")]

    # Stale pre-dream render MUST NOT have cleared user_refresh_pending or restored pre-dream USER.md.
    snap_after_stale = json.loads(snap_path.read_text(encoding="utf-8"))
    assert snap_after_stale["sections"]["user"] == ""
    assert snap_after_stale["user_refresh_pending"] is True

    monkeypatch.undo()

    # Next turn renders at current persona_epoch -> consumes user_refresh_pending and loads full consolidated USER.
    fresh_prefix, fresh_updates = _prefix_and_updates(scene.builder, _prompt_request(thread_id="thread-interleave"))
    for entry in core_entries:
        assert entry in fresh_prefix
    assert "变化超过尾部预算" not in fresh_updates
    assert "## user" not in fresh_updates
    snap_after_fresh = json.loads(snap_path.read_text(encoding="utf-8"))
    assert "user_refresh_pending" not in snap_after_fresh


def test_ordinary_empty_user_snapshot_without_pending_preserves_freeze_and_delta(tmp_path: Path) -> None:
    scene = _build_scene(tmp_path)
    req = _prompt_request(thread_id="thread-empty-user")

    first_prefix, first_updates = _prefix_and_updates(scene.builder, req)
    assert first_updates == ""
    snap_path = scene.owner_home / "persona" / "thread_prefix" / "thread-empty-user.json"
    initial_snap = json.loads(snap_path.read_text(encoding="utf-8"))
    assert initial_snap["sections"]["user"] == ""
    assert "user_refresh_pending" not in initial_snap

    # Normal USER.md edit without dreaming keeps frozen prefix unchanged and reports delta in persona_updates.
    scene.user_path.write_text("# USER.md\n- 偏好: 普通增量编辑测试\n", encoding="utf-8")
    second_prefix, second_updates = _prefix_and_updates(scene.builder, req)
    assert second_prefix == first_prefix
    assert "普通增量编辑测试" in second_updates
    assert "## user" in second_updates

    # If that originally empty-USER thread later grows >70 entries and dreams, selective refresh activates cleanly.
    core_entries = _seed_large_user_profile(scene.user_path, core_count=48, episodic_count=28)
    report = run_user_profile_dream_if_due(
        owner_home=scene.owner_home,
        persona_repository=scene.repo,
        long_term_store=scene.long_term,
        config=AgentConfig(),
        now=datetime(2026, 11, 15, 14, 0, tzinfo=timezone.utc),
    )
    assert report["status"] == "consolidated"
    assert report["expired_thread_prefixes"] == 1
    assert expire_thread_user_persona_prefixes(scene.owner_home) == 0

    refreshed_prefix, refreshed_updates = _prefix_and_updates(scene.builder, req)
    for entry in core_entries:
        assert entry in refreshed_prefix
    assert "变化超过尾部预算" not in refreshed_updates
    assert "## user" not in refreshed_updates


def test_pending_agents_source_stays_pending_and_epoch_bump_clears_refresh_flag(tmp_path: Path) -> None:
    scene = _build_scene(tmp_path, with_agents_source=True)
    core_entries = _seed_large_user_profile(scene.user_path, core_count=48, episodic_count=28)

    first_prefix, _ = _prefix_and_updates(scene.builder, _prompt_request(thread_id="thread-source-pending"))
    assert "FROZEN_AGENTS_LINE" in first_prefix

    # Switch agents_source to a new local_path; for epoch generation=0 it must remain pending.
    new_agents = scene.owner_home / "pending-rules.md"
    new_agents.write_text("# AGENTS.md\n- PENDING_AGENTS_MARKER\n", encoding="utf-8")
    _select_local_agents_source(scene.source, new_agents)

    report = run_user_profile_dream_if_due(
        owner_home=scene.owner_home,
        persona_repository=scene.repo,
        long_term_store=scene.long_term,
        config=AgentConfig(),
        now=datetime(2026, 11, 15, 14, 0, tzinfo=timezone.utc),
    )
    assert report["status"] == "consolidated"
    assert report["expired_thread_prefixes"] == 1

    # Same-epoch turn refreshes USER without activating or exposing pending agents_source.
    same_epoch_prefix, same_epoch_updates = _prefix_and_updates(
        scene.builder, _prompt_request(thread_id="thread-source-pending", generation=0)
    )
    for entry in core_entries:
        assert entry in same_epoch_prefix
    assert "FROZEN_AGENTS_LINE" in same_epoch_prefix
    assert "PENDING_AGENTS_MARKER" not in same_epoch_prefix
    assert "PENDING_AGENTS_MARKER" not in same_epoch_updates

    # Also verify epoch transition while user_refresh_pending=True replaces snapshot cleanly without stale flag.
    assert expire_thread_user_persona_prefixes(scene.owner_home) == 1
    snap_path = scene.owner_home / "persona" / "thread_prefix" / "thread-source-pending.json"
    assert json.loads(snap_path.read_text(encoding="utf-8"))["user_refresh_pending"] is True

    next_epoch_prefix, next_epoch_updates = _prefix_and_updates(
        scene.builder, _prompt_request(thread_id="thread-source-pending", generation=1)
    )
    assert "PENDING_AGENTS_MARKER" in next_epoch_prefix
    assert next_epoch_updates == ""
    assert "user_refresh_pending" not in json.loads(snap_path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("bad_flag", ["yes", 1, None, []])
def test_malformed_user_refresh_pending_rebuilds_snapshot_without_logging_content(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, bad_flag: object
) -> None:
    scene = _build_scene(tmp_path)
    scene.user_path.write_text("# USER.md\n- 偏好: NO_CONTENT_LEAK_MARKER\n", encoding="utf-8")
    req = _prompt_request(thread_id="thread-bad-flag")
    _prefix_and_updates(scene.builder, req)

    snap_path = scene.owner_home / "persona" / "thread_prefix" / "thread-bad-flag.json"
    saved = json.loads(snap_path.read_text(encoding="utf-8"))
    saved["user_refresh_pending"] = bad_flag
    snap_path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")

    # Verify _read_snapshot treats non-bool user_refresh_pending as invalid and rebuild recovers cleanly.
    caplog.set_level(logging.WARNING)
    prefix, updates = _prefix_and_updates(scene.builder, req)
    assert "NO_CONTENT_LEAK_MARKER" in prefix
    assert updates == ""
    rebuilt = json.loads(snap_path.read_text(encoding="utf-8"))
    assert "user_refresh_pending" not in rebuilt
    warnings = [row for row in caplog.records if row.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].getMessage() == "thread_id=thread-bad-flag reason_code=PERSONA_PREFIX_INVALID"
    assert "NO_CONTENT_LEAK_MARKER" not in caplog.text
    assert thread_persona_mod._read_snapshot(snap_path) is not None


def test_direct_epoch_sequenced_freeze_and_revoked_source_preservation(tmp_path: Path) -> None:
    scene = _build_scene(tmp_path)
    scope = ThreadPersonaScope("thread-direct-seq", 0, "m1", ("responses", "native"))
    frozen_agents = "# AGENTS\n- DIRECT_FROZEN_AGENTS\n"
    frozen_soul = "# SOUL\n- DIRECT_FROZEN_SOUL\n"
    pre_user = "# USER\n- pre-dream line\n"
    post_user = "# USER\n- consolidated preference line\n"

    freeze_thread_persona(
        scene.owner_home,
        scope,
        RenderedPersonaSections(
            sections={"agents": frozen_agents, "soul": frozen_soul, "user": pre_user, "diagnostics": ""},
            truncated_targets=frozenset(),
            persona_epoch=_read_persona_epoch(scene.owner_home),
        ),
    )
    pre_epoch = _read_persona_epoch(scene.owner_home)
    assert expire_thread_user_persona_prefixes(scene.owner_home) == 1
    curr_epoch = _read_persona_epoch(scene.owner_home)
    assert curr_epoch > pre_epoch

    snap_path = scene.owner_home / "persona" / "thread_prefix" / "thread-direct-seq.json"
    assert json.loads(snap_path.read_text(encoding="utf-8"))["user_refresh_pending"] is True

    # Stale pre-dream render with pre_epoch must be rejected and leave user_refresh_pending intact.
    stale_sections, stale_updates = freeze_thread_persona(
        scene.owner_home,
        scope,
        RenderedPersonaSections(
            sections={"agents": frozen_agents, "soul": frozen_soul, "user": pre_user, "diagnostics": ""},
            truncated_targets=frozenset(),
            persona_epoch=pre_epoch,
        ),
    )
    assert stale_sections == []
    assert stale_updates == ""
    still_pending = json.loads(snap_path.read_text(encoding="utf-8"))
    assert still_pending["user_refresh_pending"] is True
    assert still_pending["sections"]["user"] == ""

    # Current-epoch render consumes user_refresh_pending, loads post_user, and preserves frozen agents/soul.
    fresh_sections, fresh_updates = freeze_thread_persona(
        scene.owner_home,
        scope,
        RenderedPersonaSections(
            sections={"agents": "# NEW_DISK_AGENTS", "soul": "# NEW_DISK_SOUL", "user": post_user, "diagnostics": ""},
            truncated_targets=frozenset(),
            persona_epoch=curr_epoch,
        ),
    )
    assert fresh_sections == [frozen_agents, frozen_soul, post_user]
    assert "## user" not in fresh_updates
    assert "NEW_DISK_AGENTS" in fresh_updates
    assert "user_refresh_pending" not in json.loads(snap_path.read_text(encoding="utf-8"))
