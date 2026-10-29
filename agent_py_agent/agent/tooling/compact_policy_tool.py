# LLM: 压缩策略工具复用resolve_thread_compact_policy、thread_with_compact_threshold与ManagedUserGovernanceService唯一事实源；
#   status为read_only，修改动作为mutating；子代理/隔离上下文拒绝访问，群组先过resolve_host_actor，非管理员严禁触及全局强制或跨用户策略。
# 模块用途: 提供按需展开的会话与多级压缩阈值查询和治理工具（compact_policy），承接self_map(section="settings")导航。
from __future__ import annotations

import json
from typing import Any

from ..conversation.compact_threshold_policy import (
    resolve_thread_compact_policy,
    thread_with_compact_threshold,
)
from ..conversation.host_actor import HostActorError, resolve_host_actor
from ..plugin_host.errors import HostAPIError
from ..runtime_context import current_subagent_run_id, current_task_attributes
from ..settings.config_layers import compact_idle_parameters
from ..user_space.group_authorization import GroupAuthorizationError
from ..user_space.owner_access import is_complete_local_admin_owner
from .models import (
    BaseTool,
    ConcurrencyPolicy,
    EffectResolverPolicy,
    IdempotencyPolicy,
    OutputPolicy,
    ResourceScopePolicy,
    SandboxPolicy,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)
from .user_config_tool import _decision_thread_scope


# LLM: 子代理与三类隔离scope均不得读取或修改父级/宿主压缩策略；不依赖模型参数判断范围。
# 函数用途: 判断当前运行上下文是否属于受限子代理或隔离范围。
def _restricted_compact_scope(agent: object | None) -> bool:
    if current_subagent_run_id(agent):
        return True
    attrs = current_task_attributes(agent) or {}
    scope = str(attrs.get("context_scope") or getattr(getattr(agent, "_current_run_params", None), "context_scope", "") or "default")
    return scope in {"isolated", "task_local", "control_plane"}


# LLM: 仅从宿主已绑定的runner上下文解析当前会话，校验owner_id与owner_home一致，错归属或缺失记录绝不回退默认线程。
# 函数用途: 加载并校验当前可信会话记录，返回(thread, error_code)。
def _trusted_thread(agent: Any) -> tuple[Any | None, str]:
    thread_id, _source = _decision_thread_scope(agent)
    if not thread_id:
        return None, ""
    home = getattr(agent, "home_paths", None)
    thread, error = agent.conversation_store.threads.load_report(thread_id)
    if error or thread is None or home is None:
        return None, "SELF_MAP_THREAD_UNAVAILABLE"
    if thread.owner_id != home.owner_id or thread.owner_home != str(home.owner_home_dir):
        return None, "SELF_MAP_THREAD_UNAVAILABLE"
    return thread, ""


# LLM: 非管理员隐藏admin_default_percent，仅暴露当前可信owner/会话的生效比例、来源层、强制状态与闲置参数。
# 函数用途: 构造compact_policy只读状态回执字典。
def _status_payload(agent: Any, thread: Any | None, *, admin: bool) -> dict[str, Any]:
    policy = resolve_thread_compact_policy(agent, thread, use_frozen=False)
    threshold, seconds = compact_idle_parameters(getattr(agent, "config", None))
    payload: dict[str, Any] = {
        "trigger_percent": policy["effective_trigger_percent"],
        "source_layer": policy["effective_source"],
        "admin_force_enabled": policy["force_locked"],
        "idle_compact_enabled": bool(threshold and seconds),
        "idle_trigger_percent": threshold,
        "idle_seconds": seconds,
        "thread_override_percent": policy["thread_override_percent"],
        "user_default_percent": policy["user_default_percent"],
        "thread_id": getattr(thread, "thread_id", "") if thread is not None else "",
    }
    if admin:
        payload["admin_default_percent"] = policy["admin_default_percent"]
    return payload


