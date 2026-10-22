"""E8 终态投影扫描提速的回归测试。

覆盖：
- 稳态（目录时间戳没变且已沿文件名追平目录末尾）不再全量 glob 终态目录：打桩计数证明；
- 未追平（积压多于每轮上限）时继续推进，直到追平后才允许跳过；
- 新终态文件出现（目录变化）后限定轮数内补齐投影；
- 每轮工作量上限（limit）；
- 崩溃场景：终态已写但投影缺失 / 标记被改坏 / 回执缺失，由启动全扫或低频全量复核补齐；
- 调用方（gateway_loops）启动后第一次投影扫描强制全量，之后走稳态节流。

背景：修复前 repair_gateway_terminal_projections 每 0.75 秒 sorted(glob("*.json")) 全部历史终态
（测试机几千个、本机生产约 4400 个），已确认完整的投影被永远循环复查。现在按"目录增删时间戳 +
游标追平状态"节流，1 小时低频全量复核兜住运行中被外部改坏的情况。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from agent_py_agent.agent.gateway_parts import (
    GatewayAskParams,
    _process_gateway_requests,
    gateway_paths,
    read_json_file,
    submit_gateway_ask,
    write_json_file,
)
from agent_py_agent.agent.gateway_parts.io import (
    gateway_request_fingerprint,
    read_json_file_report,
)
from agent_py_agent.agent.gateway_parts.recovery import (
    repair_gateway_terminal_projections,
)
from agent_py_agent.cli import gateway_loops
from agent_py_agent.tests.test_gateway_admission_wait import _paths
from agent_py_agent.tests.test_gateway_dispatcher_resilience import _stub_dispatcher
from agent_py_agent.tests.test_local_store_gateway_recovery import (
    _ensure_gateway_dirs,
    _make_recovery_agent,
)

_CURSOR_RELATIVE = Path("projection_state") / "terminal_cursor.json"


def _submit_real_terminal(agent, paths) -> str:
    """走真实流程造一个完整终态请求（完成目录投影、回执、历史、标记都齐）。"""
    request_id, _request_path, _created = submit_gateway_ask(
        paths, params=GatewayAskParams(prompt="终态扫描样本", save=False)
    )
    assert _process_gateway_requests(agent, paths, worker_id="terminal-scan") == 1
    return request_id


def _copy_terminal_as(paths, source_id: str, new_id: str) -> None:
    """把终态文件复制成新 id，模拟"终态已写、投影与标记缺失"的崩溃残留。

    复制时必须同步改写请求身份字段（id、会话车道引用）并重算指纹，否则重放会按
    "与不可变请求内容冲突"拒绝——那是数据损坏路径，不是本测试要构造的场景。
    """
    payload = json.loads(json.dumps(read_json_file(paths.terminal / f"{source_id}.json")))
    payload["id"] = new_id
    payload["terminal_response"]["id"] = new_id
    claim = payload.get("conversation_claim")
    if isinstance(claim, dict):
        claim["request_id"] = new_id
        claim["task_id"] = f"gateway:{new_id}"
    payload["request_fingerprint"] = gateway_request_fingerprint(payload, new_id)
    write_json_file(paths.terminal / f"{new_id}.json", payload)


def _seed_terminal_archive(agent, paths, copies: int) -> str:
    """真实终态 1 个 + 复制 copies 个待补副本，返回真实请求 id。"""
    request_id = _submit_real_terminal(agent, paths)
    for index in range(copies):
        _copy_terminal_as(paths, request_id, f"gwreq-probe-{index:03d}")
    return request_id


def _warm_terminal_locks(paths) -> None:
    """先读一遍全部终态文件，让 <name>.json.lock sidecar 先落盘。

    锁文件是终态目录里的新条目；先创建它们，之后 repair 轮次之间的目录时间戳才稳定，
    测试才能证明"继续推进"来自游标的追平状态，而不是锁文件带来的目录变化。
    """
    for path in sorted(paths.terminal.glob("*.json")):
        read_json_file_report(path, context="tests.terminal_projection_scan.warm")


def _directory_stamp(directory: Path) -> tuple[int, int]:
    stat = directory.stat()
    return (int(stat.st_mtime_ns), int(stat.st_size))


def _marker_path(paths, request_id: str) -> Path:
    return paths.root / "projection_state" / "terminal_complete" / f"{request_id}.json"


def _projection_path(paths, request_id: str) -> Path:
    payload = read_json_file(paths.terminal / f"{request_id}.json")
    ok = bool(payload.get("terminal_response", {}).get("ok"))
    return (paths.done if ok else paths.failed) / f"{request_id}.json"


def _terminal_file_stamp(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}


def _counting_terminal_glob(monkeypatch, paths) -> list[str]:
    """给 pathlib.Path.glob 打桩：只记录针对终态目录的调用，其余原样转发。"""
    calls: list[str] = []
    original = Path.glob

    def counting(self, pattern, *args, **kwargs):
        if str(self) == str(paths.terminal):
            calls.append(pattern)
        return original(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", counting)
    return calls


def test_steady_state_skips_without_terminal_glob(monkeypatch, tmp_path):
    agent = _make_recovery_agent(tmp_path)
    paths = gateway_paths(agent)
    _ensure_gateway_dirs(paths)
    request_id = _seed_terminal_archive(agent, paths, copies=0)

    # 启动全扫建立游标；单个文件一轮即追平。
    projected, errors = repair_gateway_terminal_projections(
        paths, agent=agent, limit=16, full_scan=True
    )
    assert (projected, errors) == (0, [])
    calls = _counting_terminal_glob(monkeypatch, paths)

    for _ in range(2):
        projected, errors = repair_gateway_terminal_projections(paths, agent=agent, limit=16)
        assert (projected, errors) == (0, [])

    assert calls == [], "稳态（目录时间戳没变且已追平）不允许再 glob 终态目录"
    assert _marker_path(paths, request_id).exists()


def test_new_terminal_file_is_projected_after_directory_change(tmp_path):
    agent = _make_recovery_agent(tmp_path)
    paths = gateway_paths(agent)
    _ensure_gateway_dirs(paths)
    request_id = _seed_terminal_archive(agent, paths, copies=0)
    repair_gateway_terminal_projections(paths, agent=agent, limit=16, full_scan=True)

    # 新到一个终态文件（无投影、无标记）→ 目录变化必须放开节流并补齐。
    _copy_terminal_as(paths, request_id, "gwreq-probe-000")

    projected, errors = repair_gateway_terminal_projections(paths, agent=agent, limit=16)
    assert (projected, errors) == (1, [])
    assert _marker_path(paths, "gwreq-probe-000").exists()
    assert _projection_path(paths, "gwreq-probe-000").exists()


# LLM: 真实终态流程跨页追赶时只枚举一次并复用名称元组；目录新增后须立即重枚举且保留原追平合同。
# 函数用途: 验证分页补投影、缓存复用、二分游标定位及新增终态失效，不放宽原轮数和修复数量断言。
def test_backlog_drains_across_rounds_with_stable_directory_stamp(tmp_path, monkeypatch):
    from bisect import bisect_right

    from agent_py_agent.agent.gateway_parts import recovery

    agent = _make_recovery_agent(tmp_path)
    paths = gateway_paths(agent)
    _ensure_gateway_dirs(paths)
    request_id = _seed_terminal_archive(agent, paths, copies=5)
    _warm_terminal_locks(paths)
    stamp = _directory_stamp(paths.terminal)
    calls = _counting_terminal_glob(monkeypatch, paths)
    seeks = []

    # LLM: 名称来自产品真实候选，原bisect算法负责定位；不替换排序结果或游标。
    # 函数用途: 记录跨页是否二分定位及是否复用同一名称元组。
    def seek(names, previous):
        seeks.append((names, previous))
        return bisect_right(names, previous)

    monkeypatch.setattr(recovery, "bisect_right", seek, raising=False)

    rounds: list[tuple[int, int, bool]] = []
    for _ in range(3):
        projected, errors = repair_gateway_terminal_projections(paths, agent=agent, limit=2)
        rounds.append((projected, len(errors), _directory_stamp(paths.terminal) == stamp))

    assert rounds == [(1, 0, True), (2, 0, True), (2, 0, True)], (
        "未追平时必须跨轮继续推进（每轮 limit=2，首轮真实请求占位跳过）；"
        "撤修复：第二轮起按目录时间戳跳过，5 个待补只补 1 个"
    )
    assert all(_marker_path(paths, f"gwreq-probe-{index:03d}").exists() for index in range(5))
    cursor = read_json_file(paths.root / _CURSOR_RELATIVE)
    assert cursor["caught_up"] is True
    assert calls == ["*.json"], "目录代次不变的多页追赶只能枚举一次"
    assert [previous for _names, previous in seeks] == ["gwreq-probe-000.json", "gwreq-probe-002.json"]
    assert seeks[0][0] is seeks[1][0]

    # 追平后恢复稳态跳过。
    projected, errors = repair_gateway_terminal_projections(paths, agent=agent, limit=2)
    assert (projected, errors) == (0, [])
    assert calls == ["*.json"]

    # 只预热新文件的真实读锁，不用glob辅助函数污染产品枚举计数。
    new_id = "gwreq-probe-005"
    _copy_terminal_as(paths, request_id, new_id)
    read_json_file_report(paths.terminal / f"{new_id}.json", context="tests.terminal_projection_scan.warm")
    assert _directory_stamp(paths.terminal) != stamp
    projected, errors = repair_gateway_terminal_projections(paths, agent=agent, limit=2)
    assert (projected, errors) == (0, [])
    assert calls == ["*.json", "*.json"], "目录变化后的首轮就必须重新枚举"
    for _ in range(3):
        _projected, errors = repair_gateway_terminal_projections(paths, agent=agent, limit=2)
        assert errors == []
    assert _marker_path(paths, new_id).exists()
    assert _projection_path(paths, new_id).exists()
    assert read_json_file(paths.root / _CURSOR_RELATIVE)["caught_up"] is True
    assert calls == ["*.json", "*.json"]


def test_round_limit_caps_files_examined_per_call(tmp_path):
    agent = _make_recovery_agent(tmp_path)
    paths = gateway_paths(agent)
    _ensure_gateway_dirs(paths)
    _seed_terminal_archive(agent, paths, copies=5)
    _warm_terminal_locks(paths)

    per_round: list[tuple[int, int]] = []
    for _ in range(3):
        projected, errors = repair_gateway_terminal_projections(paths, agent=agent, limit=3)
        per_round.append((projected, len(errors)))

    assert per_round == [(2, 0), (3, 0), (0, 0)], (
        "limit=3 时 5 个待补分两轮完成（首轮真实请求占位），任何一轮都不允许超过上限；"
        "撤修复：单轮把全部积压一次做完"
    )


def test_missing_projection_is_repaired_on_startup_full_scan(tmp_path):
    agent = _make_recovery_agent(tmp_path)
    paths = gateway_paths(agent)
    _ensure_gateway_dirs(paths)
    request_id = _submit_real_terminal(agent, paths)
    projection = _projection_path(paths, request_id)
    marker = _marker_path(paths, request_id)
    assert projection.exists() and marker.exists()

    # 崩溃残留：终态已写，投影和标记缺失。
    projection.unlink()
    marker.unlink()

    projected, errors = repair_gateway_terminal_projections(
        paths, agent=agent, limit=16, full_scan=True
    )
    assert (projected, errors) == (1, [])
    assert projection.exists()
    assert marker.exists()


def test_missing_projection_is_repaired_by_low_frequency_rescan(tmp_path):
    agent = _make_recovery_agent(tmp_path)
    paths = gateway_paths(agent)
    _ensure_gateway_dirs(paths)
    request_id = _submit_real_terminal(agent, paths)
    repair_gateway_terminal_projections(paths, agent=agent, limit=16, full_scan=True)

    projection = _projection_path(paths, request_id)
    marker = _marker_path(paths, request_id)
    projection.unlink()
    marker.unlink()

    # 目录时间戳没变、已追平：稳态调用跳过不复查（提速的设计行为），破坏留给低频复核。
    projected, errors = repair_gateway_terminal_projections(paths, agent=agent, limit=16)
    assert (projected, errors) == (0, [])
    assert not projection.exists()

    # 推进游标里的全量复核时间（模拟 1 小时窗口到期）→ 必须补齐。
    cursor_path = paths.root / _CURSOR_RELATIVE
    cursor = read_json_file(cursor_path)
    cursor["full_scan_at"] = time.time() - 3700.0
    write_json_file(cursor_path, cursor)

    projected, errors = repair_gateway_terminal_projections(paths, agent=agent, limit=16)
    assert (projected, errors) == (1, [])
    assert projection.exists()
    assert marker.exists()


def test_corrupt_marker_is_repaired_on_full_scan(tmp_path):
    agent = _make_recovery_agent(tmp_path)
    paths = gateway_paths(agent)
    _ensure_gateway_dirs(paths)
    request_id = _submit_real_terminal(agent, paths)
    marker = _marker_path(paths, request_id)
    payload = read_json_file(marker)
    payload["canonical_stamp"] = {"size": 0, "mtime_ns": 0}
    write_json_file(marker, payload)

    projected, errors = repair_gateway_terminal_projections(
        paths, agent=agent, limit=16, full_scan=True
    )
    assert (projected, errors) == (1, [])
    fixed = read_json_file(marker)
    assert fixed["canonical_stamp"] == _terminal_file_stamp(
        paths.terminal / f"{request_id}.json"
    )


def test_missing_response_receipt_is_repaired_on_full_scan(tmp_path):
    agent = _make_recovery_agent(tmp_path)
    paths = gateway_paths(agent)
    _ensure_gateway_dirs(paths)
    request_id = _submit_real_terminal(agent, paths)
    response_path = paths.responses / f"{request_id}.json"
    original = read_json_file(response_path)
    response_path.unlink()

    projected, errors = repair_gateway_terminal_projections(
        paths, agent=agent, limit=16, full_scan=True
    )
    assert (projected, errors) == (1, [])
    assert response_path.exists()
    assert read_json_file(response_path) == original


def test_first_projection_tick_forces_full_scan_then_steady(monkeypatch, tmp_path):
    paths = _paths(tmp_path)
    dispatcher = _stub_dispatcher(paths, [], threading.Event())
    dispatcher._terminal_projection_startup = True
    seen: list[bool] = []

    def fake_repair(*_args, **kwargs):
        seen.append(bool(kwargs.get("full_scan")))
        return (0, [])

    monkeypatch.setattr(
        gateway_loops, "repair_gateway_terminal_projections", fake_repair
    )
    dispatcher._project_terminals_once()
    dispatcher._project_terminals_once()

    assert seen == [True, False], (
        "启动后的第一次投影扫描必须全量（崩溃恢复保证）；之后交给稳态节流。"
        "撤修复：首次也不全量，崩溃残留永远等不到补齐"
    )


def test_legacy_wrapped_cursor_cannot_hide_new_prefix(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts import recovery

    terminal = tmp_path / "terminal"
    terminal.mkdir()
    paths = SimpleNamespace(root=tmp_path, terminal=terminal)
    repaired = set()
    new_id = "gwreq-prefix-009a"
    for index in range(100):
        name = f"gwreq-prefix-{index:03d}"
        write_json_file(terminal / f"{name}.json", {"id": name})

    def marker(_paths, canonical):
        return canonical.stem != new_id or canonical.stem in repaired

    def repair(_paths, _canonical, request_id, **_kwargs):
        repaired.add(request_id)

    monkeypatch.setattr(recovery, "_terminal_projection_marker_matches", marker)
    monkeypatch.setattr(recovery, "_recover_committed_terminal_processing", repair)
    cursor_path = tmp_path / _CURSOR_RELATIVE
    for _ in range(20):
        repair_gateway_terminal_projections(paths, agent=None, limit=16)
        if read_json_file(cursor_path).get("caught_up"):
            break
    cursor = read_json_file(cursor_path)
    assert cursor["caught_up"] is True
    cursor["last_name"] = "gwreq-prefix-011.json"
    write_json_file(cursor_path, cursor)
    write_json_file(terminal / f"{new_id}.json", {"id": new_id})
    for _ in range(20):
        repair_gateway_terminal_projections(paths, agent=None, limit=16)
        if read_json_file(cursor_path).get("caught_up"):
            break
    assert new_id in repaired
    assert read_json_file(cursor_path)["caught_up"] is True


def test_changed_directory_does_not_starve_previous_backlog(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts import recovery

    terminal = tmp_path / "terminal"
    terminal.mkdir()
    paths = SimpleNamespace(root=tmp_path, terminal=terminal)
    repaired = set()
    for index in range(100):
        name = f"gwreq-backlog-{index:03d}"
        write_json_file(terminal / f"{name}.json", {"id": name})

    def repair(_paths, _canonical, request_id, **_kwargs):
        repaired.add(request_id)

    monkeypatch.setattr(recovery, "_terminal_projection_marker_matches", lambda _paths, canonical: canonical.stem in repaired)
    monkeypatch.setattr(recovery, "_recover_committed_terminal_processing", repair)
    for index in range(12):
        name = f"gwreq-added-{index:03d}"
        write_json_file(terminal / f"{name}.json", {"id": name})
        repair_gateway_terminal_projections(paths, agent=None, limit=16)
    assert "gwreq-backlog-099" in repaired


# LLM: 仅准备临时目录与候选JSON；缓存测试另行将marker匹配置为真，不模拟业务执行或授予恢复权限。
# 函数用途: 为有界候选缓存测试提供小型真实文件目录。
def _opt6_scan_paths(root):
    from types import SimpleNamespace

    terminal = root / "terminal"
    terminal.mkdir(parents=True)
    (terminal / "gwreq-000.json").write_text('{"id":"gwreq-000"}', encoding="utf-8")
    return SimpleNamespace(root=root, terminal=terminal)


# LLM: 调用真实分页入口装入33个目录；只替换投影完成判定，缓存和glob都由产品实现管理。
# 函数用途: 验证缓存最多32个目录，最早目录被逐出后再次请求确实重枚举。
def test_opt6_terminal_candidate_cache_is_bounded(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import recovery

    cache = {}
    monkeypatch.setattr(recovery, "_TERMINAL_CANDIDATES_CACHE", cache, raising=False)
    monkeypatch.setattr(recovery, "_terminal_projection_marker_matches", lambda *_args: True)
    paths = [_opt6_scan_paths(tmp_path / str(index)) for index in range(33)]
    calls = _counting_terminal_glob(monkeypatch, paths[0])
    for item in paths:
        assert repair_gateway_terminal_projections(item, agent=None, full_scan=True) == (0, [])
    assert len(cache) == 32
    assert paths[0].terminal not in cache and paths[-1].terminal in cache
    assert repair_gateway_terminal_projections(paths[0], agent=None, full_scan=True) == (0, [])
    assert calls == ["*.json", "*.json"]
    assert len(cache) == 32


# LLM: stamp未知时不能借旧缓存或误命中稳态跳过；只在首次预热使用full_scan，后续走默认入口。
# 函数用途: 验证缺少目录代次时移除旧缓存，追平后新增文件仍逐次重新枚举。
def test_opt6_unknown_stamp_discards_terminal_candidate_cache(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import recovery

    paths = _opt6_scan_paths(tmp_path)
    cache = {}
    monkeypatch.setattr(recovery, "_TERMINAL_CANDIDATES_CACHE", cache, raising=False)
    monkeypatch.setattr(recovery, "_terminal_projection_marker_matches", lambda *_args: True)
    calls = _counting_terminal_glob(monkeypatch, paths)
    assert repair_gateway_terminal_projections(paths, agent=None, full_scan=True) == (0, [])
    assert paths.terminal in cache
    monkeypatch.setattr(recovery, "_terminal_directory_stamp", lambda _directory: None)
    for index in range(2):
        request_id = f"gwreq-new-{index}"
        (paths.terminal / f"{request_id}.json").write_text(json.dumps({"id": request_id}), encoding="utf-8")
        assert repair_gateway_terminal_projections(paths, agent=None) == (0, [])
        assert paths.terminal not in cache
    assert calls == ["*.json"] * 3


# LLM: 空目录候选也是可复用的不可变快照，但持久游标不得保留已消失的last_name。
# 函数用途: 验证空目录清空旧游标，并在代次稳定时复用空候选。
def test_opt6_empty_terminal_candidates_reset_cursor_and_are_cached(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import recovery

    paths = _opt6_scan_paths(tmp_path)
    (paths.terminal / "gwreq-000.json").unlink()
    monkeypatch.setattr(recovery, "_TERMINAL_CANDIDATES_CACHE", {}, raising=False)
    write_json_file(paths.root / _CURSOR_RELATIVE, {"last_name": "gwreq-old.json", "caught_up": False})
    calls = _counting_terminal_glob(monkeypatch, paths)
    assert repair_gateway_terminal_projections(paths, agent=None, full_scan=True) == (0, [])
    cursor = read_json_file(paths.root / _CURSOR_RELATIVE)
    assert cursor["last_name"] == "" and cursor["caught_up"] is True
    assert repair_gateway_terminal_projections(paths, agent=None, full_scan=True) == (0, [])
    assert calls == ["*.json"]
