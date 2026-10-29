# LLM: 清理测试只在tmp_path建owner、工作区和真实runtime库；不接触生产数据。
# 模块用途: 验证孤儿保护、七日瘦身、三十日耐久归档及失败不删原件。
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest


# LLM: 所有文件mtime按显式时钟设置，避免sleep和当前机器时区影响边界。
# 函数用途: 创建独立投影夹具，可选缺失工作区和中间文件。
def _projection(tmp_path, *, status="DONE", days=8, dangling=False, scratch=True, run_id="child"):
    home = tmp_path / "home"
    run = home / "agents" / run_id
    run.mkdir(parents=True)
    now = time.time()
    workspace = tmp_path / f"workspace-{run_id}"
    if not dangling:
        workspace.mkdir()
    payload = {"run_id": run_id, "parent_id": "parent", "root_id": "root", "task_id": "task",
               "status": status, "created_at": now - 40 * 86400, "updated_at": now - days * 86400,
               "result_summary": "完成摘要", "error_summary": "", "usage_summary": {"tokens": 42},
               "deliverable_paths": [str(workspace / "output.md")]}
    (run / "state.json").write_text(json.dumps(payload), encoding="utf-8")
    (run / "workspace_locator.json").write_text(json.dumps({"workspace_root": str(workspace)}))
    if scratch:
        (run / "events.jsonl").write_bytes(b'{"event":1}\n')
        (run / "snapshots").mkdir()
        (run / "snapshots" / "old.json").write_bytes(b"{}")
        (run / "tmp").mkdir()
        (run / "tmp" / "scratch").write_bytes(b"scratch")
    for path in sorted(run.rglob("*"), reverse=True) + [run]:
        os.utime(path, (now - days * 86400, now - days * 86400))
    return home, run, now, payload


# LLM: 延迟导入并断言调用入口存在，使模块尚未实现时在断言处清晰失败。
# 函数用途: 获取被测服务而不把ImportError当作有效红灯。
def _service():
    import importlib.util
    name = "agent_py_agent.agent.subagents.services.persistence.locator_hygiene"
    assert importlib.util.find_spec(name) is not None, "缺少孤儿投影维护服务"
    return __import__(name, fromlist=["classify_agent_projection_dir"])


# LLM: 缺失工作区、非终态以及七日/三十日边界共同决定结构化分类。
# 函数用途: 检查五类基础结果和一小时安全宽限。
@pytest.mark.parametrize("status,days,dangling,expected", [
    ("PLANNING", 2, True, "dangling_orphan"), ("PLANNING", .01, True, "active_or_protected"),
    ("RUNNING", 40, False, "active_or_protected"), ("DONE", 6, False, "active_or_protected"),
    ("DONE", 7, False, "terminal_slim_candidate"), ("FAILED", 30, False, "terminal_archive_candidate"),
])
def test_classification(tmp_path, status, days, dangling, expected):
    home, run, now, _ = _projection(tmp_path, status=status, days=days, dangling=dangling)
    assert _service().classify_agent_projection_dir(home, run, now=now)["classification"] == expected


# LLM: 活跃ID、固定保留、未收口及未消费父唤醒都强于删除候选。
# 函数用途: 验证显式保护覆盖缺失工作区和超龄终态。
@pytest.mark.parametrize("protection", ["active", "pinned", "closeout", "wake"])
def test_protections(tmp_path, protection):
    home, run, now, payload = _projection(tmp_path, days=40, dangling=True)
    options = {}
    if protection in {"active", "pinned"}:
        options[f"{protection}_run_ids"] = {"child"}
    else:
        payload["runtime_closeout_pending" if protection == "closeout" else "parent_wake_pending"] = True
        (run / "state.json").write_text(json.dumps(payload))
        os.utime(run / "state.json", (now - 40 * 86400, now - 40 * 86400))
    report = _service().maintain_owner_agent_projections(home, now=now, **options)
    assert run.exists() and report["removed_dirs"] == 0


# LLM: 删除统计必须与实际文件字节吻合；dry_run不得创建锁、归档或compact。
# 函数用途: 验证孤儿删除精确计量和纯只读预演。
def test_orphan_counts_and_dry_run(tmp_path):
    home, run, now, _ = _projection(tmp_path, status="PLANNING", dangling=True)
    before = {p.relative_to(home): p.read_bytes() for p in home.rglob("*") if p.is_file()}
    service = _service()
    preview = service.maintain_owner_agent_projections(home, now=now, dry_run=True)
    assert preview["removed_dirs"] == 0 and preview["would_remove_dirs"] == 1
    assert {p.relative_to(home): p.read_bytes() for p in home.rglob("*") if p.is_file()} == before
    report = service.maintain_owner_agent_projections(home, now=now)
    assert not run.exists()
    assert report["removed_dirs"] == 1 and report["removed_files"] == len(before)
    assert report["removed_bytes"] == sum(map(len, before.values()))


