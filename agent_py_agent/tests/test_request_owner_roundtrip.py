"""认证写端接受的owner必须能由冻结读端准确恢复，不能跨成local/main。"""
from dataclasses import asdict

import pytest

from agent_py_agent.agent.gateway_parts.request_owner_binding import (
    REQUEST_OWNER_KEY,
    frozen_request_owner,
)
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home


@pytest.mark.parametrize("provider,kind", [("local", "user"), ("local", "group"), ("feishu", "user"), ("feishu", "group")])
def test_canonical_provider_owner_roundtrips_and_stays_separate(tmp_path, provider, kind):
    factory = OwnerIdentity.provider_user if kind == "user" else OwnerIdentity.provider_group
    owner = factory(provider, "fixture-alice")
    assert frozen_request_owner({REQUEST_OWNER_KEY: asdict(owner)}) == owner
    assert resolve_owner_home(tmp_path, owner).home_dir != resolve_owner_home(tmp_path, OwnerIdentity.local_main()).home_dir


@pytest.mark.parametrize("raw", [
    {"provider": "local", "owner_kind": "main", "owner_id": "alice"},
    {"provider": "feishu", "owner_kind": "main", "owner_id": "main"},
    {"provider": "local", "owner_kind": "user", "owner_id": "../main"},
    {"provider": "local", "owner_kind": "user", "owner_id": ""},
])
def test_invalid_frozen_owner_still_fails_closed(raw):
    with pytest.raises(DataCorruptionError):
        frozen_request_owner({REQUEST_OWNER_KEY: raw})


def test_only_explicit_main_tuple_recovers_main():
    assert frozen_request_owner({REQUEST_OWNER_KEY: asdict(OwnerIdentity.local_main())}) == OwnerIdentity.local_main()
    assert frozen_request_owner({}) is None
