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


# LLM: 缓存信任龄同时核 locator 与 canonical 两个文件；只改两者 mtime，不改内容或指纹以外的事实。
# 函数用途: 把一个子代理的 task.json 及其 canonical_state.json 一起推到粗 mtime 窗口外。
def _age_task_file(agent: SimpleAgent, run_id: str, age_seconds: int = 10) -> None:
    from agent_py_agent.agent.subagents.services.agent_run_state import (
        canonical_state_path_from_payload,
    )

    task_file = agent.subagents.workspace / run_id / "task.json"
    canonical = canonical_state_path_from_payload(json.loads(task_file.read_text(encoding="utf-8")))
    old_mtime_ns = time.time_ns() - age_seconds * 1_000_000_000
    for path in (task_file, canonical):
        if path is not None:
            os.utime(path, ns=(path.stat().st_atime_ns, old_mtime_ns))


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


# LLM: 只在pytest临时目录写原reader支持的legacy任务，mtime移出保护窗；不启动Agent、模型或宿主。
# 函数用途: 创建可被真实持久化服务读取和外部修改的任务记录。
def _selected_task_file(manager, run_id, status, *, updated_at=2.0):
    path = manager.workspace / run_id / "task.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": run_id, "goal": "selection fixture", "thought": "", "plan": [],
        "status": status, "created_at": 1.0, "updated_at": updated_at, "attributes": {"nested": []}}), encoding="utf-8")
    old = time.time_ns() - 10_000_000_000
    os.utime(path, ns=(old, old))
    assert manager.load(run_id).status == status
    return path


# LLM: 真实非托管manager只读临时任务；不构造SimpleAgent或接入真实owner运行账。
# 函数用途: 准备筛选接口与巡检接线共同使用的轻量真实manager。
def _selected_manager(tmp_path):
    from agent_py_agent.agent.subagents.manager import SubAgentManager

    return SubAgentManager(tmp_path / "runs")


# LLM: 新接口尚未实现时在明确的接口断言失败，而非导入报错；不提供测试专用产品旁路。
# 函数用途: 取得任务要求的选择读口并验证接口存在。
def _selected_reader(target, name):
    reader = getattr(target, name, None)
    assert callable(reader), f"missing selection API: {name}"
    return reader


# LLM: 冷/热扫描都应完整校验文件而不复制未选终态；坏文件报错和删除淘汰不可被筛选吞掉。
# 函数用途: 同时钉住零deepcopy、实时坏账与删除失效。
@pytest.mark.parametrize("warm_cache", [False, True])
def test_selected_terminal_history_skips_deepcopy_but_keeps_fresh_errors(tmp_path, monkeypatch, warm_cache):
    manager = _selected_manager(tmp_path)
    paths = [_selected_task_file(manager, f"history-{i}", status)
             for i, status in enumerate(("COMPLETED", "FAILED", "CANCELLED"))]
    service = manager.persistence
    if warm_cache:
        assert len(service.list_runs()) == 3
    copied = []
    original = copy.deepcopy

    # LLM: 不替换真实复制结果，计数发生在夹具和热缓存准备之后。
    # 函数用途: 捕获选择扫描的所有deepcopy调用。
    def counted(value, memo=None):
        copied.append(value)
        return original(value, memo)

    monkeypatch.setattr(copy, "deepcopy", counted)
    selected = _selected_reader(service, "list_runs_selected_report")
    first = selected(lambda task: task.status == "RUNNING")
    assert first.runs == [] and first.load_errors == [] and copied == []
    assert len(service._run_cache) == 3
    paths[0].write_text("broken-json", encoding="utf-8")
    paths[1].unlink()
    second = selected(lambda task: task.status == "RUNNING")
    assert second.runs == [] and copied == []
    assert [error["run_id"] for error in second.load_errors] == ["history-0"]
    assert "history-1" not in service._run_cache


