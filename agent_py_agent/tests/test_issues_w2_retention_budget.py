# LLM: 用真实临时Owner、权威线程/投影文件与plan/apply反证TTL、分片和完整退休清单。
# 模块用途: 覆盖W2-B的子代理快速退休、usage搬移及预算游标主链。
from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_py_agent.agent.common.json_io import write_private_json_file_atomic
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.memory_store import retention_apply as apply
from agent_py_agent.agent.memory_store import retention_scan as scan
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.retention import (
    MemoryRetentionService,
    apply_owner_retention,
)
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)


# LLM: 只创建真实pytest临时Owner，服务与生产解析、执行、门禁不替换。
# 函数用途: 装配本轮预算与退休测试环境。
def _runtime(tmp_path):
    home = ensure_my_agent_home(tmp_path / "home")
    return home, MemoryRetentionService(home_paths=home, candidates=CandidateService(home.owner_memory_candidates_jsonl))


# LLM: 线程用生产DTO与私有原子写者落盘；不建立路由，明确模拟已解绑权威记录，mtime不授权TTL。
# 函数用途: 创建指定ID、业务年龄和可选活跃任务的临时线程。
def _thread(home, thread_id="thread-old", *, days=400, active=()):
    root = Path(home.owner_home_dir) / "conversations"
    thread = ConversationThread(thread_id=thread_id, canonical_user_id="test",
        updated_at=(NOW - timedelta(days=days)).timestamp(), active_task_ids=tuple(active))
    path = root / "threads" / f"{thread_id}.json"
    write_private_json_file_atomic(path, thread.to_dict())
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))
    return thread, path


# LLM: 投影由精确run ID路径与正式私有写者生成，缺失与非终态不靠mtime补判。
# 函数用途: 写入用于快速退休的子代理投影。
def _projection(home, projection_run_id="subagent-abc", **updates):
    path = Path(home.owner_home_dir) / "agents" / projection_run_id / "state.json"
    write_private_json_file_atomic(path, {"run_id": projection_run_id, "status": "done",
        "ended_at": (NOW - timedelta(days=5)).timestamp(), **updates})
    return path


# LLM: 仅明确终态、已交付且投影足龄的子代理走3天快路，普通线程保留365天。
# 函数用途: 验证快路的正面对照及缺失、pending、运行中、错ID保护。
@pytest.mark.parametrize("projection,expected", [
    ({}, True), (None, False), ({"status": "running"}, False),
    ({"completion_notification_status": "pending"}, False), ({"delivery_pending": True}, False),
    ({"consume_pending": True}, False), ({"callback_pending": True}, False),
    ({"run_id": "subagent-other"}, False), ({"ended_at": None}, False),
])
def test_subagent_fast_path_requires_positive_settled_projection(tmp_path, projection, expected):
    home, service = _runtime(tmp_path)
    thread, path = _thread(home, "thread-subagent-abc", days=4)
    _, normal = _thread(home, "thread-normal", days=4)
    if projection is not None:
        _projection(home, **projection)
    report = service.apply(now=NOW)
    assert path.exists() is not expected
    assert normal.exists()
    actions = [action for action in report.actions if action.record_id == thread.thread_id]
    assert bool(actions) is expected
    if expected:
        assert actions[0].status == "trashed"
        assert actions[0].reason == "terminal_subagent_thread_older_than_3_days"


# LLM: 新配置可缺省为3天，零关闭快路，bool或负值不能被自动纠正。
# 函数用途: 验证可选策略解析与原365天字段保持。
@pytest.mark.parametrize("value", [0, True, -1, "3", 1.5])
def test_subagent_policy_zero_and_invalid_values(tmp_path, value):
    home, service = _runtime(tmp_path)
    _, path = _thread(home, "thread-subagent-abc", days=4)
    _projection(home)
    policy = json.loads(home.owner_retention_json.read_text())
    policy["subagent_thread_days"] = value
    write_private_json_file_atomic(home.owner_retention_json, policy)
    report = service.apply(now=NOW)
    assert path.exists()
    if value != 0:
        assert any(error.error_code == "MEMORY_RETENTION_POLICY_INVALID" for error in report.errors)
    else:
        assert report.ok and not report.actions
    assert policy["conversation_days"] == 365


