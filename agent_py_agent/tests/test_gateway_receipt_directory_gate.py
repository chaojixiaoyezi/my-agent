"""回执 pending 投影：有界分页、已结责任退出、变化重新入队，不回退全目录 glob。"""
import json
import os
import time
from dataclasses import replace

import pytest

from agent_py_agent.agent.gateway_parts import input_delivery_service as delivery
from agent_py_agent.agent.gateway_parts import recovery_pending_index as recovery_index
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.tests.test_gateway_input_delivery import _receipts


def _settled_directory(tmp_path, count):
    paths = _receipts(tmp_path, ["consumed"] * count)
    directory = paths.root / "input_receipts"
    (directory / "state").mkdir(exist_ok=True)
    old = time.time_ns() - 5_000_000_000
    os.utime(directory, ns=(old, old))
    return paths


def _observe_passes(monkeypatch):
    scans, commits = [], []
    original_scan, original_commit = delivery._reconcile_receipt_paths, delivery._commit_reconcile_cursor

    def scan(*args, **kwargs):
        page = original_scan(*args, **kwargs)
        scans.append(tuple(path.stem for path in page))
        assert len(page) <= 64
        return page

    def commit(*args, **kwargs):
        commits.append(1)
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(delivery, "_reconcile_receipt_paths", scan)
    monkeypatch.setattr(delivery, "_commit_reconcile_cursor", commit)
    return scans, commits


def _observe_enumerations(monkeypatch, paths):
    calls = []
    original = recovery_index.directory_page
    directory = paths.root / "input_receipts"

    def scan(path, *args, **kwargs):
        page = original(path, *args, **kwargs)
        if path == directory:
            calls.append(page[0])
            assert len(page[0]) <= 128
        return page

    original_scandir = delivery.os.scandir

    def no_full_scan(path):
        assert path != directory, "恢复热路不能退回无界目录枚举"
        return original_scandir(path)

    monkeypatch.setattr(recovery_index, "directory_page", scan)
    monkeypatch.setattr(delivery.os, "scandir", no_full_scan)
    return calls


def test_receipts_recheck_bounded_pages_after_complete_consumed_sweep(tmp_path, monkeypatch):
    paths = _settled_directory(tmp_path, 530)
    scans, commits = _observe_passes(monkeypatch)
    enumerations = _observe_enumerations(monkeypatch, paths)
    checked = []
    for _ in range(9):
        summary = delivery.reconcile_gateway_input_receipts(paths, object(), limit=64)
        assert summary["errors"] == summary["terminal_unknown_errors"] == 0
        assert summary["checked"] == summary["consumed"]
        checked.append(summary["checked"])
    assert checked == [64] * 8 + [18]
    # 不重复读取已确认的消费历史；页内及跨页无遗漏、无重复，尾页仍提交原游标。
    visited = [key for page in scans for key in page]
    assert set(visited) == {f"receipt-{number:03d}" for number in range(530)}
    assert len(visited) == len(set(visited)) == 530
    assert len(scans) == len(commits) == 9
    summary = delivery.reconcile_gateway_input_receipts(paths, object(), limit=64)
    assert summary["checked"] == summary["consumed"] == 0
    assert summary["errors"] == summary["terminal_unknown_errors"] == 0
    assert len(scans) == 10 and len(commits) == 9
    assert 0 < len(enumerations) <= len(scans)


