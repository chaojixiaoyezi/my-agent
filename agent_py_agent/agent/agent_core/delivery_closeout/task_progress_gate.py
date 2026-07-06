from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ...contracts.gates.models import GateDecision, GateFinding
from ...contracts.recovery import RecoveryAction
from ...task_progress import (
    progress_path,
    read_task_progress,
    task_progress_status_is_closed,
    task_progress_status_is_done,
    task_progress_summary,
)
from .dispatch_coverage_reconcile import reconcile_dispatch_coverage


@dataclass(frozen=True)
class ArtifactEvidenceProjectionRequest:
    report: dict[str, Any]
    progress: dict[str, Any]
    run_id: str
    path: Path
    summary: dict[str, Any]


def task_progress_has_open_items(closeout: object) -> bool:
    """模型自己列的 task_progress 清单里是否还有开放(未 done/skipped)项。

    供交付前的"别提前放行"判据复用(completion 自动收口 / 出口续航同源):只认模型
    自己声明的待办,清单空 / 没建过账本一律 False(不误伤没用清单的普通任务)。
    """
    root = _progress_root(closeout)
    run_id = _run_id(closeout)
    if not root or not run_id:
        return False
    path = progress_path(root, run_id)
    if not path.exists():
        return False
    return bool(_open_items(read_task_progress(root, run_id)))


# LLM: 假标 done 形态判据(A3-u3 真机实锤:模型卡住后把 7 个待办全标 done 但 0 产出,
#   账本 open=0 绕过 todo 守门,只剩空交付闸兜成 ok=False 静默失败、无人对质无返工)。
#   全部客观事实:账本存在、全部 closed、done 项 ≥ min_done_items(3,防 1-2 项小账本
#   误伤)、且所有 done 项的 evidence 都解析不出任何实存文件。命中返回证据 payload,
#   由 uncontracted 早退分支决定"一次性返工"(R9-safe:对的是模型自己声明的 done,
#   幂等一次、给双出口,不是外部配额)。
# 函数用途: 回答"账本是不是全标了 done 却拿不出一个实存证据文件"。
def task_progress_all_done_without_artifact_evidence(
    closeout: object,
    *,
    workspace_root: Path | None = None,
    min_done_items: int = 3,
) -> dict[str, Any] | None:
    root = _progress_root(closeout)
    run_id = _run_id(closeout)
    if not root or not run_id:
        return None
    path = progress_path(root, run_id)
    if not path.exists():
        return None
    progress = read_task_progress(root, run_id)
    if _open_items(progress):
        return None
    items = [item for item in progress.get("items", []) if isinstance(item, dict)] if isinstance(progress.get("items"), list) else []
    done_items = [item for item in items if task_progress_status_is_done(item.get("status"))]
    if len(done_items) < min_done_items:
        return None
    if _any_evidence_file_exists(done_items, workspace_root):
        return None
    return {
        "run_id": run_id,
        "progress_ref": str(path),
        "done_count": len(done_items),
        "done_items": [_compact_item(item) for item in done_items[:12]],
    }


# 函数用途: done 项的 evidence 引用里是否有任何一个解析为实存文件(有=不是假 done)。
def _any_evidence_file_exists(done_items: list[dict[str, Any]], workspace_root: Path | None) -> bool:
    return any(
        _evidence_token_file_exists(token, workspace_root)
        for item in done_items
        for token in _evidence_candidate_paths(item.get("evidence"))
    )


# 函数用途: 从 evidence 字符串提取候选路径。与 _evidence_tokens 不同,这里保留前导
#   "/"(绝对路径要原样查存在性,不做投影 strip),并额外把整串与"path:line"的 path
#   头当候选。
def _evidence_candidate_paths(value: object) -> list[str]:
    candidates: list[str] = []
    for item in _list(value):
        text = str(item or "").strip()
        if not text:
            continue
        candidates.append(text)
        head = text.split(":", 1)[0].strip()
        if head and head != text:
            candidates.append(head)
        for match in _EVIDENCE_TOKEN_RE.finditer(text):
            candidates.append(match.group("token"))
    return list(dict.fromkeys([item for item in candidates if item]))


def _evidence_token_file_exists(token: str, workspace_root: Path | None) -> bool:
    try:
        candidate = Path(token).expanduser()
        if candidate.is_absolute():
            return candidate.is_file()
        if workspace_root is None:
            return False
        return (workspace_root / candidate).is_file()
    except OSError:
        return False


