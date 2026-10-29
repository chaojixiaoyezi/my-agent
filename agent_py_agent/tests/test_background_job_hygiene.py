# LLM: 所有日志与回执仅在tmp_path创建；不启动、查询或终止进程。
# 模块用途: 验证后台作业3/7/30天维护与运行、pin、链接及引用保留规则。
import importlib.util
import json
import os

import pytest

NOW = 2_000_000_000.0
DAY = 86400


# LLM: 入口缺失时在断言上失败，避免把导入错误当成红灯证据。
# 函数用途: 加载新的显式日志维护入口。
def _maintain(root, **kwargs):
    name = "agent_py_agent.tools.shell_tools.background_job_hygiene"
    try:
        spec = importlib.util.find_spec(name)
    except ModuleNotFoundError:
        spec = None
    assert spec is not None, "缺少后台日志维护实现"
    return __import__(name, fromlist=["maintain_background_jobs_dir"]).maintain_background_jobs_dir(root, now=NOW, **kwargs)


# LLM: 回执保留真实终态字段；日志使用Unicode保证裁切后UTF8可解码。
# 函数用途: 创建临时平铺或子目录作业。
def _job(tmp_path, *, age=4, status="completed", nested=False, **fields):
    job_id = "job-example"
    root = tmp_path / job_id if nested else tmp_path
    root.mkdir(parents=True, exist_ok=True)
    log = root / f"{job_id}.log"
    raw = ("首🙂" * 3000 + "尾中" * 3000).encode()
    log.write_bytes(raw)
    meta = root / f"{job_id}.json"
    payload = {"job_id": job_id, "status": status, "command": "example", "started_at": NOW - (age + 1) * DAY,
               "ended_at": NOW - age * DAY, "exit_code": 0, "output_file": str(log), "artifact_refs": [], **fields}
    meta.write_text(json.dumps(payload))
    os.utime(meta, (NOW - age * DAY,) * 2)
    return meta, log, payload, raw


# LLM: 摘要只裁原始输出且写明原大小；重复运行不再次裁剪。
# 函数用途: 验证3天头尾UTF8摘要及原回执字段保留。
def test_slim_utf8_head_tail_and_receipt(tmp_path):
    meta, log, original, raw = _job(tmp_path)
    assert _maintain(tmp_path)["slimmed"] == 1
    content = log.read_text()
    assert content.startswith("首🙂") and content.endswith("尾中")
    assert "...[truncated by 3d background_job_hygiene]..." in content
    assert len(log.read_bytes()) <= 8192
    result = json.loads(meta.read_text())
    assert result["log_slimmed_at"] == NOW
    assert result["original_log_bytes"][log.name] == len(raw)
    assert all(result[k] == v for k, v in original.items())
    assert _maintain(tmp_path)["slimmed"] == 0


# LLM: 原始日志7天删除前先保存tail，30天无引用再删回执；Store永久锁不能随目录回收分裂锁身份。
# 函数用途: 验证两阶段删除及只留协调锁的目录收尾。
def test_raw_ttl_and_receipt_ttl(tmp_path):
    meta, log, original, _ = _job(tmp_path, age=8, nested=True)
    assert _maintain(tmp_path)["logs_removed"] == 1
    assert not log.exists()
    receipt = json.loads(meta.read_text())
    assert receipt["exit_code"] == 0 and receipt["command"] == original["command"]
    assert receipt["tail_preview"][log.name].endswith("尾中")
    assert _maintain(tmp_path, summary_ttl_days=7)["receipts_removed"] == 1
    assert not meta.exists()
    # 新增Store锁后目录已非空；不删永久锁才能保护等待中的写者。
    assert {path.name for path in meta.parent.iterdir()} == {".process-sessions.lock"}
    lock_identity = (meta.parent / ".process-sessions.lock").stat().st_ino
    _maintain(tmp_path)
    assert (meta.parent / ".process-sessions.lock").stat().st_ino == lock_identity


