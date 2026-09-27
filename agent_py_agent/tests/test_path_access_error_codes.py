# LLM: 路径拒绝的错误码必须来自 PathAccessDecision.code，不得从消息文字猜；分阶段归类供"连续失败即停"统计。
# 模块用途: 钉住 handler 层路径拒绝报权限码而不是参数错误，以及非权限路径错误仍报参数错误。
from __future__ import annotations

from pathlib import Path

import pytest
from agent.tooling._filesystem_helpers import path_resolution_error_outcome
from agent.tooling._filesystem_read import PathAccessError


def test_registered_permission_code_is_reported_as_is() -> None:
    exc = PathAccessError("路径访问被拒绝。")
    exc.access_code = "PATH_OWNER_SCOPE_BLOCKED"

    outcome = path_resolution_error_outcome("read_file", exc)

    assert outcome.ok is False
    assert outcome.error_code == "PATH_OWNER_SCOPE_BLOCKED"
    assert outcome.failure_stage == "authorization"


def test_every_registered_permission_path_code_is_reported_as_is() -> None:
    for code in (
        "PATH_OWNER_SCOPE_BLOCKED",
        "PATH_CROSS_OWNER_BLOCKED",
        "PATH_ADMIN_GRANTS_BLOCKED",
        "PATH_DANGEROUS_ROOT_BLOCKED",
        "PATH_CREDENTIAL_FILE_BLOCKED",
    ):
        exc = PathAccessError("被拒绝。")
        exc.access_code = code

        outcome = path_resolution_error_outcome("list_files", exc)

        assert outcome.error_code == code, code
        assert outcome.failure_stage == "authorization", code


def test_plain_value_error_stays_invalid_arguments() -> None:
    outcome = path_resolution_error_outcome("read_file", ValueError("window 参数不合法"))

    assert outcome.ok is False
    assert outcome.error_code == "TOOL_INVALID_ARGUMENTS"
    assert outcome.failure_stage == ""


def test_unregistered_code_falls_back_to_invalid_arguments() -> None:
    exc = PathAccessError("未知拒绝。")
    exc.access_code = "PATH_NOT_REGISTERED_ANYWHERE"

    outcome = path_resolution_error_outcome("read_file", exc)

    assert outcome.error_code == "TOOL_INVALID_ARGUMENTS"
    assert outcome.failure_stage == ""


def test_non_permission_registered_code_falls_back() -> None:
    exc = PathAccessError("符号链接逃逸。")
    exc.access_code = "PATH_SYMLINK_ESCAPE_BLOCKED"

    outcome = path_resolution_error_outcome("read_file", exc)

    assert outcome.error_code == "TOOL_INVALID_ARGUMENTS"
    assert outcome.failure_stage == ""


def test_missing_code_attribute_is_tolerated() -> None:
    class Bare(ValueError):
        pass

    outcome = path_resolution_error_outcome("read_file", Bare("没有 code"))

    assert outcome.error_code == "TOOL_INVALID_ARGUMENTS"
    assert outcome.failure_stage == ""


def test_error_message_is_preserved() -> None:
    exc = PathAccessError("当前 owner 只能访问自己的目录和 shared 公共区。")
    exc.access_code = "PATH_OWNER_SCOPE_BLOCKED"

    outcome = path_resolution_error_outcome("read_file", exc)

    assert "只能访问自己的目录" in outcome.output


# ===== 下面是用真实工具打 owner 墙的端到端用例（不是只测 helper） =====

from agent.tooling._filesystem_find import FindFilesTool
from agent.tooling._filesystem_list import ListFilesTool
from agent.tooling._filesystem_read import FileSystemAccessOptions, ReadFileTool
from agent.tooling._filesystem_search import SearchTextTool


def _wall(tmp_path, monkeypatch):
    """造一个 owner 家目录 + 另一个 owner 的家，返回 (工具构造参数, 被拦路径)。"""
    home = tmp_path / ".my-agent"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    owner_a = home / "owners" / "feishu" / "A"
    owner_b = home / "owners" / "feishu" / "B"
    (owner_a / "work").mkdir(parents=True, exist_ok=True)
    (owner_b / "work").mkdir(parents=True, exist_ok=True)
    (owner_b / "work" / "secret.txt").write_text("别人的数据", encoding="utf-8")

    access = FileSystemAccessOptions(owner_scope_root=str(owner_a))
    blocked = owner_b / "work"
    return owner_a, access, blocked


