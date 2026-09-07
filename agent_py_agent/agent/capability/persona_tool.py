# LLM: update_persona 工具——把用户的【长期人设/画像/工作约定】写进对应人格文件(SOUL/USER/AGENTS.md),
#   这三件每轮整文件注入系统上下文、真正塑造每次交互。区别于 remember(记"需要时才想起"的具体事实/事件到
#   长期记忆,按相关性召回)。用户表达"以后叫我X/我是做Y的/你说话别太正式"这类长期设定时用本工具,不用 remember。
#   写入前 scan_memory_content 注入扫描(人格文件每轮注入=注入长效面)；首次写入保留 version 0 修改前基线。
#   改动时同步 tests/test_persona_tool.py。
# 模块用途: 向模型暴露按 owner 隔离的人格增删改查、历史和回滚；提交前拒绝可纠正，写入中断仍按未知副作用保护。
from __future__ import annotations

import copy
import json
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..conversation.authority import (
    CONVERSATION_AUDIT_PREPARE_ATTR,
    current_conversation_task_attributes,
)
from ..memory_store.security import scan_memory_content
from ..tooling.models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolAvailability,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)
from ..user_space.owner_quota import OwnerQuotaExceeded, OwnerQuotaUnavailable
from .persona_repository import (
    PersonaBatchMutationRequest,
    PersonaConflictError,
    PersonaEntryNotFoundError,
    PersonaMutationRequest,
    PersonaRepository,
    PersonaRepositoryError,
    PersonaSecurityError,
)

if TYPE_CHECKING:
    from ..core import SimpleAgent

_TARGET_ATTR = {"soul": "owner_soul_md", "user": "owner_user_md", "agents": "owner_agents_md"}
_PERSONA_ACTIONS = ["add", "list", "replace", "remove", "batch", "history", "rollback", "status"]
_PERSONA_DESCRIPTION = (
    "把用户的长期设定写进人格文件(每轮整文件注入)。区别于 remember(只记'需要时才想起'的具体事实/事件)。"
    "**target=user(用户画像/称呼/长期偏好)可直接写**;"
    "同一条用户消息里有多个 USER 事实时，必须把全部变更放进一次 operations 批量调用；"
    "整批全部校验后只写一次，任一项失败则全部不写。"
    "**target=agents(长期工作约定)由当前 owner 的 Agent 自主维护；"
    "target=soul(你自己的性格语气)的任何变更必须通过统一危险动作审批门。"
    "禁止改用 write/edit/patch/shell 绕过。**"
)
_PERSONA_USE_CASES = (
    "用户说怎么称呼他 / 自我介绍身份角色 / 表达长期偏好 → target=user(直接写)",
    "用户明确要求长期调整性格/语气/风格 → target=soul，由统一审批门裁决",
    "长期工作约定/产物习惯或反复验证的方法 → target=agents，可自主结构化更新",
)
_PERSONA_AVOID_WHEN = (
    "用基础文件或 shell 工具改人格文件 → 必须改用 update_persona；只有 soul 需要用户确认",
    "一次性临时语气(如'这次说话活泼点')→ 当场照做即可,别写进 soul",
    "只是'需要时才想起'的具体事实/事件(如'下周三交报告''项目叫X')→ 用 remember 记 memory",
)
_PERSONA_KEYWORDS = (
    "以后叫我",
    "喊我",
    "称呼",
    "我是做",
    "人设",
    "画像",
    "性格",
    "语气",
    "长期偏好",
    "以后都",
    "写进设定",
)
_PERSONA_PARAMETERS = {
    "action": "可选。add(默认)/list/replace/remove/batch/history/rollback/status。replace/remove 必须先 list 取得 entry_id。",
    "target": "必填。user(用户画像/称呼,自主写)/ soul(性格语气,需用户确认)/ agents(长期工作约定,自主写)。",
    "content": "单项 add/replace 必填。要写进的一句话纯描述；不得换行。list/remove 不需要。",
    "entry_id": "replace/remove 必填。只能使用 list 返回的精确 entry_id，不能按自然语言猜删除目标。",
    "source_quote": (
        "可选审计说明。可记录促成本次 USER 画像变更的用户原话；只进入版本记录，不参与授权或文本匹配。"
    ),
    "expected_sha256": "可选。list 返回的文件哈希；并发修改后不匹配则拒绝覆盖。",
    "rollback_version": "rollback 必填。history 返回的精确版本号；0 表示首次修改前基线。",
    "operations": (
        "可选，仅 target=user。同一条消息要写多个画像事实时使用；"
        "每项为 action/content/entry_id，可选 source_quote 仅作审计记录；按顺序原子执行。"
    ),
}
_PERSONA_PARAMETER_SCHEMA = {
    "action": {"type": "string", "enum": _PERSONA_ACTIONS},
    "target": {"type": "string", "enum": ["soul", "user", "agents"]},
    "content": {"type": "string"},
    "entry_id": {"type": "string"},
    "source_quote": {"type": "string"},
    "expected_sha256": {"type": "string"},
    "rollback_version": {"type": "integer", "minimum": 0},
    "operations": {
        "type": "array",
        "minItems": 1,
        "maxItems": 32,
        "items": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "replace", "remove"]},
                "content": {"type": "string"},
                "entry_id": {"type": "string"},
                "source_quote": {"type": "string"},
            },
            "required": ["action"],
        },
    },
}
_PERSONA_EXAMPLES = (
    '{"tool":"update_persona","target":"user","content":"称呼:松果"}',
    '{"tool":"update_persona","action":"batch","target":"user","operations":[{"action":"add","content":"称呼:松果"},{"action":"add","content":"回答偏好:简洁"}]}',
    '{"tool":"update_persona","action":"list","target":"user"}',
    '{"tool":"update_persona","action":"remove","target":"user","entry_id":"persona-..."}',
    '{"tool":"update_persona","action":"rollback","target":"user","rollback_version":0}',
    '{"tool":"update_persona","target":"soul","content":"语气偏活泼"}',
)


