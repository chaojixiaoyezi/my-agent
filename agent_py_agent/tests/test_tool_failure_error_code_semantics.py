from __future__ import annotations

"""防回归：工具失败路径的错误码语义准确性(ntu-stage2 审计修复)。

背景:错误码暗示的恢复动作(recommended_action)必须与真实失败原因一致，否则会把
模型往沟里带——典型坑:
  - 资源/状态问题(路径不存在/文件缺失)却用 TOOL_INVALID_ARGUMENTS(暗示"改参数格式")，
    模型会反复纠结参数而非改路径/先定位。
  - 失败返回漏传 error_code → ToolHandlerOutcome.__post_init__ 兜底成 UNKNOWN_ERROR
    (retryable=False, report_blocker)，模型被告知"未知失败、报阻塞、别重试"，
    而实际是改参数/换工具就能修的。

本组守住审计直接修复的 8 条路径。参照 COMMAND_TOO_LONG 的修法(精确码 + 注册 + 对齐
retryable/recommended_action)。
"""

from pathlib import Path

from agent_py_agent.agent.tooling._filesystem_edit import EditFileTool
from agent_py_agent.agent.tooling._filesystem_find import FindFilesTool
from agent_py_agent.agent.tooling._filesystem_list import ListFilesTool
from agent_py_agent.agent.tooling._filesystem_patch import ApplyPatchTool
from agent_py_agent.agent.tooling._filesystem_search import SearchTextTool


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


# ---- read 工具:参数错→TOOL_INVALID_ARGUMENTS;路径不存在→PATH_NOT_FOUND(都不能是 UNKNOWN_ERROR) ----


def test_find_files_missing_path_is_path_not_found(tmp_path: Path):
    tool = FindFilesTool(_workspace(tmp_path), max_matches=50)
    r = tool.execute({"path": "does_not_exist_dir", "pattern": "*.py"})
    assert r.ok is False
    assert r.error_code == "PATH_NOT_FOUND", r.error_code
    assert r.error_code != "UNKNOWN_ERROR"
    assert r.retryable is True


def test_find_files_bad_args_is_invalid_arguments(tmp_path: Path):
    tool = FindFilesTool(_workspace(tmp_path), max_matches=50)
    # limit=0 触发 _int_param(min_value=1) 的 ValueError(参数问题，改参可修)
    r = tool.execute({"pattern": "*", "limit": 0})
    assert r.ok is False
    assert r.error_code == "TOOL_INVALID_ARGUMENTS", r.error_code
    assert r.error_code != "UNKNOWN_ERROR"


def test_list_files_bad_args_is_invalid_arguments(tmp_path: Path):
    tool = ListFilesTool(_workspace(tmp_path), max_entries=100)
    r = tool.execute({"path": ".", "limit": 0})
    assert r.ok is False
    assert r.error_code == "TOOL_INVALID_ARGUMENTS", r.error_code
    assert r.error_code != "UNKNOWN_ERROR"


def test_list_files_missing_path_is_path_not_found(tmp_path: Path):
    tool = ListFilesTool(_workspace(tmp_path), max_entries=100)
    r = tool.execute({"path": "nope"})
    assert r.ok is False
    assert r.error_code == "PATH_NOT_FOUND", r.error_code


def test_search_text_bad_args_is_invalid_arguments(tmp_path: Path):
    tool = SearchTextTool(_workspace(tmp_path), max_matches=50)
    # limit=0 触发 _int_param(min_value=1) 的 ValueError(参数问题)
    r = tool.execute({"path": ".", "query": "x", "limit": 0})
    assert r.ok is False
    assert r.error_code == "TOOL_INVALID_ARGUMENTS", r.error_code
    assert r.error_code != "UNKNOWN_ERROR"


def test_search_text_invalid_regex_is_invalid_arguments(tmp_path: Path):
    ws = _workspace(tmp_path)
    (ws / "a.txt").write_text("hello")
    tool = SearchTextTool(ws, max_matches=50)
    # literal=False + 非法正则在 SearchMatcher.from_request 抛 ValueError(正则表达式无效)
    # → 应是改 pattern 可修的 TOOL_INVALID_ARGUMENTS，不是 UNKNOWN_ERROR
    r = tool.execute({"path": ".", "query": "(unclosed", "literal": False})
    assert r.ok is False
    assert r.error_code == "TOOL_INVALID_ARGUMENTS", r.error_code
    assert r.error_code != "UNKNOWN_ERROR"