# LLM: compact必须保存原结构化结果；唯一报告与未知文件保留，绝不把它们当scratch。
# 函数用途: 验证七日瘦身后元数据完整且可幂等分类。
def test_slim_preserves_result_and_unique_report(tmp_path):
    home, run, now, payload = _projection(tmp_path)
    (run / "final_report.md").write_text("唯一报告")
    os.utime(run / "final_report.md", (now - 8 * 86400, now - 8 * 86400))
    service = _service()
    report = service.maintain_owner_agent_projections(home, now=now)
    compact = json.loads((run / "subagent_compact.json").read_text())
    assert report["slimmed_dirs"] == 1 and not (run / "events.jsonl").exists()
    assert not (run / "snapshots").exists() and not (run / "tmp").exists()
    assert (run / "state.json").exists() and (run / "workspace_locator.json").exists()
    assert (run / "final_report.md").read_text() == "唯一报告"
    assert compact["schema"] == "subagent_compact.v1"
    assert compact["parent_run_id"] == "parent" and compact["root_run_id"] == "root"
    for key in ("run_id", "task_id", "status", "created_at", "updated_at", "result_summary",
                "deliverable_paths", "error_summary", "usage_summary"):
        assert compact[key] == payload[key]
    assert service.maintain_owner_agent_projections(home, now=now)["slimmed_dirs"] == 0


# LLM: 三十日归档必须先fsync并读回；重放已有run_id不得重复追加，坏档案不得删原目录。
# 函数用途: 验证耐久归档、去重重放与错误保留。
@pytest.mark.parametrize("archive_state", ["new", "duplicate", "corrupt"])
def test_archive_readback_deduplicates_and_preserves_on_corruption(tmp_path, archive_state):
    home, run, now, payload = _projection(tmp_path, days=31)
    service = _service()
    archive = home / "agents" / (time.strftime("archived_subagents_%Y_%m.jsonl", time.gmtime(now)))
    if archive_state == "corrupt":
        archive.write_text("{partial")
    elif archive_state == "duplicate":
        service.maintain_owner_agent_projections(home, now=now)
        assert not run.exists()
        run.mkdir()
        (run / "state.json").write_text(json.dumps(payload))
        os.utime(run / "state.json", (now - 31 * 86400, now - 31 * 86400))
    report = service.maintain_owner_agent_projections(home, now=now)
    if archive_state == "corrupt":
        assert run.exists() and report["errors"]
    else:
        assert not run.exists() and report["archived_dirs"] == 1
        entries = [json.loads(line) for line in archive.read_text().splitlines()]
        assert len(entries) == 1 and entries[0]["run_id"] == "child"


# LLM: runtime是活跃事实权威，缺失工作区绝不能覆盖它；SQLite只读查询不初始化新库。
# 函数用途: 验证真实运行记录保护和坏库保守行为。
@pytest.mark.parametrize("broken", [False, True])
def test_runtime_active_or_unreadable_is_protected(tmp_path, broken):
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    home, run, now, _ = _projection(tmp_path, dangling=True, days=40)
    if broken:
        (home / "runtime.db").write_bytes(b"not sqlite")
    else:
        RuntimeRepository(home / "runtime.db").record_run_creation(owner_id="local/main", goal="g", run_id="child")
    assert _service().classify_agent_projection_dir(home, run, now=now)["classification"] == "active_or_protected"
    assert _service().maintain_owner_agent_projections(home, now=now)["removed_dirs"] == 0
    assert run.exists()


# LLM: 目录链接不能扩展清理范围，唯一交付物和未知文件都不允许随归档删除。
# 函数用途: 检查软链接逃逸、唯一报告和错误路径均保留。
@pytest.mark.parametrize("kind", ["run_symlink", "scratch_symlink", "unique_report", "unknown_file", "unknown_lock"])
def test_unsafe_or_unique_files_are_not_deleted(tmp_path, kind):
    home, run, now, _ = _projection(tmp_path, days=40)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "important").write_text("keep")
    if kind == "run_symlink":
        (run.parent / "linked").symlink_to(outside, target_is_directory=True)
    elif kind == "scratch_symlink":
        (run / "tmp/link").symlink_to(outside, target_is_directory=True)
    else:
        path = run / {"unique_report": "final_report.md", "unknown_file": "unknown.data", "unknown_lock": "poetry.lock"}[kind]
        path.write_text("keep")
        os.utime(path, (now - 40 * 86400, now - 40 * 86400))
    _service().maintain_owner_agent_projections(home, now=now)
    assert (outside / "important").read_text() == "keep"
    if kind != "run_symlink":
        assert run.exists()


# LLM: 同run旧归档不同内容不能充当当前摘要已保存的证明。
# 函数用途: 验证去重冲突保留原件与新摘要。
def test_archive_conflict_preserves_new_projection(tmp_path):
    home, run, now, _ = _projection(tmp_path, days=31)
    archive = home / "agents" / time.strftime("archived_subagents_%Y_%m.jsonl", time.gmtime(now))
    archive.write_text(json.dumps({"schema": "subagent_compact.v1", "run_id": "child", "result_summary": "旧结果"}) + "\n")
    report = _service().maintain_owner_agent_projections(home, now=now)
    assert run.exists() and (run / "state.json").exists() and report["errors"]


