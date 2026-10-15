from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.mark.parametrize('owner_kind,owner_id,canonical', [
    ('user', 'ou_original', 'canonical-original'), ('group', 'oc_linked', 'linked-canonical'),
    ('main', 'main', 'administrator-linked'),
])
def test_observed_display_name_preserves_links_and_only_appends_changed_nonempty_name(tmp_path, owner_kind, owner_id, canonical):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.identity_store import remember_provider_display_names

    home = ensure_my_agent_home(tmp_path)
    path = home.provider_identity_dir / 'feishu.jsonl'
    original = {'schema_version': 'provider-identity.v1', 'provider': 'feishu', 'provider_subject_id': 'ou_one',
        'owner_kind': owner_kind, 'owner_id': owner_id, 'canonical_user_id': canonical,
        'owner_home': 'original-owner-path', 'display_name': '旧称呼', 'policy_extension': {'preserve': True}}
    path.write_text(json.dumps(original, ensure_ascii=False) + '\n')
    assert remember_provider_display_names(home, provider='feishu', names={'ou_one': ' 新称呼 '}) == 1
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0] == original and len(rows) == 2
    assert {k: v for k, v in rows[-1].items() if k not in {'display_name', 'updated_at'}} == {
        k: v for k, v in original.items() if k != 'display_name'}
    before = path.read_bytes()
    before_stat = (path.stat().st_ino, path.stat().st_mtime_ns)
    assert remember_provider_display_names(home, provider='feishu', names={'ou_one': '新称呼'}) == 0
    assert remember_provider_display_names(home, provider='feishu', names={'ou_one': ''}) == 0
    assert path.read_bytes() == before
    assert (path.stat().st_ino, path.stat().st_mtime_ns) == before_stat


def test_display_name_new_identity_is_only_default_user_and_cannot_name_itself_admin(tmp_path):
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.identity_store import (
        lookup_provider_identity,
        remember_provider_display_names,
        resolve_owner_from_provider_identity,
    )
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home

    home = home_paths(tmp_path)
    assert remember_provider_display_names(home, provider='feishu', names={'ou_new': '管理员'}) == 1
    record = lookup_provider_identity(home, provider='feishu', provider_subject_id='ou_new')
    assert record.owner_kind == 'user' and record.owner_id == record.provider_subject_id == 'ou_new'
    assert record.canonical_user_id == '' and not (tmp_path / 'owners').exists()
    found = resolve_owner_from_provider_identity(home, provider='feishu', provider_subject_id='ou_new')
    expected = resolve_owner_home(tmp_path, OwnerIdentity.provider_user('feishu', 'ou_new'))
    assert found.owner_id == expected.owner_id and found.home_dir == expected.home_dir
    assert found.identity.owner_kind == 'user'


def test_display_name_updates_share_original_index_lock_and_empty_batch_does_not_read(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.identity_store import remember_provider_display_names

    home = home_paths(tmp_path)
    def remember(subject):
        try:
            return remember_provider_display_names(home, provider='feishu', names={subject: '同名'})
        except BlockingIOError:
            return 'busy'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(remember, ['ou_one', 'ou_two']))
    assert 1 in results and all(value in {1, 'busy'} for value in results)
    # 锁忙明确让出，模拟后续原认证快照再到达；成功更新不能被另一主体覆盖。
    for subject in ('ou_one', 'ou_two'):
        assert remember(subject) in {0, 1}
    path = home.provider_identity_dir / 'feishu.jsonl'
    assert {json.loads(line)['provider_subject_id'] for line in path.read_text().splitlines()} == {'ou_one', 'ou_two'}
    monkeypatch.setattr(Path, 'read_bytes', lambda *_: pytest.fail('无姓名不应扫描全索引'))
    assert remember_provider_display_names(home, provider='feishu', names={}) == 0
    assert remember_provider_display_names(home, provider='feishu', names={'ou_one': '  '}) == 0


