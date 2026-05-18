from __future__ import annotations

# LLM: Model-facing skill lifecycle tools expose draft learning without bypassing review.
# 模块用途: 将 skill 草稿、晋级、禁用、回滚和学习草稿创建暴露为正式工具。
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..capability.skills import SkillDraftRequest, SkillLifecycleStore
from .models import BaseTool, ToolExecutionResult, ToolSpec
from .skill_learning import LearnedSkillDraftRequest, SkillDraftLearningService

SCHEMA_VERSION = "skill_lifecycle.v1"


# LLM: SkillLifecycleCommand is the Command bundle for lifecycle tool actions.
# 类用途: 保存 skill_lifecycle 工具一次操作的动作、名称、正文、版本和原因。
@dataclass(frozen=True)
class SkillLifecycleCommand:
    action: str
    name: str = ""
    markdown: str = ""
    version: int = 0
    reason: str = ""


# LLM: SkillLifecycleTool exposes controlled draft, promotion, disable, rollback, and event actions.
# 类用途: 模型可调用的 skill 生命周期工具，所有状态变化都经过 SkillLifecycleStore。
class SkillLifecycleTool(BaseTool):
    """Create drafts, promote, disable, rollback, and inspect skill lifecycle events."""

    # LLM: SkillLifecycleTool.__init__ builds the model-facing ToolSpec for lifecycle commands.
    # 函数用途: 初始化生命周期工具依赖和工具说明。
    def __init__(self, store: SkillLifecycleStore):
        self.store = store
        self.spec = ToolSpec(
            name="skill_lifecycle",
            category="capability",
            description="Manage reviewable skill drafts and active skill versions.",
            use_cases=["Create, promote, disable, rollback, or inspect learned skills"],
            avoid_when=["需要自动总结任务经验时，优先用 skill_draft_from_task 生成草稿"],
            keywords=["skill", "lifecycle", "draft", "promote", "rollback", "disable", "学习"],
            parameters={
                "action": "create_draft/promote/disable/rollback/events",
                "name": "skill 名称",
                "markdown": "create_draft 时的完整 SKILL.md",
                "version": "rollback 时的版本号",
                "reason": "可选操作原因",
                "request": "可选 Command bundle，包含上述字段",
            },
        )

    # LLM: SkillLifecycleTool.execute normalizes params and maps lifecycle errors to tool errors.
    # 函数用途: 执行一次 skill 生命周期命令，返回结构化 JSON 结果。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        command = _lifecycle_command(params)
        try:
            payload = _execute_lifecycle_command(self.store, command)
        except (KeyError, ValueError) as exc:
            return _error(self.spec.name, str(exc))
        return _ok(self.spec.name, payload)


# LLM: SkillDraftFromTaskTool creates reviewable learned-skill drafts from structured feedback.
# 类用途: 模型可调用的自学习草稿工具，只写 draft，不自动 promote。
class SkillDraftFromTaskTool(BaseTool):
    """Turn structured task feedback into a draft SKILL.md without activating it."""

    # LLM: SkillDraftFromTaskTool.__init__ wires the learning service and tool metadata.
    # 函数用途: 初始化自学习草稿工具依赖和工具说明。
    def __init__(self, store: SkillLifecycleStore):
        self.service = SkillDraftLearningService(store)
        self.spec = ToolSpec(
            name="skill_draft_from_task",
            category="capability",
            description="Create a reviewable draft skill from a completed task pattern.",
            use_cases=["After a task succeeds, summarize reusable steps as a draft skill for later review"],
            avoid_when=["需要立即启用 skill 时；本工具只创建草稿，不 promote"],
            keywords=["skill", "learn", "draft", "task", "feedback", "self_learning"],
            parameters={
                "name": "新 skill 名称",
                "task_summary": "一句话描述可复用经验",
                "when_to_use": "什么时候应该使用这个 skill",
                "steps": "步骤列表",
                "tools_required": "需要的工具名列表",
                "tags": "检索标签列表",
                "risk_level": "low/medium/high",
                "reason": "创建原因",
                "request": "可选 Request bundle，包含上述字段",
            },
        )

    # LLM: SkillDraftFromTaskTool.execute validates minimal learning fields before writing a draft.
    # 函数用途: 从结构化任务经验创建 reviewable skill 草稿。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        request = _learned_skill_request(params)
        if not request.name or not request.task_summary:
            return _error(self.spec.name, "name and task_summary are required")
        result = self.service.create_draft(request)
        return _ok(self.spec.name, {"result": _result_payload(result)})


