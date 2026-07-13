from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...artifacts.registry import ArtifactRegistration, register_artifact, registry_path
from ...backends import ModelResponse
from ...contracts.artifact_acceptance import ArtifactAcceptanceRequest, validate_artifact
from .._runtime_params import ToolLoopExecuteParams
from ..run_task_workspace_writer import sync_run_task_workspace_closeout
from ..target_coverage_ledger import collect_target_coverage_records, target_coverage_status
from ..tool_guard.local_progress import reset_local_progress_guard
from .artifacts import _relative_report_ref, _write_report
from .evidence import target_coverage_projection_decision, target_coverage_projection_repair_message
from .expected_outputs_gate import evaluate_expected_outputs_gate
from .placeholder_density import placeholder_density_rework
from .recovery import attach_contract_recovery, failed_gate_payloads
from .source_volume import attach_source_volume_observation
from .subagent_aggregation import append_subagent_rework_context, evaluate_subagent_aggregation_gate
from .task_progress_gate import (
    coverage_incomplete_rework,
    evaluate_task_progress_closeout_gate,
    task_progress_ledger_present,
    task_progress_repair_message,
)
from .verification_evidence_gate import verification_evidence_rework

_PATH_TOKEN_RE = re.compile(
    r"(?P<path>"
    r"~[\\/][^\s'\"`<>()\[\]{}，。；;、]+"
    r"|(?<![:/])/(?!/)[^\s'\"`<>()\[\]{}，。；;、]+"
    r"|(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s'\"`<>()\[\]{}，。；;、]+"
    r"|\\\\[^\s'\"`<>()\[\]{}，。；;、]+"
    r")"
)


@dataclass(frozen=True)
class ArtifactPayloadRequest:
    record: dict[str, Any]
    path: Path
    target: dict[str, Any]
    archive_tool_calls: list[Any]
    workspace_root: Path | None


def uncontracted_task_output_closeout_response(
    request: object,
    workspace_root: Path,
) -> ModelResponse | None:
    artifacts = _uncontracted_current_artifacts(request, workspace_root)
    # 出口合同(P2-1/P5-1):零产物不再无条件早退——派过子代理的任务必须走完
    # closeout(让 SUBAGENTS_* gate 拦未收口、让空交付门要求结果文件),
    # 否则"口头放弃"零留档绕过所有门(R5b/R5c 实锤形态)。
    # §7-2 真机补:solo 一条龙把千行成品经 run_command/相对路径写到任务区外(owner
    # home 根)时,交付区 0 产物+无子代理原本也直接早退——无收口、无返工、无交付,
    # 用户什么都收不到(上轮误判"并发饿死"的真根因)。立过 task_progress 账且一次性
    # 提醒额度未花 → 进 closeout 让 _ledger_empty_delivery_rework 打回一次;额度已花
    # 仍空 → 早退走上层诚实失败出口(与既有"对质一次,二次放行诚实失败"同构)。
    children_present = _spawned_children_present(request)
    if not artifacts and not children_present and not _ledger_empty_rework_pending(request):
        return None
    params = request.params
    artifacts = _registered_artifacts(artifacts, workspace_root, params)
    delivery_mode = _delivery_mode_for_artifacts(artifacts)
    report = _uncontracted_base_report(params, workspace_root, artifacts, delivery_mode)
    report_ref = _write_report(workspace_root, report)
    report["report_ref"] = _relative_report_ref(report_ref, workspace_root)
    artifact_blocks = any(item.get("ok") is not True for item in artifacts)
    if coverage_status := _uncontracted_target_coverage_status(request, workspace_root):
        report["target_coverage_status"] = coverage_status
    projection_decision = target_coverage_projection_decision(report)
    report["target_coverage_projection_gate"] = projection_decision.to_dict()
    task_progress_decision = evaluate_task_progress_closeout_gate(request, report)
    report["task_progress_closeout_gate"] = task_progress_decision.to_dict()
    expected_outputs_decision = _attach_declared_reconciliation(request, report)
    decision = evaluate_subagent_aggregation_gate(request)
    report["subagent_aggregation_gate"] = decision.to_dict()
    decisions = [projection_decision, task_progress_decision, expected_outputs_decision, decision]
    coverage_blocks = coverage_status.get("should_block") is True if coverage_status else False
    # P5-1 空交付门:派过子代理但交付区零产物是客观事实——至少要交一份结果文件
    # (完成则交结果/汇总;不可行则交结构化不可行报告)。走返工,不是终态卡死。
    # solo+立过账的空交付走 _one_shot_rework_blocks 里的一次性提醒(哲学:质量类
    # 只温和打回一次),不进这个每轮硬门。
    empty_delivery_blocks = children_present and not artifacts
    if empty_delivery_blocks:
        _attach_empty_delivery_recovery(report)
    if coverage_blocks:
        _attach_uncontracted_target_coverage_recovery(report)
    if artifact_blocks:
        _attach_uncontracted_artifact_recovery(report)
    # 稳而不管(2026-06-12,PLAN-stability-not-control):阻断打回只守客观事实——
    # 产物打不开(artifact_blocks)/派过人零产物(empty_delivery)/未终态子代理与
    # open capreq(subagent gate)。质量与进度类 finding(task_progress open、
    # expected_outputs 数量、coverage 投影)照常写进报告供把关,但不再阻断退出:
    # R9 取证实锤,数量类打回驱动模型"凑数过门",单篇质量缩水 4 倍。
    if artifact_blocks or empty_delivery_blocks or not decision.allowed:
        _block_with_objective_rework(request, report, decisions)
        return None
    if _one_shot_rework_blocks(request, report, expected_outputs_decision):
        return None
    if coverage_blocks or not all(item.allowed for item in decisions):
        # 质量类未满足:ok 仍为 true 放行,报告里保留全部 gate 事实与 advisory。
        report["quality_advisories"] = failed_gate_payloads(report)
    _write_report(workspace_root, report)
    sync_run_task_workspace_closeout(request.agent, params, report)
    reset_local_progress_guard(request.agent, params)
    # 自学习复盘钩子(稳而不管 2-3,enable_self_learning 才跑):成功收口也回头
    # 看一眼,有可复用经验就记成待审草稿。
    from ..run_learning_review import maybe_run_learning_review

    maybe_run_learning_review(request.agent, params, report)
    return ModelResponse(text=_uncontracted_closeout_text(report), backend=request.backend)


