from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.subagent_finalize_helpers import (
    FinalizedRunnerRecordRequest,
    record_finalized_runner_result,
)
from agent_py_agent.agent.agent_core.subagent_params import SubagentFinalizeParams
from agent_py_agent.agent.subagent import SubAgentParsedOutput


# LLM: _task keeps finalize tests focused on refs-only child status instead of manager persistence.
# 函数用途: 构造最小 task 替身，覆盖 coordinator child 状态门需要的字段。
def _task(
    run_id: str,
    *,
    status: str,
    verification: str,
    children: list[str] | None = None,
):
    return SimpleNamespace(
        id=run_id,
        status=status,
        verification_status=verification,
        child_ids=list(children or []),
        agent_run_workspace_dir="",
    )


# LLM: _context models a coordinator runner with child scheduling authority.
# 函数用途: 构造 finalize 所需的执行上下文，确保 child 状态门只在 coordinator 场景触发。
def _context(
    *,
    role: str = "coordinator",
    agent_name: str = "小傻妞-coordinator",
    task_dir: str = "",
    write_boundary: dict | None = None,
):
    return SimpleNamespace(
        role=role,
        agent_name=agent_name,
        allowed_tools=["schedule_child_subagents", "dispatch_subagents"],
        goal="协调两个页面产物",
        task_dir=task_dir,
        write_boundary=write_boundary or {},
    )


# LLM: _params creates the finalize bundle without invoking the real model loop.
# 函数用途: 让测试直接覆盖 runner 结果记录前的状态修正逻辑。
def _params(run_id: str = "parent", *, context=None):
    return SubagentFinalizeParams(
        run_id=run_id,
        active_attempt_id="attempt-1",
        result=SimpleNamespace(tool_rounds=3, executed_tools=["dispatch_subagents"]),
        context=context or _context(),
        prompt="",
    )


# LLM: _agent records the exact runner-result params sent to SubAgentManager.
# 函数用途: 用最小 fake agent 验证 finalize helper 写回的 structured_output。
def _agent(tasks: dict[str, object]):
    captured = SimpleNamespace(params=None)

    def record_runner_result(params):
        captured.params = params
        return SimpleNamespace(
            status=params.structured_output.status,
            verification_status=params.structured_output.status,
        )

    return (
        SimpleNamespace(
            subagents=SimpleNamespace(
                load=lambda run_id: tasks[run_id],
                record_runner_result=record_runner_result,
            )
        ),
        captured,
    )


# LLM: child timeout must be stronger than a coordinator's optimistic completion JSON.
# 函数用途: 防止真实 E2E 里 coordinator 只看文件存在，就把 TIMEOUT child 包装成完成。
def test_coordinator_success_closeout_blocks_when_direct_child_timed_out():
    agent, captured = _agent({
        "parent": _task("parent", status="RUNNING", verification="UNVERIFIED", children=["child-a"]),
        "child-a": _task("child-a", status="TIMEOUT", verification="UNVERIFIED"),
    })
    structured = SubAgentParsedOutput(
        found=True,
        ok=True,
        status="AWAITING_ACCEPTANCE",
        summary="文件已经生成，等待验收。",
    )

    result = record_finalized_runner_result(
        FinalizedRunnerRecordRequest(agent, _params(), structured, _repair_state())
    )

    assert result.status == "BLOCKED"
    assert captured.params.structured_output.failure_type == "child_blocked"
    assert "child-a" in captured.params.structured_output.blocked_reason
    assert captured.params.structured_output.next_actions == [
        "dispatch_subagents",
        "recover_blocking_children",
    ]


# LLM: verified children still allow the existing coordinator completion override.
# 函数用途: 确认新增 child blocker 门不破坏“孩子全绿后 coordinator 可待验收”的旧行为。
def test_coordinator_tool_limit_cleanup_still_passes_when_children_verified():
    agent, captured = _agent({
        "parent": _task("parent", status="RUNNING", verification="UNVERIFIED", children=["child-a"]),
        "child-a": _task("child-a", status="DONE", verification="VERIFIED"),
    })
    structured = SubAgentParsedOutput(
        found=True,
        ok=True,
        status="BLOCKED",
        summary="达到 max_tool_round 工具轮数上限，但孩子已完成。",
        blocked_reason="max_tool_round limit",
        failure_type="tool_round_limit",
    )

    result = record_finalized_runner_result(
        FinalizedRunnerRecordRequest(agent, _params(), structured, _repair_state())
    )

    assert result.status == "AWAITING_ACCEPTANCE"
    assert captured.params.structured_output.status == "AWAITING_ACCEPTANCE"
    assert "DONE/VERIFIED" in captured.params.structured_output.summary