# ---- edit_file:文件不存在是状态问题→PATH_NOT_FOUND(不是改 old_string 格式) ----


def test_edit_file_missing_file_is_path_not_found(tmp_path: Path):
    tool = EditFileTool(_workspace(tmp_path))
    r = tool.execute({"path": "ghost.py", "old_string": "a", "new_string": "b"})
    assert r.ok is False
    assert r.error_code == "PATH_NOT_FOUND", r.error_code
    assert r.error_code != "TOOL_INVALID_ARGUMENTS"
    assert r.retryable is True


def test_edit_file_same_string_still_invalid_arguments(tmp_path: Path):
    # 真正的参数问题(old==new)仍应是 TOOL_INVALID_ARGUMENTS,没有被误伤
    tool = EditFileTool(_workspace(tmp_path))
    r = tool.execute({"path": "x.py", "old_string": "same", "new_string": "same"})
    assert r.ok is False
    assert r.error_code == "TOOL_INVALID_ARGUMENTS", r.error_code


def test_edit_file_string_not_found_is_invalid_arguments(tmp_path: Path):
    # 文件存在但 old_string 匹配不到→这才是"改 old_string"能修的 TOOL_INVALID_ARGUMENTS
    ws = _workspace(tmp_path)
    (ws / "real.py").write_text("alpha\n")
    tool = EditFileTool(ws)
    r = tool.execute({"path": "real.py", "old_string": "zzz_not_present", "new_string": "b"})
    assert r.ok is False
    assert r.error_code == "TOOL_INVALID_ARGUMENTS", r.error_code


# ---- apply_patch:Update/Delete 目标文件不存在→PATH_NOT_FOUND;解析/上下文错→TOOL_INVALID_ARGUMENTS ----


def test_apply_patch_update_missing_target_is_path_not_found(tmp_path: Path):
    tool = ApplyPatchTool(_workspace(tmp_path))
    patch = (
        "*** Begin Patch\n"
        "*** Update File: ghost.txt\n"
        "-old\n"
        "+new\n"
        "*** End Patch\n"
    )
    r = tool.execute({"patch": patch})
    assert r.ok is False
    assert r.error_code == "PATH_NOT_FOUND", r.error_code
    assert r.error_code != "TOOL_INVALID_ARGUMENTS"


def test_apply_patch_delete_missing_target_is_path_not_found(tmp_path: Path):
    tool = ApplyPatchTool(_workspace(tmp_path))
    patch = "*** Begin Patch\n*** Delete File: ghost.txt\n*** End Patch\n"
    r = tool.execute({"patch": patch})
    assert r.ok is False
    assert r.error_code == "PATH_NOT_FOUND", r.error_code


def test_apply_patch_malformed_is_invalid_arguments(tmp_path: Path):
    # 补丁格式错(缺 Begin Patch)是改补丁文本能修的→保持 TOOL_INVALID_ARGUMENTS,没被误伤成 PATH_NOT_FOUND
    tool = ApplyPatchTool(_workspace(tmp_path))
    r = tool.execute({"patch": "not a patch at all"})
    assert r.ok is False
    assert r.error_code == "TOOL_INVALID_ARGUMENTS", r.error_code


def test_apply_patch_unified_diff_explains_the_supported_headers(tmp_path: Path):
    tool = ApplyPatchTool(_workspace(tmp_path))
    patch = (
        "*** Begin Patch\n"
        "--- a/notes.txt\n"
        "+++ b/notes.txt\n"
        "*** End Patch\n"
    )

    r = tool.execute({"patch": patch})

    assert r.ok is False
    assert r.error_code == "TOOL_INVALID_ARGUMENTS", r.error_code
    assert "*** Update File:" in r.output


def test_apply_patch_context_mismatch_is_invalid_arguments(tmp_path: Path):
    # 文件存在但补丁上下文未命中→改补丁文本能修→TOOL_INVALID_ARGUMENTS(不是 PATH_NOT_FOUND)
    ws = _workspace(tmp_path)
    (ws / "real.txt").write_text("actual content\n")
    tool = ApplyPatchTool(ws)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: real.txt\n"
        "-totally different\n"
        "+new\n"
        "*** End Patch\n"
    )
    r = tool.execute({"patch": patch})
    assert r.ok is False
    assert r.error_code == "TOOL_INVALID_ARGUMENTS", r.error_code


# ---- wait 工具:store 缺失/缺 task_id 是可修复的，不能兜底成 UNKNOWN_ERROR ----