# LLM: compact写入与删scratch之间恢复为活跃必须中止，不将新库存当旧授权。
# 函数用途: 精确复现瘦身写入时出现RUNNING的新状态。
def test_slim_rechecks_original_inventory_after_compact_write(tmp_path, monkeypatch):
    home, run, now, payload = _projection(tmp_path)
    service = _service()
    original = service.write_private_json_file_atomic
    # LLM: 调用真实原子写者后模拟正常恢复，不替代被测分类或删除。
    # 函数用途: 将竞争固定在compact发表后、scratch删除前。
    def revive(path, value):
        original(path, value)
        payload["status"] = "RUNNING"
        (run / "state.json").write_text(json.dumps(payload))
    monkeypatch.setattr(service, "write_private_json_file_atomic", revive)
    report = service.maintain_owner_agent_projections(home, now=now)
    assert (run / "events.jsonl").exists() and report["slimmed_dirs"] == 0 and report["errors"]


# LLM: fsync失败或读回错误均不得删除投影；注入只针对归档写入阶段。
# 函数用途: 验证归档耐久失败保留state和中间文件。
def test_archive_fsync_failure_keeps_original(tmp_path, monkeypatch):
    home, run, now, _ = _projection(tmp_path, days=31)
    service = _service()
    # LLM: 在明确的耐久边界报错，不模拟成功返回。
    # 函数用途: 模拟磁盘同步失败。
    def fail(_fd):
        raise OSError("disk sync failed")
    monkeypatch.setattr(service.os, "fsync", fail)
    report = service.maintain_owner_agent_projections(home, now=now)
    assert (run / "state.json").exists() and (run / "events.jsonl").exists() and report["errors"]


# LLM: 状态中的结构化引用比scratch文件名优先，目录内唯一交付物永不能删除。
# 函数用途: 验证tmp内交付物在七日和三十日维护均保留。
@pytest.mark.parametrize("days", [8, 40])
def test_deliverable_inside_scratch_is_preserved(tmp_path, days):
    home, run, now, payload = _projection(tmp_path, days=days)
    payload["deliverable_paths"] = [str(run / "tmp/scratch")]
    (run / "state.json").write_text(json.dumps(payload))
    os.utime(run / "state.json", (now - days * 86400, now - days * 86400))
    _service().maintain_owner_agent_projections(home, now=now)
    assert (run / "tmp/scratch").read_bytes() == b"scratch"


# LLM: 会话磁盘未读唤醒独立于state中的通知标志，不能仅看投影就清理。
# 函数用途: 验证真实wake文件的source.run_id保护。
def test_unread_parent_wake_file_protects_projection(tmp_path):
    home, run, now, _ = _projection(tmp_path, days=40)
    queue = home / "conversations/wake_queue/normal"
    queue.mkdir(parents=True)
    (queue / "wake.json").write_text(json.dumps({"status": "pending", "source": {"run_id": "child"}}))
    assert _service().classify_agent_projection_dir(home, run, now=now)["classification"] == "active_or_protected"
    _service().maintain_owner_agent_projections(home, now=now)
    assert run.exists()


# LLM: 自定义保留期和开关应实际生效，max_dirs限制扫描而不是只限制删除计数。
# 函数用途: 验证显式维护策略不被默认值覆盖。
def test_custom_retention_orphan_switch_and_scan_budget(tmp_path):
    home, run, now, _ = _projection(tmp_path, days=8)
    service = _service()
    assert service.maintain_owner_agent_projections(home, now=now, slim_after_days=10)["slimmed_dirs"] == 0
    assert service.maintain_owner_agent_projections(home, now=now, slim_after_days=1, archive_after_days=2)["archived_dirs"] == 1
    assert not run.exists()
    home, run, now, _ = _projection(tmp_path / "orphan", days=2, dangling=True, status="PLANNING")
    assert service.maintain_owner_agent_projections(home, now=now, remove_dangling_orphans=False)["removed_dirs"] == 0
    assert service.maintain_owner_agent_projections(home, now=now, max_dirs=0)["scanned_dirs"] == 0
    assert run.exists()


# LLM: JSONL旧末条缺少换行不能被追加拼坏；只接受拒绝且原字节不变。
# 函数用途: 复现共享归档尾分隔符缺失的写坏窗口。
def test_archive_without_final_newline_is_unchanged(tmp_path):
    home, run, now, _ = _projection(tmp_path, days=31)
    archive = home / "agents" / time.strftime("archived_subagents_%Y_%m.jsonl", time.gmtime(now))
    before = json.dumps({"schema": "subagent_compact.v1", "run_id": "other"})
    archive.write_text(before)
    report = _service().maintain_owner_agent_projections(home, now=now)
    assert run.exists() and archive.read_text() == before and report["errors"]