# LLM: 统一构造结构化失败回执；GOVERNANCE_MIRROR_FAILED代表权威已提交但镜像失败，必须标为failed而非not_started，其余写前拒绝保持not_started。
# 函数用途: 返回compact_policy工具的标准错误结果，区分写前拒绝与写后部分失败。
def _failure(code: str, message: str, *, effect_outcome: str | None = None) -> ToolHandlerOutcome:
    resolved_outcome = effect_outcome or ("failed" if code == "GOVERNANCE_MIRROR_FAILED" else "not_started")
    return ToolHandlerOutcome(
        "compact_policy",
        False,
        json.dumps({"error": message, "error_code": code}, ensure_ascii=False),
        error_code=code,
        reported_error_code=code,
        effect_outcome=resolved_outcome,
    )


# LLM: 校验50..95整数百分比参数；拒绝布尔值、越界整数或非整数。
# 函数用途: 解析并验证目标压缩触发百分比。
def _parse_percent(raw: Any) -> int | None:
    if type(raw) is int and 50 <= raw <= 95:
        return raw
    if isinstance(raw, str) and raw.strip().isascii() and raw.strip().isdigit():
        value = int(raw.strip())
        if 50 <= value <= 95:
            return value
    return None


# LLM: 本地受管用户仅在管理员未锁定时可改自身default_trigger_percent，统一委托ManagedUserGovernanceService.set_self_user_compact_policy完成锁、CAS与镜像写口。
# 函数用途: 更新当前本地受管用户自己的默认压缩比例。
def _update_self_user_policy(
    agent: Any,
    thread: Any | None,
    value: int | None,
    expected_revision: Any = None,
) -> ToolHandlerOutcome:
    from ..user_space.managed_user_governance import (
        ManagedUserGovernanceService,
        resolve_managed_local_user,
    )

    if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
        return _failure("TOOL_INVALID_ARGUMENTS", "expected_revision 必须是非负整数。")
    if resolve_thread_compact_policy(agent, thread, use_frozen=False)["force_locked"]:
        return _failure("COMPACT_POLICY_LOCKED_BY_ADMIN", "管理员已开启强制压缩阈值锁定，无法修改个人默认压缩比例。")
    home = getattr(agent, "home_paths", None)
    if home is None or getattr(home, "owner_provider", "") != "local" or getattr(home, "owner_kind", "") != "user":
        return _failure("TOOL_PERMISSION_DENIED", "当前身份不支持设置本地受管用户默认压缩比例。")
    try:
        _uid, identity, _owner_home = resolve_managed_local_user(home.root, home.owner_id)
        service = ManagedUserGovernanceService(home.root, actor_owner=identity)
        updated = service.set_self_user_compact_policy(
            home.owner_id,
            value,
            expected_revision=expected_revision,
        )
    except (HostAPIError, ValueError) as exc:
        code = getattr(exc, "code", "TOOL_INVALID_ARGUMENTS")
        return _failure(str(code), str(exc))
    payload = _status_payload(agent, thread, admin=False)
    payload["revision"] = updated["revision"]
    return ToolHandlerOutcome("compact_policy", True, json.dumps(payload, ensure_ascii=False))


