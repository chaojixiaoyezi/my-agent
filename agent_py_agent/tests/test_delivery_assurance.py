from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

# 交付保障收口层(底座提升 A1)防回归:真机实锤三类失血——大文件分析答案全对但
# responses/*.json 的 text 长度=0;多项目分析主报告落 work/ 没进 output/;建站成果在
# output/ 但收尾汇总为空。三把钉子:①finalize 前空响应必被确定性合成收尾汇总(结构化
# 事实拼装,绝不空手);②声明过的交付物落在 work/ 时归集进 output/(只认声明,不猜);
# ③出口层空响应守卫幂等打回一次让模型自己写,不死循环。
from agent_py_agent.agent.agent_core._runtime_params import FinalizeContext
from agent_py_agent.agent.agent_core.delivery_closeout.delivery_assurance import (
    SYNTHESIZED_MARKER,
    apply_delivery_assurance,
)
from agent_py_agent.agent.agent_core.tool_loop.final_exit_contract import (
    FinalExitState,
    _append_blank_response_instruction,
    _blank_exit_guard_applies,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.task_progress import write_task_progress


def _agent(tmp_path: Path, task_root: Path | None = None) -> SimpleNamespace:
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(tmp_path)),
        root=str(tmp_path),
    )
    if task_root is not None:
        agent._current_run_task_workspace = str(task_root)
    return agent


def _ctx(*, text: str, run_id: str = "run-a1", scope: str = "default", executed: list | None = None) -> FinalizeContext:
    return FinalizeContext(
        user_prompt="goal",
        final_prompt="prompt",
        final_response=ModelResponse(text=text, backend="test"),
        memories=[],
        executed_tools=list(executed or []),
        archive_tool_calls=[],
        routed_context=None,
        resume_context_result=None,
        runtime_injections=[],
        compression_snapshot_id="",
        compression_snapshot_path="",
        compression_applied=False,
        request_id="req-a1",
        run_id=run_id,
        task_id="task-a1",
        source="cli_run",
        do_save=False,
        task_attributes=None,
        recovery_task_refs=None,
        recovery_content_paths=None,
        recovery_next_actions=None,
        context_scope=scope,
    )


def _task_root(tmp_path: Path) -> Path:
    root = tmp_path / "tasks" / "t1"
    (root / "work" / "shared").mkdir(parents=True)
    (root / "output").mkdir(parents=True)
    return root


def _append_finding(root: Path, claim: str, refs: list[str] | None = None) -> None:
    path = root / "work" / "shared" / "findings.jsonl"
    record = {"claim": claim, "evidence_refs": refs or []}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def test_blank_response_synthesized_from_structured_records(tmp_path):
    # ①空响应 → 从 findings/output/进度账本合成收尾汇总,绝不空手。
    root = _task_root(tmp_path)
    (root / "output" / "report.md").write_text("# 报告", encoding="utf-8")
    _append_finding(root, "异常账户 A-42 已确认")
    write_task_progress(tmp_path, "run-a1", {"items": [{"id": "i1", "title": "分析大文件", "status": "done"}]})
    ctx = apply_delivery_assurance(_agent(tmp_path, root), _ctx(text="   "))
    text = ctx.final_response.text
    assert SYNTHESIZED_MARKER in text
    assert "output/report.md" in text
    assert "异常账户 A-42 已确认" in text
    assert "进度账本: 1/1" in text


def test_blank_response_without_any_facts_left_untouched(tmp_path):
    # 纯聊天空响应无任何结构化事实 → 不编造(交上游空响应链)。
    ctx = apply_delivery_assurance(_agent(tmp_path), _ctx(text=""))
    assert ctx.final_response.text == ""


def test_blank_response_tool_trace_fallback(tmp_path):
    # 有工具痕迹但无任务区 → 至少给执行痕迹汇总。
    ctx = apply_delivery_assurance(_agent(tmp_path), _ctx(text="", executed=["read_file", "read_file"]))
    assert "2 次工具调用" in ctx.final_response.text
    assert SYNTHESIZED_MARKER in ctx.final_response.text


def test_non_blank_response_untouched_without_collection(tmp_path):
    root = _task_root(tmp_path)
    original = "已完成,报告在 output/report.md"
    ctx = apply_delivery_assurance(_agent(tmp_path, root), _ctx(text=original))
    assert ctx.final_response.text == original


