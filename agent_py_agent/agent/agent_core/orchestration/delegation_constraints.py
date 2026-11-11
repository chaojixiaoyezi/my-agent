# LLM: 结构化委托约束只能单调收窄父级既有授权；未知原生强制项如实上报，外部指令运行时不冒充原生强制。
# 模块用途: 定义子代理层级委托约束数据结构、执行范围报告与范围上界单调性校验。

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ...settings.model_provider_schema import ModelProfileError

RECOGNIZED_NATIVE_PROHIBITED_ACTIONS: frozenset[str] = frozenset({
    "git_push",
    "git_reset_hard",
    "spawn_children",
    "network_access",
    "network_fetch",
    "shell_execution",
    "file_delete",
})


# LLM: 委托约束校验失败基类，继承 ModelProfileError 确保可被准备流程捕获并转换为 not_started 回执。
# 类用途: 结构化委托约束异常基类。
class DelegationConstraintError(ModelProfileError):
    def __init__(self, message: str, error_code: str = "DELEGATION_SCOPE_EXCEEDS_PARENT") -> None:
        super().__init__(message)
        self.error_code = error_code


# LLM: 工具授权越界异常，错误码为 TOOL_PERMISSION_DENIED。
# 类用途: 标记委托请求的工具超出父级授权。
class ToolPermissionDeniedError(DelegationConstraintError):
    def __init__(self, message: str) -> None:
        super().__init__(message, error_code="TOOL_PERMISSION_DENIED")


# LLM: 写入或读取范围超出父级授权异常，错误码为 DELEGATION_SCOPE_EXCEEDS_PARENT。
# 类用途: 标记委托请求的路径范围超出父级授权。
class DelegationScopeExceedsParentError(DelegationConstraintError):
    def __init__(self, message: str) -> None:
        super().__init__(message, error_code="DELEGATION_SCOPE_EXCEEDS_PARENT")


def _parse_non_negative_int(val: Any) -> int | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val if val >= 0 else None
    if isinstance(val, str) and val.strip().isdigit():
        parsed = int(val.strip())
        return parsed if parsed >= 0 else None
    return None


# LLM: 结构化约束对象冻结子代理运行时不可逾越的边界，并作为不可变契约存入 attributes 与 delegations 库。
# 类用途: 表示子代理派发时的结构化委托约束，包括累计子代理上限、后代派发策略、精确读写域与禁用动作。
@dataclass(frozen=True)
class StructuredDelegationConstraints:
    schema_version: str = "delegation_constraints.v1"
    actor_runtime: Literal["native", "external_instruction_only"] = "native"
    max_cumulative_children: int | None = None
    allow_descendants: bool = True
    max_descendant_depth: int | None = None
    exact_write_set: tuple[str, ...] = ()
    allowed_read_inputs: tuple[str, ...] = ()
    allowed_output_dirs: tuple[str, ...] = ()
    permitted_tools: tuple[str, ...] = ()
    prohibited_actions: tuple[str, ...] = ()

    # LLM: 序列化为纯 JSON 字典，供持久化与数据库存储。
    # 函数用途: 将结构化委托约束转换为字典格式。
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "actor_runtime": self.actor_runtime,
            "max_cumulative_children": self.max_cumulative_children,
            "allow_descendants": self.allow_descendants,
            "max_descendant_depth": self.max_descendant_depth,
            "exact_write_set": list(self.exact_write_set),
            "allowed_read_inputs": list(self.allowed_read_inputs),
            "allowed_output_dirs": list(self.allowed_output_dirs),
            "permitted_tools": list(self.permitted_tools),
            "prohibited_actions": list(self.prohibited_actions),
        }

    # LLM: 从字典反序列化约束并规范化各路径与动作列表，防止不规则类型污染状态。
    # 函数用途: 从字典解析并返回规范化的结构化委托约束对象。
    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> StructuredDelegationConstraints:
        if not isinstance(data, dict):
            return cls()
        runtime_raw = str(data.get("actor_runtime") or "native").strip()
        runtime: Literal["native", "external_instruction_only"] = (
            "external_instruction_only" if runtime_raw == "external_instruction_only" else "native"
        )
        max_cumulative_children = _parse_non_negative_int(data.get("max_cumulative_children"))
        allow_desc = data.get("allow_descendants")
        allow_descendants = True if allow_desc is None else bool(allow_desc)
        max_descendant_depth = _parse_non_negative_int(data.get("max_descendant_depth"))
        return cls(
            schema_version=str(data.get("schema_version") or "delegation_constraints.v1"),
            actor_runtime=runtime,
            max_cumulative_children=max_cumulative_children,
            allow_descendants=allow_descendants,
            max_descendant_depth=max_descendant_depth,
            exact_write_set=_normalize_tuple(data.get("exact_write_set")),
            allowed_read_inputs=_normalize_tuple(data.get("allowed_read_inputs")),
            allowed_output_dirs=_normalize_tuple(data.get("allowed_output_dirs")),
            permitted_tools=_normalize_tuple(data.get("permitted_tools")),
            prohibited_actions=_normalize_tuple(data.get("prohibited_actions")),
        )


