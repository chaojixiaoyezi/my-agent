"""系统级工具失败账本钉子(开发计划 A1,根治 R4b 模型归因幻觉)。

钉死三层契约:
1. 提取层:tool_failures_from_archive 只认 archive 记录的系统 ok=False,
   不读模型文本;target 从参数提取;超量截断。
2. 落盘层:record_runner_result 把账本写进 task.attributes 并真实落盘;
   [] 表示"系统确认零失败"(写空账本),None 表示"拿不到数据"(不覆盖旧账本)。
3. 对照层(R4b 核心):模型 message 声称 WRITE_FORBIDDEN 而系统账本为空时,
   closeout 的 unresolved_children 投影 tool_failure_codes=={} ——系统事实
   与模型转述并排可见,幻觉无法蒙混。账本只观测,不做硬门。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_py_agent.agent.agent_core.delivery_closeout.subagent_aggregation import (
    _compact_children,
)
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.manager_runner_result_payload import (
    RecordRunnerResultParams,
)
from agent_py_agent.agent.subagents.services.base import CreateRunParams
from agent_py_agent.agent.subagents.tool_failure_ledger import (
    TOOL_FAILURE_LEDGER_ATTR,
    record_tool_failure_ledger,
    tool_failure_code_counts,
    tool_failures_from_archive,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# 1. 提取层:只认系统 ok=False
# ---------------------------------------------------------------------------


def test_tool_failures_from_archive_only_records_system_failures() -> None:
    archive = [
        {"tool": "write_file", "call_id": "3-1", "ok": True, "parameters": {"path": "/a.py"}},
        {
            "tool": "write_file",
            "call_id": "5-1",
            "ok": False,
            "error_code": "WRITE_FORBIDDEN",
            "parameters": {"path": "/out/core/__init__.py"},
        },
        {"tool": "run_command", "call_id": "6-2", "ok": False, "error_code": "", "parameters": {"command": "ls"}},
        "garbage-not-a-dict",
        {"tool": "read_file", "ok": True},
    ]
    failures = tool_failures_from_archive(archive)
    assert failures == [
        {
            "tool": "write_file",
            "call_id": "5-1",
            "error_code": "WRITE_FORBIDDEN",
            "target": "/out/core/__init__.py",
            "message": "",
        },
        {"tool": "run_command", "call_id": "6-2", "error_code": "", "target": "", "message": ""},
    ]


def test_tool_failures_from_archive_none_and_empty_yield_empty() -> None:
    assert tool_failures_from_archive(None) == []
    assert tool_failures_from_archive([]) == []


def test_tool_failures_from_archive_truncates_runaway_loops() -> None:
    archive = [
        {"tool": "write_file", "call_id": f"r-{i}", "ok": False, "error_code": "E", "parameters": {}}
        for i in range(200)
    ]
    assert len(tool_failures_from_archive(archive)) == 50


# ---------------------------------------------------------------------------
# 2. 落盘层:None 不覆盖 / [] 写空账本 / 失败清单真实落盘
# ---------------------------------------------------------------------------


def _make_manager(tmp_path: Path) -> SubAgentManager:
    return SubAgentManager(workspace=tmp_path / "ws")


def _create_task(manager: SubAgentManager):
    return manager.create_run(
        params=CreateRunParams(goal="账本钉子任务", thought="t", plan=["p1"], role="worker")
    )


def _record_params(run_id: str, *, message: str, tool_failures, ok: bool = False) -> RecordRunnerResultParams:
    return RecordRunnerResultParams(
        run_id=run_id,
        dry_run=False,
        ok=ok,
        message=message,
        status="BLOCKED" if not ok else "DONE",
        verification_status="UNVERIFIED",
        tool_failures=tool_failures,
    )


def test_record_runner_result_persists_failure_ledger_to_disk(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    task = _create_task(manager)
    failures = [
        {"tool": "write_file", "call_id": "5-1", "error_code": "WRITE_FORBIDDEN", "target": "/out/a.py"},
        {"tool": "write_file", "call_id": "7-1", "error_code": "WRITE_FORBIDDEN", "target": "/out/b.py"},
    ]

    manager.runner_result.record_runner_result(
        _record_params(task.id, message="写入失败", tool_failures=failures)
    )

    reloaded = manager.load(task.id)
    ledger = reloaded.attributes[TOOL_FAILURE_LEDGER_ATTR]
    assert ledger["failures"] == failures
    assert ledger["updated_at"] > 0
    assert tool_failure_code_counts(reloaded.attributes) == {"WRITE_FORBIDDEN": 2}


def test_record_runner_result_empty_list_means_system_confirmed_zero_failures(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    task = _create_task(manager)

    manager.runner_result.record_runner_result(
        _record_params(task.id, message="模型口头声称 WRITE_FORBIDDEN(系统未记录)", tool_failures=[])
    )

    reloaded = manager.load(task.id)
    ledger = reloaded.attributes[TOOL_FAILURE_LEDGER_ATTR]
    assert ledger["failures"] == []
    assert tool_failure_code_counts(reloaded.attributes) == {}


def test_record_runner_result_none_keeps_previous_ledger(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    task = _create_task(manager)
    failures = [{"tool": "write_file", "call_id": "1-1", "error_code": "WRITE_FORBIDDEN", "target": "/x"}]
    manager.runner_result.record_runner_result(
        _record_params(task.id, message="第一轮真实失败", tool_failures=failures)
    )

    # 第二轮超时/worker 异常路径拿不到 archive(None):不得伪造"零失败"覆盖旧账本。
    manager.runner_result.record_runner_result(
        _record_params(task.id, message="runner timeout", tool_failures=None)
    )

    reloaded = manager.load(task.id)
    assert reloaded.attributes[TOOL_FAILURE_LEDGER_ATTR]["failures"] == failures


def test_record_ledger_helper_overwrites_per_attempt() -> None:
    class _Task:
        def __init__(self) -> None:
            self.attributes: dict = {}

    task = _Task()
    record_tool_failure_ledger(task, [{"tool": "a", "call_id": "1", "error_code": "X", "target": ""}], 1.0)
    record_tool_failure_ledger(task, [], 2.0)
    assert task.attributes[TOOL_FAILURE_LEDGER_ATTR] == {"updated_at": 2.0, "failures": []}


# ---------------------------------------------------------------------------
# 3. 对照层(R4b 核心):closeout 投影让"模型转述 vs 系统事实"并排可见
# ---------------------------------------------------------------------------


def test_aggregation_projection_exposes_hallucinated_failure_claims(tmp_path: Path) -> None:
    """R4b 形态钉子:模型 summary 声称 WRITE_FORBIDDEN,系统账本为空 → 投影 {}。"""
    manager = _make_manager(tmp_path)
    task = _create_task(manager)
    manager.runner_result.record_runner_result(
        _record_params(
            task.id,
            message="write_file 返回 WRITE_FORBIDDEN,目录被 locked_files 锁定",  # 模型口头转述
            tool_failures=[],  # 系统事实:零失败
        )
    )
    reloaded = manager.load(task.id)

    child_item = {
        "run_id": reloaded.id,
        "status": reloaded.status,
        "latest_summary": "所有 write_file 调用均返回 WRITE_FORBIDDEN",
        "attributes": dict(reloaded.attributes),
    }
    rows = _compact_children([child_item])
    assert rows[0]["tool_failure_codes"] == {}, (
        "系统账本为空时投影必须为 {}:模型转述的 WRITE_FORBIDDEN 与系统事实的"
        "矛盾要在 closeout 报告里直接可见"
    )


def test_aggregation_projection_counts_real_system_failures(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    task = _create_task(manager)
    manager.runner_result.record_runner_result(
        _record_params(
            task.id,
            message="写入被边界拒绝",
            tool_failures=[
                {"tool": "write_file", "call_id": "5-1", "error_code": "WRITE_FORBIDDEN", "target": "/out/a.py"},
                {"tool": "write_file", "call_id": "6-1", "error_code": "WRITE_FORBIDDEN", "target": "/out/b.py"},
                {"tool": "run_command", "call_id": "7-1", "error_code": "", "target": ""},
            ],
        )
    )
    reloaded = manager.load(task.id)
    rows = _compact_children([{"run_id": reloaded.id, "attributes": dict(reloaded.attributes)}])
    assert rows[0]["tool_failure_codes"] == {"WRITE_FORBIDDEN": 2, "UNSPECIFIED": 1}


# ---------------------------------------------------------------------------
# 4. finalize 接线:AgentRunResult.archive_tool_calls → tool_failures 参数
# ---------------------------------------------------------------------------


def test_finalize_extracts_failures_from_agent_run_result_archive() -> None:
    from agent_py_agent.agent.agent_core.models import AgentRunResult

    result = AgentRunResult(
        prompt="p",
        response="r",
        backend="echo",
        used_memories=0,
        archive_tool_calls=[
            {"tool": "write_file", "call_id": "2-1", "ok": False, "error_code": "WRITE_FORBIDDEN", "parameters": {"path": "/out/x.py"}},
            {"tool": "read_file", "call_id": "3-1", "ok": True, "parameters": {"path": "/out/x.py"}},
        ],
    )
    failures = tool_failures_from_archive(result.archive_tool_calls)
    assert failures == [
        {"tool": "write_file", "call_id": "2-1", "error_code": "WRITE_FORBIDDEN", "target": "/out/x.py", "message": ""}
    ]


def test_agent_config_smoke_for_ledger_imports() -> None:
    """配置对象可构造(账本不需要任何新配置开关——纯观测,无行为变更)。"""
    assert AgentConfig() is not None


def test_finalize_passes_none_when_result_lacks_archive_field() -> None:
    """result 缺 archive_tool_calls(替身/异常路径)→ 必须传 None(拿不到数据),
    不得伪造 [](系统确认零失败)。语义钉子:防止 finalize 端把两者混为一谈。"""
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.subagent.finalize_helpers import (
        record_finalized_runner_result,
    )

    captured: dict = {}

    class _RunnerResultSink:
        def record_runner_result(self, params):
            captured["tool_failures"] = params.tool_failures
            return SimpleNamespace(ok=True, status="DONE")

    agent = SimpleNamespace(subagents=SimpleNamespace(runner_result=_RunnerResultSink()))
    request = SimpleNamespace(
        agent=agent,
        params=SimpleNamespace(
            run_id="r1",
            active_attempt_id="a1",
            result=SimpleNamespace(tool_rounds=1, executed_tools=[]),  # 无 archive 字段
        ),
        structured=SimpleNamespace(found=False, ok=True),
        repair_state={
            "message": "m",
            "prompt_for_log": "",
            "response_for_log": "",
            "backend_name": "test",
            "attempted": False,
            "ok": False,
            "error": "",
        },
    )

    record_finalized_runner_result(request)
    assert captured["tool_failures"] is None

    # 对照:archive 为空 list(正常轮零失败)→ 传 [](系统确认零失败)
    request.params.result = SimpleNamespace(tool_rounds=1, executed_tools=[], archive_tool_calls=[])
    record_finalized_runner_result(request)
    assert captured["tool_failures"] == []


def test_failure_entries_carry_system_rejection_message():
    """R8 接力取证实锤钉子:账本只有 error_code 时,WRITE_FORBIDDEN 的具体拒因
    (边界/锁/危险目录)无处可查;拒绝原文是决策时刻的系统事实,必须随账本留痕。"""
    from agent_py_agent.agent.subagents.tool_failure_ledger import tool_failures_from_archive

    failures = tool_failures_from_archive([
        {
            "tool": "write_file",
            "call_id": "4-1",
            "ok": False,
            "error_code": "WRITE_FORBIDDEN",
            "parameters": {"path": "/t/output/x.md"},
            "output": "写入被阻止: 目标不在允许目录内。allowed=[/t/work] target=/t/output/x.md" + "x" * 400,
        },
        {"tool": "web_search", "call_id": "5-1", "ok": False, "error_code": "TOOL_UNAVAILABLE"},
    ])

    assert failures[0]["message"].startswith("写入被阻止: 目标不在允许目录内")
    assert len(failures[0]["message"]) <= 240, "原文截断留痕,不撑爆 attributes"
    assert failures[1]["message"] == ""
