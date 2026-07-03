from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...contracts.gates.models import GateDecision, GateFinding
from ...contracts.recovery import RecoveryAction
from ...subagents.model_capabilities import capability_request_requires_parent_resolution
from ...subagents.models import (
    SUBAGENT_FAILURE_STATUSES,
    SUBAGENT_RESOLVED_TERMINAL_STATUSES,
    TaskStatus,
    task_status_in,
)
from ...subagents.services.output_alignment import looks_like_output_path
from ...subagents.tool_failure_ledger import tool_failure_code_counts


@dataclass(frozen=True)
class SubagentAggregationIssues:
    open_capability_request_children: list[dict[str, Any]]
    unfinished: list[dict[str, Any]]
    unresolved: list[dict[str, Any]]
    undelivered: list[dict[str, Any]]

    def any(self) -> bool:
        return bool(
            self.open_capability_request_children or self.unfinished or self.unresolved or self.undelivered
        )


def evaluate_subagent_aggregation_gate(closeout: object) -> GateDecision:
    task_root = _current_task_root(closeout)
    if task_root is None:
        return GateDecision.allow("subagent_aggregation", evidence={"checked": False, "reason": "task_root_missing"})
    # 只数 closing run 自己的【直接后代】(parent_id 归属),不数 task 下的全部 run:
    # 编队并行时 task_root/work/agents/ 混着一批兄弟,按目录全扫会让每个子代理的收口
    # 把【兄弟】当成"自己未完成的孩子"被 SUBAGENTS_UNFINISHED 打回(真机回归③实锤:
    # 盯源子代理提交被 sibling 聚合门拦→BLOCKED→整路被取消)。主代理收口数编队(其
    # parent_id=主 run)、子代理收口数自己派的孙代理;孙代理未完由其父自己的收口拦,
    # 主代理经直接孩子传递覆盖全树。当事人自己与无 parent_id 的老数据保持原行为。
    params = getattr(closeout, "params", None)
    self_run_id = str(getattr(params, "run_id", "") or "").strip()
    accepted_parents = _accepted_parent_ids(params, self_run_id, task_root)
    children = [
        item
        for item in _child_states(task_root)
        if _is_own_child(item, self_run_id, accepted_parents)
    ]
    if not children:
        return _allowed_decision(task_root, children)
    issues = SubagentAggregationIssues(
        open_capability_request_children=[item for item in children if _has_open_capability_request(item)],
        unfinished=[item for item in children if _is_unfinished(item)],
        unresolved=[item for item in children if _is_unresolved_failure(item)],
        undelivered=_undelivered_done_children(children),
    )
    if issues.any():
        return _rework_decision(task_root, children, issues)
    return _allowed_decision(task_root, children)


def _allowed_decision(task_root: Path, children: list[dict[str, Any]]) -> GateDecision:
    return GateDecision.allow(
        "subagent_aggregation",
        evidence={
            "checked": True,
            "task_root": str(task_root),
            "child_count": len(children),
            **({"terminal_statuses": _status_counts(children)} if children else {}),
        },
    )


def _rework_decision(
    task_root: Path,
    children: list[dict[str, Any]],
    issues: SubagentAggregationIssues,
) -> GateDecision:
    return GateDecision.repair(
        "subagent_aggregation",
        _rework_findings(issues),
        recommended_action=RecoveryAction.REPAIR.value,
        evidence=_blocked_evidence(task_root, children, issues),
    )


def _rework_findings(issues: SubagentAggregationIssues) -> list[GateFinding]:
    findings: list[GateFinding] = []
    if issues.open_capability_request_children:
        findings.append(_open_capability_finding(issues.open_capability_request_children))
    if issues.unfinished:
        findings.append(
            GateFinding(
                "SUBAGENTS_UNFINISHED",
                "P1",
                message=(
                    "当前任务还有子代理没有完成或没有被明确接管/取消；"
                    "提交前需要等待完成，或用 cancel_subagents/takeover 明确处理后再汇总。"
                ),
                evidence={"unfinished_run_ids": _run_ids(issues.unfinished)},
            )
        )
    if issues.unresolved:
        findings.append(
            GateFinding(
                "SUBAGENTS_UNRESOLVED",
                "P1",
                message=(
                    "当前任务还有失败或阻塞的子代理没有被明确处理；"
                    "提交前需要取消、接管、重跑或在最终交付中说明处理结果。"
                ),
                evidence={"unresolved_run_ids": _run_ids(issues.unresolved)},
            )
        )
    if issues.undelivered:
        findings.append(_undelivered_finding(issues.undelivered))
    return findings