# LLM: 区分真正在原生内核中受保护的约束与仅作为外部 prompt 指令的约束；诚实暴露无法原生效验的字段。
# 类用途: 结构化委托执行范围报告，记录已原生执行、仅指令提示与未知无法执行的字段。
@dataclass(frozen=True)
class DelegationEnforcementReport:
    enforced_fields: tuple[str, ...] = ()
    instruction_only_fields: tuple[str, ...] = ()
    unknown_enforcement_fields: tuple[str, ...] = ()
    upper_bound_preserved: bool = True

    # LLM: 转字典格式供回执或事件上报。
    # 函数用途: 将执行范围报告转换为字典。
    def to_dict(self) -> dict[str, Any]:
        return {
            "enforced_fields": list(self.enforced_fields),
            "instruction_only_fields": list(self.instruction_only_fields),
            "unknown_enforcement_fields": list(self.unknown_enforcement_fields),
            "upper_bound_preserved": self.upper_bound_preserved,
        }


# LLM: 规范化字符串序列为去重并保留顺序的元组。
# 函数用途: 清理并转换输入列表为元组。
def _normalize_tuple(values: object) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple, set)):
        return ()
    seen: set[str] = set()
    result: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return tuple(result)


# LLM: 评估约束在当前运行时的真实生效情况；external_instruction_only 诚实列入 instruction_only_fields。
# 函数用途: 对委托约束进行效验判定，返回详细执行能力报告。
def evaluate_delegation_enforcement(
    constraints: StructuredDelegationConstraints,
    *,
    upper_bound_preserved: bool = True,
) -> DelegationEnforcementReport:
    enforced: list[str] = []
    instruction_only: list[str] = []
    unknown: list[str] = []

    specified_fields: list[str] = []
    if constraints.max_cumulative_children is not None:
        specified_fields.append("max_cumulative_children")
    if not constraints.allow_descendants:
        specified_fields.append("allow_descendants")
    if constraints.max_descendant_depth is not None:
        specified_fields.append("max_descendant_depth")
    if constraints.exact_write_set:
        specified_fields.append("exact_write_set")
    if constraints.allowed_read_inputs:
        specified_fields.append("allowed_read_inputs")
    if constraints.allowed_output_dirs:
        specified_fields.append("allowed_output_dirs")
    if constraints.permitted_tools:
        specified_fields.append("permitted_tools")
    if constraints.prohibited_actions:
        specified_fields.append("prohibited_actions")

    if constraints.actor_runtime == "external_instruction_only":
        instruction_only.extend(specified_fields)
        return DelegationEnforcementReport(
            enforced_fields=(),
            instruction_only_fields=tuple(instruction_only),
            unknown_enforcement_fields=(),
            upper_bound_preserved=upper_bound_preserved,
        )

    for field_name in specified_fields:
        if field_name == "prohibited_actions":
            for action in constraints.prohibited_actions:
                if action in RECOGNIZED_NATIVE_PROHIBITED_ACTIONS:
                    if "prohibited_actions" not in enforced:
                        enforced.append("prohibited_actions")
                else:
                    unknown.append(action)
        else:
            enforced.append(field_name)

    return DelegationEnforcementReport(
        enforced_fields=tuple(enforced),
        instruction_only_fields=(),
        unknown_enforcement_fields=tuple(unknown),
        upper_bound_preserved=upper_bound_preserved,
    )