# LLM: 管理员动作只对is_complete_local_admin_owner开放，复用ManagedUserGovernanceService的全局锁、CAS与批量治理；
#   批量修改严禁静默忽略expected_revision，部分提交或镜像失败如实保留committed副作用与精确错误码。
# 函数用途: 执行管理员全局强制开关、全局默认比例或受管用户单改/批量改。
def _execute_admin_action(agent: Any, thread: Any | None, action: str, params: dict[str, Any]) -> ToolHandlerOutcome:
    from ..user_space.managed_user_governance import (
        ManagedUserGovernanceService,
        resolve_managed_local_user,
    )
    from ..user_space.owner_resolver import OwnerIdentity

    home = getattr(agent, "home_paths", None)
    if not is_complete_local_admin_owner(home):
        return _failure("TOOL_PERMISSION_DENIED", "仅本机管理员可执行全局或跨用户压缩策略治理。")
    service = ManagedUserGovernanceService(home.root, actor_owner=OwnerIdentity.local_main())
    expected_revision = params.get("expected_revision")
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
        return _failure("TOOL_INVALID_ARGUMENTS", "expected_revision 必须是非负整数。")
    try:
        if action == "admin_force":
            enabled = params.get("admin_force_enabled")
            if type(enabled) is not bool:
                return _failure("TOOL_INVALID_ARGUMENTS", "admin_force 必须提供布尔值 admin_force_enabled。")
            pct = params.get("trigger_percent")
            parsed_pct = None
            if pct is not None:
                parsed_pct = _parse_percent(pct)
                if parsed_pct is None:
                    return _failure("TOOL_INVALID_ARGUMENTS", "trigger_percent 必须是 50 到 95 之间的整数。")
            applied = service.set_global_compact_force_policy(
                enabled,
                trigger_percent=parsed_pct,
                expected_revision=expected_revision,
            )
            return ToolHandlerOutcome(
                "compact_policy",
                True,
                json.dumps({"global_policy": applied, "status": _status_payload(agent, thread, admin=True)}, ensure_ascii=False),
            )
        if action in {"admin_set_user", "admin_reset_user"}:
            target = str(params.get("target_owner_id") or "").strip()
            if not target:
                return _failure("TOOL_INVALID_ARGUMENTS", "必须提供 target_owner_id。")
            pct = None if action == "admin_reset_user" else _parse_percent(params.get("trigger_percent"))
            if action == "admin_set_user" and pct is None:
                return _failure("TOOL_INVALID_ARGUMENTS", "trigger_percent 必须是 50 到 95 之间的整数。")
            patch: dict[str, Any] = {"default_trigger_percent": pct}
            if type(params.get("force_locked")) is bool:
                patch["force_locked"] = params["force_locked"]
            applied = service.set_user_compact_policy(target, patch, expected_revision=expected_revision)
            return ToolHandlerOutcome("compact_policy", True, json.dumps({"user_policy": applied}, ensure_ascii=False))
        if action == "admin_batch_users":
            reset = bool(params.get("reset"))
            pct = None if reset else _parse_percent(params.get("trigger_percent"))
            if not reset and pct is None:
                return _failure("TOOL_INVALID_ARGUMENTS", "批量设置必须提供 50..95 的 trigger_percent 或设置 reset=true。")
            patch = {"default_trigger_percent": pct}
            if type(params.get("force_locked")) is bool:
                patch["force_locked"] = params["force_locked"]
            all_users = bool(params.get("all_users"))
            user_ids = params.get("user_ids")
            raw_revisions = params.get("expected_revisions")
            if expected_revision is not None and raw_revisions is not None:
                return _failure("TOOL_INVALID_ARGUMENTS", "expected_revision 与 expected_revisions 不能同时提供。")
            revisions_map: dict[str, int] | None = None
            if raw_revisions is not None:
                if not isinstance(raw_revisions, dict) or not raw_revisions:
                    return _failure("TOOL_INVALID_ARGUMENTS", "expected_revisions 必须是非空用户修订号字典。")
                revisions_map = {}
                for target_key, rev_val in raw_revisions.items():
                    if type(target_key) is not str or not target_key.strip():
                        return _failure("TOOL_INVALID_ARGUMENTS", "expected_revisions 的键必须是非空用户标识字符串。")
                    if type(rev_val) is not int or rev_val < 0:
                        return _failure("TOOL_INVALID_ARGUMENTS", "expected_revisions 中的修订号必须是非负整数。")
                    uid, _ident, _ohome = resolve_managed_local_user(home.root, target_key)
                    if uid in revisions_map:
                        return _failure(
                            "TOOL_INVALID_ARGUMENTS",
                            f"expected_revisions 包含指向同一受管用户（{uid}）的重复或别名键。",
                        )
                    revisions_map[uid] = rev_val
            elif expected_revision is not None:
                if not all_users and isinstance(user_ids, list) and len(user_ids) == 1 and isinstance(user_ids[0], str):
                    uid, _ident, _ohome = resolve_managed_local_user(home.root, user_ids[0])
                    revisions_map = {uid: expected_revision}
                else:
                    return _failure(
                        "TOOL_INVALID_ARGUMENTS",
                        "多目标或全量 admin_batch_users 不支持单一 expected_revision，请使用 expected_revisions 映射或 admin_set_user。",
                    )
            applied = service.batch_set_users_compact_policy(
                patch,
                user_ids=user_ids if isinstance(user_ids, list) else None,
                scope="all_managed_users" if all_users else None,
                expected_revisions=revisions_map,
            )
            failed_items = [item for item in applied.get("items", []) if item.get("status") != "applied"]
            if failed_items:
                failed_code = str(failed_items[0].get("error_code") or "TOOL_EXECUTION_ERROR")
                batch_effect = (
                    "failed"
                    if applied.get("partial") or failed_code == "GOVERNANCE_MIRROR_FAILED"
                    else "not_started"
                )
                return ToolHandlerOutcome(
                    "compact_policy",
                    False,
                    json.dumps(applied, ensure_ascii=False),
                    error_code=failed_code,
                    reported_error_code=failed_code,
                    effect_outcome=batch_effect,
                )
            return ToolHandlerOutcome("compact_policy", True, json.dumps(applied, ensure_ascii=False))
    except (HostAPIError, ValueError) as exc:
        code = getattr(exc, "code", "TOOL_INVALID_ARGUMENTS")
        return _failure(str(code), str(exc))
    return _failure("TOOL_INVALID_ARGUMENTS", f"不支持的动作：{action}")