# LLM: 两次分类后DB恢复或唤醒插入也必须与删除互斥，而不是只保护投影文件。
# 函数用途: 真实SQLite第二连接验证归档临界区持有写者保留锁。
def test_runtime_writer_cannot_commit_during_archive(tmp_path, monkeypatch):
    import sqlite3

    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    home, run, now, _ = _projection(tmp_path, days=31)
    repo = RuntimeRepository(home / "runtime.db")
    created = repo.record_run_creation(owner_id="local/main", goal="g", run_id="child")
    repo.settle_agent_run(agent_run_id=created["agent_run_id"], status="done", attempt_id=created["attempt_id"])
    service = _service()
    original = service._archive_entry
    attempted = []
    # LLM: 第二连接只尝试原runtime更新；锁忙是并发裁决，不伪造查询结果。
    # 函数用途: 在归档写入后、删除前精确插入恢复尝试。
    def try_reactivate(path, compact):
        original(path, compact)
        with sqlite3.connect(home / "runtime.db", timeout=0.01) as conn:
            try:
                conn.execute("UPDATE agent_runs SET status='running' WHERE run_id='child'")
                conn.commit()
            except sqlite3.OperationalError:
                attempted.append("blocked")
            else:
                attempted.append("committed")
    monkeypatch.setattr(service, "_archive_entry", try_reactivate)
    service.maintain_owner_agent_projections(home, now=now)
    assert attempted == ["blocked"] and not run.exists()


# LLM: refs采用生产final_report字段，引用在scratch内时与state直接声明同等保护。
# 函数用途: 证伪独立静态审查提出的refs唯一产物被瘦身删除风险。
def test_refs_deliverable_inside_scratch_is_preserved(tmp_path):
    home, run, now, _ = _projection(tmp_path)
    refs = run / "refs.json"
    refs.write_text(json.dumps({"final_report": str(run / "tmp/scratch")}))
    os.utime(refs, (now - 8 * 86400, now - 8 * 86400))
    report = _service().maintain_owner_agent_projections(home, now=now)
    assert (run / "tmp/scratch").read_bytes() == b"scratch" and report["slimmed_dirs"] == 1


# LLM: 文件唤醒不受SQLite保留锁约束，归档后必须重查其真实结构，晚到责任不应删投影。
# 函数用途: 在写归档之后确定性插入生产形态source_agent_id的父级唤醒。
def test_late_parent_wake_after_archive_keeps_projection(tmp_path, monkeypatch):
    home, run, now, _ = _projection(tmp_path, days=31)
    service = _service()
    original = service._archive_entry
    # LLM: 真实归档完成后创建独立文件唤醒，不修改state，从而校验保护并非仅来自库存变化。
    # 函数用途: 精确设置父通知到达的竞态观察点。
    def append_wake(path, compact):
        original(path, compact)
        queue = home / "conversations/wake_queue/normal"
        queue.mkdir(parents=True)
        (queue / "late.json").write_text(json.dumps({"source_agent_id": "child", "root_task_id": "task"}))
    monkeypatch.setattr(service, "_archive_entry", append_wake)
    report = service.maintain_owner_agent_projections(home, now=now)
    assert run.exists() and report["errors"] and report["archived_dirs"] == 0


# LLM: 并发事件放在末次验证之后的真实删除入口，不再沿用过早的归档后注入点。
# 函数用途: 复现首次runtime登记和真实父唤醒发布抢在删叶前的竞态。
@pytest.mark.parametrize("event", ["runtime", "wake"])
def test_r1_late_registration_or_publication_preserves_projection(tmp_path, monkeypatch, event):
    from agent_py_agent.agent.conversation.models import WakeSignal
    from agent_py_agent.agent.conversation.store_layout import ConversationStorage
    from agent_py_agent.agent.conversation.store_wake_publication import publish_deduped
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

    home, run, now, _ = _projection(tmp_path, days=31)
    assert not (home / "runtime.db").exists()
    storage = ConversationStorage(home / "conversations")
    service, fired = _service(), []
    original = service._remove_records
    # LLM: 调真实发布/登记；锁忙须中止本次维护，成功写入则必须由末次保护识别。
    # 函数用途: 将业务写者固定在删除函数入口。
    def interleave(*args, **kwargs):
        fired.append(True)
        if event == "runtime":
            db = home / "runtime.db"
            repo = RuntimeRepository(db, existing_only=db.exists())
            repo.record_run_creation(owner_id="local/main", goal="late", run_id="child")
        else:
            publish_deduped(storage, WakeSignal.from_dict({"wake_signal_id": "wake-race", "thread_id": "thread-race",
                "source_agent_id": "child", "root_task_id": "task", "dedupe_key": "late"}), blocking=False)
        return original(*args, **kwargs)
    monkeypatch.setattr(service, "_remove_records", interleave)
    report = service.maintain_owner_agent_projections(home, now=now)
    assert fired and (run / "state.json").exists()
    assert report["removed_dirs"] == 0


