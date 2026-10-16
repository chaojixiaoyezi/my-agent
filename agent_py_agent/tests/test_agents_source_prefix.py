"""从唯一来源仓库经真实PromptBuilder验证默认兼容、pending/active与撤权。"""
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.persona_repository import PersonaRepository
from agent_py_agent.agent.prompting_parts.builder import PromptBuilder
from agent_py_agent.agent.prompting_parts.thread_persona import _read_persona_epoch
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_agents_source_repository import choose
from agent_py_agent.tests.test_agents_source_repository import source as source
from agent_py_agent.tests.test_thread_persona_freeze import prefix_and_updates, request


@pytest.fixture
def scene(source):
    for name in ("USER.md", "SOUL.md"):
        (source.owner_home / name).write_text("# 标题\n- 保留用户人格。\n")
    repository = PersonaRepository(owner_home=source.owner_home, agents_path=source.default_path,
                                   user_path=source.owner_home / "USER.md", soul_path=source.owner_home / "SOUL.md")
    # 让未接线版本也能跑到可见正文断言，不用导入错误或构造器签名错误充当TDD红。
    repository.agents_source = source
    paths = SimpleNamespace(owner_home_dir=source.owner_home)
    builder = PromptBuilder(AgentConfig(prompt_files=[]), source.owner_home, home_paths=paths, persona_repository=repository)
    return source, repository, builder


def select(source, name, text):
    path = source.owner_home / name
    path.write_text(text)
    choose(source, path)
    return path


def test_persona_repository_uses_the_one_configured_source(scene):
    source, repository, _ = scene
    select(source, "instructions.rulebook", "# 约定\n- SOURCE_ONLY_MARKER\n")
    document = repository.load("agents")
    assert "SOURCE_ONLY_MARKER" in document.content
    assert document.agents_source.kind == "local_path"


@pytest.mark.parametrize("refresh", [request(1), request(profile="m2"), request(protocol="anthropic")])
def test_new_source_is_pending_without_early_tail_then_activates_at_boundary(scene, refresh):
    source, _, builder = scene
    first, _ = prefix_and_updates(builder, request())
    select(source, "new-name.txt", "# 新来源\n- PENDING_SOURCE_MARKER\n")
    pending, updates = prefix_and_updates(builder, request())
    assert pending == first
    assert "PENDING_SOURCE_MARKER" not in updates, "pending新来源不得提前从动态尾巴生效"
    active, updates = prefix_and_updates(builder, refresh)
    assert "PENDING_SOURCE_MARKER" in active
    assert not updates


def test_default_prefix_bytes_are_legacy_compatible_with_crlf_and_bom(scene):
    source, repository, builder = scene
    old = PersonaRepository(owner_home=source.owner_home, agents_path=source.default_path,
                            user_path=source.owner_home / "USER.md", soul_path=source.owner_home / "SOUL.md")
    legacy = PromptBuilder(builder.config, builder.root, home_paths=builder.home_paths, persona_repository=old)
    assert builder.read_home_context("") == legacy.read_home_context("")
    assert repository.load("agents").agents_source.content_sha256 == source.load().snapshot.content_sha256


def test_repeated_load_time_does_not_refresh_epoch_or_snapshot(scene):
    source, _, builder = scene
    select(source, "stable", "# 稳定来源\n- SAME_SOURCE_MARKER\n")
    first, _ = prefix_and_updates(builder, request())
    path = source.owner_home / "persona/thread_prefix/thread-one.json"
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    for _ in range(3):
        prefix, updates = prefix_and_updates(builder, request())
        assert prefix == first and not updates
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == mtime
    assert _read_persona_epoch(source.owner_home) == 0


def test_active_source_revoke_drops_old_bytes_once_and_blocks_old_render(scene, monkeypatch):
    source, _, builder = scene
    select(source, "active", "# 可撤回来源\n- REVOKED_SOURCE_MARKER\n")
    first, _ = prefix_and_updates(builder, request())
    assert "REVOKED_SOURCE_MARKER" in first
    from agent_py_agent.agent.prompting_parts import builder as module
    original = module._render_persona_sections
    def render_then_revoke(*args, **kwargs):
        rendered = original(*args, **kwargs)
        source._enabled = lambda: False
        return rendered
    monkeypatch.setattr(module, "_render_persona_sections", render_then_revoke)
    prefix, updates = prefix_and_updates(builder, request())
    assert "REVOKED_SOURCE_MARKER" not in prefix + updates
    assert _read_persona_epoch(source.owner_home) == 1
    saved = json.loads((source.owner_home / "persona/thread_prefix/thread-one.json").read_text())
    assert "REVOKED_SOURCE_MARKER" not in saved["sections"]["agents"]
    prefix_and_updates(builder, request())
    assert _read_persona_epoch(source.owner_home) == 1