# LLM: 默认收起（default_deferred=True），通过tool_search(query="compact_policy")或names加载；
#   status动作标记为read_only以配合self_map的_item_entry发现导航，修改动作保持mutating。
# 类用途: 查询与配置当前会话、当前用户或管理员全局多级压缩比例策略。
class CompactPolicyTool(BaseTool):
    model_spec = ToolModelSpec(
        name="compact_policy",
        description=(
            "查询或配置会话级、用户级与管理员全局上下文压缩触发比例（50%–95%）及强制锁定开关。"
            "action=status 只读返回当前会话解析后的四层压缩比例（admin_forced > thread > user > admin_default）与闲置自动压缩参数；"
            "action=set_thread / reset_thread 设置或重置当前会话的压缩触发比例（受管理员强制锁约束）；"
            "action=set_user / reset_user 设置或重置当前本地用户的默认压缩比例；"
            "action=admin_force / admin_set_user / admin_reset_user / admin_batch_users 仅供本机管理员切换全局强制开关或单改、批量修改受管用户压缩比例。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "status",
                        "set_thread",
                        "reset_thread",
                        "set_user",
                        "reset_user",
                        "admin_force",
                        "admin_set_user",
                        "admin_reset_user",
                        "admin_batch_users",
                    ],
                    "description": "status 只读查询；set_thread/reset_thread 管理当前会话阈值；set_user/reset_user 管理个人默认阈值；admin_* 仅限管理员治理。",
                },
                "trigger_percent": {
                    "type": "integer",
                    "minimum": 50,
                    "maximum": 95,
                    "description": "目标压缩触发百分比（50–95）。",
                },
                "admin_force_enabled": {
                    "type": "boolean",
                    "description": "action=admin_force 时必填：是否开启管理员全局强制压缩阈值锁定。",
                },
                "target_owner_id": {
                    "type": "string",
                    "description": "action=admin_set_user / admin_reset_user 时的目标受管用户（如 local/alice 或 alice）。",
                },
                "user_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "action=admin_batch_users 时指定目标受管用户 ID 列表。",
                },
                "all_users": {
                    "type": "boolean",
                    "description": "action=admin_batch_users 时是否作用于全部受管用户。",
                },
                "reset": {
                    "type": "boolean",
                    "description": "action=admin_batch_users 时是否清除用户默认比例以恢复跟随管理员默认。",
                },
                "force_locked": {
                    "type": "boolean",
                    "description": "管理员设置单用户或批量用户时可选：是否锁定该用户的压缩比例。",
                },
                "expected_revision": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "单目标更新时可选 CAS 修订号。",
                },
                "expected_revisions": {
                    "type": "object",
                    "additionalProperties": {"type": "integer", "minimum": 0},
                    "description": "action=admin_batch_users 时可选的逐用户 CAS 修订号字典（键为用户 ID，值为非负整数）。",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="system",
            use_cases=(
                "查询当前会话或用户的生效压缩比例、来源层级与闲置自动压缩状态",
                "设置或重置当前会话或个人的上下文压缩触发比例（50%–95%）",
                "管理员开启或关闭强制压缩比例开关，或单改、批量修改受管用户压缩比例",
            ),
            avoid_when=("需要修改与上下文压缩无关的普通运行参数时（请用 user_config）",),
            keywords=("compact_policy", "compact", "压缩策略", "压缩比例", "压缩阈值", "闲置压缩"),
            default_deferred=True,
            deferred_summary="查询或配置多级上下文压缩触发比例与管理员强制开关",
        ),
    )
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy(
            default_effect="mutating",
            by_parameter=(("action", (("status", "read_only"),)),),
        ),
        sandbox_policy=SandboxPolicy("none"),
        concurrency_policy=ConcurrencyPolicy("serial"),
        idempotency_policy=IdempotencyPolicy("operation"),
        resource_scopes=ResourceScopePolicy(
            mode="declared",
            static_scopes=("compact_policy:current",),
        ),
        output_policy=OutputPolicy(trust="runtime"),
        promotes_task=False,
        mutates_workspace=False,
    )

    # LLM: 仅绑定当前owner的Agent实例，不跨owner共享或缓存动态策略。
    # 函数用途: 初始化当前Agent的压缩策略工具实例。
    def __init__(self, agent: object | None = None) -> None:
        self._agent = agent

    # LLM: 先核受限子代理/隔离上下文与可信会话，群组有绑定线程时过resolve_host_actor，再按owner身份分派只读status或写入动作。
    # 函数用途: 执行压缩策略查询、会话级/用户级比例修改或管理员全局治理。
    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        if self._agent is None or _restricted_compact_scope(self._agent):
            return _failure("TOOL_PERMISSION_DENIED", "受限子代理或隔离上下文不能访问会话压缩策略。")
        thread, thread_error = _trusted_thread(self._agent)
        if thread_error:
            return _failure(thread_error, "当前绑定的会话不可用或不属于当前用户。")
        home = getattr(self._agent, "home_paths", None)
        action = str(params.get("action") or "status").strip().lower()
        if getattr(home, "owner_kind", "") == "group" and (thread is not None or action != "status"):
            if thread is None:
                return _failure("CHANNEL_ACTOR_IDENTITY_REQUIRED", "群聊修改会话压缩比例需要绑定当前会话。")
            try:
                resolve_host_actor(self._agent, thread.thread_id)
            except (HostActorError, GroupAuthorizationError) as exc:
                return _failure(getattr(exc, "error_code", "TOOL_PERMISSION_DENIED"), str(exc))
        admin = is_complete_local_admin_owner(home)
        if action == "status":
            return ToolHandlerOutcome(
                "compact_policy",
                True,
                json.dumps(_status_payload(self._agent, thread, admin=admin), ensure_ascii=False),
            )
        if action in {"set_thread", "reset_thread"}:
            if thread is None:
                return _failure("TOOL_INVALID_ARGUMENTS", "当前未绑定活动会话，无法修改会话级压缩比例。")
            pct = None if action == "reset_thread" else _parse_percent(params.get("trigger_percent"))
            if action == "set_thread" and pct is None:
                return _failure("TOOL_INVALID_ARGUMENTS", "trigger_percent 必须是 50 到 95 之间的整数。")
            try:
                updated = self._agent.conversation_store.threads.update_atomic(
                    thread.thread_id,
                    lambda current: thread_with_compact_threshold(self._agent, current, pct),
                )
            except HostAPIError as exc:
                return _failure(exc.code, str(exc))
            return ToolHandlerOutcome(
                "compact_policy",
                True,
                json.dumps(_status_payload(self._agent, updated, admin=admin), ensure_ascii=False),
            )
        if action in {"set_user", "reset_user"}:
            pct = None if action == "reset_user" else _parse_percent(params.get("trigger_percent"))
            if action == "set_user" and pct is None:
                return _failure("TOOL_INVALID_ARGUMENTS", "trigger_percent 必须是 50 到 95 之间的整数。")
            return _update_self_user_policy(self._agent, thread, pct, params.get("expected_revision"))
        if action in {"admin_force", "admin_set_user", "admin_reset_user", "admin_batch_users"}:
            return _execute_admin_action(self._agent, thread, action, params)
        return _failure("TOOL_INVALID_ARGUMENTS", f"不支持的 compact_policy action：{action}")
