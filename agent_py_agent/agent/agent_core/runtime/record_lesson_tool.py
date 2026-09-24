# LLM: record_lesson 是子代理专属的结构化经验入口（与 record_finding 同位于 runtime 工具层，但账本与下游独立）：
#   run/attempt 身份只取当前 runner 上下文，task 身份取该 run 的持久任务记录，参数 Schema 不含任何身份字段；
#   账本路径、字段边界、幂等与每 run 上限全部由 subagents/lesson_ledger.py 裁决。主线程没有 child run 时返回
#   TOOL_UNAVAILABLE，不伪造身份；注册表默认隐藏本工具，只随子代理 allowed_tools 显式下发。所有拒绝都声明
#   effect_outcome=not_started（确定没写），只有追加中途的 OSError 保持未知。改动须同步 test_subagent_lesson_ledger.py。
# 模块用途: 提供 record_lesson 工具，让子代理把一条可复用做法写进本 run 的 lessons.jsonl，供结果收口转成经验候选。
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from ...runtime_context import current_subagent_attempt_id, current_subagent_run_id
from ...subagents.lesson_ledger import (
    APPEND_ALREADY_RECORDED,
    APPEND_BYTES_EXCEEDED,
    APPEND_LIMIT_REACHED,
    APPEND_RECORDED,
    LESSON_FIELD_LIMITS,
    MAX_LESSON_LEDGER_BYTES,
    MAX_LESSONS_PER_RUN,
    LessonAppendResult,
    LessonFieldError,
    LessonIdentity,
    LessonLedgerCorruptError,
    append_lesson_record,
    lesson_ledger_record,
    normalize_lesson_fields,
)
from ...subagents.role_templates import RECORD_LESSON_TOOL
from ...tooling.models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)

# 超限拒绝只借用已登记的控制码（错误分类表不归本模块维护），原因码放在 reported_error_code 保留。
_REFUSALS = {
    APPEND_LIMIT_REACHED: (
        "TOOL_GUARDRAIL_DENIED",
        "LESSON_LIMIT_REACHED",
        f"本 run 已记满 {MAX_LESSONS_PER_RUN} 条经验，本条未记录；不要重试，继续原任务。",
    ),
    APPEND_BYTES_EXCEEDED: (
        "TOOL_INVALID_ARGUMENTS",
        "LESSON_LEDGER_BYTES_EXCEEDED",
        "本 run 经验账本已接近字节上限，本条未记录；可精简成更短的一条再记，或直接继续原任务。",
    ),
}
_FIELD_DESCRIPTIONS = {
    "title": "一句话标题：这条可复用做法是什么。",
    "when_to_use": "什么情况下应该用这条做法（触发条件）。",
    "procedure": "具体怎么做：关键步骤或要点，写成一段。",
    "applies_to": "适用范围：哪类任务、工具、语言或环境。",
}


# LLM: 目标只由宿主事实组成：账本路径来自任务记录字段，身份来自 runner 上下文与任务记录。
# 类用途: 打包一次写入要用的账本路径与来源身份。
@dataclass(frozen=True)
class _LessonTarget:
    path: Path
    identity: LessonIdentity


# LLM: 工具实例只持有 agent 引用；每次调用都重新读 runner 上下文，不缓存任何 run 身份。
# 类用途: record_lesson 工具的注册实现，声明模型 Schema 与运行策略。
class RecordLessonTool(BaseTool):
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("mutating"),
        idempotency_policy=IdempotencyPolicy("operation"),
        resource_scopes=ResourceScopePolicy(mode="declared", static_scopes=("subagent_lessons",)),
    )

    # LLM: 构造不读文件、不绑定 run；Schema 由 build_record_lesson_model_spec 统一生成。
    # 函数用途: 绑定当前 agent 并挂上工具说明。
    def __init__(self, agent: object) -> None:
        self.agent = agent
        self.model_spec = build_record_lesson_model_spec()

    # LLM: 顺序固定：先确认有 child run 与账本路径，再校验字段，最后在账本锁内追加；参数里的任何身份字段都被忽略。
    #   副作用：成功时在本 run 的 lessons.jsonl 追加一行（可能同时创建目录与锁文件）。
    # 函数用途: 记录一条可复用做法，返回已记录/已存在/拒绝的结构化结果。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        target = _lesson_target(self.agent)
        if target is None:
            return _refusal("TOOL_UNAVAILABLE", {"status": "unavailable", "reason": "no_subagent_run_ledger"},
                            "record_lesson 只在子代理 run 内可用；当前上下文没有子代理 run 或经验账本。")
        fields = normalize_lesson_fields(params)
        if isinstance(fields, LessonFieldError):
            return _field_refusal(fields)
        record = lesson_ledger_record(fields, target.identity, created_at=time.time())
        try:
            result = append_lesson_record(target.path, record)
        except LessonLedgerCorruptError as exc:
            return _refusal("TOOL_PERSISTENCE_FAILED", {"status": "ledger_unusable", "ledger_ref": str(target.path)},
                            f"经验账本不可用，本条未记录: {exc}")
        except OSError as exc:
            return ToolHandlerOutcome(
                RECORD_LESSON_TOOL, False, f"经验账本写入失败: {exc}", error_code="TOOL_PERSISTENCE_FAILED"
            )
        return _append_outcome(result, target.path)


