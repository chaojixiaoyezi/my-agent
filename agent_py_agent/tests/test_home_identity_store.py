from __future__ import annotations

import json
from pathlib import Path


# LLM: provider identity index is the lookup bridge before resolving an owner home.
# 函数用途: 验证 provider 用户身份会写到按 provider 分片的索引文件，避免全局 JSONL 全表扫。
def test_provider_identity_index_is_provider_sharded(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.identity_store import (
        ProviderIdentityRecord,
        link_provider_identity,
        lookup_provider_identity,
    )

    home = ensure_my_agent_home(tmp_path)
    record = ProviderIdentityRecord(
        provider="feishu",
        provider_subject_id="ou_123",
        owner_kind="user",
        owner_id="ou_123",
        canonical_user_id="canonical_user_001",
    )

    written = link_provider_identity(home, record)
    found = lookup_provider_identity(home, provider="feishu", provider_subject_id="ou_123")

    assert written == home.provider_identity_dir / "feishu.jsonl"
    assert found is not None
    assert found.owner_home == home.root / "owners" / "providers" / "feishu" / "users" / "ou_123"
    assert found.canonical_user_id == "canonical_user_001"


# LLM: canonical user state is a directory profile, while provider bindings remain JSONL rows.
# 函数用途: 验证 canonical user 目录不会和 canonical_user_001.json 文件冲突。
def test_canonical_user_profile_is_directory_based(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.identity_store import ensure_canonical_user_profile

    home = ensure_my_agent_home(tmp_path)

    profile = ensure_canonical_user_profile(home, "canonical_user_001", display_name="小叶子")
    payload = json.loads(profile.read_text(encoding="utf-8"))

    assert profile == home.canonical_users_dir / "canonical_user_001" / "profile.json"
    assert payload["canonical_user_id"] == "canonical_user_001"
    assert payload["display_name"] == "小叶子"
    assert not (home.canonical_users_dir / "canonical_user_001.json").exists()


# LLM: provider requests should resolve to an owner home through identity rows, not path guessing.
# 函数用途: 验证外部通道身份可通过 provider_identity 索引找到并初始化对应 owner home。
def test_provider_identity_resolves_owner_home(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.identity_store import (
        ProviderIdentityRecord,
        link_provider_identity,
        resolve_owner_from_provider_identity,
    )

    home = ensure_my_agent_home(tmp_path)
    link_provider_identity(
        home,
        ProviderIdentityRecord(
            provider="feishu",
            provider_subject_id="ou_456",
            owner_kind="user",
            owner_id="ou_456",
            canonical_user_id="canonical_user_001",
        ),
    )

    owner = resolve_owner_from_provider_identity(home, provider="feishu", provider_subject_id="ou_456")

    assert owner.owner_id == "providers/feishu/users/ou_456"
    assert owner.home_dir == home.root / "owners" / "providers" / "feishu" / "users" / "ou_456"
    assert owner.memory_md.exists()


# LLM: canonical links keep cross-provider merge facts explicit and auditable.
# 函数用途: 验证同一真实用户的多平台绑定会写 canonical 目录和 linked identity 账本，不覆盖 provider memory。
def test_canonical_identity_links_are_append_only(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.identity_store import (
        CanonicalIdentityLinkRequest,
        link_canonical_identity,
        list_canonical_identity_links,
    )

    home = ensure_my_agent_home(tmp_path)

    first = link_canonical_identity(
        home,
        CanonicalIdentityLinkRequest("canonical_user_001", "feishu", "ou_123", "providers/feishu/users/ou_123"),
    )
    second = link_canonical_identity(
        home,
        CanonicalIdentityLinkRequest("canonical_user_001", "wechat", "wx_456", "providers/wechat/users/wx_456"),
    )
    links = list_canonical_identity_links(home, canonical_user_id="canonical_user_001")

    assert first.path == home.canonical_users_dir / "canonical_user_001" / "linked_identities.jsonl"
    assert second.path == first.path
    assert [item.provider for item in links] == ["feishu", "wechat"]
    assert links[0].owner_id == "providers/feishu/users/ou_123"


# LLM: canonical memory is a shared preference layer, not a provider memory overwrite.
# 函数用途: 验证 canonical 用户记忆和 provider owner 记忆分开保存，避免跨平台绑定时互相覆盖。
def test_canonical_memory_note_does_not_overwrite_provider_memory(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.identity_store import (
        CanonicalMemoryNoteRequest,
        canonical_memory_note_path,
        write_canonical_memory_note,
    )
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home

    home = ensure_my_agent_home(tmp_path)
    owner = ensure_owner_home(home.root, OwnerIdentity.provider_user("feishu", "ou_123"))
    provider_memory = owner.long_term_dir / "preferences.jsonl"
    provider_memory.write_text('{"scope":"provider","language":"zh"}\n', encoding="utf-8")

    note = write_canonical_memory_note(
        home,
        CanonicalMemoryNoteRequest("canonical_user_001", "preference", "默认中文回复", owner.owner_id),
    )

    assert note.path == canonical_memory_note_path(home, "canonical_user_001")
    assert '"默认中文回复"' in note.path.read_text(encoding="utf-8")
    assert provider_memory.read_text(encoding="utf-8") == '{"scope":"provider","language":"zh"}\n'


# LLM: provider owner lifecycle is owner metadata, not a global state file.
# 函数用途: 验证暂停/归档等 owner 状态写在对应 provider owner home，并留下审计记录。
def test_provider_owner_lifecycle_is_owner_scoped(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_lifecycle import (
        read_owner_lifecycle,
        update_owner_lifecycle,
    )
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home

    home = ensure_my_agent_home(tmp_path)
    owner = ensure_owner_home(home.root, OwnerIdentity.provider_user("feishu", "ou_789"))

    initial = read_owner_lifecycle(owner)
    updated = update_owner_lifecycle(owner, status="suspended", reason="用户要求暂停后台任务")

    assert initial.status == "active"
    assert updated.path == owner.home_dir / "owner_status.json"
    assert updated.status == "suspended"
    assert "用户要求暂停后台任务" in (owner.home_dir / "audit_log.jsonl").read_text(encoding="utf-8")