# LLM: 一次性提醒类打回的归并入口(都幂等、二次放行,绝不卡死):①模型自我声明的
#   expected_outputs 缺口(_declared_gap_rework);②模型自己的 task_progress 账本还挂着
#   open 项就提交(_open_todo_rework,P1 守望真机实锤:第一个唤醒轮命中后账本写着
#   "第2轮盯守 in_progress/文件持续增长中"却当轮 submit_for_acceptance 收口——自动收口
#   有 open 项挡、显式提交没有,这里补对称);③任务要求真实跑测试却零测试执行证据
#   且交了代码产物(verification_evidence_rework,native 回归修复)。任一命中即打回。
# 函数用途: 跑完客观事实门后,再过一遍"温和提醒一次"的软门,命中则打回(True)。
def _one_shot_rework_blocks(request: object, report: dict[str, Any], expected_outputs_decision) -> bool:
    if _declared_gap_rework(getattr(request, "params", None), report, expected_outputs_decision):
        return True
    if _open_todo_rework(getattr(request, "params", None), report):
        return True
    # A3 覆盖对账(模型自声明 coverage 范围没对完账就提交,幂等一次;详见 task_progress_gate)。
    if coverage_incomplete_rework(getattr(request, "params", None), report):
        return True
    # P3 占位密度闸(交付代码占位记号密度明显过高,幂等一次;详见 placeholder_density)。
    if placeholder_density_rework(request, report):
        return True
    if _ledger_empty_delivery_rework(request, report):
        return True
    # P1 消费吞吐:盯守 spool 还有停摆的未判积压就收口(已抬升的候选没人判完=活没干完),
    # 幂等一次;详见 _watch_backlog_rework。
    if _watch_backlog_rework(request, report):
        return True
    return verification_evidence_rework(request, report)


_LEDGER_EMPTY_DELIVERY_MARKER = "[ledger-empty-delivery-rework]"
_WATCH_BACKLOG_MARKER = "[watch-spool-backlog-rework]"


# LLM: ⑤盯守积压一次性提醒(P1 消费吞吐真机实锤:引擎已把候选抬进 spool、消费者却停了,
#   主代理照常收口 → 已抬升的真事卡在缓冲区永远没人判)。判据全结构化:owner 名下未 close
#   盯守路的未判完计数(ack 口径)>0 且消费已停摆(>新鲜窗;有人正在消费的活岗不拦)。
#   幂等一次:打回让模型继续 pull 清账或显式 close(close 回执会亮弃判账);二次同形态
#   放行走 advisory,绝不死锁。
def _watch_backlog_rework(request: object, report: dict[str, Any]) -> bool:
    lanes = _stalled_watch_lanes(request)
    if not lanes:
        return False
    params = getattr(request, "params", None)
    context = getattr(params, "tool_context", None)
    if not isinstance(context, list) or any(_WATCH_BACKLOG_MARKER in str(item) for item in context):
        return False
    import json as _json

    payload = {
        "stalled_watch_lanes": lanes[:8],
        "unjudged_total": sum(int(row.get("unjudged_candidates") or 0) for row in lanes),
        "instruction": (
            "盯守缓冲区里还有已初筛抬升、未确认判完的候选,且没有人在消费(见 stalled_watch_lanes)"
            "——它们是盯守期内的事件,现在收尾等于把可能的真命中弃判。二选一后再提交:"
            "①继续 watch_stream action=pull 把积压逐条重判上报(或确认在岗子代理正在清账);"
            "②确认这路盯守不再需要,就 action=close 显式关闭(关闭回执会把弃判数如实入账)。"
            "不要在积压没人管的状态下直接收尾。"
        ),
    }
    context.append(f"{_WATCH_BACKLOG_MARKER}\n" + _json.dumps(payload, ensure_ascii=False))
    report["ok"] = False
    root_text = str(report.get("workspace_root") or "").strip()
    if root_text:
        _write_report(Path(root_text), report)
    return True


# 函数用途: 停摆的未判积压盯守路清单(判定本体在 wake_backstop,与补岗/自愈同一把尺)。
def _stalled_watch_lanes(request: object) -> list[dict[str, Any]]:
    agent = getattr(request, "agent", None)
    if agent is None:
        return []
    try:
        from ...ingestion.wake_backstop import stalled_unjudged_watch_lanes

        return stalled_unjudged_watch_lanes(agent)
    except Exception:
        return []


def _ledger_empty_rework_pending(request: object) -> bool:
    """立过账、且"账本空交付"一次性提醒额度未花 → True(该进 closeout 被打回一次)。
    额度已花(marker 在 tool_context)→ False,零产物早退走上层诚实失败出口。"""
    params = getattr(request, "params", None)
    context = getattr(params, "tool_context", None)
    if isinstance(context, list) and any(_LEDGER_EMPTY_DELIVERY_MARKER in str(item) for item in context):
        return False
    return task_progress_ledger_present(getattr(request, "agent", None), params)


# LLM: ④账本空交付一次性提醒(§7-2 真机实锤:solo 一条龙把 1083 行成品经 run_command/
#   相对路径写到 owner home 根,交付区 0 产物 → 原本 closeout 静默不触发,无收口无交付,
#   用户什么都收不到;此前"fake done"门只覆盖【证据文件不实存】的虚标形态,成品真实存在
#   但落错位置的形态漏网)。判据全客观:立过 task_progress 账 + 交付区零产物 + 没派子代理。
#   幂等一次:打回让模型把成品/汇总搬进任务交付目录(或交结构化不可行报告);二次仍空则
#   放行走诚实失败,绝不死锁。
def _ledger_empty_delivery_rework(request: object, report: dict[str, Any]) -> bool:
    if report.get("artifacts") or _spawned_children_present(request):
        return False
    if not _ledger_empty_rework_pending(request):
        return False
    params = getattr(request, "params", None)
    context = getattr(params, "tool_context", None)
    if not isinstance(context, list):
        return False
    _attach_empty_delivery_recovery(report)
    report["ok"] = False
    _write_report(Path(report["workspace_root"]), report)
    context.append(
        f"{_LEDGER_EMPTY_DELIVERY_MARKER}\n"
        "你的任务清单显示这个任务真干了活,但任务交付目录(task_output_dir)里没有任何产物文件。"
        "若成品写在了别处(工作目录/主目录下),把成品或其汇总落到任务交付目录再提交验收;"
        "任务确实无法完成则按 infeasibility_report_schema 写结构化不可行报告落到交付目录。"
        "不要在交付目录为空的状态下直接收尾。"
    )
    return True