# LLM: 路径中的agents在最终验证后可被替换，最终run叶NOFOLLOW不能保护祖先。
# 函数用途: 确定性复现父目录链接置换后的域外删叶。
def test_r1_swapped_agents_parent_cannot_delete_outside(tmp_path, monkeypatch):
    home, run, now, _ = _projection(tmp_path, dangling=True, status="PLANNING", days=2)
    service, original = _service(), _service()._remove_records
    moved = tmp_path / "outside-agents"
    # LLM: 仅改变真实路径关系，不伪造inode或清单；同一旧inode被移到owner外。
    # 函数用途: 在删除入口换成祖先软链接。
    def swap(*args, **kwargs):
        (home / "agents").rename(moved)
        (home / "agents").symlink_to(moved, target_is_directory=True)
        return original(*args, **kwargs)
    monkeypatch.setattr(service, "_remove_records", swap)
    report = service.maintain_owner_agent_projections(home, now=now)
    assert (moved / "child/state.json").exists()
    assert report["errors"] and report["removed_dirs"] == 0


# LLM: 同时在旧Path.open与新os.open的真实追加缝隙注入；禁止靠绕过触发点让回归虚绿。
# 函数用途: 复现归档叶预检后被链接替换时的域外追加。
def test_r1_archive_leaf_swap_never_appends_outside(tmp_path, monkeypatch):
    home, run, now, _ = _projection(tmp_path, days=31)
    service = _service()
    archive = home / "agents" / time.strftime("archived_subagents_%Y_%m.jsonl", time.gmtime(now))
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(b"")
    real_path_open, real_os_open, fired = Path.open, os.open, []
    # LLM: 置换在实际打开前发生；受管NOFOLLOW必须拒绝而不是沿链接写外部文件。
    # 函数用途: 安装一次恶意归档叶链接。
    def replace_leaf():
        if not fired:
            fired.append(True)
            if archive.exists():
                archive.unlink()
            archive.symlink_to(outside)
    # LLM: 老实现的路径追加也要触发相同窗口，保证红灯有效。
    # 函数用途: 截获Path追加打开。
    def path_open(path, mode="r", *args, **kwargs):
        if path == archive and "a" in mode:
            replace_leaf()
        return real_path_open(path, mode, *args, **kwargs)
    # LLM: 新实现以dir_fd和O_APPEND打开同一叶，注入后仍交真实系统调用裁决。
    # 函数用途: 截获受管FD追加打开。
    def fd_open(path, flags, *args, **kwargs):
        if str(path) in {str(archive), archive.name} and flags & os.O_APPEND:
            replace_leaf()
        return real_os_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(Path, "open", path_open)
    monkeypatch.setattr(os, "open", fd_open)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {fd_open})
    report = service.maintain_owner_agent_projections(home, now=now)
    assert fired and outside.read_bytes() == b""
    assert (run / "state.json").exists() and report["errors"]


# LLM: refs的..不能被词法首段误认成tmp，含歧义段时整份投影须保留。
# 函数用途: 验证scratch中经父路径定位的唯一报告不会被七日瘦身删除。
@pytest.mark.parametrize("absolute", [False, True])
def test_r1_dotdot_deliverable_protects_entire_run(tmp_path, absolute):
    home, run, now, payload = _projection(tmp_path)
    report = run / "snapshots/report.txt"
    report.write_text("唯一报告")
    ref = Path("tmp/../snapshots/report.txt")
    payload["deliverable_paths"] = [str(run / ref if absolute else ref)]
    (run / "state.json").write_text(json.dumps(payload))
    for path in [*run.rglob("*"), run]:
        os.utime(path, (now - 8 * 86400, now - 8 * 86400))
    result = _service().maintain_owner_agent_projections(home, now=now)
    assert report.exists() and report.read_text() == "唯一报告"
    assert (run / "events.jsonl").exists() and result["slimmed_dirs"] == 0


# LLM: 非..形式也可能不能唯一定位交付子项，目录自身与缺失相对引用都不得当作清理许可。
# 函数用途: 验证歧义相对引用保守保护整个投影。
@pytest.mark.parametrize("ref", [".", "snapshots/missing.txt"])
def test_r1_ambiguous_relative_ref_preserves_all_scratch(tmp_path, ref):
    home, run, now, payload = _projection(tmp_path)
    payload["deliverable_paths"] = [ref]
    (run / "state.json").write_text(json.dumps(payload))
    for path in [*run.rglob("*"), run]:
        os.utime(path, (now - 8 * 86400, now - 8 * 86400))
    result = _service().maintain_owner_agent_projections(home, now=now)
    assert (run / "events.jsonl").exists() and (run / "tmp/scratch").exists()
    assert result["slimmed_dirs"] == 0


# LLM: 外部引用别名实际指向同一run时也要保护；解析只增加保留范围，不能扩展删除根。
# 函数用途: 验证规范化实际交付位置而不是仅做路径字符串前缀比较。
def test_r1_deliverable_alias_resolves_to_internal_report(tmp_path):
    home, run, now, payload = _projection(tmp_path)
    (run / "snapshots/report.txt").write_text("唯一报告")
    alias = tmp_path / "report-alias"
    alias.symlink_to(run, target_is_directory=True)
    payload["deliverable_paths"] = [str(alias / "snapshots/report.txt")]
    (run / "state.json").write_text(json.dumps(payload))
    for path in [*run.rglob("*"), run]:
        os.utime(path, (now - 8 * 86400, now - 8 * 86400))
    _service().maintain_owner_agent_projections(home, now=now)
    assert (run / "snapshots/report.txt").exists()


