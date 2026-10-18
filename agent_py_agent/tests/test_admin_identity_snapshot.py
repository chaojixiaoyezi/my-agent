"""C首次prepare只读复用原绑定存储，默认查询不增加读取或新权威。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_py_agent.agent.user_space import admin_channel_identity as identities


@pytest.fixture
def home(tmp_path, monkeypatch):
    target = (tmp_path / "home").resolve()
    monkeypatch.setenv("MY_AGENT_HOME", str(target))
    assert target != (Path.home() / ".my-agent").resolve()
    return target


def test_missing_snapshot_does_not_create_home_or_identity_file(home):
    snapshot = identities.read_admin_channel_identity_snapshot(home)
    assert snapshot == identities.AdminChannelIdentitySnapshot((), "none")
    assert not home.exists()


def test_snapshot_and_default_list_share_one_validated_read_each(home, monkeypatch):
    identity = identities.bind_admin_channel_identity(home, "feishu", "fixture-user", now=1.0)
    reader = identities.read_private_config_json
    reads = []

    def observed(*args):
        reads.append(args)
        return reader(*args)

    monkeypatch.setattr(identities, "read_private_config_json", observed)
    assert identities.list_admin_channel_identities(home) == [identity]
    assert len(reads) == 1
    snapshot = identities.read_admin_channel_identity_snapshot(home)
    assert snapshot.identities == (identity,) and len(snapshot.version) == 64
    assert len(reads) == 2
    assert reads[0] == reads[1]


def test_version_covers_complete_valid_payload_without_whitespace_dependency(home):
    identities.bind_admin_channel_identity(home, "feishu", "fixture-user", now=1.0)
    before = identities.read_admin_channel_identity_snapshot(home)
    path = identities.admin_channel_identities_path(home)
    original = json.loads(path.read_text())
    # 只改合成私有文件的格式，不改变原no-follow读端或0600权限。
    path.write_text(json.dumps(original, indent=4))
    assert identities.read_admin_channel_identity_snapshot(home) == before
    original["identities"][0]["bound_at"] = 2.0
    identities.write_private_config_json(home, path.name, original)
    after = identities.read_admin_channel_identity_snapshot(home)
    assert after.version != before.version
    assert after.identities == (identities.AdminChannelIdentity("feishu", "fixture-user", 2.0),)


@pytest.mark.parametrize("payload", [{"schema": "wrong", "identities": []},
    {"schema": identities.ADMIN_CHANNEL_IDENTITIES_SCHEMA, "identities": [None]},
    {"schema": identities.ADMIN_CHANNEL_IDENTITIES_SCHEMA,
     "identities": [{"channel": "feishu", "user_id": "fixture user"}]},
    {"schema": identities.ADMIN_CHANNEL_IDENTITIES_SCHEMA,
     "identities": [{"channel": "feishu", "user_id": "fixture-user", "bound_at": float("inf")}]},
])
def test_snapshot_and_default_query_keep_same_corruption_rejection(home, monkeypatch, payload):
    monkeypatch.setattr(identities, "read_private_config_json", lambda *_: payload)
    with pytest.raises(identities.AdminIdentityStoreError):
        identities.read_admin_channel_identity_snapshot(home)
    assert identities.find_admin_channel_identity(home, "feishu", "fixture-user") is None
    assert not home.exists()


def test_snapshot_keeps_original_no_follow_and_never_reads_external_target(home, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"schema": identities.ADMIN_CHANNEL_IDENTITIES_SCHEMA, "identities": []}))
    path = identities.admin_channel_identities_path(home)
    path.parent.mkdir(parents=True, mode=0o700)
    path.symlink_to(outside)
    before = outside.read_bytes()
    with pytest.raises(identities.AdminIdentityStoreError):
        identities.read_admin_channel_identity_snapshot(home)
    assert identities.find_admin_channel_identity(home, "feishu", "fixture-user") is None
    assert path.is_symlink() and outside.read_bytes() == before
