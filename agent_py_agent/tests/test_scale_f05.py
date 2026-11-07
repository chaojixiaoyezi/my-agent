"""创建容量的历史规模、提交恢复与结构化谱系回归。"""
from __future__ import annotations

import ast
import builtins
import copy
import gc
import io
import json
import os
import threading
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.capacity import available_creation_slots
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.models import SubAgentTask
from agent_py_agent.agent.subagents.services.base import CreateRunParams
from agent_py_agent.agent.subagents.services.persistence.run_index import SubagentRunIndex
from agent_py_agent.agent.subagents.services.persistence.service import (
    SubAgentPersistenceService,
    load_subagent_task,
)


# LLM: 只在隔离目录构造旧版可读任务；新索引存在时用相同任务增量登记，不替换容量实现。
# 函数用途: 准备历史规模夹具，避免把一万次工作单生成时间算入一次容量读取。
def _history(manager, count):
    index = getattr(manager.persistence, "run_index", None)
    with index.connect() if index is not None else nullcontext(None) as connection:
        for number in range(count):
            task = SubAgentTask(id=f"history-{number}", goal="已完成工作", thought="", plan=[], status="DONE", root_id="old-root")
            directory = manager.workspace / task.id
            directory.mkdir()
            (directory / "task.json").write_text(json.dumps({"id": task.id, "goal": task.goal, "thought": "", "plan": [], "status": task.status, "root_id": task.root_id}))
            if index is not None:
                index.upsert(task, connection=connection)


# LLM: 只读语法树检查给定函数及嵌套表达式，不依赖行号或字符串 grep。
# 函数用途: 拦截容量或请求谱系重新直接调用全量运行列表。
def _reject_full_listing(source):
    tree = ast.parse(source)
    return [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute) and node.func.attr == "list_runs"]


# LLM: 语法树门只读函数源码；无效样例应被同一检查器拒绝。
# 函数用途: 验证创建容量和请求谱系入口不直接调用历史全列表。
def test_ast_creation_entries_reject_full_listing():
    import inspect
    import textwrap

    from agent_py_agent.agent.agent_core.orchestration.capacity import _active_subagent_counts

    for function in (_active_subagent_counts, SimpleAgent.subagent_run_ids_for_request):
        assert not _reject_full_listing(textwrap.dedent(inspect.getsource(function)))
    assert _reject_full_listing("def invalid(agent):\n    return agent.subagents.list_runs()\n") == [2]


# LLM: pytest 隔离规模回归；只记录夹具读取量，不启动真实子任务或使用生产数据。
# 函数用途: 检查历史扩大后，容量判断不会重新枚举所有已结束任务。
@pytest.mark.parametrize("history", [1000, 10000])
@pytest.mark.parametrize("active", [0, 1, 8])
def test_creation_capacity_does_not_enumerate_terminal_history(tmp_path, monkeypatch, history, active):
    manager = SubAgentManager(tmp_path / "runs", workspace_root=tmp_path)
    _history(manager, history)
    for _ in range(active):
        manager.create_run(goal="当前工作", root_id="current-root")
    agent = SimpleNamespace(subagents=manager, config=SimpleNamespace(max_subagents=8),
                            owner_policy=None, _current_run_params=SimpleNamespace(task_id="current-root"))
    agent.subagent_run_ids_for_request = lambda key: SimpleAgent.subagent_run_ids_for_request(agent, key)
    calls = []
    original = Path.glob

    # LLM: 透明保留glob可选参数；Python3.14的rglob会携带关键字调用此观察器。
    # 函数用途: 只记录工作区枚举，其他参数原样交给真实glob。
    def observe(path, pattern, *args, **kwargs):
        if path == manager.workspace:
            calls.append(pattern)
        return original(path, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", observe)
    before = len(os.listdir("/dev/fd" if Path("/dev/fd").exists() else "/proc/self/fd"))
    slots, details = available_creation_slots(agent)
    after = len(os.listdir("/dev/fd" if Path("/dev/fd").exists() else "/proc/self/fd"))
    assert slots == 8 - active
    assert details["owner_active"] == active
    assert details["task_active"] == active
    assert calls == []
    assert after - before == 0


# LLM: 使用正式 defer_start 参数延后孩子执行；不绕过创建、命名、covers、持久化或工具返回链，无替身。
# 函数用途: 比较完整创建事务在 1k/10k 终态及 0/1/8 活跃时的实际 I/O、满额拒绝和重启容量，不外推自动执行链。
@pytest.mark.parametrize("active", [0, 1, 8])
def test_real_creation_service_avoids_history_and_survives_restart(tmp_path, monkeypatch, active):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    observations = []
    for history in (1000, 10000):
        root = tmp_path / str(history)
        root.mkdir()
        config = AgentConfig(model_backend="echo", max_subagents=8, my_agent_home=str(root / "home"))
        agent = SimpleAgent(config, root)
        _history(agent.subagents, history)
        for _ in range(active):
            agent.subagents.create_run(goal="当前工作")
        assert available_creation_slots(agent)[0] == 8 - active
        enumerations, opened = [], []
        original_glob, original_os_open = Path.glob, os.open
        original_io_open, original_open = io.open, builtins.open

        # LLM: 不改变真实glob行为或调用计数；兼容rglob传来的case_sensitive等参数。
        # 函数用途: 记录子代理工作区枚举，并透明转发位置与关键字参数。
        def observe_glob(path, pattern, *args, **kwargs):
            if path == agent.subagents.workspace:
                enumerations.append(pattern)
            return original_glob(path, pattern, *args, **kwargs)

        def record(path):
            if isinstance(path, (str, bytes, os.PathLike)):
                leaf = Path(os.fsdecode(path)).name
                if leaf in {"task.json", "canonical_state.json"}:
                    opened.append(leaf)

        def observe_os_open(path, flags, *args, **kwargs):
            if flags & os.O_ACCMODE != os.O_WRONLY:
                record(path)
            return original_os_open(path, flags, *args, **kwargs)

        def observe_io_open(path, mode="r", *args, **kwargs):
            if "r" in mode or "+" in mode:
                record(path)
            return original_io_open(path, mode, *args, **kwargs)

        def observe_open(path, mode="r", *args, **kwargs):
            if "r" in mode or "+" in mode:
                record(path)
            return original_open(path, mode, *args, **kwargs)

        with monkeypatch.context() as observed:
            observed.setattr(Path, "glob", observe_glob)
            observed.setattr(os, "open", observe_os_open)
            # 透明包装保留原函数已证明的 dir_fd 能力；否则 no-follow 探针会把“换了函数身份”误判为平台缺能力。
            if original_os_open in os.supports_dir_fd:
                observed.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, observe_os_open})
            observed.setattr(io, "open", observe_io_open)
            observed.setattr(builtins, "open", observe_open)
            # 清理先前夹具的不可达对象只发生在观察开始前；调用结束不靠 GC 回收来满足零泄漏。
            gc.collect()
            before_fd = _fd_count()
            before_fds = _fd_snapshot()
            result = CreateSubagentsTool(agent).execute({"items": [{"goal": "整理一个独立模块"}], "defer_start": True})
            fd_delta = _fd_count() - before_fd
            after_fds = _fd_snapshot()
        assert result.ok is (active < 8), result.output
        if active == 8:
            assert result.error_code == "SUBAGENT_CAPACITY_EXCEEDED", result.output
            assert json.loads(result.output)["available"] == 0
        else:
            payload = json.loads(result.output)
            assert payload["created"] == 1 and payload["auto_start"]["status"] == "deferred"
        if fd_delta:
            print("wire_fd_delta=" + json.dumps({"history": history, "active": active,
                  "before": before_fds, "after": after_fds}, sort_keys=True))
        assert fd_delta == 0, {"history": history, "active": active, "before": before_fds, "after": after_fds}
        before = available_creation_slots(agent)
        restarted = SimpleAgent(config, root)
        assert available_creation_slots(restarted) == before
        observations.append({"history": history, "active": active, "reads": len(opened),
                             "task_reads": opened.count("task.json"),
                             "canonical_reads": opened.count("canonical_state.json"),
                             "glob": enumerations, "fd_delta": fd_delta, "ok": result.ok})
    print("wire_scale_f05=" + json.dumps(observations, sort_keys=True))
    assert all(not item["glob"] for item in observations), observations
    assert all(item["reads"] <= 64 for item in observations), observations
    assert observations[1]["reads"] == observations[0]["reads"], observations


