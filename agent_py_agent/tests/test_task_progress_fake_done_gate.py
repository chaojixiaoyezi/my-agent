"""任务进度账本与消息交付的边界钉子。

账本是“做了什么、还剩什么”的内部事实，不是用户要求文件的证据。没有显式
artifact contract 时，全部完成的账本允许 message 交付；挂着 open 项仍由待办收口门处理。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.delivery_closeout.closeout import (
    MainAgentDeliveryCloseoutRequest,
    main_agent_delivery_closeout_response,
)
from agent_py_agent.agent.agent_core.runtime.owner_roots import runtime_owner_root
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.task_progress import write_task_progress

_RUN_ID = "run-fake-done"


def _closeout_request(td: str, *, items: list[dict], write_progress: bool = True):
    workspace = Path(td)
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            my_agent_home=str(workspace / ".my-agent"),
            run_task_workspace_enabled=False,
        ),
        workspace,
    )
    task_root = workspace / "tasks" / "2026-07-02" / "fake-done"
    task_root.mkdir(parents=True, exist_ok=True)
    if write_progress:
        write_task_progress(
            runtime_owner_root(agent),
            _RUN_ID,
            {"summary": "全部完成", "next_action": "", "items": items},
        )
    params = SimpleNamespace(
        run_id=_RUN_ID,
        request_id="req-fake-done",
        task_id=_RUN_ID,
        tool_context=[],
        task_attributes={
            "run_workspace": {
                "task_root": str(task_root),
                "output_dir": str(task_root / "output"),
                "work_dir": str(task_root / "work"),
            }
        },
        archive_tool_calls=[],
        delivery_contract=None,
        user_prompt="做一个博客系统",
        root_user_prompt="做一个博客系统",
        executed_tools=[],
    )
    return agent, params, task_root


def _all_done_items(count: int = 4, evidence: list[str] | None = None) -> list[dict]:
    return [
        {"id": f"item-{index}", "title": f"步骤{index}", "status": "done", "evidence": list(evidence or [])}
        for index in range(count)
    ]


def _run_closeout(agent, params) -> object:
    return main_agent_delivery_closeout_response(
        MainAgentDeliveryCloseoutRequest(agent=agent, params=params, backend="test")
    )


def _closeout_report(task_root: Path) -> dict:
    return json.loads((task_root / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))


def test_done_ledger_without_artifact_contract_closes_as_message(td=None):
    with tempfile.TemporaryDirectory() as td:
        agent, params, task_root = _closeout_request(td, items=_all_done_items())
        response = _run_closeout(agent, params)
        assert response is not None
        report = _closeout_report(task_root)
        assert report["ok"] is True
        assert report["delivery_mode"] == "message"
        assert report["artifacts"] == []
        assert "empty_delivery_gate" not in report


def test_evidence_ref_does_not_turn_analysis_into_file_delivery():
    with tempfile.TemporaryDirectory() as td:
        real_file = Path(td) / "notes" / "source.txt"
        real_file.parent.mkdir(parents=True, exist_ok=True)
        real_file.write_text("分析证据\n", encoding="utf-8")
        agent, params, task_root = _closeout_request(td, items=_all_done_items(evidence=[str(real_file)]))
        response = _run_closeout(agent, params)
        assert response is not None
        assert _closeout_report(task_root)["delivery_mode"] == "message"


def test_open_items_remain_non_terminal_without_creating_file_requirement():
    with tempfile.TemporaryDirectory() as td:
        open_items = [*_all_done_items(3), {"id": "item-open", "title": "还没做完", "status": "in_progress"}]
        agent, params, task_root = _closeout_request(td, items=open_items)
        response = _run_closeout(agent, params)
        assert response is None
        assert _closeout_report(task_root)["reason"] == "delivery_contract_missing"
        assert "empty_delivery_gate" not in _closeout_report(task_root)


def test_not_triggered_without_progress_ledger():
    # 没立账(纯聊天/说明类):不触发,保持 delivery_contract_missing 原路,零打扰。
    with tempfile.TemporaryDirectory() as td:
        agent, params, task_root = _closeout_request(td, items=[], write_progress=False)
        response = _run_closeout(agent, params)
        assert response is None
        assert _closeout_report(task_root)["reason"] == "delivery_contract_missing"


def test_ledger_key_follows_task_across_wake_rounds():
    # P4(a) 机制根因钉子:账本键解析——唯一特殊分支=后台唤醒轮(source=background_main_agent)
    # 按 task_id 续主任务的账;子代理(task_id=root 主任务)与主 run 一律 run_id 原状,绝不误切。
    from agent_py_agent.agent.agent_core.delivery_closeout.task_progress_gate import _run_id

    wake = SimpleNamespace(
        agent=None,
        params=SimpleNamespace(source="background_main_agent", run_id="bg-main-thread-x", task_id="req_task_1"),
    )
    assert _run_id(wake) == "req_task_1"
    subagent = SimpleNamespace(
        agent=None,
        params=SimpleNamespace(source="subagent_run_model_turn", run_id="subagent-9", task_id="req_task_1"),
    )
    assert _run_id(subagent) == "subagent-9"
    main_run = SimpleNamespace(agent=None, params=SimpleNamespace(source="gateway", run_id="req_task_1", task_id="req_task_1"))
    assert _run_id(main_run) == "req_task_1"
