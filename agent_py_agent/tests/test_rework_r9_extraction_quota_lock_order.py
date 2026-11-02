"""R9-R1 P1-R9-10：提炼提交与资产投递在同一 owner 上按 quota→资产锁同序，1 GB 配额下并发不成环。

真实临时 Owner、LocalUserStore、ConversationStore、提炼提案、ScriptStore 与 AssetDeliveryService；只在原
锁内调用点放置观察钩子编排交错，不替换配额或锁实现，不访问网络。
"""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets import turn_extractor as extract
from agent_py_agent.agent.assets.delivery_service import AssetDeliveryService
from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.agent.conversation.native_history import canonical_native_messages_envelope
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.user_space.local_user_store import LocalUser, LocalUserStore
from agent_py_agent.agent.user_space.owner_quota import (
    OwnerQuotaLockContention,
    owner_quota_enforcer_from_policy,
)

QUOTA_MB = 1024
TOGGLE_SCRIPT = "r9toggle"


def _local_owner(root, uid):
    home = root / "owners/local" / uid
    home.mkdir(parents=True)
    users = LocalUserStore(root)
    user = LocalUser(uid, "enabled", 1, uid, "2026-10-25", "2026-10-25", 1)
    users.save(uid, {"schema": "local-user.v1", "user": user.to_dict(),
                     "provision": {"phase": "committed", "operation_id": "fixture",
                                   "home_identity": users.directory_identity(home.relative_to(root).parts)},
                     "operations": {"fixture": user.to_dict()}})
    (home / "quota.json").write_text(json.dumps({"max_disk_mb": QUOTA_MB}), encoding="utf-8")
    return home


@pytest.fixture
def kit(tmp_path):
    root = tmp_path / "host"
    (root / "owners/local/main").mkdir(parents=True)
    home = _local_owner(root, "alice")
    other = _local_owner(root, "bob")
    ScriptStore(home).save_script(AssetDescriptor(TOGGLE_SCRIPT, "script", "投递开关"), 'print("{}")\n')
    conversations = ConversationStore(home / "conversations")
    thread = conversations.threads.get_or_create({"channel": "tui", "channel_conversation_id": "r9", "title": "日报"})
    first = conversations.messages.append({"thread_id": thread.thread_id, "role": "user", "content": "整理中文订单",
                                           "now": 1792886400})
    native = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "call1", "name": "run_command", "input": {"command": "printf hello"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call1", "content": "hello"}]},
    ]
    conversations.messages.append({"thread_id": thread.thread_id, "role": "assistant", "content": "订单整理完毕",
                                   "metadata": {"canonical_native_messages": canonical_native_messages_envelope(native)},
                                   "now": 1792886401})
    draft = extract.propose_assets_from_turns(conversations, owner_home=home,
                                              turn_refs=[{"thread_id": thread.thread_id, "message_id": first.message_id}])
    assert draft["status"] == "DRAFT" and draft["revision"] == 1
    return SimpleNamespace(root=root, home=home, other=other, draft=draft, service=AssetDeliveryService(root))


def _quota_held(home) -> bool:
    # 从另一线程非阻塞试同一 owner 原配额锁；争用即说明当前事务仍持有该准入。
    seen = []

    def probe():
        try:
            with owner_quota_enforcer_from_policy(home).admission(blocking=False):
                seen.append(False)
        except OwnerQuotaLockContention:
            seen.append(True)

    worker = threading.Thread(target=probe, name="r9-quota-probe")
    worker.start()
    worker.join(10)
    assert not worker.is_alive() and len(seen) == 1
    return seen[0]


def _run_into(results, name, call):
    try:
        results[name] = call()
    except BaseException as exc:  # noqa: BLE001 - 交给测试线程断言
        results[name] = exc


def _commit(kit):
    return extract.commit_extracted_assets(kit.home, proposal_id=kit.draft["proposal_id"], expected_revision=1)


def _assert_published(kit, receipt):
    assert receipt["ok"] is True and receipt["status"] == "COMMITTED" and len(receipt["committed_assets"]) == 3
    proposal = extract.get_extraction_proposal(kit.home, kit.draft["proposal_id"])
    assert proposal["status"] == "COMMITTED" and proposal["revision"] == 2
    registry = SkillLearningStore(kit.home / "data" / "skill_learning", kit.home / "skills").load_registry()
    assert len(registry.assets) == 3
    assert len(ScriptStore(kit.home).list_scripts()) == 2


