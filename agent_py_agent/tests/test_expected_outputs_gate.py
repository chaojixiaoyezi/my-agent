"""产物类型/数量对账门钉子(REFACTORING_BACKLOG"产物类型/数量对账门",实锤
R6b/R6c:prompt 要求 24 周/每篇一个 PDF,实交 1 个 md/0 个 PDF,closeout 仍 ok=true)。

钉死四层契约:
1. 声明驱动零误伤:不声明 expected_outputs 的任务完全不受影响(unchecked allow)。
2. 开放世界对账:pattern 带扩展名即声明类型(声明 *.pdf 只有 md → 缺失);
   min_count 声明数量(要 24 个只有 1 个 → 缺失);满足即 allow;repair 非硬卡死。
3. 存储层:声明随 task_progress 账本持久化、合并(同 pattern 覆盖/新 pattern 追加/
   不带声明的更新不丢已有声明)、坏条目(空/越界)丢弃。
4. closeout 端到端:uncontracted 路径声明不足 → ok=false + EXPECTED_OUTPUTS_MISSING;
   补齐产物后重新提交 → 通过。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.delivery_closeout.expected_outputs_gate import (
    evaluate_expected_outputs_gate,
)
from agent_py_agent.agent.agent_core.tool_loop.final_exit_contract import (
    FinalExitRequest,
    FinalExitState,
    final_exit_closeout_decision,
)
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.task_progress import (
    merge_task_progress,
    normalize_expected_outputs,
    read_task_progress,
    write_task_progress,
)

pytestmark = pytest.mark.integration

_RUN_ID = "run-expected"


def _agent(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        config=AgentConfig(),
        tools=SimpleNamespace(workspace_root=tmp_path),
        root=tmp_path,
    )


def _params(task_root: Path, *, archive_tool_calls: list | None = None) -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt="测试任务",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        granted_capabilities=None,
        write_boundary=None,
        task_attributes={
            "run_workspace": {
                "task_root": str(task_root),
                "output_dir": str(task_root / "output"),
                "work_dir": str(task_root / "work"),
            }
        },
        request_id="req-expected",
        run_id=_RUN_ID,
        task_id=_RUN_ID,
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=list(archive_tool_calls or []),
    )


def _closeout(agent: SimpleNamespace, params: ToolLoopExecuteParams) -> SimpleNamespace:
    return SimpleNamespace(agent=agent, params=params, backend="echo")


def _declare(root: Path, expected_outputs: list) -> None:
    write_task_progress(root, _RUN_ID, {"expected_outputs": expected_outputs})


# ---------------------------------------------------------------------------
# 1. 声明驱动零误伤
# ---------------------------------------------------------------------------


def test_no_declaration_is_unchecked_allow(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    decision = evaluate_expected_outputs_gate(_closeout(agent, _params(tmp_path / "tasks" / "t0")))
    assert decision.allowed is True
    assert decision.evidence["checked"] is False
    assert decision.evidence["reason"] == "no_expected_outputs_declared"


# ---------------------------------------------------------------------------
# 2. 开放世界对账
# ---------------------------------------------------------------------------


def test_type_mismatch_md_instead_of_pdf_is_caught(tmp_path: Path) -> None:
    """R6c 形态:声明每篇一个 PDF,实际只交 md → 缺失打回。"""
    agent = _agent(tmp_path)
    task_root = tmp_path / "tasks" / "t-pdf"
    output = task_root / "output"
    output.mkdir(parents=True)
    (output / "论文清单.md").write_text("# 清单", encoding="utf-8")
    _declare(tmp_path, [{"pattern": "*.pdf", "min_count": 6, "note": "每篇一个中文 PDF"}])

    decision = evaluate_expected_outputs_gate(_closeout(agent, _params(task_root)))

    assert decision.allowed is False
    finding = decision.findings[0]
    assert finding.code == "EXPECTED_OUTPUTS_MISSING"
    assert finding.severity == "medium", "对账走返工,不是硬卡死"
    missing = finding.evidence["missing"][0]
    assert missing["pattern"] == "*.pdf"
    assert missing["min_count"] == 6
    assert missing["actual_count"] == 0


def test_count_shortfall_is_caught_and_satisfied_passes(tmp_path: Path) -> None:
    """R6b 形态:声明 24 周文件只交 1 个 → 缺失;补齐后 → 放行。"""
    agent = _agent(tmp_path)
    task_root = tmp_path / "tasks" / "t-weeks"
    output = task_root / "output"
    output.mkdir(parents=True)
    (output / "WEEK24.md").write_text("第24周", encoding="utf-8")
    _declare(tmp_path, [{"pattern": "WEEK*.md", "min_count": 24}])

    short = evaluate_expected_outputs_gate(_closeout(agent, _params(task_root)))
    assert short.allowed is False
    assert short.findings[0].evidence["missing"][0]["actual_count"] == 1

    for week in range(1, 24):
        (output / f"WEEK{week:02d}.md").write_text(f"第{week}周", encoding="utf-8")
    full = evaluate_expected_outputs_gate(_closeout(agent, _params(task_root)))
    assert full.allowed is True
    assert full.evidence["reconciliation"][0]["actual_count"] == 24


def test_directories_do_not_count_as_outputs(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    task_root = tmp_path / "tasks" / "t-dir"
    output = task_root / "output"
    (output / "report.pdf").mkdir(parents=True)  # 同名目录不算交付物
    _declare(tmp_path, [{"pattern": "report.pdf"}])

    decision = evaluate_expected_outputs_gate(_closeout(agent, _params(task_root)))

    assert decision.allowed is False
    assert decision.findings[0].evidence["missing"][0]["actual_count"] == 0


# ---------------------------------------------------------------------------
# 3. 存储层:声明持久化与合并
# ---------------------------------------------------------------------------


def test_declaration_survives_updates_without_field(tmp_path: Path) -> None:
    _declare(tmp_path, [{"pattern": "*.pdf", "min_count": 3}])
    write_task_progress(tmp_path, _RUN_ID, {"items": [{"id": "step-1", "status": "done", "notes": "已检索"}]})

    progress = read_task_progress(tmp_path, _RUN_ID)

    assert progress["expected_outputs"] == [{"pattern": "*.pdf", "min_count": 3}]
    assert progress["items"][0]["id"] == "step-1"


def test_same_pattern_overrides_and_new_pattern_appends(tmp_path: Path) -> None:
    _declare(tmp_path, [{"pattern": "*.pdf", "min_count": 3}])
    _declare(tmp_path, [{"pattern": "*.pdf", "min_count": 6}, {"pattern": "清单.md"}])

    declared = read_task_progress(tmp_path, _RUN_ID)["expected_outputs"]

    assert declared == [
        {"pattern": "*.pdf", "min_count": 6},
        {"pattern": "清单.md", "min_count": 1},
    ]


def test_bad_entries_are_dropped() -> None:
    declared = normalize_expected_outputs(
        {
            "expected_outputs": [
                {"pattern": "../escape.md"},  # 路径逃逸
                {"pattern": ""},  # 空
                "直接字符串.md",  # 字符串简写
                {"pattern": "ok.pdf", "min_count": "bad"},  # 坏数量回退 1
                42,  # 坏类型
            ]
        }
    )
    assert declared == [
        {"pattern": "直接字符串.md", "min_count": 1},
        {"pattern": "ok.pdf", "min_count": 1},
    ]


def test_merge_preserves_note_and_order() -> None:
    base = {"expected_outputs": [{"pattern": "a.md", "note": "说明A"}]}
    update = {"expected_outputs": [{"pattern": "b.pdf", "min_count": 2, "note": "说明B"}]}
    merged = merge_task_progress(base, update, run_id=_RUN_ID)
    assert merged["expected_outputs"] == [
        {"pattern": "a.md", "min_count": 1, "note": "说明A"},
        {"pattern": "b.pdf", "min_count": 2, "note": "说明B"},
    ]


# ---------------------------------------------------------------------------
# 4. closeout 端到端(uncontracted 路径经出口合同触发)
# ---------------------------------------------------------------------------


def test_scan_fallback_sees_artifacts_without_write_records(tmp_path: Path) -> None:
    """R7b 实锤钉子(缺陷④):compact 后写入记录丢失 / run_command 生成文件无
    路径 ref 时,closeout 必须靠交付目录扫描看见真实产物并跑完对账门。"""
    agent = _agent(tmp_path)
    task_root = tmp_path / "tasks" / "t-scan"
    output = task_root / "output"
    output.mkdir(parents=True)
    for week in range(1, 25):
        (output / f"WEEK{week:02d}.xlsx").write_bytes(b"PK\x03\x04zip-like")
    _declare(tmp_path, [{"pattern": "WEEK*.xlsx", "min_count": 24}])
    params = _params(task_root)  # archive_tool_calls 为空 = R7b compact 后形态

    decision = final_exit_closeout_decision(
        FinalExitRequest(agent, params, SimpleNamespace(text="任务完成。", backend="echo"), FinalExitState())
    )

    report = json.loads((task_root / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))
    assert report.get("reason") != "delivery_contract_missing", "有真实产物不得落入合同缺失兜底"
    assert len(report["artifacts"]) == 24, "扫描兜底必须看见全部 24 个交付文件"
    assert all(item["source"] == "task_output_scan" for item in report["artifacts"])
    assert report["expected_outputs_gate"]["allowed"] is True, "对账门必须有出场机会且通过"
    del decision


def test_contract_without_required_artifacts_falls_back_to_real_outputs(tmp_path: Path) -> None:
    """R7a 实锤钉子(缺陷②):路由注入的空壳合同(无 required_artifacts/coverage)
    不得压制真实产物——必须回落 uncontracted 验收链而非误导性打回。"""
    agent = _agent(tmp_path)
    task_root = tmp_path / "tasks" / "t-shell"
    output = task_root / "output"
    output.mkdir(parents=True)
    (output / "分析报告.md").write_text("# 分析\n真实内容", encoding="utf-8")
    params = _params(task_root)
    # 空壳合同:过 doctor(artifacts 是 list)但零 required artifact(R7a 路由注入形态)
    params.task_attributes["delivery_contract"] = {"case_id": "shell-case", "artifacts": []}

    decision = final_exit_closeout_decision(
        FinalExitRequest(agent, params, SimpleNamespace(text="做完了。", backend="echo"), FinalExitState())
    )

    report = json.loads((task_root / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))
    assert report.get("reason") != "required_artifacts_missing", "空壳合同不得再打出误导性缺产物指令"
    assert any(item["path"].endswith("分析报告.md") for item in report["artifacts"])
    del decision


def test_uncontracted_closeout_records_declared_gap_without_blocking(tmp_path: Path) -> None:
    """语义升级(R14c 实锤,自我承诺单次提醒):模型自我声明的缺口第一次打回
    提醒(双出口:补齐/改声明),同缺口第二次放行 ok=true+advisories——
    R9 防凑数核心(绝不无限数量打回)由幂等一次+改声明合法出口保留。"""
    agent = _agent(tmp_path)
    task_root = tmp_path / "tasks" / "t-e2e"
    output = task_root / "output"
    output.mkdir(parents=True)
    report_file = output / "调研报告.md"
    report_file.write_text("# 调研\n内容", encoding="utf-8")
    _declare(tmp_path, [{"pattern": "数据.xlsx", "min_count": 1, "note": "必须 xlsx 交付"}])
    archive = [
        {
            "tool": "write_file",
            "call_id": "1-1",
            "ok": True,
            "parameters": {"path": str(report_file)},
            "output": "written",
        }
    ]
    params = _params(task_root, archive_tool_calls=archive)
    state = FinalExitState()

    first = final_exit_closeout_decision(
        FinalExitRequest(agent, params, SimpleNamespace(text="做完了。", backend="echo"), state)
    )
    assert first.should_continue is True, "自我声明缺口:第一次提醒打回"

    second = final_exit_closeout_decision(
        FinalExitRequest(agent, params, SimpleNamespace(text="做完了。", backend="echo"), state)
    )
    assert second.should_continue is False, "同缺口第二次放行,绝不无限数量打回"
    report = json.loads((task_root / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))
    assert report["ok"] is True, "质量类缺口不阻断验收"
    gate = report["expected_outputs_gate"]
    assert gate["allowed"] is False, "对账事实必须保留在报告里供把关"
    assert gate["findings"][0]["code"] == "EXPECTED_OUTPUTS_MISSING"
    assert any(
        item.get("gate") == "expected_outputs_reconciliation"
        for item in report.get("quality_advisories", [])
    ), "质量缺口进 advisory 投影"


def test_recorded_and_scanned_artifacts_union(tmp_path: Path) -> None:
    """R13c 实锤钉子:write_file 只记录了 1 个 md,3 个 run_command 下载的 PDF
    实存于交付区——产物清单必须是记录 ∪ 扫描的并集(记录非封闭白名单),
    closeout 必须看见全部 4 个文件。"""
    agent = _agent(tmp_path)
    task_root = tmp_path / "tasks" / "t-union"
    output = task_root / "output"
    output.mkdir(parents=True)
    listing = output / "paper_list.md"
    listing.write_text("# 清单\n3 篇论文", encoding="utf-8")
    for pid in ("2601.20552", "2601.07372", "2602.21548"):
        (output / f"{pid}_en.pdf").write_bytes(b"%PDF-1.4 fake-but-headered")
    archive = [
        {
            "tool": "write_file",
            "call_id": "9-1",
            "ok": True,
            "parameters": {"path": str(listing)},
            "output": "written",
        }
    ]
    params = _params(task_root, archive_tool_calls=archive)

    decision = final_exit_closeout_decision(
        FinalExitRequest(agent, params, SimpleNamespace(text="任务完成。", backend="echo"), FinalExitState())
    )

    report = json.loads((task_root / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))
    paths = [item["path"] for item in report["artifacts"]]
    assert len(paths) == 4, f"记录 1 + 扫描 3 必须全见,实际 {len(paths)}: {paths}"
    assert sum(p.endswith(".pdf") for p in paths) == 3, "三个下载 PDF 不得被记录产物屏蔽"
    sources = {item["path"]: item.get("source") for item in report["artifacts"]}
    assert sources[str(listing)] != "task_output_scan", "同路径记录优先"
    del decision


def test_self_declared_gap_gets_single_rework_then_passes(tmp_path: Path) -> None:
    """R14c 实锤钉子:模型自我声明的 expected_outputs 缺口→单次打回提醒
    (注入含双出口:补齐 or 改声明);同缺口第二次→放行进 advisories(不凑数)。"""
    agent = _agent(tmp_path)
    task_root = tmp_path / "tasks" / "t-promise"
    output = task_root / "output"
    output.mkdir(parents=True)
    (output / "papers_list.md").write_text("# 论文清单\n\n| 标题 | ID |\n|---|---|\n| 示例论文 | 0000.00001 |\n", encoding="utf-8")
    _declare(tmp_path, [{"pattern": "*_cn.pdf", "min_count": 1}])
    params = _params(task_root)
    state = FinalExitState()

    first = final_exit_closeout_decision(
        FinalExitRequest(agent, params, SimpleNamespace(text="任务完成。", backend="echo"), state)
    )
    assert first.should_continue is True, "自我承诺未兑现:第一次必须打回提醒"
    joined = "\n".join(str(item) for item in params.tool_context)
    assert "[declared-outputs-rework]" in joined
    assert "补齐" in joined and "更新" in joined, "双出口都要给"

    second = final_exit_closeout_decision(
        FinalExitRequest(agent, params, SimpleNamespace(text="还是这些。", backend="echo"), state)
    )
    assert second.should_continue is False, "第二次同缺口放行,不逼凑数"
    report = json.loads((task_root / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert report.get("quality_advisories"), "缺口事实保留给把关者"
