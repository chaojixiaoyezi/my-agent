"""list_runs mtime 缓存测试:命中复用 + 副本独立(不污染)+ mtime 变重读 + 删除清理。

大数量子代理下 dispatch 每轮全量 list_runs 拖慢(实测 600 个 745ms);mtime 缓存让未变的
子代理只 stat 复用(实测省 ~88%)。这里钉死缓存正确性,尤其副本独立——改返回对象不污染缓存。
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


def _agent(tmp_path: Path) -> SimpleAgent:
    return SimpleAgent(AgentConfig(enable_tools=False, memory_path="m.jsonl"), tmp_path)


def _age_task_file(agent: SimpleAgent, run_id: str, age_seconds: int = 10) -> None:
    task_file = agent.subagents.workspace / run_id / "task.json"
    task_stat = task_file.stat()
    old_mtime_ns = time.time_ns() - age_seconds * 1_000_000_000
    os.utime(task_file, ns=(task_stat.st_atime_ns, old_mtime_ns))


def test_list_runs_cache_reuses_and_counts_stable(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    for i in range(3):
        run_id = agent.subagents.create_run(goal=f"t{i}", agent_name=f"a{i}").id
        _age_task_file(agent, run_id)
    r1 = agent.subagents.list_runs()
    r2 = agent.subagents.list_runs()  # 第二次走缓存
    assert len(r1) == 3 and len(r2) == 3
    assert {t.id for t in r1} == {t.id for t in r2}


def test_recent_window_read_is_not_cached_aba_probe(tmp_path: Path, monkeypatch) -> None:
    from agent_py_agent.agent.subagents.services.persistence import service as persistence_service

    agent = _agent(tmp_path)
    run_id = agent.subagents.create_run(goal="窗口内不缓存", agent_name="worker").id
    task_file = agent.subagents.workspace / run_id / "task.json"
    first_stat = task_file.stat()
    mtime_seconds = first_stat.st_mtime_ns / 1_000_000_000

    with monkeypatch.context() as clock:
        clock.setattr(persistence_service, "_now", lambda: mtime_seconds + 1.0)
        first_read = next(task for task in agent.subagents.list_runs() if task.id == run_id)
    cached_in_window = run_id in agent.subagents.persistence._run_cache
    assert first_read.status == "PLANNING"

    running = agent.subagents.load(run_id)
    running.status = "RUNNING"
    agent.subagents.save(running)
    saved_stat = task_file.stat()
    assert saved_stat.st_ino != first_stat.st_ino
    assert agent.subagents.load(run_id).status == "RUNNING"

    path_type = type(task_file)
    real_stat = path_type.stat

    def frozen_first_generation_stat(path, *args, **kwargs):
        if path == task_file:
            return first_stat
        return real_stat(path, *args, **kwargs)

    with monkeypatch.context() as clock:
        clock.setattr(path_type, "stat", frozen_first_generation_stat)
        clock.setattr(persistence_service, "_now", lambda: mtime_seconds + 3.0)
        after_window = next(task for task in agent.subagents.list_runs() if task.id == run_id)

    assert after_window.status == "RUNNING"
    assert cached_in_window is False


def test_list_runs_returns_independent_copies(tmp_path: Path) -> None:
    """改 list_runs 返回的对象不污染缓存——下次拿到的仍是磁盘真值。"""
    agent = _agent(tmp_path)
    run_id = agent.subagents.create_run(goal="t", agent_name="a").id
    _age_task_file(agent, run_id)
    r1 = agent.subagents.list_runs()
    r1[0].status = "MUTATED"  # 改返回的副本
    r2 = agent.subagents.list_runs()
    assert r2[0].status != "MUTATED"  # 缓存没被污染


def test_list_runs_reloads_on_mtime_change(tmp_path: Path) -> None:
    """子代理被 save(mtime 变)后，窗口外的缓存读取应识别新指纹并重读。"""
    agent = _agent(tmp_path)
    run_id = agent.subagents.create_run(goal="t", agent_name="a").id
    task_file = agent.subagents.workspace / run_id / "task.json"
    _age_task_file(agent, run_id, age_seconds=10)
    cached_mtime_ns = task_file.stat().st_mtime_ns
    agent.subagents.list_runs()  # 在粗 mtime 窗口外建立旧缓存。
    task = agent.subagents.load(run_id)
    task.status = "RUNNING"
    agent.subagents.save(task)
    _age_task_file(agent, run_id, age_seconds=5)
    assert task_file.stat().st_mtime_ns != cached_mtime_ns
    reloaded = next(item for item in agent.subagents.list_runs() if item.id == run_id)
    assert reloaded.status == "RUNNING"


def test_list_runs_evicts_deleted_run_from_cache(tmp_path: Path) -> None:
    """子代理从磁盘消失后,缓存项被清理,list_runs 不再返回它。"""
    agent = _agent(tmp_path)
    rid = agent.subagents.create_run(goal="t", agent_name="a").id
    _age_task_file(agent, rid)
    agent.subagents.list_runs()  # 窗口外才应进入缓存。
    assert rid in agent.subagents.persistence._run_cache
    shutil.rmtree(agent.subagents.persistence.workspace / rid)
    assert agent.subagents.list_runs() == []
    assert rid not in agent.subagents.persistence._run_cache


def test_list_runs_by_ids_reads_only_selected_canonical_runs(tmp_path: Path) -> None:
    """活动投影的 exact-id 入口不扫描或缓存未选中的历史 run。"""
    agent = _agent(tmp_path)
    selected = agent.subagents.create_run(goal="selected", agent_name="selected").id
    ignored = agent.subagents.create_run(goal="ignored", agent_name="ignored").id
    _age_task_file(agent, selected)
    _age_task_file(agent, ignored)

    report = agent.subagents.list_runs_by_ids_report([selected, selected])

    assert [task.id for task in report.runs] == [selected]
    assert report.load_errors == []
    assert selected in agent.subagents.persistence._run_cache
    assert ignored not in agent.subagents.persistence._run_cache


def test_list_runs_by_ids_keeps_missing_run_as_structured_error(tmp_path: Path) -> None:
    """索引若指向已丢失记录，不能把它静默当成空列表。"""
    agent = _agent(tmp_path)

    report = agent.subagents.list_runs_by_ids_report(["subagent-missing"])

    assert report.runs == []
    assert len(report.load_errors) == 1
    assert report.load_errors[0]["run_id"] == "subagent-missing"


def test_list_runs_for_root_uses_index_then_exact_canonical_reads(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """后台 root 查询不能为一棵小树复制其它历史 run。"""
    agent = _agent(tmp_path)
    selected = agent.subagents.create_run(goal="selected", agent_name="selected")
    selected.root_id = "root-selected"
    selected.parent_id = "root-selected"
    selected.depth = 1
    agent.subagents.save(selected)
    ignored = agent.subagents.create_run(goal="ignored", agent_name="ignored")
    ignored.root_id = "root-ignored"
    ignored.parent_id = "root-ignored"
    ignored.depth = 1
    agent.subagents.save(ignored)
    agent.subagents.persistence._run_cache.clear()
    _age_task_file(agent, selected.id)
    _age_task_file(agent, ignored.id)

    def fail_full_scan():
        raise AssertionError("managed root lookup must not scan every run")

    monkeypatch.setattr(agent.subagents.persistence, "list_runs_report", fail_full_scan)

    report = agent.subagents.list_runs_for_root_report("root-selected")

    assert [task.id for task in report.runs] == [selected.id]
    assert report.load_errors == []
    assert selected.id in agent.subagents.persistence._run_cache
    assert ignored.id not in agent.subagents.persistence._run_cache


# LLM: 固定任务要求的独立条数预算及单项字节预算，不能用无界缓存代替容量修复。
# 函数用途: 防止正负缓存容量退回导致历史任务循环扫描抖动。
def test_wp8_cache_default_budgets() -> None:
    from agent_py_agent.agent.subagents.services.persistence.service import (
        SubAgentPersistenceService,
    )

    assert SubAgentPersistenceService._MAX_RUN_CACHE_ENTRIES == 4096
    assert SubAgentPersistenceService._MAX_NEGATIVE_RUN_CACHE_ENTRIES == 4096
    assert SubAgentPersistenceService._MAX_RUN_CACHE_ITEM_BYTES == 256 * 1024


# LLM: 只在临时目录写合法直达locator，路径、原解析器和缺失错误都真实，不创建运行中的Agent。
# 函数用途: 为失效历史记录的读盘计数和恢复测试准备文件。
def _missing_locator(workspace: Path, run_id: str) -> tuple[Path, Path]:
    task_file = workspace / run_id / "task.json"
    task_file.parent.mkdir(parents=True)
    canonical = workspace.parent / "work" / "agents" / run_id / "canonical_state.json"
    task_file.write_text(json.dumps({
        "schema_version": "subagent-state-locator.v1",
        "run_id": run_id,
        "task_workspace_dir": str(workspace.parent),
        "agent_run_workspace_dir": str(canonical.parent),
    }), encoding="utf-8")
    old = time.time_ns() - 10_000_000_000
    os.utime(task_file, ns=(old, old))
    return task_file, canonical


# LLM: 640条真实缺失locator超过旧512容量；分别记录冷/热扫描读数，错误不许被优化成无记录。
# 函数用途: 验证首扫每文件只读一次，第二扫零正文读取且全部负缓存保留。
def test_wp8_large_missing_locator_scan_reads_once_then_hits_cache(tmp_path, monkeypatch) -> None:
    from agent_py_agent.agent.subagents.services.persistence.service import (
        SubAgentPersistenceService,
    )

    service = SubAgentPersistenceService(SimpleNamespace(workspace=tmp_path / "locators"))
    run_ids = {f"missing-{number:04d}" for number in range(640)}
    paths = {_missing_locator(service.workspace, run_id)[0] for run_id in sorted(run_ids)}
    reads: list[Path] = []
    original = Path.read_text

    # LLM: 透传真实Path读取，只对本轮locator记数，不伪造JSON或文件存在性。
    # 函数用途: 统计两轮扫描各自的正文读取次数。
    def counted_read(path, *args, **kwargs):
        if path in paths:
            reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted_read)
    first = service.list_runs_report()
    cold_reads = len(reads)
    cold_cache_size = len(service._missing_canonical_cache)
    second = service.list_runs_report()

    for report in (first, second):
        assert report.runs == []
        assert len(report.load_errors) == len(run_ids)
        assert {error["run_id"] for error in report.load_errors} == run_ids
    assert (cold_reads, len(reads) - cold_reads, cold_cache_size) == (640, 0, 640)
    assert set(reads) == paths
    assert set(service._missing_canonical_cache) == run_ids


# LLM: 用另一个真实线程非阻塞获取RLock，而非同线程重入；冷/热两条返回路径均须保深副本。
# 函数用途: 验证深拷贝期间其它读者可拿缓存锁，返回对象仍与快照隔离。
@pytest.mark.parametrize("warm_cache", [False, True])
def test_wp8_deepcopy_does_not_hold_cache_lock(tmp_path, monkeypatch, warm_cache) -> None:
    from agent_py_agent.agent.subagents.services.persistence import service as module

    agent = _agent(tmp_path)
    run_id = agent.subagents.create_run(goal="锁外复制", agent_name="worker").id
    _age_task_file(agent, run_id)
    service = agent.subagents.persistence
    if warm_cache:
        assert len(service.list_runs_report().runs) == 1
    original = copy.deepcopy
    unlocked: list[bool] = []

    # LLM: 非阻塞获取实际缓存锁，拿到后同线程归还，不用时延推断持锁状态。
    # 函数用途: 由竞争线程观测深拷贝时锁是否已释放。
    def probe_lock():
        acquired = service._run_cache_lock.acquire(blocking=False)
        if acquired:
            service._run_cache_lock.release()
        return acquired

    with ThreadPoolExecutor(max_workers=1) as executor:
        # LLM: 原deepcopy仍执行，探针只观察该run的根对象，不截断递归复制。
        # 函数用途: 在复制发生时让竞争线程试锁并记录结果。
        def observed_copy(value, memo=None):
            if getattr(value, "id", None) == run_id:
                unlocked.append(executor.submit(probe_lock).result(timeout=5))
            return original(value, memo)

        monkeypatch.setattr(module.copy, "deepcopy", observed_copy)
        report = service.list_runs_report()

    assert report.load_errors == []
    assert unlocked == [True]
    snapshot = service._run_cache[run_id][1]
    assert report.runs[0] is not snapshot
    report.runs[0].attributes["caller_mutation"] = True
    assert "caller_mutation" not in snapshot.attributes


# LLM: 只给真实缺失且原始声明等于已解析父路径的locator附加优化提示；其它缺失仍沿原异常类型。
# 函数用途: 验证reader传出可信canonical路径，使缓存不用重读locator。
def test_wp8_missing_locator_error_carries_canonical_path(tmp_path) -> None:
    from agent_py_agent.agent.subagents.services.agent_run_state import read_agent_state_payload

    task_file, canonical = _missing_locator(tmp_path / "locators", "missing")
    with pytest.raises(FileNotFoundError) as error:
        read_agent_state_payload(task_file)
    assert getattr(error.value, "canonical_path", None) == canonical


# LLM: 快捷参数已由reader核验，缓存仍须重核实际存在性；直接调用不允许发生任何locator正文读取。
# 函数用途: 验证免重读路径以及reader报缺失后目标重现时不误建负缓存。
@pytest.mark.parametrize("reappeared", [False, True])
def test_wp8_missing_cache_hint_rechecks_existence_without_reading(tmp_path, monkeypatch, reappeared):
    from agent_py_agent.agent.common.cache_freshness import cache_stat_signature
    from agent_py_agent.agent.subagents.services.persistence.service import (
        SubAgentPersistenceService,
    )

    workspace = tmp_path / "locators"
    service = SubAgentPersistenceService(SimpleNamespace(workspace=workspace))
    task_file, canonical = _missing_locator(workspace, "missing")
    if reappeared:
        canonical.parent.mkdir(parents=True)
        canonical.write_text("{}", encoding="utf-8")

    # LLM: 本测试只调用缓存快捷口，任何read_text都说明偷偷退回二次解析。
    # 函数用途: 让快捷口的非预期读盘成为显式失败。
    def forbidden_read(*args, **kwargs):
        raise AssertionError("canonical hint must avoid reading locator")

    monkeypatch.setattr(Path, "read_text", forbidden_read)
    service._cache_missing_canonical(
        "missing", task_file, cache_stat_signature(task_file.stat()), canonical_path=canonical,
    )
    assert ("missing" in service._missing_canonical_cache) is not reappeared


# LLM: 负缓存命中仍须核真实canonical是否重现，恢复后沿原reader加载，不能永远记作丢失。
# 函数用途: 验证已缓存的缺失状态可被真实权威文件恢复打破。
def test_wp8_missing_cache_invalidates_when_canonical_returns(tmp_path):
    agent = _agent(tmp_path)
    task = agent.subagents.create_run(goal="恢复权威文件", agent_name="worker")
    _age_task_file(agent, task.id)
    canonical = Path(task.agent_run_workspace_dir) / "canonical_state.json"
    original = canonical.read_text(encoding="utf-8")
    canonical.unlink()
    service = agent.subagents.persistence

    missing = service.list_runs_report()
    assert missing.runs == [] and len(missing.load_errors) == 1
    assert task.id in service._missing_canonical_cache
    canonical.write_text(original, encoding="utf-8")
    restored = service.list_runs_report()

    assert restored.load_errors == []
    assert [item.id for item in restored.runs] == [task.id]
    assert task.id not in service._missing_canonical_cache


# LLM: 非缺失错误和未满足原始路径等式的locator不得携带快捷提示，不改变原FileNotFoundError合同。
# 函数用途: 防止优化把存在但坏格式、非法路径或解析后不等的声明当可信缺失。
@pytest.mark.parametrize("case", ["present_non_object", "parent_traversal", "symlink_declared"])
def test_wp8_reader_does_not_hint_unverified_missing_paths(tmp_path, case):
    from agent_py_agent.agent.subagents.services.agent_run_state import read_agent_state_payload

    task_file, canonical = _missing_locator(tmp_path / "locators", "missing")
    if case == "present_non_object":
        canonical.parent.mkdir(parents=True)
        canonical.write_text("[]", encoding="utf-8")
    else:
        payload = json.loads(task_file.read_text(encoding="utf-8"))
        if case == "parent_traversal":
            payload["agent_run_workspace_dir"] = str(canonical.parent / ".." / "missing")
        else:
            target = tmp_path / "physical"
            target.mkdir()
            alias = tmp_path / "alias"
            alias.symlink_to(target, target_is_directory=True)
            payload["agent_run_workspace_dir"] = str(alias)
        task_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(FileNotFoundError) as error:
        read_agent_state_payload(task_file)
    assert not hasattr(error.value, "canonical_path")