@dataclass(frozen=True)
class _PersonaToolRequest:
    """Validated structured parameters for one update_persona action."""

    action: str
    target: str
    content: str
    entry_id: str
    source_quote: str
    expected_sha256: str
    rollback_version: int | None
    operations: tuple[_PersonaToolOperation, ...] = ()


@dataclass(frozen=True)
class _PersonaToolOperation:
    """One structured USER.md change within an atomic tool call."""

    action: str
    content: str
    entry_id: str
    source_quote: str


def build_update_persona_model_spec() -> ToolModelSpec:
    properties = copy.deepcopy(_PERSONA_PARAMETER_SCHEMA)
    for name, description in _PERSONA_PARAMETERS.items():
        properties[name]["description"] = description
    operations = properties["operations"]["items"]
    operations["additionalProperties"] = False
    return ToolModelSpec(
        name="update_persona",
        description=_PERSONA_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": ["target"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="capability",
            use_cases=_PERSONA_USE_CASES,
            avoid_when=_PERSONA_AVOID_WHEN,
            keywords=_PERSONA_KEYWORDS,
            examples=_PERSONA_EXAMPLES,
        ),
    )


class UpdatePersonaTool(BaseTool):
    model_spec = build_update_persona_model_spec()
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy(
            "mutating",
            by_parameter=((
                "action",
                (
                    ("list", "read_only"),
                    ("history", "read_only"),
                    ("status", "read_only"),
                ),
            ),),
            by_parameter_combinations=tuple(
                (
                    (("target", "soul"), ("action", action)),
                    "dangerous",
                )
                for action in ("", "add", "replace", "remove", "batch", "rollback")
            ),
        ),
        idempotency_policy=IdempotencyPolicy("operation"),
        resource_scopes=ResourceScopePolicy(parameter_names=("target", "entry_id"),
            parameter_kinds={"target": "logical", "entry_id": "logical"}),
    )

    # 类用途: 把"更新用户人设/画像/工作约定"暴露成模型工具,落到 owner 的 SOUL/USER/AGENTS.md(每轮注入)。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent

    def availability(self) -> ToolAvailability:
        attributes = current_conversation_task_attributes(self.agent)
        if attributes.get(CONVERSATION_AUDIT_PREPARE_ATTR) is True:
            return ToolAvailability.unavailable(
                "named Audit preparation is task-scoped and cannot mutate owner persona"
            )
        return ToolAvailability.ready()

    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        request = _parse_persona_tool_request(params)
        if isinstance(request, ToolHandlerOutcome):
            return request
        try:
            repository = self._repository()
            repository.path_for(request.target)
        except PersonaRepositoryError as exc:
            return _err(str(exc), "TOOL_UNAVAILABLE")
        read_result = _persona_read_result(repository, request)
        if read_result is not None:
            return read_result
        # 只读分支（list/history/status）不计数；只限写更新。
        if request.target == "user" and _persona_update_rate_limited(repository):
            return _err(
                "人格更新过于频繁（30 秒内最多 3 次）。普通任务不需要反复更新人设，"
                "只在用户明确表达长期设定时更新。",
                "PERSONA_UPDATE_RATE_LIMITED",
                effect_outcome="not_started",
            )
        if request.target == "user":
            operations = request.operations or (
                _PersonaToolOperation(
                    action=request.action,
                    content=request.content,
                    entry_id=request.entry_id,
                    source_quote=request.source_quote,
                ),
            )
            for operation in operations:
                shape_error = _user_persona_shape_error(
                    action=operation.action,
                    content=operation.content,
                )
                if shape_error is not None:
                    return shape_error
        contents = [operation.content for operation in request.operations] or [request.content]
        for content in contents:
            if not content:
                continue
            scan = scan_memory_content(content)  # 人格文件每轮注入,写入前过注入/外泄扫描
            if not scan.safe:
                return _err(
                    scan.reason(),
                    "PERSONA_INJECTION_BLOCKED",
                    hint="人格文件每轮注入,改成纯描述再写",
                )
        return _execute_persona_mutation(self.agent, repository, request)

    def _repository(self) -> PersonaRepository:
        repository = getattr(self.agent, "persona_repository", None)
        if isinstance(repository, PersonaRepository):
            return repository
        return PersonaRepository.from_home_paths(getattr(self.agent, "home_paths", None))

