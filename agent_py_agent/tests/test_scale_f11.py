"""W8 #119：按真实目录规模验证有界枚举、恢复公平性和持久变更责任。"""
from __future__ import annotations

import json
import os
import resource
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import input_delivery_service as inputs
from agent_py_agent.agent.gateway_parts import recovery
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root


# LLM: 包装真实读句柄，不改内容、close或异常；C层SQLite页读不混入此Python字节计数。
# 函数用途: 累计规范文件实际返回的字节量。
def _count_read_bytes(handle, total):
    if "r" not in handle.mode and "+" not in handle.mode:
        return handle
    original = handle.read

    # LLM: 返回值和异常来自原句柄，文本按UTF-8计数。
    # 函数用途: 累计一次真实read结果。
    def read(*args, **options):
        data = original(*args, **options)
        total[0] += len(data.encode("utf-8") if isinstance(data, str) else data)
        return data

    handle.read = read
    return handle


# LLM: 标准库 fstat 不打开新文件，直接比较每拍结束的有效描述符。
# 函数用途: 取得进程当前 FD 数量。
def _fd_count():
    total = 0
    for descriptor in range(1024):
        try:
            os.fstat(descriptor)
        except OSError:
            continue
        total += 1
    return total


# LLM: 回执样本用原codec和摘要规则生成，不能用只有state字段的假规范通过结算确认。
# 函数用途: 构造可被真实回执读口验证的consumed样本。
def _consumed_payload(request_id):
    digest = f"digest-{request_id}"
    prepared = {"id": request_id, "kind": "ask", "goal": "original input", "user_id": "actor",
                "metadata": {"client_input_digest": digest, "channel": "chat"}}
    return inputs.GatewayInputReceipt(request_id, digest, "message", "consumed", prepared,
        inputs._prepared_payload_digest(prepared), "guidance-key", updated_at=1.0).to_dict()