# LLM: A3 结构化软引导：evidence 带 recommended_tool 与可直接传参的 request id 清单，
#   主代理不必从文本猜工具名（R4b 实测 0 次调用 resolve_capability_requests 的
#   针对性修复）。只做报告字段引导，不拦主链路。
# 函数用途: 构造"能力申请待裁决"finding，并直接告诉主代理调什么工具、传哪些 id。
def _open_capability_finding(children: list[dict[str, Any]]) -> GateFinding:
    return GateFinding(
        "SUBAGENTS_CAPABILITY_REQUESTS_OPEN",
        "P1",
        message=(
            "当前任务还有子代理能力申请未处理；请用 resolve_capability_requests"
            "（decision=grant/deny，request_ids 见 evidence）逐项裁决，"
            "或取消/接管对应子代理后再提交。"
        ),
        evidence={
            "open_capability_request_run_ids": _run_ids(children),
            "recommended_tool": "resolve_capability_requests",
            "open_capability_request_ids": _all_open_request_ids(children),
        },
    )


# 函数用途: 构造"声明产物缺失"finding（R4 声明 40 实交 1 形态的拦截证据）。
def _undelivered_finding(undelivered: list[dict[str, Any]]) -> GateFinding:
    return GateFinding(
        "SUBAGENTS_DECLARED_OUTPUTS_MISSING",
        "P1",
        message=(
            "有子代理状态为完成、但派工时声明的产物文件在声明位置缺失；"
            "提交前需要补齐产物、重派任务，或确实可接受时用 "
            "resolve_capability_requests(decision=accept_output_gaps) 显式登记豁免。"
        ),
        evidence={
            "undelivered": [
                {
                    "run_id": str(item.get("run_id") or ""),
                    "missing_refs": list(item.get("missing_refs") or [])[:10],
                    "missing_count": int(item.get("missing_count") or 0),
                }
                for item in undelivered[:10]
            ]
        },
    )


def _blocked_evidence(
    task_root: Path,
    children: list[dict[str, Any]],
    issues: SubagentAggregationIssues,
) -> dict[str, Any]:
    return {
        "checked": True,
        "task_root": str(task_root),
        "child_count": len(children),
        "open_capability_request_children": _compact_children(issues.open_capability_request_children),
        "unfinished_children": _compact_children(issues.unfinished),
        "unresolved_children": _compact_children(issues.unresolved),
        "undelivered_children": list(issues.undelivered[:10]),
        "required_actions": [
            "inspect_agent_tree",
            # 与真实工具名一致（曾写作 resolve_open_capability_requests，易诱导模型
            # 调用不存在的工具名）。
            "resolve_capability_requests",
            "read_child_result_or_wait_for_done",
            "cancel_subagents_or_takeover_if_child_is_no_longer_needed",
            "deliver_missing_declared_outputs_or_explain",
            "merge_child_outputs_before_submit_for_acceptance",
        ],
    }


