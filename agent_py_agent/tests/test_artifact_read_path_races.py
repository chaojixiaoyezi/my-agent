# LLM: c1确定性交错测试；只在临时owner树替换路径，必须证明屏障实际触发且未读取外部正文。
# 模块用途: 覆盖索引/正文逃逸、根与父目录替换、严格打开能力及同大小篡改。
from __future__ import annotations

import json
import os
from errno import EPERM
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_py_agent.agent.common import nofollow_fs
from agent_py_agent.agent.memory_archive.artifact import reader
from agent_py_agent.agent.tooling.artifact import ReadArtifactTool
from agent_py_agent.tests.test_memory_artifact_read import _write_externalized_tool_output


# LLM: Tamper a non-returned middle byte in place after warmup; unchanged size/mtime is never content evidence.
# 函数用途: 证明热缓存仍完整哈希，不能只校验返回窗口的块。
@pytest.mark.parametrize("restore_ctime_key", [False, True])
def test_blob_middle_tampering_is_rehashed(tmp_path, monkeypatch, restore_ctime_key):
    path = _write_externalized_tool_output(tmp_path, content="x" * 2_000_000)
    blob = path.with_suffix(".blob")
    assert blob.exists()
    request = reader.ReadToolOutputArtifactRequest(tmp_path, str(path), max_chars=8)
    assert reader.read_tool_output_artifact(request)["ok"]
    before = blob.stat()
    with blob.open("r+b") as handle:
        handle.seek(1_000_000)
        handle.write(b"y")
    os.utime(blob, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert blob.stat().st_size == before.st_size and blob.stat().st_mtime_ns == before.st_mtime_ns
    if restore_ctime_key:
        original = reader.os.fstat
        # LLM: Keep all identity/stat facts as before only for this blob to force a warm key; bytes still come from the real FD.
        # 函数用途: 模拟mtime/ctime缓存键均复原，强制证明热读不能跳过全文哈希。
        def fstat(descriptor):
            value = original(descriptor)
            return before if value.st_ino == before.st_ino else value
        monkeypatch.setattr(reader.os, "fstat", fstat)
    result = reader.read_tool_output_artifact(request)
    assert not result["ok"] and result["error_code"] == "artifact_hash_mismatch"
    assert "content" not in result


# LLM: Missing/symlink sidecars fail at strict root-anchored open, both before and after a verified index is cached.
# 函数用途: 验证侧文件缺失和打开前被链接替换不会泄露正文。
@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("shape", ["missing", "symlink"])
def test_blob_missing_or_symlink_fails_closed(tmp_path, warm, shape):
    root = tmp_path / "owner"
    path = _write_externalized_tool_output(root, content="x" * 600_000)
    blob = path.with_suffix(".blob")
    assert blob.exists()
    request = reader.ReadToolOutputArtifactRequest(root, str(path), max_chars=8)
    if warm:
        assert reader.read_tool_output_artifact(request)["ok"]
    blob.unlink()
    if shape == "symlink":
        outside = tmp_path / "outside"
        outside.write_bytes(b"secret" * 100_000)
        blob.symlink_to(outside)
    result = reader.read_tool_output_artifact(request)
    expected = "artifact_missing" if shape == "missing" else "artifact_unreadable"
    assert not result["ok"] and result["error_code"] == expected and "content" not in result


# LLM: Strict basename validation precedes every sidecar open; wrapper-injected inline content retains legacy priority.
# 函数用途: 拒绝路径穿越、错名和非字符串content，并保持手工注入的内联正文兼容。
@pytest.mark.parametrize("field,value,error", [
    ("content_blob", "../escape.blob", "invalid_tool_output_artifact"),
    ("content_blob", "sub\\escape.blob", "invalid_tool_output_artifact"),
    ("content_blob", "other.blob", "invalid_tool_output_artifact"),
    ("blob_block_hashes", ["0" * 64] * 10, "artifact_hash_mismatch"),
    ("blob_block_hashes", [], "artifact_hash_mismatch"),
    ("content", None, "invalid_tool_output_artifact"),
    ("artifact_format", "unknown", "invalid_tool_output_artifact"),
])
def test_blob_metadata_validation(tmp_path, field, value, error):
    path = _write_externalized_tool_output(tmp_path, content="x" * 600_000)
    payload = json.loads(path.read_bytes())
    assert payload.get("artifact_format") == "blob_v1"
    payload[field] = value
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = reader.read_tool_output_artifact(reader.ReadToolOutputArtifactRequest(tmp_path, str(path)))
    assert not result["ok"] and result["error_code"] == error


# LLM: Declared blob_v1 format is authoritative; injecting inline content into a blob wrapper must fail closed.
# 函数用途: 验证blob_v1包装被注入内联content时不会绕过缺失或损坏的.blob侧文件。
def test_blob_wrapper_rejects_inline_content_override(tmp_path):
    import hashlib

    path = _write_externalized_tool_output(tmp_path, content="x" * 600_000)
    payload = json.loads(path.read_bytes())
    assert payload.get("artifact_format") == "blob_v1"
    payload.update(content="inline\r\n中文😀", sha256=hashlib.sha256("inline\r\n中文😀".encode()).hexdigest())
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.with_suffix(".blob").unlink()
    result = reader.read_tool_output_artifact(reader.ReadToolOutputArtifactRequest(tmp_path, str(path)))
    assert not result["ok"] and result["error_code"] == "invalid_tool_output_artifact"


# LLM: A deterministic post-hash edit with deliberately unchanged stat observations proves actual returned bytes are verified.
# 函数用途: 复现完整解码分支的校验后修改，不把stat相同误当成内容相同。
@pytest.mark.parametrize("mode,limit", [("slice", 0), ("search", 80)])
def test_blob_full_decode_rechecks_returned_bytes(tmp_path, monkeypatch, mode, limit):
    path = _write_externalized_tool_output(tmp_path, content="needle original\r\n" * 40_000)
    blob = path.with_suffix(".blob")
    request = reader.ReadToolOutputArtifactRequest(tmp_path, str(path), max_chars=limit, mode=mode, query="needle")
    assert reader.read_tool_output_artifact(request)["ok"]
    before = blob.stat()
    digest, fstat = reader._stream_blob_digest, reader.os.fstat
    fired = []

    # LLM: Return the genuine hash then modify the real same FD's file; not a forged digest.
    # 函数用途: 在全文验证和返回正文之间制造确定性的原位修改。
    def after_digest(handle):
        actual = digest(handle)
        with blob.open("r+b") as writer:
            writer.seek(7)
            writer.write(b"tampered")
        os.utime(blob, ns=(before.st_atime_ns, before.st_mtime_ns))
        fired.append(True)
        return actual

    # LLM: Simulate coarse/unchanged stat observations, while every body byte is still read from the real file.
    # 函数用途: 移除时间戳这一旁证，确保测试由字节校验而非stat拦住。
    def unchanged_stat(descriptor):
        actual = fstat(descriptor)
        return before if actual.st_ino == before.st_ino else actual

    monkeypatch.setattr(reader, "_stream_blob_digest", after_digest)
    monkeypatch.setattr(reader.os, "fstat", unchanged_stat)
    result = reader.read_tool_output_artifact(request)
    assert fired == [True]
    assert not result["ok"] and result["error_code"] == "artifact_hash_mismatch"
    assert "content" not in result


# LLM: Returned windows may use old FDs but never reopen an attacker-selected sibling or parent chain.
# 函数用途: 在JSON已读后、blob打开前确定性替换根/父目录/叶，要求安全拒绝。
@pytest.mark.parametrize("part", ["root", "parent", "leaf"])
def test_blob_open_race_rejects_foreign_path(tmp_path, monkeypatch, part):
    root, foreign = tmp_path / "owner", tmp_path / "foreign"
    path = _write_externalized_tool_output(root, content="x" * 600_000)
    outside = _write_externalized_tool_output(foreign, content="y" * 600_000).with_suffix(".blob")
    blob = path.with_suffix(".blob")
    original, fired = reader.open_readonly_file_beneath, []

    # LLM: Intercept only the blob open, not authorization, index lookup or wrapper reading.
    # 函数用途: 在侧文件安全打开屏障前换入外部链接。
    def open_blob(anchor, parts):
        if parts[-1].endswith(".blob"):
            source, target = ((root, foreign) if part == "root" else (blob.parent, outside.parent))
            if part == "leaf":
                blob.unlink()
                blob.symlink_to(outside)
            else:
                source.rename(source.with_name(source.name + "-old"))
                source.symlink_to(target, target_is_directory=True)
            fired.append(True)
        return original(anchor, parts)

    monkeypatch.setattr(reader, "open_readonly_file_beneath", open_blob)
    result = reader.read_tool_output_artifact(reader.ReadToolOutputArtifactRequest(root, str(path)))
    assert fired == [True]
    assert not result["ok"] and result["error_code"] == "artifact_unreadable" and "content" not in result


# LLM: Strict UTF-8 decoding is an independent requirement even if wrapper hashes have been changed to match invalid bytes.
# 函数用途: 验证非法UTF-8不会缓存或暴露成成功正文。
def test_blob_invalid_utf8_is_unreadable(tmp_path):
    import hashlib

    path = _write_externalized_tool_output(tmp_path, content="中" * 200_000)
    blob = path.with_suffix(".blob")
    raw = blob.read_bytes()[:-1] + b"\xff"
    blob.write_bytes(raw)
    payload = json.loads(path.read_bytes())
    payload["sha256"] = hashlib.sha256(raw).hexdigest()
    payload["blob_block_hashes"] = [hashlib.sha256(raw[i:i + 65536]).hexdigest() for i in range(0, len(raw), 65536)]
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = reader.read_tool_output_artifact(reader.ReadToolOutputArtifactRequest(tmp_path, str(path)))
    assert not result["ok"] and result["error_code"] == "artifact_unreadable" and "content" not in result


# LLM: 返回真实注册的两个owner档案，外owner的有效wrapper不能凭自身sha获得授权。
# 函数用途: 为竞态用例创建内容不同、结构相同的本地归档。
def _archives(tmp_path):
    root, foreign = tmp_path / "owner", tmp_path / "foreign"
    own = _write_externalized_tool_output(root, content="inside-original-content")
    outside = _write_externalized_tool_output(foreign, content="outside-secret-content!")
    destination = outside.with_name(own.name)
    destination.write_bytes(outside.read_bytes())
    return root, foreign, own, destination


# LLM: 发现的已resolve目录不能自授权，外owner索引必须在解析前被拒绝。
# 函数用途: 验证tool_outputs发现路径逃出认证根时不读取外部内容。
def test_discovered_external_directory_cannot_authorize_itself(tmp_path):
    root, foreign, own, outside = _archives(tmp_path)
    directory = root / "runs/2026-10-23/escape/work/blobs/tool_outputs"
    directory.parent.mkdir(parents=True)
    directory.symlink_to(outside.parent, target_is_directory=True)
    request = reader.ReadToolOutputArtifactRequest(root, "run-artifact:call-artifact")
    result = reader.read_tool_output_artifact(request)
    assert result["ok"] is True and result["content"] == "inside-original-content"


# LLM: 规范root用于查找去重，不能把认证根本身的预置symlink转成对外owner的授权。
# 函数用途: 验证首次prepare之前替换根目录也不会导入外部索引。
def test_preexisting_root_symlink_is_not_a_new_authority(tmp_path):
    root, foreign, own, outside = _archives(tmp_path)
    root.rename(root.with_name("owner-old"))
    root.symlink_to(foreign, target_is_directory=True)
    result = reader.read_tool_output_artifact(reader.ReadToolOutputArtifactRequest(root, "run-artifact:call-artifact"))
    assert not result["ok"] and result["error_code"] == "artifact_unreadable" and "content" not in result


# LLM: 真实工具入口必须保留宿主认证根的原路径，不能在内部根选择阶段resolve后自授权。
# 函数用途: 覆盖默认根及两个宿主内部根参数在prepare前被换成symlink的拒绝。
@pytest.mark.parametrize("root_key", ["", "__artifact_read_root", "__task_work_dir"])
def test_tool_root_selection_preserves_symlink_rejection(tmp_path, root_key):
    root, foreign, own, outside = _archives(tmp_path)
    root.rename(root.with_name("owner-old"))
    root.symlink_to(foreign, target_is_directory=True)
    tool = ReadArtifactTool(root, artifact_read_budget_max_chars=0)
    params = {"artifact_ref": "run-artifact:call-artifact", "run_id": "run-artifact"}
    if root_key:
        params[root_key] = str(root)
    outcome = tool.execute(params)
    result = json.loads(outcome.output)
    assert not outcome.ok and outcome.error_code == "ARTIFACT_UNREADABLE" and "content" not in result


# LLM: 在路径校验完成、正文尚未打开的函数边界替换root/父目录/叶；nofollow必须锚定认证根。
# 函数用途: 复现检查与正文打开之间的三种路径逃逸窗口。
@pytest.mark.parametrize("part", ["root", "parent", "leaf"])
def test_body_replacement_after_validation_never_reads_foreign_bytes(tmp_path, part):
    root, foreign, own, outside = _archives(tmp_path)
    fired = []
    original = reader._read_registered_artifact
    # LLM: 屏障只在认证和打开之间换路径，正文仍调用真实读取器并校验结果。
    # 函数用途: 制造确定性的root、parent或leaf置换。
    def read_registered(read):
        if not fired:
            fired.append(part)
            source, target = (root, foreign) if part == "root" else (own.parent, outside.parent)
            if part == "leaf":
                own.unlink()
                own.symlink_to(outside)
            else:
                source.rename(source.with_name(source.name + "-old"))
                source.symlink_to(target, target_is_directory=True)
        return original(read)
    with patch.object(reader, "_read_registered_artifact", read_registered):
        result = reader.read_tool_output_artifact(reader.ReadToolOutputArtifactRequest(root, "run-artifact:call-artifact"))
    assert fired == [part]
    assert not result["ok"] and "content" not in result


# LLM: 分别在安全打开前和已持有FD后换叶；拒绝symlink或继续读原FD，均不能导入外owner记录。
# 函数用途: 验证索引的检查/打开窗口及流包装窗口都不按可变路径读取。
@pytest.mark.parametrize("stage", ["before_safe_open", "after_safe_open"])
def test_index_leaf_replacement_does_not_import_foreign_record(tmp_path, stage):
    root, foreign, own, outside = _archives(tmp_path)
    index = own.parent / "index.jsonl"
    foreign_index = outside.parent / "foreign-index.jsonl"
    foreign_index.write_text(json.dumps({"path": str(own), "call_id": "foreign-only"}) + "\n")
    owner = reader if stage == "before_safe_open" else Path
    name = "open_readonly_file_beneath" if stage == "before_safe_open" else "open"
    original = getattr(owner, name)
    fired = []
    # LLM: 真实安全打开前拦认证根和相对组件，FD包装时拦展示路径，不替代文件读取。
    # 函数用途: 在指定索引打开阶段换入外部索引链接。
    def open_path(path, *args, **kwargs):
        target = Path(path).joinpath(*args[0]) if stage == "before_safe_open" else path
        if target.name == index.name and target.parent == index.parent and not fired:
            fired.append(True)
            index.unlink()
            index.symlink_to(foreign_index)
        return original(path, *args, **kwargs)
    with patch.object(owner, name, open_path):
        result = reader.read_tool_output_artifact(reader.ReadToolOutputArtifactRequest(root, "foreign-only"))
    assert fired == [True]
    assert not result["ok"] and "content" not in result


# LLM: 根内alias沿旧合同可用，最终仍从认证根逐段打开解析后的普通单链接目标。
# 函数用途: 保留正常alias读取，拒绝硬链接正文及缺少严格能力时的静默降级。
@pytest.mark.parametrize("shape", ["alias", "hardlink", "unavailable"])
def test_safe_open_compatibility_and_fail_closed(tmp_path, shape):
    root, foreign, own, outside = _archives(tmp_path)
    if shape == "alias":
        alias = own.with_name("alias.json")
        alias.symlink_to(own)
        request = reader.ReadToolOutputArtifactRequest(root, str(alias))
        result = reader.read_tool_output_artifact(request)
        assert result["ok"] and result["content"] == "inside-original-content"
        return
    if shape == "hardlink":
        own.chmod(0o600)
        try:
            os.link(own, own.with_name("second-link"))
        except PermissionError as exc:
            if exc.errno == EPERM:
                pytest.skip("当前执行环境创建普通文件硬链接也被EPERM拒绝；需Linux车道验证真实硬链接")
            raise
        result = reader.read_tool_output_artifact(reader.ReadToolOutputArtifactRequest(root, "run-artifact:call-artifact"))
    else:
        with patch.object(nofollow_fs, "_supports_dir_fd", return_value=False):
            result = reader.read_tool_output_artifact(reader.ReadToolOutputArtifactRequest(root, "run-artifact:call-artifact"))
    assert not result["ok"] and result["error_code"] == "artifact_unreadable" and "content" not in result


# LLM: 旧wrapper优先sha合同保持，但同大小同mtime不允许绕过新一轮完整content哈希。
# 函数用途: 验证元数据未变的正文篡改仍在每次读取被识别。
def test_same_size_restored_mtime_tampering_is_rehashed(tmp_path):
    root, foreign, own, outside = _archives(tmp_path)
    request = reader.ReadToolOutputArtifactRequest(root, "run-artifact:call-artifact")
    assert reader.read_tool_output_artifact(request)["ok"]
    before = own.stat()
    raw = own.read_bytes()
    own.write_bytes(raw.replace(b"inside-original-content", b"inside-tampered-content"))
    os.utime(own, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert own.stat().st_size == before.st_size and own.stat().st_mtime_ns == before.st_mtime_ns
    result = reader.read_tool_output_artifact(request)
    assert not result["ok"] and result["error_code"] == "artifact_hash_mismatch"