# LLM: 规模测试只隔离深层业务修复，不替代另组真实终态证明；目录、canonical writer 和分页入口均为真实实现。
# 函数用途: 复现一个新提交触发全目录 glob 与回执全量 scandir 的问题。
@pytest.mark.parametrize("size", [10000, 100000])
def test_new_commit_and_receipt_tick_are_bounded_at_scale(tmp_path, monkeypatch, size):
    paths = gateway_paths_from_root(tmp_path)
    receipts = paths.root / "input_receipts"
    paths.terminal.mkdir(parents=True)
    receipts.mkdir(parents=True)
    for number in range(size):
        name = f"gwreq-old-{number:06d}"
        (paths.terminal / f"{name}.json").write_text(json.dumps({"id": name}), encoding="utf-8")
        (receipts / f"{name}.json").write_text(json.dumps(_consumed_payload(name)), encoding="utf-8")
    new_id = "gwreq-new-commit"
    write_json_file_atomic(paths.terminal / f"{new_id}.json", {"id": new_id})
    write_json_file_atomic(receipts / f"{new_id}.json", _consumed_payload(new_id))
    enumerated = []
    real_glob = Path.glob

    # LLM: 观测真正 yield 的目录条目，而不是只计算 glob 被调用几次。
    # 函数用途: 让全目录枚举在断言里留下实际规模证据。
    def glob(directory, pattern, *args, **kwargs):
        for path in real_glob(directory, pattern, *args, **kwargs):
            if directory == paths.terminal:
                enumerated.append(path.name)
            yield path

    repaired = set()
    monkeypatch.setattr(Path, "glob", glob)
    monkeypatch.setattr(recovery, "_terminal_projection_marker_matches",
                        lambda _paths, path: path.stem != new_id or path.stem in repaired)
    monkeypatch.setattr(recovery, "_recover_committed_terminal_processing",
                        lambda _p, _c, request_id, **_kw: repaired.add(request_id))
    from agent_py_agent.agent.gateway_parts import recovery_pending_index as pending
    byte_count, native_pages, decodes = [0], [], []
    real_open, real_fdopen, real_page, real_load = Path.open, os.fdopen, pending.directory_page, json.loads

    # LLM: 保留真实打开及关闭，只度量读结果。
    # 函数用途: 观测Path读口字节量。
    def opened(path, *args, **options):
        return _count_read_bytes(real_open(path, *args, **options), byte_count)

    # LLM: 描述符来自原no-follow入口，不替换权限裁决。
    # 函数用途: 观测描述符读口字节量。
    def fdopened(*args, **options):
        return _count_read_bytes(real_fdopen(*args, **options), byte_count)

    # LLM: 原生读取每次硬上界32KiB，实际yield数量由真实页返回。
    # 函数用途: 证明目录项和读取上界不随历史增加。
    def page(*args, **options):
        result = real_page(*args, **options)
        native_pages.append(len(result[0]))
        assert len(result[0]) <= 128
        return result

    # LLM: 仍使用真实JSON解析器。
    # 函数用途: 观测tick解析次数。
    def load(*args, **options):
        decodes.append(1)
        return real_load(*args, **options)

    monkeypatch.setattr(Path, "open", opened)
    monkeypatch.setattr(os, "fdopen", fdopened)
    monkeypatch.setattr(pending, "directory_page", page)
    monkeypatch.setattr(json, "loads", load)
    baseline = _fd_count()
    started, usage = time.perf_counter(), resource.getrusage(resource.RUSAGE_SELF)
    projected, errors = recovery.repair_gateway_terminal_projections(paths, agent=None, limit=16)
    assert errors == [] and projected <= 16
    assert len(enumerated) <= 128
    assert _fd_count() == baseline
    assert new_id in repaired
    visits = []

    # LLM: 只替代单条业务步骤；候选选择、SQLite、目录发现及扫描上限仍由产品负责。
    # 函数用途: 观测入口真正送入对账的回执集合。
    def consume(_paths, _agent, path, summary):
        visits.append(path.name)
        summary["checked"] += 1
        summary["consumed"] += 1

    monkeypatch.setattr(inputs, "_reconcile_receipt_path", consume)
    summary = inputs.reconcile_gateway_input_receipts(paths, SimpleNamespace(), limit=64)
    assert summary["errors"] == 0 and len(visits) <= 64
    assert _fd_count() == baseline
    assert f"{new_id}.json" in visits
    first_visits = set(visits)
    visits.clear()
    summary = inputs.reconcile_gateway_input_receipts(paths, SimpleNamespace(), limit=64)
    assert summary["errors"] == 0 and len(visits) <= 64 and not first_visits.intersection(visits)
    assert _fd_count() == baseline
    assert byte_count[0] < 1024 * 1024 and len(decodes) < 1024
    assert len(native_pages) <= 3
    print("scale_f11", {"history": size, "python_bytes_read": byte_count[0], "json_decodes": len(decodes),
        "native_items": native_pages, "native_read_upper_bytes": len(native_pages) * 32768,
        "elapsed_s": time.perf_counter()-started,
        "input_blocks": resource.getrusage(resource.RUSAGE_SELF).ru_inblock-usage.ru_inblock, "fd_delta": 0})


# LLM: 仅测试发现队列，不替代canonical业务核验；异常文件必须保留，重启也不得重复卡住同页。
# 函数用途: 验证不合法旧文件名不会阻止合法pending及目录游标推进。
@pytest.mark.parametrize("kind", ["terminal", "receipt"])
def test_invalid_discovery_names_do_not_block_valid_responsibilities(tmp_path, kind, caplog):
    from agent_py_agent.agent.gateway_parts.recovery_pending_index import RecoveryPendingIndex

    directory = tmp_path / ("requests/terminal" if kind == "terminal" else "input_receipts")
    directory.mkdir(parents=True)
    invalid = (directory / ".json", directory / "bad:name.json")
    for path in invalid:
        path.write_text("preserved invalid name", encoding="utf-8")
    (directory / "valid.json").write_text('{"id":"valid"}', encoding="utf-8")
    errors = []
    baseline = _fd_count()
    for _ in range(3):
        index = RecoveryPendingIndex(tmp_path)
        try:
            index.refresh(kind, directory)
        except ValueError as exc:
            errors.append(str(exc))
        assert _fd_count() == baseline
    assert errors == []
    assert [key for key, _version in index.page(kind)] == ["valid"]
    assert all(path.read_text(encoding="utf-8") == "preserved invalid name" for path in invalid)
    with index.connect() as db:
        diagnostic = db.execute("SELECT value FROM meta WHERE key=?", (f"invalid_{kind}",)).fetchone()
    assert diagnostic is not None
    report = json.loads(diagnostic[0])
    assert report["count"] == 2 and set(report["names"]) == {".json", "bad:name.json"}
    assert any("RECOVERY_DISCOVERY_INVALID_NAMES" in row.message for row in caplog.records)


