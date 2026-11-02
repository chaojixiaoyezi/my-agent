# LLM: 只用 pytest 临时工作区、真实 SubAgentManager 与公开 write_patch_apply_report；模拟 I/O 错误只发生在临时目标的写入口，
#   不替换前像账本、回滚或报告写入实现。
# 模块用途: 回归 P1-R9-24——patch 事务由执行器持有字节级前像账本，写到一半失败如实回滚、元数据一致、不产出成功输出。
from __future__ import annotations

import errno
import json
import os
import stat
from pathlib import Path

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.patch.patch_apply import PatchApplyOptions

# CRLF 与孤立 CR：文本读取的通用换行会把它们改成 LF，回滚必须按字节恢复，不能经文本往返。
ORIGINAL = "old-a\r\n第二行\r尾\r\n".encode()


# 函数用途: 构造一个 write_file patch 声明。
def _patch(path: str, content: str) -> dict:
    return {"path": path, "status": "planned", "tool": "write_file", "content": content}


# LLM: 目标根经 extra_write_roots 进入原写边界；output.json 只写本测试的 patch 声明。
# 函数用途: 准备项目目录、真实管理器、子代理任务及其输出账本。
def _case(tmp_path: Path, patches: list[dict]):
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.txt").write_bytes(ORIGINAL)
    manager = SubAgentManager(tmp_path / "workspace", workspace_root=project)
    task = manager.create_run(goal="写入补丁", extra_write_roots=[str(project)])
    Path(task.output_json).write_text(json.dumps({"patches": patches}, ensure_ascii=False), encoding="utf-8")
    return manager, task, project


# 函数用途: 经公开报告入口执行一次 dry-run 或真实 apply，返回唯一记录。
def _apply(manager, task, *, apply: bool):
    report = manager.patch.write_patch_apply_report(run_ids=[task.id], options=PatchApplyOptions(apply=apply))
    assert len(report.records) == 1
    return report.records[0]