# LLM: 不读取PID判退出；缺少结构化终态时即使文件很老也不动。
# 函数用途: 保护运行态、未知态及pin的所有文件。
@pytest.mark.parametrize("status", ["running", "starting", "pending", "unknown"])
def test_unconfirmed_jobs_untouched(tmp_path, status):
    meta, log, _, raw = _job(tmp_path, age=80, status=status, ended_at=None, exit_code=None, pid=999999999)
    before = meta.read_bytes()
    result = _maintain(tmp_path)
    assert result["logs_removed"] == result["slimmed"] == result["receipts_removed"] == 0
    assert log.read_bytes() == raw and meta.read_bytes() == before


# LLM: pin保护整项；长期artifact引用保护回执而不无限保留原始日志。
# 函数用途: 验证pin优先及产物引用不会丢失。
def test_pins_and_artifact_refs(tmp_path):
    meta, log, _, raw = _job(tmp_path, age=40, artifact_refs=["artifact:proof"])
    assert _maintain(tmp_path, pinned_job_ids={"job-example"})["logs_removed"] == 0
    assert log.read_bytes() == raw
    assert _maintain(tmp_path)["logs_removed"] == 1
    assert meta.exists() and json.loads(meta.read_text())["artifact_refs"] == ["artifact:proof"]


# LLM: 元数据中的路径不能越出jobs_root，符号链接或硬链接输出不能修改。
# 函数用途: 防止清理越界或跟随链接。
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "outside"])
def test_unsafe_output_is_untouched(tmp_path, monkeypatch, kind):
    root = tmp_path / "jobs"
    meta, log, payload, raw = _job(root)
    outside = tmp_path / "outside.log"
    outside.write_bytes(raw)
    if kind == "outside":
        payload["output_file"] = str(outside)
        meta.write_text(json.dumps(payload))
    else:
        log.unlink()
        if kind == "symlink":
            log.symlink_to(outside)
        else:
            try:
                os.link(outside, log)
            except PermissionError:
                # 保留多链接拒绝断言；link被拒时仅注入同一目标叶的stat形状，物理验收另记未验证。
                log.write_bytes(raw)
                from pathlib import Path
                original_lstat = Path.lstat
                # LLM: 精确模拟目标日志的nlink=2，不拦截读写、不替代维护结果。
                # 函数用途: 系统禁止创建硬链接时仍执行原拒绝分支。
                def multi_link(path, *args, **kwargs):
                    value = original_lstat(path, *args, **kwargs)
                    if path == log:
                        fields = list(value)
                        fields[3] = 2
                        return os.stat_result(fields)
                    return value
                monkeypatch.setattr(Path, "lstat", multi_link)
    before = meta.read_bytes()
    _maintain(root)
    assert outside.read_bytes() == raw and log.read_bytes() == raw and meta.read_bytes() == before


# LLM: 有终态但尚不足阈值时零写入；max_jobs为0同样不产生维护副作用。
# 函数用途: 验证年轻任务和禁用批次。
def test_young_and_zero_budget(tmp_path):
    meta, log, _, raw = _job(tmp_path, age=2)
    before = meta.read_bytes()
    assert _maintain(tmp_path)["slimmed"] == 0
    assert _maintain(tmp_path, slim_after_days=0, max_jobs=0)["slimmed"] == 0
    assert log.read_bytes() == raw and meta.read_bytes() == before


# LLM: 显式exit_code或finished_at本身可以确认退出；mtime回退一旦写摘要必须固定不随维护漂移。
# 函数用途: 验证实际进程回执字段及无完成日期的终态回执。
@pytest.mark.parametrize("fields", [{"status": "running", "exit_code": 0}, {"status": "exited", "exit_code": None}, {"status": "running", "exit_code": None, "finished_at": NOW - 4 * DAY}])
def test_explicit_exit_evidence_and_mtime_age(tmp_path, fields):
    meta, log, _, _ = _job(tmp_path, ended_at=None, **fields)
    assert _maintain(tmp_path)["slimmed"] == 1
    assert json.loads(meta.read_text())["hygiene_completed_at"] <= NOW - 3 * DAY
    assert _maintain(tmp_path, raw_log_ttl_days=3)["logs_removed"] == 1
    assert not log.exists()


# LLM: 一份退出回执不得清理另一份运行回执仍引用的共享日志。
# 函数用途: 验证全局运行引用保护。
def test_shared_active_log_is_protected(tmp_path):
    meta, log, _, raw = _job(tmp_path, age=40)
    active = tmp_path / "job-active.json"
    active.write_text(json.dumps({"status": "running", "job_id": "job-active", "output_file": str(log)}))
    _maintain(tmp_path)
    assert meta.exists() and log.read_bytes() == raw