# LLM: 规划后投影转pending时不能只依赖旧线程cutoff；仍沿原执行入口复核。
# 函数用途: 验证执行端重新确认子代理终态交付证明。
def test_subagent_projection_changed_after_plan_is_kept(tmp_path):
    home, service = _runtime(tmp_path)
    _, path = _thread(home, "thread-subagent-abc", days=4)
    _projection(home)
    plan = service.plan(now=NOW)
    assert any(action.path == path for action in plan.actions)
    _projection(home, callback_pending=True)
    report = apply.execute_retention_plan(home=home, candidates=service.candidates, plan=plan, now=NOW)
    assert path.exists()
    assert next(action for action in report.actions if action.path == path).status == "state_changed"


# LLM: usage家族按精确安全线程名整体移动，不搬共享锁及append-once侧车；永久清除只在宽限后。
# 函数用途: 验证模型usage的完整清单、字节保持和31天后真实purge。
def test_retirement_moves_usage_family_and_purges_after_grace(tmp_path):
    home, service = _runtime(tmp_path)
    thread, path = _thread(home)
    root = path.parent.parent
    usage = root / "model_usage"
    usage.mkdir()
    paths = [usage / f"{thread.thread_id}.jsonl", *[usage / f".{thread.thread_id}-usage.sqlite3{suffix}"
        for suffix in ("", "-wal", "-shm", "-journal")]]
    for index, item in enumerate(paths):
        item.write_bytes(f"usage-{index}\n".encode())
    sidecars = [usage / f"{thread.thread_id}.jsonl.lock", usage / f"{thread.thread_id}.jsonl.append-once"]
    for item in sidecars:
        item.write_bytes(b"shared sidecar")
    before = {item.relative_to(root): item.read_bytes() for item in paths}
    report = service.apply(now=NOW)
    moved = next(action for action in report.actions if action.record_id == thread.thread_id)
    assert moved.status == "trashed"
    assert not any(item.exists() for item in paths)
    assert {relative: (moved.destination / "payload" / relative).read_bytes() for relative in before} == before
    assert all(item.exists() for item in sidecars)
    assert not [action for action in service.plan(now=NOW).actions if action.path == moved.destination]
    later = service.apply(now=NOW + timedelta(days=31))
    assert any(action.path == moved.destination and action.status == "deleted" for action in later.actions)
    assert not moved.destination.exists()


# LLM: 观察真实thread解析上下文；不能把mtime当预筛选，不能用跳过或替身删除凑出分片通过。
# 函数用途: 验证300个旧业务线程在默认预算下有界推进并最终完整退休。
def test_three_hundred_threads_resume_and_finish(tmp_path, monkeypatch):
    home, _service = _runtime(tmp_path)
    paths = [_thread(home, f"thread-{index:04d}")[1] for index in range(300)]
    original, parsed = scan.read_json_object_report, []

    # LLM: 只统计扫描层真实解析，不替换pin扫描或JSON内容。
    # 函数用途: 记录各片实际解析的线程权威路径。
    def counted(path, **kwargs):
        if kwargs.get("context") == "memory_retention.conversation_thread":
            parsed.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(scan, "read_json_object_report", counted)
    first = apply_owner_retention(home, now=NOW, scan_budget=True)
    assert first.scan_partial and 0 < len(parsed) <= 128
    cursor = Path(home.owner_data_dir) / "retention_scan_cursor.json"
    assert cursor.is_file() and cursor.stat().st_mode & 0o777 == 0o600
    assert first.scan_cursor.get("conversation:conversations")
    retired = sum(action.status == "trashed" for action in first.actions)
    for _ in range(32):
        before = len(parsed)
        report = apply_owner_retention(home, now=NOW, scan_budget=True)
        assert len(parsed) - before <= 128
        retired += sum(action.status == "trashed" for action in report.actions)
        if not report.scan_partial:
            break
    assert not report.scan_partial and retired == 300
    assert not any(path.exists() for path in paths)
    assert "conversation:conversations" not in json.loads(cursor.read_text())