def task_progress_ledger_present(agent: object, params: object) -> bool:
    """本 run 的 task_progress 账本存在、有条目、且【无 open 项】(全部终态)——结构化信号:
    模型自认为这个任务的活干完了。"要不要收口"两道判定(finalization candidate + uncontracted
    零产物早退)的兜底判据:solo 一条龙把成品写到任务交付区外(如经 run_command 相对路径落到
    owner home 根)+账本全 done 时,交付区 0 产物+无子代理原本直接跳过全部收口门——真机§7-2
    实锤:活干完了(千行级+测试全过)却无收口无交付,用户什么都收不到。
    必须限定【无 open 项】:账本还挂 open(如刚派完子代理等调度、盯守中途让出)的 run 走
    原非阻塞出口(保留模型原文+RUN_UNFINISHED,R6a 语义),不得被拉进 closeout 打回;
    纯聊天问答没有账本,照旧零打扰。"""
    shim = SimpleNamespace(agent=agent, params=params)  # 复用 closeout 形状的 root/run_id 解析
    root = _progress_root(shim)
    run_id = _run_id(shim)
    if not root or not run_id:
        return False
    path = progress_path(root, run_id)
    if not path.exists():
        return False
    progress = read_task_progress(root, run_id)
    items = progress.get("items")
    if not (isinstance(items, list) and items):
        return False
    return not _open_items(progress)


def evaluate_task_progress_closeout_gate(closeout: object, report: dict[str, Any] | None = None) -> GateDecision:
    root = _progress_root(closeout)
    run_id = _run_id(closeout)
    if not root or not run_id:
        return _unchecked_progress_decision("progress_scope_missing")
    path = progress_path(root, run_id)
    if not path.exists():
        return _unchecked_progress_decision("progress_file_missing", run_id=run_id)
    # §11.1 派工路 coverage 结构对账:子代理交付后,把有产物证据的父需求项先标 done,
    # 再由本门读账——补 solo 路有、派工路失效的完整性兜底网。只增不减 / 纯结构信号 /
    # 只在有已完成子代理时动(solo 路 own_done_children 空 → 一字不动)/ 永不抛错。
    reconcile_dispatch_coverage(closeout, report, root, run_id)
    progress = read_task_progress(root, run_id)
    summary = task_progress_summary({**progress, "ref": str(path)})
    open_items = _open_items(progress)
    if open_items:
        return _open_progress_decision(open_items, run_id, path, summary)
    evidence_decision = _artifact_evidence_projection_decision(
        ArtifactEvidenceProjectionRequest(report or {}, progress, run_id, path, summary)
    )
    if evidence_decision is not None:
        # coverage 对账 advisory 不能只挂"closed"退出路(真机实锤:走 projection 修补路
        # 时 coverage 清单 4 项全 open 却无人问)——两条"进度已收"退出路都要带上。
        return _with_coverage_advisories(evidence_decision, progress)
    return _closed_progress_decision(run_id, path, summary, progress)


def _with_coverage_advisories(decision: GateDecision, progress: dict[str, Any]) -> GateDecision:
    extra = [
        finding
        for finding in _coverage_incomplete_findings(progress)
        if finding.code not in {existing.code for existing in decision.findings}
    ]
    if not extra:
        return decision
    return dataclasses.replace(decision, findings=(*decision.findings, *extra))


def task_progress_repair_message(report: dict[str, Any]) -> str:
    payload = report.get("task_progress_closeout_gate")
    if not isinstance(payload, dict) or payload.get("allowed") is True:
        return ""
    message = str(payload.get("model_message") or "").strip()
    if message:
        return message
    return _first_finding_message(payload.get("findings"))


def _first_finding_message(findings: object) -> str:
    if not isinstance(findings, list):
        return ""
    return next(
        (
            text
            for finding in findings
            if isinstance(finding, dict)
            and (text := str(finding.get("message") or "").strip())
        ),
        "",
    )


def _unchecked_progress_decision(reason: str, run_id: str = "") -> GateDecision:
    evidence: dict[str, object] = {"checked": False, "reason": reason}
    if run_id:
        evidence["run_id"] = run_id
    return GateDecision.allow("task_progress_closeout", evidence=evidence)


