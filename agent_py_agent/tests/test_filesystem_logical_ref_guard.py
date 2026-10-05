"""逻辑引用编号被当写路径的守卫（artrefguard）：只使用临时工作区，不碰真实 owner home。"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_py_agent.agent.common.logical_reference_ids import (
    GATEWAY_REQUEST_ID_PREFIX,
    RUN_REQUEST_ID_PREFIX,
    is_logical_reference_segment,
)
from agent_py_agent.agent.tooling._filesystem_edit import EditFileTool
from agent_py_agent.agent.tooling._filesystem_patch import ApplyPatchTool
from agent_py_agent.agent.tooling._filesystem_read import FileSystemTool, WriteScopeError
from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool
from agent_py_agent.agent.tooling.filesystem_artifact_guard import (
    logical_reference_write_path_error,
)


# LLM: 判定只看结构化形态：产品编号前缀 + ":call_"；普通文件名、只有前缀没有冒号、只有冒号没有前缀都不命中。
# 函数用途: 锁定守卫的命中集合，防止把正常相对路径误伤。
def test_logical_reference_segment_detection() -> None:
    scoped = f"{GATEWAY_REQUEST_ID_PREFIX}1791207296-571ad5c6e0e4480c91296df966ab426d:call_00_abc"
    assert is_logical_reference_segment(scoped) is True
    assert is_logical_reference_segment(f"{RUN_REQUEST_ID_PREFIX}1781948513617517000:call_function_x_2") is True
    assert is_logical_reference_segment("subagent-run:xxx:call_01_yyy") is True  # 复合 scope 仍以产品前缀开头
    assert is_logical_reference_segment("notes.md") is False
    assert is_logical_reference_segment("gwreq-notes.md") is False  # 前缀像但没有冒号+call
    assert is_logical_reference_segment("report:call_log.md") is False  # 有冒号+call 但没有产品前缀
    assert is_logical_reference_segment("") is False


# LLM: 守卫只拦相对路径；绝对路径由既有访问策略处理，blobs/tool_outputs 重定向保持原样。
# 函数用途: 锁定"绝对路径不拦截"与多段相对路径的中间段也会被拦。
def test_write_path_error_scope(tmp_path: Path) -> None:
    scoped = f"{GATEWAY_REQUEST_ID_PREFIX}x:call_y"
    assert logical_reference_write_path_error(f"/abs/{scoped}/notes.md") == ""
    assert scoped in logical_reference_write_path_error(f"{scoped}/notes.md")
    assert scoped in logical_reference_write_path_error(f"out/{scoped}/notes.md")  # 中间段同样拦
    assert logical_reference_write_path_error("notes/plain.md") == ""


# LLM: 集成层走真实写入口 resolve_write_path（write_file/apply_patch/edit_file 共用），证明拒绝发生在解析与落盘之前。
# 函数用途: 相对路径命中逻辑引用时统一写入口结构化拒绝且零落盘。
def test_write_file_rejects_logical_reference_relative_path(tmp_path: Path) -> None:
    tool = FileSystemTool(tmp_path)
    scoped = f"{GATEWAY_REQUEST_ID_PREFIX}x:call_y"
    with pytest.raises(WriteScopeError) as raised:
        tool.resolve_write_path(f"{scoped}/notes.md")
    assert raised.value.access_code == "ARTIFACT_REF_AS_WRITE_PATH"
    assert "read_artifact" in str(raised.value)
    assert not (tmp_path / scoped).exists()  # 零落盘：没有创建目录


# LLM: 正常相对路径（含产品前缀字面量但无逻辑引用形态）必须照常解析与写入，守卫不能把任务产物路径误伤。
# 函数用途: 回归正常写路径仍然成功且落在工作区里。
def test_normal_relative_path_still_writes(tmp_path: Path) -> None:
    tool = FileSystemTool(tmp_path)
    target = tool.resolve_write_path("notes/gwreq-notes.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("ok", encoding="utf-8")
    assert (tmp_path / "notes" / "gwreq-notes.md").read_text(encoding="utf-8") == "ok"


# LLM: 校验真实文件工具均将路径错误作为结构化失败返回，并在任何创建/修改前拦截；回执不可包含宿主临时根。
# 函数用途: 验证 write_file、edit_file、apply_patch 三个公开写入口拒绝逻辑引用且错误码对模型可见。
def test_all_write_tools_reject_logical_reference_without_disk_effect(tmp_path: Path) -> None:
    scoped = f"{GATEWAY_REQUEST_ID_PREFIX}x:call_y"
    logical_dir = tmp_path / scoped
    logical_dir.mkdir()
    existing = logical_dir / "existing.md"
    existing.write_text("before\n", encoding="utf-8")
    write_target = logical_dir / "new.md"
    patch_target = logical_dir / "patch.md"
    patch = (
        "*** Begin Patch\n"
        f"*** Add File: {scoped}/patch.md\n"
        "+new\n"
        "*** End Patch"
    )
    outcomes = (
        WriteFileTool(tmp_path).execute({"path": f"{scoped}/new.md", "content": "new"}),
        EditFileTool(tmp_path).execute(
            {"path": f"{scoped}/existing.md", "old_string": "before", "new_string": "after"}
        ),
        ApplyPatchTool(tmp_path).execute({"patch": patch}),
    )
    for outcome in outcomes:
        assert outcome.ok is False
        assert outcome.error_code == "ARTIFACT_REF_AS_WRITE_PATH"
        assert outcome.reported_error_code == "ARTIFACT_REF_AS_WRITE_PATH"
        prompt = outcome.render_for_prompt()
        assert "error_code=ARTIFACT_REF_AS_WRITE_PATH" in prompt
        assert str(tmp_path) not in prompt
    assert existing.read_text(encoding="utf-8") == "before\n"
    assert not write_target.exists()
    assert not patch_target.exists()
