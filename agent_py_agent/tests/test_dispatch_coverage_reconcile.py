"""§11.1+P1 派工路 coverage 结构对账:子代理交付后,把有【结构化证据】的父需求项标 done,
补 solo 路有、派工路失效的完整性兜底网。两道判据(都是结构信号,只增不减,solo 路一字不动):
①【covers=id 绑定】(P1 主修):派工时模型在 item 里声明 covers=[清单项 id],子代理 DONE
  按 id 精确打勾——功能名类(自然语言)需求项从此也能对上账,不再靠收尾猜文件名;
②【路径段证据】(§11.1 兜底):没绑 covers 时,项目名类需求(路径式标识符)仍可被交付
  产物路径证据 credit。

真机根因(u-g6a2 派工分析):子代理干完活、报告实覆盖 5/5,父 requirement coverage 却停
0/pending——solo 路模型边做边标,派工路不回来标。这里逐项验证:covers 绑定按 id credit
(含端到端"漏绑项仍 open 被 rework 打回");路径段证据照旧;漏做项仍 open;solo 路不动;
只增不减。
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
    covers: list[str] | None = None,
) -> None:
    agent_dir = task_root / "work" / "agents" / child_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    attributes: dict = {}
    if output_files is not None:
        attributes["output_files"] = output_files
    if placeholder:
        attributes["placeholder_artifacts"] = [{"path": f"ph-{i}"} for i in range(placeholder)]
    if covers is not None:
        attributes["covers"] = covers
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


# --- P1 第一道:派工时 covers=id 绑定 → 子代理 DONE 按 id 打勾 -------------------------


def test_covers_binding_credits_nl_titled_target_by_id(tmp_path):
    """功能名类(自然语言标题)需求项:路径段证据永远对不上,covers 绑定按 id 就能打勾。
    这正是 P1 修的洞——上一棒只能治项目名类,功能名类全靠这道。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["注册登录", "全文搜索"])
    _write_child(task_root, "sub-a", covers=["req-01"])

    credited = reconcile_dispatch_coverage(_closeout(home, task_root), {}, home, _RUN_ID)

    assert credited == ["req-01"]
    statuses = _statuses(home)
    assert statuses["req-01"] == "done"  # covers 绑定 + 子代理 DONE → 按 id 打勾
    assert statuses["req-02"] == "pending"  # 没绑没证据 → 仍 open
    target = next(
        t for t in read_task_progress(home, _RUN_ID)["coverage"]["targets"] if t["id"] == "req-01"
    )
    assert target["source_ref"] == "auto:dispatch-covers-binding"
    assert "subagent-done:sub-a" in target["evidence"]
    # 瑕疵A回归:credit 打勾只更新 status/evidence/source_ref/notes,功能名 title 必须原样保留
    # (真机实锤:12 个 done 项 title 全被覆盖成 req-NN,账本"做了啥"看不清)。
    assert target["title"] == "注册登录"
    # 幂等:重跑不重复 credit、不碰别的项。
    assert reconcile_dispatch_coverage(_closeout(home, task_root), {}, home, _RUN_ID) == []


def test_covers_binding_deliberately_missed_item_stays_open_and_reworked(tmp_path):
    """验收判据:故意漏一项(没绑 covers 也没交付)→ 该项仍 open,coverage rework 照打回。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["注册登录", "全文搜索", "Dark Mode"])
    write_task_progress(home, _RUN_ID, {"items": [{"id": "i1", "title": "整合", "status": "done"}]})
    _write_child(task_root, "sub-a", covers=["req-01"])
    _write_child(task_root, "sub-b", covers=["req-02"])
    report = _report_with_artifact(tmp_path, "交付 login.html / search.js;Dark Mode 没做。")

    reworked = _drive_gate_then_rework(home, task_root, report)

    assert reworked is True  # 漏项仍 open → 返工门如实打回
    statuses = _statuses(home)
    assert statuses["req-01"] == "done" and statuses["req-02"] == "done"
    assert statuses["req-03"] == "pending"


def test_covers_binding_fully_bound_no_false_rework(tmp_path):
    """全部派工绑定且子代理全 DONE → 清单全打勾,返工门不误报打回。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["注册登录", "全文搜索"])
    write_task_progress(home, _RUN_ID, {"items": [{"id": "i1", "title": "整合", "status": "done"}]})
    _write_child(task_root, "sub-a", covers=["req-01"])
    _write_child(task_root, "sub-b", covers=["req-02"])
    report = _report_with_artifact(tmp_path, "交付 login.html / search.js。")

    reworked = _drive_gate_then_rework(home, task_root, report)

    assert reworked is False
    assert all(status == "done" for status in _statuses(home).values())