# LLM: 末次业务重查之后的真实发布仍须被共享临界区拦住；仅多读一次队列不能保证原子。
# 函数用途: 在真实首个删叶前发布父唤醒，要求一份原投影都不能被部分删除。
def test_r1_publication_at_unlink_cannot_partially_delete_projection(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.models import WakeSignal
    from agent_py_agent.agent.conversation.store_layout import ConversationStorage
    from agent_py_agent.agent.conversation.store_wake_publication import publish_deduped

    home, run, now, _ = _projection(tmp_path, days=31, scratch=False)
    before = {p.name: p.read_bytes() for p in run.iterdir()}
    storage, fired, original = ConversationStorage(home / "conversations"), [], os.unlink
    # LLM: 委托真实发布与unlink，非阻塞只避免测试自等待；锁忙必须向维护方传播并保原件。
    # 函数用途: 将发布固定在最终检查与删叶系统调用之间。
    def unlink(path, *args, **kwargs):
        if str(path) == "workspace_locator.json" and not fired:
            fired.append(True)
            publish_deduped(storage, WakeSignal.from_dict({"wake_signal_id": "wake-last-leaf", "thread_id": "thread-race",
                "source_agent_id": "child", "root_task_id": "task", "dedupe_key": "last-leaf"}), blocking=False)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {unlink})
    result = _service().maintain_owner_agent_projections(home, now=now)
    assert fired
    assert {p.name: p.read_bytes() for p in run.iterdir()} == before
    assert result["removed_files"] == 0 and result["errors"]


# LLM: 根、旧data及任务工作区会话必须都覆盖同一owner维护锁；锁忙前不能写prepared或队列。
# 函数用途: 验证真实非阻塞发布跨进程也不能穿过维护临界区。
@pytest.mark.parametrize("prefix", ["", "data", "workspace/runtime/workspaces/task-one"])
def test_r1p2_publication_lock_covers_store_layouts_and_processes(tmp_path, prefix):
    import subprocess
    import sys

    from agent_py_agent.agent.conversation.store_layout import ConversationStorage

    home, run, _, _ = _projection(tmp_path)
    storage = ConversationStorage(home / prefix / "conversations")
    script = """import sys
sys.path.insert(0, sys.argv[2])
from agent_py_agent.agent.conversation.models import WakeSignal
from agent_py_agent.agent.conversation.store_layout import ConversationStorage
from agent_py_agent.agent.conversation.store_wake_publication import publish_deduped
s=ConversationStorage(sys.argv[1], initialize=False)
try:
    publish_deduped(s, WakeSignal.from_dict({'wake_signal_id':'wake-cross','thread_id':'thread-race','source_agent_id':'child','dedupe_key':'cross'}), blocking=False)
except BlockingIOError:
    print('blocked')
else:
    print('published')
"""
    with _service()._maintenance_guard(home, run):
        result = subprocess.run([sys.executable, "-B", "-c", script, str(storage.root), str(Path(__file__).resolve().parents[2])],
                                capture_output=True, text=True, timeout=10, check=False,
                                cwd=Path(__file__).resolve().parents[2])
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "blocked", result.stderr
    assert not list(storage.wake_dedupe_dir.glob("*.json"))
    assert not list((storage.wake_queue_dir / "normal").glob("*.json"))