def test_active_source_parent_replacement_invalidates_frozen_body(scene):
    source, _, builder = scene
    directory = source.owner_home / "source-dir"
    directory.mkdir()
    path = select(source, "source-dir/rules.bin", "# 约定\n- REPLACED_PARENT_MARKER\n")
    first, _ = prefix_and_updates(builder, request())
    assert "REPLACED_PARENT_MARKER" in first
    body = path.read_bytes()
    directory.rename(source.owner_home / "old-dir")
    directory.mkdir()
    path.write_bytes(body)
    prefix, updates = prefix_and_updates(builder, request())
    assert "REPLACED_PARENT_MARKER" not in prefix + updates
    assert _read_persona_epoch(source.owner_home) == 1


@pytest.mark.parametrize("prior", [False, True])
def test_first_or_new_boundary_does_not_activate_render_revoked_after_materialization(scene, monkeypatch, prior):
    source, _, builder = scene
    if prior:
        prefix_and_updates(builder, request())
    select(source, "before-revoke", "# 约定\n- INFLIGHT_REVOKE_MARKER\n")
    from agent_py_agent.agent.prompting_parts import builder as module
    original = module._render_persona_sections
    def render_then_revoke(*args, **kwargs):
        rendered = original(*args, **kwargs)
        source._enabled = lambda: False
        return rendered
    monkeypatch.setattr(module, "_render_persona_sections", render_then_revoke)
    prefix, updates = prefix_and_updates(builder, request(1) if prior else request())
    assert "INFLIGHT_REVOKE_MARKER" not in prefix + updates
    assert _read_persona_epoch(source.owner_home) == 1
    prefix_and_updates(builder, request(1) if prior else request())
    assert _read_persona_epoch(source.owner_home) == 1


@pytest.mark.parametrize("explicit_default", [False, True])
def test_authorized_default_persona_edit_keeps_active_prefix_and_source_identity(scene, explicit_default):
    from agent_py_agent.agent.capability.persona_repository import PersonaMutationRequest
    from agent_py_agent.tests.test_agents_source_repository import grant
    source, repository, builder = scene
    if explicit_default:
        value = {"kind": "owner_default", "locator": str(source.default_path)}
        auth, context = grant(source, "set_source", value)
        source.set_source(value["kind"], value["locator"], authorization=auth, context=context)
    first, _ = prefix_and_updates(builder, request())
    inode = source.default_path.stat().st_ino
    repository.mutate(PersonaMutationRequest("agents", "add", content="LEGITIMATE_DEFAULT_EDIT_MARKER"))
    prefix, updates = prefix_and_updates(builder, request())
    assert prefix == first, "合法人格修改不得被误判为撤权并删掉固定AGENTS"
    assert "LEGITIMATE_DEFAULT_EDIT_MARKER" in updates
    assert _read_persona_epoch(source.owner_home) == 0
    assert source.default_path.stat().st_ino == inode
    assert source.load().snapshot.status == "ready"
    source.require_active_permission(source.activation_metadata(source.load().snapshot))


def test_default_persona_ledger_failure_restores_exact_bytes_and_inode(scene, monkeypatch):
    from agent_py_agent.agent.capability import persona_repository as module
    from agent_py_agent.agent.capability.persona_repository import PersonaMutationRequest
    source, repository, builder = scene
    first, _ = prefix_and_updates(builder, request())
    original, inode = source.default_path.read_bytes(), source.default_path.stat().st_ino
    def fail_ledger(*args, **kwargs):
        raise RuntimeError("injected ledger failure")
    monkeypatch.setattr(module, "append_private_jsonl_records", fail_ledger)
    with pytest.raises(RuntimeError, match="injected ledger failure"):
        repository.mutate(PersonaMutationRequest("agents", "add", content="NOT_COMMITTED_MARKER"))
    assert source.default_path.read_bytes() == original
    assert source.default_path.stat().st_ino == inode
    prefix, updates = prefix_and_updates(builder, request())
    assert prefix == first and not updates
    assert _read_persona_epoch(source.owner_home) == 0