class _StoreWithoutPolicy:
    """有 conversation_store 但没有 set_progress_policy(不满足 wait 需要的能力)。"""


class _StoreWithPolicy:
    def set_progress_policy(self, *args, **kwargs):  # noqa: D401 - 仅供 callable 检测
        return None

    def thread_for_task(self, task_id):
        return None


class _AgentNoStore:
    conversation_store = None
    _current_run_params = None
    _main_agent_run_id = ""


class _AgentStoreNoPolicy:
    conversation_store = _StoreWithoutPolicy()
    _current_run_params = None
    _main_agent_run_id = ""


class _AgentNoTaskId:
    conversation_store = _StoreWithPolicy()
    _current_run_params = None
    _main_agent_run_id = ""


def test_wait_conversation_store_unavailable_is_tool_unavailable():
    from agent_py_agent.agent.agent_core.runtime.wait_tool import _target

    for agent in (_AgentNoStore(), _AgentStoreNoPolicy()):
        result = _target(agent, {})
        assert not isinstance(result, tuple)
        assert result.ok is False
        assert result.error_code == "TOOL_UNAVAILABLE", result.error_code
        assert result.error_code != "UNKNOWN_ERROR"


def test_wait_missing_task_id_is_parameter_required():
    from agent_py_agent.agent.agent_core.runtime.wait_tool import _target

    result = _target(_AgentNoTaskId(), {})
    assert not isinstance(result, tuple)
    assert result.ok is False
    assert result.error_code == "TOOL_PARAMETER_REQUIRED", result.error_code
    assert result.error_code != "UNKNOWN_ERROR"
    assert result.retryable is True


# ============================================================================
# ntu-stage2 收口轮：编排工具族 / web HTTP / registry 畸形 payload 的错误码语义
# （第二批"误导模型"剩余项；参照上面 read/edit/wait 同一修法）
# ============================================================================

import urllib.error  # noqa: E402

from agent_py_agent.agent.core import SimpleAgent  # noqa: E402
from agent_py_agent.agent.settings import AgentConfig  # noqa: E402


def _agent(tmp_path: Path) -> SimpleAgent:
    return SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)


# ---- web_fetch HTTP 4xx 细分：401/403 鉴权→PERMISSION_BLOCKED(不可重试,别空转改 URL) ----


def _http_error_result(status: int):
    from agent_py_agent.agent.tooling.web import _format_http_error

    err = urllib.error.HTTPError(url="https://api.example.com", code=status, msg="x", hdrs={}, fp=None)
    err.read = lambda *_a, **_k: b"body"  # type: ignore[assignment]
    return _format_http_error("web_fetch", err, 1000)


def test_web_fetch_401_is_permission_blocked():
    r = _http_error_result(401)
    assert r.error_code == "PERMISSION_BLOCKED", r.error_code
    # 鉴权失败不可重试：避免模型反复改 URL/header 空转。
    assert r.retryable is False


def test_web_fetch_403_is_permission_blocked():
    r = _http_error_result(403)
    assert r.error_code == "PERMISSION_BLOCKED", r.error_code
    assert r.retryable is False


def test_web_fetch_404_stays_invalid_arguments():
    # 404 用 TOOL_INVALID_ARGUMENTS(改 URL)，不用 PATH_NOT_FOUND(其 hint 指向文件系统会误导)。
    r = _http_error_result(404)
    assert r.error_code == "TOOL_INVALID_ARGUMENTS", r.error_code


def test_web_fetch_400_and_422_stay_invalid_arguments():
    for status in (400, 422):
        r = _http_error_result(status)
        assert r.error_code == "TOOL_INVALID_ARGUMENTS", (status, r.error_code)


def test_web_fetch_5xx_and_429_stay_retryable_network():
    for status in (500, 503, 408, 429):
        r = _http_error_result(status)
        assert r.error_code == "NETWORK_REQUEST_FAILED", (status, r.error_code)
        assert r.retryable is True


# ---- raise_event：运行时 lookup/load 异常→TOOL_EXECUTION_FAILED；缺参→TOOL_PARAMETER_REQUIRED ----


