"""J1原formal前工具账本隐私链：真实SQLite、JSON门外、精确CAS及原checkpoint。"""
from __future__ import annotations

import json
import sqlite3

import pytest

from agent_py_agent.agent.common.global_write_commit import global_write_commit_scope
from agent_py_agent.agent.common.owner_write_errors import LocalControlCoordinatorUnavailable
from agent_py_agent.agent.local_storage import ledger_redaction as ledger
from agent_py_agent.agent.local_storage.store import LocalStore
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.tests.test_local_store_coordinator_io import Door

SECRET = "应删除的私有正文"


# LLM: 只生成测试的既有原账本行；身份/状态/字段结构沿真实schema，不替真实任务补产物。
# 函数用途: 准备将由原redaction算法读取的临时SQLite事实。
def seed(store, count=1):
    with sqlite3.connect(store.db_path) as conn:
        for index in range(count):
            payload = json.dumps({"content": SECRET, "nested": [SECRET, {"safe": "unchanged"}]}, ensure_ascii=False)
            conn.execute("INSERT INTO runtime_gate_ledger(run_id,operation_id,tool,parameters_json,runtime_gate_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                         ("r", f"op-{index}", "remember", payload, payload, "succeeded", 1, 2))
            conn.execute("INSERT INTO tool_operations(owner_id,run_id,operation_id,tool,args_hash,idempotency_key,idempotency_scope,idempotency_namespace,status,holder_id,lease_expires_at,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         ("owner", "r", f"op-{index}", "remember", "args", f"key-{index}", "business", "test", "succeeded", "holder", 0, payload, 1, 2))


# LLM: 读实际sqlite全行作前后事实，不使用被测Store绕过缺源或修账。
# 函数用途: 精确核对身份/状态/partial，而不引入另一个持久状态。
def rows(store, table):
    assert table in {"runtime_gate_ledger", "tool_operations"}
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY operation_id")]


# LLM: 真实C Store与原global scope，fake仅外层同步末门，不替换SQL/JSON业务。
# 函数用途: 为每次测试创建隔离同库并保实际门持时。
def store_with_source(tmp_path, count=1):
    door = Door()
    with global_write_commit_scope(door):
        store = LocalStore(tmp_path / "ledger.db", coordinator_io=True)
    seed(store, count)
    return store, door


def test_original_and_c_redaction_produce_same_payloads_and_preserve_identity(tmp_path):
    default = LocalStore(tmp_path / "default.db")
    seed(default)
    store, door = store_with_source(tmp_path / "c")
    before = {table: rows(store, table) for table in ["runtime_gate_ledger", "tool_operations"]}
    assert default.redact_tool_ledger_content(tool="remember", contents={SECRET}) == 2
    with global_write_commit_scope(door):
        assert store.redact_tool_ledger_content(tool="remember", contents={SECRET}) == 2
    for table in before:
        actual = rows(store, table)
        assert actual == rows(default, table)
        for key in before[table][0]:
            if key not in {"parameters_json", "runtime_gate_json", "result_json"}:
                assert actual[0][key] == before[table][0][key]
        assert SECRET not in str(actual)


@pytest.mark.parametrize("offset", [1, 2, 3])
def test_redaction_prepare_transaction_checkpoint_source_failures_keep_same_object(tmp_path, offset):
    store, door = store_with_source(tmp_path)
    before = rows(store, "runtime_gate_ledger")
    door.reject_at = door.calls + offset
    with global_write_commit_scope(door), pytest.raises(LocalControlCoordinatorUnavailable) as caught:
        store.redact_tool_ledger_content(tool="remember", contents={SECRET})
    assert caught.value is door.error
    assert door.calls == door.reject_at
    if offset < 3:
        assert rows(store, "runtime_gate_ledger") == before
    else:
        assert SECRET not in str(rows(store, "runtime_gate_ledger"))


@pytest.mark.parametrize("table,change", [("runtime_gate_ledger", "update"), ("tool_operations", "update"),
                                          ("runtime_gate_ledger", "insert"), ("tool_operations", "delete")])
def test_full_raw_cas_detects_concurrent_changes_before_any_redaction_dml(tmp_path, monkeypatch, table, change):
    store, door = store_with_source(tmp_path)
    original = ledger._prepare_ledger_redaction
    expected = []
    def prepare_then_change(*args):
        plan = original(*args)
        with sqlite3.connect(store.db_path) as conn:
            if change == "update":
                conn.execute(f"UPDATE {table} SET updated_at=999 WHERE operation_id='op-0'")
            elif change == "delete":
                conn.execute(f"DELETE FROM {table} WHERE operation_id='op-0'")
            else:
                conn.execute("INSERT INTO runtime_gate_ledger(run_id,operation_id,tool,parameters_json,runtime_gate_json,created_at,updated_at) VALUES('r','new','remember','{}','{}',0,0)")
        expected.extend([rows(store, "runtime_gate_ledger"), rows(store, "tool_operations")])
        return plan
    monkeypatch.setattr(ledger, "_prepare_ledger_redaction", prepare_then_change)
    with global_write_commit_scope(door), pytest.raises(BlockingIOError):
        store.redact_tool_ledger_content(tool="remember", contents={SECRET})
    assert [rows(store, "runtime_gate_ledger"), rows(store, "tool_operations")] == expected


def test_unrelated_tool_change_does_not_conflict(tmp_path, monkeypatch):
    store, door = store_with_source(tmp_path)
    original = ledger._prepare_ledger_redaction
    def prepare_then_change(*args):
        plan = original(*args)
        with sqlite3.connect(store.db_path) as conn:
            conn.execute("INSERT INTO runtime_gate_ledger(run_id,operation_id,tool,parameters_json,runtime_gate_json,created_at,updated_at) VALUES('r','unrelated','other','{}','{}',0,0)")
        return plan
    monkeypatch.setattr(ledger, "_prepare_ledger_redaction", prepare_then_change)
    with global_write_commit_scope(door):
        assert store.redact_tool_ledger_content(tool="remember", contents={SECRET}) == 2


def test_json_scanning_outside_root_and_actual_local_transaction_measurement(tmp_path, monkeypatch, record_property):
    store, door = store_with_source(tmp_path, count=12)
    redact = ledger._redact_json_text
    observed = []
    def scanner(raw, variants):
        assert not door.inside
        observed.append(len(str(raw).encode("utf-8")))
        return redact(raw, variants)
    monkeypatch.setattr(ledger, "_redact_json_text", scanner)
    start = len(door.durations)
    with global_write_commit_scope(door):
        assert store.redact_tool_ledger_content(tool="remember", contents={SECRET}) == 24
    record_property("matched_rows", 24)
    record_property("json_input_bytes", sum(observed))
    record_property("max_local_root_seconds", max(door.durations[start:]))
    assert len(observed) == 36


def test_redaction_sql_failure_rolls_back_without_new_compensation(tmp_path, monkeypatch):
    store, door = store_with_source(tmp_path)
    before = [rows(store, "runtime_gate_ledger"), rows(store, "tool_operations")]
    original = ledger._prepare_ledger_redaction
    def corrupt_target(*args):
        plan = original(*args)
        return ledger._LedgerRedactionPlan(plan.runtime, plan.operations, plan.runtime_updates,
            (("new", "owner", "r", object()),), plan.db_identity, plan.content_bytes)
    monkeypatch.setattr(ledger, "_prepare_ledger_redaction", corrupt_target)
    with global_write_commit_scope(door), pytest.raises(sqlite3.ProgrammingError):
        store.redact_tool_ledger_content(tool="remember", contents={SECRET})
    assert [rows(store, "runtime_gate_ledger"), rows(store, "tool_operations")] == before


def test_actual_jsonl_remove_rejects_ledger_source_before_formal_write(tmp_path):
    store, door = store_with_source(tmp_path)
    memory = JsonlMemory(tmp_path / "memory.jsonl", local_store=store)
    with global_write_commit_scope(door):
        record = memory.add("user", SECRET)
    before = memory.path.read_bytes()
    door.reject_at = door.calls + 1
    with global_write_commit_scope(door), pytest.raises(LocalControlCoordinatorUnavailable) as caught:
        memory.remove(record.entry_id)
    assert caught.value is door.error
    assert memory.path.read_bytes() == before
    assert SECRET in str(rows(store, "runtime_gate_ledger"))


def test_raw_cas_does_not_fold_numeric_types():
    assert not ledger._ledger_snapshot_equal((("x",), ((1,),)), (("x",), ((1.0,),)))