def test_receipt_gate_does_not_hide_unconsumed_later_page(tmp_path, monkeypatch):
    paths = _settled_directory(tmp_path, 70)
    receipt = delivery.read_gateway_input_receipt(paths, "receipt-069")
    write_json_file_atomic(delivery.gateway_input_receipt_path(paths, receipt.request_id), replace(receipt, state="queued").to_dict())
    scans, commits = _observe_passes(monkeypatch)
    _observe_enumerations(monkeypatch, paths)
    summaries = [delivery.reconcile_gateway_input_receipts(paths, object(), limit=64) for _ in range(4)]
    assert [row["checked"] for row in summaries] == [64, 6, 1, 1]
    assert all(row["errors"] == row["terminal_unknown_errors"] == 0 for row in summaries)
    assert sum(row["consumed"] for row in summaries) == 69
    assert sum(row["queued"] for row in summaries) == 3
    assert scans[-2:] == [(receipt.request_id,), (receipt.request_id,)]
    assert len(scans) == len(commits) == 4
    assert delivery.read_gateway_input_receipt(paths, receipt.request_id).state == "queued"


@pytest.mark.parametrize("change", ["new", "replace"])
def test_receipt_directory_change_invalidates_gate(tmp_path, monkeypatch, change):
    paths = _settled_directory(tmp_path, 2)
    scans, commits = _observe_passes(monkeypatch)
    enumerations = _observe_enumerations(monkeypatch, paths)
    first = delivery.reconcile_gateway_input_receipts(paths, object())
    assert first["checked"] == first["consumed"] == 2 and first["errors"] == 0
    idle = delivery.reconcile_gateway_input_receipts(paths, object())
    assert idle["checked"] == idle["consumed"] == idle["errors"] == 0
    receipt = delivery.read_gateway_input_receipt(paths, "receipt-001")
    untouched = delivery.gateway_input_receipt_path(paths, "receipt-000").read_bytes()
    if change == "new":
        target = paths.root / "input_receipts" / "receipt-new.json"
        target.write_text("{}")
    else:
        write_json_file_atomic(delivery.gateway_input_receipt_path(paths, receipt.request_id), replace(receipt, state="queued").to_dict())
    summary = delivery.reconcile_gateway_input_receipts(paths, object())
    assert summary["checked"] == (0 if change == "new" else 1)
    assert summary["errors"] == (1 if change == "new" else 0)
    assert summary["queued"] == (0 if change == "new" else 1)
    assert summary["consumed"] == 0
    assert scans[-1] == (("receipt-new",) if change == "new" else (receipt.request_id,))
    assert delivery.gateway_input_receipt_path(paths, "receipt-000").read_bytes() == untouched
    assert len(scans) == 3 and len(commits) == 2
    assert len(enumerations) == 3


def test_receipt_in_place_change_is_visible_before_sweep_expiry(tmp_path, monkeypatch):
    paths = _settled_directory(tmp_path, 2)
    clock = [100.0]
    monkeypatch.setattr(delivery.time, "monotonic", lambda: clock[0])
    scans, commits = _observe_passes(monkeypatch)
    enumerations = _observe_enumerations(monkeypatch, paths)
    first = delivery.reconcile_gateway_input_receipts(paths, object())
    assert first["checked"] == first["consumed"] == 2 and first["errors"] == 0
    clock[0] = 110.0
    idle = delivery.reconcile_gateway_input_receipts(paths, object())
    assert idle["checked"] == idle["consumed"] == idle["errors"] == 0
    receipt = delivery.read_gateway_input_receipt(paths, "receipt-001")
    path = delivery.gateway_input_receipt_path(paths, receipt.request_id)
    directory_stamp = path.parent.stat().st_mtime_ns
    path.write_text(json.dumps(replace(receipt, state="queued").to_dict()), encoding="utf-8")
    assert path.parent.stat().st_mtime_ns == directory_stamp
    for clock[0] in (140.0, 159.9, 160.1):
        summary = delivery.reconcile_gateway_input_receipts(paths, object())
        assert summary["checked"] == summary["queued"] == 1 and summary["consumed"] == 0
        assert summary["errors"] == summary["terminal_unknown_errors"] == 0
        assert scans[-1] == (receipt.request_id,)
    assert len(scans) == 5 and len(commits) == 4
    # EOF 后只逐页审计已知键；原地变化不要求重新枚举目录，也不等旧60秒期限。
    assert len(enumerations) == 2