# LLM: 验证子级委托约束是否完全落在父级既有职权之内；若任何单项企图越权或放宽，立即返回违规详情。
# 函数用途: 静态校验约束相对于父级授权的单调收窄性（上界保持）。
def validate_delegation_constraints_upper_bound(
    constraints: StructuredDelegationConstraints,
    *,
    parent_tools: list[str] | set[str] | None = None,
    parent_write_roots: list[str] | None = None,
    parent_read_scope: Any = None,
    parent_constraints: StructuredDelegationConstraints | None = None,
) -> tuple[bool, str]:
    if parent_tools is not None and constraints.permitted_tools:
        parent_set = set(parent_tools)
        for tool in constraints.permitted_tools:
            if tool not in parent_set:
                return (
                    False,
                    f"TOOL_PERMISSION_DENIED: Permitted tool '{tool}' exceeds parent tool authority.",
                )

    if parent_write_roots is not None:
        parent_roots_resolved = [
            Path(r).expanduser().resolve(strict=False) for r in parent_write_roots if str(r).strip()
        ]
        if constraints.exact_write_set:
            for path_str in constraints.exact_write_set:
                p = Path(path_str).expanduser().resolve(strict=False)
                if not any(_is_subpath(p, root) for root in parent_roots_resolved):
                    return (
                        False,
                        f"DELEGATION_SCOPE_EXCEEDS_PARENT: Exact write path '{path_str}' is outside parent write roots.",
                    )
        if constraints.allowed_output_dirs:
            for out_str in constraints.allowed_output_dirs:
                d = Path(out_str).expanduser().resolve(strict=False)
                if not any(_is_subpath(d, root) for root in parent_roots_resolved):
                    return (
                        False,
                        f"DELEGATION_SCOPE_EXCEEDS_PARENT: Allowed output dir '{out_str}' is outside parent write roots.",
                    )

    if parent_constraints is not None:
        if not parent_constraints.allow_descendants and constraints.allow_descendants:
            return (
                False,
                "DELEGATION_SCOPE_EXCEEDS_PARENT: Parent forbids descendants, child cannot allow descendants.",
            )
        if parent_constraints.max_cumulative_children is not None:
            if constraints.max_cumulative_children is None:
                return (
                    False,
                    "DELEGATION_SCOPE_EXCEEDS_PARENT: Parent has max_cumulative_children, child cannot be unlimited.",
                )
            if constraints.max_cumulative_children > parent_constraints.max_cumulative_children:
                return (
                    False,
                    f"DELEGATION_SCOPE_EXCEEDS_PARENT: Child max_cumulative_children ({constraints.max_cumulative_children}) exceeds parent ({parent_constraints.max_cumulative_children}).",
                )
        if parent_constraints.max_descendant_depth is not None:
            if constraints.max_descendant_depth is None:
                return (
                    False,
                    "DELEGATION_SCOPE_EXCEEDS_PARENT: Parent has max_descendant_depth, child cannot be unlimited.",
                )
            if constraints.max_descendant_depth >= parent_constraints.max_descendant_depth:
                return (
                    False,
                    f"DELEGATION_SCOPE_EXCEEDS_PARENT: Child max_descendant_depth ({constraints.max_descendant_depth}) must be strictly less than parent ({parent_constraints.max_descendant_depth}).",
                )
        if parent_constraints.allowed_read_inputs:
            if not constraints.allowed_read_inputs:
                return (
                    False,
                    "DELEGATION_SCOPE_EXCEEDS_PARENT: Parent restricts allowed_read_inputs, child cannot be unlimited.",
                )
            parent_read_roots = [
                Path(r).expanduser().resolve(strict=False)
                for r in parent_constraints.allowed_read_inputs
                if str(r).strip()
            ]
            for path_str in constraints.allowed_read_inputs:
                p = Path(path_str).expanduser().resolve(strict=False)
                if not any(_is_subpath(p, root) for root in parent_read_roots):
                    return (
                        False,
                        f"DELEGATION_SCOPE_EXCEEDS_PARENT: Allowed read input '{path_str}' is outside parent allowed read inputs.",
                    )
        if parent_constraints.prohibited_actions:
            missing_prohibited = set(parent_constraints.prohibited_actions) - set(constraints.prohibited_actions)
            if missing_prohibited:
                return (
                    False,
                    f"DELEGATION_SCOPE_EXCEEDS_PARENT: Child prohibited_actions must include all parent prohibited actions: missing {sorted(missing_prohibited)}.",
                )

    if parent_read_scope is not None:
        exact_roots = getattr(parent_read_scope, "exact_roots", None) or getattr(
            parent_read_scope, "allowed_read_inputs", None
        )
        if exact_roots:
            if not constraints.allowed_read_inputs:
                return (
                    False,
                    "DELEGATION_SCOPE_EXCEEDS_PARENT: Parent read scope restricts exact roots, child cannot be unlimited.",
                )
            resolved_exact = [
                Path(r).expanduser().resolve(strict=False)
                for r in exact_roots
                if str(r).strip()
            ]
            for path_str in constraints.allowed_read_inputs:
                p = Path(path_str).expanduser().resolve(strict=False)
                if not any(_is_subpath(p, root) for root in resolved_exact):
                    return (
                        False,
                        f"DELEGATION_SCOPE_EXCEEDS_PARENT: Allowed read input '{path_str}' is outside parent read scope exact roots.",
                    )

    return True, ""