# 函数用途: 客观事实阻断的统一收尾:报告标失败、附恢复动作、注入返工指令。
#   decisions 末位约定为 subagent gate(其 rework 注入有专用渲染)。
def _block_with_objective_rework(request: object, report: dict[str, Any], decisions: list[Any]) -> None:
    subagent_decision = decisions[-1]
    report["ok"] = False
    attach_contract_recovery(report, decisions, contract={})
    _write_report(Path(report["workspace_root"]), report)
    if not subagent_decision.allowed:
        append_subagent_rework_context(request.params, subagent_decision, report)
    _append_uncontracted_repair_context(request.params, report)


# LLM: 自我承诺单次提醒(R14c 实锤:模型自己声明 expected_outputs cn.pdf
#   min_count=1,两轮都在 0 实存时提前收口——quality_advisory 形态把关者可见
#   但模型收不到)。与 R9 凑数反噬的区别:①数量是模型自我声明非外部写死;
#   ②仅打回一次(幂等,二次同缺口放行进 advisories);③注入明确给双出口——
#   补齐,或用 task_progress 更新声明并说明原因(改声明合法,漂移留痕由对账
#   历史负责)。帮模型守自己的承诺,不替它选哪条路。
# 函数用途: 自我声明缺口且本 run 提醒额度未用 → 写报告打回(True);否则 False。
def _declared_gap_rework(params, report: dict[str, Any], decision) -> bool:
    if decision.allowed or not _declared_gap_rework_pending(params):
        return False
    _append_declared_gap_rework_context(params, report)
    report["ok"] = False
    _write_report(Path(report["workspace_root"]), report)
    return True


_DECLARED_GAP_MARKER = "[declared-outputs-rework]"
_OPEN_TODO_MARKER = "[open-todo-items-rework]"


# 模型自己声明"还没做完"的规范 open 状态。非规范完成别名(completed/ok/read…)不算——
#   那是标签不规范不是没做完,按四档裁决保持 L3 advisory 不拦(见
#   test_acceptance_submit_keeps_non_canonical_done_status_advisory 钉住的设计决策)。
_CANONICAL_OPEN_STATUSES = frozenset({"pending", "in_progress", "blocked"})


# 函数用途: 从 gate 顶层 evidence 与各 finding 的 evidence 里汇集 open_items(结构里
#   open_items 通常只在 finding evidence 中)。
def _gate_open_items(gate: dict[str, Any], evidence: dict[str, Any]) -> list:
    items = list(evidence.get("open_items") or []) if isinstance(evidence.get("open_items"), list) else []
    for finding in gate.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        finding_evidence = finding.get("evidence")
        if isinstance(finding_evidence, dict) and isinstance(finding_evidence.get("open_items"), list):
            items.extend(finding_evidence["open_items"])
    return items


# LLM: 账本挂真 open 项就提交的单次提醒(P1 守望真机实锤 + 接手文档 P4(a) 的"轻提醒对账")。
#   与 _declared_gap_rework 同款 R9-safe 三要素:①对的是模型【自己列的】待办清单且状态是它
#   自己写的 pending/in_progress/blocked(自我声明"没做完"),非外部配额;②幂等一次,二次同
#   形态放行进 advisory;③双出口——把活干完标 done,或确认不需要就标 done/skipped 写明原因,
#   改账本合法。非规范 done 别名不触发(保持 advisory,别为标签规范打回)。
# 函数用途: 模型自己的账本还挂"没做完"项就提交 → 打回一次让它对账(True);否则 False。
def _open_todo_rework(params, report: dict[str, Any]) -> bool:
    gate = report.get("task_progress_closeout_gate")
    if not isinstance(gate, dict) or gate.get("allowed") is True:
        return False
    evidence = gate.get("evidence") if isinstance(gate.get("evidence"), dict) else {}
    # open_items 落在 finding 的 evidence 里(gate 顶层 evidence 只有 open_count),两处都找。
    truly_open = [
        item
        for item in _gate_open_items(gate, evidence)
        if isinstance(item, dict) and str(item.get("status") or "").strip().lower() in _CANONICAL_OPEN_STATUSES
    ]
    if not truly_open:
        return False
    context = getattr(params, "tool_context", None)
    if not isinstance(context, list) or any(_OPEN_TODO_MARKER in str(item) for item in context):
        return False
    import json as _json

    # 标记行 + 纯 JSON(指令放 instruction 字段,不追加散文尾巴)——与 [delivery-closeout-check]
    #   同款可机读形态,测试/消费层按"首行标记+JSON"解析不被尾巴破坏。
    payload = {
        "open_count": len(truly_open),
        "open_items": truly_open[:12],
        "next_action": str(evidence.get("next_action") or ""),
        "progress_ref": str(evidence.get("progress_ref") or ""),
        "instruction": (
            "你自己的 task_progress 账本还有未完成项(见 open_items),现在提交会留下没做完的活。"
            "二选一后再提交:①把没做完的项继续做完,用 task_progress 标 done 并附证据;"
            "②如果这些项其实已完成或确认不再需要,用 task_progress 把状态改成 done/skipped 并写明原因。"
            "不要在账本仍挂 open 项的情况下收尾。"
        ),
    }
    context.append(f"{_OPEN_TODO_MARKER}\n" + _json.dumps(payload, ensure_ascii=False))
    report["ok"] = False
    root_text = str(report.get("workspace_root") or "").strip()
    if root_text:
        _write_report(Path(root_text), report)
    return True


# 函数用途: 本 run 是否还没用过"自我声明缺口"的那一次提醒机会。
def _declared_gap_rework_pending(params) -> bool:
    context = getattr(params, "tool_context", None)
    if not isinstance(context, list):
        return False
    return not any(_DECLARED_GAP_MARKER in str(item) for item in context)


