# LLM: 仅在tmp_path生成真实展示归档；验证无损、保留期限与失败时不删唯一副本。
# 模块用途: 覆盖展示归档合页、墓碑及危险文件边界，不访问生产Home。
import json
import os
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import display_archive as archive

NOW = 2_000_000_000.0
DAY = 86400


# LLM: 用原producer建立临时归档；时间只调整manifest，避免测试依赖真实时钟。
# 函数用途: 返回测试存储、引用和归档目录。
def _case(tmp_path, *, age=2, text="中🙂e\u0301" * 9000):
    store = SimpleNamespace(storage=SimpleNamespace(root=tmp_path), threads=SimpleNamespace(load=lambda _: True))
    ref = archive.archive_display_rows(SimpleNamespace(conversation_store=store), thread_id="thread-test", rows=[{"text": text}])
    directory = tmp_path / "display_archives" / ref["archive_id"]
    os.utime(directory / "manifest.json", (NOW - age * DAY,) * 2)
    return store, ref, directory


# LLM: 动态取入口使缺失实现表现为合同断言失败，而非收集错误。
# 函数用途: 加载待实现的归档维护模块。
def _maintenance():
    import importlib.util
    name = "agent_py_agent.agent.conversation.display_archive_maintenance"
    assert importlib.util.find_spec(name) is not None, "缺少归档维护实现"
    return __import__(name, fromlist=["compact_display_archives"])


# LLM: 合页前后完整响应及Unicode分片必须完全一致，不能只检查文件数量。
# 函数用途: 验证合页读回、0600权限与重复运行幂等。
def test_bundle_roundtrip_and_idempotence(tmp_path):
    store, ref, directory = _case(tmp_path)
    before = [archive.read_display_archive_page(store, ref, i) for i in range(ref["page_count"])]
    result = _maintenance().compact_display_archives(store, now=NOW)
    assert result["compacted"] == 1
    assert {p.name for p in directory.iterdir()} == {"manifest.json", "pages_bundle.json"}
    assert (directory / "pages_bundle.json").stat().st_mode & 0o777 == 0o600
    assert [archive.read_display_archive_page(store, ref, i) for i in range(ref["page_count"])] == before
    assert _maintenance().compact_display_archives(tmp_path, now=NOW)["compacted"] == 0


# LLM: 墓碑是明确过期而不是空页或损坏；原六字段引用继续可验证。
# 函数用途: 验证pin、14天过期和墓碑保留30天后的删除。
def test_expiry_pin_and_stub_removal(tmp_path):
    store, ref, directory = _case(tmp_path, age=15)
    module = _maintenance()
    assert module.prune_expired_display_archives(store, now=NOW, pinned_archive_ids={ref["archive_id"]})["expired"] == 0
    assert module.prune_expired_display_archives(store, now=NOW)["expired"] == 1
    assert list(directory.iterdir()) == [directory / "manifest.json"]
    assert archive.validate_display_archive_reference(ref) == (ref["archive_id"], ref["thread_id"])
    with pytest.raises(archive.DisplayArchiveError) as error:
        archive.read_display_archive_page(store, ref, 0)
    assert error.value.code == "DISPLAY_ARCHIVE_EXPIRED"
    assert module.prune_expired_display_archives(store, now=NOW + 29 * DAY)["removed"] == 0
    assert module.prune_expired_display_archives(store, now=NOW + 31 * DAY)["removed"] == 1
    assert not directory.exists()
    with pytest.raises(archive.DisplayArchiveError) as error:
        archive.read_display_archive_page(store, ref, 0)
    assert error.value.code == "DISPLAY_ARCHIVE_UNAVAILABLE"