def test_covers_binding_unknown_id_inert(tmp_path):
    """绑了不存在的 id(拼错/瞎编)→ 不生效不抛错,真实项一个不误标。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["注册登录"])
    _write_child(task_root, "sub-a", covers=["req-99", "  ", ""])

    assert reconcile_dispatch_coverage(_closeout(home, task_root), {}, home, _RUN_ID) == []
    assert _statuses(home)["req-01"] == "pending"


def test_covers_binding_ignores_unfinished_and_placeholder_children(tmp_path):
    """绑定只认【DONE 且非占位空壳】的子代理:还在跑的、占位兜底的都不算数。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["注册登录", "全文搜索"])
    _write_child(task_root, "sub-a", status="RUNNING", covers=["req-01"])
    _write_child(task_root, "sub-b", placeholder=2, covers=["req-02"])

    assert reconcile_dispatch_coverage(_closeout(home, task_root), {}, home, _RUN_ID) == []
    assert all(status == "pending" for status in _statuses(home).values())


def test_covers_binding_skips_target_with_open_checks(tmp_path):
    """项上还有未闭环 checks(模型自定义细粒度维度)→ covers 不动它,checks 归模型自己管。"""
    home, task_root = _setup(tmp_path)
    write_task_progress(
        home,
        _RUN_ID,
        {
            "coverage": {
                "targets": [
                    {"id": "req-01", "title": "注册登录", "status": "pending", "checks": {"实现": "pending"}}
                ]
            }
        },
    )
    _write_child(task_root, "sub-a", covers=["req-01"])

    assert reconcile_dispatch_coverage(_closeout(home, task_root), {}, home, _RUN_ID) == []
    assert _statuses(home)["req-01"] == "pending"


def test_covers_binding_credits_model_created_target(tmp_path):
    """模型自立的 coverage 项(非自动种):covers 是派工方显式声明,同样按 id credit
    (路径段兜底仍只动自动种的项,行为不变,见 test_only_auto_seeded_targets_credited)。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["模块A"], auto=False)
    _write_child(task_root, "sub-a", covers=["req-01"])

    assert reconcile_dispatch_coverage(_closeout(home, task_root), {}, home, _RUN_ID) == ["req-01"]
    assert _statuses(home)["req-01"] == "done"


def test_covers_binding_and_path_evidence_compose(tmp_path):
    """两道判据同 run 各管各:功能名项走 covers、项目名项走路径段证据,一次对账都打上。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["注册登录", "agentscope-main"])
    _write_child(task_root, "sub-a", covers=["req-01"])
    report = _report_with_artifact(tmp_path, "分析见 agentscope-main/README.md。")

    credited = reconcile_dispatch_coverage(_closeout(home, task_root), report, home, _RUN_ID)

    assert credited == ["req-01", "req-02"]
    targets = {t["id"]: t for t in read_task_progress(home, _RUN_ID)["coverage"]["targets"]}
    assert targets["req-01"]["source_ref"] == "auto:dispatch-covers-binding"
    assert targets["req-02"]["source_ref"] == "auto:dispatch-coverage-reconcile"