def test_raise_event_runtime_lookup_failure_is_execution_failed(tmp_path: Path, monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration_tools import RaiseEventTool

    agent = _agent(tmp_path)

    def boom(_task_id):
        raise ValueError("index broken")

    monkeypatch.setattr(agent.conversation_store, "thread_for_task", boom)
    r = RaiseEventTool(agent).execute({"task_id": "task-x", "summary": "s"})
    assert r.ok is False
    assert r.error_code == "TOOL_EXECUTION_FAILED", r.error_code
    assert r.error_code != "TOOL_INVALID_ARGUMENTS"
    assert r.retryable is True


def test_raise_event_missing_thread_is_parameter_required(tmp_path: Path):
    from agent_py_agent.agent.agent_core.orchestration_tools import RaiseEventTool

    # 既无 thread_id 也无 task_id → 缺必填参数(不是参数无效，更不是 UNKNOWN_ERROR)。
    r = RaiseEventTool(_agent(tmp_path)).execute({"summary": "s"})
    assert r.ok is False
    assert r.error_code == "TOOL_PARAMETER_REQUIRED", r.error_code
    assert r.retryable is True


# ---- cancel_subagents：缺选择器→TOOL_PARAMETER_REQUIRED；status 非法→TOOL_INVALID_ARGUMENTS ----


def test_cancel_subagents_no_selector_is_parameter_required(tmp_path: Path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CancelSubagentsTool

    r = CancelSubagentsTool(_agent(tmp_path)).execute({})
    assert r.ok is False
    assert r.error_code == "TOOL_PARAMETER_REQUIRED", r.error_code
    assert r.error_code != "UNKNOWN_ERROR"


def test_cancel_subagents_invalid_status_is_invalid_arguments(tmp_path: Path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CancelSubagentsTool

    r = CancelSubagentsTool(_agent(tmp_path)).execute({"status": "not_a_real_status"})
    assert r.ok is False
    assert r.error_code == "TOOL_INVALID_ARGUMENTS", r.error_code
    assert r.error_code != "UNKNOWN_ERROR"


# ---- dispatch_subagents：不支持的执行开关→TOOL_INVALID_ARGUMENTS（不是 UNKNOWN_ERROR） ----


def test_dispatch_unsupported_exec_param_is_invalid_arguments(tmp_path: Path):
    from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

    r = DispatchSubagentsTool(_agent(tmp_path)).execute({"apply": True})
    assert r.ok is False
    assert r.error_code == "TOOL_INVALID_ARGUMENTS", r.error_code
    assert r.error_code != "UNKNOWN_ERROR"


# ---- resolve_capability_requests：缺参→TOOL_PARAMETER_REQUIRED；run_id 不存在→TOOL_INVALID_ARGUMENTS ----


def test_resolve_capability_missing_params_is_parameter_required(tmp_path: Path):
    from agent_py_agent.agent.agent_core.orchestration_tools import ResolveCapabilityRequestsTool

    r = ResolveCapabilityRequestsTool(_agent(tmp_path)).execute({})
    assert r.ok is False
    assert r.error_code == "TOOL_PARAMETER_REQUIRED", r.error_code
    assert r.error_code != "UNKNOWN_ERROR"


def test_resolve_capability_missing_run_is_invalid_arguments(tmp_path: Path):
    from agent_py_agent.agent.agent_core.orchestration_tools import ResolveCapabilityRequestsTool

    r = ResolveCapabilityRequestsTool(_agent(tmp_path)).execute(
        {"run_id": "ghost-run", "decision": "grant", "reason": "ok"}
    )
    assert r.ok is False
    assert r.error_code == "TOOL_INVALID_ARGUMENTS", r.error_code
    assert r.error_code != "UNKNOWN_ERROR"


# ---- send_guidance：缺 message/target→TOOL_PARAMETER_REQUIRED；scope 非法→TOOL_INVALID_ARGUMENTS ----


def test_send_guidance_missing_message_is_parameter_required(tmp_path: Path):
    from agent_py_agent.agent.agent_core.orchestration_tools import SendGuidanceTool

    r = SendGuidanceTool(_agent(tmp_path)).execute({"target_type": "agent_run", "target_id": "r1"})
    assert r.ok is False
    assert r.error_code == "TOOL_PARAMETER_REQUIRED", r.error_code
    assert r.error_code != "UNKNOWN_ERROR"


def test_send_guidance_unknown_scope_is_invalid_arguments(tmp_path: Path):
    from agent_py_agent.agent.agent_core.orchestration_tools import SendGuidanceTool

    # target_scope 取值非法(不是 children/descendants)→改参数可修。
    r = SendGuidanceTool(_agent(tmp_path)).execute({"message": "hi", "target_scope": "bogus_scope"})
    assert r.ok is False
    assert r.error_code == "TOOL_INVALID_ARGUMENTS", r.error_code
    assert r.error_code != "UNKNOWN_ERROR"