# LLM: 任一页缺失、坏形状或链接都不允许创建可导致原页删除的bundle。
# 函数用途: 验证整档验证失败保留原件。
@pytest.mark.parametrize("damage", ["missing", "corrupt", "symlink", "hardlink"])
def test_invalid_pages_are_not_compacted(tmp_path, monkeypatch, damage):
    store, ref, directory = _case(tmp_path)
    page = directory / "0.json"
    original = page.read_bytes()
    if damage == "missing":
        page.unlink()
    elif damage == "corrupt":
        page.write_text('{"rows": [{}]}')
    else:
        other = tmp_path / "outside.json"
        other.write_bytes(original)
        page.unlink()
        if damage == "symlink":
            page.symlink_to(other)
        else:
            try:
                os.link(other, page)
            except PermissionError:
                # 当前执行环境可拒绝link；保留同一多链接拒绝断言，用精确fstat形状注入而非跳过测试。
                page.write_bytes(original)
                inode = page.stat().st_ino
                original_fstat = os.fstat
                # LLM: 只替代目标叶的nlink观察，不改变维护返回或删除操作；真实硬链接验收须另行注明。
                # 函数用途: link系统调用不可执行时仍验证多链接拒绝分支。
                def multi_link(fd):
                    value = original_fstat(fd)
                    if value.st_ino == inode:
                        fields = list(value)
                        fields[3] = 2
                        return os.stat_result(fields)
                    return value
                monkeypatch.setattr(os, "fstat", multi_link)
    result = _maintenance().compact_display_archives(store, now=NOW)
    assert result["compacted"] == 0
    assert not (directory / "pages_bundle.json").exists()
    assert (directory / "1.json").exists()


# LLM: 删除中断时已经发表的bundle仍是完整副本，重跑应完成余页清理。
# 函数用途: 模拟保留部分独立页的可恢复中间态。
def test_resume_partially_deleted_bundle(tmp_path):
    store, ref, directory = _case(tmp_path)
    pages = [archive.read_display_archive_page(store, ref, i)["rows"] for i in range(ref["page_count"])]
    (directory / "pages_bundle.json").write_text(json.dumps({"schema": "display_archive_bundle.v1", "archive_id": ref["archive_id"], "thread_id": ref["thread_id"], "pages": pages}))
    (directory / "0.json").unlink()
    assert _maintenance().compact_display_archives(store, now=NOW)["compacted"] == 1
    assert not list(directory.glob("[0-9]*.json"))


# LLM: bundle身份、行结构及独立页优先级都要维持原读接口合同。
# 函数用途: 拒绝串档合页且不掩盖独立页损坏。
def test_bundle_validation_and_page_precedence(tmp_path):
    store, ref, directory = _case(tmp_path)
    _maintenance().compact_display_archives(store, now=NOW)
    bundle = directory / "pages_bundle.json"
    value = json.loads(bundle.read_text())
    value["thread_id"] = "other"
    bundle.write_text(json.dumps(value))
    with pytest.raises(archive.DisplayArchiveError):
        archive.read_display_archive_page(store, ref, 0)


# LLM: 年轻归档与受限批次不可被提前清理，目录或manifest链接不可触碰。
# 函数用途: 验证期限、批次上限与链接保护。
def test_age_limit_and_symlink_manifest(tmp_path):
    store, ref, directory = _case(tmp_path, age=0.5)
    module = _maintenance()
    assert module.compact_display_archives(store, now=NOW)["compacted"] == 0
    assert module.prune_expired_display_archives(store, now=NOW)["expired"] == 0
    assert module.compact_display_archives(store, now=NOW + 2 * DAY, max_archives=0)["compacted"] == 0
    manifest = directory / "manifest.json"
    other = tmp_path / "manifest-copy"
    manifest.rename(other)
    manifest.symlink_to(other)
    assert module.prune_expired_display_archives(store, now=NOW + 40 * DAY)["expired"] == 0
    assert manifest.is_symlink() and other.exists()


# LLM: 即使已经写出临时bundle，刷盘失败也不能删除任何独立页。
# 函数用途: 验证持久化失败保留唯一原件。
def test_fsync_failure_preserves_pages(tmp_path, monkeypatch):
    store, ref, directory = _case(tmp_path)
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    module = _maintenance()
    # LLM: 在真实写入后的fsync边界注入错误，不替代维护控制流。
    # 函数用途: 制造磁盘刷盘失败。
    def fail_fsync(_fd):
        raise OSError("测试刷盘失败")
    monkeypatch.setattr(module.os, "fsync", fail_fsync)
    assert module.compact_display_archives(store, now=NOW)["errors"] == 1
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before