def test_covers_binding_solo_untouched(tmp_path):
    """账本里有 open 项但无任何子代理(solo 路)→ covers 道也一字不动。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["注册登录"])

    assert reconcile_dispatch_coverage(_closeout(home, task_root), {}, home, _RUN_ID) == []
    assert _statuses(home)["req-01"] == "pending"


# --- P-bigbuild 树归并:整棵后代树的 covers/产物证据归并回父清单 -----------------------


def test_grandchild_covers_binding_credits_parent_target(tmp_path):
    """大工程递归转包形态:主代理派的子代理没绑 covers,子代理递归派的【孙代理】绑了
    (主账本回落让树深处的派工现场看得到主清单)→ 父对账把孙代理的绑定收回来打勾。
    这正是 u-big1/u-big2 断档的洞:只认直接孩子时父清单全程 0/24。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["用户管理", "消息中心"])
    _write_child(task_root, "sub-a")  # 直接孩子:没绑 covers(乱派现场)
    _write_child(task_root, "grand-b", parent_id="sub-a", covers=["req-01"])  # 孙代理绑了

    credited = reconcile_dispatch_coverage(_closeout(home, task_root), {}, home, _RUN_ID)

    assert credited == ["req-01"]
    statuses = _statuses(home)
    assert statuses["req-01"] == "done"
    assert statuses["req-02"] == "pending"  # 没人绑没证据 → 仍 open,推力不断
    target = next(
        t for t in read_task_progress(home, _RUN_ID)["coverage"]["targets"] if t["id"] == "req-01"
    )
    assert "subagent-done:grand-b" in target["evidence"]


def test_deep_descendant_chain_credits(tmp_path):
    """三层转包(子→孙→曾孙):曾孙的 covers 与声明产物路径都归并得回父清单。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["用户管理", "agentscope-main"])
    _write_child(task_root, "sub-a")
    _write_child(task_root, "grand-b", parent_id="sub-a")
    _write_child(
        task_root,
        "great-c",
        parent_id="grand-b",
        covers=["req-01"],
        output_files=["/x/out/agentscope-main/module.py"],
    )

    credited = reconcile_dispatch_coverage(_closeout(home, task_root), {}, home, _RUN_ID)

    assert credited == ["req-01", "req-02"]
    assert all(status == "done" for status in _statuses(home).values())


def test_grandchild_under_failed_child_still_counts(tmp_path):
    """孙代理真干完了活(DONE+covers),它的父(中间层子代理)却 FAILED → 孙的工作不被抹掉。
    树归并按 parent_id 闭包收后代,证据源只要求后代自身 DONE 非占位。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["用户管理"])
    _write_child(task_root, "sub-a", status="FAILED")
    _write_child(task_root, "grand-b", parent_id="sub-a", covers=["req-01"])

    assert reconcile_dispatch_coverage(_closeout(home, task_root), {}, home, _RUN_ID) == ["req-01"]


def test_sibling_subtree_not_counted(tmp_path):
    """兄弟 run 的整棵子树(兄弟 + 兄弟的孙)都不是我的后代 → 绑了 covers 也不算(兄弟隔离)。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["用户管理"])
    _write_child(task_root, "sub-sibling", parent_id="some-other-run", covers=["req-01"])
    _write_child(task_root, "grand-of-sibling", parent_id="sub-sibling", covers=["req-01"])

    assert reconcile_dispatch_coverage(_closeout(home, task_root), {}, home, _RUN_ID) == []
    assert _statuses(home)["req-01"] == "pending"


def test_placeholder_and_running_grandchildren_not_evidence(tmp_path):
    """孙代理还在跑 / DONE 但占位空壳 → 不作证据源(占位闸语义沿用到全树,空壳骗不到 credit)。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["用户管理", "消息中心"])
    _write_child(task_root, "sub-a")
    _write_child(task_root, "grand-running", parent_id="sub-a", status="RUNNING", covers=["req-01"])
    _write_child(task_root, "grand-placeholder", parent_id="sub-a", placeholder=2, covers=["req-02"])

    assert reconcile_dispatch_coverage(_closeout(home, task_root), {}, home, _RUN_ID) == []
    assert all(status == "pending" for status in _statuses(home).values())


