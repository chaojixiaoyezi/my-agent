# LLM: S2 持久写瘦身回归：遥测元数据/运行事实进度非耐久、只脱敏新轮次行、每工具轮一次事实写、同字节跳过；
#   同时守住默认耐久、开始/收尾事实耐久、坏进度文件按缺失处理等安全性质。只写 pytest 临时目录。
# 模块用途: 以 fsync/写入计数验证 S2 的耐久写次数与保留的崩溃/脱敏语义。
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime import live_archive as runtime_live_archive
from agent_py_agent.agent.agent_core.runtime.live_archive import (
    archive_assistant_tool_round_if_enabled,
    update_runtime_fact_progress_if_enabled,
    update_runtime_fact_terminal_if_enabled,
    write_runtime_fact_start_if_enabled,
)
from agent_py_agent.agent.common import json_io
from agent_py_agent.agent.common.nofollow_fs import write_text_atomic_beneath
from agent_py_agent.agent.memory_archive import runtime_fact_source as fact_module
from agent_py_agent.agent.memory_archive.runtime_fact_source import (
    RuntimeFactSourceRequest,
    write_runtime_fact_source,
)
from agent_py_agent.agent.observability import task_call_inspector as inspector_module
from agent_py_agent.agent.observability import task_rounds as rounds_module
from agent_py_agent.agent.observability.task_call_inspector import TaskCallInspector
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.context_bundle import (
    MainContextBundleRequest,
    build_main_context_bundle,
)
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.run_workspace import (
    EnsureRunWorkspaceRequest,
    activate_run_workspace,
)

_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX fsync/权限语义")


# LLM: 只计数，不改变真实 fsync 行为；计数器由调用方在窗口前清零。
# 函数用途: 统计被测窗口内的 os.fsync 次数。
@pytest.fixture
def fsyncs(monkeypatch):
    counter = {"n": 0}
    real = os.fsync

    def counting(fd):
        counter["n"] += 1
        return real(fd)

    monkeypatch.setattr(os, "fsync", counting)
    return counter


# 函数用途: 默认私有原子写仍刷文件与父目录，显式 durable=False 才省去两次 fsync，内容与权限一致。
@_POSIX_ONLY
def test_private_writer_durable_default_and_explicit_opt_out(tmp_path, fsyncs):
    target = tmp_path / "state.json"
    json_io.write_private_text_file_atomic(target, "one")
    assert fsyncs["n"] == 2
    fsyncs["n"] = 0
    json_io.write_private_text_file_atomic(target, "two", durable=False)
    assert fsyncs["n"] == 0
    assert target.read_text() == "two" and stat.S_IMODE(target.stat().st_mode) == 0o600
    fsyncs["n"] = 0
    json_io.write_private_json_file_atomic(target, {"a": 1})
    assert fsyncs["n"] == 2


# 函数用途: no-follow 原子写默认仍耐久；durable=False 只省 fsync，不改 no-follow 原子替换结果。
@_POSIX_ONLY
def test_nofollow_writer_durable_default_and_explicit_opt_out(tmp_path, fsyncs):
    write_text_atomic_beneath(tmp_path, ("d", "a.json"), "one")
    assert fsyncs["n"] == 2
    fsyncs["n"] = 0
    write_text_atomic_beneath(tmp_path, ("d", "a.json"), "two", durable=False)
    assert fsyncs["n"] == 0
    assert (tmp_path / "d" / "a.json").read_text() == "two"


def _round(index, **extra):
    return dict(round_id=f"r{index}", run_id="run-1", prompt_tokens=10 + index, completion_tokens=2,
                cached_tokens=1, duration_ms=5.0, **extra)