# LLM: 终态不代表所有共享引用都到期，较老回执不得替较新作业决定删除共享日志。
# 函数用途: 复现不同年龄终态作业共享日志的提前回收。
def test_shared_younger_terminal_log_is_protected(tmp_path):
    meta, log, _, raw = _job(tmp_path, age=40)
    young = tmp_path / "job-young.json"
    young.write_text(json.dumps({"status": "completed", "ended_at": NOW - DAY, "output_file": str(log)}))
    _maintain(tmp_path)
    assert meta.exists() and log.read_bytes() == raw


# LLM: 永久保留的artifact回执不占用每批实际维护额度，否则排在后面的作业会永远不清。
# 函数用途: 复现max_jobs前缀饥饿。
def test_unchanged_receipt_does_not_starve_later_jobs(tmp_path):
    meta, log, _, _ = _job(tmp_path, age=40)
    pinned_by_ref = tmp_path / "aaa-retained.json"
    pinned_by_ref.write_text(json.dumps({"status": "completed", "ended_at": NOW - 40 * DAY, "artifact_refs": ["artifact:keep"]}))
    assert _maintain(tmp_path, max_jobs=1)["logs_removed"] == 1
    assert not meta.exists() and not log.exists() and pinned_by_ref.exists()


# LLM: 非有限配置、负时间和负批次都不能产生写入副作用。
# 函数用途: 验证维护参数早期拒绝。
@pytest.mark.parametrize("fields", [{"slim_after_days": -1}, {"raw_log_ttl_days": float("nan")}, {"max_jobs": -1}])
def test_invalid_options_rejected(tmp_path, fields):
    with pytest.raises(ValueError):
        _maintain(tmp_path, **fields)


# LLM: 在真实初扫结束后才发表另一份共享引用；不替代维护判定或日志写删。
# 函数用途: 保证晚到的运行态、pin或终态共享回执都保护原日志。
@pytest.mark.parametrize("age", [4, 40])
@pytest.mark.parametrize("reference", ["running", "pinned", "completed"])
def test_new_active_reference_after_inventory_is_protected(tmp_path, monkeypatch, age, reference):
    from agent_py_agent.tools.shell_tools import background_job_hygiene as hygiene
    meta, log, _, raw = _job(tmp_path, age=age)
    original = hygiene._maintain_job
    # LLM: 首次进入候选处理时插入真实回执，复现清单快照过时。
    # 函数用途: 精确安排引用发布与维护之间的交错。
    def publish(*args, **kwargs):
        (tmp_path / "active.json").write_text(json.dumps({"status": reference, "output_file": str(log)}))
        return original(*args, **kwargs)
    monkeypatch.setattr(hygiene, "_maintain_job", publish)
    result = _maintain(tmp_path, pinned_job_ids={"active"} if reference == "pinned" else set())
    assert result["logs_removed"] == result["slimmed"] == 0
    assert meta.exists() and log.read_bytes() == raw


# LLM: 在真实rename发表或隔离边界写入新引用，验证旧快照不能覆盖最新回执。
# 函数用途: 复现比较后新增artifact_refs被覆盖再删除的问题。
def test_new_artifact_reference_between_compare_and_save_is_preserved(tmp_path, monkeypatch):
    meta, log, payload, raw = _job(tmp_path, age=40)
    original = os.rename
    changed = []
    # LLM: 一次性在最终rename前更新原回执，其余rename均执行真实系统调用。
    # 函数用途: 注入不参与锁协议的写者以检查隔离后身份复核。
    def publish(source, target, *args, **kwargs):
        if not changed and (source == meta.name or target == meta.name):
            changed.append(True)
            meta.write_text(json.dumps({**payload, "artifact_refs": ["artifact:must-keep"]}))
        return original(source, target, *args, **kwargs)
    monkeypatch.setattr(os, "rename", publish)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {publish})
    _maintain(tmp_path)
    assert changed and meta.exists()
    assert json.loads(meta.read_text())["artifact_refs"] == ["artifact:must-keep"]
    assert log.read_bytes() == raw