# LLM: 判定 target 是否为 base 的自身或其子路径。
# 函数用途: 安全判断子路径归属性。
def _is_subpath(target: Path, base: Path) -> bool:
    try:
        target.relative_to(base)
        return True
    except ValueError:
        return False


# LLM: 从入参或任务属性中安全提取结构化委托约束对象。
# 函数用途: 统一提取并构造 StructuredDelegationConstraints 实例。
def extract_delegation_constraints(
    raw_params: dict[str, object] | None,
    agent: object = None,
) -> StructuredDelegationConstraints | None:
    if isinstance(raw_params, dict):
        direct = raw_params.get("delegation_constraints")
        if isinstance(direct, StructuredDelegationConstraints):
            return direct
        if isinstance(direct, dict):
            return StructuredDelegationConstraints.from_dict(direct)
        has_constraint_keys = any(
            k in raw_params
            for k in (
                "max_cumulative_children",
                "allow_descendants",
                "max_descendant_depth",
                "exact_write_set",
                "allowed_read_inputs",
                "allowed_output_dirs",
                "permitted_tools",
                "prohibited_actions",
                "actor_runtime",
            )
        )
        if has_constraint_keys:
            return StructuredDelegationConstraints.from_dict(raw_params)
        attrs = raw_params.get("attributes")
        if isinstance(attrs, dict) and "delegation_constraints" in attrs:
            val = attrs["delegation_constraints"]
            if isinstance(val, StructuredDelegationConstraints):
                return val
            if isinstance(val, dict):
                return StructuredDelegationConstraints.from_dict(val)

    if agent is not None:
        from ...runtime_context import current_subagent_run_id, current_task_attributes

        current_attrs = current_task_attributes(agent)
        if isinstance(current_attrs, dict) and "delegation_constraints" in current_attrs:
            val = current_attrs["delegation_constraints"]
            if isinstance(val, StructuredDelegationConstraints):
                return val
            if isinstance(val, dict):
                return StructuredDelegationConstraints.from_dict(val)

        parent_run_id = current_subagent_run_id(agent)
        manager = getattr(agent, "subagents", None)
        if parent_run_id and manager:
            try:
                parent_task = manager.load(parent_run_id)
                if parent_task and isinstance(getattr(parent_task, "attributes", None), dict):
                    val = parent_task.attributes.get("delegation_constraints")
                    if isinstance(val, StructuredDelegationConstraints):
                        return val
                    if isinstance(val, dict):
                        return StructuredDelegationConstraints.from_dict(val)
            except Exception:
                pass

    return None