# LLM: 显式回填测试停用后台调度以分开观察入口；不替换索引或 canonical reader。
# 函数用途: 证明 partial 热查询拒绝且不枚举，有限轮次迁移后与规范状态一致。
def test_legacy_index_is_partial_and_never_backfills_on_hot_miss(tmp_path, monkeypatch):
    _disable_maintenance(monkeypatch)
    workspace = tmp_path / "runs"
    (workspace / "legacy").mkdir(parents=True)
    _legacy_task(workspace, "legacy")
    manager = SubAgentManager(workspace, workspace_root=tmp_path)
    index = getattr(manager.persistence, "run_index", None)
    assert index is not None
    assert index.coverage()["complete"] is False
    with monkeypatch.context() as hot:
        hot.setattr(os, "scandir", _unexpected_scan)
        with pytest.raises(RuntimeError, match="partial"):
            index.active_counts()
    report = manager.persistence.backfill_run_index(batch_size=1)
    assert report["loaded"] <= 1
    for _ in range(8):
        if report["complete"]:
            break
        report = manager.persistence.backfill_run_index(batch_size=1)
    assert report["complete"], report
    assert index.active_counts()[0] == 1


# LLM: 失败后的修复必须读取旧 canonical，不得把未发表的 DONE 投影进容量。
# 函数用途: 验证既有 run 的 pending 可以通过精确恢复清理而不再次保存业务状态。
def test_failed_canonical_commit_leaves_capacity_unavailable(tmp_path, monkeypatch):
    from agent_py_agent.agent.subagents.services.persistence import service

    _disable_maintenance(monkeypatch)
    manager = SubAgentManager(tmp_path / "runs", workspace_root=tmp_path)
    task = manager.create_run(goal="规范状态提交")
    original = service.write_agent_run_state

    def unavailable(state):
        raise OSError("simulated I/O error")

    monkeypatch.setattr(service, "write_agent_run_state", unavailable)
    task.status = "DONE"
    with pytest.raises(OSError):
        manager.save(task)
    index = getattr(manager.persistence, "run_index", None)
    assert index is not None
    with pytest.raises(RuntimeError, match="pending"):
        index.active_counts()
    monkeypatch.setattr(service, "write_agent_run_state", original)
    with monkeypatch.context() as recovery:
        recovery.setattr(os, "scandir", _unexpected_scan)
        report = _api(manager.persistence, "recover_pending_run_index")(batch_size=1)
    assert report["recovered"] == 1 and not report["errors"]
    assert manager.load(task.id).status != "DONE"
    assert index.active_counts()[0] == 1


# LLM: 调度接缝仅用于把手动维护与自动生产接线的观察分开，不跳过任何业务断言。
# 函数用途: 关闭本测试中竞争的后台线程，让失败状态可以确定性复查。
def _disable_maintenance(monkeypatch):
    monkeypatch.setattr(SubAgentPersistenceService, "_start_run_index_maintenance", lambda self: None, raising=False)


# LLM: 所有旧格式样例都包含模型必填 thought/plan，避免夹具 TypeError 冒充迁移缺陷。
# 函数用途: 写一个可由正式 canonical reader 读取的旧 run。
def _legacy_task(workspace, run_id, **values):
    payload = {"id": run_id, "goal": "旧工作", "thought": "", "plan": [], "status": "RUNNING", **values}
    path = workspace / run_id / "task.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


# LLM: 只写真实 locator schema；声明的 canonical 目录不存在，复现 agent_run_state 的确定缺失错误。
# 函数用途: 写一个规范状态已丢失的旧 run 定位叶。
def _locator_without_canonical(workspace, run_id, **values):
    from agent_py_agent.agent.subagents.services.agent_run_state import STATE_LOCATOR_SCHEMA_VERSION

    lost = workspace.parent / "lost" / run_id
    path = workspace / run_id / "task.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": STATE_LOCATOR_SCHEMA_VERSION, "id": run_id, "run_id": run_id,
                                "task_dir": str(lost), "task_workspace_dir": str(lost),
                                "agent_run_workspace_dir": str(lost), **values}), encoding="utf-8")


# LLM: 热查询及 pending 点修复都不应通过目录枚举发现其它 run。
# 函数用途: 对意外的同步全扫立即报错。
def _unexpected_scan(*args, **kwargs):
    raise AssertionError("unexpected workspace enumeration")


# LLM: 同一进程同一观察点比较描述符，连接必须词法关闭而非依赖 GC。
# 函数用途: 返回当前 FD 数供每轮 I/O 和恢复校验使用。
def _fd_count():
    return len(os.listdir("/dev/fd" if Path("/dev/fd").exists() else "/proc/self/fd"))


# LLM: 描述符诊断不关闭句柄或回收对象；只在 FD 断言失败时提供实际观察点便于归因。
# 函数用途: 返回当前 FD 对应路径或设备身份，不把临时读失败解释成泄漏原因。
def _fd_snapshot():
    import fcntl

    directory = "/dev/fd" if Path("/dev/fd").exists() else "/proc/self/fd"
    result = {}
    for name in os.listdir(directory):
        try:
            descriptor = int(name)
            info = os.fstat(descriptor)
            if hasattr(fcntl, "F_GETPATH"):
                try:
                    target = os.fsdecode(fcntl.fcntl(descriptor, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0])
                except OSError:
                    target = f"dev={info.st_dev},ino={info.st_ino},mode={info.st_mode}"
            else:
                target = os.readlink(f"{directory}/{name}")
            result[name] = target
        except OSError:
            continue
    return result


