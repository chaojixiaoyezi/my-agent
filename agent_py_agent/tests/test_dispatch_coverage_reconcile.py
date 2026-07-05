"""§11.1 派工路 coverage 结构对账:子代理交付后,把有【结构化产物证据】的父需求项标 done,
补 solo 路有、派工路失效的完整性兜底网。判据纯结构信号(路径段匹配),只增不减,solo 路一字不动。

真机根因(u-g6a2 派工分析):子代理干完活、报告实覆盖 5/5,父 requirement coverage 却停
0/pending——solo 路模型边做边标,派工路不回来标。这里逐项验证:项目名类需求(路径式标识符)
被交付产物路径证据 credit;功能名类/漏做项仍 open 交给 rework;solo 路不动;只增不减。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.delivery_closeout.dispatch_coverage_reconcile import (
    reconcile_dispatch_coverage,
)
from agent_py_agent.agent.agent_core.delivery_closeout.task_progress_gate import (
    _run_id,
    coverage_incomplete_rework,
    evaluate_task_progress_closeout_gate,
)
from agent_py_agent.agent.task_progress import read_task_progress, write_task_progress

_RUN_ID = "run-parent-1"


def _closeout(home: Path, task_root: Path):
    return SimpleNamespace(
        agent=SimpleNamespace(
            home_paths=SimpleNamespace(owner_home_dir=str(home)),
            root=str(home),
            _main_agent_run_id=_RUN_ID,
        ),
        params=SimpleNamespace(
            run_id=_RUN_ID,
            task_id=_RUN_ID,
            source="cli_run",
            context_scope="default",
            task_attributes={"run_workspace": {"task_root": str(task_root)}},
        ),
    )


def _write_child(
    task_root: Path,
    child_id: str,
    *,
    status: str = "DONE",
    parent_id: str = _RUN_ID,
    output_files: list[str] | None = None,
    artifact_refs: list[str] | None = None,
    placeholder: int = 0,
) -> None:
    agent_dir = task_root / "work" / "agents" / child_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    attributes: dict = {}
    if output_files is not None:
        attributes["output_files"] = output_files
    if placeholder:
        attributes["placeholder_artifacts"] = [{"path": f"ph-{i}"} for i in range(placeholder)]
    payload = {
        "run_id": child_id,
        "id": child_id,
        "parent_id": parent_id,
        "status": status,
        "attributes": attributes,
        "artifact_refs": artifact_refs or [],
    }
    (agent_dir / "canonical_state.json").write_text(json.dumps(payload), encoding="utf-8")


def _seed_requirement_coverage(home: Path, titles: list[str], *, auto: bool = True) -> None:
    targets = []
    for index, title in enumerate(titles, start=1):
        target = {"id": f"req-{index:02d}", "title": title, "status": "pending"}
        if auto:
            target["coverage_kind"] = "requirement_item"
            target["source_ref"] = "auto:requirement-enumeration"
        targets.append(target)
    write_task_progress(home, _RUN_ID, {"coverage": {"goal": "需求枚举项对账", "targets": targets}})


def _report_with_artifact(tmp_path: Path, text: str) -> dict:
    report_file = tmp_path / "output" / "analysis_report.md"
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(text, encoding="utf-8")
    return {"artifacts": [{"path": str(report_file), "ok": True}]}


def _statuses(home: Path) -> dict[str, str]:
    coverage = read_task_progress(home, _RUN_ID).get("coverage") or {}
    return {t["id"]: t["status"] for t in coverage.get("targets", [])}


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    task_root = tmp_path / "task"
    home.mkdir()
    task_root.mkdir()
    return home, task_root


# --- 核心:项目名类需求(路径式标识符)被交付产物路径证据 credit ---------------------


def test_credits_project_name_req_from_report_path_evidence(tmp_path):
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["agentscope-main", "langgraph-main"])
    _write_child(task_root, "sub-a")
    # 报告只引用了 agentscope 的源文件路径,没引用 langgraph → 只 credit agentscope。
    report = _report_with_artifact(
        tmp_path, "分析见 agentscope-main/src/agentscope/agents/agent.py 与 agentscope-main/README.md。"
    )

    credited = reconcile_dispatch_coverage(_closeout(home, task_root), report, home, _RUN_ID)

    assert credited == ["req-01"]
    statuses = _statuses(home)
    assert statuses["req-01"] == "done"  # agentscope-main 有路径证据
    assert statuses["req-02"] == "pending"  # langgraph-main 无证据 → 仍 open


def test_credits_from_child_declared_output_path(tmp_path):
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["openai-agents-python-main"])
    _write_child(
        task_root,
        "sub-b",
        output_files=["/x/tasks/output/openai-agents-python-main/analysis.md"],
    )
    # 没有父报告 artifact,证据全来自子代理声明的产物路径。
    credited = reconcile_dispatch_coverage(_closeout(home, task_root), {}, home, _RUN_ID)

    assert credited == ["req-01"]
    assert _statuses(home)["req-01"] == "done"


def test_missing_project_stays_open_and_reflected_in_gate(tmp_path):
    """故意让一个项目没被交付覆盖 → 该项仍 open,收口门 coverage 计数如实反映(不是无脑全标)。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["agentscope-main", "openai-agents-python-main", "langgraph-main"])
    _write_child(task_root, "sub-a")
    report = _report_with_artifact(
        tmp_path,
        "见 agentscope-main/README.md 和 openai-agents-python-main/pyproject.toml;langgraph 没做。",
    )

    closeout = _closeout(home, task_root)
    decision = evaluate_task_progress_closeout_gate(closeout, report)

    statuses = _statuses(home)
    assert statuses["req-01"] == "done"
    assert statuses["req-02"] == "done"
    assert statuses["req-03"] == "pending"  # langgraph-main 漏做 → 仍 open
    # 收口门读到的是对账后的真实状态:仍有 1 个未完成 → coverage-incomplete 软 finding 在。
    codes = {f.code for f in decision.findings}
    assert "TASK_PROGRESS_COVERAGE_INCOMPLETE" in codes


