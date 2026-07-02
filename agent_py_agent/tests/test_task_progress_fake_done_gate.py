"""账本空交付一次性提醒门钉子(原"假标 done 对质门"的继任,判据更宽)。

历史形态 A3-u3:模型 stall 后把全部待办标 done 但 0 产出,静默滑进 non_terminal 失败。
§7-2 真机新形态:solo 一条龙【真干了活】(千行成品+测试全过)但经 run_command/相对路径
把成品写到任务交付区外(owner home 根)——交付区 0 产物,原 fake-done 门只认"证据不实存"
而漏掉"成品实存但落错位置",closeout 静默不触发,用户什么都收不到。

继任门 `_ledger_empty_delivery_rework`(uncontracted 一次性提醒归并入口 ④)契约:
①立过 task_progress 账 + 交付区零产物 + 没派子代理 → 第一次打回(empty_delivery_gate
  UNCONTRACTED_EMPTY_DELIVERY + 指引"成品在别处就搬进交付目录/或交不可行报告");
②幂等一次:第二次同形态不再打回、不覆盖首轮报告(诚实失败由上层出口负责,绝不死锁);
③没立账(纯聊天)→ 不触发,closeout 走 delivery_contract_missing 原路;
④账本挂 open 项 → 由 _open_todo_rework(③号一次性门)先接管,本门不重复打。
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
_MARKER = "[ledger-empty-delivery-rework]"


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


def test_ledger_empty_delivery_reworks_once_then_stays(td=None):
    with tempfile.TemporaryDirectory() as td:
        agent, params, task_root = _closeout_request(td, items=_all_done_items())
        response = _run_closeout(agent, params)
        assert response is None
        report = _closeout_report(task_root)
        assert report["ok"] is False
        assert report["empty_delivery_gate"]["finding"] == "UNCONTRACTED_EMPTY_DELIVERY"
        markers = [item for item in params.tool_context if _MARKER in str(item)]
        assert len(markers) == 1
        # 双出口:把成品/汇总搬进交付目录,或交结构化不可行报告。
        assert "交付目录" in markers[0]
        assert "不可行报告" in markers[0]

        # 第二次同形态:幂等不再打回,也不得覆盖首轮阻断报告(诚实失败由上层出口负责)。
        response = _run_closeout(agent, params)
        assert response is None
        report = _closeout_report(task_root)
        assert report["empty_delivery_gate"]["finding"] == "UNCONTRACTED_EMPTY_DELIVERY"
        assert len([item for item in params.tool_context if _MARKER in str(item)]) == 1


def test_evidence_elsewhere_still_reworks_to_move_into_delivery():
    # §7-2 真机形态:成品文件真实存在但落在交付区外 → 照样打回一次要求搬进交付目录
    # (原 fake-done 门在这形态下静默放行,正是用户"什么都收不到"的根因)。
    with tempfile.TemporaryDirectory() as td:
        real_file = Path(td) / "library_system" / "README.md"
        real_file.parent.mkdir(parents=True, exist_ok=True)
        real_file.write_text("真实产出\n", encoding="utf-8")
        agent, params, task_root = _closeout_request(td, items=_all_done_items(evidence=[str(real_file)]))
        response = _run_closeout(agent, params)
        assert response is None
        assert _closeout_report(task_root)["empty_delivery_gate"]["finding"] == "UNCONTRACTED_EMPTY_DELIVERY"
        assert any(_MARKER in str(item) for item in params.tool_context)


def test_open_items_do_not_trigger_ledger_empty_gate():
    # 账本挂 open 项=活没干完:不算"立账终态"信号,不进本门(走原非阻塞出口/续跑,
    # R6a 语义:派完子代理等调度的 run 不得被拉进 closeout 打回;显式提交时由
    # ③_open_todo_rework 接管)。
    with tempfile.TemporaryDirectory() as td:
        open_items = [*_all_done_items(3), {"id": "item-open", "title": "还没做完", "status": "in_progress"}]
        agent, params, task_root = _closeout_request(td, items=open_items)
        response = _run_closeout(agent, params)
        assert response is None
        assert not any(_MARKER in str(item) for item in params.tool_context)
        assert _closeout_report(task_root)["reason"] == "delivery_contract_missing"


def test_not_triggered_without_progress_ledger():
    # 没立账(纯聊天/说明类):不触发,保持 delivery_contract_missing 原路,零打扰。
    with tempfile.TemporaryDirectory() as td:
        agent, params, task_root = _closeout_request(td, items=[], write_progress=False)
        response = _run_closeout(agent, params)
        assert response is None
        assert _closeout_report(task_root)["reason"] == "delivery_contract_missing"
        assert not any(_MARKER in str(item) for item in params.tool_context)