# LLM: 返回值须与缓存隔离、按原时间倒序；predicate只读私有引用且不持缓存锁执行。
# 函数用途: 验证只复制命中任务及锁外回调/复制合同。
@pytest.mark.parametrize("warm_cache", [False, True])
def test_selected_matching_copies_are_sorted_isolated_and_outside_lock(tmp_path, monkeypatch, warm_cache):
    manager = _selected_manager(tmp_path)
    for run_id, status, updated in (("first", "RUNNING", 10), ("second", "RUNNING", 20), ("old", "DONE", 30)):
        _selected_task_file(manager, run_id, status, updated_at=updated)
    service = manager.persistence
    if warm_cache:
        service.list_runs()
    copied, selected_refs = [], []
    original = copy.deepcopy

    # LLM: 竞争线程真实尝试获取RLock，不能用同线程重入代替锁外证明。
    # 函数用途: 观测筛选与复制期间缓存锁是否可用。
    def probe_lock():
        acquired = service._run_cache_lock.acquire(blocking=False)
        if acquired:
            service._run_cache_lock.release()
        return acquired

    with ThreadPoolExecutor(max_workers=1) as executor:
        # LLM: 只读任务状态并记录引用，不修改缓存中的任务。
        # 函数用途: 确认过滤在复制前、锁外执行。
        def predicate(task):
            assert executor.submit(probe_lock).result(timeout=5)
            selected_refs.append(task)
            return task.status == "RUNNING"

        # LLM: 保留原deepcopy，锁状态断言仅针对任务根对象。
        # 函数用途: 记录实际命中任务的复制次数。
        def counted(value, memo=None):
            if hasattr(value, "id"):
                assert executor.submit(probe_lock).result(timeout=5)
                copied.append(value.id)
            return original(value, memo)

        monkeypatch.setattr(copy, "deepcopy", counted)
        result = _selected_reader(service, "list_runs_selected_report")(predicate)
    assert result.load_errors == []
    assert [task.id for task in result.runs] == ["second", "first"]
    assert sorted(copied) == ["first", "second"]
    assert all(task is service._run_cache[task.id][1] for task in selected_refs)
    result.runs[0].attributes["nested"].append("caller mutation")
    assert service._run_cache["second"][1].attributes["nested"] == []


# LLM: 各阶段独立走真实locator stat；同轮直接磁盘改写及新增不能被TTL或共享快照遮蔽。
# 函数用途: 验证标准manager筛选在阶段间立即看见外部状态变化。
def test_selected_manager_observes_disk_changes_between_stages(tmp_path):
    from agent_py_agent.agent.subagents import manager as module

    manager = _selected_manager(tmp_path)
    _selected_task_file(manager, "old", "DONE")
    choose = _selected_reader(module, "select_manager_runs")
    assert choose(manager, lambda task: task.status == "PENDING") == []
    _selected_task_file(manager, "old", "PENDING")
    _selected_task_file(manager, "new", "PENDING")
    assert {task.id for task in choose(manager, lambda task: task.status == "PENDING")} == {"old", "new"}


# LLM: 必须保留实例/类级_cached_run_copy替身，并在其返回副本上过滤，不能绕过原测试接缝。
# 函数用途: 验证选择API的复制替身兼容路径。
@pytest.mark.parametrize("patch_class", [False, True])
def test_selected_service_preserves_cached_copy_monkeypatch(tmp_path, monkeypatch, patch_class):
    manager = _selected_manager(tmp_path)
    _selected_task_file(manager, "active", "RUNNING")
    _selected_task_file(manager, "old", "DONE")
    service = manager.persistence
    seen = []
    original = type(service)._cached_run_copy

    # LLM: 保留真实复制并记录所有任务，检验替身路径不能被选择优化跳过。
    # 函数用途: 模拟类级复制替身。
    def patched(self, run_id, task_file):
        seen.append(run_id)
        return original(self, run_id, task_file)

    monkeypatch.setattr(type(service) if patch_class else service, "_cached_run_copy",
                        patched if patch_class else patched.__get__(service, type(service)))
    tasks = _selected_reader(service, "list_runs_selected")(lambda task: task.status == "RUNNING")
    assert [task.id for task in tasks] == ["active"] and seen == ["active", "old"]


# LLM: 原manager或persistence的旧读口被覆盖时，优化必须让位给旧list_runs合同，包括类级覆盖。
# 函数用途: 防止按需筛选绕过既有测试替身与业务适配器。
@pytest.mark.parametrize("target,name", [("manager", "list_runs"), ("manager", "list_runs_report"),
                                         ("persistence", "list_runs"), ("persistence", "list_runs_report")])
@pytest.mark.parametrize("patch_class", [False, True])
def test_select_manager_runs_honors_old_read_hooks(tmp_path, monkeypatch, target, name, patch_class):
    from agent_py_agent.agent.subagents import manager as module

    manager = _selected_manager(tmp_path)
    _selected_task_file(manager, "active", "RUNNING")
    _selected_task_file(manager, "old", "DONE")
    choose = _selected_reader(module, "select_manager_runs")
    owner = manager if target == "manager" else manager.persistence
    original_reader = getattr(type(owner), name)

    # LLM: 包装原读口而不改变任务集合，通过复制数证明真正沿旧list_runs执行。
    # 函数用途: 模拟实例或类级旧读口替换。
    def replaced(self):
        return original_reader(self)

    monkeypatch.setattr(type(owner) if patch_class else owner, name,
                        replaced if patch_class else replaced.__get__(owner, type(owner)))
    copied = []
    original_copy = copy.deepcopy

    # LLM: 只记录任务根复制，原复制行为继续执行；旧路径必须复制全部两条后再筛选。
    # 函数用途: 区分旧读口兼容分支与提前筛选分支。
    def counted(value, memo=None):
        if hasattr(value, "id"):
            copied.append(value.id)
        return original_copy(value, memo)

    monkeypatch.setattr(copy, "deepcopy", counted)
    assert [task.id for task in choose(manager, lambda task: task.status == "RUNNING")] == ["active"]
    assert sorted(copied) == ["active", "old"]


