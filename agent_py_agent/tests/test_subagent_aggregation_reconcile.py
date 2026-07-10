from __future__ import annotations

import tempfile
from pathlib import Path

# 防回归: 真实任务测试(M2.7 子代理协作)暴露——子代理声明 output_ref 路径目录名拼写错,
# 但实际产物在 workspace 内同名 basename, 交付门却照声明的错路径判"缺失"→死循环 rework
# (即使产物在、集成测试过),违背永不停机。修复: _declared_ref_missing 在声明路径不存在时,
# 在该子代理 workspace_root 内按 basename 兜底对账,命中即放行;找不到才记缺失(护 R4 声明40实交1)。
from agent_py_agent.agent.agent_core.delivery_closeout.subagent_aggregation import (
    _declared_ref_missing,
)


def _workspace_with_file(rel_path: str) -> tuple[Path, Path]:
    root = Path(tempfile.mkdtemp())
    target = root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x", encoding="utf-8")
    return root, target


def test_typo_declared_path_reconciled_by_basename():
    # 声明路径目录名拼写错(实际产物在 workspace 内同名 basename)→ 不应误判缺失。
    root, _ = _workspace_with_file("output/analyzer.py")
    typo = "/Users/WRONGUSER/some/wrong/dir/output/analyzer.py"
    assert _declared_ref_missing(typo, str(root)) is False


def test_truly_missing_still_flagged():
    # 护 R4 声明40实交1: workspace 内无同名产物时仍判缺失(不放跑吹牛)。
    root, _ = _workspace_with_file("output/analyzer.py")
    typo = "/Users/WRONGUSER/wrong/dir/output/never_written_zzz.py"
    assert _declared_ref_missing(typo, str(root)) is True


def test_declared_path_exists_unchanged():
    # 原行为: 声明路径真实存在 → 不缺失。
    root, real = _workspace_with_file("output/report.md")
    assert _declared_ref_missing(str(real), str(root)) is False


def test_no_workspace_root_absolute_missing_is_conservative():
    # workspace_root 为空、绝对路径不存在 → 无对账依据,保守判缺失(与原行为一致)。
    assert _declared_ref_missing("/Users/x/nonexistent_abc_zzz.py", "") is True


# ---- 回归③钉子:编队并行时,子代理收口只数【自己的后代】,不把兄弟当"未完成的孩子" ----
# 真机实锤:5 路盯源编队共享一个 task_root,聚合门按目录全扫 → 每个子代理提交都被
# SUBAGENTS_UNFINISHED(其实是兄弟)打回,一路被拖成 BLOCKED→CANCELLED。

import json
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.delivery_closeout.subagent_aggregation import (
    evaluate_subagent_aggregation_gate,
)

_MAIN_RUN = "req_test_main"


def _fleet_task_root(tmp_path, members: dict[str, dict]) -> Path:
    agents = tmp_path / "work" / "agents"
    for run_id, extra in members.items():
        d = agents / run_id
        d.mkdir(parents=True, exist_ok=True)
        payload = {"id": run_id, "run_id": run_id, "status": "RUNNING", "parent_id": _MAIN_RUN, **extra}
        (d / "canonical_state.json").write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path


def _closeout(task_root: Path, run_id: str):
    return SimpleNamespace(
        params=SimpleNamespace(
            run_id=run_id,
            task_attributes={"run_workspace": {"task_root": str(task_root)}},
        ),
        agent=None,
    )


def test_fleet_sibling_not_counted_as_own_child(tmp_path):
    # 子代理 A 收口:兄弟 B 还 RUNNING(parent=主 run)→ 不是 A 的孩子,放行
    root = _fleet_task_root(tmp_path, {"subagent-A": {"status": "DONE"}, "subagent-B": {}})
    decision = evaluate_subagent_aggregation_gate(_closeout(root, "subagent-A"))
    assert decision.allowed, decision.to_dict()


def test_main_agent_still_blocked_by_unfinished_fleet(tmp_path):
    # 主代理收口:编队(parent=主 run)有未终态 → 照旧打回(原语义不回退)
    root = _fleet_task_root(tmp_path, {"subagent-A": {"status": "DONE"}, "subagent-B": {}})
    decision = evaluate_subagent_aggregation_gate(_closeout(root, _MAIN_RUN))
    assert not decision.allowed
    assert any(f.code == "SUBAGENTS_UNFINISHED" for f in decision.findings)


def test_subagent_blocked_by_its_own_grandchild(tmp_path):
    # 子代理 A 派了孙代理(parent=A)且未终态 → A 收口仍要被拦(自己的孩子自己管)
    root = _fleet_task_root(tmp_path, {
        "subagent-A": {"status": "DONE"},
        "subagent-A-child": {"parent_id": "subagent-A"},
    })
    decision = evaluate_subagent_aggregation_gate(_closeout(root, "subagent-A"))
    assert not decision.allowed


def test_legacy_state_without_parent_counts_conservatively(tmp_path):
    # 无 parent_id 的老数据:保守按原行为算进来(不放走真未收口)
    root = _fleet_task_root(tmp_path, {"subagent-legacy": {"parent_id": ""}})
    decision = evaluate_subagent_aggregation_gate(_closeout(root, _MAIN_RUN))
    assert not decision.allowed