# LLM: 只等待真实生产线程；有限等待失败保留覆盖事实，不用无界轮询掩盖卡住的迁移。
# 函数用途: 等待构造器启动的后台维护并确认工作已经停止。
def _join_maintenance(service):
    worker = getattr(service, "_run_index_maintenance_thread", None)
    assert worker is not None, "production maintenance was not scheduled"
    worker.join(timeout=10)
    assert not worker.is_alive(), service.run_index.coverage()


# LLM: 改变 scandir 顺序不能改变已持久库存；重启后只能按库存 seq 读固定批次。
# 函数用途: 证明批次游标不依赖目录位置，且一次库存后不重复跳过历史前缀。
def test_stable_inventory_survives_restart_and_directory_order_change(tmp_path, monkeypatch):
    workspace = tmp_path / "runs"
    for number in range(7):
        _legacy_task(workspace, f"legacy-{number}")
    index = SubagentRunIndex(workspace)
    original = os.scandir
    scans = []

    class Snapshot:
        def __init__(self, path):
            with original(path) as entries:
                self.entries = sorted(entries, key=lambda item: item.name, reverse=bool(len(scans) % 2))
            scans.append(path)

        def __enter__(self):
            return iter(self.entries)

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(os, "scandir", Snapshot)
    reads = []

    def loader(run_id):
        reads.append(run_id)
        return load_subagent_task(workspace, run_id)

    report = index.backfill(loader, batch_size=2)
    assert report["loaded"] <= 2 and not report["complete"]
    inventory_scans = len(scans)
    index = SubagentRunIndex(workspace)
    for _ in range(5):
        report = index.backfill(loader, batch_size=2)
        assert report["loaded"] <= 2
        if report["complete"]:
            break
    assert report["complete"], report
    assert len(scans) == inventory_scans == 1
    assert set(reads) == {f"legacy-{number}" for number in range(7)}
    assert index.active_counts() == (7, 0)


# LLM: 旧目录升级不能要求用户手动调用测试专用 backfill；这条测试不触发任何维护方法。
# 函数用途: 通过正式 manager 构造自动迁移旧目录并在重启后保持覆盖。
def test_manager_automatically_maintains_legacy_index(tmp_path):
    workspace = tmp_path / "runs"
    for number in range(130):
        _legacy_task(workspace, f"legacy-{number}")
    manager = SubAgentManager(workspace, workspace_root=tmp_path)
    _join_maintenance(manager.persistence)
    assert manager.persistence.run_index.active_counts() == (130, 0)
    restarted = SubAgentManager(workspace, workspace_root=tmp_path)
    assert restarted.persistence.run_index.active_counts() == (130, 0)


# LLM: 首存没有 locator 时仍必须用 pending 保存的精确框架路径恢复；不重放业务写入。
# 函数用途: 覆盖 canonical 已发表而首次 locator 写失败的真实保存入口及重启恢复。
def test_pending_first_write_repairs_missing_locator_on_restart(tmp_path, monkeypatch):
    from agent_py_agent.agent.subagents.services.persistence import service

    manager = SubAgentManager(tmp_path / "runs", workspace_root=tmp_path)
    original = service.write_private_json_file_atomic
    failed = []

    def unavailable(path, payload, *args, **kwargs):
        if path.parent.parent == manager.workspace and path.name == "task.json":
            failed.append(payload["run_id"])
            raise OSError("simulated I/O error")
        return original(path, payload, *args, **kwargs)

    monkeypatch.setattr(service, "write_private_json_file_atomic", unavailable)
    with pytest.raises(OSError, match="simulated"):
        manager.create_run(goal="首次发表")
    assert failed and not (manager.workspace / failed[0] / "task.json").exists()
    assert manager.persistence.run_index.coverage()["pending"] == 1
    monkeypatch.setattr(service, "write_private_json_file_atomic", original)
    with monkeypatch.context() as recovery:
        recovery.setattr(os, "scandir", _unexpected_scan)
        restarted = SubAgentManager(manager.workspace, workspace_root=tmp_path)
        _join_maintenance(restarted.persistence)
    assert restarted.load(failed[0]).id == failed[0]
    assert restarted.persistence.run_index.active_counts() == (1, 0)
    assert restarted.persistence.run_index.coverage()["pending"] == 0


# LLM: 未发表首存的 pending 只能在同 canonical 锁内确认没有旧状态后删除，不制造新 run。
# 函数用途: 覆盖 canonical 尚未发表、没有 locator 的首次保存中断及自动恢复。
def test_pending_first_write_without_publication_is_removed_on_restart(tmp_path, monkeypatch):
    from agent_py_agent.agent.subagents.services.persistence import service

    manager = SubAgentManager(tmp_path / "runs", workspace_root=tmp_path)
    original = service.write_agent_run_state
    failed = []

    def unavailable(state):
        failed.append(state)
        raise OSError("simulated I/O error")

    monkeypatch.setattr(service, "write_agent_run_state", unavailable)
    with pytest.raises(OSError, match="simulated"):
        manager.create_run(goal="尚未发表")
    assert failed and not failed[0].canonical_path.exists()
    assert manager.persistence.run_index.coverage()["pending"] == 1
    monkeypatch.setattr(service, "write_agent_run_state", original)
    with monkeypatch.context() as recovery:
        recovery.setattr(os, "scandir", _unexpected_scan)
        restarted = SubAgentManager(manager.workspace, workspace_root=tmp_path)
        _join_maintenance(restarted.persistence)
    assert restarted.persistence.run_index.active_counts() == (0, 0)
    assert restarted.persistence.run_index.known_ids([failed[0].run_id]) == set()


# LLM: 坏行必须保留 partial，修复同一精确文件后再续批，不能把错误当成“迁移完成”。
# 函数用途: 验证库存的错误重试与重启游标不会丢失未加载记录。
def test_inventory_errors_remain_partial_until_canonical_repaired(tmp_path, monkeypatch):
    _disable_maintenance(monkeypatch)
    workspace = tmp_path / "runs"
    _legacy_task(workspace, "legacy")
    path = workspace / "legacy" / "task.json"
    path.write_text("{", encoding="utf-8")
    manager = SubAgentManager(workspace, workspace_root=tmp_path)
    report = manager.persistence.backfill_run_index(batch_size=1)
    assert report["errors"] and not report["complete"]
    with pytest.raises(RuntimeError, match="partial"):
        manager.persistence.run_index.active_counts()
    _legacy_task(workspace, "legacy", status="DONE")
    restarted = SubAgentManager(workspace, workspace_root=tmp_path)
    report = restarted.persistence.backfill_run_index(batch_size=1)
    assert report["complete"] and not report["errors"]
    assert restarted.persistence.run_index.active_counts() == (0, 0)


