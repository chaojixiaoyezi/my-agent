"""Three-run self-stability, committed baseline equality and independent invariant checks."""
from __future__ import annotations

import difflib
import json
import os
import subprocess
import sys
import time
from fnmatch import fnmatchcase
from pathlib import Path
from types import SimpleNamespace

import pytest

from .normalize import assert_invariants, normalize
from .recorder import Recorder
from .replayer import Replayer, ReplayGatewayProcess, _child_worker_exited, replay

HERE = Path(__file__).parent
SCENARIOS = [json.loads(path.read_text()) for path in sorted((HERE / "scenarios").glob("*.json"))]


def _atomic_temporary(name):
    """只排除原子发表的暂存名，正式账本、锁和其它隐藏文件仍是证据。"""
    return any(fnmatchcase(name, pattern) for pattern in (".persona-ledger-*", "*.tmp", "*.tmp.*", ".tmp-*"))


def replay_paths(root, pattern):
    """在进入暂存目录前剪枝；只有本根内暂存项自然消失可以忽略。"""
    root = Path(root)

    def onerror(error):
        if isinstance(error, FileNotFoundError) and error.filename is not None:
            path = Path(error.filename)
            if path.is_relative_to(root) and any(_atomic_temporary(part) for part in path.relative_to(root).parts):
                return
        raise error

    for directory, directories, files in os.walk(root, onerror=onerror):
        directories[:] = [name for name in directories if not _atomic_temporary(name)]
        for name in (*directories, *files):
            if not _atomic_temporary(name) and fnmatchcase(name, pattern):
                yield Path(directory) / name


@pytest.fixture(autouse=True)
def atomic_snapshot_paths(tmp_path, monkeypatch):
    """只适配当前 golden 临时根内的观察扫描，不改变子进程或共享回放器。"""
    original = Path.rglob

    def snapshot_rglob(root, pattern, *args, **kwargs):
        if (root.is_relative_to(tmp_path) and isinstance(pattern, str)
                and "/" not in pattern and not args and not kwargs):
            return replay_paths(root, pattern)
        return original(root, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "rglob", snapshot_rglob)


# LLM: Capabilities use the existing root marker; no local try/skip or expected-failure path masks a golden failure.
# 函数用途: Build one collected test per declared scenario with its exact capability requirements.
def scenario_parameters():
    fixture = json.loads((HERE.parent / "slo" / "slo_targets.json").read_text())["fixture"]
    rows = []
    for scenario in SCENARIOS:
        capabilities = list(scenario["required_capabilities"])
        # Linux 后台命令还需实际创建 bwrap 命名空间，能力不足沿根 conftest 的统一门处理。
        if (sys.platform.startswith("linux") and "background_launcher_identity" in capabilities
                and "linux_bwrap" not in capabilities):
            capabilities.append("linux_bwrap")
        marks = [pytest.mark.sandbox_capability(*capabilities)] if capabilities else []
        # 用例预算只有场景声明一份权威；pytest 总门另留既有清理余量，覆盖 CLI 的默认 60 秒。
        marks.append(pytest.mark.timeout(scenario["deadline_seconds"] * scenario["repetitions"] + fixture["teardown_seconds"]))
        rows.append(pytest.param(scenario, id=scenario["id"], marks=marks))
    return rows