# LLM: SimpleNamespace即使提供同名新接口，也不是标准manager；旧list_runs仍是替身权威读口。
# 函数用途: 验证轻量替身不被新接口绕过。
def test_select_manager_runs_uses_list_runs_for_namespace():
    from agent_py_agent.agent.subagents import manager as module

    seen = []
    active = SimpleNamespace(id="active", status="RUNNING")
    old = SimpleNamespace(id="old", status="DONE")

    # LLM: 新接口在替身上不得被调用，避免用假优化掩盖旧行为兼容问题。
    # 函数用途: 捕捉对SimpleNamespace新读口的误调用。
    def forbidden(*args):
        pytest.fail("namespace must use list_runs")

    fake = SimpleNamespace(list_runs=lambda: seen.append(True) or [active, old], list_runs_selected=forbidden)
    choose = _selected_reader(module, "select_manager_runs")
    assert choose(fake, lambda task: task.status == "RUNNING") == [active]
    assert seen == [True]


# LLM: 使用真实manager及全部四个实际巡检入口，未命中历史任务不能产生根deepcopy。
# 函数用途: 验证按需API已真正接进各阶段，而不只是新增未使用的读口。
def test_idle_supervision_stages_do_not_copy_terminal_history(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration.dispatch import (
        capability_auto_sweep as sweep,
    )
    from agent_py_agent.agent.subagents.direct_parent_lifecycle import reconcile_all_parent_waits

    manager = _selected_manager(tmp_path)
    for index in range(12):
        _selected_task_file(manager, f"old-{index}", ("DONE", "FAILED", "CANCELLED")[index % 3])
    manager.list_runs()
    agent = SimpleNamespace(subagents=manager, conversation_store=None)
    copied = []
    original = copy.deepcopy

    # LLM: 计数真实任务复制，不触碰状态或巡检控制分支。
    # 函数用途: 度量空闲巡检是否仍复制终态任务。
    def counted(value, memo=None):
        if hasattr(value, "id"):
            copied.append(value.id)
        return original(value, memo)

    monkeypatch.setattr(copy, "deepcopy", counted)
    assert sweep.auto_start_stalled_orphans(agent)["started"] == 0
    assert not any(sweep._reconcile_conversation_parent_lifecycle(agent).values())
    assert sweep._reclaim_dead_running_runs(agent) == []
    assert reconcile_all_parent_waits(manager)["checked"] == 0
    assert copied == []


# LLM: report中的逐文件load_errors从来不使旧list_runs抛异常；只有真正的列表异常才返回等待不可用。
# 函数用途: 保持父等待巡检原有的best-effort和异常返回口径。
def test_selected_parent_wait_keeps_partial_load_and_raised_error_contract(tmp_path, monkeypatch):
    from agent_py_agent.agent.subagents.direct_parent_lifecycle import reconcile_all_parent_waits

    manager = _selected_manager(tmp_path)
    path = _selected_task_file(manager, "bad", "DONE")
    path.write_text("broken-json", encoding="utf-8")
    selected_report = _selected_reader(manager, "list_runs_selected_report")
    assert len(selected_report(lambda task: False).load_errors) == 1
    assert reconcile_all_parent_waits(manager)["ok"] is True

    # LLM: 仅制造新列表入口自身的异常，不将结构化坏行误当该异常。
    # 函数用途: 验证原PARENT_WAIT_LOAD_ERROR错误合同。
    def unavailable(predicate=None):
        raise OSError("run list unavailable")

    monkeypatch.setattr(manager, "list_runs_selected", unavailable)
    report = reconcile_all_parent_waits(manager)
    assert report["ok"] is False and report["checked"] is None
    assert report["error_code"] == "PARENT_WAIT_LOAD_ERROR" and report["error_type"] == "OSError"


# LLM: 空列表、已关闭和非Audit的真实_open_audit_watch拒绝均不应读取运行列表。
# 函数用途: 验证没有打开Audit Watch时完全跳过快照扫描。
@pytest.mark.parametrize("rows", [[], [{"audit_guarantee": True, "closed": True}], [{"audit_guarantee": False}]])
def test_empty_audit_watch_scan_does_not_load_worker_runs(tmp_path, monkeypatch, rows):
    from agent_py_agent.agent.ingestion import source_worker as module

    reads = []
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path),
                            subagents=SimpleNamespace(list_runs=lambda: reads.append(True) or []))
    monkeypatch.setattr(module, "list_states", lambda home: rows)
    report = module.reconcile_audit_source_workers(agent)
    assert report["checked"] == 0 and report["workers"] == []
    assert reads == []