# LLM: 规范状态确定缺失的旧行按锚点 list_runs 跳过：库存结项、不计容量；单次瞬态 I/O 只保留待重试，下一轮加载。
# 函数用途: 验证一条不可加载旧记录不会让索引永久 partial，容量与请求谱系读口恢复可用并排除它。
def test_unloadable_legacy_record_is_skipped_and_transient_error_retries(tmp_path, monkeypatch):
    from agent_py_agent.agent.subagents.manager import request_scope_runs

    _disable_maintenance(monkeypatch)
    workspace = tmp_path / "runs"
    _legacy_task(workspace, "legacy-live", root_id="parent")
    _legacy_task(workspace, "legacy-flaky", root_id="parent")
    _locator_without_canonical(workspace, "legacy-lost", root_id="parent")
    manager = SubAgentManager(workspace, workspace_root=tmp_path)
    service = manager.persistence
    load = service.load
    transient = []

    def flaky(run_id):
        if run_id == "legacy-flaky" and not transient:
            transient.append(run_id)
            raise OSError("simulated I/O error")
        return load(run_id)

    monkeypatch.setattr(service, "load", flaky)
    reports = []
    for _ in range(6):
        report = service.backfill_run_index(batch_size=1)
        reports.append(report)
        if report["errors"]:
            assert report["errors"] == [{"run_id": "legacy-flaky", "error_type": "OSError"}]
            assert not report["complete"] and report["remaining"] >= 1
            with service.run_index.connect() as db:
                assert db.execute("SELECT done,error_type FROM inventory WHERE run_id='legacy-flaky'").fetchone() == (0, "OSError")
        if report["complete"]:
            break
    # 三行各一轮，外加恰好一次瞬态重试。
    assert len(reports) == 4 and transient == ["legacy-flaky"]
    assert sum(bool(item["errors"]) for item in reports) == 1 and sum(item["loaded"] for item in reports) == 2
    assert report["complete"] and report["skipped"] == 1 and report["remaining"] == 0 and not report["errors"]
    index = service.run_index
    with index.connect() as db:
        assert db.execute("SELECT done,error_type FROM inventory WHERE run_id='legacy-lost'").fetchone() == (1, "FileNotFoundError")
    assert index.active_counts() == (2, 0)
    assert index.active_counts({"parent"}, "", None) == (2, 2)
    assert index.known_ids(["legacy-live", "legacy-flaky", "legacy-lost"]) == {"legacy-live", "legacy-flaky"}
    assert sorted(task.id for task in request_scope_runs(manager, {"parent"}, "", None)) == ["legacy-flaky", "legacy-live"]


# LLM: 生产后台维护不能因确定缺失的旧行停在首批；一次维护线程须越过它收敛到完整覆盖。
# 函数用途: 验证真实 manager 自动维护在跨批库存中排除缺失规范状态的旧行。
def test_manager_maintenance_completes_past_missing_canonical_record(tmp_path):
    workspace = tmp_path / "runs"
    for number in range(130):
        _legacy_task(workspace, f"legacy-{number}")
    _locator_without_canonical(workspace, "legacy-lost")
    manager = SubAgentManager(workspace, workspace_root=tmp_path)
    _join_maintenance(manager.persistence)
    coverage = manager.persistence.run_index.coverage()
    assert coverage["complete"] and coverage["skipped"] == 1 and coverage["remaining"] == 0, coverage
    assert manager.persistence.run_index.active_counts() == (130, 0)


# LLM: 缺入口以明确的契约断言报红，不把 AttributeError 当作有效业务回归。
# 函数用途: 在调用新索引 API 前验证它确实是可调用的生产接口。
def _api(owner, name):
    method = getattr(owner, name, None)
    assert callable(method), f"required bounded index API is missing: {name}"
    return method


# LLM: 系统编号含所有终态，cover 占用则保留未收口失败态，两者不可误套容量状态集。
# 函数用途: 验证新查询的精确 parent、展示前缀、给定 ID 和 covers closed 语义。
def test_index_lookup_apis_keep_system_ordinals_and_cover_ownership(tmp_path, monkeypatch):
    workspace = tmp_path / "runs"
    workspace.mkdir()
    index = SubagentRunIndex(workspace)
    next_index = _api(index, "next_system_index")
    known = _api(index, "known_ids")
    children = _api(index, "active_child_ids")
    rows = [
        ("done", "DONE", "parent", "agent-d1-custom-role-19"),
        ("blocked", "BLOCKED", "parent", "agent-d1-worker-7"),
        ("failed", "FAILED", "parent", "custom-name-999"),
        ("running", "RUNNING", "parent", "agent-d1-worker-3"),
        ("cancelled", "CANCELLED", "parent", "agent-d1-worker-5"),
        ("abandoned", "ABANDONED", "parent", "agent-d1-worker-6"),
        ("taken", "TAKEN_OVER", "parent", "agent-d1-worker-8"),
        ("other", "RUNNING", "other-parent", "agent-d1-worker-999"),
        ("depth", "DONE", "parent", "agent-d2-worker-29"),
        ("zero", "DONE", "parent", "agent-d1-worker-099"),
    ]
    with index.connect() as db:
        for number, (run_id, status, parent, name) in enumerate(rows):
            index.upsert(SubAgentTask(id=run_id, goal="状态", thought="", plan=[], status=status,
                                    parent_id=parent, agent_name=name, updated_at=number + 1), connection=db)
    with monkeypatch.context() as hot:
        hot.setattr(os, "scandir", _unexpected_scan)
        hot.setattr(Path, "glob", _unexpected_scan)
        before = _fd_count()
        for _ in range(40):
            assert next_index("parent", 1) == 20
            assert next_index("parent", 2) == 30
            assert next_index("missing", 1) == 1
            assert known(["done", "missing", "done", "failed"]) == {"done", "failed"}
            assert children("parent") == ["running", "failed", "blocked"]
            assert _fd_count() - before == 0
    restarted = SubagentRunIndex(workspace)
    assert restarted.next_system_index("parent", 1) == 20
    assert restarted.known_ids(["other"]) == {"other"}