def _closed_progress_decision(
    run_id: str,
    path: Path,
    summary: dict[str, Any],
    progress: dict[str, Any],
) -> GateDecision:
    advisory_findings = _advisory_findings(progress)
    return GateDecision(
        "task_progress_closeout",
        "ALLOW",
        True,
        tuple(advisory_findings),
        RecoveryAction.CONTINUE.value,
        {
            "checked": True,
            "run_id": run_id,
            "progress_ref": str(path),
            "counts": summary.get("counts", {}),
            "advisory_finding_codes": [finding.code for finding in advisory_findings],
        },
    )


def _open_progress_decision(
    open_items: list[dict[str, Any]],
    run_id: str,
    path: Path,
    summary: dict[str, Any],
) -> GateDecision:
    next_action = str(summary.get("next_action") or "").strip()
    finding = GateFinding(
        "TASK_PROGRESS_OPEN_ITEMS",
        "soft",
        message=_open_items_repair_message(open_items, next_action),
        evidence={
            "run_id": run_id,
            "progress_ref": str(path),
            "open_count": len(open_items),
            "open_items": [_compact_item(item) for item in open_items[:12]],
            "counts": summary.get("counts", {}),
            "summary": summary.get("summary", ""),
            "next_action": next_action,
        },
    )
    return GateDecision.repair(
        "task_progress_closeout",
        (finding,),
        recommended_action=RecoveryAction.CONTINUE.value,
        evidence={
            "checked": True,
            "run_id": run_id,
            "progress_ref": str(path),
            "open_count": len(open_items),
            "counts": summary.get("counts", {}),
            "summary": summary.get("summary", ""),
            "next_action": next_action,
            "required_actions": [
                "continue_open_task_progress_items",
                "read_or_finish_remaining_sources",
                "submit_for_acceptance_after_open_items_are_done_or_skipped",
            ],
        },
    )


def _artifact_evidence_projection_decision(request: ArtifactEvidenceProjectionRequest) -> GateDecision | None:
    item_tokens = _done_item_evidence_tokens(request.progress)
    if len(_unique_projected_tokens(item_tokens)) < 3:
        return None
    artifact_text = _artifact_text(request.report)
    if not artifact_text.strip():
        return None
    missing = _missing_projected_evidence_items(item_tokens, artifact_text)
    if not missing:
        return None
    return _artifact_evidence_projection_repair(request, item_tokens, missing)


def _missing_projected_evidence_items(item_tokens: list[tuple[str, list[str]]], artifact_text: str) -> list[dict[str, Any]]:
    return [
        {"id": item_id, "evidence_tokens": tokens[:8]}
        for item_id, tokens in item_tokens
        if not _any_token_present(artifact_text, tokens)
    ]


def _artifact_evidence_projection_repair(
    request: ArtifactEvidenceProjectionRequest,
    item_tokens: list[tuple[str, list[str]]],
    missing: list[dict[str, Any]],
) -> GateDecision:
    finding = GateFinding(
        "TASK_PROGRESS_EVIDENCE_NOT_IN_ARTIFACT",
        "medium",
        message=(
            f"进度账本里有 {len(missing)} 个 done 项的证据没有出现在最终交付物中；"
            "请把对应文件、模块或来源引用写进最终报告后再提交。"
        ),
        evidence={
            "run_id": request.run_id,
            "progress_ref": str(request.path),
            "missing_count": len(missing),
            "checked_done_items": len(item_tokens),
            "missing_items": missing[:20],
            "artifact_paths": _artifact_paths(request.report)[:12],
            "counts": request.summary.get("counts", {}),
        },
    )
    return GateDecision.repair(
        "task_progress_closeout",
        (finding,),
        recommended_action=RecoveryAction.CONTINUE.value,
        evidence={
            "checked": True,
            "run_id": request.run_id,
            "progress_ref": str(request.path),
            "missing_evidence_projection_count": len(missing),
            "checked_done_items": len(item_tokens),
            "required_actions": [
                "copy_task_progress_evidence_refs_into_final_artifact",
                "rewrite_or_append_final_artifact_with_source_file_refs",
                "submit_for_acceptance_after_final_artifact_mentions_evidence",
            ],
        },
    )


def _progress_root(closeout: object) -> Path | None:
    agent = getattr(closeout, "agent", None)
    home_paths = getattr(agent, "home_paths", None)
    owner_home = getattr(home_paths, "owner_home_dir", None)
    if owner_home:
        return Path(owner_home).expanduser().resolve(strict=False)
    root = getattr(agent, "root", None)
    return Path(root).expanduser().resolve(strict=False) if root else None