# LLM: 判定具体动作或命令是否受当前约束阻止。
# 函数用途: 检查某个动作或命令是否在禁用列表并给出原因。
def is_action_prohibited(
    action: str,
    constraints: StructuredDelegationConstraints | None,
    *,
    tool_name: str = "",
) -> tuple[bool, str]:
    if constraints is None or not constraints.prohibited_actions:
        return False, ""
    normalized_action = str(action or "").strip().lower()
    normalized_tool = str(tool_name or "").strip().lower()
    prohibited = set(constraints.prohibited_actions)

    if normalized_action in prohibited or normalized_tool in prohibited:
        matched = normalized_action if normalized_action in prohibited else normalized_tool
        return True, f"Action '{matched}' is prohibited by delegation constraints."

    if "git_push" in prohibited and (
        normalized_action == "git_push"
        or "git push" in normalized_action
        or normalized_tool == "git_push"
    ):
        return True, "Action 'git_push' is prohibited by delegation constraints."

    if "git_reset_hard" in prohibited and (
        normalized_action == "git_reset_hard"
        or "git reset --hard" in normalized_action
        or "git reset -hard" in normalized_action
        or normalized_tool == "git_reset_hard"
    ):
        return True, "Action 'git_reset_hard' is prohibited by delegation constraints."

    if "spawn_children" in prohibited and (
        normalized_action in {"create_subagents", "spawn_children"}
        or normalized_tool in {"create_subagents", "spawn_children"}
    ):
        return True, "Action 'spawn_children' is prohibited by delegation constraints."

    if "network_access" in prohibited and (
        normalized_action in {"web_search", "web_fetch", "network_access", "network_fetch"}
        or normalized_tool in {"web_search", "web_fetch", "network_access", "network_fetch"}
        or "curl " in normalized_action
        or normalized_action.startswith("curl")
        or "wget " in normalized_action
        or normalized_action.startswith("wget")
        or "http://" in normalized_action
        or "https://" in normalized_action
    ):
        return True, "Action 'network_access' is prohibited by delegation constraints."

    if "network_fetch" in prohibited and (
        normalized_action in {"web_fetch", "network_fetch"}
        or normalized_tool in {"web_fetch", "network_fetch"}
        or "curl " in normalized_action
        or normalized_action.startswith("curl")
        or "wget " in normalized_action
        or normalized_action.startswith("wget")
        or "http://" in normalized_action
        or "https://" in normalized_action
    ):
        return True, "Action 'network_fetch' is prohibited by delegation constraints."

    if "shell_execution" in prohibited and (
        normalized_tool in {"run_command", "process_session", "terminal_session"}
        or normalized_action in {"run_command", "process_session", "terminal_session", "shell_execution"}
    ):
        return True, "Action 'shell_execution' is prohibited by delegation constraints."

    if "file_delete" in prohibited:
        if normalized_action == "file_delete" or normalized_tool == "file_delete":
            return True, "Action 'file_delete' is prohibited by delegation constraints."
        import re

        has_rm = bool(re.search(r"(?:^|[;&|\s])rm(?:\s|$)", normalized_action))
        has_unlink = bool(re.search(r"(?:^|[;&|\s])unlink(?:\s|$)", normalized_action))
        if (
            has_rm
            or has_unlink
            or "shutil.rmtree" in normalized_action
            or "os.remove" in normalized_action
            or "os.unlink" in normalized_action
            or "*** delete file:" in normalized_action
        ):
            return True, "Action 'file_delete' is prohibited by delegation constraints."

    return False, ""