def test_bad_display_index_keeps_original_bytes(tmp_path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.identity_store import remember_provider_display_names

    home = ensure_my_agent_home(tmp_path)
    path = home.provider_identity_dir / 'feishu.jsonl'
    path.write_bytes(b'{invalid-record}\n')
    with pytest.raises(ValueError):
        remember_provider_display_names(home, provider='feishu', names={'ou_one': '名字'})
    assert path.read_bytes() == b'{invalid-record}\n'


@pytest.mark.parametrize('kind', ['user', 'group'])
def test_same_names_display_exact_ids_for_ordinary_and_group_creators_without_owner_creation(tmp_path, kind):
    from types import SimpleNamespace

    from agent_py_agent.agent.scheduler.creator_access import creator_display_rows
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.identity_store import remember_provider_display_names
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        home_paths_with_owner,
        resolve_owner_home,
    )

    identity = OwnerIdentity('feishu', kind, 'ou_host' if kind == 'user' else 'oc_host')
    home = home_paths_with_owner(home_paths(tmp_path), resolve_owner_home(tmp_path, identity))
    assert remember_provider_display_names(home, provider='feishu', names={'ou_one': '同名', 'ou_two': '同名'}) == 2
    jobs = [{'creator': {'channel': 'feishu', 'actor_id': actor}} for actor in ('ou_one', 'ou_two')]
    rows = creator_display_rows(SimpleNamespace(home_paths=home), jobs)
    assert [row['creator_display'] for row in rows] == ['同名（feishu:ou_one）', '同名（feishu:ou_two）']
    assert [row['creator'] for row in rows] == [row['creator'] for row in jobs]
    assert not (tmp_path / 'owners').exists()


@pytest.mark.parametrize('provider,subject', [('feishu/other', 'ou_one'), ('feishu', 'ou_one/other'), ('feishu', ' ou_one')])
def test_display_projection_rejects_identity_normalization_collisions(tmp_path, provider, subject):
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.identity_store import remember_provider_display_names

    home = home_paths(tmp_path)
    with pytest.raises(ValueError):
        remember_provider_display_names(home, provider=provider, names={subject: '同名'})
    assert not home.provider_identity_dir.exists()


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


def test_provider_identity_lookup_reports_corrupt_index_rows(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.identity_store import (
        ProviderIdentityRecord,
        link_provider_identity,
        lookup_provider_identity_report,
    )

    home = ensure_my_agent_home(tmp_path)
    home.provider_identity_dir.mkdir(parents=True, exist_ok=True)
    (home.provider_identity_dir / "feishu.jsonl").write_text("{bad-json}\n", encoding="utf-8")
    link_provider_identity(
        home,
        ProviderIdentityRecord(
            provider="feishu",
            provider_subject_id="ou_123",
            owner_kind="user",
            owner_id="ou_123",
            canonical_user_id="canonical_user_001",
        ),
    )

    report = lookup_provider_identity_report(home, provider="feishu", provider_subject_id="ou_123")

    assert report.record is not None
    assert report.record.owner_id == "ou_123"
    assert report.load_errors
    assert report.load_errors[0]["context"] == "identity_store.provider_identity"


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


def test_provider_identity_owner_resolution_report_keeps_load_errors(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.identity_store import (
        ProviderIdentityRecord,
        link_provider_identity,
        resolve_owner_from_provider_identity_report,
    )

    home = ensure_my_agent_home(tmp_path)
    home.provider_identity_dir.mkdir(parents=True, exist_ok=True)
    (home.provider_identity_dir / "feishu.jsonl").write_text("{bad-json}\n", encoding="utf-8")
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

    report = resolve_owner_from_provider_identity_report(home, provider="feishu", provider_subject_id="ou_456")

    assert report.owner is not None
    assert report.owner.owner_id == "providers/feishu/users/ou_456"
    assert report.load_errors


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


def test_provider_owner_lifecycle_reports_corrupt_status_file(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_lifecycle import read_owner_lifecycle_report
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home

    home = ensure_my_agent_home(tmp_path)
    owner = ensure_owner_home(home.root, OwnerIdentity.provider_user("feishu", "ou_bad_status"))
    status_path = owner.home_dir / "owner_status.json"
    status_path.write_text("{bad-json", encoding="utf-8")

    report = read_owner_lifecycle_report(owner)

    assert report.state.status == "UNKNOWN"
    assert report.load_error["category"] == "data_parse"
    assert report.load_error["context"] == "owner_lifecycle.status"
    assert report.load_error["path"] == str(status_path)