# --- 诚实边界 + 安全:该不动的一律不动 ------------------------------------------------


def test_does_not_credit_non_pathlike_requirement(tmp_path):
    """功能名/自然语言需求(注册登录、全文搜索、Dark Mode)结构上无法证明 → 一律不 credit。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["注册登录", "全文搜索", "Dark Mode", "index"])
    _write_child(task_root, "sub-a", artifact_refs=["index.html", "login.html", "search.js"])
    report = _report_with_artifact(tmp_path, "交付 index.html / login.html / search.js,含注册登录、全文搜索。")

    credited = reconcile_dispatch_coverage(_closeout(home, task_root), report, home, _RUN_ID)

    assert credited == []
    assert all(status == "pending" for status in _statuses(home).values())


def test_solo_path_untouched_when_no_children(tmp_path):
    """无子代理(solo 路)→ own_done_children 为空 → 一字不动(哪怕报告含路径证据)。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["agentscope-main"])
    # 不写任何子代理 canonical。
    report = _report_with_artifact(tmp_path, "见 agentscope-main/README.md。")

    credited = reconcile_dispatch_coverage(_closeout(home, task_root), report, home, _RUN_ID)

    assert credited == []
    assert _statuses(home)["req-01"] == "pending"


def test_ignores_running_child(tmp_path):
    """子代理还没 DONE(还在跑)→ 不作证据源 → 不 credit(治"没跑完误标")。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["agentscope-main"])
    _write_child(task_root, "sub-a", status="RUNNING")
    report = _report_with_artifact(tmp_path, "见 agentscope-main/README.md。")

    assert reconcile_dispatch_coverage(_closeout(home, task_root), report, home, _RUN_ID) == []
    assert _statuses(home)["req-01"] == "pending"


def test_ignores_placeholder_child(tmp_path):
    """DONE 但产物是占位兜底桩 → 不作证据源(不被空壳骗)。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["agentscope-main"])
    _write_child(task_root, "sub-a", placeholder=2)
    report = _report_with_artifact(tmp_path, "见 agentscope-main/README.md。")

    assert reconcile_dispatch_coverage(_closeout(home, task_root), report, home, _RUN_ID) == []


def test_ignores_sibling_child(tmp_path):
    """非本 run 的兄弟子代理(parent_id 不属于我)→ 不作证据源(兄弟隔离)。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["agentscope-main"])
    _write_child(task_root, "sub-sibling", parent_id="some-other-run")
    report = _report_with_artifact(tmp_path, "见 agentscope-main/README.md。")

    assert reconcile_dispatch_coverage(_closeout(home, task_root), report, home, _RUN_ID) == []


def test_only_auto_seeded_targets_credited(tmp_path):
    """模型自立的 coverage 项(非 auto 种)由模型自己管 → 不 credit。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["agentscope-main"], auto=False)
    _write_child(task_root, "sub-a")
    report = _report_with_artifact(tmp_path, "见 agentscope-main/README.md。")

    assert reconcile_dispatch_coverage(_closeout(home, task_root), report, home, _RUN_ID) == []
    assert _statuses(home)["req-01"] == "pending"


# --- 幂等 / 只增不减 / 永不抛错 -------------------------------------------------------


def test_idempotent_and_only_adds(tmp_path):
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["agentscope-main", "langgraph-main"])
    _write_child(task_root, "sub-a")
    report = _report_with_artifact(tmp_path, "见 agentscope-main/README.md。")
    closeout = _closeout(home, task_root)

    first = reconcile_dispatch_coverage(closeout, report, home, _RUN_ID)
    second = reconcile_dispatch_coverage(closeout, report, home, _RUN_ID)

    assert first == ["req-01"]
    assert second == []  # 只增不减:已 done 的不再重复 credit,重跑无副作用
    statuses = _statuses(home)
    assert statuses["req-01"] == "done"
    assert statuses["req-02"] == "pending"  # 从没被误标