def _outcome(tool_cls, access, owner_home, params):
    # 四个工具的签名都是 (workspace_root, <数量上限>, workspace_roots, access_options)。
    tool = tool_cls(owner_home, 8000, [owner_home], access)
    return tool.execute(params)


def test_read_file_reports_permission_code_on_owner_wall(tmp_path, monkeypatch) -> None:
    owner_home, access, blocked = _wall(tmp_path, monkeypatch)

    outcome = _outcome(ReadFileTool, access, owner_home, {"path": str(blocked / "secret.txt")})

    assert outcome.ok is False
    assert outcome.error_code == "PATH_CROSS_OWNER_BLOCKED"
    assert outcome.failure_stage == "authorization"


def test_list_files_reports_permission_code_on_owner_wall(tmp_path, monkeypatch) -> None:
    owner_home, access, blocked = _wall(tmp_path, monkeypatch)

    outcome = _outcome(ListFilesTool, access, owner_home, {"path": str(blocked)})

    assert outcome.ok is False
    assert outcome.error_code == "PATH_CROSS_OWNER_BLOCKED"
    assert outcome.failure_stage == "authorization"


def test_find_files_reports_permission_code_on_owner_wall(tmp_path, monkeypatch) -> None:
    owner_home, access, blocked = _wall(tmp_path, monkeypatch)

    outcome = _outcome(
        FindFilesTool, access, owner_home, {"path": str(blocked), "pattern": "*.txt"}
    )

    assert outcome.ok is False
    assert outcome.error_code == "PATH_CROSS_OWNER_BLOCKED"
    assert outcome.failure_stage == "authorization"


def test_search_text_reports_permission_code_on_owner_wall(tmp_path, monkeypatch) -> None:
    owner_home, access, blocked = _wall(tmp_path, monkeypatch)

    outcome = _outcome(
        SearchTextTool, access, owner_home, {"path": str(blocked), "query": "别人的"}
    )

    assert outcome.ok is False
    assert outcome.error_code == "PATH_CROSS_OWNER_BLOCKED"
    assert outcome.failure_stage == "authorization"


def test_all_four_tools_agree_on_code_and_stage(tmp_path, monkeypatch) -> None:
    """四个工具在同一个墙上必须落在同一个 (code, stage)，否则"连续失败即停"统计不到。"""
    owner_home, access, blocked = _wall(tmp_path, monkeypatch)
    cases = (
        (ReadFileTool, {"path": str(blocked / "secret.txt")}),
        (ListFilesTool, {"path": str(blocked)}),
        (FindFilesTool, {"path": str(blocked), "pattern": "*.txt"}),
        (SearchTextTool, {"path": str(blocked), "query": "别人的"}),
    )

    seen = set()
    for tool_cls, params in cases:
        outcome = _outcome(tool_cls, access, owner_home, params)
        seen.add((outcome.error_code, outcome.failure_stage))

    assert seen == {("PATH_CROSS_OWNER_BLOCKED", "authorization")}


def test_own_workspace_still_reads_normally(tmp_path, monkeypatch) -> None:
    """改造不能让合法读取变坏：自己家照常读到。"""
    owner_home, access, _ = _wall(tmp_path, monkeypatch)
    target = owner_home / "work" / "mine.txt"
    target.write_text("我自己的内容", encoding="utf-8")

    outcome = _outcome(ReadFileTool, access, owner_home, {"path": str(target)})

    assert outcome.ok is True
    assert "我自己的内容" in outcome.output


def test_type_error_still_invalid_arguments(tmp_path, monkeypatch) -> None:
    """真正的参数错误（不是权限拒绝）仍报 TOOL_INVALID_ARGUMENTS。"""
    owner_home, access, _ = _wall(tmp_path, monkeypatch)

    outcome = _outcome(ReadFileTool, access, owner_home, {"path": 12345})

    assert outcome.ok is False
    assert outcome.error_code == "TOOL_INVALID_ARGUMENTS"
    assert outcome.failure_stage == ""