# LLM: 包住原 Path.write_text，对指定目标先写入一半再抛普通 ENOSPC，模拟写到一半的真实部分写入。
# 函数用途: 让一个 patch 目标发生一次部分写失败。
def _partial_write_failure(monkeypatch, target: Path) -> dict[str, int]:
    original = Path.write_text
    calls = {"failed": 0}

    def flaky(self, data, *args, **kwargs):
        if self == target and not calls["failed"]:
            calls["failed"] += 1
            original(self, data[: max(1, len(data) // 2)], *args, **kwargs)
            raise OSError(errno.ENOSPC, "simulated I/O error")
        return original(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", flaky)
    return calls


# LLM: 公开 dry-run 不写目标；真实 apply 写入并把记录私有写到任务真实 reports_dir/task_dir。
# 函数用途: 验证原公开报告链路的 dry-run 与成功路径。
def test_public_report_dry_run_then_success(tmp_path):
    manager, task, project = _case(tmp_path, [_patch("a.txt", "new-a\n"), _patch("sub/b.txt", "new-b\n")])

    dry = _apply(manager, task, apply=False)
    assert (dry.decision, dry.ok, dry.rollback_performed) == ("WOULD_APPLY", True, False)
    assert (project / "a.txt").read_bytes() == ORIGINAL and not (project / "sub").exists()
    record_json = Path(task.reports_dir) / "patch_apply.json"
    record_md = Path(task.task_dir) / "PATCH_APPLY.md"
    assert json.loads(record_json.read_text(encoding="utf-8"))["decision"] == "WOULD_APPLY"
    assert record_md.is_file()
    if os.name == "posix":
        assert {stat.S_IMODE(path.stat().st_mode) for path in (record_json, record_md)} == {0o600}

    applied = _apply(manager, task, apply=True)
    assert (applied.decision, applied.ok, applied.applied_count) == ("APPLIED", True, 2)
    assert (project / "a.txt").read_text(encoding="utf-8") == "new-a\n"
    assert (project / "sub" / "b.txt").read_text(encoding="utf-8") == "new-b\n"
    assert json.loads(record_json.read_text(encoding="utf-8"))["decision"] == "APPLIED"
    output = json.loads(Path(task.output_json).read_text(encoding="utf-8"))
    assert [patch["apply_status"] for patch in output["patches"]] == ["APPLIED", "APPLIED"]


# LLM: 第二个目标写到一半失败：已写的第一个目标按前像逐字节恢复，事务新建的目标（含失败的半截文件与未写到的）删除；
#   记录如实为 ROLLBACK，output.json 保持事务前字节，不出现 applied 元数据。
# 函数用途: 验证部分写失败的完整回滚与不产出成功输出。
def test_partial_second_file_failure_restores_exact_bytes_and_new_files(tmp_path, monkeypatch):
    manager, task, project = _case(tmp_path, [
        _patch("a.txt", "new-a\n"), _patch("sub/b.txt", "new-b" * 50), _patch("c.txt", "new-c\n"),
    ])
    output_before = Path(task.output_json).read_bytes()
    calls = _partial_write_failure(monkeypatch, (project / "sub" / "b.txt").resolve())

    record = _apply(manager, task, apply=True)

    assert calls["failed"] == 1
    assert (record.decision, record.ok, record.rollback_performed, record.applied) == ("ROLLBACK", False, True, False)
    assert (project / "a.txt").read_bytes() == ORIGINAL
    assert not (project / "sub" / "b.txt").exists() and not (project / "c.txt").exists()
    assert [entry["apply_status"] for entry in record.patches] == ["ROLLED_BACK"] * 3
    assert all(entry["actual_diff"] == "" for entry in record.patches)
    assert Path(task.output_json).read_bytes() == output_before
    assert record.failure_recovery["rollback_performed"] is True


# LLM: 同一目标出现两次时账本只保留第一份前像；后续失败回滚到事务前原字节而不是第一份 patch 的内容；
#   无故障时最后一份 patch 生效。
# 函数用途: 验证重复目标的前像语义。
def test_duplicate_targets_keep_first_preimage(tmp_path, monkeypatch):
    patches = [_patch("a.txt", "first\n"), _patch("a.txt", "second\n"), _patch("c.txt", "new-c\n")]
    manager, task, project = _case(tmp_path, patches)
    _partial_write_failure(monkeypatch, (project / "c.txt").resolve())

    failed = _apply(manager, task, apply=True)
    assert failed.decision == "ROLLBACK"
    assert (project / "a.txt").read_bytes() == ORIGINAL and not (project / "c.txt").exists()

    monkeypatch.undo()
    applied = _apply(manager, task, apply=True)
    assert applied.decision == "APPLIED"
    assert (project / "a.txt").read_text(encoding="utf-8") == "second\n"


# LLM: 回滚自身恢复失败时不能宣称已回滚：决策 ROLLBACK_FAILED、rollback_performed=False，未恢复目标单独标记，
#   能恢复的目标照常恢复。
# 函数用途: 验证回滚结果如实上报。
def test_rollback_failure_is_reported_truthfully(tmp_path, monkeypatch):
    manager, task, project = _case(tmp_path, [_patch("a.txt", "new-a\n"), _patch("sub/b.txt", "new-b\n")])
    _partial_write_failure(monkeypatch, (project / "sub" / "b.txt").resolve())
    original_write_bytes = Path.write_bytes
    target = (project / "a.txt").resolve()

    def failing_restore(self, data):
        if self == target:
            raise OSError(errno.EIO, "simulated I/O error")
        return original_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", failing_restore)
    record = _apply(manager, task, apply=True)

    assert (record.decision, record.ok, record.rollback_performed) == ("ROLLBACK_FAILED", False, False)
    assert "1 个文件未恢复" in record.message
    assert [entry["apply_status"] for entry in record.patches] == ["ROLLBACK_FAILED", "ROLLED_BACK"]
    assert not (project / "sub" / "b.txt").exists()
    assert (project / "a.txt").read_text(encoding="utf-8") == "new-a\n"
    assert record.failure_recovery["rollback_performed"] is False