def test_declared_work_deliverable_collected_into_output(tmp_path):
    # ②主报告落 work/ + findings 声明了它 → 归集进 output/(剥掉 work/ 前缀)。
    root = _task_root(tmp_path)
    (root / "work" / "reports").mkdir(parents=True)
    (root / "work" / "reports" / "main.md").write_text("主报告", encoding="utf-8")
    _append_finding(root, "主报告完成", refs=["work/reports/main.md"])
    ctx = apply_delivery_assurance(_agent(tmp_path, root), _ctx(text="干完了"))
    assert (root / "output" / "reports" / "main.md").read_text(encoding="utf-8") == "主报告"
    assert "[交付归集]" in ctx.final_response.text
    assert "output/reports/main.md" in ctx.final_response.text


def test_expected_outputs_pattern_recovers_misplaced_file(tmp_path):
    # expected_outputs 声明 pattern,成果误落 work/ → 按同名 pattern 寻回。
    root = _task_root(tmp_path)
    (root / "work" / "site.zip").write_bytes(b"zip")
    write_task_progress(tmp_path, "run-a1", {"items": [{"id": "i1", "title": "x", "status": "done"}], "expected_outputs": [{"pattern": "site.zip"}]})
    apply_delivery_assurance(_agent(tmp_path, root), _ctx(text="done"))
    assert (root / "output" / "site.zip").exists()


def test_collection_rejects_paths_outside_task_root(tmp_path):
    # 越界声明(逃逸任务区)绝不搬运。
    root = _task_root(tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_text("s", encoding="utf-8")
    _append_finding(root, "x", refs=[str(outside), "../../secret.txt"])
    apply_delivery_assurance(_agent(tmp_path, root), _ctx(text="done"))
    assert not list((root / "output").rglob("secret.txt"))


def test_collection_never_overwrites_existing_output(tmp_path):
    root = _task_root(tmp_path)
    (root / "work" / "r.md").write_text("new", encoding="utf-8")
    (root / "output" / "r.md").write_text("old", encoding="utf-8")
    _append_finding(root, "x", refs=["work/r.md"])
    ctx = apply_delivery_assurance(_agent(tmp_path, root), _ctx(text="done"))
    assert (root / "output" / "r.md").read_text(encoding="utf-8") == "old"
    assert ctx.final_response.text == "done"  # 没归集就不加注


def test_task_local_scope_untouched(tmp_path):
    # 子代理 runner 有自己的收尾修复链,本层不得插手。
    root = _task_root(tmp_path)
    ctx = apply_delivery_assurance(_agent(tmp_path, root), _ctx(text="", scope="task_local"))
    assert ctx.final_response.text == ""


def test_synthesis_survives_namespace_response(tmp_path):
    # 响应对象不是 dataclass(旧调用方/测试桩)也不允许静默失效。
    root = _task_root(tmp_path)
    _append_finding(root, "结论一")
    base = _ctx(text="x")
    ctx = FinalizeContext(**{**base.__dict__, "final_response": SimpleNamespace(text="", backend="t")})
    out = apply_delivery_assurance(_agent(tmp_path, root), ctx)
    assert SYNTHESIZED_MARKER in out.final_response.text


def test_blank_exit_guard_fires_once(tmp_path):
    # ③出口层空响应守卫:幂等一次,打回带结构化指令;第二次放行(锚1兜底)。
    state = FinalExitState()
    params = SimpleNamespace(executed_tools=["read_file"], tool_context=[])
    response = SimpleNamespace(text="   ", backend="t")
    assert _blank_exit_guard_applies(params, response, state)
    state.blank_guard_fired = True
    _append_blank_response_instruction(params)
    assert any("[final-exit-blank-response]" in str(item) for item in params.tool_context)
    assert not _blank_exit_guard_applies(params, response, state)


def test_blank_exit_guard_skips_toolless_and_nonblank():
    state = FinalExitState()
    assert not _blank_exit_guard_applies(SimpleNamespace(executed_tools=[], tool_context=[]), SimpleNamespace(text=""), state)
    assert not _blank_exit_guard_applies(SimpleNamespace(executed_tools=["x"], tool_context=[]), SimpleNamespace(text="答案"), state)