# LLM: prepared失败重试同样先取维护互斥；旧半提交必须保护投影，释放锁后重放原身份。
# 函数用途: 验证失败重放不丢唤醒、不在维护期间补装且仍可恢复。
def test_r1p2_prepared_replay_is_protected_and_blocked(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import store_wake_publication as publication
    from agent_py_agent.agent.conversation.models import WakeSignal
    from agent_py_agent.agent.conversation.store_layout import ConversationStorage

    home, run, now, _ = _projection(tmp_path, days=31)
    storage = ConversationStorage(home / "conversations")
    signal = WakeSignal.from_dict({"wake_signal_id": "wake-prepared", "thread_id": "thread-race", "urgency": "normal",
                                 "source_agent_id": "child", "dedupe_key": "prepared"})
    original = publication.write_private_json_file_atomic
    # LLM: 只阻断第一次信号安装，prepared仍由真实发布写入，不伪造半提交。
    # 函数用途: 注入一次安装磁盘错误。
    def fail_install(*args, **kwargs):
        raise OSError("install failed")
    monkeypatch.setattr(publication, "write_private_json_file_atomic", fail_install)
    with pytest.raises(OSError):
        publication.publish_deduped(storage, signal)
    monkeypatch.setattr(publication, "write_private_json_file_atomic", original)
    receipt = storage.wake_dedupe_path(signal.thread_id, signal.dedupe_key)
    frozen = receipt.read_bytes()
    assert json.loads(frozen)["phase"] == "prepared"
    result = _service().maintain_owner_agent_projections(home, now=now)
    assert (run / "state.json").exists() and result["removed_dirs"] == 0
    with _service()._maintenance_guard(home, run):
        with pytest.raises(BlockingIOError):
            publication.publish_deduped(storage, signal, blocking=False)
        assert receipt.read_bytes() == frozen
    _, installed = publication.publish_deduped(storage, signal, blocking=False)
    assert installed.wake_signal_id == signal.wake_signal_id
    assert json.loads(receipt.read_text())["phase"] == "published"
    assert len(list((storage.wake_queue_dir / "normal").glob("*.json"))) == 1


# LLM: 默认与非阻塞发布在prepared到install阶段均必须持共享锁；真实维护EX不能进入。
# 函数用途: 反向验证发布临界区排斥维护并保留已有安装次序。
@pytest.mark.parametrize("blocking", [True, False])
def test_r1p2_publisher_holds_lock_through_install(tmp_path, monkeypatch, blocking):
    from agent_py_agent.agent.conversation import store_wake_publication as publication
    from agent_py_agent.agent.conversation.models import WakeSignal
    from agent_py_agent.agent.conversation.store_layout import ConversationStorage

    home, run, _, _ = _projection(tmp_path)
    storage = ConversationStorage(home / "conversations")
    original, attempts = publication._install, []
    # LLM: 维护尝试发生在真实prepared之后，原安装仍执行；不能只断言锁工具被调用。
    # 函数用途: 在安装入口争抢真实维护锁。
    def install(*args, **kwargs):
        try:
            with _service()._maintenance_guard(home, run):
                attempts.append("entered")
        except BlockingIOError:
            attempts.append("blocked")
        return original(*args, **kwargs)
    monkeypatch.setattr(publication, "_install", install)
    publication.publish_deduped(storage, WakeSignal.from_dict({"wake_signal_id": "wake-held",
        "thread_id": "thread-race", "source_agent_id": "child", "dedupe_key": "held"}), blocking=blocking)
    assert attempts == ["blocked"] and (run / "state.json").exists()


# LLM: 无覆盖必须由实际rename系统调用裁决；stat之后突然出现的空目录也不能被覆盖。
# 函数用途: 验证恢复冲突保留原隔离内容并提供结构化恢复位置。
def test_r1p2_restore_conflict_is_atomic_and_reported(tmp_path, monkeypatch):
    home, run, now, _ = _projection(tmp_path, days=31, scratch=False)
    service, fired = _service(), []
    real_rename = os.rename
    native = getattr(service, "_rename_noreplace", None)
    # LLM: 在恢复底层rename之前才占位，旧stat检查已经通过，不能提前把竞态变成普通存在分支。
    # 函数用途: 植入并发创建的空原目录。
    def occupy(src, dst, parent_fd):
        if str(src).startswith(".purge_") and str(dst) == "child" and not fired:
            os.mkdir("child", dir_fd=parent_fd)
            fired.append(True)
    # LLM: 旧实现的os.rename和新原子入口均注入相同实际时点，保持红绿触发有效。
    # 函数用途: 截获原普通重命名。
    def rename(src, dst, *args, **kwargs):
        occupy(src, dst, kwargs.get("dst_dir_fd"))
        return real_rename(src, dst, *args, **kwargs)
    # LLM: 新入口仍委托真实无覆盖系统调用，不能用mock假造EEXIST。
    # 函数用途: 截获无覆盖重命名的恢复目标。
    def no_replace(source_fd, source, target_fd, target):
        occupy(source, target, target_fd)
        return native(source_fd, source, target_fd, target)
    # LLM: 义务失败发生在隔离后、物理删除前，要求原件完整保存在隔离目录。
    # 函数用途: 强制进入恢复路径。
    def fail(_boundary):
        raise ValueError("force restore")
    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {rename})
    if native is not None:
        monkeypatch.setattr(service, "_rename_noreplace", no_replace)
    monkeypatch.setattr(service, "_assert_no_new_obligation", fail)
    result = service.maintain_owner_agent_projections(home, now=now)
    assert fired and run.is_dir() and not list(run.iterdir())
    recovery = result["errors"][0]["recovery"]
    assert recovery["status"] == "quarantined" and not recovery["partial_deletion"]
    assert any((Path(path) / "state.json").exists() for path in recovery["quarantine_paths"])
    assert result["removed_files"] == 0


# LLM: 整批所有待删根须先移出元数据投影并校验；不能一边检查原目录一边逐叶销毁。
# 函数用途: 在首次真实unlink观察全部隔离完成且元数据未进入scratch批次。
@pytest.mark.parametrize("days", [8, 31])
def test_r1p2_entire_batch_isolated_before_first_unlink(tmp_path, monkeypatch, days):
    home, run, now, _ = _projection(tmp_path, days=days)
    service, original, observed = _service(), os.unlink, []
    # LLM: 观察真实FD中的内容，保留真实unlink；不是只检查私有方法是否存在。
    # 函数用途: 核对物理删除前元数据根已无任何待删项。
    def unlink(path, *args, **kwargs):
        if not observed and kwargs.get("dir_fd") is not None:
            boundary = service._MAINTENANCE_BOUNDARY.get()
            remaining = {entry.name for entry in os.scandir(boundary["run"])}
            assert not (remaining & {"events.jsonl", "tmp", "snapshots"})
            if days == 31:
                assert not remaining
            else:
                assert {"state.json", "workspace_locator.json", "subagent_compact.json"} <= remaining
            observed.append(True)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {unlink})
    result = service.maintain_owner_agent_projections(home, now=now)
    assert observed and not result["errors"]