# LLM: 已有坏bundle不能成为删除健康单页的依据，独立页仍优先可读。
# 函数用途: 验证坏合页保留原页与读接口优先级。
def test_existing_bad_bundle_keeps_pages(tmp_path):
    store, ref, directory = _case(tmp_path)
    before = archive.read_display_archive_page(store, ref, 0)
    (directory / "pages_bundle.json").write_text('{"schema":"wrong"}')
    assert _maintenance().compact_display_archives(store, now=NOW)["errors"] == 1
    assert archive.read_display_archive_page(store, ref, 0) == before


# LLM: 总合页预算必须在累积页过程中检查，不等整档装入内存才拒绝。
# 函数用途: 验证多页总字节超限不会发表bundle或删页。
def test_bundle_total_budget_is_bounded(tmp_path, monkeypatch):
    store, ref, directory = _case(tmp_path)
    module = _maintenance()
    monkeypatch.setattr(module, "_bundle_limit", lambda _: 1000)
    original_read = module._read
    reads = []
    # LLM: 只统计实际JSON页读取次数，不改变内容或大小。
    # 函数用途: 确认总预算耗尽后不继续装载剩余页。
    def read(*args):
        if args[1][0].isdigit():
            reads.append(args[1])
        return original_read(*args)
    monkeypatch.setattr(module, "_read", read)
    assert module.compact_display_archives(store, now=NOW)["compacted"] == 0
    assert (directory / "0.json").exists() and not (directory / "pages_bundle.json").exists()
    assert reads == ["0.json"]


# LLM: TTL墓碑已发表后删除中断，重跑仍必须完成载荷清理，错误码始终为过期。
# 函数用途: 验证过期事务可恢复。
def test_expiry_resumes_after_payload_removal_error(tmp_path, monkeypatch):
    store, ref, directory = _case(tmp_path, age=15)
    module = _maintenance()
    original = module._unlink
    # LLM: 只在首次载荷unlink边界失败，manifest提交仍走真实实现。
    # 函数用途: 模拟过期清理中断。
    def fail_unlink(*args):
        raise OSError("测试删除中断")
    monkeypatch.setattr(module, "_unlink", fail_unlink)
    assert module.prune_expired_display_archives(store, now=NOW)["errors"] == 1
    with pytest.raises(archive.DisplayArchiveError) as error:
        archive.read_display_archive_page(store, ref, 0)
    assert error.value.code == "DISPLAY_ARCHIVE_EXPIRED"
    monkeypatch.setattr(module, "_unlink", original)
    module.prune_expired_display_archives(store, now=NOW)
    assert {p.name for p in directory.iterdir()} == {"manifest.json"}


# LLM: 最后删manifest后rmdir失败不能遗留永久不可重试的空UUID；新producer的空目录不能提前删除。
# 函数用途: 复现存根删除最后两步之间的恢复缺口。
def test_stub_rmdir_failure_is_retryable(tmp_path, monkeypatch):
    store, ref, directory = _case(tmp_path, age=15)
    module = _maintenance()
    module.prune_expired_display_archives(store, now=NOW)
    original = module.os.rmdir
    # LLM: 只在最终rmdir故障边界注入失败，前面的真实删除不被替代。
    # 函数用途: 模拟目录暂时不可删除。
    def fail_rmdir(*args, **kwargs):
        raise OSError("测试rmdir中断")
    monkeypatch.setattr(module.os, "rmdir", fail_rmdir)
    assert module.prune_expired_display_archives(store, now=NOW + 31 * DAY)["errors"] == 1
    monkeypatch.setattr(module.os, "rmdir", original)
    assert module.prune_expired_display_archives(store, now=NOW + 31 * DAY)["removed"] == 1
    assert not directory.exists()
