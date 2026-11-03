# LLM: 回归只读写隔离 owner 目录，不触碰真实 ~/.my-agent；断言发现端与消费端看同一个 home。
# 模块用途: 覆盖 W1 #53-1a..f：local 身份双路径分裂、旧位置迁移诊断、冲突去重、分页与硬事实路径。
"""W1 #53-1：owner 发现的规范位置唯一性回归（发现位置必须等于消费位置）。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from agent_py_agent.agent import owner_wake_discovery as discovery
from agent_py_agent.agent.owner_scoped_pool import ActiveOwnerRegistry
from agent_py_agent.agent.owner_wake_discovery import (
    discover_owner_home_page,
    discover_wake_pending_owner_page,
    discover_wake_pending_owners,
    seed_registry_from_disk,
)
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    _owner_home_dir,
    resolve_owner_home,
)

_DISCOVERY_LOGGER = "agent_py_agent.agent.owner_wake_discovery"


@pytest.fixture(autouse=True)
def _clean_discovery_state():
    """每个用例前后清空目录快照与位置诊断台账，避免跨用例互相影响。"""
    discovery._OWNER_HOME_SNAPSHOTS.clear()
    ledger = getattr(discovery, "_OwnerHomeDiagnosticLedger", None)
    if ledger is not None:
        ledger._reported.clear()
    yield
    discovery._OWNER_HOME_SNAPSHOTS.clear()
    if ledger is not None:
        ledger._reported.clear()


def _local_user_home(owners: Path, user_id: str) -> Path:
    """local 用户的规范位置：owners/local/<id>。"""
    home = owners / "local" / user_id
    home.mkdir(parents=True, exist_ok=True)
    return home


def _legacy_local_user_home(owners: Path, user_id: str) -> Path:
    """local 用户的历史遗留位置：owners/providers/local/users/<id>。"""
    home = owners / "providers" / "local" / "users" / user_id
    home.mkdir(parents=True, exist_ok=True)
    return home


def _provider_home(owners: Path, provider: str, bucket: str, owner_id: str) -> Path:
    """provider 身份的规范位置：owners/providers/<provider>/<bucket>/<id>。"""
    home = owners / "providers" / provider / bucket / owner_id
    home.mkdir(parents=True, exist_ok=True)
    return home


def _write_enabled_policy(owner_home: Path) -> None:
    """在 owner home 写一条 enabled 的 progress policy（硬事实）。"""
    store = owner_home / "workspace" / "runtime" / "workspaces" / "slug-x" / "conversations"
    policies = store / "progress_policies"
    policies.mkdir(parents=True, exist_ok=True)
    (policies / "policy-a.json").write_text(json.dumps({"enabled": True}), encoding="utf-8")


def _identity_keys(page) -> list[tuple[str, str, str]]:
    return [
        (target.identity.provider, target.identity.owner_kind, target.identity.owner_id)
        for target in page.targets
    ]


def _diagnostics(caplog, code: str) -> list:
    return [record for record in caplog.records if code in record.getMessage()]


def test_canonical_local_user_is_discovered_once_at_its_canonical_home(tmp_path) -> None:
    """#53-1a/#53-1e：只有规范位置时，发现结果与解析结果一致且只出现一次。"""
    owners = tmp_path / "owners"
    canonical = _local_user_home(owners, "alice")
    _write_enabled_policy(canonical)

    page = discover_owner_home_page(owners, limit=64)

    assert _identity_keys(page) == [("local", "user", "alice")]
    assert Path(page.targets[0].home_dir) == _owner_home_dir(tmp_path, OwnerIdentity.local_user("alice"))
    assert [owner.owner_id for owner in discover_wake_pending_owners(owners)] == ["alice"]


def test_legacy_only_local_user_is_not_seeded_and_reports_one_diagnostic(tmp_path, caplog) -> None:
    """#53-1b：仅旧位置时该身份不种入，只发一条结构化诊断，旧目录保持原样。"""
    owners = tmp_path / "owners"
    legacy = _legacy_local_user_home(owners, "ghost")
    _write_enabled_policy(legacy)

    with caplog.at_level(logging.WARNING, logger=_DISCOVERY_LOGGER):
        found = discover_wake_pending_owners(owners)
        page = discover_owner_home_page(owners, limit=64)
        registry = ActiveOwnerRegistry(max_owners=4)
        seeded = seed_registry_from_disk(registry, owners)

    assert found == []
    assert page.targets == ()
    assert seeded == 0
    assert registry.snapshot() == []

    diagnostics = _diagnostics(caplog, "OWNER_HOME_LEGACY_LOCATION")
    assert len(diagnostics) == 1
    message = diagnostics[0].getMessage()
    assert "identity=local/user/ghost" in message
    assert str(legacy) in message
    assert str(owners / "local" / "ghost") in message
    assert legacy.is_dir(), "旧目录不得被删除或迁移"
    assert not (owners / "local" / "ghost").exists(), "不得为旧身份新建规范目录"


