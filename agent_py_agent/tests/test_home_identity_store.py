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
