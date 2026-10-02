# LLM: 决策冷却持久化的离线合同：CLI 新进程与重启后的 Gateway 都要能从 owner 数据域的冷却文件恢复退避。
#   只测纯函数与 S2 入口的落盘行为，不发送模型请求；进程内表用独立 OrderedDict 隔离，不污染其它测试。
"""决策冷却持久化：restore/persist_cooldown_snapshot 与 S2 入口落盘到 skill_proposals 目录。"""
from __future__ import annotations

import json
import time
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.backends.errors import ProviderQuotaExhaustedError
from agent_py_agent.agent.conversation import decision_policy
from agent_py_agent.agent.conversation.decision_policy import (
    COOLDOWN_FILE_SCHEMA,
    MAX_COOLDOWN_FILE_BYTES,
    persist_cooldown_snapshot,
    restore_cooldown_snapshot,
)
from agent_py_agent.tests.test_decision_skill_proposal_review import (
    install as review_install,
)
from agent_py_agent.tests.test_decision_skill_proposal_review import (
    seeded,
)

_OWNER = "owner-1"
_PROFILE = "profile-1"
_REVISION = "rev-1"


def _key(*parts: str) -> tuple[str, ...]:
    return (_OWNER, _PROFILE, _REVISION, *parts)


def _fresh_table(monkeypatch) -> None:
    # 模拟全新进程：进程内冷却表从空开始。
    monkeypatch.setattr(decision_policy, "_FAILURES", OrderedDict())


def test_persist_then_restore_survives_process_boundary(tmp_path: Path, monkeypatch) -> None:
    _fresh_table(monkeypatch)
    decision_policy.record_failure(_key(), _REVISION, ProviderQuotaExhaustedError("额度耗尽"))
    path = tmp_path / "cooldown.json"
    assert persist_cooldown_snapshot(path, _OWNER) == 1

    _fresh_table(monkeypatch)
    assert decision_policy.cooldown_state(_key(), _REVISION) == ("", 0.0)  # 新进程看不到
    assert restore_cooldown_snapshot(path, _OWNER) == 1

    status, remaining = decision_policy.cooldown_state(_key(), _REVISION)
    assert status == "cooldown" and remaining > 0
    assert decision_policy.record_success(_key()) is None


def test_persisted_file_keeps_point_backoff_key_shape(tmp_path: Path, monkeypatch) -> None:
    _fresh_table(monkeypatch)
    decision_policy.record_failure(_key("skill_proposal_review"), _REVISION, TimeoutError("超时"))
    path = tmp_path / "cooldown.json"
    persist_cooldown_snapshot(path, _OWNER)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["schema"] == COOLDOWN_FILE_SCHEMA and payload["owner"] == _OWNER
    key_text = json.dumps([_OWNER, _PROFILE, _REVISION, "skill_proposal_review"], sort_keys=True)
    status, revision, until_wall, streak = payload["entries"][key_text]
    assert (status, revision, streak) == ("cooldown", _REVISION, 1)
    assert until_wall is not None and until_wall > time.time()


def test_restore_drops_expired_entries(tmp_path: Path, monkeypatch) -> None:
    _fresh_table(monkeypatch)
    path = tmp_path / "cooldown.json"
    path.write_text(json.dumps({
        "schema": COOLDOWN_FILE_SCHEMA, "owner": _OWNER, "saved_at": time.time(),
        "entries": {json.dumps(list(_key())): ["cooldown", _REVISION, time.time() - 10, 2]},
    }), encoding="utf-8")

    assert restore_cooldown_snapshot(path, _OWNER) == 0
    assert decision_policy.cooldown_state(_key(), _REVISION) == ("", 0.0)


def test_restore_ignores_corrupt_foreign_and_oversized_files(tmp_path: Path, monkeypatch) -> None:
    _fresh_table(monkeypatch)
    bad_path = tmp_path / "bad.json"
    bad_path.write_text("{not json", encoding="utf-8")
    assert restore_cooldown_snapshot(bad_path, _OWNER) == 0

    foreign = tmp_path / "foreign.json"
    foreign.write_text(json.dumps({
        "schema": COOLDOWN_FILE_SCHEMA, "owner": "other-owner", "saved_at": time.time(),
        "entries": {json.dumps(list(_key())): ["cooldown", _REVISION, time.time() + 60, 1]},
    }), encoding="utf-8")
    assert restore_cooldown_snapshot(foreign, _OWNER) == 0
    assert decision_policy.cooldown_state(_key(), _REVISION) == ("", 0.0)

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * (MAX_COOLDOWN_FILE_BYTES + 1))
    assert restore_cooldown_snapshot(oversized, _OWNER) == 0


def test_review_entry_persists_cooldown_next_to_proposals(tmp_path, monkeypatch) -> None:
    from agent_py_agent.agent.capability import decision_skill_proposal_review as module

    ctx = seeded(tmp_path, 3)
    host = SimpleNamespace(home_paths=ctx.home)
    owner_ref = decision_policy.decision_owner_ref(host)
    decision_policy.record_failure((owner_ref, _PROFILE, _REVISION), _REVISION, ProviderQuotaExhaustedError("q"))
    review_install(monkeypatch, {"proposal_3": "review_first"})

    result = module.skill_proposal_review_order(host, ctx.service, ctx.service.list())
    assert result is not None
    cooldown_file = ctx.home.owner_skill_proposals_dir / ".cooldown.json"
    assert cooldown_file.is_file()
    payload = json.loads(cooldown_file.read_text(encoding="utf-8"))
    assert payload["schema"] == COOLDOWN_FILE_SCHEMA and payload["owner"] == owner_ref
    key_text = json.dumps([owner_ref, _PROFILE, _REVISION], sort_keys=True)
    assert payload["entries"][key_text][0] == "cooldown"