def _run_id(closeout: object) -> str:
    """账本键解析(与 task_progress 工具/派工 seed 同一套语义,三处必须同本):
    唯一的特殊分支=【后台唤醒轮】(params.source=="background_main_agent"):其 run_id 是
    新的(bg-main-*),但 task_id 仍是主任务——账本必须按【任务】延续,否则派工 seed 立的账
    在唤醒轮里读写不到、模型只能另立新账,收口门读到的是那本新账(真机§7-3 实锤:主账
    6 项全 open 却 ok=True 收口,P4(a)"建完不标 done"的机制根因=账本跨唤醒轮分裂,非模型
    纪律)。其余场景一律 params.run_id 原状:子代理 closeout 的 run_id=自己(其 task_id=
    root 主任务,绝不能误切,且 finalize 时 runner thread-local 已 restore、不可用作判据);
    主 run/cli 单趟 task_id==run_id 等值。"""
    params = getattr(closeout, "params", None)
    values = [getattr(params, "run_id", ""), getattr(getattr(closeout, "agent", None), "_main_agent_run_id", "")]
    if str(getattr(params, "source", "") or "").strip() == "background_main_agent":
        values.insert(0, getattr(params, "task_id", ""))
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def closeout_ledger_run_id(closeout: object) -> str:
    """公开的账本键解析:与本门 _run_id 同一把尺(task_progress 工具在读账前跑派工对账时,
    用它确认"当前 run 的账本"与要读的账本是同一本,防止对错账)。"""
    return _run_id(closeout)


def _open_items(progress: dict[str, Any]) -> list[dict[str, Any]]:
    items = progress.get("items") if isinstance(progress, dict) else []
    if not isinstance(items, list):
        return []
    return [
        item
        for item in items
        if isinstance(item, dict) and not task_progress_status_is_closed(item.get("status") or "pending")
    ]


def _done_item_evidence_tokens(progress: dict[str, Any]) -> list[tuple[str, list[str]]]:
    rows: list[tuple[str, list[str]]] = []
    for item in progress.get("items", []) if isinstance(progress.get("items"), list) else []:
        if not isinstance(item, dict) or not task_progress_status_is_done(item.get("status")):
            continue
        tokens = _evidence_tokens(item.get("evidence"))
        if tokens:
            rows.append((_item_id(item), tokens))
    return rows


_EVIDENCE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_.@/-])"
    r"(?P<token>[A-Za-z0-9_.@/-]+"
    r"(?:\.(?:py|pyi|ts|tsx|js|jsx|mjs|cjs|rs|go|md|mdx|toml|json|yaml|yml|txt|java|kt|swift|c|cc|cpp|h|hpp|cs|rb|php|scala|sh|sql|html|css|vue|svelte)|/[A-Za-z0-9_.@/-]+))"
    r"(?![A-Za-z0-9_.@/-])"
)


def _evidence_tokens(value: object) -> list[str]:
    tokens: list[str] = []
    for item in _list(value):
        text = str(item or "")
        for match in _EVIDENCE_TOKEN_RE.finditer(text):
            tokens.extend(_token_variants(match.group("token").strip("/")))
    return _dedupe_tokens(tokens)


def _token_variants(token: str) -> list[str]:
    variants = [token]
    if "/" in token:
        variants.extend(part for part in token.split("/") if "." in part)
    return variants