def test_legacy_and_canonical_both_present_prefers_canonical_and_reports_conflict(
    tmp_path, caplog
) -> None:
    """#53-1c：新旧并存时身份唯一、取规范 home，并发一条冲突诊断。"""
    owners = tmp_path / "owners"
    legacy = _legacy_local_user_home(owners, "bob")
    canonical = _local_user_home(owners, "bob")
    _write_enabled_policy(canonical)

    with caplog.at_level(logging.WARNING, logger=_DISCOVERY_LOGGER):
        page = discover_owner_home_page(owners, limit=64)

    assert _identity_keys(page) == [("local", "user", "bob")]
    assert Path(page.targets[0].home_dir) == canonical

    conflicts = _diagnostics(caplog, "OWNER_HOME_LOCATION_CONFLICT")
    assert len(conflicts) == 1
    message = conflicts[0].getMessage()
    assert "identity=local/user/bob" in message
    assert str(legacy) in message
    assert str(canonical) in message
    assert legacy.is_dir(), "旧目录不得被删除"


def test_ordinary_provider_users_and_groups_behaviour_unchanged(tmp_path) -> None:
    """#53-1a 对照组：普通 provider 的 users/groups 行为不变，发现位置等于解析位置。"""
    owners = tmp_path / "owners"
    user_home = _provider_home(owners, "synthetic-provider", "users", "u1")
    group_home = _provider_home(owners, "synthetic-provider", "groups", "g1")
    _write_enabled_policy(user_home)
    _write_enabled_policy(group_home)

    page = discover_owner_home_page(owners, limit=64)

    assert _identity_keys(page) == [
        ("synthetic-provider", "group", "g1"),
        ("synthetic-provider", "user", "u1"),
    ]
    for target in page.targets:
        assert Path(target.home_dir) == _owner_home_dir(tmp_path, target.identity)
    assert {owner.owner_id for owner in discover_wake_pending_owners(owners)} == {"g1", "u1"}


def test_limit_one_pagination_returns_the_canonical_target_exactly_once(tmp_path) -> None:
    """#53-1d：limit=1 连翻两页，规范目标恰好出现一次，不被同键旧目录跳过。"""
    owners = tmp_path / "owners"
    _legacy_local_user_home(owners, "carol")
    canonical = _local_user_home(owners, "carol")
    other = _provider_home(owners, "synthetic-provider", "users", "u1")
    _write_enabled_policy(canonical)
    _write_enabled_policy(other)

    seen: list[tuple[str, str, str]] = []
    homes: list[Path] = []
    cursor = None
    pages = 0
    while True:
        page = discover_owner_home_page(owners, limit=1, after_cursor=cursor)
        pages += 1
        assert pages < 10, "分页必须收敛"
        seen.extend(_identity_keys(page))
        homes.extend(Path(target.home_dir) for target in page.targets)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert seen == [("local", "user", "carol"), ("synthetic-provider", "user", "u1")]
    assert len(seen) == len(set(seen))
    assert homes == [canonical, other]


def test_hard_facts_are_evaluated_at_the_home_the_consumer_uses(tmp_path, monkeypatch) -> None:
    """#53-1e：发现事实的路径必须等于消费端(resolve_owner_home)使用的路径。"""
    owners = tmp_path / "owners"
    legacy = _legacy_local_user_home(owners, "dave")
    canonical = _local_user_home(owners, "dave")
    _write_enabled_policy(canonical)

    observed: list[Path] = []
    original = discovery._owner_fact_kind

    def recording(home):
        observed.append(Path(home))
        return original(home)

    monkeypatch.setattr(discovery, "_owner_fact_kind", recording)

    page = discover_wake_pending_owner_page(owners, limit=64)
    consumer_home = resolve_owner_home(tmp_path, OwnerIdentity.local_user("dave")).home_dir

    assert [owner.owner_id for owner in page.hard_owners] == ["dave"]
    assert observed, "必须真的判定事实"
    assert all(path.resolve() == Path(consumer_home).resolve() for path in observed)
    assert all(path.resolve() != legacy.resolve() for path in observed)


def test_legacy_diagnostic_is_reported_once_per_process(tmp_path, caplog) -> None:
    """#53-1b：重复扫描（强制重建快照）也不会重复刷同一条诊断。"""
    owners = tmp_path / "owners"
    _legacy_local_user_home(owners, "erin")

    with caplog.at_level(logging.WARNING, logger=_DISCOVERY_LOGGER):
        for _ in range(3):
            discovery._OWNER_HOME_SNAPSHOTS.clear()
            discover_owner_home_page(owners, limit=64)

    assert len(_diagnostics(caplog, "OWNER_HOME_LEGACY_LOCATION")) == 1
