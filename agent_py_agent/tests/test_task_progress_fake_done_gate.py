"""编排稳定性 §5.2 钉子测试：假标 done + 零产物零证据的一次性返工门。

复刻 A3-u3 真机形态：模型 stall 后把全部 task_progress 待办标 done 但 0 产出，
open=0 绕过 todo 守门，静默滑进 non_terminal 失败且无人对质。新门必须：
①账本全 done、done≥3、evidence 无一实存文件、交付区零产物零子代理 → 第一次打回
（写 reason=task_progress_done_without_delivery 报告 + 注入双出口返工指令）;
②幂等一次，第二次同形态放行走 non_terminal（诚实失败合法，绝不死锁）;
③有实存证据 / 有 open 项 / done 项太少 → 不触发（不误伤）。
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
from agent_py_agent.agent.agent_core.delivery_closeout.task_progress_gate import (
    task_progress_all_done_without_artifact_evidence,
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


def test_fake_done_rework_fires_once_then_allows_honest_failure():
    with tempfile.TemporaryDirectory() as td:
        agent, params, task_root = _closeout_request(td, items=_all_done_items())
        response = _run_closeout(agent, params)
        assert response is None
        report = _closeout_report(task_root)
        assert report["ok"] is False
        assert report["reason"] == "task_progress_done_without_delivery"
        assert report["task_progress_fake_done_gate"]["evidence"]["done_count"] == 4
        marker_entries = [item for item in params.tool_context if "[task-progress-fake-done-rework]" in str(item)]
        assert len(marker_entries) == 1
        # 双出口:真建产物,或修正账本+结构化说明。
        assert "submit_for_acceptance" in marker_entries[0]
        assert "pending/blocked" in marker_entries[0]

        # 第二次同形态:幂等放行,走 non_terminal 诚实失败,不再重复打回。
        response = _run_closeout(agent, params)
        assert response is None
        report = _closeout_report(task_root)
        assert report["reason"] == "delivery_contract_missing"
        assert len([item for item in params.tool_context if "[task-progress-fake-done-rework]" in str(item)]) == 1


def test_fake_done_not_triggered_when_evidence_file_exists():
    with tempfile.TemporaryDirectory() as td:
        real_file = Path(td) / "real_evidence.md"
        real_file.write_text("真实产出\n", encoding="utf-8")
        agent, params, task_root = _closeout_request(td, items=_all_done_items(evidence=[str(real_file)]))
        assert (
            task_progress_all_done_without_artifact_evidence(
                SimpleNamespace(agent=agent, params=params), workspace_root=task_root
            )
            is None
        )
        response = _run_closeout(agent, params)
        assert response is None
        assert _closeout_report(task_root)["reason"] == "delivery_contract_missing"
        assert not any("[task-progress-fake-done-rework]" in str(item) for item in params.tool_context)


def test_fake_done_not_triggered_with_open_items_or_few_done():
    with tempfile.TemporaryDirectory() as td:
        open_items = [*_all_done_items(3), {"id": "item-open", "status": "in_progress"}]
        agent, params, task_root = _closeout_request(td, items=open_items)
        assert (
            task_progress_all_done_without_artifact_evidence(
                SimpleNamespace(agent=agent, params=params), workspace_root=task_root
            )
            is None
        )
    with tempfile.TemporaryDirectory() as td:
        agent, params, task_root = _closeout_request(td, items=_all_done_items(2))
        assert (
            task_progress_all_done_without_artifact_evidence(
                SimpleNamespace(agent=agent, params=params), workspace_root=task_root
            )
            is None
        )


def test_fake_done_not_triggered_without_progress_ledger():
    with tempfile.TemporaryDirectory() as td:
        agent, params, task_root = _closeout_request(td, items=[], write_progress=False)
        response = _run_closeout(agent, params)
        assert response is None
        assert _closeout_report(task_root)["reason"] == "delivery_contract_missing"
        assert not any("[task-progress-fake-done-rework]" in str(item) for item in params.tool_context)