# 函数用途: 模型遥测每轮最多一次 fsync（逐行追加源），含新任务首轮；基线首轮 9 次、后续每轮 3 次。
@_POSIX_ONLY
def test_task_round_append_costs_at_most_one_fsync_per_round(tmp_path, fsyncs):
    inspector = TaskCallInspector(tmp_path, owner_id="local/main")
    per_round = []
    for index in range(4):
        fsyncs["n"] = 0
        inspector.append_round("task-s2", _round(index), create_if_missing=True, include_rounds=False)
        per_round.append(fsyncs["n"])
    assert per_round == [1, 1, 1, 1], per_round
    detail = inspector.get_task_call_detail("task-s2")
    assert [row["round_id"] for row in detail["rounds"]] == ["r0", "r1", "r2", "r3"]
    assert detail["rounds_coverage"]["status"] == "complete"
    assert detail["token_usage"]["prompt_tokens"] == sum(10 + i for i in range(4))


# 函数用途: 稳态追加只脱敏新轮次行一次，已存元数据不再整份脱敏；新行与公开详情中的凭据仍被遮蔽。
def test_task_round_append_redacts_only_the_new_row(tmp_path, monkeypatch):
    secret = "sk-s2-secret-value-0123456789"
    inspector = TaskCallInspector(tmp_path, owner_id="local/main")
    inspector.append_round("task-red", _round(0), create_if_missing=True, include_rounds=False)
    seen_rows, metadata_passes = [], {"n": 0}
    real_row_redact, real_metadata_redact = rounds_module.redact, inspector_module.redact_sensitive_value

    def row_redact(value):
        seen_rows.append(value)
        return real_row_redact(value)

    def metadata_redact(value):
        metadata_passes["n"] += 1
        return real_metadata_redact(value)

    monkeypatch.setattr(rounds_module, "redact", row_redact)
    monkeypatch.setattr(inspector_module, "redact_sensitive_value", metadata_redact)
    inspector.append_round("task-red", _round(1, note="Authorization: Bearer " + secret),
                           include_rounds=False)
    assert len(seen_rows) == 1 and seen_rows[0]["round_id"] == "r1"
    assert metadata_passes["n"] == 0
    monkeypatch.setattr(inspector_module, "redact_sensitive_value", real_metadata_redact)
    rounds_source = tmp_path.joinpath(*rounds_module._parts("task-red")).read_text()
    assert secret not in rounds_source
    assert secret not in json.dumps(inspector.get_task_call_detail("task-red"), ensure_ascii=False)


# 函数用途: 新任务首轮的中断窗口（轮次源已建、元数据未写）仍按确定性代次复用，后续追加不冲突。
def test_task_round_creation_interrupted_before_metadata_recovers(tmp_path, monkeypatch):
    inspector = TaskCallInspector(tmp_path, owner_id="local/main")
    real_write = rounds_module._write_metadata

    def fail_first(home, row):
        raise OSError("interrupted before metadata")

    monkeypatch.setattr(rounds_module, "_write_metadata", fail_first)
    with pytest.raises(OSError):
        inspector.append_round("task-crash", _round(0), create_if_missing=True, include_rounds=False)
    monkeypatch.setattr(rounds_module, "_write_metadata", real_write)
    inspector.append_round("task-crash", _round(0), create_if_missing=True, include_rounds=False)
    inspector.append_round("task-crash", _round(1), include_rounds=False)
    detail = inspector.get_task_call_detail("task-crash")
    assert [row["round_id"] for row in detail["rounds"]] == ["r0", "r1"]


def _fact_request(root, **changes):
    values = dict(root=root, request_id="req-s2", run_id="run-s2", task_id="task-s2", user_prompt="整理报告",
                  status="running", phase="tool_loop", source="live_tool_loop", tool_rounds=1)
    values.update(changes)
    return RuntimeFactSourceRequest(**values)