# LLM: Parse and validate the action schema before repository or consent side effects.
# 函数用途: 把 update_persona 参数整理成一个结构化请求，统一返回可读的参数错误。
def _parse_persona_tool_request(
    params: dict[str, object],
) -> _PersonaToolRequest | ToolHandlerOutcome:
    action = str(params.get("action") or "add").strip().lower()
    target = str(params.get("target") or "").strip().lower()
    content = str(params.get("content") or "").strip()
    entry_id = str(params.get("entry_id") or "").strip()
    source_quote = str(params.get("source_quote") or "").strip()
    expected_sha256 = str(params.get("expected_sha256") or "").strip()
    raw_operations = params.get("operations")
    allowed = {"add", "list", "replace", "remove", "batch", "history", "rollback", "status"}
    if target not in _TARGET_ATTR or action not in allowed:
        return _err(
            "target 须为 soul/user/agents，action 须为 add/list/replace/remove/batch/history/rollback/status",
            "TOOL_INVALID_ARGUMENTS",
        )
    operations = _parse_persona_operations(target, params, raw_operations)
    if isinstance(operations, ToolHandlerOutcome):
        return operations
    if operations:
        return _PersonaToolRequest(
            action="batch",
            target=target,
            content="",
            entry_id="",
            source_quote="",
            expected_sha256=expected_sha256,
            rollback_version=None,
            operations=operations,
        )
    if action == "batch":
        return _err("batch 必须提供 operations", "TOOL_INVALID_ARGUMENTS")
    if action in {"add", "replace"} and not content:
        return _err("add/replace 的 content 必填", "TOOL_INVALID_ARGUMENTS")
    if action in {"replace", "remove"} and not entry_id:
        return _err("replace/remove 必须提供 list 返回的 entry_id", "TOOL_INVALID_ARGUMENTS")
    rollback_version = _parse_rollback_version(action, params.get("rollback_version"))
    if isinstance(rollback_version, ToolHandlerOutcome):
        return rollback_version
    return _PersonaToolRequest(
        action=action,
        target=target,
        content=content,
        entry_id=entry_id,
        source_quote=source_quote,
        expected_sha256=expected_sha256,
        rollback_version=rollback_version,
    )


def _parse_persona_operations(
    target: str,
    params: dict[str, object],
    raw_operations: object,
) -> tuple[_PersonaToolOperation, ...] | ToolHandlerOutcome:
    if raw_operations is None:
        return ()
    if target != "user":
        return _err("operations 批量变更仅支持 target=user", "TOOL_INVALID_ARGUMENTS")
    if str(params.get("action") or "").strip().lower() not in {"", "batch"} or any(
        params.get(name) not in (None, "") for name in ("content", "entry_id")
    ):
        return _err(
            "operations 不能与顶层 action/content/entry_id 混用",
            "TOOL_INVALID_ARGUMENTS",
        )
    if not isinstance(raw_operations, list) or not raw_operations:
        return _err("operations 必须是非空列表", "TOOL_INVALID_ARGUMENTS")
    if len(raw_operations) > 32:
        return _err("operations 最多 32 项", "TOOL_INVALID_ARGUMENTS")
    operations: list[_PersonaToolOperation] = []
    for index, raw in enumerate(raw_operations, start=1):
        if not isinstance(raw, dict):
            return _err(f"operations[{index}] 必须是对象", "TOOL_INVALID_ARGUMENTS")
        action = str(raw.get("action") or "").strip().lower()
        content = str(raw.get("content") or "").strip()
        entry_id = str(raw.get("entry_id") or "").strip()
        source_quote = str(raw.get("source_quote") or "").strip()
        if action not in {"add", "replace", "remove"}:
            return _err(
                f"operations[{index}] action 须为 add/replace/remove",
                "TOOL_INVALID_ARGUMENTS",
            )
        if action in {"add", "replace"} and not content:
            return _err(
                f"operations[{index}] add/replace 的 content 必填",
                "TOOL_INVALID_ARGUMENTS",
            )
        if action in {"replace", "remove"} and not entry_id:
            return _err(
                f"operations[{index}] replace/remove 必须提供 entry_id",
                "TOOL_INVALID_ARGUMENTS",
            )
        operations.append(
            _PersonaToolOperation(
                action=action,
                content=content,
                entry_id=entry_id,
                source_quote=source_quote,
            )
        )
    return tuple(operations)


