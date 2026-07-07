from __future__ import annotations

import json
import time
from types import SimpleNamespace

# A4 持续型委派语义(底座提升)防回归:盯守(无终态持续任务)派给子代理后,子代理按
# "做完即退"提前 DONE,整任务停摆(真机 u-t1b:只 18 条,wake_queue 有 subagent-finished
# DONE)。三把钉子:①声明端 service_window_seconds 随 attributes 透传;②子代理收口层
# 窗口未走完不因"落一次产物"提前收口;③父代理 wake 载荷带"窗口未走完"结构化事实。
from agent_py_agent.agent.agent_core.orchestration.create_policy import _create_attributes
from agent_py_agent.agent.agent_core.subagent.progress_closeout import (
    _progress_ready_for_closeout,
)
from agent_py_agent.agent.subagents.runner_completion_wake import _metadata, _summary
from agent_py_agent.agent.subagents.service_window import service_window_remaining_seconds


def _task(*, long_running: bool = True, window: int = 600, created_ago: float = 10.0) -> SimpleNamespace:
    return SimpleNamespace(
        id="run-w1",
        agent_name="watcher",
        attributes={"long_running": long_running, "service_window_seconds": window},
        created_at=time.time() - created_ago,
        output_json="",
        context_packs=[],
    )


def test_service_window_remaining_semantics():
    assert service_window_remaining_seconds(_task()) > 500
    assert service_window_remaining_seconds(_task(long_running=False)) == 0.0
    assert service_window_remaining_seconds(_task(created_ago=700.0)) == 0.0
    assert service_window_remaining_seconds(SimpleNamespace(attributes={}, created_at=0.0)) == 0.0
    # 坏声明不炸、按无窗口处理。
    bad = SimpleNamespace(attributes={"long_running": True, "service_window_seconds": "abc"}, created_at=time.time())
    assert service_window_remaining_seconds(bad) == 0.0


def test_progress_closeout_suppressed_while_window_open(tmp_path):
    # 窗口未走完:哪怕产物已落盘,也不因"落了一次产物"被系统提前收口。
    artifact = tmp_path / "out.md"
    artifact.write_text("首批发现", encoding="utf-8")
    progress = {"latest_written_path": str(artifact)}
    task = _task()
    task.attributes["output_files"] = [str(artifact)]
    assert _progress_ready_for_closeout(progress, task) is False
    # 窗口走完:恢复正常收口判定(声明产物匹配 → 可收口)。
    elapsed = _task(created_ago=700.0)
    elapsed.attributes["output_files"] = [str(artifact)]
    assert _progress_ready_for_closeout(progress, elapsed) is True


def test_progress_closeout_suppressed_while_watch_backlog_unjudged(tmp_path):
    # P1 消费吞吐:窗口走完、产物也落了,但本 run 名下盯守路 spool 还有已抬升未判完的
    # 候选——不许体面收口(积压是盯守期内的事件,判完才算干完);账清后自动放行。
    from agent_py_agent.agent.ingestion.watch_state import new_state, persist_state, state_dir

    artifact = tmp_path / "out.md"
    artifact.write_text("首批发现", encoding="utf-8")
    progress = {"latest_written_path": str(artifact)}
    task = _task(created_ago=700.0)
    task.attributes["output_files"] = [str(artifact)]
    owner_home = tmp_path / "owner"
    lane = new_state(owner_home, "http://127.0.0.1:9/pull", {"watch_window_seconds": 600})
    lane.opened_at = time.time() - 700.0
    lane.last_puller_run_id = task.id
    lane.totals["spool_candidates"] = 5
    persist_state(lane)
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=str(owner_home)))

    assert _progress_ready_for_closeout(progress, task, agent=agent) is False

    sidecar = state_dir(owner_home) / f"{lane.watch_id}.read.json"
    sidecar.write_text(
        json.dumps({"read_seq": 9, "candidates_consumed": 5, "candidates_acked": 5, "updated_at": time.time()}),
        encoding="utf-8",
    )
    assert _progress_ready_for_closeout(progress, task, agent=agent) is True


def test_create_attributes_pass_service_window_through():
    attrs = _create_attributes({"goal": "盯 2 小时", "long_running": True, "service_window_seconds": 7200})
    assert attrs["long_running"] is True
    assert attrs["service_window_seconds"] == 7200
    # 坏值/缺省不进 attributes(零声明零影响)。
    assert "service_window_seconds" not in _create_attributes({"goal": "x", "service_window_seconds": "abc"})
    assert "service_window_seconds" not in _create_attributes({"goal": "x"})


def test_create_tool_passes_service_window_into_created_run(monkeypatch):
    # 工具层端到端:create_subagents 声明的窗口经 create_policy 落进 create_run 的
    # attributes(mock manager 捕获真实调用参数,验证声明→任务落盘链)。
    from pathlib import Path
    from unittest.mock import MagicMock

    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    mock_agent = MagicMock()
    mock_agent.config.enable_subagents = True
    mock_agent.config.max_subagents = 10
    mock_agent.config.subagent_workflow_mode = "off"
    mock_agent.subagents.workspace_root = Path("/tmp/project")
    mock_agent.subagents.workspace_roots = [Path("/tmp/project")]
    mock_agent.subagents.workspace = Path("/tmp/project/.my-agent/subagents")
    created = MagicMock()
    created.id = "run_w"
    created.goal = "盯 2 小时"
    created.status = "PLANNING"
    created.verification_status = "UNVERIFIED"
    created.task_dir = "/tmp/run_w"
    mock_agent.subagents.create_run.return_value = created
    tool = CreateSubagentsTool(mock_agent)
    result = tool.execute(
        {"goal": "盯 2 小时", "long_running": True, "service_window_seconds": 7200, "defer_start": True}
    )
    assert result.ok, result.output
    params = mock_agent.subagents.create_run.call_args.kwargs.get("params")
    if params is None:
        params = mock_agent.subagents.create_run.call_args.args[0]
    assert params.attributes.get("long_running") is True
    assert params.attributes.get("service_window_seconds") == 7200


def test_wake_payload_carries_window_incomplete_fact():
    task = _task()
    result = SimpleNamespace(status="DONE", verification_status="UNVERIFIED", result_json="", run_id="run-w1")
    meta = _metadata(task, result, {})
    assert meta["service_window_incomplete"] is True
    assert meta["service_window_remaining_seconds"] > 0
    assert "值守窗口" in _summary(task, result, "DONE")
    # 窗口走完的正常完成:载荷不带该事实,概要不吓唬人。
    done = _task(created_ago=700.0)
    meta_done = _metadata(done, result, {})
    assert "service_window_incomplete" not in meta_done
    assert "值守窗口" not in _summary(done, result, "DONE")