# LLM: 在旧stat后、真实unlink/隔离rename前换入普通新inode；该新日志从未被验证。
# 函数用途: 防止删除或截断路径上后来发布的新日志。
@pytest.mark.parametrize("age", [4, 8])
def test_log_replacement_after_validation_is_not_deleted(tmp_path, monkeypatch, age):
    from agent_py_agent.agent.common.nofollow_fs import unlink_file_beneath
    from agent_py_agent.tools.shell_tools import background_job_hygiene as hygiene
    _, log, _, _ = _job(tmp_path, age=age)
    rename, unlink = os.rename, unlink_file_beneath
    changed = []
    # LLM: 仅第一次目标动作前换入新内容，原操作继续执行。
    # 函数用途: 建立确定性的检查后inode替换窗口。
    def replace_log():
        if not changed:
            changed.append(True)
            fresh = tmp_path / "fresh.tmp"
            fresh.write_bytes(b"new-running-output")
            os.replace(fresh, log)
    # LLM: 新隔离协议与旧原子发表均经过rename，钩子不改系统调用结果。
    # 函数用途: 在维护尝试变更日志前换inode。
    def rename_log(source, target, *args, **kwargs):
        if source == log.name or target == log.name:
            replace_log()
        return rename(source, target, *args, **kwargs)
    # LLM: 保留旧实现的unlink边界，使本测试在修复前也能复现。
    # 函数用途: 注入旧删除链的同一竞态。
    def unlink_log(root, parts):
        if parts[-1] == log.name:
            replace_log()
        return unlink(root, parts)
    monkeypatch.setattr(os, "rename", rename_log)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {rename_log})
    monkeypatch.setattr(hygiene, "unlink_file_beneath", unlink_log, raising=False)
    _maintain(tmp_path)
    assert changed and log.read_bytes() == b"new-running-output"


# LLM: 使用真实路径生成器而非自造同层布局；回执仍来自进程Store而日志在workspace内。
# 函数用途: 检查无owner模式下子目录回执可维护父目录日志。
def test_process_sessions_subdir_receipts_maintain_parent_background_job_logs(tmp_path):
    from agent_py_agent.agent.tooling.process_session_store import process_session_store_root
    workspace = tmp_path.resolve()
    jobs = workspace / ".background_jobs"
    meta, log, payload, _ = _job(jobs, age=8)
    receipts = process_session_store_root(workspace)
    receipts.mkdir()
    meta.rename(receipts / meta.name)
    result = _maintain(jobs)
    assert result["errors"] == 0 and result["logs_removed"] == 1
    assert not log.exists() and json.loads((receipts / meta.name).read_text())["command"] == payload["command"]


# LLM: 显式owner只决定权威回执根；绝不把外置Store当作日志根。
# 函数用途: 检查真实owner模式的外置回执与workspace日志分离布局。
def test_external_owner_process_session_store_maintains_workspace_background_job_logs(tmp_path):
    import inspect

    from agent_py_agent.agent.tooling.process_session_store import process_session_store_root
    from agent_py_agent.tools.shell_tools.background_job_hygiene import maintain_background_jobs_dir
    assert "owner_home" in inspect.signature(maintain_background_jobs_dir).parameters
    workspace, owner = tmp_path.resolve() / "workspace", tmp_path.resolve() / "owner"
    jobs = workspace / ".background_jobs"
    meta, log, _, _ = _job(jobs, age=8)
    receipts = process_session_store_root(workspace, owner)
    receipts.mkdir(parents=True)
    meta.rename(receipts / meta.name)
    result = _maintain(jobs, owner_home=owner)
    assert result["errors"] == 0 and result["logs_removed"] == 1
    assert not log.exists() and (receipts / meta.name).exists()