# LLM: 明确的空运行快照仍是有效快照；首个活跃watch才读，后续watch不能因空列表重复读取。
# 函数用途: 验证延迟快照的调用顺序和空快照复用。
def test_audit_watch_snapshot_is_lazy_and_empty_snapshot_is_reused(tmp_path, monkeypatch):
    from agent_py_agent.agent.ingestion import source_worker as module

    events, snapshots = [], []
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path),
                            subagents=SimpleNamespace(list_runs=lambda: events.append("runs") or []))
    monkeypatch.setattr(module, "list_states", lambda home: ["closed", "first", "second"])

    # LLM: 只替换来源投影，记录扫描顺序；不赋予假watch任何执行权。
    # 函数用途: 提供首个非空state之前的关闭来源。
    def opened(home, row):
        events.append(row)
        return None if row == "closed" else SimpleNamespace(watch_id=row)

    # LLM: 不创建或派发worker，只记录该来源收到的实际快照对象。
    # 函数用途: 核对两个来源共享同一有效空列表。
    def reconciled(agent, state, *, runs_snapshot=None):
        snapshots.append(runs_snapshot)
        return {}

    monkeypatch.setattr(module, "_open_audit_watch", opened)
    monkeypatch.setattr(module, "_reconcile_one_audit_source_worker", reconciled)
    monkeypatch.setattr(module, "_record_source_reconcile_result", lambda *args: None)
    report = module.reconcile_audit_source_workers(agent)
    assert events == ["closed", "first", "runs", "second"]
    assert snapshots == [[], []] and snapshots[0] is snapshots[1]
    assert len(report["workers"]) == 2


# LLM: predicate错误不是加载坏账；新旧选择路径须传播同一异常，并完成本轮逐文件核验和删除淘汰。
# 函数用途: 防止筛选故障被伪装成健康空列表，也不让异常留下已删除的缓存任务。
@pytest.mark.parametrize("entry", ["service_list", "service_report", "manager_list", "manager_report", "helper", "fake_helper"])
@pytest.mark.parametrize("error_type", [OSError, ValueError])
def test_selected_predicate_error_propagates_after_full_cache_maintenance(tmp_path, monkeypatch, entry, error_type):
    from agent_py_agent.agent.subagents.manager import select_manager_runs

    manager = _selected_manager(tmp_path)
    paths = {run_id: _selected_task_file(manager, run_id, "DONE")
             for run_id in ("a-good", "b-good", "c-bad", "gone")}
    manager.list_runs()
    paths["gone"].unlink()
    paths["c-bad"].write_text("broken-json", encoding="utf-8")
    service = manager.persistence
    original = service._cached_run_ref
    loaded, evaluated = [], []
    error = error_type("predicate failed")

    # LLM: 透传真实私有引用读取，坏行继续真实报错；不替换stat或缓存状态。
    # 函数用途: 观察筛选失败后其余locator是否仍被核验。
    def observed(run_id, task_file):
        loaded.append(run_id)
        return original(run_id, task_file)

    # LLM: 模拟可信只读predicate自身失败，不对私有任务做任何写操作。
    # 函数用途: 检验原异常对象传播且后续predicate不再执行。
    def predicate(task):
        evaluated.append(task.id)
        raise error

    monkeypatch.setattr(service, "_cached_run_ref", observed)
    if entry in {"helper", "fake_helper"}:
        target = manager if entry == "helper" else SimpleNamespace(list_runs=manager.list_runs)

        # LLM: 标准和替身均经实际公共选择辅助函数，不在测试里复制产品分支。
        # 函数用途: 为两种manager适配同一错误断言。
        def reader(predicate):
            return select_manager_runs(target, predicate)
    else:
        target = service if entry.startswith("service") else manager
        reader = getattr(target, "list_runs_selected_report" if entry.endswith("report") else "list_runs_selected")
    with pytest.raises(error_type) as raised:
        reader(predicate)
    assert raised.value is error
    assert evaluated == ["a-good"]
    assert loaded == ["a-good", "b-good", "c-bad"]
    assert "gone" not in service._run_cache