# LLM: 没有 runner run 身份、任务记录读不到或记录里没有账本路径时都返回 None，调用方统一报 TOOL_UNAVAILABLE。
# 函数用途: 从当前 runner 上下文解析本次写入的账本路径与来源身份。
def _lesson_target(agent: object) -> _LessonTarget | None:
    run_id = current_subagent_run_id(agent)
    task = _run_task(agent, run_id) if run_id else None
    ledger = str(getattr(task, "agent_run_lessons_jsonl", "") or "").strip()
    if task is None or not ledger:
        return None
    identity = LessonIdentity(
        run_id=run_id,
        attempt_id=current_subagent_attempt_id(agent),
        task_id=str(getattr(task, "root_id", "") or run_id).strip(),
    )
    return _LessonTarget(Path(ledger), identity)


# LLM: 与 record_finding 的 run 账本定位同一口径：只经 subagents manager 读取持久任务，不从参数或路径猜。
# 函数用途: 读取当前 run 的任务记录，读不到时返回 None。
def _run_task(agent: object, run_id: str) -> object | None:
    load = getattr(getattr(agent, "subagents", None), "load", None)
    if not callable(load):
        return None
    try:
        return load(run_id)
    except (FileNotFoundError, TypeError, ValueError):
        return None


# LLM: 已记录/已存在都是成功；两种超限是拒绝，带 reported_error_code 与 not_started。
# 函数用途: 把账本追加结论转成工具结果。
def _append_outcome(result: LessonAppendResult, path: Path) -> ToolHandlerOutcome:
    payload: dict[str, object] = {
        "ok": result.status in {APPEND_RECORDED, APPEND_ALREADY_RECORDED},
        "status": result.status,
        "recorded": result.status == APPEND_RECORDED,
        "lesson_id": result.lesson_id,
        "ledger_ref": str(path),
        "lesson_count": result.lesson_count,
        "lesson_limit": MAX_LESSONS_PER_RUN,
        "ledger_bytes": result.ledger_bytes,
        "ledger_byte_limit": MAX_LESSON_LEDGER_BYTES,
    }
    if payload["ok"]:
        return ToolHandlerOutcome(RECORD_LESSON_TOOL, True, json.dumps(payload, ensure_ascii=False, indent=2))
    code, reported, message = _REFUSALS[result.status]
    payload["next_action"] = "continue_task"
    return _refusal(code, payload, message, reported=reported)


# LLM: 字段错误在任何写入之前发生，所以同样声明 not_started。
# 函数用途: 把字段校验失败转成结构化拒绝。
def _field_refusal(error: LessonFieldError) -> ToolHandlerOutcome:
    payload = {"status": "invalid_arguments", "field": error.field, "reason": error.reason, "max_chars": error.limit}
    return _refusal(error.error_code, payload, f"参数 {error.field} 不合规（{error.reason}，上限 {error.limit} 字）。")


# LLM: 所有拒绝都保证账本未写（not_started），执行层据此按确定失败收口，不进入未知副作用闸。
# 函数用途: 组装一个带结构化载荷的失败工具结果。
def _refusal(code: str, payload: dict[str, object], message: str, *, reported: str = "") -> ToolHandlerOutcome:
    body = {"ok": False, "recorded": False, **payload, "message": message}
    return ToolHandlerOutcome(
        RECORD_LESSON_TOOL,
        False,
        json.dumps(body, ensure_ascii=False, indent=2),
        error_code=code,
        reported_error_code=reported,
        effect_outcome="not_started",
    )


# LLM: Schema 只含四个经验字段、全部必填且有 maxLength，additionalProperties=false，模型无法传入身份；
#   category=collaboration 与 record_finding 一致（主线程本就隐藏，runner 显式 allowed_tools 全量可见）。
# 函数用途: 生成 record_lesson 的模型可见说明与参数 Schema。
def build_record_lesson_model_spec() -> ToolModelSpec:
    properties = {
        name: {"type": "string", "minLength": 1, "maxLength": limit, "description": _FIELD_DESCRIPTIONS[name]}
        for name, limit in LESSON_FIELD_LIMITS
    }
    return ToolModelSpec(
        name=RECORD_LESSON_TOOL,
        description=(
            "把本次工作中形成的一条【以后同类任务可复用的具体做法】记进本 run 的经验账本（lessons.jsonl）。"
            "可选，不影响任务是否完成；只记可复用做法，不记本任务的事实结论。"
            f"同样内容重复调用只记一次，每个 run 最多 {MAX_LESSONS_PER_RUN} 条；宿主随结果交回父级并登记为待审核的经验候选。"
        ),
        input_schema={
            "type": "object",
            "properties": properties,
            "required": [name for name, _limit in LESSON_FIELD_LIMITS],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="collaboration",
            use_cases=(
                "踩坑后找到了稳定可行的做法，以后同类任务可以直接照做",
                "摸索出某类工具、环境或数据的正确用法",
            ),
            avoid_when=(
                "只是本任务的事实结论或产物说明，写在最终回复里",
                "还没验证过的猜测，不要记成经验",
            ),
            keywords=("经验", "做法", "复用", "教训", "lesson", "record_lesson"),
        ),
    )


__all__ = ["RecordLessonTool", "build_record_lesson_model_spec"]