# LLM: non-blocking child states should leave ordinary coordinator output unchanged.
# 函数用途: 避免 child 状态门把没有失败 child 的正常结构化汇报误改为 BLOCKED。
def test_coordinator_success_closeout_keeps_non_blocking_children():
    agent, captured = _agent({
        "parent": _task("parent", status="RUNNING", verification="UNVERIFIED", children=["child-a"]),
        "child-a": _task("child-a", status="AWAITING_ACCEPTANCE", verification="NEEDS_ACCEPTANCE"),
    })
    structured = SubAgentParsedOutput(
        found=True,
        ok=True,
        status="AWAITING_ACCEPTANCE",
        summary="等待父级验收。",
    )

    result = record_finalized_runner_result(
        FinalizedRunnerRecordRequest(agent, _params(), structured, _repair_state())
    )

    assert result.status == "AWAITING_ACCEPTANCE"
    assert captured.params.structured_output.failure_type == ""


# LLM: leaf workers cannot mark a broken HTML artifact as ready for acceptance.
# 函数用途: 子代理写出半截 HTML 时，即使模型自称完成，finalize 也要改成 BLOCKED 让父级修复。
def test_leaf_success_closeout_blocks_incomplete_html_artifact(tmp_path: Path):
    artifact = tmp_path / "artifacts" / "index.html"
    artifact.parent.mkdir()
    artifact.write_text("<html><body><main>unfinished", encoding="utf-8")
    agent, captured = _agent({"worker": _task("worker", status="RUNNING", verification="UNVERIFIED")})
    structured = SubAgentParsedOutput(
        found=True,
        ok=True,
        status="AWAITING_ACCEPTANCE",
        summary="页面已完成。",
        artifacts=[{"path": str(artifact), "kind": "file", "summary": "homepage"}],
    )

    result = record_finalized_runner_result(
        FinalizedRunnerRecordRequest(
            agent,
            _params(
                "worker",
                context=_context(role="leaf_worker", agent_name="小小傻妞-page"),
            ),
            structured,
            _repair_state(),
        )
    )

    assert result.status == "BLOCKED"
    assert captured.params.structured_output.failure_type == "artifact_integrity_failed"
    assert "missing_html_close" in captured.params.structured_output.blocked_reason


# LLM: leaf workers cannot close out web artifacts with inert or broken links.
# 函数用途: 子代理写完 HTML 后如果还留 href="#" 或不存在的锚点，finalize 必须转 BLOCKED 让父级派修复。
def test_leaf_success_closeout_blocks_invalid_html_links(tmp_path: Path):
    artifact = tmp_path / "artifacts" / "index.html"
    artifact.parent.mkdir()
    artifact.write_text(
        "<html><body><main id='home'>done</main><a href='#'>咨询</a><a href='#missing'>更多</a></body></html>",
        encoding="utf-8",
    )
    agent, captured = _agent({"worker": _task("worker", status="RUNNING", verification="UNVERIFIED")})
    structured = SubAgentParsedOutput(
        found=True,
        ok=True,
        status="AWAITING_ACCEPTANCE",
        summary="页面已完成。",
        artifacts=[{"path": str(artifact), "kind": "file", "summary": "homepage"}],
    )

    result = record_finalized_runner_result(
        FinalizedRunnerRecordRequest(
            agent,
            _params(
                "worker",
                context=_context(role="leaf_worker", agent_name="小小傻妞-page"),
            ),
            structured,
            _repair_state(),
        )
    )

    assert result.status == "BLOCKED"
    assert captured.params.structured_output.failure_type == "artifact_integrity_failed"
    assert "placeholder_hash_link" in captured.params.structured_output.blocked_reason
    assert "missing_hash_target" in captured.params.structured_output.blocked_reason


# LLM: latest tool progress should restore artifact refs omitted from output.json closeout.
# 函数用途: 复现真实 E2E 中 output.json.artifacts=[]；finalize 应从 latest_tool_progress 补产物并继续 integrity gate。
def test_leaf_success_closeout_recovers_missing_artifact_from_latest_progress(tmp_path: Path):
    artifact = tmp_path / "deliverables" / "furniture-home" / "index.html"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("<html><body><main id='home'>done</main><a href='#'>咨询</a></body></html>", encoding="utf-8")
    workspace = tmp_path / "tasks" / "worker" / "agents" / "worker"
    progress_dir = workspace / "progress"
    progress_dir.mkdir(parents=True)
    (progress_dir / "latest_tool_progress.json").write_text(
        json.dumps({
            "latest_written_path": str(artifact),
            "artifact_integrity": {"kind": "html", "warning_codes": ["placeholder_hash_link"]},
        }),
        encoding="utf-8",
    )
    task = _task("worker", status="RUNNING", verification="UNVERIFIED")
    task.agent_run_workspace_dir = str(workspace)
    agent, captured = _agent({"worker": task})
    structured = SubAgentParsedOutput(
        found=True,
        ok=True,
        status="AWAITING_ACCEPTANCE",
        summary="页面已完成。",
        artifacts=[],
    )

    result = record_finalized_runner_result(
        FinalizedRunnerRecordRequest(
            agent,
            _params("worker", context=_context(role="leaf_worker", agent_name="小小傻妞-page")),
            structured,
            _repair_state(),
        )
    )

    assert result.status == "BLOCKED"
    assert captured.params.structured_output.artifacts[0]["path"] == str(artifact)
    assert captured.params.structured_output.failure_type == "artifact_integrity_failed"
    assert "placeholder_hash_link" in captured.params.structured_output.blocked_reason