# LLM: _execute_lifecycle_command dispatches one normalized command to the lifecycle store.
# 函数用途: 根据 action 调用 create_draft、promote、disable、rollback 或 events。
def _execute_lifecycle_command(store: SkillLifecycleStore, command: SkillLifecycleCommand) -> dict[str, Any]:
    if command.action == "create_draft":
        if not command.name or not command.markdown:
            raise ValueError("create_draft requires name and markdown")
        result = store.create_draft(SkillDraftRequest(command.name, command.markdown, command.reason))
        return {"result": _result_payload(result)}
    if command.action == "promote":
        return {"result": _result_payload(store.promote(command.name, reason=command.reason))}
    if command.action == "disable":
        return {"result": _result_payload(store.disable(command.name, reason=command.reason))}
    if command.action == "rollback":
        return {"result": _result_payload(store.rollback(command.name, version=command.version, reason=command.reason))}
    if command.action == "events":
        return {"events": [_event_payload(event) for event in store.events() if _event_visible(event, command.name)]}
    raise ValueError(f"unknown skill lifecycle action: {command.action}")


# LLM: _lifecycle_command normalizes flat fields or request bundle into a Command.
# 函数用途: 解析 skill_lifecycle 工具参数。
def _lifecycle_command(params: dict[str, Any]) -> SkillLifecycleCommand:
    raw = _normalized_params(params)
    return SkillLifecycleCommand(
        action=str(raw.get("action") or "").strip(),
        name=str(raw.get("name") or "").strip(),
        markdown=str(raw.get("markdown") or ""),
        version=_int(raw.get("version")),
        reason=str(raw.get("reason") or "").strip(),
    )


# LLM: _learned_skill_request normalizes model feedback into a learned-skill Request bundle.
# 函数用途: 解析 skill_draft_from_task 的结构化学习请求。
def _learned_skill_request(params: dict[str, Any]) -> LearnedSkillDraftRequest:
    raw = _normalized_params(params)
    return LearnedSkillDraftRequest(
        name=str(raw.get("name") or "").strip(),
        task_summary=str(raw.get("task_summary") or "").strip(),
        when_to_use=str(raw.get("when_to_use") or raw.get("task_summary") or "").strip(),
        steps=_string_list(raw.get("steps")),
        tools_required=_string_list(raw.get("tools_required")),
        tags=_string_list(raw.get("tags")),
        risk_level=str(raw.get("risk_level") or "low").strip(),
        reason=str(raw.get("reason") or "").strip(),
    )


# LLM: _normalized_params lets flat params override request-bundled params.
# 函数用途: 合并 request 参数包和顶层字段。
def _normalized_params(params: dict[str, Any]) -> dict[str, Any]:
    request = params.get("request")
    normalized = dict(request) if isinstance(request, dict) else {}
    normalized.update({key: value for key, value in params.items() if key != "request"})
    return normalized


# LLM: _result_payload serializes lifecycle results without exposing store internals.
# 函数用途: 把生命周期结果转成工具 JSON payload。
def _result_payload(result) -> dict[str, Any]:
    return {
        "name": result.name,
        "status": result.status,
        "version": result.version,
        "path": str(Path(result.path)),
    }


# LLM: _event_payload serializes append-only lifecycle audit events.
# 函数用途: 把生命周期事件转成工具 JSON payload。
def _event_payload(event) -> dict[str, Any]:
    return asdict(event)


# LLM: _event_visible filters events by optional skill name.
# 函数用途: 判断事件是否应该返回给当前 events 查询。
def _event_visible(event, name: str) -> bool:
    return not name or event.name == name


# LLM: _string_list accepts only explicit list input for learned-skill list fields.
# 函数用途: 将列表字段规范化成非空字符串列表。
def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


# LLM: _int keeps malformed model version input non-fatal until command validation.
# 函数用途: 将版本字段转成整数，失败时返回 0。
def _int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# LLM: _ok wraps successful skill lifecycle responses in a stable schema envelope.
# 函数用途: 构造成功的 ToolExecutionResult。
def _ok(tool: str, payload: dict[str, Any]) -> ToolExecutionResult:
    full = {"schema_version": SCHEMA_VERSION, **payload}
    return ToolExecutionResult(tool, True, json.dumps(full, ensure_ascii=False, indent=2))


# LLM: _error wraps validation failures in the normal tool error contract.
# 函数用途: 构造失败的 ToolExecutionResult。
def _error(tool: str, message: str) -> ToolExecutionResult:
    payload = {"schema_version": SCHEMA_VERSION, "error": {"message": message}}
    return ToolExecutionResult(tool, False, json.dumps(payload, ensure_ascii=False), error_code="TOOL_INVALID_ARGUMENTS")