# LLM: 幂等/修复身份只经共享 contract_identity 解析，期望结果直接调用原 _same_* 比较器。
# 函数用途: 用同一结构化夹具逐项对照原复用过滤和反向历史排序，覆盖 wildcard 与展示身份。
@pytest.mark.parametrize("kind", ["work", "repair", "idempotency"])
@pytest.mark.parametrize("profile", ["exact", "root_wildcard", "role_wildcard", "custom_display"])
def test_reusable_ids_match_original_constraints(tmp_path, kind, profile):
    from agent_py_agent.agent.agent_core.orchestration.create_constraints import (
        _same_idempotency_scope,
        _same_repair_scope,
        _same_work_scope,
    )
    from agent_py_agent.agent.subagents.services.contract_identity import (
        idempotency_contract_identity_from_context_packs,
        repair_contract_identity_from_context_packs,
    )

    workspace = tmp_path / "runs"
    workspace.mkdir()
    index = SubagentRunIndex(workspace)
    query = _api(index, "reusable_ids")
    packs = [{"contract": {"schema": "subagent_idempotency_contract.v1", "kind": "build",
                           "idempotency_key": "key", "scope_refs": ["/product/"]}},
             {"contract": {"schema": "subagent_repair_contract.v1", "kind": "repair",
                           "failed_run_ids": ["failed"], "target_artifact_refs": ["/product/"]}}]
    params = CreateRunParams(goal="复用", thought="", plan=[], parent_id="parent", root_id="root", role="worker",
                             agent_name="agent-d1-worker-99", extra_write_roots=[" /product/ ", "/product"],
                             context_packs=packs, attributes={"work_scope_key": "scope"})
    if profile == "root_wildcard":
        params = replace(params, root_id="")
    elif profile == "role_wildcard":
        params = replace(params, role="")
    elif profile == "custom_display":
        params = replace(params, agent_name=" My Module ")
    base = SubAgentTask(id="matching-old", goal="复用", thought="", plan=[], status="DONE",
                        parent_id="parent", root_id="root", role="worker", agent_name="agent-d1-worker-1",
                        task_dir="/internal/task", allowed_write_roots=["/internal/task/", "/product/"],
                        context_packs=packs, attributes={"work_scope_key": "scope"}, updated_at=1)
    changes = [
        {}, {"id": "matching-tie"}, {"id": "newer", "updated_at": 9},
        {"id": "wrong-parent", "parent_id": "other"}, {"id": "wrong-root", "root_id": "other"},
        {"id": "empty-root", "root_id": ""}, {"id": "empty-role", "role": ""},
        {"id": "wrong-role", "role": "tester"}, {"id": "wrong-roots", "allowed_write_roots": ["/other"]},
        {"id": "custom-display", "agent_name": "my module"},
        {"id": "generic-display", "agent_name": "agent-d2-tester-4"},
        {"id": "failed-status", "status": "FAILED"},
        {"id": "blocked-status", "status": "BLOCKED"},
        {"id": "paused-status", "status": "PAUSED"},
        {"id": "other-scope", "context_packs": [], "attributes": {"work_scope_key": "other"}},
    ]
    tasks = []
    for change in changes:
        task = copy.deepcopy(base)
        for key, value in change.items():
            setattr(task, key, value)
        tasks.append(task)
        index.upsert(task)
    identity = {"work": "scope", "repair": repair_contract_identity_from_context_packs(packs),
                "idempotency": idempotency_contract_identity_from_context_packs(packs)}[kind]
    predicate = {"work": _same_work_scope, "repair": _same_repair_scope,
                 "idempotency": _same_idempotency_scope}[kind]
    original_order = sorted(tasks, key=lambda task: task.id)
    original_order.sort(key=lambda task: task.updated_at or task.created_at, reverse=True)
    expected = [task.id for task in reversed(original_order) if predicate(task, params, identity)]
    assert query(params, kind, identity) == expected
    assert SubagentRunIndex(workspace).reusable_ids(params, kind, identity) == expected


# LLM: 精确复用候选过多时不静默截断后冒充“未找到”，而是 fail closed 让调用方停止新建。
# 函数用途: 钉住候选资源上界和未知类型拒绝行为。
def test_reusable_candidate_budget_is_explicit(tmp_path):
    workspace = tmp_path / "runs"
    workspace.mkdir()
    index = SubagentRunIndex(workspace)
    query = _api(index, "reusable_ids")
    params = CreateRunParams(goal="复用", thought="", plan=[], parent_id="parent", attributes={"work_scope_key": "scope"})
    with index.connect() as db:
        for number in range(65):
            index.upsert(SubAgentTask(id=f"same-{number}", goal="复用", thought="", plan=[], parent_id="parent",
                                     attributes={"work_scope_key": "scope"}), connection=db)
    with pytest.raises(RuntimeError, match="candidate.*budget"):
        query(params, "work", "scope")
    with pytest.raises(ValueError, match="kind"):
        query(params, "unknown", "scope")


# LLM: 正常读入口只修固定批次 pending，不得因冷 miss 同步建立目录库存。
# 函数用途: 验证 prepare 生产接缝真实可用且健康查询不触发全扫。
def test_prepare_run_index_is_bounded_and_repairs_pending(tmp_path, monkeypatch):
    _disable_maintenance(monkeypatch)
    manager = SubAgentManager(tmp_path / "runs", workspace_root=tmp_path)
    prepare = _api(manager.persistence, "prepare_run_index")
    task = manager.create_run(goal="已发表")
    manager.persistence.run_index.mark_pending(task.id)
    with monkeypatch.context() as hot:
        hot.setattr(os, "scandir", _unexpected_scan)
        before = _fd_count()
        report = prepare()
        assert report["pending"] == 0
        assert manager.persistence.run_index.active_counts() == (1, 0)
        assert _fd_count() - before == 0


# LLM: 索引包含 owner 私有的谱系及合约身份，新建权限不得由进程 umask 放宽。
# 函数用途: 验证 SQLite 文件私有权限及显式关闭连接不留下 FD。
def test_run_index_file_is_private_and_descriptors_are_closed(tmp_path):
    workspace = tmp_path / "runs"
    workspace.mkdir(mode=0o700)
    before = _fd_count()
    index = SubagentRunIndex(workspace)
    assert index.path.stat().st_mode & 0o077 == 0
    for _ in range(20):
        assert index.coverage()["complete"]
        assert _fd_count() == before


# LLM: 所有危险文件都在隔离测试目录；拒绝应先于 SQLite 打开，外部样本字节不得被改动。
# 函数用途: 验证索引及 SQLite sidecar 的符号链接不能重定向读写。
@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm"])
def test_run_index_rejects_symbolic_database_files(tmp_path, suffix):
    workspace = tmp_path / "runs"
    workspace.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_bytes(b"private fixture")
    (workspace / (".run-index.sqlite3" + suffix)).symlink_to(outside)
    before = _fd_count()
    with pytest.raises((OSError, ValueError), match="regular|symlink|private"):
        SubagentRunIndex(workspace)
    assert outside.read_bytes() == b"private fixture"
    assert _fd_count() == before


# LLM: 原派工点查不能跟随子目录链接；复用主代理已有红绿夹具，保留准确观察点。
# 函数用途: 阻止一个 child id 通过目录链接领取外部任务状态。
def test_point_read_does_not_follow_child_directory_symlink(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration.dispatch_progress_seed import (
        _canonical_child_rows,
    )

    root = tmp_path / "task"
    directory = root / "work" / "agents"
    directory.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "canonical_state.json").write_text(json.dumps({"run_id": "child-1", "status": "DONE"}))
    (directory / "child-1").symlink_to(outside, target_is_directory=True)
    assert _canonical_child_rows(root, ["child-1"]) == {}


