"""W1/R1回执扫描：稳定名字只枚举一次，正文仍按有界页复核，不能掩蔽原地改写。"""
import json
import os
import time
from dataclasses import replace

import pytest

from agent_py_agent.agent.gateway_parts import input_delivery_service as delivery
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.tests.test_gateway_input_delivery import _receipts


# LLM: 使用真实回执及原游标，不替换枚举或状态机；预建state以隔离自身目录创建。
# 函数用途: 准备稳定且能进入正式名字缓存的回执目录。
def _settled_directory(tmp_path, count):
    paths = _receipts(tmp_path, ["consumed"] * count)
    directory = paths.root / "input_receipts"
    (directory / "state").mkdir(exist_ok=True)
    old = time.time_ns() - 5_000_000_000
    os.utime(directory, ns=(old, old))
    return paths


# LLM: 只记录正式分页入口和游标提交，仍运行原函数，不伪造扫描结果。
# 函数用途: 区分真实分页检查与名字投影缓存命中，验证每页游标确实落盘。
def _observe_passes(monkeypatch):
    scans, commits = [], []
    original_scan, original_commit = delivery._reconcile_receipt_paths, delivery._commit_reconcile_cursor

    def scan(*args, **kwargs):
        scans.append(1)
        return original_scan(*args, **kwargs)

    def commit(*args, **kwargs):
        commits.append(1)
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(delivery, "_reconcile_receipt_paths", scan)
    monkeypatch.setattr(delivery, "_commit_reconcile_cursor", commit)
    return scans, commits


# LLM: 530条超过512条消费戳LRU及64条页上限；完整轮转后仍逐页核文件，不以目录戳判正文未改。
# 函数用途: 验证每轮有界、每页提交游标，而稳定名字列表只枚举一次。
def test_receipts_recheck_bounded_pages_after_complete_consumed_sweep(tmp_path, monkeypatch):
    paths = _settled_directory(tmp_path, 530)
    scans, commits = _observe_passes(monkeypatch)
    enumerations = _observe_enumerations(monkeypatch, paths)
    checked = 0
    for _ in range(9):
        summary = delivery.reconcile_gateway_input_receipts(paths, object(), limit=64)
        assert summary["errors"] == summary["terminal_unknown_errors"] == 0
        assert summary["checked"] == summary["consumed"] == 64
        checked += summary["checked"]
    assert checked >= 530 and len(scans) == len(commits) == 9
    summary = delivery.reconcile_gateway_input_receipts(paths, object(), limit=64)
    assert summary["checked"] == summary["consumed"] == 64
    assert summary["errors"] == summary["terminal_unknown_errors"] == 0
    assert len(scans) == len(commits) == 10
    assert enumerations == [paths.root / "input_receipts"]


def _observe_enumerations(monkeypatch, paths):
    calls = []
    original = delivery.os.scandir
    directory = paths.root / "input_receipts"

    def scan(path):
        if path == directory:
            calls.append(path)
        return original(path)

    monkeypatch.setattr(delivery.os, "scandir", scan)
    return calls


# LLM: 单页consumed不代表后页无queued；保原物化入口，名字缓存不能掩蔽待办。
# 函数用途: 验证后页未消费回执在原轮数内被轮到，且每页仍受64条上限约束。
def test_receipt_gate_does_not_hide_unconsumed_later_page(tmp_path, monkeypatch):
    paths = _settled_directory(tmp_path, 70)
    receipt = delivery.read_gateway_input_receipt(paths, "receipt-069")
    write_json_file_atomic(delivery.gateway_input_receipt_path(paths, receipt.request_id), replace(receipt, state="queued").to_dict())
    scans, commits = _observe_passes(monkeypatch)
    queued = 0
    for _ in range(4):
        summary = delivery.reconcile_gateway_input_receipts(paths, object(), limit=64)
        assert summary["checked"] == 64 and summary["errors"] == 0
        queued += summary["queued"]
    assert queued > 0 and len(scans) == len(commits) == 4
    assert delivery.read_gateway_input_receipt(paths, receipt.request_id).state == "queued"


# LLM: 名字投影按目录代次缓存；新增和原子替换下一轮可见，不等旧60秒周期。
# 函数用途: 验证新坏账立即上报，正式writer替换的queued立即恢复物化。
@pytest.mark.parametrize("change", ["new", "replace"])
def test_receipt_directory_change_invalidates_gate(tmp_path, monkeypatch, change):
    paths = _settled_directory(tmp_path, 2)
    scans, commits = _observe_passes(monkeypatch)
    enumerations = _observe_enumerations(monkeypatch, paths)
    for _ in range(2):
        summary = delivery.reconcile_gateway_input_receipts(paths, object())
        assert summary["checked"] == summary["consumed"] == 2 and summary["errors"] == 0
    receipt = delivery.read_gateway_input_receipt(paths, "receipt-001")
    if change == "new":
        target = paths.root / "input_receipts" / "receipt-new.json"
        target.write_text("{}")
    else:
        write_json_file_atomic(delivery.gateway_input_receipt_path(paths, receipt.request_id), replace(receipt, state="queued").to_dict())
    summary = delivery.reconcile_gateway_input_receipts(paths, object())
    assert summary["checked"] == 2
    assert summary["errors"] == (1 if change == "new" else 0)
    assert summary["queued"] == (0 if change == "new" else 1)
    assert len(scans) == len(commits) == 3
    assert enumerations == [paths.root / "input_receipts"] * 2


# LLM: 原地改写不改变目录戳；稳定名字缓存可复用，回执正文必须立即复核，不能等60秒到期。
# 函数用途: 验证热命中不掩蔽consumed变queued，跨旧复查周期仍不额外枚举目录。
def test_receipt_in_place_change_is_visible_before_sweep_expiry(tmp_path, monkeypatch):
    paths = _settled_directory(tmp_path, 2)
    clock = [100.0]
    monkeypatch.setattr(delivery.time, "monotonic", lambda: clock[0])
    scans, commits = _observe_passes(monkeypatch)
    enumerations = _observe_enumerations(monkeypatch, paths)
    for clock[0] in (100.0, 110.0):
        summary = delivery.reconcile_gateway_input_receipts(paths, object())
        assert summary["checked"] == summary["consumed"] == 2 and summary["errors"] == 0
    receipt = delivery.read_gateway_input_receipt(paths, "receipt-001")
    path = delivery.gateway_input_receipt_path(paths, receipt.request_id)
    directory_stamp = path.parent.stat().st_mtime_ns
    path.write_text(json.dumps(replace(receipt, state="queued").to_dict()), encoding="utf-8")
    assert path.parent.stat().st_mtime_ns == directory_stamp
    for clock[0] in (140.0, 159.9, 160.1):
        summary = delivery.reconcile_gateway_input_receipts(paths, object())
        assert summary["checked"] == 2 and summary["queued"] == summary["consumed"] == 1
        assert summary["errors"] == summary["terminal_unknown_errors"] == 0
    assert len(scans) == len(commits) == 5
    assert enumerations == [paths.root / "input_receipts"]