# 函数用途: 开始/收尾事实保持耐久；进度写不刷盘，且同字节进度不重写 task.json 与 locator。
@_POSIX_ONLY
def test_runtime_fact_progress_non_durable_and_skips_identical(tmp_path, fsyncs, monkeypatch):
    monkeypatch.setattr(fact_module, "_utc_timestamp", lambda: "2026-11-16T00:00:00Z")
    write_runtime_fact_source(_fact_request(tmp_path, phase="started", source="run"))
    assert fsyncs["n"] == 4
    fsyncs["n"] = 0
    write_runtime_fact_source(_fact_request(tmp_path, durable=False, executed_tools=["read_file"]))
    assert fsyncs["n"] == 0
    path = tmp_path / "memory_archive" / "runtime_facts" / "req-s2" / "task.json"
    before = path.stat()
    locator = tmp_path / "memory_archive" / ".runtime_facts.latest-run-intent.json"
    locator_before = locator.stat()
    write_runtime_fact_source(_fact_request(tmp_path, durable=False, executed_tools=["read_file"]))
    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert locator.stat().st_ino == locator_before.st_ino
    fsyncs["n"] = 0
    # 收尾即使与进度同字节也必须重新耐久落盘。
    write_runtime_fact_source(_fact_request(tmp_path, executed_tools=["read_file"]))
    assert fsyncs["n"] == 4


# 函数用途: 非耐久进度文件断电后可能不可解析；下一次进度写与读者都按缺失处理，不抛异常。
def test_unparseable_progress_fact_is_treated_as_absent(tmp_path):
    from agent_py_agent.agent.run_intent import latest_run_intent_report

    write_runtime_fact_source(_fact_request(tmp_path, phase="started", source="run"))
    path = tmp_path / "memory_archive" / "runtime_facts" / "req-s2" / "task.json"
    path.write_text('{"version": 1, "goal": "trunc', encoding="utf-8")
    intent, errors = latest_run_intent_report(tmp_path)
    assert intent == {} and errors and errors[0]["path"].endswith("task.json")
    write_runtime_fact_source(_fact_request(tmp_path, durable=False, incremental=True, executed_tools=["grep"]))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["goal"] == "整理报告" and payload["runtime_progress"]["executed_tools"] == ["grep"]


def _live_agent_and_params(tmp_path):
    agent = SimpleNamespace(root=tmp_path, config=AgentConfig(), session_id="session-s2")
    params = SimpleNamespace(request_id="req-live", run_id="run-live", task_id="task-live", user_prompt="继续任务",
                             executed_tools=[], tool_context=[], archive_tool_calls=[], live_archive_state={},
                             save=True)
    return agent, params


def _record_tool(params, index):
    params.archive_tool_calls.append({"tool": "read_file", "index": index, "raw_archive_path": f"/a/{index}"})
    params.executed_tools.append(f"read_file_{index}")


def _count_fact_writes(monkeypatch):
    calls = []
    real = runtime_live_archive.write_runtime_fact_source

    def counting(request):
        calls.append(request)
        return real(request)

    monkeypatch.setattr(runtime_live_archive, "write_runtime_fact_source", counting)
    return calls


# 函数用途: 一个模型工具轮（3 个工具 + 轮末）只写一次运行事实进度，且这次写包含全部 3 个工具；基线写 4 次。
def test_one_runtime_fact_progress_write_per_tool_round(tmp_path, monkeypatch):
    agent, params = _live_agent_and_params(tmp_path)
    writes = _count_fact_writes(monkeypatch)
    for tool_round in (1, 2):
        archive_assistant_tool_round_if_enabled(agent, params, tool_round=tool_round, response_text="", tool_calls=[])
        for index in range(3):
            _record_tool(params, tool_round * 10 + index)
            update_runtime_fact_progress_if_enabled(agent, params, tool_round=tool_round)
        update_runtime_fact_progress_if_enabled(agent, params, tool_round=tool_round)
    assert len(writes) == 2 and all(request.durable is False for request in writes)
    fact = json.loads((tmp_path / "memory_archive" / "runtime_facts" / "req-live" / "task.json").read_text())
    assert fact["runtime_progress"]["executed_tools"][-6:] == [
        "read_file_10", "read_file_11", "read_file_12", "read_file_20", "read_file_21", "read_file_22"]
    assert fact["runtime_progress"]["tool_rounds"] == 2