def test_orphan_parent_cycle_terminates_and_not_counted(tmp_path):
    """parent_id 脏数据互指成环且不连到本 run → 不死循环、不误 credit(闭包只收得着的)。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["用户管理"])
    _write_child(task_root, "cyc-a", parent_id="cyc-b", covers=["req-01"])
    _write_child(task_root, "cyc-b", parent_id="cyc-a", covers=["req-01"])

    assert reconcile_dispatch_coverage(_closeout(home, task_root), {}, home, _RUN_ID) == []
    assert _statuses(home)["req-01"] == "pending"


# --- 不足2 第三道:后代账本自声明(descendant ledger claims)---------------------------


def _write_child_ledger(home: Path, child_id: str, update: dict) -> None:
    """后代在【自己账本】里的声明(run 现场用 task_progress 写的那本,root 同 owner home)。"""
    write_task_progress(home, child_id, update)


def test_descendant_ledger_claim_credits_nl_target(tmp_path):
    """对账少认的主修:功能名项、没绑 covers,但后代在自己账本按同 id 标 done+evidence
    (run 入口注入了主清单与自声明指引)→ 第三道按 id 归并回主清单。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["用户管理", "消息中心"])
    _write_child(task_root, "sub-a")  # 派工现场没绑 covers(乱派形态)
    _write_child_ledger(
        home,
        "sub-a",
        {"coverage": {"targets": [{"id": "req-01", "status": "done", "evidence": ["output/users/"]}]}},
    )

    credited = reconcile_dispatch_coverage(_closeout(home, task_root), {}, home, _RUN_ID)

    assert credited == ["req-01"]
    statuses = _statuses(home)
    assert statuses["req-01"] == "done"
    assert statuses["req-02"] == "pending"  # 没人声明 → 仍 open,推力不断
    target = next(
        t for t in read_task_progress(home, _RUN_ID)["coverage"]["targets"] if t["id"] == "req-01"
    )
    assert target["source_ref"] == "auto:descendant-ledger-reconcile"
    assert "descendant-ledger:sub-a" in target["evidence"]
    assert "output/users/" in target["evidence"]  # 后代的证据一并归并
    assert target["title"] == "用户管理"  # 打勾保 title 不回退


def test_descendant_ledger_claim_requires_evidence(tmp_path):
    """后代账本标 done 但没附 evidence → 不算完成声明(与需求项 done 证据闸同一纪律,防空勾)。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["用户管理"])
    _write_child(task_root, "sub-a")
    _write_child_ledger(home, "sub-a", {"coverage": {"targets": [{"id": "req-01", "status": "done"}]}})

    assert reconcile_dispatch_coverage(_closeout(home, task_root), {}, home, _RUN_ID) == []
    assert _statuses(home)["req-01"] == "pending"


def test_descendant_ledger_skipped_not_credited(tmp_path):
    """后代把某项标 skipped(它认为不适用)→ 不是完成声明,主清单不打勾(skip 判断归主代理)。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["用户管理"])
    _write_child(task_root, "sub-a")
    _write_child_ledger(
        home,
        "sub-a",
        {"coverage": {"targets": [{"id": "req-01", "status": "skipped", "evidence": ["x"]}]}},
    )

    assert reconcile_dispatch_coverage(_closeout(home, task_root), {}, home, _RUN_ID) == []
    assert _statuses(home)["req-01"] == "pending"


def test_descendant_ledger_items_row_also_counts(tmp_path):
    """后代用 items(而非 coverage.targets)记同 id 的 done+evidence → 同样认(两种记法真机都有)。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["用户管理"])
    _write_child(task_root, "sub-a")
    _write_child_ledger(
        home,
        "sub-a",
        {"items": [{"id": "req-01", "title": "用户管理", "status": "done", "evidence": ["output/users/api.py"]}]},
    )

    assert reconcile_dispatch_coverage(_closeout(home, task_root), {}, home, _RUN_ID) == ["req-01"]
    assert _statuses(home)["req-01"] == "done"


def test_descendant_ledger_running_child_claim_ignored(tmp_path):
    """还在跑的后代账本声明不算(证据源仍限 DONE 非占位后代——半成品不进账)。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["用户管理"])
    _write_child(task_root, "sub-a", status="RUNNING")
    _write_child_ledger(
        home,
        "sub-a",
        {"coverage": {"targets": [{"id": "req-01", "status": "done", "evidence": ["output/users/"]}]}},
    )

    assert reconcile_dispatch_coverage(_closeout(home, task_root), {}, home, _RUN_ID) == []