# LLM: Rollback accepts the persisted baseline version zero and rejects all negative/non-integer values.
# 函数用途: 校验 history 返回的精确版本号；0 专门表示首次修改前状态。
def _parse_rollback_version(
    action: str,
    value: object,
) -> int | None | ToolHandlerOutcome:
    if action == "rollback" and value in (None, ""):
        return _err("rollback 必须提供 history 返回的 rollback_version", "TOOL_INVALID_ARGUMENTS")
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return _err("rollback_version 必须是非负整数", "TOOL_INVALID_ARGUMENTS")
    if parsed < 0:
        return _err("rollback_version 必须是非负整数", "TOOL_INVALID_ARGUMENTS")
    return parsed


# LLM: Rate limits are keyed by the canonical owner home. A single Gateway serves many owners;
# process-global timestamps would let one user's valid writes consume another user's allowance.
# Stale buckets are swept at most once per window so the soft guard cannot leak owner identities.
# 函数用途: 按当前用户独立计算 30 秒内最多 3 次 USER 画像更新，用户之间绝不互相占额度。
_persona_update_lock = threading.Lock()
_PERSONA_UPDATE_WINDOW_SECONDS = 30.0
_PERSONA_UPDATE_MAX_WRITES = 3
_persona_update_timestamps: dict[str, list[float]] = {}
_persona_update_last_cleanup = 0.0


def _persona_update_rate_limited(repository: PersonaRepository) -> bool:
    global _persona_update_last_cleanup
    now = time.monotonic()
    owner_key = str(repository.owner_home.resolve(strict=False))
    with _persona_update_lock:
        if now - _persona_update_last_cleanup >= _PERSONA_UPDATE_WINDOW_SECONDS:
            for key, timestamps in tuple(_persona_update_timestamps.items()):
                live = [
                    item
                    for item in timestamps
                    if now - item < _PERSONA_UPDATE_WINDOW_SECONDS
                ]
                if live:
                    _persona_update_timestamps[key] = live
                else:
                    _persona_update_timestamps.pop(key, None)
            _persona_update_last_cleanup = now
        timestamps = _persona_update_timestamps.setdefault(owner_key, [])
        timestamps[:] = [
            item
            for item in timestamps
            if now - item < _PERSONA_UPDATE_WINDOW_SECONDS
        ]
        if len(timestamps) >= _PERSONA_UPDATE_MAX_WRITES:
            return True
        timestamps.append(now)
        return False


# LLM: This test-only hook clears every owner bucket and the sweep clock; production callers must
# never use it to bypass admission.
# 函数用途: 测试之间清空画像限频状态，避免前一个用例占用后一个用例的额度。
def _reset_persona_update_rate_limit() -> None:
    global _persona_update_last_cleanup
    with _persona_update_lock:
        _persona_update_timestamps.clear()
        _persona_update_last_cleanup = 0.0


# LLM: Read-only Persona actions bypass consent and mutation while sharing repository diagnostics.
# 函数用途: 统一 list/history/status 三种只读返回，避免主执行函数重复异常处理。
def _persona_read_result(
    repository: PersonaRepository,
    request: _PersonaToolRequest,
) -> ToolHandlerOutcome | None:
    try:
        if request.action == "list":
            payload = repository.list_entries(request.target)
            payload.update({"ok": True, "action": "list"})
        elif request.action == "history":
            payload = {
                "ok": True,
                "action": "history",
                "target": request.target,
                "versions": repository.history(request.target),
            }
        elif request.action == "status":
            payload = {
                "ok": True,
                "action": "status",
                "target": request.target,
                "diagnostic": repository.load(request.target).diagnostic.to_dict(),
            }
        else:
            return None
    except (OSError, PersonaRepositoryError) as exc:
        return _err(f"读取人格状态失败: {exc}", "TOOL_EXECUTION_FAILED")
    return ToolHandlerOutcome("update_persona", True, json.dumps(payload, ensure_ascii=False))