def append_subagent_rework_context(params: object, decision: GateDecision, report: dict[str, Any]) -> None:
    tool_context = getattr(params, "tool_context", None)
    if not isinstance(tool_context, list):
        return
    tool_context.append(
        "[subagent-aggregation-check]\n"
        + json.dumps(
            {
                "ok": False,
                "report_ref": report.get("report_ref", ""),
                "failed_gate": decision.to_dict(),
                "repair_guidance": {
                    "mode": "subagent_aggregation_rework",
                    "required_actions": list(decision.evidence.get("required_actions") or []),
                    "message_zh": (
                        "提交前建议查看当前任务子代理状态，读取已完成子代理结果；"
                        "仍在运行的继续等待或补充引导，确定不需要的用 cancel_subagents 明确取消，"
                        "然后把所有已完成/已处理的子代理结果合并进最终报告；如直接接管或跳过，应在最终报告里说明原因。"
                    ),
                    "submit_when_ready": "submit_for_acceptance",
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _current_task_root(closeout: object) -> Path | None:
    params = getattr(closeout, "params", None)
    attrs = getattr(params, "task_attributes", None)
    if root := _task_root_from_attrs(attrs):
        return root
    workspace = getattr(getattr(closeout, "agent", None), "_current_run_task_workspace", None)
    root = getattr(workspace, "task_root", None) or getattr(workspace, "root", None)
    return Path(root).expanduser().resolve(strict=False) if root else None


def _task_root_from_attrs(attrs: object) -> Path | None:
    workspace = attrs.get("run_workspace") if isinstance(attrs, dict) else None
    if not isinstance(workspace, dict):
        return None
    text = str(workspace.get("task_root") or "").strip()
    return Path(text).expanduser().resolve(strict=False) if text else None


def _accepted_parent_ids(params: object, self_run_id: str, task_root: Path) -> frozenset[str]:
    """closing run 认领"我的孩子"的 parent_id 集合。

    主代理(default scope)收口:除本轮 run_id 外还认【根任务 id】(task_root 目录名)——
    gateway 的后台整合轮 run_id 是 bg-main-thread-*,而编队子代理的 parent_id 落的是
    根请求 id;只按 run_id 匹配会把整支编队滤成 0 孩子,聚合门形同虚设(真机实锤:
    4 个 BLOCKED 子代理在场,门 child_count=0 恒放行=假绿)。子代理收口(task_local 等
    非 default scope)保持只认自己 run_id:兄弟隔离语义不变。
    """
    accepted = {self_run_id} if self_run_id else set()
    scope = str(getattr(params, "context_scope", "") or "default").strip().lower()
    if scope in {"", "default"}:
        root_id = str(task_root.name or "").strip()
        if root_id:
            accepted.add(root_id)
    return frozenset(accepted)


def _is_own_child(item: dict[str, Any], self_run_id: str, accepted_parent_ids: frozenset[str]) -> bool:
    """closing run 的直接后代才算"我的孩子"。当事人自己不算(自指拦截会永远收不了口);
    读得出 parent_id 的按归属判;读不出的(STATE_UNREADABLE/老数据)保守按原行为算进来。"""
    if str(item.get("run_id") or "") == self_run_id:
        return False
    parent_id = str(item.get("parent_id") or "").strip()
    if not parent_id:
        return True
    return parent_id in accepted_parent_ids


def _child_states(task_root: Path) -> list[dict[str, Any]]:
    agents_dir = task_root / "work" / "agents"
    if not agents_dir.exists():
        return []
    children: list[dict[str, Any]] = []
    for path in sorted(agents_dir.glob("*/canonical_state.json")):
        payload = _read_json(path)
        if not payload:
            children.append(
                {
                    "run_id": path.parent.name,
                    "status": "STATE_UNREADABLE",
                    "canonical_state_ref": str(path),
                }
            )
            continue
        payload.setdefault("run_id", payload.get("id") or path.parent.name)
        payload.setdefault("canonical_state_ref", str(path))
        children.append(payload)
    return children


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _is_unfinished(item: dict[str, Any]) -> bool:
    status = _status(item)
    return not task_status_in(
        status,
        {TaskStatus.DONE.value} | SUBAGENT_RESOLVED_TERMINAL_STATUSES | SUBAGENT_FAILURE_STATUSES,
    )


def _is_unresolved_failure(item: dict[str, Any]) -> bool:
    return task_status_in(_status(item), SUBAGENT_FAILURE_STATUSES)


# LLM: R4 子项③的"声明产物对账"：只对 DONE 的子代理查账（失败/未完成的由其他
#   issue 拦），声明意图位置（attributes.output_files/output_refs，相对路径按该子代理
#   task_workspace_dir 解析）缺文件即记缺失。这是产物存在性的客观事实门。
# 函数用途: 找出"号称完成但声明产物缺失"的子代理（R4 声明 40 实交 1 的形态）。
def _undelivered_done_children(children: list[dict[str, Any]]) -> list[dict[str, Any]]:
    undelivered: list[dict[str, Any]] = []
    for item in children:
        if not task_status_in(_status(item), {TaskStatus.DONE.value}):
            continue
        missing = _missing_declared_refs(item)
        if missing:
            undelivered.append(
                {
                    "run_id": str(item.get("run_id") or ""),
                    "missing_refs": missing[:20],
                    "missing_count": len(missing),
                }
            )
    return undelivered


# LLM: 缺失清单会先扣除主代理显式登记的豁免（attributes.output_delivery_exemptions，
#   由 accept_subagent_output_gaps 工具写入）。通配 "*" 表示整个子代理豁免（纯汇报
#   任务/已确认接受），返回空缺失。豁免是结构化、可审计的，不是静默放水。
# 函数用途: 解析一个子代理声明产物的实际位置并返回缺失清单（已扣除豁免）。
def _missing_declared_refs(item: dict[str, Any]) -> list[str]:
    attrs = item.get("attributes")
    attrs = attrs if isinstance(attrs, dict) else {}
    exempt_refs, exempt_all = _output_gap_exemptions(attrs)
    if exempt_all:
        return []
    workspace_root = str(item.get("task_workspace_dir") or "").strip()
    declared = [
        text
        for field_name in ("output_files", "output_refs")
        for text in _declared_path_texts(attrs.get(field_name))
    ]
    missing = [
        text
        for text in declared
        if _declared_ref_missing(text, workspace_root) and text not in exempt_refs
    ]
    return list(dict.fromkeys(missing))


# 函数用途: 读子代理已登记的产物缺失豁免，返回 (豁免 ref 集合, 是否整体通配豁免)。
def _output_gap_exemptions(attrs: dict[str, Any]) -> tuple[set[str], bool]:
    records = attrs.get("output_delivery_exemptions")
    if not isinstance(records, list):
        return set(), False
    refs = {str(r.get("ref") or "").strip() for r in records if isinstance(r, dict)}
    refs.discard("")
    exempt_all = "*" in refs
    refs.discard("*")
    return refs, exempt_all


# 函数用途: 取一个声明字段里路径形态的条目（滤掉非字符串/空值/非路径声明）。
def _declared_path_texts(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    texts: list[str] = []
    for raw in value:
        text = raw.strip() if isinstance(raw, str) else ""
        if text and looks_like_output_path(text):
            texts.append(text)
    return texts


# 函数用途: 判定一条声明产物是否缺失（相对路径按该子代理 task_workspace_dir 解析）。
def _declared_ref_missing(text: str, workspace_root: str) -> bool:
    path = Path(text)
    if not path.is_absolute():
        if not workspace_root:
            return False
        path = Path(workspace_root) / path
    if path.exists():
        return False
    # 兜底对账(R4 子项③韧性): 声明路径不存在时,用其尾部(父目录段+文件名)在该子代理
    # workspace_root 内做后缀匹配——命中说明"声明路径前缀打错字(如用户名/深目录拼写错)但产物
    # 真写出来了",不记缺失,避免一个拼写错让交付死循环 rework(违背永不停机)。用后缀匹配(非纯
    # basename)避免 requirements.txt 等常见名跨目录误命中;找不到才是真缺失(护"声明40实交1")。
    return not _declared_product_in_workspace(path, workspace_root)


# 函数用途: 用声明路径尾部(父目录段+basename)在 workspace_root 内做后缀匹配(兜底对账,
#   比纯 basename 严,避免常见文件名跨目录误命中)。
def _declared_product_in_workspace(declared: Path, workspace_root: str) -> bool:
    root = Path(workspace_root)
    if not workspace_root or not root.is_dir():
        return False
    try:
        candidates = list(root.rglob(declared.name))
    except OSError:
        return False
    parent_name = declared.parent.name
    return any(_product_suffix_matches(found, parent_name) for found in candidates)


# 函数用途: 判断候选文件是否匹配声明产物尾部(是文件且父目录段一致/无父目录段约束)。
def _product_suffix_matches(found: Path, parent_name: str) -> bool:
    return found.is_file() and (not parent_name or found.parent.name == parent_name)


def _has_open_capability_request(item: dict[str, Any]) -> bool:
    # 已了结终态(CANCELLED/ABANDONED/TAKEN_OVER)的子代理永远不会再执行,它遗留的
    # OPEN 申请没有任何可执行的裁决意义——再拦只会让"取消了结"救不回收尾(真机拖死链)。
    # cancel_subagents 现会顺手 CLOSED 这些申请;此处兜住历史数据与崩溃遗留。
    if task_status_in(_status(item), SUBAGENT_RESOLVED_TERMINAL_STATUSES):
        return False
    requests = item.get("capability_requests")
    if not isinstance(requests, list):
        return False
    return any(_capability_request_open(request) for request in requests)


def _capability_request_open(request: object) -> bool:
    if isinstance(request, dict):
        return capability_request_requires_parent_resolution(request.get("status", "OPEN"))
    return capability_request_requires_parent_resolution(getattr(request, "status", "OPEN"))


def _status(item: dict[str, Any]) -> str:
    return str(item.get("status") or item.get("state") or "").strip().upper() or "UNKNOWN"


def _compact_children(children: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in children[:20]:
        rows.append(
            {
                "run_id": str(item.get("run_id") or item.get("id") or ""),
                "status": _status(item),
                "progress": item.get("progress"),
                "channel_status": str(item.get("channel_status") or ""),
                "summary": str(item.get("latest_summary") or "")[:240],
                "open_capability_request_ids": _open_capability_request_ids(item),
                # 系统级工具失败账本投影(A1):error_code→次数。summary 是模型转述,
                # 这里是系统事实——模型声称 WRITE_FORBIDDEN 而此处为空时,归因幻觉
                # 在 closeout 报告里直接可见(R4b 形态)。只观测,不做门。
                "tool_failure_codes": tool_failure_code_counts(item.get("attributes")),
                # P2-2 占位符明示:该子代理交付位里有几个是兜底摘要而非真产物。
                "placeholder_artifact_count": _placeholder_artifact_count(item.get("attributes")),
                "canonical_state_ref": str(item.get("canonical_state_ref") or ""),
            }
        )
    return rows


def _open_capability_request_ids(item: dict[str, Any]) -> list[str]:
    requests = item.get("capability_requests")
    if not isinstance(requests, list):
        return []
    ids: list[str] = []
    for request in requests:
        if not _capability_request_open(request):
            continue
        if isinstance(request, dict):
            request_id = str(request.get("id") or "").strip()
        else:
            request_id = str(getattr(request, "id", "") or "").strip()
        if request_id:
            ids.append(request_id)
    return ids


# LLM: A3 软引导的数据源：聚合所有子代理的 open capability request id（去重保序，
#   上限 20 防报告膨胀），主代理可直接把它们传给 resolve_capability_requests。
# 函数用途: 收齐"等待主代理裁决"的能力申请 id 清单，放进 finding evidence。
def _all_open_request_ids(children: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for item in children:
        _extend_unique_ids(ids, _open_capability_request_ids(item))
    return ids[:20]


# 函数用途: 数一个子代理的占位符产物数(attributes.placeholder_artifacts,P2-2)。
def _placeholder_artifact_count(attrs: object) -> int:
    attrs = attrs if isinstance(attrs, dict) else {}
    entries = attrs.get("placeholder_artifacts")
    return len(entries) if isinstance(entries, list) else 0


# 函数用途: 把新 id 去重追加进清单（保持先见先记的顺序）。
def _extend_unique_ids(ids: list[str], new_ids: list[str]) -> None:
    for request_id in new_ids:
        if request_id not in ids:
            ids.append(request_id)


# 函数用途: "未收口子代理"的唯一判据(非终态 or 未处理失败);open_task_state_summary
#   的 open_children 计数与 open_children_states 的名单都走它,保证两处永不漂移。
def _is_open_child(item: dict[str, Any]) -> bool:
    return _is_unfinished(item) or _is_unresolved_failure(item)


# LLM: 未收口(非终态 / 未处理失败)第一层子代理的 payload 名单——与
#   open_task_state_summary 的 open_children 计数同一判据(_is_open_child)。
#   P2 非阻塞出口门的后台活性判据(background_liveness)据此逐个 run_id 查 pid/线程。
#   task_root 为 None/不存在时返回空。只读 canonical 文件事实,不加载 manager。
# 函数用途: 给出"这轮还没收口的子代理"具体名单(带 run_id),供活性判据逐个核查。
def open_children_states(task_root: Path | None) -> list[dict[str, Any]]:
    if task_root is None:
        return []
    return [item for item in _child_states(Path(task_root)) if _is_open_child(item)]


# LLM: 出口合同(tool_loop/final_exit_contract)的事实源:统计任务工作区里
#   "未收口"的子代理(非终态)与 open capability_request 数。只读 canonical
#   文件系统事实,复用本模块的 _child_states/_is_open_child/_capability_request
#   谓词(同一权威,不另造判定)。task_root 为 None/不存在时返回全零(纯问答
#   run 零影响)。
# 函数用途: 回答"这轮任务还有没有没收口的子代理/能力申请",给 run 出口做依据。
def open_task_state_summary(task_root: Path | None) -> dict[str, int]:
    if task_root is None:
        return {"open_children": 0, "open_capability_requests": 0, "children_total": 0}
    children = _child_states(Path(task_root))
    open_children = [item for item in children if _is_open_child(item)]
    open_requests = sum(len(_open_capability_request_ids(item)) for item in children)
    return {
        "open_children": len(open_children),
        "open_capability_requests": int(open_requests),
        # 含全部已收口的子代理总数:P5-1 空交付门需要"派过帮手"这个事实
        # (哪怕全终态,零产物也不许口头收尾)。
        "children_total": len(children),
    }


def _run_ids(children: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("run_id") or "") for item in children]


def _status_counts(children: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in children:
        status = _status(item)
        counts[status] = counts.get(status, 0) + 1
    return counts


__all__ = [
    "append_subagent_rework_context",
    "evaluate_subagent_aggregation_gate",
    "open_children_states",
    "open_task_state_summary",
]