# 函数用途: 宿主工具路径没有轮首标记，逐次进度写保持原样（不会因合并丢失唯一一次事实写）。
def test_host_tool_progress_without_round_marker_still_writes(tmp_path, monkeypatch):
    agent, params = _live_agent_and_params(tmp_path)
    writes = _count_fact_writes(monkeypatch)
    _record_tool(params, 0)
    update_runtime_fact_progress_if_enabled(agent, params, tool_round=1)
    assert len(writes) == 1
    assert (tmp_path / "memory_archive" / "runtime_facts" / "req-live" / "task.json").exists()


# 函数用途: 工具轮被异常打断（无轮末调用）时，终态写前先补写已合并的进度，终态仍保留已执行工具。
def test_interrupted_round_flushes_pending_progress_before_terminal(tmp_path):
    agent, params = _live_agent_and_params(tmp_path)
    write_runtime_fact_start_if_enabled(agent, params)
    archive_assistant_tool_round_if_enabled(agent, params, tool_round=1, response_text="", tool_calls=[])
    for index in range(2):
        _record_tool(params, index)
        update_runtime_fact_progress_if_enabled(agent, params, tool_round=1)
    update_runtime_fact_terminal_if_enabled(agent, params, RuntimeError("tool round aborted"))
    fact = json.loads((tmp_path / "memory_archive" / "runtime_facts" / "req-live" / "task.json").read_text())
    assert fact["run_status"]["status"] == "failed"
    assert fact["runtime_progress"]["executed_tools"][-2:] == ["read_file_0", "read_file_1"]


# 函数用途: 同字节、0600 的私有文件不重写不刷盘；内容变化或权限偏宽时照常耐久重写并收紧。
@_POSIX_ONLY
def test_private_write_if_changed_skips_only_identical_private_files(tmp_path, fsyncs):
    target = tmp_path / "task.yaml"
    assert json_io.write_private_text_file_if_changed(target, "a: 1\n") is True
    inode = target.stat().st_ino
    fsyncs["n"] = 0
    assert json_io.write_private_text_file_if_changed(target, "a: 1\n") is False
    assert fsyncs["n"] == 0 and target.stat().st_ino == inode
    assert json_io.write_private_text_file_if_changed(target, "a: 2\n") is True
    os.chmod(target, 0o644)
    assert json_io.write_private_text_file_if_changed(target, "a: 2\n") is True
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


# 函数用途: 同一执行重复激活工作区时 task.yaml 字节相同不再替换（基线每次替换为新 inode）。
@_POSIX_ONLY
def test_same_activation_does_not_rewrite_task_yaml(tmp_path):
    request = EnsureRunWorkspaceRequest(home=tmp_path / "home", template="runs", task_name="报告",
                                        user_prompt="整理报告", request_id="req-ws", run_id="run-ws",
                                        task_id="task-ws", owner_id="local/main", owner_home=str(tmp_path))
    paths = activate_run_workspace(tmp_path / "ws", request)
    inode = paths.task_yaml.stat().st_ino
    activate_run_workspace(tmp_path / "ws", request)
    assert paths.task_yaml.stat().st_ino == inode


# 函数用途: 同 stem 以相同输入重建上下文包时四个快照文件都不重写（基线每次各替换一次并刷盘）。
@_POSIX_ONLY
def test_identical_context_bundle_rebuild_skips_all_snapshot_writes(tmp_path, fsyncs):
    root = tmp_path / "workspace"
    root.mkdir()
    request = MainContextBundleRequest(root=root, home_paths=ensure_my_agent_home(tmp_path / "home"),
                                       user_prompt="整理报告", request_id="bundle-s2", run_id="run-b",
                                       task_id="task-b", created_at="2026-11-16T00:00:00+00:00")
    first = build_main_context_bundle(request)
    inode = Path(first.json_path).stat().st_ino
    fsyncs["n"] = 0
    second = build_main_context_bundle(request)
    assert second.json_path == first.json_path and fsyncs["n"] == 0
    assert Path(second.json_path).stat().st_ino == inode