# LLM: relative artifact refs from repair workers should resolve to product write roots first.
# 函数用途: 修复子代理常用 artifacts/index.html 相对路径汇报；finalize 要检查真实产物目录而不是内部 task_dir。
def test_leaf_relative_artifact_uses_product_write_root(tmp_path: Path):
    artifact = tmp_path / "artifacts" / "index.html"
    artifact.parent.mkdir()
    artifact.write_text("<html><body><main>done</main></body></html>", encoding="utf-8")
    agent, captured = _agent({"worker": _task("worker", status="RUNNING", verification="UNVERIFIED")})
    structured = SubAgentParsedOutput(
        found=True,
        ok=True,
        status="AWAITING_ACCEPTANCE",
        summary="页面已修复。",
        artifacts=[{"path": "artifacts/index.html", "kind": "file", "summary": "homepage"}],
    )
    context = _context(
        role="leaf_worker",
        agent_name="小小傻妞-repair",
        task_dir=str(tmp_path / ".my_agent" / "subagents" / "worker"),
        write_boundary={"product_write_roots": [str(artifact)]},
    )

    result = record_finalized_runner_result(
        FinalizedRunnerRecordRequest(agent, _params("worker", context=context), structured, _repair_state())
    )

    assert result.status == "AWAITING_ACCEPTANCE"
    assert captured.params.structured_output.failure_type == ""


# LLM: product-root relative refs may include the root suffix already, as real models often report.
# 函数用途: 覆盖 deliverables/site/index.html 这类相对路径，避免 integrity gate 误拼成 site/deliverables/site/index.html。
def test_leaf_relative_artifact_with_product_root_suffix(tmp_path: Path):
    product_root = tmp_path / "deliverables" / "furniture-home"
    artifact = product_root / "index.html"
    product_root.mkdir(parents=True)
    artifact.write_text("<html><body><main>done</main></body></html>", encoding="utf-8")
    agent, captured = _agent({"worker": _task("worker", status="RUNNING", verification="UNVERIFIED")})
    structured = SubAgentParsedOutput(
        found=True,
        ok=True,
        status="AWAITING_ACCEPTANCE",
        summary="页面已完成。",
        artifacts=[{"path": "deliverables/furniture-home/index.html", "kind": "file", "summary": "homepage"}],
    )
    context = _context(
        role="leaf_worker",
        agent_name="小傻妞-worker",
        task_dir=str(tmp_path / "runtime" / "subagents" / "worker"),
        write_boundary={"product_write_roots": [str(product_root)]},
    )

    result = record_finalized_runner_result(
        FinalizedRunnerRecordRequest(agent, _params("worker", context=context), structured, _repair_state())
    )

    assert result.status == "AWAITING_ACCEPTANCE"
    assert captured.params.structured_output.failure_type == ""


# LLM: relative product roots in real runner contexts must resolve from the project workspace, not task_dir.
# 函数用途: 复现 Live Lab 中 product_write_roots=lab_outputs/...；finalize 应检查项目根下的产物，不能误拼到 .my_agent/subagents。
def test_leaf_relative_product_root_uses_derived_workspace_root(tmp_path: Path):
    workspace_root = tmp_path / "fixture_project"
    product_root = workspace_root / "lab_outputs" / "furniture-home"
    artifact = product_root / "index.html"
    product_root.mkdir(parents=True)
    artifact.write_text("<html><body><main>done</main></body></html>", encoding="utf-8")
    task_dir = workspace_root / ".my_agent" / "subagents" / "worker"
    agent, captured = _agent({"worker": _task("worker", status="RUNNING", verification="UNVERIFIED")})
    structured = SubAgentParsedOutput(
        found=True,
        ok=True,
        status="AWAITING_ACCEPTANCE",
        summary="页面已完成。",
        artifacts=[{"path": "lab_outputs/furniture-home/index.html", "kind": "file", "summary": "homepage"}],
    )
    context = _context(
        role="leaf_worker",
        agent_name="小傻妞-page",
        task_dir=str(task_dir),
        write_boundary={"product_write_roots": ["lab_outputs/furniture-home"]},
    )

    result = record_finalized_runner_result(
        FinalizedRunnerRecordRequest(agent, _params("worker", context=context), structured, _repair_state())
    )

    assert result.status == "AWAITING_ACCEPTANCE"
    assert captured.params.structured_output.failure_type == ""


# LLM: _repair_state is the minimum finalize metadata persisted with a runner result.
# 函数用途: 统一测试里的 finalize repair_state 字段，避免每个用例重复散写。
def _repair_state() -> dict[str, object]:
    return {
        "message": "runner 已完成模型调用，等待独立验收。",
        "prompt_for_log": "",
        "response_for_log": "",
        "backend_name": "test",
        "attempted": False,
        "ok": False,
        "error": "",
    }
