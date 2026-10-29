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


# LLM: 原始日志7天删除前先把有界tail发表到回执，30天无引用再删回执。
# 函数用途: 验证两阶段删除和空子目录收尾。
def test_raw_ttl_and_receipt_ttl(tmp_path):
    meta, log, original, _ = _job(tmp_path, age=8, nested=True)
    assert _maintain(tmp_path)["logs_removed"] == 1
    assert not log.exists()
    receipt = json.loads(meta.read_text())
    assert receipt["exit_code"] == 0 and receipt["command"] == original["command"]
    assert receipt["tail_preview"][log.name].endswith("尾中")
    assert _maintain(tmp_path, summary_ttl_days=7)["receipts_removed"] == 1
    assert not meta.parent.exists()


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