def test_descendant_ledger_grandchild_claim_credits(tmp_path):
    """孙代理账本里的自声明也归并(树闭包同一证据源口径)。"""
    home, task_root = _setup(tmp_path)
    _seed_requirement_coverage(home, ["用户管理"])
    _write_child(task_root, "sub-a")
    _write_child(task_root, "grand-b", parent_id="sub-a")
    _write_child_ledger(
        home,
        "grand-b",
        {"coverage": {"targets": [{"id": "req-01", "status": "done", "evidence": ["output/users/"]}]}},
    )

    assert reconcile_dispatch_coverage(_closeout(home, task_root), {}, home, _RUN_ID) == ["req-01"]


def test_descendant_ledger_target_with_open_checks_untouched(tmp_path):
    """主清单项还有未闭 checks → 第三道同样不动它(与第一道同规,checks 归模型管)。"""
    home, task_root = _setup(tmp_path)
    write_task_progress(
        home,
        _RUN_ID,
        {
            "coverage": {
                "targets": [
                    {"id": "req-01", "title": "用户管理", "status": "pending", "checks": {"实现": "pending"}}
                ]
            }
        },
    )
    _write_child(task_root, "sub-a")
    _write_child_ledger(
        home,
        "sub-a",
        {"coverage": {"targets": [{"id": "req-01", "status": "done", "evidence": ["output/users/"]}]}},
    )

    assert reconcile_dispatch_coverage(_closeout(home, task_root), {}, home, _RUN_ID) == []
    assert _statuses(home)["req-01"] == "pending"


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


def test_rework_payload_carries_open_target_titles_for_skip_exit(tmp_path):
    """瑕疵B回归(容忍+可跳过):打回载荷必须带 open 项 title——A2 字面枚举可能把需求
    结尾的指令碎片(如"别省略")种成假需求,模型只有看得见"这项是什么",才能行使
    "确认非功能 → 标 skipped 写原因"的双出口;只给 req-NN 的 id 双出口形同虚设。"""
    home, task_root = _setup(tmp_path)
    # 第二项模拟字面枚举混入的非功能碎片(真机形态:prompt 结尾"别用占位、别省略…")。
    _seed_requirement_coverage(home, ["注册登录", "别省略"])
    write_task_progress(home, _RUN_ID, {"items": [{"id": "i1", "title": "整合", "status": "done"}]})
    _write_child(task_root, "sub-a", covers=["req-01"])
    report = _report_with_artifact(tmp_path, "交付 login.html。")

    closeout = _closeout(home, task_root)
    decision = evaluate_task_progress_closeout_gate(closeout, report)
    report["task_progress_closeout_gate"] = decision.to_dict()
    params = SimpleNamespace(tool_context=[], executed_tools=[])

    assert coverage_incomplete_rework(params, report) is True
    payload = "\n".join(str(item) for item in params.tool_context)
    assert "别省略" in payload  # open 项 title 在载荷里,模型可判"做 or skipped"
    assert "skipped" in payload  # 双出口指令在场
    # 模型行使 skip 出口后,完整度判定认账(skipped=closed),"做全"不被假需求阻塞。
    write_task_progress(
        home,
        _RUN_ID,
        {"coverage": {"targets": [{"id": "req-02", "status": "skipped", "notes": "指令碎片,非功能"}]}},
    )
    progress = read_task_progress(home, _RUN_ID)
    assert progress["coverage"]["counts"]["targets_incomplete"] == 0
    skipped = next(t for t in progress["coverage"]["targets"] if t["id"] == "req-02")
    assert skipped["title"] == "别省略"  # skip 部分更新同样不丢 title(瑕疵A同根)
