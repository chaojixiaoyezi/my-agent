"""Q3：完整扫描证明无未消费回执之后，稳定目录短路，不能只凭一页consumed睡过去。"""
import os
import time
from dataclasses import replace

import pytest

from agent_py_agent.agent.gateway_parts import input_delivery_service as delivery
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.tests.test_gateway_input_delivery import _receipts


# LLM: 使用真实回执及原游标，老化目录只为越过现有粗时间戳保护窗口，不替换枚举或状态机。
# 函数用途: 准备能进入原目录缓存且已创建state子目录的回执目录。
def _settled_directory(tmp_path, count):
    paths = _receipts(tmp_path, ["consumed"] * count)
    directory = paths.root / "input_receipts"
    (directory / "state").mkdir(exist_ok=True)
    old = time.time_ns() - 5_000_000_000
    os.utime(directory, ns=(old, old))
    return paths


# LLM: 只记录正式分页入口和游标提交，仍运行原函数，不伪造扫描结果。
# 函数用途: 捕获短路时是否连候选查找及游标写入都跳过。
def _observe_passes(monkeypatch):
    scans, commits = [], []
    original_scan, original_commit = delivery._reconcile_receipt_paths, delivery._commit_reconcile_cursor

    # LLM: 调用正式分页路径，计数包含目录缓存命中，不把缓存查询冒充目录门短路。
    # 函数用途: 统计每次实际进入回执分页入口。
    def scan(*args, **kwargs):
        scans.append(1)
        return original_scan(*args, **kwargs)

    # LLM: 游标必须仍真实落盘，否则无法验证多页连续性。
    # 函数用途: 统计每次真实的扫描位置提交。
    def commit(*args, **kwargs):
        commits.append(1)
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(delivery, "_reconcile_receipt_paths", scan)
    monkeypatch.setattr(delivery, "_commit_reconcile_cursor", commit)
    return scans, commits


# LLM: 530条超过旧512条LRU，也超过64条单批上限；只有完整连续遍历后才准许跳过。
# 函数用途: 验证大目录完整轮转一次后稳定空闲检查为O(1)，不再碰分页或游标。
def test_receipts_gate_only_after_complete_consumed_sweep(tmp_path, monkeypatch):
    paths = _settled_directory(tmp_path, 530)
    scans, commits = _observe_passes(monkeypatch)
    checked = 0
    for _ in range(9):
        summary = delivery.reconcile_gateway_input_receipts(paths, object(), limit=64)
        assert summary["errors"] == 0
        checked += summary["checked"]
    assert checked >= 530 and len(scans) == len(commits) == 9
    summary = delivery.reconcile_gateway_input_receipts(paths, object(), limit=64)
    assert summary["checked"] == 0
    assert len(scans) == len(commits) == 9


# LLM: 单页的consumed不能证明后页没有queued；未消费状态不替换为已消费，不跳过原物化路径。
# 函数用途: 验证未扫描页中的未消费回执始终被轮到且不会错误挂起整个目录。
def test_receipt_gate_does_not_hide_unconsumed_later_page(tmp_path, monkeypatch):
    paths = _settled_directory(tmp_path, 70)
    receipt = delivery.read_gateway_input_receipt(paths, "receipt-069")
    write_json_file_atomic(delivery.gateway_input_receipt_path(paths, receipt.request_id), replace(receipt, state="queued").to_dict())
    old = time.time_ns() - 5_000_000_000
    os.utime(paths.root / "input_receipts", ns=(old, old))
    scans, _commits = _observe_passes(monkeypatch)
    for _ in range(4):
        summary = delivery.reconcile_gateway_input_receipts(paths, object(), limit=64)
        assert summary["checked"] > 0 and summary["errors"] == 0
    assert len(scans) == 4


# LLM: 原子更换回执会改目录戳，符合正式writer；必须在60秒周期核验以前立刻恢复扫描。
# 函数用途: 验证新回执或原子改写立即解除目录门。
@pytest.mark.parametrize("change", ["new", "replace"])
def test_receipt_directory_change_invalidates_gate(tmp_path, monkeypatch, change):
    paths = _settled_directory(tmp_path, 2)
    scans, _commits = _observe_passes(monkeypatch)
    delivery.reconcile_gateway_input_receipts(paths, object())
    assert delivery.reconcile_gateway_input_receipts(paths, object())["checked"] == 0
    receipt = delivery.read_gateway_input_receipt(paths, "receipt-001")
    if change == "new":
        target = paths.root / "input_receipts" / "receipt-new.json"
        target.write_text("{}")
    else:
        write_json_file_atomic(delivery.gateway_input_receipt_path(paths, receipt.request_id), replace(receipt, state="queued").to_dict())
    assert delivery.reconcile_gateway_input_receipts(paths, object())["checked"] > 0
    assert len(scans) == 2


# LLM: 原地违规写可能不改父目录；用单调时钟验证60秒核验不会因热命中滑动续期。
# 函数用途: 验证目录门即使戳未变也会定时重新检查真实回执。
def test_receipt_gate_expires_without_sliding_on_hits(tmp_path, monkeypatch):
    paths = _settled_directory(tmp_path, 2)
    clock = [100.0]
    monkeypatch.setattr(delivery.time, "monotonic", lambda: clock[0])
    scans, _commits = _observe_passes(monkeypatch)
    delivery.reconcile_gateway_input_receipts(paths, object())
    for clock[0] in (110.0, 140.0, 159.9):
        assert delivery.reconcile_gateway_input_receipts(paths, object())["checked"] == 0
    clock[0] = 160.1
    assert delivery.reconcile_gateway_input_receipts(paths, object())["checked"] == 2
    assert len(scans) == 2