# LLM: Repository entry/CAS/security exceptions arise before _commit_persona_mutation;
# mark only those as not_started. I/O or generic repository errors may follow a partial
# commit and must retain unknown-effect reconciliation. Keep repository and runtime tests aligned.
# 函数用途: 区分画像尚未写入的校验拒绝与可能部分写入的异常，让模型能改正条目编号而不丢失副作用保护。
def _execute_persona_mutation(
    agent: object,
    repository: PersonaRepository,
    request: _PersonaToolRequest,
) -> ToolHandlerOutcome:
    try:
        source = _persona_source(agent)
        if request.operations:
            payload = repository.mutate_batch(
                PersonaBatchMutationRequest(
                    target=request.target,
                    operations=tuple(
                        PersonaMutationRequest(
                            target=request.target,
                            action=operation.action,
                            content=operation.content,
                            entry_id=operation.entry_id,
                            source_quote=operation.source_quote,
                            confirmed=True,
                            source=source,
                        )
                        for operation in request.operations
                    ),
                    expected_sha256=request.expected_sha256,
                    source=source,
                )
            )
        else:
            payload = repository.mutate(
                PersonaMutationRequest(
                    target=request.target,
                    action=request.action,
                    content=request.content,
                    entry_id=request.entry_id,
                    source_quote=request.source_quote,
                    confirmed=True,
                    expected_sha256=request.expected_sha256,
                    rollback_version=request.rollback_version,
                    source=source,
                )
            )
    except PersonaEntryNotFoundError:
        return _err(
            "entry_id 或版本不存在；请重新 list/history 后再操作", "PERSONA_ENTRY_NOT_FOUND",
            effect_outcome="not_started",
        )
    except PersonaConflictError as exc:
        return _err(
            str(exc), "PERSONA_VERSION_CONFLICT", hint="人格文件已被其他会话修改，请重新 list。",
            effect_outcome="not_started",
        )
    except PersonaSecurityError as exc:
        return _err(str(exc), "PERSONA_INJECTION_BLOCKED", effect_outcome="not_started")
    except OwnerQuotaExceeded as exc:
        return _err(
            str(exc), "OWNER_DISK_QUOTA_EXCEEDED", hint="清理当前 owner 文件或联系管理员调整配额。"
        )
    except OwnerQuotaUnavailable:
        return _err(
            "owner 配额策略当前不可用",
            "OWNER_QUOTA_UNAVAILABLE",
            hint="配额策略恢复前拒绝写入。",
        )
    except (OSError, PersonaRepositoryError, ValueError) as exc:
        return _err(f"写入失败: {exc}", "TOOL_EXECUTION_FAILED")
    payload["hint"] = "人格文件已按结构化 entry_id 更新，下一轮起长期生效。"
    return ToolHandlerOutcome("update_persona", True, json.dumps(payload, ensure_ascii=False))


def _user_persona_shape_error(*, action: str, content: str) -> ToolHandlerOutcome | None:
    """USER 画像保留结构化单事实边界，不从自然语言推断授权。"""
    if action in {"add", "replace"} and len(content.splitlines()) != 1:
        return _err(
            "USER.md 一次只能变更一个单行事实；本次没有写入。",
            "PERSONA_CONTENT_NOT_ATOMIC",
            hint="用一次 operations 批量调用提交多个单行事实。",
        )
    return None


def _persona_source(agent: object) -> str:
    current = getattr(agent, "_current_run_params", None)
    request_id = str(getattr(current, "request_id", "") or "").strip()
    return f"agent_tool:{request_id}" if request_id else "agent_tool"


# LLM: The owning branch must prove not_started; never infer it from error text or taxonomy.
# Keep unspecified write failures uncertain for ToolOperationCoordinator, including partial persistence.
# 函数用途: 返回画像工具错误；调用方明确证明未提交时附上校验阶段，否则保留未知副作用判断。
def _err(
    msg: str,
    code: str,
    hint: str = "",
    *,
    effect_outcome: str = "",
) -> ToolHandlerOutcome:
    body = {"error": msg}
    if hint:
        body["hint"] = hint
    return ToolHandlerOutcome(
        "update_persona",
        False,
        json.dumps(body, ensure_ascii=False),
        error_code=code,
        effect_outcome=effect_outcome,
        failure_stage="validation" if effect_outcome == "not_started" else "",
    )


__all__ = [
    "UpdatePersonaTool",
    "build_update_persona_model_spec",
]