# LLM: 只注入fdopen构造错误；使用真实fstat核对并回收测试发现的泄漏，避免测试污染宿主。
# 函数用途: 确保日志FD在交给文件对象之前失败也会关闭。
def test_preview_fdopen_failure_releases_opened_fd(tmp_path, monkeypatch):
    from agent_py_agent.tools.shell_tools import background_job_hygiene as hygiene
    _job(tmp_path)
    original, fstat, close = os.fdopen, os.fstat, os.close
    captured = []
    # LLM: 只在读取日志的rb模式拒绝构造，其余文件封装沿原实现。
    # 函数用途: 在FD所有权交接点制造错误。
    def fail(fd, mode="r", *args, **kwargs):
        if mode == "rb":
            captured.append(fd)
            raise OSError("injected fdopen")
        return original(fd, mode, *args, **kwargs)
    monkeypatch.setattr(os, "fdopen", fail)
    assert _maintain(tmp_path)["errors"] == 1
    leaked = []
    for fd in captured:
        try:
            fstat(fd)
        except OSError:
            continue
        leaked.append(fd)
        close(fd)
    assert captured and not leaked


# LLM: 在真实保存边界另开锁FD做非阻塞flock，验证与ProcessSessionStore同名锁而非只锁目录inode。
# 函数用途: 确认维护持有实际回执目录的跨进程互斥。
def test_receipt_mutation_holds_process_session_store_lock(tmp_path, monkeypatch):
    import fcntl

    from agent_py_agent.tools.shell_tools import background_job_hygiene as hygiene
    _job(tmp_path)
    save = hygiene._save
    blocked = []
    # LLM: 另一个文件描述符模拟独立写者；无论是否锁冲突都关闭探针FD。
    # 函数用途: 在保存事务内部验证真正的系统锁占用。
    def probe(*args, **kwargs):
        fd = os.open(tmp_path / ".process-sessions.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                blocked.append(True)
            else:
                blocked.append(False)
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        return save(*args, **kwargs)
    monkeypatch.setattr(hygiene, "_save", probe)
    _maintain(tmp_path)
    assert blocked == [True]


# LLM: 在最后一次缺失stat返回的准确窗口重建名字；不能靠先stat再普通rename冒充原子不覆盖。
# 函数用途: 保护隔离后新建的日志或新增产物引用回执。
@pytest.mark.parametrize("kind", ["log", "receipt"])
def test_recreated_name_after_last_check_is_not_overwritten(tmp_path, monkeypatch, kind):
    meta, log, payload, _ = _job(tmp_path)
    target = log if kind == "log" else meta
    expected = b"new-running-output" if kind == "log" else json.dumps({**payload, "artifact_refs": ["artifact:late"]}).encode()
    original_stat = os.stat
    recreated = []
    # LLM: 真stat确认缺失后再发表另一版本，同时保留原缺失返回给被测代码。
    # 函数用途: 精确触发原名缺失判断与最终发表间的竞态。
    def publish(name, *args, **kwargs):
        try:
            return original_stat(name, *args, **kwargs)
        except FileNotFoundError:
            if name == target.name and kwargs.get("dir_fd") is not None and not recreated:
                recreated.append(True)
                target.write_bytes(expected)
            raise
    monkeypatch.setattr(os, "stat", publish)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {publish})
    result = _maintain(tmp_path)
    assert recreated and target.read_bytes() == expected
    assert result["errors"] == 1


# LLM: 当前回执也必须重核；不能只扫描其它回执而漏掉保存后变为运行态的自身记录。
# 函数用途: 保护摘要保存后再次变化的当前回执和日志。
def test_current_receipt_change_after_save_preserves_log(tmp_path, monkeypatch):
    from agent_py_agent.tools.shell_tools import background_job_hygiene as hygiene
    meta, log, _, raw = _job(tmp_path, age=40)
    save = hygiene._save
    # LLM: 真保存完成后用不参与锁的写者改变当前回执，日志动作尚未开始。
    # 函数用途: 复现只排除own引用时漏掉自身变化。
    def change(*args, **kwargs):
        current = save(*args, **kwargs)
        record = json.loads(meta.read_text())
        meta.write_text(json.dumps({**record, "status": "running", "ended_at": None, "exit_code": None}))
        return current
    monkeypatch.setattr(hygiene, "_save", change)
    _maintain(tmp_path)
    assert log.exists() and log.read_bytes() == raw
    assert json.loads(meta.read_text())["status"] == "running"


