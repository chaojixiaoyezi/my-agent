"""原资源账精确消费合同；只使用合成身份，不向测试 PID 发信号。"""

from copy import deepcopy

import pytest

from agent_py_agent.agent.tooling import process_session_store as store_module
from agent_py_agent.agent.tooling.process_cleanup_evidence import process_cleanup_reference
from agent_py_agent.agent.tooling.process_session_store import ProcessSessionStore
from agent_py_agent.tests.test_process_activation_scope import shared_record
from agent_py_agent.tests.test_process_session_store import _reservation


# LLM: 纯记录夹具不证明 OS 退出；产品证明只由原 stop_process_session 写入。
# 函数用途: 构造严格已确认记录，测试删除的版本、身份及原生回执边界。
def confirmed_record(store, session="bg-plugin"):
    row = shared_record(session, status="exited")
    row.update(stop_requested=True, termination={"cleanup": {"confirmed": True, "instances": [{
        "method": "fixture", "confirmed": True, "return_code": 0, "observed_processes": 1, "unresolved_pids": [],
    }]}})
    return store.write(row)


@pytest.mark.parametrize("damage", ["identity", "revision", "cleanup", "missing_field"])
def test_all_references_checked_before_any_delete(tmp_path, damage):
    store = ProcessSessionStore(tmp_path)
    refs = [process_cleanup_reference(confirmed_record(store, session)) for session in ("bg-first", "bg-second")]
    broken = deepcopy(refs[1])
    if damage == "identity":
        broken["identity_sha256"] = "a" * 64
    elif damage == "revision":
        broken["revision"] += 1
    elif damage == "cleanup":
        broken["cleanup"]["confirmed"] = False
    else:
        broken.pop("identity_sha256")
    with store.transaction() as transaction, pytest.raises(ValueError):
        transaction.consume_cleanup((refs[0], broken))
    assert len(store.list_records()[0]) == 2


def test_partial_unlink_failure_resubmits_exact_refs_and_keeps_unrelated_record(tmp_path, monkeypatch):
    store = ProcessSessionStore(tmp_path)
    refs = tuple(process_cleanup_reference(confirmed_record(store, session)) for session in ("bg-first", "bg-second"))
    other = confirmed_record(store, "bg-other")
    original = store_module.unlink_file_beneath
    def fail_second(root, parts):
        if parts[-1] == "bg-second.json":
            raise OSError("fixture unlink failed")
        return original(root, parts)
    monkeypatch.setattr(store_module, "unlink_file_beneath", fail_second)
    with store.transaction() as transaction, pytest.raises(OSError):
        transaction.consume_cleanup(refs)
    assert not store.load("bg-first").record and store.load("bg-second").record
    monkeypatch.setattr(store_module, "unlink_file_beneath", original)
    with store.transaction() as transaction:
        transaction.consume_cleanup(refs)
        transaction.consume_cleanup(refs)
    assert store.list_records()[0] == [other]


def test_reused_id_is_not_old_instance_and_late_revision_does_not_prevent_cleanup(tmp_path):
    store = ProcessSessionStore(tmp_path)
    record = confirmed_record(store)
    reference = process_cleanup_reference(record)
    store.write(record)  # 同身份的迟到写入可以前进版本。
    with store.transaction() as transaction:
        transaction.consume_cleanup((reference,))
    changed = shared_record(status="exited")
    changed.update(stop_requested=True, termination=record["termination"], reserved_at=5.0)
    store.write(changed)
    with store.transaction() as transaction, pytest.raises(ValueError):
        transaction.consume_cleanup((reference,))
    assert store.load(reference["session_id"]).record["reserved_at"] == 5.0


def test_explicit_retention_survives_pruning_without_changing_ordinary_history(tmp_path):
    store = ProcessSessionStore(tmp_path)
    retained = store.write(_reservation("bg-retained", status="not_started", finished_at=3,
                                        retain_until_consumed=True))
    store.write(_reservation("bg-ordinary", status="not_started", finished_at=3))
    store.prune_finished(0)
    assert store.list_records()[0] == [retained]
    with pytest.raises(ValueError):
        store.write({**retained, "retain_until_consumed": False})
    with pytest.raises(ValueError):
        process_cleanup_reference(retained)


def test_previous_v3_keeps_original_schema_and_cannot_smuggle_new_retention(tmp_path):
    from agent_py_agent.agent.tooling.process_session_records import (
        ACTIVATION_PROCESS_SESSION_SCHEMA,
    )

    store = ProcessSessionStore(tmp_path)
    legacy = shared_record()
    legacy["schema"] = ACTIVATION_PROCESS_SESSION_SCHEMA
    with pytest.raises(ValueError):
        store.write(legacy)
    legacy.pop("retain_until_consumed")
    saved = store.write(legacy)
    changed = store.write({**saved, "stop_requested": True})
    assert changed["schema"] == ACTIVATION_PROCESS_SESSION_SCHEMA and "retain_until_consumed" not in changed
    store.prune_finished(0)
    assert store.load(changed["session_id"]).record == changed