# LLM: 在SQLite真正打开前观测权限，不能靠连接后的chmod冒充私有边界；使用的仅为本测试临时目录。
# 函数用途: 验证升级遗留的宽权限目录被安全收紧后才允许SQLite访问。
@pytest.mark.parametrize("mode", [0o755, 0o770, 0o777])
def test_projection_parent_is_private_before_sqlite_connect(tmp_path, monkeypatch, mode):
    from agent_py_agent.agent.gateway_parts import recovery_pending_index as pending

    directory = tmp_path / "projection_state"
    directory.mkdir()
    directory.chmod(mode)
    path = directory / "sample.sqlite3"
    real_connect = pending.sqlite3.connect
    seen_modes = []

    # LLM: 仍连接真实SQLite，只记录进入连接时的受管父目录模式。
    # 函数用途: 区分打开前收紧与打开后补救。
    def connect(*args, **kwargs):
        seen_modes.append(directory.stat().st_mode & 0o777)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(pending.sqlite3, "connect", connect)
    baseline = _fd_count()
    with pending.private_projection_connection(path) as db:
        assert db.execute("SELECT 1").fetchone() == (1,)
    assert seen_modes == [0o700]
    assert path.stat().st_mode & 0o777 == 0o600
    assert _fd_count() == baseline