# LLM: 发布临时文件与最终rename必须共用原parent FD，不能在已替换的新目录里落盘。
# 函数用途: 核验发布器不重新按路径打开临时文件目录。
def test_publish_uses_pinned_parent_after_directory_replacement(tmp_path):
    from agent_py_agent.tools.shell_tools import background_job_hygiene as hygiene
    root = tmp_path / "jobs"
    root.mkdir()
    (root / ".purge_log_test").write_bytes(b"old")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    moved = tmp_path / "old-jobs"
    root.rename(moved)
    root.mkdir()
    try:
        hygiene._publish_claimed(root, ("output.log",), b"summary", fd, ".purge_log_test")
    finally:
        os.close(fd)
    assert (moved / "output.log").read_bytes() == b"summary"
    assert not list(root.iterdir())


# LLM: 无内核不覆盖rename时不得先隔离再留下原名缺失；安全能力必须在首个变更前取得。
# 函数用途: 验证不支持平台零内容变更地拒绝维护。
def test_missing_exclusive_rename_preserves_original_names(tmp_path, monkeypatch):
    from agent_py_agent.tools.shell_tools import background_job_hygiene as hygiene
    meta, log, _, raw = _job(tmp_path)
    before = meta.read_bytes()
    # LLM: 仅模拟固定libc能力缺失，不改任何文件系统调用结果。
    # 函数用途: 触发不支持平台的保守拒绝。
    def unavailable():
        raise OSError("injected unavailable exclusive rename")
    monkeypatch.setattr(hygiene, "_exclusive_rename_api", unavailable)
    assert _maintain(tmp_path)["errors"] == 1
    assert meta.exists() and meta.read_bytes() == before and log.read_bytes() == raw
    assert not list(tmp_path.glob(".purge_*"))


# LLM: 已发表后fsync失败只可保留旧隔离证据并显式报恢复责任，不静默删除或伪报完整成功。
# 函数用途: 验证部分提交及重跑的可诊断状态。
def test_publication_fsync_failure_reports_retained_recovery(tmp_path, monkeypatch):
    import stat

    from agent_py_agent.tools.shell_tools import background_job_hygiene as hygiene
    meta, log, _, raw = _job(tmp_path)
    original = os.fsync
    failed = []
    # LLM: 只对新回执发表后的父目录fsync注入一次，前面的临时文件刷盘成功。
    # 函数用途: 构造已发表但收尾未结的真实中间态。
    def fail(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode) and not failed:
            failed.append(True)
            raise OSError("injected directory fsync")
        return original(fd)
    monkeypatch.setattr(os, "fsync", fail)
    result = _maintain(tmp_path)
    assert result["errors"] == result["recovery_required"] == 1
    assert meta.exists() and log.read_bytes() == raw and list(tmp_path.glob(".purge_receipt_*"))
    monkeypatch.setattr(os, "fsync", original)
    retry = _maintain(tmp_path)
    assert retry["errors"] == retry["recovery_required"] == 1
    assert log.read_bytes() == raw


# LLM: 通过真实ProcessSessionStore写v4权威记录，维护后再经原Store验证读取，不只测试手造JSON。
# 函数用途: 验证两种生产布局及显式Store参数保留原协议字段。
@pytest.mark.parametrize("layout", ["subdir", "owner", "explicit"])
def test_authoritative_process_store_roundtrip(tmp_path, layout):
    from agent_py_agent.agent.tooling.process_session_store import (
        ProcessSessionStore,
        process_session_store_root,
    )
    from agent_py_agent.tests.test_process_session_store import _running

    workspace = tmp_path.resolve() / "workspace"
    jobs = workspace / ".background_jobs"
    meta, log, _, _ = _job(jobs, age=8)
    meta.unlink()
    owner = tmp_path.resolve() / "owner" if layout == "owner" else None
    receipt_root = process_session_store_root(workspace, owner)
    store = ProcessSessionStore(receipt_root)
    original = store.write(_running("bg-a", status="exited", exit_code=0, finished_at=NOW - 8 * DAY,
                                    output_file=str(log), command="original-command"))
    options = {"owner_home": owner} if owner is not None else {}
    if layout == "explicit":
        options = {"receipt_store_root": receipt_root}
    result = _maintain(jobs, **options)
    assert result["errors"] == 0 and result["logs_removed"] == 1 and not log.exists()
    loaded = store.load("bg-a")
    assert loaded.load_error is None and all(loaded.record[key] == value for key, value in original.items())
    assert loaded.record["tail_preview"][log.name].endswith("尾中")