def _dedupe_tokens(tokens: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for token in tokens:
        clean = token.strip()
        if len(clean) < 4 or clean in seen:
            continue
        seen.add(clean)
        deduped.append(clean)
    return deduped


def _unique_projected_tokens(item_tokens: list[tuple[str, list[str]]]) -> list[str]:
    return _dedupe_tokens([token for _item_id, tokens in item_tokens for token in tokens])


def _artifact_text(report: dict[str, Any]) -> str:
    parts: list[str] = []
    for path in _artifact_paths(report):
        text = _artifact_file_text(Path(path))
        if text:
            parts.append(text)
    return "\n".join(parts)


def _artifact_file_text(path: Path) -> str:
    try:
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")[:200_000]
    except OSError:
        return ""


def _artifact_paths(report: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for item in report.get("artifacts", []) if isinstance(report.get("artifacts"), list) else []:
        if not isinstance(item, dict) or item.get("ok") is not True:
            continue
        path = str(item.get("path") or "").strip()
        if path:
            paths.append(path)
    return paths


def _any_token_present(text: str, tokens: list[str]) -> bool:
    return any(token and token in text for token in tokens)


def _advisory_findings(progress: dict[str, Any]) -> list[GateFinding]:
    return [
        *_empty_done_advisory_findings([item for item in progress.get("items", []) if isinstance(item, dict)]),
        *_coverage_incomplete_findings(progress),
    ]


def _empty_done_advisory_findings(items: list[dict[str, Any]]) -> list[GateFinding]:
    empty_done = [
        item
        for item in items
        if task_progress_status_is_done(item.get("status"))
        and not str(item.get("notes") or "").strip()
        and not str(item.get("result") or item.get("outcome") or item.get("conclusion") or "").strip()
        and not _list(item.get("evidence"))
    ]
    if not empty_done:
        return []
    return [
        GateFinding(
            "TASK_PROGRESS_DONE_WITHOUT_EVIDENCE",
            "soft",
            message=(
                f"进度账本里有 {len(empty_done)} 个 done 项没有事实或证据；"
                "建议补上读到的关键值、来源行；这是软提醒，不阻断验收。"
            ),
            evidence={
                "item_ids": [_item_id(item) for item in empty_done[:20]],
                "empty_done_count": len(empty_done),
            },
        )
    ]


# LLM: A3 结构化自检的收口侧闭环(底座提升:治"双用户方差"——同任务一个覆盖 5/5
#   一个只覆盖 1/5 就自认完成)。模型自己在 coverage 里声明了要覆盖 N 个对象,收口时
#   还有 M 个未闭环 → 幂等打回一次(R9-safe 三要素:①对的是模型自我声明的范围,非外部
#   配额;②一次性,二次同形态放行进 advisory,绝不死锁;③双出口——继续覆盖,或确认
#   不需要就改声明标 skipped 写明原因)。零声明零影响(纯问答/未用 coverage 的任务不沾)。
_COVERAGE_INCOMPLETE_MARKER = "[coverage-incomplete-rework]"


# 函数用途: coverage 清单(模型自声明或需求枚举自动派生)没对完账就收口 → 打回一次
#   让它"继续覆盖或改声明";打回载荷附本 run 的动手痕迹计数(A3 扩面:代码/数据类的
#   "该写的写了没/该跑的跑了没"给模型看结构化事实,判断仍归模型)。
def coverage_incomplete_rework(params: object, report: dict[str, Any]) -> bool:
    finding = _coverage_incomplete_gate_finding(report)
    if finding is None:
        return False
    evidence = finding.get("evidence") if isinstance(finding.get("evidence"), dict) else {}
    context = getattr(params, "tool_context", None)
    if not isinstance(context, list) or any(_COVERAGE_INCOMPLETE_MARKER in str(item) for item in context):
        return False
    payload = {
        "targets_incomplete": int(evidence.get("targets_incomplete") or 0),
        "checks_incomplete": int(evidence.get("checks_incomplete") or 0),
        "active_targets": list(evidence.get("active_targets") or [])[:12],
        "action_trace": _action_trace_facts(params),
        "instruction": (
            "coverage 清单(你声明的,或从需求枚举自动登记的)还没对完账(见 active_targets)。"
            "二选一后再提交:①继续覆盖余下对象,逐个把 checks 做完标 done 并附 evidence"
            "(需求项标 done 必须带证据,否则写不进账);②确认某个对象不需要覆盖,就用 "
            "task_progress 把它标 skipped 并写明原因(skipped 不需要证据,也算闭环)。"
            "注意:自动登记的清单是按需求原文的枚举记号字面拆出来的,可能混入不是功能/"
            "交付物的碎片(如约束、指令片段)——看 active_targets 的 title 逐项判断,"
            "这类项一律标 skipped 写明原因,不算偷工;别把它当功能做,也别标 done(它没有产物证据)。"
            "action_trace 是本轮动手痕迹计数——写文件/跑命令为 0 而清单要求产出/计算时,"
            "先真动手再对账。改声明合法;但别在清单没对账的状态下收尾。"
        ),
    }
    context.append(_COVERAGE_INCOMPLETE_MARKER + "\n" + json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return True


# 产出类/执行类工具名(与 final_exit_contract._PRODUCTIVE_TOOL_NAMES 的口径同源:
# 前者是"写了东西",这里加"跑了东西"——纯计数,零内容判断)。
_WRITE_TOOL_NAMES = frozenset(
    {"write_file", "apply_patch", "replace_in_file", "file_write_session", "data_to_workbook", "markdown_to_pdf"}
)
_EXEC_TOOL_NAMES = frozenset({"run_command", "controlled_exec"})


def _action_trace_facts(params: object) -> dict[str, int]:
    executed = [str(tool) for tool in (getattr(params, "executed_tools", None) or [])]
    return {
        "writes": sum(1 for tool in executed if tool in _WRITE_TOOL_NAMES),
        "commands": sum(1 for tool in executed if tool in _EXEC_TOOL_NAMES),
    }


# 函数用途: 从收口报告的 task_progress gate 里取"覆盖未对账"的软 finding(带计数证据)。
def _coverage_incomplete_gate_finding(report: dict[str, Any]) -> dict[str, Any] | None:
    gate = report.get("task_progress_closeout_gate")
    if not isinstance(gate, dict):
        return None
    for finding in gate.get("findings") or []:
        if not isinstance(finding, dict) or str(finding.get("code") or "") != "TASK_PROGRESS_COVERAGE_INCOMPLETE":
            continue
        evidence = finding.get("evidence") if isinstance(finding.get("evidence"), dict) else {}
        if int(evidence.get("targets_incomplete") or 0) > 0 or int(evidence.get("checks_incomplete") or 0) > 0:
            return finding
    return None


def _coverage_incomplete_findings(progress: dict[str, Any]) -> list[GateFinding]:
    coverage = progress.get("coverage")
    counts = coverage.get("counts") if isinstance(coverage, dict) else {}
    if not isinstance(counts, dict):
        return []
    incomplete_targets = int(counts.get("targets_incomplete") or 0)
    incomplete_checks = int(counts.get("checks_incomplete") or 0)
    if incomplete_targets <= 0 and incomplete_checks <= 0:
        return []
    active = []
    for target in coverage.get("targets", []) if isinstance(coverage, dict) else []:
        if not isinstance(target, dict):
            continue
        target_counts = dict(target.get("checks") or {})
        checks_open = [name for name, status in target_counts.items() if not task_progress_status_is_closed(status)]
        if checks_open or not task_progress_status_is_closed(target.get("status") or "pending"):
            # title 必带:返工载荷只给 id(req-NN)模型无从判断该项是真功能还是字面枚举
            # 混入的非功能碎片,"做 or skipped"双出口就形同虚设(真机瑕疵B实锤)。
            active.append(
                {
                    "id": str(target.get("id") or target.get("title") or ""),
                    "title": str(target.get("title") or ""),
                    "checks_open": checks_open[:8],
                    "next": str(target.get("next") or ""),
                }
            )
    return [
        GateFinding(
            "TASK_PROGRESS_COVERAGE_INCOMPLETE",
            "soft",
            message="覆盖账本还有未完成对象或字段；这是软提醒，建议继续补齐 checks/evidence。",
            evidence={
                "targets_incomplete": incomplete_targets,
                "checks_incomplete": incomplete_checks,
                "active_targets": active[:12],
            },
        )
    ]


def _compact_item(item: dict[str, Any]) -> dict[str, str]:
    return {
        "id": str(item.get("id") or ""),
        "title": str(item.get("title") or item.get("id") or ""),
        "status": str(item.get("status") or ""),
        "next": str(item.get("next") or ""),
    }


def _item_id(item: dict[str, Any]) -> str:
    return str(item.get("id") or item.get("title") or "").strip()


def _list(value: object) -> list:
    return list(value) if isinstance(value, list | tuple) else []


def _open_items_repair_message(open_items: list[dict[str, Any]], next_action: str) -> str:
    first = str(open_items[0].get("id") or open_items[0].get("title") or "").strip() if open_items else ""
    suffix = f"；下一步：{next_action}" if next_action else f"；先继续处理 {first}" if first else ""
    return f"进度账本还有 {len(open_items)} 个未完成项，建议提交前处理或在最终说明里解释{suffix}。"


__all__ = [
    "closeout_ledger_run_id",
    "coverage_incomplete_rework",
    "evaluate_task_progress_closeout_gate",
    "task_progress_all_done_without_artifact_evidence",
    "task_progress_has_open_items",
    "task_progress_ledger_present",
    "task_progress_repair_message",
]