# 函数用途: 把"你自己声明的交付还差什么"连同双出口写给模型(只一次)。
def _append_declared_gap_rework_context(params, report: dict[str, Any]) -> None:
    gate = report.get("expected_outputs_gate")
    findings = gate.get("findings", []) if isinstance(gate, dict) else []
    import json as _json

    params.tool_context.append(
        f"{_DECLARED_GAP_MARKER}\n"
        + _json.dumps({"unmet_declarations": findings[:8]}, ensure_ascii=False)
        + "\n这是你自己通过 task_progress 声明的交付承诺,当前交付区还没满足。"
        "二选一后再提交:①把缺的产物补齐;②如果确实无法完成,用 task_progress 更新"
        " expected_outputs 声明,并在交付报告里写明原因。不要在未兑现也未改声明的情况下收尾。"
    )


# 函数用途: 声明侧对账两件套:expected_outputs 对账门(声明驱动)+ 来源比例观测
#   (R8b 隐蔽编造实锤,检索量 vs 交付量并排数字,纯观测零判定)。
def _attach_declared_reconciliation(request: object, report: dict[str, Any]):
    expected_outputs_decision = evaluate_expected_outputs_gate(request)
    report["expected_outputs_gate"] = expected_outputs_decision.to_dict()
    attach_source_volume_observation(request, report)
    return expected_outputs_decision


def _uncontracted_current_artifacts(request: object, workspace_root: Path) -> list[dict[str, Any]]:
    artifacts = _current_run_task_output_artifacts(
        getattr(request, "params", None),
        workspace_root=_tool_workspace_root(getattr(request, "agent", None)),
    )
    if artifacts:
        return artifacts
    return _current_run_task_output_artifacts(getattr(request, "params", None), workspace_root=workspace_root)


def _uncontracted_base_report(
    run_params: ToolLoopExecuteParams,
    workspace_root: Path,
    artifacts: list[dict[str, Any]],
    delivery_mode: str,
) -> dict[str, Any]:
    return {
        "schema_version": "main_agent_delivery_closeout.v1",
        "ok": True,
        "case_id": "",
        "request_id": run_params.request_id,
        "run_id": run_params.run_id,
        "task_id": run_params.task_id,
        "workspace_root": str(workspace_root),
        "canonical_artifact_registry_ref": _relative_report_ref(registry_path(workspace_root), workspace_root),
        "artifacts": artifacts,
        "delivery_mode": delivery_mode,
        "message_zh": _message_for_delivery_mode(delivery_mode),
    }