# LLM: 更新只写稳定回放；旧基线也先核原哈希再应用同一幂等归一规则，不重录未解释的产品差异。
# 函数用途: Replay the same Gateway scenario three times and compare it with the committed oracle.
@pytest.mark.parametrize("scenario", scenario_parameters())
def test_golden_replay(tmp_path, golden_parameters, scenario, record_property):
    started = time.monotonic()
    traces = []
    for index in range(scenario["repetitions"]):
        replay_started = time.monotonic()
        trace = replay(tmp_path / f"run-{index}", scenario, golden_parameters)
        assert time.monotonic() - replay_started < scenario["deadline_seconds"], "single replay exceeded its CI deadline"
        assert_invariants(trace, scenario["expected_effects"], scenario["forbidden_effects"])
        traces.append(trace)
    assert len(traces) == 3, "the golden contract requires exactly three consecutive runs"
    for trace in traces[1:]:
        assert trace == traces[0], _difference(traces[0], trace)
    expected = HERE / "expected" / f"{scenario['id']}.json"
    if os.environ.get("MY_AGENT_GOLDEN_UPDATE") == "1":
        expected.parent.mkdir(parents=True, exist_ok=True)
        expected.write_text(json.dumps(traces[0], ensure_ascii=False, indent=2) + "\n")
    assert expected.is_file(), f"missing committed baseline: {expected.name}; explicit MY_AGENT_GOLDEN_UPDATE=1 is required"
    baseline = normalize(json.loads(expected.read_text()))
    assert_invariants(baseline, scenario["expected_effects"], scenario["forbidden_effects"])
    assert traces[0] == baseline, _difference(baseline, traces[0])
    elapsed = time.monotonic() - started
    record_property("golden_runtime_seconds", elapsed)
    assert elapsed < scenario["deadline_seconds"] * scenario["repetitions"], f"scenario exceeded its CI deadline: {elapsed}"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario["id"])
def test_observation_budget_preserves_ci_headroom_and_polling(tmp_path, golden_parameters, scenario):
    # 实际 Replayer 已扩大观察预算；固定这条合同，避免新调用入口退回共享 SLO 的短预算。
    player = Replayer(tmp_path, scenario, golden_parameters)
    try:
        assert player.bounds["condition_seconds"] >= 20.0
        assert player.bounds["poll_seconds"] == golden_parameters["poll_seconds"]
        assert all(entry["deadline_seconds"] > player.bounds["condition_seconds"]
                   for entry in player.provider.script if entry.get("wait_for_release"))
    finally:
        player.provider.close()


