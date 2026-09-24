from __future__ import annotations

"""把子代理结构化结果投递到 owner 唯一 Memory CandidateService。"""

# LLM: 子代理 workspace 只保留原始 output/finding/evidence 与 lesson 账本；候选当前态只能写 owner candidates.jsonl。
# 模块用途: 将父代理已接收的 lesson（含 record_lesson 账本条目）/finding 转成统一 CandidateObservation，不做审核或晋升。

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone

from ...memory_store.candidate_models import CandidateObservation, MemoryScope
from ...memory_store.candidates import CandidateService
from ..lesson_ledger import LessonLedgerEntry
from ..models import SubAgentTask


# LLM: 本 Service 不读取 learning_drafts 或 memory_gate，也不按相似文本另建候选身份。
# 类用途: 统一记录一个子代理结果批次中的 lesson 和 finding 候选。
class SubAgentMemoryCandidateService:
    # LLM: manager 只提供当前 owner CandidateService 和结构化 run 身份，不能成为第二候选仓库。
    # 函数用途: 初始化子代理结果到 owner 候选主链的窄适配器。
    def __init__(self, manager: object) -> None:
        self.manager = manager

    # LLM: 一批结果先完整构造，再由 CandidateService.observe_many 原子落盘；部分失败不得半写。
    #   lesson_entries 是 lesson 账本读回的已校验条目，先于纯文本 lessons 记录并带账本结构化来源；
    #   与账本渲染正文完全相同的纯文本 lesson 不再另记，避免同一经验两种来源、两个适用场景。
    # 函数用途: 返回本次已创建或幂等合并的 owner 级候选。
    def record_result_candidates(
        self,
        task: SubAgentTask,
        *,
        lessons: list[str],
        findings: list[object],
        lesson_entries: tuple[LessonLedgerEntry, ...] | list[LessonLedgerEntry] = (),
    ) -> list[object]:
        service = getattr(self.manager, "candidate_service", None)
        if not isinstance(service, CandidateService):
            return []
        ledger_texts = {entry.text for entry in lesson_entries}
        observations = [
            *(_ledger_lesson_observation(task, entry) for entry in lesson_entries),
            *(_lesson_observation(task, text) for text in _dedupe_text(lessons) if text not in ledger_texts),
            *(
                observation
                for finding in findings
                if (observation := _finding_observation(task, finding)) is not None
            ),
        ]
        if not observations:
            return []
        return list(service.observe_many(observations))

    # LLM: 失败自省属于 model_inferred lesson 候选；即使模型给出高 confidence 也不得直接写 long-term 或正式 lesson。
    # 函数用途: 将一次结构化失败调参自省写入同一个 owner candidates.jsonl，并保持重放幂等。
    def record_introspection_lesson(
        self,
        task: SubAgentTask,
        *,
        suggested_params: Mapping[str, object],
        failure_type: str,
        attempts: int,
        confidence: float,
    ) -> object | None:
        service = getattr(self.manager, "candidate_service", None)
        if not isinstance(service, CandidateService):
            return None
        normalized_params = dict(suggested_params)
        params_text = json.dumps(
            normalized_params,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        failure = str(failure_type or "failure").strip() or "failure"
        task_id, run_id = _task_identity(task)
        observation = CandidateObservation(
            candidate_type="lesson",
            content=(
                f"子代理出现 {failure} 时，失败自省曾采用参数调整 {params_text}；"
                "再次遇到同类结构化失败时应先审核证据，再决定是否复用。"
            ),
            subject_key=f"subagent.introspection.{_stable_key(failure)}.parameter-adjustment",
            scope=_task_scope(task, task_id),
            origin="model_inferred",
            evidence_refs=(
                {
                    "source_ref": f"subagent-run:{run_id}",
                    "evidence_type": "subagent_failure_introspection",
                    "task_id": task_id,
                    "run_id": run_id,
                    "failure_type": failure,
                    "attempts": max(0, int(attempts or 0)),
                },
            ),
            source_artifact_refs=tuple(_output_refs(task, task_id=task_id, run_id=run_id)),
            source_task_ids=(task_id,),
            source_run_ids=(run_id,),
            observed_at=_observed_at(task),
            confidence=max(0.0, min(1.0, float(confidence or 0.0))),
            proposed_action="add",
            promotion_target="lesson",
            observation_id=(
                f"subagent:{run_id}:failure-introspection:"
                f"{max(0, int(attempts or 0))}:{_digest(params_text)}"
            ),
        )
        return service.observe(observation)


# LLM: lesson 的详细正文只在晋升后进入 LessonRepository；此处只提出可审核候选。
# 函数用途: 构造 subagent_lesson 观察及稳定 task/run/artifact 证据。
def _lesson_observation(task: SubAgentTask, text: str) -> CandidateObservation:
    digest = _digest(text)
    task_id, run_id = _task_identity(task)
    return CandidateObservation(
        candidate_type="lesson",
        content=text,
        subject_key=f"subagent.lesson.{digest}",
        scope=_task_scope(task, task_id),
        origin="subagent_lesson",
        source_artifact_refs=tuple(_output_refs(task, task_id=task_id, run_id=run_id)),
        source_task_ids=(task_id,),
        source_run_ids=(run_id,),
        observed_at=_observed_at(task),
        confidence=0.5,
        proposed_action="add",
        promotion_target="lesson",
        observation_id=f"subagent:{run_id}:lesson:{digest}",
    )


# LLM: 账本经验的正文是固定模板渲染结果，适用场景取 when_to_use（不再用任务 goal）；身份只来自宿主 task，
#   证据引用指向账本条目（ledger#lesson_id）并带 task/run/attempt。observation_id 按 lesson_id 稳定，重放不增计数。
# 函数用途: 将一条 lesson 账本条目转成带结构化来源的 subagent_lesson 候选观察。
def _ledger_lesson_observation(task: SubAgentTask, entry: LessonLedgerEntry) -> CandidateObservation:
    content = entry.text
    task_id, run_id = _task_identity(task)
    return CandidateObservation(
        candidate_type="lesson",
        content=content,
        subject_key=f"subagent.lesson.{_digest(content)}",
        scope=_task_scope(task, task_id, applies_when=entry.fields.when_to_use),
        origin="subagent_lesson",
        evidence_refs=(
            {
                "source_ref": f"{entry.ledger_ref}#{entry.lesson_id}",
                "evidence_type": "subagent_lesson_ledger",
                "ledger_ref": entry.ledger_ref,
                "lesson_id": entry.lesson_id,
                "task_id": task_id,
                "run_id": run_id,
                "attempt_id": entry.attempt_id,
            },
        ),
        source_artifact_refs=tuple(_output_refs(task, task_id=task_id, run_id=run_id)),
        source_task_ids=(task_id,),
        source_run_ids=(run_id,),
        observed_at=_iso_time(entry.created_at) or _observed_at(task),
        confidence=0.5,
        proposed_action="add",
        promotion_target="lesson",
        observation_id=f"subagent:{run_id}:lesson:{entry.lesson_id}",
    )


# LLM: finding 的 claim 是提议，不因为子代理置信度或时间较新就变成正式项目事实。
# 函数用途: 将一条结构化 finding 转成 project-scope 长期事实候选。
def _finding_observation(
    task: SubAgentTask,
    finding: object,
) -> CandidateObservation | None:
    payload = asdict(finding) if is_dataclass(finding) else dict(finding) if isinstance(finding, dict) else {}
    content = str(payload.get("claim") or "").strip()
    if not content:
        return None
    task_id, run_id = _task_identity(task)
    finding_id = str(payload.get("id") or "").strip()
    digest = _digest(finding_id or content)
    evidence = [
        {
            "source_ref": ref,
            "task_id": task_id,
            "run_id": run_id,
        }
        for ref in _dedupe_text(
            [
                *list(payload.get("evidence_refs") or []),
                *list(payload.get("evidence_packet_ids") or []),
                *list(payload.get("counter_evidence_refs") or []),
            ]
        )
    ]
    return CandidateObservation(
        candidate_type="long_term_fact",
        content=content,
        subject_key=f"subagent.finding.{digest}",
        scope=_task_scope(task, task_id),
        origin="subagent_finding",
        evidence_refs=tuple(evidence),
        source_artifact_refs=tuple(_output_refs(task, task_id=task_id, run_id=run_id)),
        source_task_ids=(task_id,),
        source_run_ids=(run_id,),
        observed_at=_iso_time(payload.get("created_at")) or _observed_at(task),
        confidence=max(0.0, min(1.0, _float(payload.get("confidence"), 0.0))),
        proposed_action="add",
        promotion_target="long_term",
        observation_id=f"subagent:{run_id}:finding:{finding_id or digest}",
    )


# LLM: task/root ID 决定 typed scope；applies_when 只作人类说明：默认取任务 goal，账本经验显式传入 when_to_use。
# 新持久化一律 project:<id>（单一权威，task:<id> 仅旧账本召回别名），并经共享
# 写侧合同归一，不允许以 task:<id> 新持久化（避免 project/task 双正式身份）。
# 函数用途: 构造项目范围并保证 scope_key 符合统一稳定键合同。
def _task_scope(task: SubAgentTask, task_id: str, *, applies_when: str | None = None) -> MemoryScope:
    safe_id = re.sub(r"[^A-Za-z0-9_.:/-]+", "-", task_id).strip("-")[:140]
    if not safe_id:
        safe_id = _digest(task_id or str(getattr(task, "goal", "") or "task"))
    condition = str(getattr(task, "goal", "") or "") if applies_when is None else applies_when
    return MemoryScope.for_new_observation(
        {
            "scope_type": "project",
            "scope_key": f"project:{safe_id}",
            "applies_when": condition[:500],
        }
    )


# LLM: artifact ref 只保存可定位路径和 task/run ID，不复制 output.json 正文。
# 函数用途: 构造子代理权威结果文件引用。
def _output_refs(
    task: SubAgentTask,
    *,
    task_id: str,
    run_id: str,
) -> list[dict[str, object]]:
    path = str(getattr(task, "output_json", "") or "").strip()
    if not path:
        return []
    return [{"artifact_ref": path, "task_id": task_id, "run_id": run_id}]


# LLM: task_id 与 run_id 来自持久任务模型，不从文件名或展示标题反推。
# 函数用途: 返回候选来源的根任务和具体运行身份。
def _task_identity(task: SubAgentTask) -> tuple[str, str]:
    run_id = str(getattr(task, "id", "") or "").strip()
    return str(getattr(task, "root_id", "") or run_id).strip(), run_id


# LLM: observation 时间只作经历时间，不参与自动替换或事实胜负。
# 函数用途: 读取 task 更新时间并规范为 UTC ISO。
def _observed_at(task: SubAgentTask) -> str:
    return _iso_time(getattr(task, "updated_at", 0.0)) or datetime.now(timezone.utc).isoformat()


# LLM: 仅接受 epoch 数值；坏时间回退空值，由上层使用 task 时间。
# 函数用途: 将子代理结构化时间转 UTC ISO。
def _iso_time(value: object) -> str:
    try:
        timestamp = float(value or 0.0)
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat() if timestamp > 0 else ""


# LLM: 文本去重只做精确 trim，不做另一套相似度合并；稳定候选 ID 由 CandidateService 决定。
# 函数用途: 保留首个非空文本顺序。
def _dedupe_text(values: object) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    return list(dict.fromkeys(text for value in values if (text := str(value or "").strip())))


# LLM: digest 仅用于 subject/observation 子键，不替代统一 candidate_id 算法。
# 函数用途: 生成短稳定内容标识。
def _digest(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()[:20]


# LLM: subject_key 只接受稳定 ASCII 片段；失败展示文本不能原样进入机器主题键。
# 函数用途: 将失败枚举或状态值规范成有界 subject_key 片段。
def _stable_key(value: object) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip().lower())
    return normalized.strip("-.")[:80] or "failure"


# LLM: 子代理置信度解析失败必须安全降为默认值，不能提升权威。
# 函数用途: 解析有限浮点字段。
def _float(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


__all__ = ["SubAgentMemoryCandidateService"]