# LLM: 物理删除已经开始时不能谎称完整内容回滚；恢复名称后也必须明示部分完成。
# 函数用途: 验证中途I/O错误的实际删量及结构化恢复状态。
def test_r1p2_partial_io_failure_reports_recovery(tmp_path, monkeypatch):
    home, run, now, _ = _projection(tmp_path, days=31)
    original, deleted = os.unlink, []
    # LLM: 第一次真实删除成功，第二次真实删除边界失败，不能把错误包装成外层成功。
    # 函数用途: 注入可复现的部分删除。
    def unlink(path, *args, **kwargs):
        if deleted:
            raise OSError("second unlink failed")
        original(path, *args, **kwargs)
        deleted.append(str(path))
    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {unlink})
    result = _service().maintain_owner_agent_projections(home, now=now)
    assert deleted and result["removed_files"] == 1 and result["removed_dirs"] == 0
    assert (run / "state.json").exists()
    recovery = result["errors"][0]["recovery"]
    assert recovery["status"] == "partial" and recovery["partial_deletion"]
    assert recovery["quarantine_paths"] == []


# LLM: 入口拒绝错误schema、空ID和与隔离run不符的ID，必须在创建归档之前完成。
# 函数用途: 验证新条目不能制造下轮自拒绝的坏归档。
@pytest.mark.parametrize("compact", [
    {"schema": "wrong", "run_id": "child"},
    {"schema": "subagent_compact.v1", "run_id": ""},
    {"schema": "subagent_compact.v1", "run_id": "other"},
])
def test_r1p2_archive_validates_new_entry_before_write(tmp_path, compact):
    home, run, _, _ = _projection(tmp_path, days=31)
    service = _service()
    archive = home / "agents/archived_subagents_test.jsonl"
    with service._maintenance_guard(home, run):
        with pytest.raises(ValueError):
            service._archive_entry(archive, compact)
    assert not archive.exists() and (run / "state.json").exists()


# LLM: 批次mkdir之后且任何搬移之前的EMFILE必须保留原件并恢复run名称，不能让未初始化FD掩盖首因。
# 函数用途: 验证批次打开失败时只留下明示的空容器，不遗失原投影路径。
def test_r1p2_unopened_batch_restores_projection_and_reports_residue(tmp_path, monkeypatch):
    import errno

    home, run, now, _ = _projection(tmp_path, days=31)
    before = {path.relative_to(run): path.read_bytes() for path in run.rglob("*") if path.is_file()}
    original, fired = os.open, []
    # LLM: 只在本批次的首次真实open注入失败，其余父链打开仍由真实系统调用完成。
    # 函数用途: 在已创建但未固定FD的边界制造可恢复磁盘错误。
    def fail_open(path, flags, *args, **kwargs):
        if str(path).startswith(".purge_") and str(path).endswith(".files"):
            fired.append(True)
            raise OSError(errno.EMFILE, "injected batch open failure")
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", fail_open)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {fail_open})
    result = _service().maintain_owner_agent_projections(home, now=now)
    assert fired
    assert run.is_dir(), "尚未搬移任何内容时，原投影名称必须恢复"
    assert {path.relative_to(run): path.read_bytes() for path in run.rglob("*") if path.is_file()} == before
    assert result["removed_dirs"] == result["removed_files"] == 0
    recovery = result["errors"][0]["recovery"]
    assert recovery["primary_error_type"] == "OSError" and recovery["recovery_error_type"] is None
    assert recovery["status"] == "quarantined" and not recovery["partial_deletion"]
    residue = {Path(path) for path in recovery["quarantine_paths"]}
    assert residue == set((home / "agents").glob(".purge_*")) and len(residue) == 1
    assert all(path.name.endswith(".files") and path.is_dir() and not list(path.iterdir()) for path in residue)


# LLM: 公共候选必须有state，私有删除帮助函数也要显式拒绝空库存，不能把空目录删除后的异常记成已恢复。
# 函数用途: 验证空库存不构成隔离或删除授权，原目录与计量均保持不变。
def test_r1p2_empty_inventory_cannot_authorize_projection_removal(tmp_path):
    home, run, _, _ = _projection(tmp_path, scratch=False)
    for path in run.iterdir():
        path.unlink()
    service, rejected = _service(), False
    report = {"removed_dirs": 0, "removed_files": 0, "removed_bytes": 0}
    with service._maintenance_guard(home, run) as boundary:
        boundary["payload"] = {}
        try:
            service._remove_records(run, {}, None, report)
        except ValueError:
            rejected = True
    assert rejected, "空库存必须在任何隔离/删除之前被拒绝"
    assert run.is_dir() and not list(run.iterdir())
    assert report == {"removed_dirs": 0, "removed_files": 0, "removed_bytes": 0}
    assert not list((home / "agents").glob(".purge_*"))