def test_does_not_undo_existing_done(tmp_path):
    """已 done 项(不管谁标的)绝不被回退(只增不减)。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["agentscope-main", "langgraph-main"])
    write_task_progress(home, _RUN_ID, {"coverage": {"targets": [{"id": "req-02", "status": "done"}]}})
    _write_child(task_root, "sub-a")
    report = _report_with_artifact(tmp_path, "见 agentscope-main/README.md。")

    reconcile_dispatch_coverage(_closeout(home, task_root), report, home, _RUN_ID)

    statuses = _statuses(home)
    assert statuses["req-01"] == "done"  # 新 credit
    assert statuses["req-02"] == "done"  # 既有 done 保持


def test_never_throws_on_bad_inputs(tmp_path):
    home, task_root = _setup(tmp_path)
    # root=None / 空 run_id / 垃圾 closeout —— 一律返回 [] 不抛。
    assert reconcile_dispatch_coverage(object(), {}, None, _RUN_ID) == []
    assert reconcile_dispatch_coverage(object(), {}, home, "") == []
    assert reconcile_dispatch_coverage(object(), None, home, _RUN_ID) == []


def test_no_coverage_no_op(tmp_path):
    """父账本没有 coverage(纯 items 任务)→ 无 eligible → no-op。"""
    home, task_root = _setup(tmp_path)
    write_task_progress(home, _RUN_ID, {"items": [{"id": "i1", "title": "t", "status": "in_progress"}]})
    _write_child(task_root, "sub-a")
    report = _report_with_artifact(tmp_path, "见 agentscope-main/README.md。")

    assert reconcile_dispatch_coverage(_closeout(home, task_root), report, home, _RUN_ID) == []


def test_run_id_resolver_matches_gate(tmp_path):
    """对账用的 run_id 必须与收口门同一把(否则写错账本)。"""
    home, task_root = _setup(tmp_path)
    assert _run_id(_closeout(home, task_root)) == _RUN_ID


# --- 端到端:对账 → 收口门 → coverage-incomplete rework 决策链(真实决策函数,非只看单项) ---


def _drive_gate_then_rework(home: Path, task_root: Path, report: dict) -> bool:
    """跑真实收口门(内部对账)→ 把门决策塞进 report → 跑真实 coverage_incomplete_rework。
    返回是否打回(True=账本还有未对完的需求项 → 安全网 fire;False=对完了 → 不误报)。"""
    closeout = _closeout(home, task_root)
    decision = evaluate_task_progress_closeout_gate(closeout, report)
    report["task_progress_closeout_gate"] = decision.to_dict()
    params = SimpleNamespace(tool_context=[], executed_tools=[])
    return coverage_incomplete_rework(params, report)


def test_fully_delivered_dispatch_no_false_incomplete_rework(tmp_path):
    """报告实覆盖全部 5 项目 → 对账逐项标 done → 收口门不再误报"不完整"、rework 不打回
    (治 u-g6a2"报告做全了、账本却 0/N、被误当不完整")。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["agentscope-main", "langgraph-main", "openai-agents-python-main"])
    # 模型已把派工 items 逐项标 done(dispatch 路模型会标 items,只是不回填 coverage)。
    write_task_progress(home, _RUN_ID, {"items": [{"id": "i1", "title": "整合", "status": "done"}]})
    _write_child(task_root, "sub-a")
    report = _report_with_artifact(
        tmp_path,
        "章节见 agentscope-main/README.md、langgraph-main/langgraph/graph.py、"
        "openai-agents-python-main/pyproject.toml。",
    )

    reworked = _drive_gate_then_rework(home, task_root, report)

    assert reworked is False  # 全对账 → 不打回
    assert all(s == "done" for s in _statuses(home).values())


def test_partially_delivered_dispatch_still_reworks_missing(tmp_path):
    """故意漏 1 项目(报告没引用它的路径)→ 对账只标已交付的、漏项仍 open → 收口门 rework
    照打回(证明安全网真生效、不是无脑全标 done)。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["agentscope-main", "langgraph-main", "openai-agents-python-main"])
    write_task_progress(home, _RUN_ID, {"items": [{"id": "i1", "title": "整合", "status": "done"}]})
    _write_child(task_root, "sub-a")
    # 只引用 2/3:漏了 openai-agents-python-main。
    report = _report_with_artifact(
        tmp_path, "章节见 agentscope-main/README.md、langgraph-main/langgraph/graph.py。"
    )

    reworked = _drive_gate_then_rework(home, task_root, report)

    assert reworked is True  # 漏项仍 open → 安全网打回
    statuses = _statuses(home)
    assert statuses["req-01"] == "done" and statuses["req-02"] == "done"
    assert statuses["req-03"] == "pending"  # 漏项如实 open