# LLM: Diffs compare actual structured traces; no values are stripped to make a mismatch pass.
# 函数用途: Render an inspectable unified diff for a baseline or stability failure.
def _difference(expected, actual):
    before = json.dumps(expected, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    after = json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    return "\n".join(difflib.unified_diff(before, after, fromfile="expected", tofile="actual"))


@pytest.mark.parametrize("staging_name", [".persona-ledger-deadbeef", "write.tmp", "write.tmp.123", ".tmp-123"])
def test_recorder_ignores_atomic_staging_directories(tmp_path, staging_name):
    home = tmp_path / "home"
    message = {"message_id": "one", "role": "assistant", "content": "committed", "created_at": 1}
    for directory in (home / "messages", home / staging_name / "messages"):
        directory.mkdir(parents=True)
        (directory / "thread.jsonl").write_text(json.dumps(message) + "\n")
    recorder = Recorder(tmp_path / "evidence")
    paths = SimpleNamespace(**{name: home / name for name in ("processing", "done", "failed")})
    recorder.session_and_tools(home, paths, {"thread_id": "thread", "request_id": "request"})
    assert [event["content"] for event in recorder.events] == ["committed"]


def test_replay_scan_does_not_enter_disappearing_atomic_directory(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = home / "gateway"
    root.mkdir(parents=True)
    (root / "gateway_state.json").write_text("{}")
    staging = home / ".persona-ledger-deadbeef"
    staging.mkdir()
    entered = []
    scandir = os.scandir

    def disappear(path):
        if Path(path) == staging:
            entered.append(path)
            staging.rmdir()
        return scandir(path)

    monkeypatch.setattr(os, "scandir", disappear)
    paths = ReplayGatewayProcess.paths(SimpleNamespace(home=home))
    assert paths.root == root
    assert entered == [], "golden scans must prune atomic directories before descent"


def test_replay_paths_preserves_committed_files_and_ignores_temporary_files(tmp_path):
    committed = {"persona_ledger.json", ".hidden.json", "write.tmpish", "versions.jsonl.lock"}
    temporary = {".persona-ledger-123", "write.tmp", "write.tmp.123", ".tmp-123"}
    for name in committed | temporary:
        (tmp_path / name).write_text(name)
    paths = list(replay_paths(tmp_path, "*"))
    assert {path.name: path.read_text() for path in paths} == {name: name for name in committed}


@pytest.mark.parametrize("name,error_type,ignored", [
    (".persona-ledger-123", FileNotFoundError, True),
    ("write.tmp", FileNotFoundError, True),
    ("write.tmp.123", FileNotFoundError, True),
    (".tmp-123", FileNotFoundError, True),
    ("messages", FileNotFoundError, False),
    (".persona-ledger-123", PermissionError, False),
])
def test_replay_scan_only_ignores_missing_atomic_paths(tmp_path, monkeypatch, name, error_type, ignored):
    committed = tmp_path / "gateway_state.json"
    committed.write_text("{}")
    walk = os.walk
    error = error_type(2, "injected scan failure", str(tmp_path / name))

    def scan(root, *, onerror):
        onerror(error)
        yield from walk(root, onerror=onerror)

    monkeypatch.setattr(os, "walk", scan)
    if ignored:
        assert list(replay_paths(tmp_path, "gateway_state.json")) == [committed]
    else:
        with pytest.raises(error_type) as raised:
            list(replay_paths(tmp_path, "gateway_state.json"))
        assert raised.value is error


def test_recorder_does_not_hide_missing_committed_evidence(tmp_path, monkeypatch):
    home = tmp_path / "home"
    directory = home / "messages"
    directory.mkdir(parents=True)
    committed = directory / "thread.jsonl"
    committed.write_text("{}")
    read_text = Path.read_text
    missing = []

    def disappear(path, *args, **kwargs):
        if path == committed:
            path.unlink()
            missing.append(path)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", disappear)
    recorder = Recorder(tmp_path / "evidence")
    paths = SimpleNamespace(**{name: home / name for name in ("processing", "done", "failed")})
    with pytest.raises(FileNotFoundError):
        recorder.session_and_tools(home, paths, {"thread_id": "thread", "request_id": "request"})
    assert missing == [committed]


def test_replay_scan_still_requires_one_authoritative_gateway_state(tmp_path):
    for name in ("first", "second"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "gateway_state.json").write_text("{}")
    with pytest.raises(AssertionError):
        ReplayGatewayProcess.paths(SimpleNamespace(home=tmp_path))


def test_snapshot_filter_keeps_other_roots_on_the_original_glob(tmp_path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside-golden")
    temporary = outside / "write.tmp"
    temporary.write_text("outside this replay")
    assert list(outside.rglob("*.tmp")) == [temporary]
    (tmp_path / "write.tmp").write_text("atomic staging")
    assert list(tmp_path.rglob("*.tmp")) == []


@pytest.mark.sandbox_capability("ps", "background_launcher_identity")
def test_child_terminal_state_waits_for_exact_owned_worker_exit():
    from agent_py_agent.agent.tooling.process_registry import capture_process_birth_token

    process = subprocess.Popen([sys.executable, '-c', 'import sys;sys.stdin.buffer.read()'],
                               stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        child = {'id': 'child', 'status': 'DONE', 'runner_active_attempt_id': '', 'attributes': {
            'runner_session': {'schema_version': 'runner_session_pool.v1', 'run_id': 'child',
                'attempt_id': 'attempt', 'launch_id': 'launch', 'worker_pid': process.pid,
                'worker_birth_token': capture_process_birth_token(process.pid),
                'status': 'completed', 'in_process': False}}}
        assert child['attributes']['runner_session']['worker_birth_token']
        assert not _child_worker_exited(child), '终态时真实worker仍活着，不能提前推进下一ask'
        process.stdin.close()
        assert process.wait(timeout=5) == 0
        assert _child_worker_exited(child)
        session = child['attributes']['runner_session']
        for key, invalid in (('run_id', 'other'), ('attempt_id', ''), ('launch_id', ''),
                             ('worker_pid', True), ('worker_birth_token', ''),
                             ('status', 'running'), ('in_process', True)):
            original = session[key]
            session[key] = invalid
            assert not _child_worker_exited(child), key
            session[key] = original
        child['runner_active_attempt_id'] = 'new-attempt'
        assert not _child_worker_exited(child)
        del child['runner_active_attempt_id']
        assert not _child_worker_exited(child)
    finally:
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        process.wait(timeout=5)