# LLM: 在原锁获取窗口交换受管父目录，保留外部样本；实现必须在SQLite重开路径前拒绝身份漂移。
# 函数用途: 验证静态no-follow之外的父目录身份复核。
def test_projection_parent_replacement_is_rejected_before_sqlite(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from agent_py_agent.agent.gateway_parts import recovery_pending_index as pending

    parent, outside = tmp_path / "projection_state", tmp_path / "outside"
    parent.mkdir(mode=0o700)
    outside.mkdir(mode=0o700)
    path = parent / "sample.sqlite3"
    real_connect, real_lock = pending.sqlite3.connect, pending.locked_json_path
    with real_connect(outside / path.name) as db:
        db.execute("CREATE TABLE sentinel(value)")
    original_bytes = (outside / path.name).read_bytes()
    connections = []

    # LLM: 只在本样本锁窗口改目录，不影响其它调用；外部目录实际位于同一个测试tmp_path。
    # 函数用途: 在初检之后、SQLite之前稳定制造路径换代。
    @contextmanager
    def replace_parent(target, **options):
        with real_lock(target, **options):
            parent.rename(tmp_path / "retained-parent")
            parent.symlink_to(outside, target_is_directory=True)
            yield

    # LLM: 记录SQLite是否被提前打开，不伪造连接成功。
    # 函数用途: 证明拒绝发生在可能的SQLite副作用之前。
    def connect(*args, **kwargs):
        connections.append(args)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(pending, "locked_json_path", replace_parent)
    monkeypatch.setattr(pending.sqlite3, "connect", connect)
    baseline = _fd_count()
    with pytest.raises((ValueError, OSError)):
        with pending.private_projection_connection(path) as db:
            db.execute("CREATE TABLE unexpected_write(value)")
    assert connections == []
    assert (outside / path.name).read_bytes() == original_bytes
    assert _fd_count() == baseline


# LLM: 多进程consumer仅共用这两个冻结helper；规范consumed须在原锁内核codec/摘要，cursor写入不是结算。
# 函数用途: 验证仅使用共享候选与cursor的consumer也不反复领取不变的consumed历史。
def test_shared_receipt_helper_acknowledges_consumed_without_cursor_authority(tmp_path):
    from agent_py_agent.agent.gateway_parts.recovery_pending_index import RecoveryPendingIndex

    paths = gateway_paths_from_root(tmp_path)
    for number in range(3):
        name = f"input-{number}"
        write_json_file_atomic(paths.root / "input_receipts" / f"{name}.json", _consumed_payload(name))
    page = inputs._reconcile_receipt_paths(paths, limit=64)
    assert {path.stem for path in page} == {"input-0", "input-1", "input-2"}
    assert RecoveryPendingIndex(tmp_path).page("receipt") == (), "不靠cursor确认也必须核实规范consumed并清投影责任"
    for path in page:
        assert inputs.read_gateway_input_receipt(paths, path.stem).state == "consumed"
        inputs._commit_reconcile_cursor(paths, path, write_cursor=write_json_file_atomic)
    for _ in range(3):
        assert inputs._reconcile_receipt_paths(paths, limit=64) == ()
    changed = page[0]
    payload = _consumed_payload(changed.stem)
    payload["prepared_payload_digest"] = "invalid"
    write_json_file_atomic(changed, payload)
    assert inputs._reconcile_receipt_paths(paths, limit=64) == (changed,)
    assert [key for key, _version in RecoveryPendingIndex(tmp_path).page("receipt")] == [changed.stem]


# LLM: 历史投影也是SQLite消费者；投影侧车不能借规范history锁绕过no-follow，连接本身也可能有恢复副作用。
# 函数用途: 验证历史数据库在真正打开前拒绝外部侧车链接。
@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_history_projection_rejects_linked_sidecars_before_connect(tmp_path, monkeypatch, suffix):
    from agent_py_agent.agent.gateway_parts import gateway_history_index as history

    paths = gateway_paths_from_root(tmp_path)
    index = history.GatewayHistoryIndex(paths)
    outside = tmp_path / "sentinel"
    outside.write_bytes(b"preserved external sidecar")
    index.path.with_name(index.path.name + suffix).symlink_to(outside)
    real_connect = history.sqlite3.connect
    connections, errors = [], []

    # LLM: 仍使用真实SQLite，记录是否越过拒门。
    # 函数用途: 检测先打开再补救的错误顺序。
    def connect(*args, **options):
        connections.append(args)
        return real_connect(*args, **options)

    monkeypatch.setattr(history.sqlite3, "connect", connect)
    try:
        with index.connection():
            pass
    except (ValueError, OSError, history.sqlite3.Error) as exc:
        errors.append(type(exc).__name__)
    assert errors and connections == []
    assert outside.read_bytes() == b"preserved external sidecar"


# LLM: 只在原修复前建立线程屏障；提交、完成、历史与最终修复均走真实入口，不伪造终态权威。
# 函数用途: 验证tick期间同键新版本及另一终态提交保留责任，业务补投影各执行一次。
def test_real_terminal_commit_during_tick_preserves_versions_and_settles_once(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.gateway_history_index import GatewayHistoryIndex
    from agent_py_agent.agent.gateway_parts.recovery_pending_index import RecoveryPendingIndex
    from agent_py_agent.tests.test_gateway_terminal_projection_scan import (
        _ensure_gateway_dirs,
        _make_recovery_agent,
        _marker_path,
        _projection_path,
        _submit_real_terminal,
        gateway_paths,
    )

    agent = _make_recovery_agent(tmp_path)
    paths = gateway_paths(agent)
    _ensure_gateway_dirs(paths)
    first = _submit_real_terminal(agent, paths)
    _marker_path(paths, first).unlink()
    _projection_path(paths, first).unlink()
    entered, release = threading.Event(), threading.Event()
    original = recovery._recover_committed_terminal_processing
    calls, reports, errors = [], [], []

    # LLM: 屏障不持canonical或history锁，主线程仍用原writer完成另一真实请求。
    # 函数用途: 固定已领取候选、尚未执行原修复的交错点。
    def repair(*args, **options):
        if not entered.is_set():
            entered.set()
            assert release.wait(10)
        calls.append(args[2])
        return original(*args, **options)

    # LLM: 回传线程异常，不能以join完成冒充业务成功。
    # 函数用途: 保存一个有界恢复tick的真实结果。
    def tick():
        try:
            reports.append(recovery.repair_gateway_terminal_projections(paths, agent=agent, limit=1))
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(recovery, "_recover_committed_terminal_processing", repair)
    worker = threading.Thread(target=tick)
    worker.start()
    try:
        assert entered.wait(10)
        path = paths.terminal / f"{first}.json"
        write_json_file_atomic(path, json.loads(path.read_text(encoding="utf-8")))
        second = _submit_real_terminal(agent, paths)
        _marker_path(paths, second).unlink()
        _projection_path(paths, second).unlink()
    finally:
        release.set()
        worker.join(15)
    assert not worker.is_alive() and errors == [] and reports == [(1, [])]
    index = RecoveryPendingIndex(paths.root)
    assert {key for key, _version in index.page("terminal")} == {first, second}
    for _ in range(4):
        _count, failures = recovery.repair_gateway_terminal_projections(paths, agent=agent, limit=16)
        assert failures == []
    assert calls.count(first) == calls.count(second) == 1 and len(calls) == 2
    assert index.page("terminal") == ()
    assert all(_marker_path(paths, key).exists() and _projection_path(paths, key).exists() for key in (first, second))
    rows = GatewayHistoryIndex(paths).page(after="", limit=128)
    assert len(rows) == 2 and {row["id"] for row in rows} == {first, second}
