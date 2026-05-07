# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""manages self-learning draft candidates derived from runner lessons.
给人看的解释：
这个 mixin 只负责 learning draft 的保存、去重、确认和统计。
它不会直接改正式 skill，只维护'候选草稿'这一层安全缓冲。
"""
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .models import LearningCandidate, SubAgentTask
from .utils import _new_id

_LEARNING_STATUSES = {"draft", "accepted", "rejected"}


# LLM: UpdateLearningCandidateParams 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存updatelearningcandidate参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class UpdateLearningCandidateParams:
    """Params bundle for merging one lesson into a candidate."""

    # LLM: candidate update touches evidence, confidence, and variants as one mutation.
    candidate: LearningCandidate
    text: str
    normalized: str
    task: SubAgentTask
    now: float


# LLM: _normalize_learning_text 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 解析并归一化learning文本的输入形态，让下游只处理稳定结构；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _normalize_learning_text(text: str) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "").strip().lower())
    normalized = re.sub(r"[^\w\u4e00-\u9fff ]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized
# LLM: _learning_tokens 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理learning令牌数相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _learning_tokens(text: str) -> set[str]:
    normalized = _normalize_learning_text(text)
    tokens = {item for item in normalized.split(" ") if item}
    compact = normalized.replace(" ", "")
    if compact:
        if len(compact) == 1:
            tokens.add(compact)
        else:
            tokens.update(compact[index : index + 2] for index in range(len(compact) - 1))
    return tokens
# LLM: _learning_similarity 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理learningsimilarity相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _learning_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        return 0.92
    left_tokens = _learning_tokens(left)
    right_tokens = _learning_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = left_tokens & right_tokens
    union = left_tokens | right_tokens
    return len(overlap) / max(1, len(union))
# LLM: _candidate_confidence 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理candidateconfidence相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _candidate_confidence(occurrence_count: int) -> float:
    return round(min(0.95, 0.45 + max(0, occurrence_count - 1) * 0.12), 2)
# LLM: _normalize_candidate 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 解析并归一化candidate的输入形态，让下游只处理稳定结构；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _normalize_candidate(payload: dict[str, object]) -> LearningCandidate:
    status = str(payload.get("status", "draft") or "draft").strip().lower()
    if status not in _LEARNING_STATUSES:
        status = "draft"
    lesson = str(payload.get("lesson", "") or "").strip()
    normalized_key = _normalize_learning_text(str(payload.get("normalized_key", "") or lesson))
    source_runs = [str(item) for item in payload.get("source_runs", []) or [] if str(item).strip()]
    evidence = [item for item in payload.get("evidence", []) or [] if isinstance(item, dict)]
    variants = [str(item) for item in payload.get("variants", []) or [] if str(item).strip()]
    try:
        confidence = float(payload.get("confidence", 0.5) or 0.5)
    except (TypeError, ValueError):
        confidence = 0.5
    try:
        occurrence_count = max(1, int(payload.get("occurrence_count", 1) or 1))
    except (TypeError, ValueError):
        occurrence_count = 1
    try:
        evidence_count = max(1, int(payload.get("evidence_count", len(evidence) or 1) or 1))
    except (TypeError, ValueError):
        evidence_count = max(1, len(evidence) or 1)
    try:
        created_at = float(payload.get("created_at", 0.0) or 0.0)
    except (TypeError, ValueError):
        created_at = 0.0
    try:
        updated_at = float(payload.get("updated_at", created_at) or created_at)
    except (TypeError, ValueError):
        updated_at = created_at
    return LearningCandidate(
        id=str(payload.get("id", "") or _new_id("learn")),
        lesson=lesson,
        normalized_key=normalized_key,
        status=status,
        confidence=confidence,
        occurrence_count=occurrence_count,
        evidence_count=evidence_count,
        source_runs=source_runs,
        evidence=evidence,
        variants=variants,
        created_at=created_at,
        updated_at=updated_at,
    )
# LLM: SubAgentLearningMixin 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 拆分subagentlearning混入流程片段，复用宿主对象上的状态和服务依赖；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
class SubAgentLearningMixin:
    # LLM: learning_drafts_dir 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理learningdraftsdir相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def learning_drafts_dir(self) -> Path:
        root = getattr(self, "workspace_root", None) or self.workspace
        path = Path(root) / "data" / "learning_drafts"
        path.mkdir(parents=True, exist_ok=True)
        return path
    # LLM: learning_enabled 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理learningenabled相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def learning_enabled(self) -> bool:
        return bool(getattr(self, "enable_self_learning", False))
    # LLM: list_learning_candidates 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 读取或查询learningcandidates需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def list_learning_candidates(self) -> list[LearningCandidate]:
        candidates: list[LearningCandidate] = []
        for path in sorted(self.learning_drafts_dir().glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            candidates.append(_normalize_candidate(payload))
        return sorted(
            candidates,
            key=lambda item: (
                {"draft": 0, "accepted": 1, "rejected": 2}.get(item.status, 9),
                -item.confidence,
                -item.updated_at,
            ),
        )
    # LLM: load_learning_candidate 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 读取或查询learningcandidate需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def load_learning_candidate(self, candidate_id: str) -> LearningCandidate:
        path = self.learning_drafts_dir() / f"{candidate_id}.json"
        if not path.exists():
            raise FileNotFoundError(candidate_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise FileNotFoundError(candidate_id)
        return _normalize_candidate(payload)
    # LLM: save_learning_candidate 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 写入learningcandidate的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
    def save_learning_candidate(self, candidate: LearningCandidate) -> LearningCandidate:
        path = self.learning_drafts_dir() / f"{candidate.id}.json"
        candidate.evidence_count = len(candidate.evidence)
        candidate.source_runs = list(dict.fromkeys(candidate.source_runs))
        candidate.variants = list(dict.fromkeys(item for item in candidate.variants if item.strip()))
        path.write_text(
            json.dumps(asdict(candidate), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return candidate
    # LLM: _find_best_candidate 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 读取或查询bestcandidate需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def _find_best_candidate(self, normalized: str, active_candidates: list[LearningCandidate]) -> tuple[LearningCandidate | None, float]:
        best: LearningCandidate | None = None
        best_score = 0.0
        for candidate in active_candidates:
            score = _learning_similarity(normalized, candidate.normalized_key)
            if score > best_score:
                best = candidate
                best_score = score
        return best, best_score
    # LLM: _update_candidate 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 更新candidate对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
    def _update_candidate(self, params: UpdateLearningCandidateParams) -> LearningCandidate:
        candidate = params.candidate
        evidence_item = {
            "run_id": params.task.id,
            "output_json": params.task.output_json,
            "task_dir": params.task.task_dir,
            "recorded_at": params.now,
        }
        candidate.occurrence_count += 1
        candidate.confidence = _candidate_confidence(candidate.occurrence_count)
        candidate.updated_at = params.now
        candidate.source_runs.append(params.task.id)
        if evidence_item not in candidate.evidence:
            candidate.evidence.append(evidence_item)
        if params.text not in candidate.variants:
            candidate.variants.append(params.text)
        if len(params.text) > len(candidate.lesson):
            candidate.lesson = params.text
            candidate.normalized_key = params.normalized
        return self.save_learning_candidate(candidate)
    # LLM: _create_candidate 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 构建candidate所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def _create_candidate(self, text: str, normalized: str, task: SubAgentTask, now: float) -> tuple[LearningCandidate, dict]:
        evidence_item = {
            "run_id": task.id,
            "output_json": task.output_json,
            "task_dir": task.task_dir,
            "recorded_at": now,
        }
        candidate = LearningCandidate(
            id=_new_id("learn"),
            lesson=text,
            normalized_key=normalized,
            status="draft",
            confidence=_candidate_confidence(1),
            occurrence_count=1,
            evidence_count=1,
            source_runs=[task.id],
            evidence=[evidence_item],
            variants=[text],
            created_at=now,
            updated_at=now,
        )
        return self.save_learning_candidate(candidate), evidence_item
    # LLM: record_learning_candidates 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 写入learningcandidates的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
    def record_learning_candidates(
        self,
        task: SubAgentTask,
        lessons: list[str],
    ) -> list[LearningCandidate]:
        if not self.learning_enabled():
            return []
        now = time.time()
        created_or_updated: list[LearningCandidate] = []
        candidates = self.list_learning_candidates()
        active_candidates = [item for item in candidates if item.status != "rejected"]
        for lesson in lessons:
            text = str(lesson or "").strip()
            if not text:
                continue
            normalized = _normalize_learning_text(text)
            if not normalized:
                continue
            best, best_score = self._find_best_candidate(normalized, active_candidates)
            if best is not None and best_score >= 0.45:
                saved = self._update_candidate(UpdateLearningCandidateParams(best, text, normalized, task, now))
                created_or_updated.append(saved)
                continue
            saved, _ = self._create_candidate(text, normalized, task, now)
            active_candidates.append(saved)
            created_or_updated.append(saved)
        return created_or_updated
    # LLM: set_learning_candidate_status 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 更新learningcandidate状态对应的任务或运行状态，并保留既有字段语义；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def set_learning_candidate_status(self, candidate_id: str, status: str) -> LearningCandidate:
        normalized = str(status or "").strip().lower()
        if normalized not in _LEARNING_STATUSES:
            raise ValueError(f"unsupported learning candidate status: {status}")
        candidate = self.load_learning_candidate(candidate_id)
        candidate.status = normalized
        candidate.updated_at = time.time()
        return self.save_learning_candidate(candidate)
    # LLM: learning_stats 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理learningstats相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def learning_stats(self) -> dict[str, object]:
        candidates = self.list_learning_candidates()
        summary = {"draft": 0, "accepted": 0, "rejected": 0}
        total_evidence = 0
        high_confidence = 0
        for item in candidates:
            summary[item.status] = summary.get(item.status, 0) + 1
            total_evidence += item.evidence_count
            if item.confidence >= 0.75:
                high_confidence += 1
        return {
            "total": len(candidates),
            "draft": summary["draft"],
            "accepted": summary["accepted"],
            "rejected": summary["rejected"],
            "high_confidence": high_confidence,
            "total_evidence": total_evidence,
            "directory": str(self.learning_drafts_dir()),
        }