# LLM: 默认入口只推进一片，调用方沿返回的scan_partial重复调用才能完成全部权威。
# 函数用途: 验证默认预算模式可续扫完成300线程，没有慢路开关。
def test_default_budgeted_path_resumes_to_finish(tmp_path):
    home, _service = _runtime(tmp_path)
    paths = [_thread(home, f"thread-{index:04d}")[1] for index in range(300)]
    report = apply_owner_retention(home, now=NOW)
    assert report.scan_partial
    retired = sum(action.status == "trashed" for action in report.actions)
    for _ in range(32):
        report = apply_owner_retention(home, now=NOW)
        retired += sum(action.status == "trashed" for action in report.actions)
        if not report.scan_partial:
            break
    assert not report.scan_partial and report.scan_cursor == {}
    assert retired == 300
    assert not any(path.exists() for path in paths)


# LLM: 游标损坏只触发安全重扫；callback写游标必须经过真实提交门，不能门外偷写。
# 函数用途: 验证坏游标恢复以及协调器对游标写入的可观察提交。
def test_cursor_recovery_and_callback_commit(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import retention_scan_budget as budgets

    home, _service = _runtime(tmp_path)
    cursor = Path(home.owner_data_dir) / "retention_scan_cursor.json"
    cursor.write_text("{")
    assert budgets.load_scan_cursor(home) == {}
    for index in range(5):
        _thread(home, f"thread-{index:04d}")
    monkeypatch.setattr(budgets.RetentionScanBudget, "default", classmethod(lambda cls: cls(2, 1000000, float("inf"))))
    inside, saved = [], []
    original = budgets.save_scan_cursor

    # LLM: 观察游标的真实原子写入，断言它处于记录提交的动态范围。
    # 函数用途: 给游标写入标记门内事实。
    def saving(home, payload):
        assert inside
        original(home, payload)
        saved.append(json.loads(cursor.read_text()))

    # LLM: 同步执行原operation，不伪造授权或返回结果。
    # 函数用途: 模拟协调器既有同步短提交门。
    def commit(operation):
        inside.append(True)
        try:
            return operation()
        finally:
            inside.pop()

    monkeypatch.setattr(budgets, "save_scan_cursor", saving)
    report = apply_owner_retention(home, now=NOW, scan_budget=True, commit_transition=commit)
    assert report.scan_partial and saved == [report.scan_cursor]


# LLM: 原输入游标及磁盘逐字节不变；返回值可续扫但不能暗中推进调用方共享对象。
# 函数用途: 验证自定义条目预算与plan只读合同。
def test_custom_budget_plan_is_read_only_and_resumable(tmp_path):
    from agent_py_agent.agent.memory_store.retention_scan_budget import RetentionScanBudget

    home, service = _runtime(tmp_path)
    paths = [_thread(home, f"thread-{index:04d}")[1] for index in range(7)]
    cursor = {"conversation:conversations": {"after": "thread-0000.json"}}
    before = copy.deepcopy(cursor)
    plan = service.plan(now=NOW, scan_budget=RetentionScanBudget(3, 1000000, float("inf")), scan_cursor=cursor)
    assert plan.scan_partial
    assert [action.path.name for action in plan.actions] == [f"thread-{index:04d}.json" for index in (1, 2, 3)]
    assert plan.scan_cursor["conversation:conversations"] == {"after": "thread-0003.json"}
    assert cursor == before and all(path.is_file() for path in paths)
    assert not (Path(home.owner_data_dir) / "retention_scan_cursor.json").exists()
    assert not (Path(home.owner_trash_dir) / ".memory-retention-v2.lock").exists()


# LLM: 已到时间或字节上限不能继续解析；字节用一份真实权威大小，另有超大单项反证守住准入。
# 函数用途: 验证时间与字节预算均形成可继续的部分报告。
@pytest.mark.parametrize("kind", ["deadline", "bytes"])
def test_custom_deadline_and_byte_budget_stop(tmp_path, kind):
    from agent_py_agent.agent.memory_store.retention_scan_budget import RetentionScanBudget

    home, service = _runtime(tmp_path)
    for index in range(5):
        _, path = _thread(home, f"thread-{index:04d}")
    budget = RetentionScanBudget(100, path.stat().st_size if kind == "bytes" else 1000000,
        0.0 if kind == "deadline" else float("inf"))
    report = service.plan(now=NOW, scan_budget=budget)
    assert report.scan_partial
    assert len(report.actions) == (1 if kind == "bytes" else 0)
    assert budget.entries == (1 if kind == "bytes" else 0)


# LLM: 全局法律保留或非法策略不得保存游标；原游标字节与所有源文件保持。
# 函数用途: 验证预算开关不绕过安全停止。
@pytest.mark.parametrize("kind", ["legal_hold", "policy_error"])
def test_stopped_plan_does_not_save_cursor(tmp_path, kind):
    home, _ = _runtime(tmp_path)
    _, path = _thread(home)
    cursor = Path(home.owner_data_dir) / "retention_scan_cursor.json"
    write_private_json_file_atomic(cursor, {"kept": {"after": "original"}})
    before = cursor.read_bytes()
    policy = json.loads(home.owner_retention_json.read_text())
    policy.update({"legal_hold": True} if kind == "legal_hold" else {"conversation_days": "bad"})
    write_private_json_file_atomic(home.owner_retention_json, policy)
    report = apply_owner_retention(home, now=NOW, scan_budget=True)
    assert not report.applied and not report.scan_partial
    assert report.scan_cursor == {} and path.exists() and cursor.read_bytes() == before


# LLM: 相对路径全序跨目录一致；部分视图不能授权清理仍被先前片保留快照引用的清单。
# 函数用途: 验证跨目录分片不漏快照、不误删工具清单。
def test_snapshot_cursor_is_global_order_and_preserves_manifest_refs(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import retention_scan_budget as budgets

    home, _ = _runtime(tmp_path)
    root = Path(home.owner_home_dir) / "memory_archive/snapshots"
    snapshots = root / "context_bundles"
    paths = [snapshots / name for name in ("z.json", "a/one.json", "b/two.md", "b/three.json", "latest_context_bundle.json")]
    digest = "a" * 64
    for path in paths:
        write_private_json_file_atomic(path, {"tools": {"manifest_sha256": digest,
            "manifest_ref": f"memory_archive/snapshots/tool_manifests/{digest}.json"}})
        os.utime(path, (1, 1))
    manifest = root / "tool_manifests" / f"{digest}.json"
    write_private_json_file_atomic(manifest, {"tools": []})
    os.utime(manifest, (1, 1))
    monkeypatch.setattr(budgets.RetentionScanBudget, "default", classmethod(lambda cls: cls(2, 1000000, float("inf"))))
    retired = set()
    partials = []
    for _ in range(10):
        report = apply_owner_retention(home, now=NOW, scan_budget=True)
        retired.update(action.path for action in report.actions if action.category == "context_bundle_snapshot" and action.status == "trashed")
        assert not any(action.path == manifest for action in report.actions)
        assert manifest.exists() and paths[-1].exists()
        partials.append(report.scan_partial)
        if not report.scan_partial:
            break
    assert any(partials) and not report.scan_partial
    assert retired == set(paths[:-1]) and not any(path.exists() for path in paths[:-1])


# LLM: 子树隔离和错误合并不能丢失scan_partial/scan_cursor，回执必须保留实际续点。
# 函数用途: 验证带路径级扫描错误的预算执行仍完整报告分片事实。
def test_isolated_errors_preserve_partial_fields(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import retention_scan_budget as budgets

    home, _ = _runtime(tmp_path)
    for index in range(5):
        _thread(home, f"thread-{index:04d}")
    state = Path(home.owner_tasks_dir) / "2020-01-01/broken/work/state.json"
    state.parent.mkdir(parents=True)
    state.write_text("{")
    monkeypatch.setattr(budgets.RetentionScanBudget, "default", classmethod(lambda cls: cls(2, 1000000, float("inf"))))
    report = apply_owner_retention(home, now=NOW, scan_budget=True)
    assert report.applied and report.errors and report.isolated_errors
    assert report.scan_partial and report.scan_cursor["conversation:conversations"]["after"]
    assert report.to_dict()["scan_partial"] and report.to_dict()["scan_cursor"] == report.scan_cursor


# LLM: pins仍属尚未预算化的旧扫描；它不能在第一次有界发现之前耗尽每片时钟，形成永久零进展。
# 函数用途: 用可控单调时钟反证全量引用扫描造成的预算饥饿，不放宽预算判断。
def test_budgeted_discovery_precedes_unbudgeted_pin_scan(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import retention_scan_budget as budgets

    home, _ = _runtime(tmp_path)
    _, path = _thread(home)
    clock = [0.0]
    monkeypatch.setattr(budgets.time, "monotonic", lambda: clock[0])
    original = scan.current_retention_pins

    # LLM: 只推进虚拟单调时钟，仍执行真实pin读取与资格门，不使用sleep或跳过工作。
    # 函数用途: 模拟原全量pin扫描花费超过25ms。
    def pins(*args, **kwargs):
        clock[0] += 1.0
        return original(*args, **kwargs)

    monkeypatch.setattr(scan, "current_retention_pins", pins)
    report = apply_owner_retention(home, now=NOW, scan_budget=True)
    assert not path.exists()
    assert any(action.path == path and action.status == "trashed" for action in report.actions)
    assert not report.scan_partial


# LLM: 单个权威比整片上限还大时，不能先完整解析再声称预算已到；原文件须留存并有明确诊断。
# 函数用途: 反证首个超大线程不能突破字节准入门。
def test_oversized_authority_is_preserved_without_parsing(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store.retention_scan_budget import RetentionScanBudget

    home, service = _runtime(tmp_path)
    _, path = _thread(home)
    original, parsed = scan.read_json_object_report, []

    # LLM: 只观察真实权威解析，其他读取仍交原实现。
    # 函数用途: 检测超限文件是否进入解析器。
    def counted(target, **kwargs):
        if kwargs.get("context") == "memory_retention.conversation_thread":
            parsed.append(target)
        return original(target, **kwargs)

    monkeypatch.setattr(scan, "read_json_object_report", counted)
    budget = RetentionScanBudget(128, 1, float("inf"))
    report = service.plan(now=NOW, scan_budget=budget)
    assert not parsed and not report.actions and path.exists()
    assert any(error.error_code == "MEMORY_RETENTION_SCAN_ENTRY_TOO_LARGE" for error in report.errors)
    assert budget.size <= budget.max_bytes


# LLM: 一个完整活跃域恰好用完entry预算时，下一个域不能每片都重新排在它后面。
# 函数用途: 反证跨会话根与快照域的永久饥饿。
def test_completed_domain_is_not_restarted_before_pending_domain(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import retention_scan_budget as budgets

    home, _ = _runtime(tmp_path)
    active = [_thread(home, f"thread-{index:04d}", active=("running",))[1] for index in range(2)]
    snapshot = Path(home.owner_home_dir) / "memory_archive/snapshots/context_bundles/2020-01-01/old.json"
    write_private_json_file_atomic(snapshot, {})
    os.utime(snapshot, (1, 1))
    monkeypatch.setattr(budgets.RetentionScanBudget, "default", classmethod(lambda cls: cls(2, 1000000, float("inf"))))
    first = apply_owner_retention(home, now=NOW, scan_budget=True)
    assert first.scan_partial and snapshot.exists()
    for _ in range(4):
        report = apply_owner_retention(home, now=NOW, scan_budget=True)
        if not report.scan_partial:
            break
    assert not snapshot.exists() and not report.scan_partial
    assert all(path.exists() for path in active)


# LLM: 必须保留全序窗口以安全续读；不能重复慢枚举同一域并永远零解析，也不能跳过未读的小名字。
# 函数用途: 用确定性时钟验证名称发现耗尽当前片后下一片仍能推进。
def test_slow_name_enumeration_keeps_a_resumable_window(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import retention_scan_budget as budgets

    home, _ = _runtime(tmp_path)
    paths = [_thread(home, f"thread-{index:04d}")[1] for index in range(3)]
    clock = [0.0]
    monkeypatch.setattr(budgets.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(budgets.RetentionScanBudget, "default", classmethod(lambda cls: cls(2, 1000000, clock[0] + .025)))
    original = scan._scan_entry_names

    # LLM: 实际枚举仍完整运行，只给每个名字增加可控观察成本，保留真实乱序及磁盘内容。
    # 函数用途: 模拟不可抢占的目录枚举跨过本片deadline。
    def slow(root, **kwargs):
        for name in original(root, **kwargs):
            clock[0] += 1.0
            yield name

    monkeypatch.setattr(scan, "_scan_entry_names", slow)
    first = apply_owner_retention(home, now=NOW, scan_budget=True)
    assert first.scan_partial and all(path.exists() for path in paths)
    for _ in range(6):
        report = apply_owner_retention(home, now=NOW, scan_budget=True)
        if not report.scan_partial:
            break
    assert not report.scan_partial and not any(path.exists() for path in paths)


# LLM: 快路只接受当前所有者内的可读投影；链接或解析器递归失败都不能成为过期证明。
# 函数用途: 反证投影缺陷不能授予快速退休资格。
@pytest.mark.parametrize("kind", ["linked", "read_error", "deep_json"])
def test_untrusted_projection_is_not_settled(tmp_path, monkeypatch, kind):
    import sys

    home, _ = _runtime(tmp_path)
    path = _projection(home)
    if kind == "linked":
        external = tmp_path / "projection-outside"
        path.parent.rename(external)
        path.parent.symlink_to(external, target_is_directory=True)
    elif kind == "deep_json":
        depth = max(sys.getrecursionlimit() * 2, 20000)
        path.write_text("[" * depth + "0" + "]" * depth)
    else:
        # LLM: 模拟原读取器在exists阶段抛出的I/O错误，不改变解析或状态判定规则。
        # 函数用途: 覆盖报告器未捕获的入口级读取失败。
        def unavailable(*args, **kwargs):
            raise OSError("simulated projection lookup failure")

        monkeypatch.setattr(scan, "read_json_object_report", unavailable)
    assert not scan._subagent_run_settled(home, "subagent-abc", (NOW - timedelta(days=3)).timestamp())


# LLM: 永久错误域必须保持不完整并重试，但不能让健康域在完成一轮后永远不再扫描。
# 函数用途: 反证坏快照根不能饿死后来新增的过期会话。
def test_failed_domain_does_not_block_new_healthy_cycles(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import retention_scan_budget as budgets

    home, _ = _runtime(tmp_path)
    _, first = _thread(home, "thread-first")
    bad = Path(home.owner_home_dir) / "memory_archive/snapshots/context_bundles"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("not a directory")
    monkeypatch.setattr(budgets.RetentionScanBudget, "default", classmethod(lambda cls: cls(128, 1000000, float("inf"))))
    first_report = apply_owner_retention(home, now=NOW, scan_budget=True)
    assert not first.exists() and first_report.scan_partial and first_report.errors
    _, second = _thread(home, "thread-second")
    for _ in range(3):
        report = apply_owner_retention(home, now=NOW, scan_budget=True)
        if not second.exists():
            break
    assert not second.exists()
    assert report.scan_partial and report.errors and bad.exists()


# LLM: 局部无法枚举时保留未知分支并报告，其他可访问快照仍应进入真实回收；不得从部分引用推断孤儿清单。
# 函数用途: 反证一次walk错误不能反复丢弃已发现的健康文件。
def test_unreadable_snapshot_branch_preserves_other_progress(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import retention_scan_budget as budgets

    home, _ = _runtime(tmp_path)
    root = Path(home.owner_home_dir) / "memory_archive/snapshots"
    snapshots = root / "context_bundles"
    good = snapshots / "z-good.json"
    blocked = snapshots / "a-blocked"
    unknown = blocked / "kept.json"
    digest = "b" * 64
    write_private_json_file_atomic(good, {})
    write_private_json_file_atomic(unknown, {"tools": {"manifest_sha256": digest, "manifest_ref": "kept"}})
    manifest = root / "tool_manifests" / f"{digest}.json"
    write_private_json_file_atomic(manifest, {})
    for path in (good, unknown, manifest):
        os.utime(path, (1, 1))
    monkeypatch.setattr(budgets.RetentionScanBudget, "default", classmethod(lambda cls: cls(128, 1000000, float("inf"))))
    original = os.scandir

    # LLM: 只让目标子目录真实scandir报错，保留其他枚举、hash和执行入口。
    # 函数用途: 模拟单个快照分支的读取故障。
    def denied(path):
        if not isinstance(path, int) and Path(path) == blocked:
            raise PermissionError(13, "simulated snapshot listing failure", str(blocked))
        return original(path)

    monkeypatch.setattr(scan.os, "scandir", denied)
    report = apply_owner_retention(home, now=NOW, scan_budget=True)
    assert not good.exists()
    assert report.scan_partial and report.errors
    assert unknown.exists() and manifest.exists()
    assert not any(action.category == "tool_manifest" for action in report.actions)


# LLM: 用真实公共writer生成canonical JSONL及合法SQLite，回收后仍能校验库；不是用占位文本冒充用量库。
# 函数用途: 验证真实模型账本、索引及原锁侧车的退休链路。
def test_real_usage_writer_ledger_and_sqlite_survive_retirement(tmp_path):
    import sqlite3

    home, service = _runtime(tmp_path)
    thread, thread_path = _thread(home)
    root = thread_path.parent.parent
    store = ConversationStore(root)
    event = store.model_usage.append_once({"event_id": "usage-retire", "thread_id": thread.thread_id,
        "request_id": "request-one", "run_id": "run-one", "task_id": "task-one", "source": "main_agent",
        "model_calls": {"schema": "model_call_summary.v1", "physical_model_attempt_count": 1,
                        "accounted_input_tokens": 5, "output_tokens": 2,
                        "total_tokens": 7}, "now": 1.0})
    usage = root / "model_usage"
    ledger = usage / f"{thread.thread_id}.jsonl"
    database = usage / f".{thread.thread_id}-usage.sqlite3"
    assert ledger.is_file() and database.is_file()
    assert json.loads(ledger.read_text().splitlines()[0])["event_id"] == event.event_id
    sidecars = {path: path.read_bytes() for path in usage.iterdir() if path.is_file()
                and path not in (ledger, database) and not path.name.endswith(("-wal", "-shm", "-journal"))}
    assert sidecars
    before = {path.relative_to(root): path.read_bytes() for path in (ledger, database)}
    report = service.apply(now=NOW)
    moved = next(action for action in report.actions if action.record_id == thread.thread_id)
    assert moved.status == "trashed" and not ledger.exists() and not database.exists()
    assert {relative: (moved.destination / "payload" / relative).read_bytes() for relative in before} == before
    assert {path: path.read_bytes() for path in sidecars} == sidecars
    retired_database = moved.destination / "payload" / database.relative_to(root)
    connection = sqlite3.connect(retired_database.as_uri() + "?mode=ro", uri=True)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        connection.close()


# LLM: usage目录或叶的链接都不能让未知文件混入退休清单；整条会话必须保留并有typed诊断。
# 函数用途: 验证用量家族的链接拒绝与外部文件不变。
@pytest.mark.parametrize("kind", ["parent", "leaf"])
def test_linked_usage_keeps_the_whole_thread(tmp_path, kind):
    home, service = _runtime(tmp_path)
    thread, path = _thread(home)
    usage = path.parent.parent / "model_usage"
    outside = tmp_path / "outside-usage"
    outside.mkdir()
    original = outside / f"{thread.thread_id}.jsonl"
    original.write_bytes(b"preserve foreign usage\n")
    if kind == "parent":
        usage.symlink_to(outside, target_is_directory=True)
    else:
        usage.mkdir()
        (usage / original.name).symlink_to(original)
    report = service.apply(now=NOW)
    assert path.exists() and original.read_bytes() == b"preserve foreign usage\n"
    assert any(error.error_code == "MEMORY_RETENTION_CONVERSATION_USAGE_UNREADABLE" for error in report.errors)
    assert not any(action.record_id == thread.thread_id for action in report.actions)


# LLM: 完整但递归过深的cursor也是非法输入；只读加载必须安全从空续点重扫，不抛错或改原件。
# 函数用途: 验证游标报告器之外的递归错误被本领域关闭。
def test_deep_cursor_is_read_only_empty_fallback(tmp_path):
    import sys

    from agent_py_agent.agent.memory_store.retention_scan_budget import load_scan_cursor

    home, _ = _runtime(tmp_path)
    path = Path(home.owner_data_dir) / "retention_scan_cursor.json"
    depth = max(sys.getrecursionlimit() * 2, 20000)
    original = b"[" * depth + b"0" + b"]" * depth
    path.write_bytes(original)
    assert load_scan_cursor(home) == {} and path.read_bytes() == original