# LLM: 候选路径中的文件只有精确 run_id 一致才可计入进度，文件名不赋予另一条身份。
# 函数用途: 拒绝 canonical 内声明了另一个 child id 的行。
def test_point_read_does_not_credit_another_id(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration.dispatch_progress_seed import (
        _canonical_child_rows,
    )

    root = tmp_path / "task"
    directory = root / "work" / "agents" / "child-1"
    directory.mkdir(parents=True)
    (directory / "canonical_state.json").write_text(json.dumps({"run_id": "child-2", "status": "DONE"}))
    assert _canonical_child_rows(root, ["child-1", "child-2"]) == {}


# LLM: 只按已验证的 opaque ID 点读；含路径分量的候选不能改变目录边界。
# 函数用途: 同时验证拒绝路径跳转与正常精确行仍可见。
def test_point_read_rejects_path_parts_and_keeps_exact_row(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration.dispatch_progress_seed import (
        _canonical_child_rows,
    )

    root = tmp_path / "task"
    directory = root / "work" / "agents" / "child-1"
    directory.mkdir(parents=True)
    row = {"run_id": "child-1", "status": "DONE"}
    (directory / "canonical_state.json").write_text(json.dumps(row))
    assert _canonical_child_rows(root, ["../child-1", "child-1"]) == {"child-1": row}


# LLM: 原 covers 占用的持久索引读取失败不能变成健康空集，从而允许重复派工。
# 函数用途: 保留主代理已有的索引错误传播回归。
def test_cover_index_error_is_not_empty_occupancy(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration.dispatch_progress_seed import (
        _active_direct_cover_owners,
    )

    manager = SubAgentManager(tmp_path / "runs", workspace_root=tmp_path)
    manager.create_run(goal="原工作", parent_id="parent-1", attributes={"covers": ["req-a"]})

    def unavailable(parent_id):
        raise OSError("simulated I/O error")

    monkeypatch.setattr(manager.persistence.run_index, "active_child_ids", unavailable, raising=False)
    with pytest.raises(OSError, match="simulated I/O error"):
        _active_direct_cover_owners(SimpleNamespace(subagents=manager), "parent-1")


# LLM: 仅创建调用显式给候选 ID；非创建的原无参调用仍保留全集合协议，不借性能修复收窄功能。
# 函数用途: 保留主代理已有的无参 known-child 兼容回归。
def test_legacy_known_child_call_keeps_original_membership(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration.dispatch_progress_seed import (
        _known_child_run_ids,
    )

    manager = SubAgentManager(tmp_path / "runs", workspace_root=tmp_path)
    child = manager.create_run(goal="旧展示行")
    monkeypatch.setattr(manager.persistence.run_index, "known_ids", lambda ids: {child.id}.intersection(ids), raising=False)
    assert child.id in _known_child_run_ids(SimpleNamespace(subagents=manager))


# LLM: 精确 ID 读取器仍按更新时间倒序；复用调用方必须再反向才能保持原最早优先。
# 函数用途: 保留主代理已有的 canonical 复核后顺序回归。
def test_reuse_candidates_keep_earliest_canonical_order(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration.create_constraints import _reuse_candidates

    manager = SubAgentManager(tmp_path / "runs", workspace_root=tmp_path)
    older = manager.create_run(goal="同一工作")
    newer = manager.create_run(goal="同一工作")
    older.updated_at, newer.updated_at = 10.0, 20.0
    manager.save(older)
    manager.save(newer)
    monkeypatch.setattr(manager.persistence.run_index, "reusable_ids", lambda *args: [older.id, newer.id], raising=False)
    selected = list(_reuse_candidates(manager, SimpleNamespace(), "work", "same"))
    assert [task.id for task in selected] == [older.id, newer.id]


# LLM: 调用方一轮 prepare 只允许一个固定 pending 页，剩余未知继续阻止容量；下一轮再精确续修。
# 函数用途: 验证首存未发表库存超过一页时同步恢复上界为 64，且不用目录发现。
def test_prepare_pending_recovery_has_fixed_batch_and_zero_fd_delta(tmp_path, monkeypatch):
    _disable_maintenance(monkeypatch)
    manager = SubAgentManager(tmp_path / "runs", workspace_root=tmp_path)
    index = manager.persistence.run_index
    for number in range(65):
        run_id = f"unpublished-{number}"
        task = SubAgentTask(id=run_id, goal="未发表", thought="", plan=[],
                            task_dir=str(tmp_path / "orders" / run_id),
                            task_workspace_dir=str(tmp_path / "task"),
                            agent_run_workspace_dir=str(tmp_path / "task" / "work" / "agents" / run_id))
        index.mark_pending(run_id, task=task)
    with monkeypatch.context() as hot:
        hot.setattr(os, "scandir", _unexpected_scan)
        before = _fd_count()
        report = manager.persistence.prepare_run_index()
        assert report["discarded"] == 64 and report["pending"] == 1
        assert not report["errors"]
        with pytest.raises(RuntimeError, match="pending"):
            index.active_counts()
        assert _fd_count() == before
        report = manager.persistence.prepare_run_index()
        assert report["discarded"] == 1 and report["pending"] == 0
        assert index.active_counts() == (0, 0)
        assert _fd_count() == before


# LLM: 已发表状态丢失或换成别的 ID 都是未知，不可清 pending 释放容量；只修正同一精确样本后才恢复。
# 函数用途: 验证精确 pending 恢复对身份改变及已发表 canonical 缺失 fail closed。
@pytest.mark.parametrize("failure", ["wrong_id", "missing"])
def test_pending_published_state_is_not_discarded_on_invalid_canonical(tmp_path, monkeypatch, failure):
    _disable_maintenance(monkeypatch)
    manager = SubAgentManager(tmp_path / "runs", workspace_root=tmp_path)
    task = manager.create_run(goal="已发表")
    manager.persistence.run_index.mark_pending(task.id, task=task)
    canonical = Path(task.agent_run_workspace_dir) / "canonical_state.json"
    original = canonical.read_bytes()
    if failure == "wrong_id":
        payload = json.loads(original)
        payload["id"] = "another-run"
        canonical.write_text(json.dumps(payload), encoding="utf-8")
    else:
        canonical.unlink()
    report = manager.persistence.prepare_run_index()
    assert report["errors"] and report["pending"] == 1
    with pytest.raises(RuntimeError, match="pending"):
        manager.persistence.run_index.active_counts()
    canonical.write_bytes(original)
    report = manager.persistence.prepare_run_index()
    assert not report["errors"] and report["pending"] == 0
    assert manager.persistence.run_index.active_counts() == (1, 0)


# LLM: 目录库存中途失败不能发布覆盖；耐久页按 UNIQUE 去重续建并保留已经分配的 seq。
# 函数用途: 模拟库存阶段中断，重启后在有界 canonical 批次中完整恢复所有旧 run。
def test_interrupted_inventory_restarts_without_duplicate_or_lost_rows(tmp_path, monkeypatch):
    workspace = tmp_path / "runs"
    for number in range(70):
        _legacy_task(workspace, f"legacy-{number}")
    index = SubagentRunIndex(workspace)
    original = os.scandir

    class Interrupted:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            with original(self.path) as entries:
                self.entries = list(entries)
            return self.iterate()

        def iterate(self):
            seen = 0
            for entry in self.entries:
                yield entry
                if entry.is_dir(follow_symlinks=False):
                    seen += 1
                    if seen == 66:
                        raise OSError("simulated inventory interruption")

        def __exit__(self, *args):
            return False

    with monkeypatch.context() as interrupted:
        interrupted.setattr(os, "scandir", Interrupted)
        with pytest.raises(OSError, match="inventory interruption"):
            index.backfill(lambda run_id: load_subagent_task(workspace, run_id), batch_size=7)
    assert not index.coverage()["inventory_ready"]
    with index.connect() as db:
        retained = dict(db.execute("SELECT run_id,seq FROM inventory"))
    assert len(retained) == 64
    restarted = SubagentRunIndex(workspace)
    for _ in range(11):
        report = restarted.backfill(lambda run_id: load_subagent_task(workspace, run_id), batch_size=7)
        assert report["loaded"] <= 7
        if report["complete"]:
            break
    assert report["complete"] and restarted.active_counts() == (70, 0)
    with restarted.connect() as db:
        final = dict(db.execute("SELECT run_id,seq FROM inventory"))
    assert len(final) == 70
    assert all(final[run_id] == seq for run_id, seq in retained.items())


# LLM: 测试写者只操作本测试新建任务，持原 canonical guard 后才发表 DONE；不创建替代生产锁。
# 函数用途: 在另一个进程中暂停一个真实规范写入，用于验证迁移互斥。
def _canonical_guard_writer(workspace, ready, release):
    from agent_py_agent.agent.common.json_io import locked_json_path
    from agent_py_agent.agent.subagents.services.persistence.service import (
        _canonical_state_guard_path,
    )

    workspace = Path(workspace)
    with locked_json_path(_canonical_state_guard_path(workspace, SimpleNamespace(id="legacy"))):
        ready.set()
        if not release.wait(10):
            raise RuntimeError("canonical writer release timed out")
        _legacy_task(workspace, "legacy", status="DONE")


# LLM: 迁移必须在原跨进程 guard 内读取当前 canonical，不能拿锁前缓存状态回填。
# 函数用途: 对照真实文件锁证明回填等待并观察另一个进程的新终态。
def test_backfill_rechecks_canonical_under_cross_process_guard(tmp_path):
    import multiprocessing

    workspace = tmp_path / "runs"
    _legacy_task(workspace, "legacy")
    index = SubagentRunIndex(workspace)
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    writer = context.Process(target=_canonical_guard_writer, args=(str(workspace), ready, release))
    reports, reads, errors = [], [], []

    def load(run_id):
        task = load_subagent_task(workspace, run_id)
        reads.append(task.status)
        return task

    def migrate():
        try:
            reports.append(index.backfill(load, batch_size=1))
        except Exception as exc:
            errors.append(exc)

    writer.start()
    worker = threading.Thread(target=migrate)
    try:
        assert ready.wait(10), "canonical writer did not acquire guard"
        worker.start()
        worker.join(timeout=0.1)
        assert worker.is_alive() and reads == []
    finally:
        release.set()
        writer.join(timeout=10)
        if writer.is_alive():
            writer.terminate()
            writer.join(timeout=5)
        if worker.ident is not None:
            worker.join(timeout=10)
    assert writer.exitcode == 0
    writer.close()
    assert not worker.is_alive() and not errors
    assert reports[0]["complete"] and reads == ["DONE"]
    assert index.active_counts() == (0, 0)


# LLM: 宿主从不以链接创建 run；迁移不跟随、不改写 canonical、不登记成员，也不让一条链接永久阻塞覆盖与停止传播。
# 函数用途: 验证符号链接 run 或 task 文件链接按确定不可用结项跳过，而不是被当作完整成员发布。
@pytest.mark.parametrize("kind", ["directory", "file"])
def test_migration_rejects_linked_canonical_rows(tmp_path, kind):
    workspace = tmp_path / "runs"
    workspace.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    payload = _legacy_task(outside, "linked")
    target = outside / "linked" / "task.json"
    if kind == "directory":
        (workspace / "linked").symlink_to(target.parent, target_is_directory=True)
    else:
        (workspace / "linked").mkdir()
        (workspace / "linked" / "task.json").symlink_to(target)
    index = SubagentRunIndex(workspace)
    assert not index.coverage()["complete"]
    report = index.backfill(lambda run_id: load_subagent_task(workspace, run_id), batch_size=1)
    assert report["complete"] and report["skipped"] == 1 and report["loaded"] == 0 and not report["errors"]
    assert index.known_ids(["linked"]) == set() and index.active_counts() == (0, 0)
    assert json.loads(target.read_bytes()) == payload


# LLM: 旧投影已有 committed 行就是发表证据，不能因为新 lookup 尚未回填且 canonical/locator 都缺失而当作首存。
# 函数用途: 验证升级窗口中的旧 committed 行不会被 pending 恢复误删除。
def test_legacy_committed_row_is_not_treated_as_unpublished(tmp_path, monkeypatch):
    _disable_maintenance(monkeypatch)
    manager = SubAgentManager(tmp_path / "runs", workspace_root=tmp_path)
    index = manager.persistence.run_index
    with index.connect() as db:
        db.execute("INSERT INTO runs VALUES ('legacy',1,0,1,0)")
    task = SubAgentTask(id="legacy", goal="旧已发表记录", thought="", plan=[],
                        task_dir=str(tmp_path / "orders" / "legacy"),
                        task_workspace_dir=str(tmp_path / "task"),
                        agent_run_workspace_dir=str(tmp_path / "task" / "work" / "agents" / "legacy"))
    index.mark_pending(task.id, task=task)
    report = manager.persistence.prepare_run_index()
    assert report["errors"] and report["discarded"] == 0 and report["pending"] == 1
    with pytest.raises(RuntimeError, match="pending"):
        index.active_counts()


# LLM: v1 的 complete/offset 不能证明新增 lookup 覆盖；升级必须重新核 canonical，不能信旧容量零值。
# 函数用途: 验证真实旧表结构升级会 partial、重核固定批次并恢复所有新增查询。
def test_v1_projection_upgrade_rechecks_canonical_and_resets_offset(tmp_path):
    import sqlite3

    workspace = tmp_path / "runs"
    _legacy_task(workspace, "legacy", parent_id="parent", root_id="root", agent_name="agent-d1-worker-41")
    db = sqlite3.connect(workspace / ".run-index.sqlite3")
    try:
        db.executescript("CREATE TABLE runs(run_id TEXT PRIMARY KEY,active INTEGER NOT NULL,audit_epoch INTEGER,"
                         "updated REAL NOT NULL,pending INTEGER NOT NULL DEFAULT 0);"
                         "CREATE TABLE meta(key TEXT PRIMARY KEY,value INTEGER NOT NULL);"
                         "INSERT INTO runs VALUES ('legacy',0,0,1,0);"
                         "INSERT INTO meta VALUES ('complete',1),('offset',9000);")
        db.commit()
    finally:
        db.close()
    index = SubagentRunIndex(workspace)
    assert not index.coverage()["complete"] and index.coverage()["offset"] == 0
    with pytest.raises(RuntimeError, match="partial"):
        index.active_counts()
    report = index.backfill(lambda run_id: load_subagent_task(workspace, run_id), batch_size=1)
    assert report["complete"] and report["loaded"] == 1
    assert index.active_counts() == (1, 0)
    restarted = SubagentRunIndex(workspace)
    assert restarted.next_system_index("parent", 1) == 42
    assert restarted.known_ids(["legacy", "missing"]) == {"legacy"}


# LLM: 三个真实 manager 热入口必须接 prepare；测试不显式恢复，不把维护方法单测当成生产接线证明。
# 函数用途: 验证容量、请求谱系和派工身份读会有界修复当前提交的 pending。
@pytest.mark.parametrize("entry", ["capacity", "request", "dispatch"])
def test_creation_manager_reads_prepare_pending_projection(tmp_path, monkeypatch, entry):
    from agent_py_agent.agent.subagents.manager import (
        creation_capacity_counts,
        creation_run_index,
        request_scope_runs,
    )

    _disable_maintenance(monkeypatch)
    manager = SubAgentManager(tmp_path / "runs", workspace_root=tmp_path)
    task = manager.create_run(goal="已发表但投影中断", parent_id="parent", root_id="parent")
    manager.persistence.run_index.mark_pending(task.id, task=task)
    with monkeypatch.context() as hot:
        hot.setattr(os, "scandir", _unexpected_scan)
        if entry == "capacity":
            assert creation_capacity_counts(SimpleNamespace(subagents=manager), "") == (1, 0)
        elif entry == "request":
            assert [item.id for item in request_scope_runs(manager, {"parent"}, "", None)] == [task.id]
        else:
            assert creation_run_index(manager).active_counts() == (1, 0)
    assert manager.persistence.run_index.coverage()["pending"] == 0


# LLM: 老投影登记本身就是待复核来源，缺 locator 不能在库存中静默消失：带错误类型结项为跳过，不再永久阻塞覆盖。
# 函数用途: 钉住升级时 SQL 旧 ID 进入库存，规范记录缺失时按确定不可用记录跳过。
def test_v1_missing_locator_is_recorded_as_skipped(tmp_path):
    import sqlite3

    workspace = tmp_path / "runs"
    workspace.mkdir()
    with sqlite3.connect(workspace / ".run-index.sqlite3") as db:
        db.executescript("CREATE TABLE runs(run_id TEXT PRIMARY KEY,active INTEGER NOT NULL,audit_epoch INTEGER,"
                         "updated REAL NOT NULL,pending INTEGER NOT NULL DEFAULT 0);"
                         "CREATE TABLE meta(key TEXT PRIMARY KEY,value INTEGER NOT NULL);"
                         "INSERT INTO runs VALUES ('missing',0,0,1,0);"
                         "INSERT INTO meta VALUES ('complete',1),('offset',50);")
    index = SubagentRunIndex(workspace)
    report = index.backfill(lambda run_id: load_subagent_task(workspace, run_id), batch_size=1)
    assert report["complete"] and report["skipped"] == 1 and not report["errors"], report
    with index.connect() as db:
        assert db.execute("SELECT done,error_type FROM inventory WHERE run_id='missing'").fetchone() == (1, "FileNotFoundError")


# LLM: 无恢复凭据的旧 pending 必须保持未知；再次登记不能倒推从未发表过。
# 函数用途: 防止重试写入把升级前未知 pending 降格后丢弃。
def test_old_unknown_pending_cannot_be_downgraded_by_retry(tmp_path, monkeypatch):
    _disable_maintenance(monkeypatch)
    manager = SubAgentManager(tmp_path / "runs", workspace_root=tmp_path)
    index = manager.persistence.run_index
    with index.connect() as db:
        db.execute("INSERT INTO runs VALUES ('legacy',1,0,1,1)")
    task = SubAgentTask(id="legacy", goal="旧中断", thought="", plan=[],
                        task_dir=str(tmp_path / "orders" / "legacy"),
                        task_workspace_dir=str(tmp_path / "task"),
                        agent_run_workspace_dir=str(tmp_path / "task" / "work" / "agents" / "legacy"))
    index.mark_pending(task.id, task=task)
    report = manager.persistence.prepare_run_index()
    assert report["errors"] and report["pending"] == 1 and report["discarded"] == 0, report


# LLM: 原全量列表先按 ID 收集再稳定按时间倒序；最终逆序在时间平局时是较大 ID 优先。
# 函数用途: 比较真实 canonical 复核后的复用次序，防止索引与调用方双重反转平局规则。
def test_reuse_ties_match_original_full_list_order(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration.create_constraints import _reuse_candidates

    manager = SubAgentManager(tmp_path / "runs", workspace_root=tmp_path)
    for _ in range(2):
        task = manager.create_run(goal="同合同", attributes={"work_scope_key": "same"})
        task.updated_at = 2_000_000_000.0
        manager.save(task)
    persisted = manager.list_runs()
    assert {item.updated_at for item in persisted} == {2_000_000_000.0}
    original = [item.id for item in reversed(persisted)]
    params = CreateRunParams(goal="同合同", thought="", plan=[], role="", attributes={"work_scope_key": "same"})
    assert len(original) == 2
    assert [item.id for item in _reuse_candidates(manager, params, "work", "same")] == original


# LLM: 用 SQLite 真实 VM 计数而非时间阈值；终态与活跃故意共享同一 scope，不能用无关旧根掩盖 UNION 扫描。
# 函数用途: 验证容量 SQL 工作量不随同一请求内 1k 到 10k 的终态增长而线性增加。
def test_same_scope_terminal_history_does_not_expand_capacity_sql_work(tmp_path, monkeypatch):
    from contextlib import contextmanager

    workspace = tmp_path / "runs"
    workspace.mkdir()
    index = SubagentRunIndex(workspace)
    observations = []
    for target in (1000, 10000):
        start = 0 if target == 1000 else 1000
        with index.connect() as db:
            for number in range(start, target):
                index.upsert(SubAgentTask(id=f"done-{number}", goal="同请求历史", thought="", plan=[],
                                         root_id="same-scope", status="DONE"), connection=db)
            if target == 1000:
                for number in range(8):
                    index.upsert(SubAgentTask(id=f"active-{number}", goal="同请求活跃", thought="", plan=[],
                                             root_id="same-scope", status="RUNNING"), connection=db)
        steps = [0]
        original = index.connect

        @contextmanager
        def measured():
            with original() as db:
                def tick():
                    steps[0] += 1
                    return 0
                db.set_progress_handler(tick, 1)
                yield db

        with monkeypatch.context() as observed:
            observed.setattr(index, "connect", measured)
            assert index.active_counts({"same-scope"}) == (8, 8)
        observations.append(steps[0])
    print("same_scope_capacity_vm_steps=" + json.dumps(observations))
    assert observations[1] <= observations[0] + 256, observations