def _append_uncontracted_repair_context(params: ToolLoopExecuteParams, report: dict[str, Any]) -> None:
    message = (
        task_progress_repair_message(report)
        or target_coverage_projection_repair_message(report)
        or _uncontracted_target_coverage_repair_message(report)
        or "当前交付物还没有通过结构化收口检查；请按 failed_gates 修复后重新提交。"
    )
    params.tool_context.append(
        "[delivery-closeout-check]\n"
        + json.dumps(
            {
                "ok": False,
                "report_ref": report.get("report_ref", ""),
                "failed_gates": failed_gate_payloads(report),
                "failed_artifacts": [
                    _closeout_artifact_payload(item)
                    for item in report.get("artifacts", [])
                    if isinstance(item, dict) and item.get("ok") is not True
                ],
                "target_coverage_status": report.get("target_coverage_status", {}),
                "repair_guidance": {
                    "mode": "closeout_rework",
                    "message_zh": message,
                    "submit_when_ready": "submit_for_acceptance",
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _uncontracted_target_coverage_status(
    request: object,
    workspace_root: Path,
) -> dict[str, object]:
    params = getattr(request, "params", None)
    contract = _delivery_contract(params)
    coverage = contract.get("target_coverage_contract")
    if not isinstance(coverage, dict):
        return {}
    coverage_root = _tool_workspace_root(getattr(request, "agent", None)) or workspace_root
    return target_coverage_status(
        coverage,
        coverage_records=collect_target_coverage_records(
            list(getattr(params, "archive_tool_calls", []) or []),
            workspace_root=coverage_root,
        ),
        workspace_root=coverage_root,
    )


def _delivery_contract(params: ToolLoopExecuteParams | None) -> dict[str, Any]:
    if params is None:
        return {}
    value = params.delivery_contract
    if isinstance(value, dict):
        return dict(value)
    attrs = params.task_attributes if isinstance(params.task_attributes, dict) else {}
    value = attrs.get("delivery_contract")
    return dict(value) if isinstance(value, dict) else {}


def _attach_uncontracted_target_coverage_recovery(report: dict[str, Any]) -> None:
    recovery = report.setdefault("contract_recovery", {})
    if not isinstance(recovery, dict):
        recovery = {}
        report["contract_recovery"] = recovery
    actions = recovery.get("required_actions")
    if not isinstance(actions, list):
        actions = []
    actions.extend(
        action
        for action in (
            "cover_missing_targets_before_submit",
            "update_final_artifact_after_required_coverage",
            "submit_for_acceptance_after_coverage_is_complete",
        )
        if action not in actions
    )
    recovery["required_actions"] = actions


def _attach_uncontracted_artifact_recovery(report: dict[str, Any]) -> None:
    recovery = report.setdefault("contract_recovery", {})
    if not isinstance(recovery, dict):
        recovery = {}
        report["contract_recovery"] = recovery
    actions = recovery.get("required_actions")
    if not isinstance(actions, list):
        actions = []
    actions.extend(
        action
        for action in (
            "rewrite_partial_final_artifacts_with_complete_write",
            "submit_for_acceptance_after_final_artifacts_are_complete",
        )
        if action not in actions
    )
    recovery["required_actions"] = actions


# LLM: P5-1 空交付门的事实判定:当前任务工作区是否真的派过子代理(work/agents 下
#   有 canonical)。只读文件系统事实,复用 subagent_aggregation 的 task_root 解析
#   与 open_task_state_summary(同一权威)。
# 函数用途: 回答"这轮任务到底有没有派过帮手"——派过就不允许零产物口头收尾。
def _spawned_children_present(request: object) -> bool:
    from .subagent_aggregation import _child_states, _current_task_root

    task_root = _current_task_root(request)
    if task_root is None:
        return False
    return bool(_child_states(task_root))


# LLM: P5-1 空交付门的返工指引(结构化,通用,零任务专项):任务完成→交结果文件;
#   不可行→交结构化不可行报告,schema 字段 tried_channels[](channel/evidence_ref/
#   failure_reason)+ untried_channels_known[](channel/why_not_tried)。让模型填
#   "已知未试渠道"这个字段本身倒逼探索完备性思考(R5b/R5c 绝对化结论的针对修复),
#   不解析自然语言、不做终态硬卡(走 rework)。
# 函数用途: 派过子代理却零产物时,告诉主代理"至少交一份结果文件,不可行也要留档"。
def _attach_empty_delivery_recovery(report: dict[str, Any]) -> None:
    report["empty_delivery_gate"] = {
        "allowed": False,
        "finding": "UNCONTRACTED_EMPTY_DELIVERY",
        "message_zh": (
            "本任务干过活（派过子代理或立过任务清单），但任务交付目录里没有任何产物文件；"
            "不允许只用口头结论收尾。若成品写在了别处（如工作目录/主目录下），把成品或其"
            "汇总落到任务交付目录（task_output_dir）再收口；任务完成则写结果/汇总文件；"
            "任务无法完成则写结构化不可行报告（按 infeasibility_report_schema 填已试渠道"
            "与证据、已知但未试的渠道及原因），落到任务交付目录后重新提交验收。"
        ),
        "infeasibility_report_schema": {
            "tried_channels": [
                {"channel": "渠道/方法名", "evidence_ref": "证据文件或调用记录引用", "failure_reason": "失败原因"}
            ],
            "untried_channels_known": [
                {"channel": "已知但未尝试的渠道", "why_not_tried": "未尝试原因"}
            ],
        },
    }
    recovery = report.setdefault("contract_recovery", {})
    if not isinstance(recovery, dict):
        recovery = {}
        report["contract_recovery"] = recovery
    actions = recovery.get("required_actions")
    if not isinstance(actions, list):
        actions = []
    actions.extend(
        action
        for action in (
            "write_result_or_infeasibility_report_into_task_output",
            "submit_for_acceptance_after_result_file_exists",
        )
        if action not in actions
    )
    recovery["required_actions"] = actions


def _uncontracted_target_coverage_repair_message(report: dict[str, Any]) -> str:
    status = report.get("target_coverage_status")
    if not isinstance(status, dict) or status.get("should_block") is not True:
        return ""
    hints = status.get("repair_hints") if isinstance(status.get("repair_hints"), list) else []
    hint_text = json.dumps(hints[:3], ensure_ascii=False, sort_keys=True)
    return (
        "当前验收失败是因为目录/来源覆盖清单还没完成。"
        "下一步按 target_coverage_status.repair_hints 的 recommended_tool_call 补读缺失目标；"
        "补完后更新最终交付物，再 submit_for_acceptance。"
        f" 当前可执行游标：{hint_text}"
    )


# LLM: 当前 run 产物候选的唯一收集口(uncontracted closeout 与出口合同
#   _has_final_closeout_candidate 共用)。两层:①写入记录路径(archive 里成功
#   write/run_command 记录的路径 refs,可附带 partial-write 检查);②交付目录
#   文件系统扫描兜底(R7b 实锤:长任务 compact 后内存 archive 丢失写入记录、
#   run_command 生成的文件记录里本就没有路径 ref → 24 个真实 xlsx 对 closeout
#   完全不可见,产物存在性本是文件系统客观事实)。扫描兜底只覆盖 task_output
#   scope(系统创建的任务交付目录)——绝不反向扫描 user_requested 目标(它们
#   提取自 prompt 文本,可能指向任意大目录,如把分析对象目录当交付物)。
# 函数用途: 回答"这轮任务到底交付了什么文件",记录看不见时直接看交付区。
def _current_run_task_output_artifacts(
    params: ToolLoopExecuteParams | None,
    *,
    workspace_root: Path | None = None,
) -> list[dict[str, Any]]:
    if params is None:
        return []
    targets = _accepted_output_targets(params)
    if not targets:
        return []
    artifacts: list[dict[str, Any]] = []
    archive_tool_calls = list(getattr(params, "archive_tool_calls", []) or [])
    for record in _successful_write_records(archive_tool_calls):
        artifacts.extend(
            _task_output_artifacts_from_record(
                record,
                targets,
                workspace_root=workspace_root,
                archive_tool_calls=archive_tool_calls,
            )
        )
    # 记录产物 ∪ 扫描产物的并集(R13c 实锤:write_file 只写了 1 个 md 清单,
    # 3 个 curl 下载的 PDF 因"已有记录产物"被旧的 if/else 短路,closeout 只见
    # 1/4 文件——交付目录里真实存在的文件就是交付事实,记录只是加速器不是
    # 封闭白名单,与 lesson 召回的并集修复同一设计裁决)。同路径记录优先。
    recorded = _unique_artifact_payloads(artifacts)
    recorded_paths = {str(item.get("path") or "") for item in recorded}
    scanned = [
        item
        for item in _artifacts_from_task_output_scan(targets)
        if str(item.get("path") or "") not in recorded_paths
    ]
    return recorded + scanned


# 交付区扫描的防御上限:超过即截断,防止异常交付区把 closeout 报告撑爆。
_OUTPUT_SCAN_MAX_FILES = 200

# 交付区扫描/展示排除的依赖、虚拟环境和缓存目录(A1-u1 实锤:npm install 的
# node_modules，以及 1.10 长任务的 output/.venv + .pytest_cache，都曾灌满
# 200 文件扫描上限，把真正交付物挤出 artifacts 清单)。只影响扫描与展示，
# 不删任何文件；模型显式 write_file 的记录产物不走这条排除。
_VENDOR_DIR_NAMES = frozenset(
    {
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".git",
    }
)


# LLM: 交付目录文件系统扫描(产物候选第②层,只走 task_output scope)。每个实存
#   文件照常过 validate_artifact(R3"md 改名 .pdf"形态仍被 opener 链拦),
#   payload 形态与记录路径产物一致,source 标 task_output_scan 以便审计区分。
# 函数用途: 写入记录丢了没关系——交付目录里真实存在的文件就是交付事实。
def _artifacts_from_task_output_scan(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for target in targets:
        if str(target.get("scope") or "") != "task_output":
            continue
        root = target.get("path")
        if not isinstance(root, Path) or not root.is_dir():
            continue
        _scan_target_dir(root, target, artifacts)
        if len(artifacts) >= _OUTPUT_SCAN_MAX_FILES:
            break
    return artifacts


# 函数用途: 扫描单个交付目录,把合格实存文件追加进产物列表(带防御上限,跳过 vendor 目录)。
def _scan_target_dir(root: Path, target: dict[str, Any], artifacts: list[dict[str, Any]]) -> None:
    for path in sorted(root.rglob("*")):
        if len(artifacts) >= _OUTPUT_SCAN_MAX_FILES:
            return
        if _in_vendor_dir(path, root):
            continue
        if not path.is_file() or not _is_task_output_file(path, target):
            continue
        acceptance = _artifact_acceptance_report(path, target)
        artifacts.append(
            {
                "artifact_id": path.name,
                "kind": path.suffix.lower().lstrip(".") or "file",
                "path": str(path),
                "ok": bool(acceptance.get("ok")),
                "acceptance_report": acceptance,
                "source": "task_output_scan",
                "output_scope": str(target.get("scope") or ""),
            }
        )


# 函数用途: 路径是否落在交付区内的 vendor/缓存子目录(只看 root 之下的目录段)。
def _in_vendor_dir(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    return any(part in _VENDOR_DIR_NAMES for part in parts[:-1])


def _registered_artifacts(
    artifacts: list[dict[str, Any]],
    workspace_root: Path,
    params: ToolLoopExecuteParams,
) -> list[dict[str, Any]]:
    registered: list[dict[str, Any]] = []
    for item in artifacts:
        record = register_artifact(
            ArtifactRegistration(
                workspace_root=workspace_root,
                path=str(item.get("path") or ""),
                artifact_id=str(item.get("artifact_id") or ""),
                run_id=str(getattr(params, "run_id", "") or ""),
                task_id=str(getattr(params, "task_id", "") or ""),
                agent_id=str(getattr(params, "run_id", "") or ""),
                kind=str(item.get("kind") or ""),
                source=str(item.get("source") or "current_run_tool_output"),
                created_by_tool="closeout",
                status="ready" if item.get("ok") is True else "invalid",
                metadata={"output_scope": str(item.get("output_scope") or "")},
            )
        )
        registered.append({**item, "registry_ref": record.to_dict()})
    return registered


def _tool_workspace_root(agent: object | None) -> Path | None:
    root = getattr(getattr(agent, "tools", None), "workspace_root", None) if agent is not None else None
    root = root or (getattr(agent, "root", None) if agent is not None else None)
    if not root:
        return None
    try:
        return Path(root).expanduser().resolve(strict=False)
    except OSError:
        return None


def _task_output_artifacts_from_record(
    record: dict[str, Any],
    targets: list[dict[str, Any]],
    *,
    workspace_root: Path | None = None,
    archive_tool_calls: list[Any] | None = None,
) -> list[dict[str, Any]]:
    return [
        _artifact_payload(ArtifactPayloadRequest(record, path, target, archive_tool_calls or [], workspace_root))
        for path in _produced_paths(record, workspace_root=workspace_root)
        for target in targets
        if _is_task_output_file(path, target)
    ]


def _successful_write_records(records: object) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict) and _successful_write_record(record)]


def _artifact_payload(request: ArtifactPayloadRequest) -> dict[str, Any]:
    acceptance_report = _artifact_acceptance_report(request.path, request.target)
    findings = list(acceptance_report.get("findings") if isinstance(acceptance_report.get("findings"), list) else [])
    if finding := _partial_unclosed_artifact_finding(request.record, request.path):
        findings.append(finding)
    if finding := _unrecovered_unclosed_write_finding(
        request.record,
        request.path,
        request.archive_tool_calls,
        request.workspace_root,
    ):
        findings.append(finding)
    ok = bool(acceptance_report.get("ok")) and not any(str(item.get("severity") or "") == "hard" for item in findings)
    acceptance_report = {**acceptance_report, "ok": ok}
    if findings:
        acceptance_report["findings"] = findings
    return {
        "artifact_id": str(request.record.get("call_id") or request.path.name),
        "kind": request.path.suffix.lower().lstrip(".") or "file",
        "path": str(request.path),
        "ok": ok,
        "acceptance_report": acceptance_report,
        "source": "current_run_tool_output",
        "output_scope": str(request.target["scope"]),
    }


def _artifact_acceptance_report(path: Path, target: dict[str, Any]) -> dict[str, Any]:
    return validate_artifact(
        ArtifactAcceptanceRequest(
            path=path,
            workspace_root=_validation_root_for_target(path, target),
            validation_contract={},
        )
    ).to_dict()


def _validation_root_for_target(path: Path, target: dict[str, Any]) -> Path:
    root = target.get("path")
    if isinstance(root, Path):
        return root if target.get("kind") == "dir" else root.parent
    return path.parent


def _partial_unclosed_artifact_finding(record: dict[str, Any], path: Path) -> dict[str, str]:
    params = record.get("parameters")
    if not isinstance(params, dict) or params.get("__partial_unclosed_write") is not True:
        return {}
    return {
        "code": "ARTIFACT_LAST_WRITE_PARTIAL_UNCLOSED",
        "severity": "hard",
        "message": "Final artifact path was last written by an incomplete partial write chunk.",
        "location": str(path),
        "value": str(record.get("call_id") or record.get("scoped_call_id") or ""),
        "action_zh": "最终交付物最后一次写入是半截分片；请用完整 write_file 覆盖或补成完整文件后再提交验收。",
    }


def _unrecovered_unclosed_write_finding(
    record: dict[str, Any],
    path: Path,
    archive_tool_calls: list[Any],
    workspace_root: Path | None,
) -> dict[str, str]:
    parse_errors = [
        item
        for item in archive_tool_calls
        if isinstance(item, dict)
        and str(item.get("tool") or "") == "__parse_error__"
        and str(item.get("error_code") or "") == "TOOL_CALL_UNCLOSED"
        and _record_targets_path(item, path, workspace_root)
    ]
    if len(parse_errors) < 2:
        return {}
    params = record.get("parameters")
    params = params if isinstance(params, dict) else {}
    if str(params.get("mode") or "overwrite") != "overwrite":
        return {}
    content = str(params.get("content") or "")
    max_chunk = _max_recovery_chunk_chars(parse_errors)
    if max_chunk <= 0 or len(content) > max_chunk * 2:
        return {}
    return {
        "code": "ARTIFACT_UNCLOSED_WRITE_RECOVERY_INCOMPLETE",
        "severity": "hard",
        "message": "Final artifact was accepted after repeated unclosed write_file attempts, but the latest overwrite is still only a small recovery chunk.",
        "location": str(path),
        "value": str(record.get("call_id") or record.get("scoped_call_id") or ""),
        "action_zh": "同一个最终文件多次长写入未闭合，最后只覆盖成一个小分片；请用 overwrite 写完整开头后，再用 mode=append 按 write_recovery.max_chunk_chars 分块续写，直到文件结构完整后再验收。",
    }


def _record_targets_path(record: dict[str, Any], path: Path, workspace_root: Path | None) -> bool:
    params = record.get("parameters")
    params = params if isinstance(params, dict) else {}
    raw = str(params.get("path") or "").strip()
    if not raw:
        recovery = params.get("write_recovery")
        if isinstance(recovery, dict):
            raw = str(recovery.get("path") or "").strip()
    if not raw:
        raw = str(record.get("source_input") or "").strip()
    if not raw or raw == "__parse_error__":
        return False
    return _canonical_path(Path(raw), workspace_root) == _canonical_path(path, workspace_root)


def _canonical_path(path: Path, workspace_root: Path | None) -> str:
    try:
        expanded = path.expanduser()
        if not expanded.is_absolute() and workspace_root is not None:
            expanded = workspace_root / expanded
        return str(expanded.resolve(strict=False))
    except OSError:
        return str(path)


def _max_recovery_chunk_chars(records: list[dict[str, Any]]) -> int:
    values = [_recovery_chunk_chars(record) for record in records]
    values = [value for value in values if value > 0]
    return max(values) if values else 0


def _recovery_chunk_chars(record: dict[str, Any]) -> int:
    params = record.get("parameters")
    recovery = params.get("write_recovery") if isinstance(params, dict) else None
    if not isinstance(recovery, dict):
        return 0
    try:
        return int(recovery.get("max_chunk_chars") or 0)
    except (TypeError, ValueError):
        return 0


def _task_output_dir(params: ToolLoopExecuteParams | None) -> Path | None:
    attrs = params.task_attributes if params is not None and isinstance(params.task_attributes, dict) else {}
    workspace = attrs.get("run_workspace")
    if not isinstance(workspace, dict):
        return None
    text = str(workspace.get("output_dir") or "").strip()
    return Path(text).expanduser().resolve(strict=False) if text else None


# LLM: 任务过程区 work/(batch1 G1 实锤:写代码任务模型在 work/ 反复迭代调试代码
#   28 次,最后只把 README 放 output/,代码这个主交付物滞留 work/,closeout 凭
#   README 误判完成)。my-agent 的 output/work 二分本是组织约定,但对"代码既是
#   过程又是成品"的任务,模型按开发习惯在 work/ 写完未必移交——与其用更硬的约定
#   逼模型(加限制),不如让交付识别也认 work/ 顶层的模型成品(交付事实优先于
#   位置,减少约定的认知负担)。只认顶层直接子文件、只走写入记录(系统状态文件
#   不是模型 write_file 写的,天然不入;agents/compact/shared 等系统子目录排除)。
# 函数用途: 取本 run 的过程区 work/ 目录(用于认其中模型写的顶层成品)。
def _task_work_dir(params: ToolLoopExecuteParams | None) -> Path | None:
    attrs = params.task_attributes if params is not None and isinstance(params.task_attributes, dict) else {}
    workspace = attrs.get("run_workspace")
    if not isinstance(workspace, dict):
        return None
    text = str(workspace.get("work_dir") or "").strip()
    return Path(text).expanduser().resolve(strict=False) if text else None


def _accepted_output_targets(params: ToolLoopExecuteParams) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    task_output = _task_output_dir(params)
    if task_output is not None:
        targets.append({"path": task_output, "kind": "dir", "scope": "task_output"})
    work_dir = _task_work_dir(params)
    if work_dir is not None:
        targets.append({"path": work_dir, "kind": "work_area", "scope": "task_work_area"})
    targets.extend(_user_requested_output_targets(params))
    return _unique_targets(targets)


def _user_requested_output_targets(params: ToolLoopExecuteParams) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    attrs = params.task_attributes if isinstance(params.task_attributes, dict) else {}
    workspace = attrs.get("run_workspace")
    if isinstance(workspace, dict):
        dir_text = str(workspace.get("user_requested_output_dir") or "").strip()
        if dir_text:
            targets.append(_output_target_for_user_path(Path(dir_text).expanduser(), force_kind="dir"))
        path_text = str(workspace.get("user_requested_output_path") or "").strip()
        if path_text:
            targets.append(_output_target_for_user_path(Path(path_text).expanduser()))
    prompt_text = "\n".join(
        text
        for text in (
            str(getattr(params, "root_user_prompt", "") or ""),
            str(getattr(params, "user_prompt", "") or ""),
        )
        if text
    )
    targets.extend(_output_target_for_user_path(path) for path in _absolute_paths_in_text(prompt_text))
    return _unique_targets(targets)


def _absolute_paths_in_text(text: str) -> list[Path]:
    if not text:
        return []
    paths: list[Path] = []
    for raw in _absolute_path_tokens_in_text(text):
        if _is_absolute_path_token(raw, platform_name=os.name):
            paths.append(Path(raw).expanduser())
    return paths


def _absolute_path_tokens_in_text(text: str) -> list[str]:
    source = str(text or "")
    tokens: list[str] = []
    for match in _PATH_TOKEN_RE.finditer(source):
        if _is_inside_url_token(source, match.start()):
            continue
        raw = _clean_path_token(match.group("path"))
        if raw:
            tokens.append(raw)
    return tokens


def _is_inside_url_token(text: str, start: int) -> bool:
    prefix = text[:start]
    token_start = max(prefix.rfind(" "), prefix.rfind("\t"), prefix.rfind("\n")) + 1
    return "://" in prefix[token_start:]


def _is_absolute_path_token(token: str, *, platform_name: str) -> bool:
    text = str(token or "").strip()
    if not text:
        return False
    if text.startswith("~"):
        return True
    if platform_name == "nt":
        return bool(re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith("\\\\"))
    return text.startswith("/")


def _clean_path_token(value: str) -> str:
    return str(value or "").strip().rstrip(".,;，。；、")


def _output_target_for_user_path(path: Path, *, force_kind: str | None = None) -> dict[str, Any]:
    resolved = path.resolve(strict=False)
    kind = force_kind or ("file" if resolved.suffix else "dir")
    return {"path": resolved, "kind": kind, "scope": "user_requested_output"}


def _successful_write_record(record: dict[str, Any]) -> bool:
    if record.get("ok") is not True:
        return False
    return str(record.get("tool") or "").strip() in {"write_file", "apply_patch", "run_command", "controlled_exec"}


def _produced_paths(record: dict[str, Any], *, workspace_root: Path | None = None) -> list[Path]:
    refs = _record_refs(record)
    paths: list[Path] = []
    for ref in dict.fromkeys(refs):
        if path := _produced_path_from_ref(ref, workspace_root):
            paths.append(path)
    return paths


def _produced_path_from_ref(ref: str, workspace_root: Path | None) -> Path | None:
    if "://" in ref:
        return None
    path = Path(ref).expanduser()
    if path.is_absolute():
        return path.resolve(strict=False)
    if workspace_root is None:
        return None
    return (workspace_root / path).resolve(strict=False)


def _record_refs(record: dict[str, Any]) -> list[str]:
    refs = [_text_ref(record.get(key)) for key in ("artifact_ref", "output_path", "path")]
    params = record.get("parameters")
    if isinstance(params, dict):
        refs.extend(_text_ref(params.get(key)) for key in ("path", "target_path", "output_path", "artifact_ref"))
    refs.extend(_record_ref_items(record.get("tool_result_refs"), ("path",)))
    refs.extend(_record_ref_items(record.get("artifact_registry_refs"), ("path",)))
    return [ref for ref in refs if ref]


def _record_ref_items(value: object, keys: tuple[str, ...]) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_first_record_ref(item, keys) for item in value if isinstance(item, dict)]


def _first_record_ref(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        text = _text_ref(item.get(key))
        if text:
            return text
    return ""


# 明显非交付物的临时/锁/缓存后缀：这些即使写在输出目录也不当作交付候选。
_NON_DELIVERABLE_SUFFIXES = frozenset(
    {".lock", ".tmp", ".temp", ".swp", ".swo", ".part", ".crdownload", ".pyc", ".pyo"}
)


# 函数用途: 判断写入路径是否落在某交付区(work_area=顶层直接子文件;file=精确;
#   dir=区内递归)。从 _is_task_output_file 抽出以降嵌套。
def _path_within_target(path: Path, output_root: Path, kind: str) -> bool:
    if kind == "work_area":
        return path.parent == output_root
    if kind == "file":
        return path == output_root
    try:
        path.relative_to(output_root)
    except ValueError:
        return False
    return True


def _is_task_output_file(path: Path, target: dict[str, Any]) -> bool:
    output_root = target.get("path")
    if not isinstance(output_root, Path):
        return False
    if not _path_within_target(path, output_root, str(target.get("kind") or "")):
        return False
    if not path.is_file():
        return False
    # 开放世界：输出目录里成功写出的文件都算候选交付物，交给验收层按格式核验
    # （已登记格式走 opener，未登记走通用兜底 + 第 0 层）。只排除明显的临时/锁/缓存
    # 与隐藏文件——不用封闭格式白名单，否则主代理声称交付的 .pdf 等格式会被无声忽略，
    # 连被验收的机会都没有（R3 实测：markdown 改名成 .pdf 蒙混过关）。
    if path.name.startswith("."):
        return False
    return path.suffix.lower() not in _NON_DELIVERABLE_SUFFIXES


def _delivery_mode_for_artifacts(artifacts: list[dict[str, Any]]) -> str:
    scopes = {str(item.get("output_scope") or "") for item in artifacts}
    if "user_requested_output" in scopes:
        return "uncontracted_user_requested_output"
    return "uncontracted_task_output"


def _message_for_delivery_mode(delivery_mode: str) -> str:
    if delivery_mode == "uncontracted_user_requested_output":
        return "没有结构化交付合同，但本轮已写入用户明确指定路径下的交付物（实际类型与文件见 artifacts 清单）；通过当前 run 产物验收（仅验客观可打开性、不预设产物类型）；主代理停止继续工具循环。"
    return "没有结构化交付合同，但本轮已写入 task output 下的交付物（实际类型与文件见 artifacts 清单）；通过当前 run 产物验收（仅验客观可打开性、不预设产物类型）；主代理停止继续工具循环。"


def _unique_artifact_payloads(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    order: list[str] = []
    by_path: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        key = str(artifact.get("path") or "")
        if not key:
            continue
        if key not in by_path:
            order.append(key)
        by_path[key] = artifact
    return [by_path[key] for key in order]


def _unique_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for target in targets:
        path = target.get("path")
        if not isinstance(path, Path):
            continue
        kind = str(target.get("kind") or "")
        if kind not in {"file", "dir", "work_area"}:
            continue
        key = (str(path), kind)
        if key in seen:
            continue
        seen.add(key)
        result.append(target)
    return result


def _uncontracted_closeout_text(report: dict[str, Any]) -> str:
    advisories = report.get("quality_advisories") or []
    payload = {
        "ok": True,
        # 大白话任务无结构化交付合同,框架未对产物逐项核验——给用户/上层机读信号,
        # 避免把"收口门未发现客观阻断"误读成"产物已被验收核实"。
        "validated": False,
        "case_id": "",
        "report_ref": report.get("report_ref", ""),
        "delivery_mode": report.get("delivery_mode", ""),
        "artifacts": [_closeout_artifact_payload(item) for item in report["artifacts"]],
    }
    if advisories:
        payload["quality_advisories"] = advisories
    note = (
        "交付验收通过。本轮已把产物写入 task output（实际交付物类型与文件以下方 artifacts 清单为准，本门只验客观可打开性、不预设产物类型）。主代理停止继续工具循环。\n"
        "⚠️ 注意:本次是无结构化交付合同的大白话任务,“验收通过”仅表示收口门未发现客观阻断,"
        "框架并未对产物逐项核验(validated=false);完成情况以实际产物为准,请自行确认结果是否正确、完整。"
    )
    if advisories:
        note += " 另有质量项未达标,详见 quality_advisories。"
    return (
        "[MAIN_AGENT_DELIVERY_COMPLETE]\n"
        + json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n[/MAIN_AGENT_DELIVERY_COMPLETE]\n"
        + note
    )


def _closeout_artifact_payload(item: dict[str, Any]) -> dict[str, object]:
    return {
        "artifact_id": item["artifact_id"],
        "kind": item["kind"],
        "path": item["path"],
        "ok": item["ok"],
    }


def _text_ref(value: object) -> str:
    return str(value or "").strip()


__all__ = ["uncontracted_task_output_closeout_response"]