def test_concurrent_extraction_and_delivery_complete_at_1gb_quota(kit, monkeypatch):
    in_assets, delivery_quota = threading.Event(), threading.Event()
    order, results = [], {}
    real_check, real_scope = extract._check_dependencies, AssetDeliveryService._write_scope

    def check_dependencies(root, chosen, scripts):
        # 提炼此刻已持资产根与集合锁；旧顺序下投递能先拿到配额再等资产锁，提炼随后等配额即成环。
        order.append(("extraction-assets", _quota_held(kit.home)))
        in_assets.set()
        delivery_quota.wait(1.0)
        return real_check(root, chosen, scripts)

    @contextmanager
    def write_scope(self, homes):
        with real_scope(self, homes) as admissions:
            order.append(("delivery-quota", extract.get_extraction_proposal(kit.home, kit.draft["proposal_id"])["status"]))
            delivery_quota.set()
            yield admissions

    def deliver():
        assert in_assets.wait(15), "extraction did not reach its asset-locked section"
        return kit.service.toggle_asset("local/alice", "script", TOGGLE_SCRIPT, True)

    monkeypatch.setattr(extract, "_check_dependencies", check_dependencies)
    monkeypatch.setattr(AssetDeliveryService, "_write_scope", write_scope)
    threads = [threading.Thread(target=_run_into, args=(results, "extraction", lambda: _commit(kit)), daemon=True),
               threading.Thread(target=_run_into, args=(results, "delivery", deliver), daemon=True)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not any(thread.is_alive() for thread in threads), f"extraction/delivery lock cycle: {order}"
    # 提炼在资产锁内仍持有本 owner 配额准入；投递只能在提炼整体结束后取得配额。
    assert order == [("extraction-assets", True), ("delivery-quota", "COMMITTED")]
    _assert_published(kit, results["extraction"])
    assert results["delivery"]["ok"] is True and results["delivery"]["enabled"] is True
    assert ScriptStore(kit.home).get_script(TOGGLE_SCRIPT)["enabled"] is True
    assert _quota_held(kit.home) is False


def test_serial_extraction_and_delivery_controls_at_1gb_quota(kit):
    assert kit.service.toggle_asset("local/alice", "script", TOGGLE_SCRIPT, True)["enabled"] is True
    receipt = _commit(kit)
    _assert_published(kit, receipt)
    # 幂等重放只读原回执，同样先取配额再取提案锁，不重复发布。
    assert _commit(kit) == receipt
    assert kit.service.toggle_asset("local/alice", "script", TOGGLE_SCRIPT, False)["enabled"] is False
    assert _quota_held(kit.home) is False


def test_rollback_keeps_original_admission_until_finalization(kit, monkeypatch):
    real_write, real_rollback, observed = extract._write_record, extract._rollback_files, []

    def write_record(home, collection, value, id_key):
        if value.get("status") == "COMMITTED" and not observed:
            observed.append(("publication", _quota_held(kit.home)))
            raise OSError("simulated proposal publication failure")
        return real_write(home, collection, value, id_key)

    def rollback_files(root, files):
        observed.append(("rollback", _quota_held(kit.home)))
        return real_rollback(root, files)

    monkeypatch.setattr(extract, "_write_record", write_record)
    monkeypatch.setattr(extract, "_rollback_files", rollback_files)
    with pytest.raises(OSError, match="proposal publication failure"):
        _commit(kit)
    assert observed == [("publication", True), ("rollback", True)]
    assert _quota_held(kit.home) is False
    proposal = extract.get_extraction_proposal(kit.home, kit.draft["proposal_id"])
    assert proposal["status"] == "DRAFT" and proposal["revision"] == 1
    assert [row["id"] for row in ScriptStore(kit.home).list_scripts()] == [TOGGLE_SCRIPT]
    # 下一次普通提交从同一 DRAFT 重新发布，回滚未留下半提交登记。
    _assert_published(kit, _commit(kit))


def test_state_lock_default_order_and_foreign_admission_rejected(kit):
    store = SkillLearningStore(kit.home / "data" / "skill_learning", kit.home / "skills")
    with store.state_lock():
        assert _quota_held(kit.home) is True
    assert _quota_held(kit.home) is False
    with owner_quota_enforcer_from_policy(kit.home).admission() as admission:
        # 同 owner 准入原样复用，不再取不可重入配额锁；退出 state 后准入仍由调用方持有。
        with store.state_lock(admission=admission) as reused:
            assert reused is admission
        assert _quota_held(kit.home) is True
        with owner_quota_enforcer_from_policy(kit.other).admission() as foreign:
            with pytest.raises(RuntimeError, match="配额事务"):
                with store.state_lock(admission=foreign):
                    pytest.fail("foreign owner admission accepted")
    assert _quota_held(kit.home) is False
