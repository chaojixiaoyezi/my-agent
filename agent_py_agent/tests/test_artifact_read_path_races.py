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
