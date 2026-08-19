from __future__ import annotations

"""Defines stable tool metadata, retrieval hits, and base execution contracts."""

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..contracts.error_taxonomy import error_contract
from ..retrieval.embedding import EmbeddingProvider, cosine


class ToolFailureStage(str, Enum):
    """Stable host-observed layer for a failed tool call.

    The value is written only by the runtime boundary that owns the decision.
    Tool output text and model prose never participate in stage selection.
    """

    PROTOCOL = "protocol"
    AUTHORIZATION = "authorization"
    VALIDATION = "validation"
    RUNTIME_GATE = "runtime_gate"
    EXECUTION = "execution"
    EFFECT_RECONCILIATION = "effect_reconciliation"
    PERSISTENCE = "persistence"


_TOOL_FAILURE_STAGE_VALUES = frozenset(item.value for item in ToolFailureStage)


# LLM: 可信补参只引用当前 Registry 提供的结构化运行事实，条件也只能是工具字段的精确值匹配。
# 类用途: 声明一个缺失参数可从哪些可信路径依次取得，以及在哪个明确动作变体下允许补入。
@dataclass(frozen=True)
class TrustedParameterBinding:
    source_refs: tuple[str, ...]
    when: tuple[tuple[str, Any], ...] = ()
    authority: str = "fill_missing"

    def __post_init__(self) -> None:
        source_refs = tuple(str(item or "").strip() for item in self.source_refs)
        if not source_refs or any(not item for item in source_refs):
            raise ValueError("trusted parameter binding requires non-empty source_refs")
        if len(set(source_refs)) != len(source_refs):
            raise ValueError("trusted parameter binding source_refs must be unique")
        conditions = tuple((str(name or "").strip(), value) for name, value in self.when)
        if any(not name for name, _value in conditions):
            raise ValueError("trusted parameter binding condition name is required")
        if len({name for name, _value in conditions}) != len(conditions):
            raise ValueError("trusted parameter binding condition names must be unique")
        authority = str(self.authority or "fill_missing").strip().lower()
        if authority not in {"fill_missing", "must_match", "host_authoritative"}:
            raise ValueError(f"invalid trusted parameter authority: {authority}")
        object.__setattr__(self, "source_refs", source_refs)
        object.__setattr__(self, "when", conditions)
        object.__setattr__(self, "authority", authority)


@dataclass(frozen=True)
class ToolModelHints:
    """Soft catalog and retrieval hints; never an authorization input."""

    category: str = "general"
    use_cases: tuple[str, ...] = ()
    avoid_when: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolModelSpec:
    """The single model-visible definition of one tool."""

    name: str
    description: str
    input_schema: dict[str, Any]
    schema_hash: str = ""
    hints: ToolModelHints = field(default_factory=ToolModelHints)

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        description = str(self.description or "").strip()
        if not name:
            raise ValueError("tool model spec name is required")
        if not description:
            raise ValueError(f"tool model spec description is required: {name}")
        if not isinstance(self.input_schema, dict):
            raise ValueError(f"tool input_schema must be an object: {name}")
        from .input_schema import canonicalize_tool_input_schema

        schema = canonicalize_tool_input_schema(self.input_schema)
        digest = tool_schema_hash(schema)
        supplied = str(self.schema_hash or "").strip()
        if supplied and supplied != digest:
            raise ValueError(f"tool schema_hash mismatch: {name}")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "input_schema", schema)
        object.__setattr__(self, "schema_hash", digest)

    def assert_schema_hash(self) -> None:
        if tool_schema_hash(self.input_schema) != self.schema_hash:
            raise ValueError(f"tool input_schema mutated after snapshot: {self.name}")

    @property
    def category(self) -> str:
        return self.hints.category

    @property
    def use_cases(self) -> list[str]:
        return list(self.hints.use_cases)

    @property
    def avoid_when(self) -> list[str]:
        return list(self.hints.avoid_when)

    @property
    def keywords(self) -> list[str]:
        return list(self.hints.keywords)

    @property
    def examples(self) -> list[str]:
        return list(self.hints.examples)

    @property
    def parameter_descriptions(self) -> dict[str, str]:
        """Derived catalog labels; ``input_schema`` remains the only declaration."""

        properties = self.input_schema.get("properties")
        if not isinstance(properties, dict):
            return {}
        return {
            str(name): str(value.get("description") or "")
            for name, value in properties.items()
            if isinstance(value, dict)
        }

    def render_catalog_entry(
        self,
        *,
        include_examples: bool = True,
        max_chars: int = 0,
    ) -> str:
        params = "、".join(self.parameter_descriptions) or "无"
        example = (
            f"\n  示例：{self.hints.examples[0]}"
            if include_examples and self.hints.examples
            else ""
        )
        rendered = (
            f"- {self.name} [{self.category}]：{self.description}\n  关键参数：{params}{example}"
        )
        return _truncate_rendered_tool_entry(
            rendered,
            max_chars=max_chars,
            label="tool_catalog_entry_max_chars",
        )

    def render_recommended_entry(self, *, max_chars: int = 0) -> str:
        params = (
            "\n".join(
                f"  - {name}: {description}"
                for name, description in self.parameter_descriptions.items()
            )
            or "  - 无"
        )
        rendered = f"- {self.name} [{self.category}]：{self.description}\n  参数：\n{params}"
        return _truncate_rendered_tool_entry(
            rendered,
            max_chars=max_chars,
            label="tool_detail_max_chars",
        )

    def render_detail_entry(self, *, max_chars: int = 0) -> str:
        params = (
            "\n".join(
                f"  - {name}: {description}"
                for name, description in self.parameter_descriptions.items()
            )
            or "  - 无"
        )
        examples = "\n".join(f"  - {item}" for item in self.hints.examples) or "  - 无"
        use_cases = "\n".join(f"  - {item}" for item in self.hints.use_cases) or "  - 无"
        avoid_when = "\n".join(f"  - {item}" for item in self.hints.avoid_when) or "  - 无"
        rendered = (
            f"## {self.name}\n"
            f"类别：{self.category}\n"
            f"一句话说明：{self.description}\n"
            f"适合在这些时候用：\n{use_cases}\n"
            f"关键参数：\n{params}\n"
            f"示例：\n{examples}\n"
            f"这些场景别优先选它：\n{avoid_when}"
        )
        return _truncate_rendered_tool_entry(
            rendered,
            max_chars=max_chars,
            label="tool_detail_max_chars",
        )


def tool_schema_hash(schema: dict[str, Any]) -> str:
    payload = json.dumps(
        schema,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EffectResolverPolicy:
    default_effect: str = "read_only"
    by_parameter: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = ()
    strategy: str = "declared"
    command_parameter: str = ""

    def __post_init__(self) -> None:
        effect = str(self.default_effect or "").strip().lower()
        strategy = str(self.strategy or "declared").strip().lower()
        if effect not in {"read_only", "mutating", "dangerous"}:
            raise ValueError(f"invalid default tool effect: {effect}")
        if strategy not in {"declared", "command"}:
            raise ValueError(f"invalid tool effect strategy: {strategy}")
        command_parameter = str(self.command_parameter or "").strip()
        if strategy == "command" and not command_parameter:
            raise ValueError("command effect strategy requires command_parameter")
        if strategy == "declared" and command_parameter:
            raise ValueError("declared effect strategy cannot carry command_parameter")
        if strategy == "command" and self.by_parameter:
            raise ValueError("command effect strategy cannot carry by_parameter mappings")
        normalized_mappings: list[tuple[str, tuple[tuple[str, str], ...]]] = []
        seen_fields: set[str] = set()
        for raw_field_name, variants in self.by_parameter:
            field_name = str(raw_field_name or "").strip()
            if not field_name:
                raise ValueError("effect parameter name is required")
            if field_name in seen_fields:
                raise ValueError(f"duplicate effect parameter mapping: {field_name}")
            seen_fields.add(field_name)
            normalized_variants: list[tuple[str, str]] = []
            seen_values: set[str] = set()
            for raw_value, raw_resolved in variants:
                value = str(raw_value)
                resolved = str(raw_resolved or "").strip().lower()
                if value in seen_values:
                    raise ValueError(f"duplicate effect parameter value: {field_name}={value}")
                seen_values.add(value)
                if resolved not in {
                    "read_only",
                    "mutating",
                    "dangerous",
                }:
                    raise ValueError(f"invalid parameter-resolved effect: {resolved}")
                normalized_variants.append((value, resolved))
            if not normalized_variants:
                raise ValueError(f"effect parameter mapping is empty: {field_name}")
            normalized_mappings.append((field_name, tuple(normalized_variants)))
        object.__setattr__(self, "default_effect", effect)
        object.__setattr__(self, "strategy", strategy)
        object.__setattr__(self, "command_parameter", command_parameter)
        object.__setattr__(self, "by_parameter", tuple(normalized_mappings))


@dataclass(frozen=True)
class ApprovalPolicy:
    mode: str = "dangerous"

    def __post_init__(self) -> None:
        mode = str(self.mode or "").strip().lower()
        if mode not in {"never", "dangerous", "mutating", "always"}:
            raise ValueError(f"invalid tool approval mode: {mode}")
        object.__setattr__(self, "mode", mode)


@dataclass(frozen=True)
class SandboxPolicy:
    mode: str = "inherit"

    def __post_init__(self) -> None:
        mode = str(self.mode or "").strip().lower()
        if mode not in {"inherit", "required", "none"}:
            raise ValueError(f"invalid tool sandbox mode: {mode}")
        object.__setattr__(self, "mode", mode)


@dataclass(frozen=True)
class IdempotencyPolicy:
    scope: str = ""

    def __post_init__(self) -> None:
        scope = str(self.scope or "").strip().lower()
        if scope not in {"", "operation", "business"}:
            raise ValueError(f"invalid tool idempotency scope: {scope}")
        object.__setattr__(self, "scope", scope)


@dataclass(frozen=True)
class TimeoutPolicy:
    seconds: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "seconds", max(0, int(self.seconds or 0)))


@dataclass(frozen=True)
class ConcurrencyPolicy:
    mode: str = "serial"

    def __post_init__(self) -> None:
        if self.mode not in {"serial", "parallel_safe", "barrier"}:
            raise ValueError(f"invalid tool concurrency mode: {self.mode}")


class ResourceScopeResolutionError(RuntimeError):
    """seq 269 #3：工具 effective_resource_scopes hook 在 claim 前解析失败。

    hook 内部正常的「解析失败」必须自己保守锁稳定域（绝不空锁）；能抛出本
    异常说明 hook 实现缺陷。executor 收到后 fail-closed 拒绝执行（
    TOOL_RESOURCE_SCOPE_RESOLUTION_FAILED），绝不静默降级成无保护执行。
    """


@dataclass(frozen=True)
class ResourceScopePolicy:
    mode: str = "from_arguments"
    parameter_names: tuple[str, ...] = ()
    static_scopes: tuple[str, ...] = ()
    # seq 248 #6：参数值类型显式区分 path/logical。path = 真实物理写根，
    # 归一化锁 "workspace:{canonical_path}"；logical = 逻辑 ID（session_id/
    # artifact_ref 等），投影 "logical:{name}:{value}" 文本 scope，绝不 resolve
    # 成 workspace 路径（此前所有字符串都被当路径锁错根）。缺省按 path。
    parameter_kinds: dict[str, str] = field(default_factory=dict)
    # seq 266 #1：资源域别名映射——参数名 → 同一底层资源域（run_id/run_ids/
    # target_id → "agent_run"）。锁的是「同一资源」，不是「参数名+原值」；
    # 投影 "logical:{domain}:{strip(value)}"。域名即参数名时无需映射。
    resource_domains: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        mode = str(self.mode or "").strip().lower()
        if mode not in {"none", "from_arguments", "declared"}:
            raise ValueError(f"invalid resource scope mode: {mode}")
        parameter_names = tuple(
            str(item).strip() for item in self.parameter_names if str(item).strip()
        )
        static_scopes = tuple(str(item).strip() for item in self.static_scopes if str(item).strip())
        kinds = {
            str(name).strip(): str(kind or "").strip().lower()
            for name, kind in (self.parameter_kinds or {}).items()
            if str(name).strip()
        }
        for name, kind in kinds.items():
            if kind not in {"path", "logical"}:
                raise ValueError(
                    f"invalid resource scope parameter kind for {name!r}: {kind!r}"
                )
        domains = {
            str(name).strip(): str(domain or "").strip()
            for name, domain in (self.resource_domains or {}).items()
            if str(name).strip() and str(domain or "").strip()
        }
        for name, domain in domains.items():
            if domain == name:
                raise ValueError(
                    f"resource domain for {name!r} equals parameter name; omit it"
                )
        if len(set(parameter_names)) != len(parameter_names):
            raise ValueError("resource scope parameter names must be unique")
        if set(kinds) - set(parameter_names):
            raise ValueError(
                "resource scope parameter_kinds names must be subset of parameter_names"
            )
        if set(domains) - set(parameter_names):
            raise ValueError(
                "resource scope resource_domains names must be subset of parameter_names"
            )
        if len(set(static_scopes)) != len(static_scopes):
            raise ValueError("declared resource scopes must be unique")
        if mode == "none" and (parameter_names or static_scopes):
            raise ValueError("resource scope mode none cannot carry scopes")
        if mode == "from_arguments" and static_scopes:
            raise ValueError("from_arguments resource scopes cannot carry static_scopes")
        if mode == "declared" and parameter_names:
            raise ValueError("declared resource scopes cannot carry parameter_names")
        if mode == "declared" and not static_scopes:
            raise ValueError("declared resource scope mode requires static_scopes")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "parameter_names", parameter_names)
        object.__setattr__(self, "static_scopes", static_scopes)
        object.__setattr__(self, "parameter_kinds", kinds)
        # seq 269 #2：规范化后的 domains 必须写回——校验用的是规范化值，
        # 运行时投影（workspace_scopes / models 回落路径）也必须是同一份；
        # 漏写回会让带空格的参数名键永远查不到映射、静默退化成参数名域
        # （校验过了但运行时没用上）。
        object.__setattr__(self, "resource_domains", domains)


@dataclass(frozen=True)
class OutputPolicy:
    refs: tuple[str, ...] = ()
    trust: str = "runtime"
    redaction: str = "default"

    def __post_init__(self) -> None:
        trust = str(self.trust or "").strip().lower()
        redaction = str(self.redaction or "").strip().lower()
        if trust not in {"runtime", "external_data"}:
            raise ValueError(f"invalid tool output trust: {trust}")
        if redaction not in {"default", "source_code"}:
            raise ValueError(f"invalid tool output redaction: {redaction}")
        object.__setattr__(self, "trust", trust)
        object.__setattr__(self, "redaction", redaction)


@dataclass(frozen=True)
class AvailabilityPolicy:
    mode: str = "handler_probe"

    def __post_init__(self) -> None:
        mode = str(self.mode or "").strip().lower()
        if mode not in {"handler_probe", "always"}:
            raise ValueError(f"invalid availability mode: {mode}")
        object.__setattr__(self, "mode", mode)


@dataclass(frozen=True)
class ToolInputPolicy:
    internal_parameters: tuple[str, ...] = ()
    safe_parameter_defaults: tuple[tuple[str, Any], ...] = ()
    trusted_parameter_bindings: tuple[tuple[str, TrustedParameterBinding], ...] = ()
    local_file_url_parameters: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolRuntimePolicy:
    effect_resolver: EffectResolverPolicy
    approval_policy: ApprovalPolicy = field(default_factory=ApprovalPolicy)
    sandbox_policy: SandboxPolicy = field(default_factory=SandboxPolicy)
    idempotency_policy: IdempotencyPolicy = field(default_factory=IdempotencyPolicy)
    timeout_policy: TimeoutPolicy = field(default_factory=TimeoutPolicy)
    concurrency_policy: ConcurrencyPolicy = field(default_factory=ConcurrencyPolicy)
    resource_scopes: ResourceScopePolicy = field(default_factory=ResourceScopePolicy)
    output_policy: OutputPolicy = field(default_factory=OutputPolicy)
    availability_policy: AvailabilityPolicy = field(default_factory=AvailabilityPolicy)
    input_policy: ToolInputPolicy = field(default_factory=ToolInputPolicy)
    promotes_task: bool = False
    # 执行锁声明(问题6):此工具是否写当前 conversation workspace 目录。与 effect
    # 正交——wait/派工/审计发布是 mutating(改内部状态)但不写 workspace 文件,
    # 声明 False 即豁免执行锁,不再维护手写工具名名单(新写工具在注册处声明)。
    mutates_workspace: bool = False


def tool_effect_for_runtime_policy(
    policy: ToolRuntimePolicy,
    arguments: object,
) -> str:
    """Resolve an effect from exact structured arguments and manifest policy."""

    values = arguments if isinstance(arguments, dict) else {}
    resolver = policy.effect_resolver
    if resolver.strategy == "command":
        from ..contracts.gates.command_policy import analyze_command

        return analyze_command(values.get(resolver.command_parameter)).resolved_effect
    matched: list[str] = []
    for field_name, variants in resolver.by_parameter:
        if field_name not in values:
            continue
        raw_value = values.get(field_name)
        if isinstance(raw_value, bool):
            value = "true" if raw_value else "false"
        else:
            value = str(raw_value or "").strip()
        mapping = {str(key): str(effect).strip().lower() for key, effect in variants}
        if value in mapping:
            matched.append(mapping[value])
    if not matched:
        return resolver.default_effect
    rank = {"read_only": 0, "mutating": 1, "dangerous": 2}
    return max(matched, key=rank.__getitem__)


def resource_scopes_for_runtime_policy(
    policy: ToolRuntimePolicy,
    arguments: object,
    *,
    additional_scopes: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Project stable resource identities from the canonical runtime policy."""

    values = arguments if isinstance(arguments, dict) else {}
    scope_policy = policy.resource_scopes
    scopes = list(scope_policy.static_scopes)
    if scope_policy.mode == "from_arguments":
        # seq 266 #1：与权威路径同一资源语义——参数名映射到资源域后再投影
        # （run_id/run_ids → agent_run），避免同一 run 的不同参数入口投影出
        # 不同 scope；先 strip 再构造，杜绝首尾空格别名绕过。
        domains = scope_policy.resource_domains or {}
        for name in scope_policy.parameter_names:
            value = values.get(name)
            for item in value if isinstance(value, list) else [value]:
                text = str(item or "").strip()
                if text:
                    domain = domains.get(name) or name
                    scopes.append(f"{domain}:{text}")
    scopes.extend(str(item).strip() for item in additional_scopes if str(item).strip())
    return tuple(dict.fromkeys(scopes))


@dataclass
class ToolHandlerOutcome:
    tool: str
    ok: bool
    output: str
    call_id: str = ""
    result_envelope: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""
    # 原始工具/提供方报码必须保留；error_code 仍是控制流使用的归一化分类。
    reported_error_code: str = ""
    error_category: str = ""
    retryable: bool = False
    recommended_action: str = ""
    recovery_hint: str = ""
    # 只有工具实现或统一执行层能写这个机器事实；模型正文不得参与判断。
    # not_started=已证明未触发副作用，unknown=可能已触发，空值=沿通用错误合同处理。
    effect_outcome: str = ""
    effect_source_ref: str = ""
    # 会话运行时 host fact: whether this call reached the registered tool handler.
    # A replay/rejection can therefore succeed or fail with handler_executed=False.
    handler_executed: bool = False
    # The runtime layer that produced a failure. Empty on successful results.
    failure_stage: str = ""
    # End-to-end registry dispatch duration for this attempt, including pre-handler gates.
    duration_ms: int = 0

    def __post_init__(self) -> None:
        self.handler_executed = bool(self.handler_executed)
        self.failure_stage = _normalized_tool_failure_stage(self.failure_stage)
        self.duration_ms = _nonnegative_duration_ms(self.duration_ms)
        self.effect_outcome = str(self.effect_outcome or "").strip().lower()
        # failed=进程完整退出自报失败(确定性, 2026-08-15 长代码真机: unittest
        # 校验命令失败被归 unknown 导致任务死; 退出码+输出是结构化确定事实)。
        if self.effect_outcome not in {"", "not_started", "failed", "unknown"}:
            raise ValueError(f"invalid tool effect outcome: {self.effect_outcome}")
        if self.ok and self.effect_outcome:
            raise ValueError("successful tool result cannot report an incomplete effect outcome")
        self.effect_source_ref = str(self.effect_source_ref or "").strip()
        if self.ok:
            if self.failure_stage:
                raise ValueError("successful tool result cannot report a failure stage")
            self.error_code = ""
            self.reported_error_code = ""
            self.error_category = ""
            self.retryable = False
            self.recommended_action = ""
            self.recovery_hint = ""
            return
        # ``output`` is untrusted business/provider data.  It may contain JSON
        # that looks like host lifecycle metadata, but only explicit fields on
        # this handler-internal outcome may influence canonical control flow.
        reported_code = self.reported_error_code or self.error_code
        control_code = self.error_code or reported_code
        self.reported_error_code = str(reported_code or "UNKNOWN_ERROR").strip().upper()
        contract = error_contract(control_code) if control_code else error_contract("UNKNOWN_ERROR")
        self.error_code = contract.code
        self.error_category = contract.category
        self.retryable = contract.retryable
        self.recommended_action = contract.recommended_action
        self.recovery_hint = contract.recovery_hint

    # LLM: prompt 结果必须同时展示工具状态与权威操作生命周期，不能只把提供方正文当终态。
    # 函数用途: 把一次工具结果渲染给下一轮模型，保留失败恢复动作和副作用核对引用。
    def render_for_prompt(self) -> str:
        parts = [self.render_status_header(), self.output]
        if not self.ok and self.recovery_hint:
            # recovery_hint comes only from the host-owned error taxonomy.  It is
            # therefore safe to show to the next model turn, unlike arbitrary
            # provider/tool error prose, and prevents blind retries when the
            # structured error code already tells us how to repair the call.
            parts.append(
                "[tool-recovery; "
                f"retryable={'true' if self.retryable else 'false'}; "
                f"hint={self.recovery_hint}]"
            )
        parts.append(self.render_execution_facts())
        return "\n".join(parts)

    def render_status_header(self) -> str:
        """Render the backward-compatible tool status and operation header."""

        status = "ok" if self.ok else "error"
        fields = [f"tool={self.tool}", f"status={status}"]
        if not self.ok and self.error_code:
            fields.extend(
                (
                    f"error_code={self.error_code}",
                    f"recommended_action={self.recommended_action}",
                )
            )
        operation = self.result_envelope.get("tool_operation")
        if isinstance(operation, dict):
            for source_key, prompt_key in (
                ("operation_id", "operation_id"),
                ("status", "operation_status"),
                ("action", "operation_action"),
            ):
                value = str(operation.get(source_key) or "").strip()
                if value:
                    fields.append(f"{prompt_key}={value}")
            if operation.get("replayed") is True:
                fields.append("operation_replayed=true")
        if self.effect_outcome:
            fields.append(f"effect_outcome={self.effect_outcome}")
        if self.effect_source_ref:
            fields.append(f"effect_source_ref={self.effect_source_ref[:240]}")
        return f"[{'; '.join(fields)}]"

    def render_execution_facts(self) -> str:
        """Render host-owned lifecycle facts outside any untrusted output body."""

        fields = [
            f"handler_executed={'true' if self.handler_executed else 'false'}",
        ]
        if self.failure_stage:
            fields.append(f"failure_stage={self.failure_stage}")
        fields.append(f"duration_ms={self.duration_ms}")
        return f"[tool-execution; {'; '.join(fields)}]"


def output_policy_for_outcome(
    policy: ToolRuntimePolicy,
    outcome: ToolHandlerOutcome,
) -> OutputPolicy:
    """Resolve the strictest host policy and handler-observed data boundary."""

    configured = policy.output_policy
    details = outcome.result_envelope if isinstance(outcome.result_envelope, dict) else {}
    observed = details.get("tool_output_policy")
    observed = observed if isinstance(observed, dict) else {}
    observed_trust = str(observed.get("trust") or "").strip().lower()
    observed_redaction = str(observed.get("redaction") or "").strip().lower()
    trust = configured.trust
    redaction = configured.redaction
    if observed_trust == "external_data":
        trust = "external_data"
    if observed_redaction == "default" or trust == "external_data":
        redaction = "default"
    return OutputPolicy(
        refs=configured.refs,
        trust=trust,
        redaction=redaction,
    )


def apply_tool_execution_facts(
    result: ToolHandlerOutcome,
    *,
    failure_stage: ToolFailureStage | str | None = None,
    handler_executed: bool | None = None,
    duration_ms: int | float | None = None,
) -> ToolHandlerOutcome:
    """Attach host-owned lifecycle facts at one explicit runtime boundary."""

    if handler_executed is not None:
        result.handler_executed = bool(handler_executed)
    if failure_stage is not None:
        stage = _normalized_tool_failure_stage(failure_stage)
        if result.ok and stage:
            raise ValueError("successful tool result cannot report a failure stage")
        result.failure_stage = stage
    if duration_ms is not None:
        result.duration_ms = _nonnegative_duration_ms(duration_ms)
    return result


def _normalized_tool_failure_stage(value: ToolFailureStage | str | object) -> str:
    if isinstance(value, ToolFailureStage):
        normalized = value.value
    else:
        normalized = str(value or "").strip().lower()
    if normalized not in {"", *_TOOL_FAILURE_STAGE_VALUES}:
        raise ValueError(f"invalid tool failure stage: {normalized}")
    return normalized


def _nonnegative_duration_ms(value: object) -> int:
    try:
        parsed = int(float(value or 0))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("tool duration_ms must be a finite non-negative number") from None
    if parsed < 0:
        raise ValueError("tool duration_ms must be non-negative")
    return parsed


# LLM: 核对上下文只携带操作账本中的结构化事实；工具不能从用户措辞猜测动作是否已经发生。
# 类用途: 给可选的工具核对器提供同一业务操作的稳定身份和此前结果。
@dataclass(frozen=True)
class ToolOperationReconciliationContext:
    owner_id: str
    run_id: str
    task_id: str
    operation_id: str
    tool_name: str
    args_hash: str
    idempotency_key: str
    idempotency_scope: str
    prior_result: dict[str, Any] = field(default_factory=dict)


# LLM: 核对器只能返回约定的机器结论；safe_to_retry 也必须来自已注册 provider 的结构化幂等能力。
# 类用途: 表示已成功、明确失败、未开始、可安全重放或仍不确定的核对结果。
@dataclass(frozen=True)
class ToolOperationReconciliation:
    outcome: str = "unknown"
    source_ref: str = ""
    result: ToolHandlerOutcome | None = None
    reason: str = ""


# LLM: 可用性只描述当前进程的结构化就绪状态，绝不能承担 owner/任务授权，也不能运行有业务副作用的探针。
# 类用途: 统一表示工具能否在当前运行环境工作，并给最终执行复检提供稳定错误原因。
@dataclass(frozen=True)
class ToolAvailability:
    available: bool
    error_code: str = ""
    reason: str = ""

    # LLM: 默认工具是就绪的；具体工具只在有可证明的配置或进程缺口时覆盖为 unavailable。
    # 函数用途: 创建无错误信息的就绪结果，避免每个工具重复拼布尔状态。
    @classmethod
    def ready(cls) -> ToolAvailability:
        return cls(available=True)

    # LLM: 不可用原因是机器事实而非授权提示，调用方仍须先完成权限检查再展示它。
    # 函数用途: 创建标准 TOOL_UNAVAILABLE 结果，供 Schema 过滤和执行前复检共用。
    @classmethod
    def unavailable(
        cls,
        reason: str,
        *,
        error_code: str = "TOOL_UNAVAILABLE",
    ) -> ToolAvailability:
        return cls(available=False, error_code=error_code, reason=str(reason or "").strip())


@dataclass(frozen=True)
class ToolExposure:
    model_visible: bool = True
    owner_types: tuple[str, ...] = ("main_agent", "task_local")


@dataclass(frozen=True)
class ToolRuntime:
    """One immutable binding of model definition, policy and business handler."""

    model_spec: ToolModelSpec
    runtime_policy: ToolRuntimePolicy
    handler: Any = field(compare=False, repr=False)
    availability: ToolAvailability = field(default_factory=ToolAvailability.ready)
    exposure: ToolExposure = field(default_factory=ToolExposure)

    def __post_init__(self) -> None:
        self.model_spec.assert_schema_hash()
        _validate_runtime_policy(self.model_spec, self.runtime_policy)
        handler_name = str(
            getattr(getattr(self.handler, "model_spec", None), "name", "") or ""
        ).strip()
        if handler_name and handler_name != self.model_spec.name:
            raise ValueError(
                f"tool handler/model name mismatch: {handler_name} != {self.model_spec.name}"
            )


def _validate_runtime_policy(
    model_spec: ToolModelSpec,
    runtime_policy: ToolRuntimePolicy,
) -> None:
    """Fail snapshot construction when policy fields contradict the sole schema."""

    properties = model_spec.input_schema.get("properties")
    if not isinstance(properties, dict):
        raise ValueError(f"tool input schema properties must be an object: {model_spec.name}")
    public_names = {str(name) for name in properties}
    policy = runtime_policy.input_policy
    internal_names = {str(name) for name in policy.internal_parameters}
    if len(internal_names) != len(policy.internal_parameters) or "" in internal_names:
        raise ValueError(f"duplicate or empty internal parameter: {model_spec.name}")
    overlap = sorted(public_names & internal_names)
    if overlap:
        raise ValueError(
            f"internal parameters must not be model-visible: {model_spec.name}: {overlap}"
        )
    defaults = dict(policy.safe_parameter_defaults)
    bindings = dict(policy.trusted_parameter_bindings)
    if len(defaults) != len(policy.safe_parameter_defaults):
        raise ValueError(f"duplicate safe parameter default: {model_spec.name}")
    if len(bindings) != len(policy.trusted_parameter_bindings):
        raise ValueError(f"duplicate trusted parameter binding: {model_spec.name}")
    duplicate_completion = sorted(set(defaults) & set(bindings))
    if duplicate_completion:
        raise ValueError(
            f"parameter cannot have both default and trusted binding: {model_spec.name}: {duplicate_completion}"
        )
    for name, value in defaults.items():
        _require_public_input_parameter(model_spec, public_names, name, "safe default")
        _validate_runtime_default(model_spec, str(name), value)
    for name, binding in bindings.items():
        _require_runtime_input_parameter(
            model_spec,
            public_names,
            internal_names,
            name,
            "trusted binding",
        )
        if not isinstance(binding, TrustedParameterBinding) or not binding.source_refs:
            raise ValueError(f"invalid trusted parameter binding: {model_spec.name}.{name}")
        for source_ref in binding.source_refs:
            _validate_trusted_source_ref(model_spec.name, source_ref)
        for condition_name, _expected in binding.when:
            _require_public_input_parameter(
                model_spec,
                public_names,
                condition_name,
                f"trusted binding condition for {name}",
            )
    for name in policy.local_file_url_parameters:
        _require_public_input_parameter(
            model_spec,
            public_names,
            name,
            "local file URL parameter",
        )
    resolver = runtime_policy.effect_resolver
    for field_name, _variants in resolver.by_parameter:
        _require_public_input_parameter(
            model_spec,
            public_names,
            field_name,
            "effect resolver",
        )
    if resolver.command_parameter:
        _require_public_input_parameter(
            model_spec,
            public_names,
            resolver.command_parameter,
            "command effect resolver",
        )
    for name in runtime_policy.resource_scopes.parameter_names:
        _require_runtime_input_parameter(
            model_spec,
            public_names,
            internal_names,
            name,
            "resource scope",
        )


def _require_runtime_input_parameter(
    model_spec: ToolModelSpec,
    public_names: set[str],
    internal_names: set[str],
    name: object,
    contract: str,
) -> None:
    field_name = str(name or "").strip()
    if not field_name or field_name not in public_names | internal_names:
        raise ValueError(
            f"{contract} references undeclared parameter: "
            f"{model_spec.name}.{field_name or '<empty>'}"
        )


def _require_public_input_parameter(
    model_spec: ToolModelSpec,
    public_names: set[str],
    name: object,
    contract: str,
) -> None:
    field_name = str(name or "").strip()
    if not field_name or field_name not in public_names:
        raise ValueError(
            f"{contract} references undeclared parameter: {model_spec.name}.{field_name or '<empty>'}"
        )


def _validate_runtime_default(
    model_spec: ToolModelSpec,
    name: str,
    value: Any,
) -> None:
    from .input_schema import validate_tool_input

    schema = model_spec.input_schema
    probe: dict[str, Any] = {
        "type": "object",
        "properties": deepcopy(schema.get("properties") or {}),
        "required": [name],
        "additionalProperties": False,
    }
    for definitions_key in ("$defs", "definitions"):
        definitions = schema.get(definitions_key)
        if isinstance(definitions, dict):
            probe[definitions_key] = deepcopy(definitions)
    validation = validate_tool_input({name: value}, probe)
    if not validation.ok:
        issues = ",".join(f"{issue.path}:{issue.keyword}" for issue in validation.issues[:4])
        raise ValueError(f"safe default violates schema: {model_spec.name}.{name}: {issues}")


def _validate_trusted_source_ref(tool_name: str, source_ref: object) -> None:
    text = str(source_ref or "").strip()
    parts = text.split(".")
    if (
        len(parts) < 2
        or parts[0] not in {"registry", "run_scope", "write_boundary"}
        or any(not part or not part.replace("_", "").isalnum() for part in parts)
    ):
        raise ValueError(f"invalid trusted source_ref: {tool_name}: {text or '<empty>'}")


# LLM: 该快照是一次 Agent run 的工具事实面；Schema、目录、搜索与最终调用只能在它上面继续做减法。
# 类用途: 固定一次请求开始时已授权且已就绪的工具集合，并保留授权范围内不可用工具的结构化原因。
@dataclass(frozen=True)
class ToolRuntimeSnapshot:
    run_id: str
    runtimes: tuple[ToolRuntime, ...]
    available_tool_names: frozenset[str]
    unavailable_tools: tuple[tuple[str, str, str], ...]
    allowed_tools: frozenset[str] | None
    owner_type: str = "main_agent"
    snapshot_hash: str = ""

    def __post_init__(self) -> None:
        names = [runtime.model_spec.name for runtime in self.runtimes]
        if len(names) != len(set(names)):
            raise ValueError("tool runtime snapshot contains duplicate names")
        expected_available = frozenset(names)
        if self.available_tool_names != expected_available:
            raise ValueError("tool runtime snapshot available names do not match runtimes")
        unavailable_names = [str(item[0] or "").strip() for item in self.unavailable_tools]
        if len(unavailable_names) != len(set(unavailable_names)):
            raise ValueError("tool runtime snapshot contains duplicate unavailable names")
        if expected_available.intersection(unavailable_names):
            raise ValueError("tool runtime cannot be both available and unavailable")
        for runtime in self.runtimes:
            runtime.model_spec.assert_schema_hash()
        digest = _tool_runtime_snapshot_hash(self.run_id, self.runtimes, self.owner_type)
        supplied = str(self.snapshot_hash or "").strip()
        if supplied and supplied != digest:
            raise ValueError("tool runtime snapshot hash mismatch")
        object.__setattr__(self, "snapshot_hash", digest)

    @property
    def specs(self) -> tuple[ToolModelSpec, ...]:
        """Derived model specs; the runtime bindings remain the authority."""

        for runtime in self.runtimes:
            runtime.model_spec.assert_schema_hash()
        return tuple(runtime.model_spec for runtime in self.runtimes)

    def runtime(self, name: str) -> ToolRuntime | None:
        runtime = next(
            (
                runtime
                for runtime in self.runtimes
                if runtime.model_spec.name == str(name or "").strip()
            ),
            None,
        )
        if runtime is not None:
            runtime.model_spec.assert_schema_hash()
        return runtime


def _tool_runtime_snapshot_hash(
    run_id: str,
    runtimes: tuple[ToolRuntime, ...],
    owner_type: str,
) -> str:
    payload = {
        "run_id": str(run_id or ""),
        "owner_type": str(owner_type or ""),
        "tools": [
            {
                "name": runtime.model_spec.name,
                "schema_hash": runtime.model_spec.schema_hash,
                "available": runtime.availability.available,
                "model_visible": runtime.exposure.model_visible,
            }
            for runtime in runtimes
        ],
    }
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )


# LLM: 工具实现拿到的是不可变调用上下文；目录工具不能再从进程全局注册表猜测本轮权限。
# 类用途: 把同一请求快照传给 list_tools/tool_search，同时让普通工具沿用原 execute 合同。
@dataclass(frozen=True)
class ToolInvocationContext:
    runtime_snapshot: ToolRuntimeSnapshot
    cancellation_token: object | None = None


@dataclass
class ToolSearchHit:
    name: str
    score: float
    reasons: list[str]


class BaseToolSearchProvider:
    name = "base"

    def search(self, query: str, specs: list[ToolModelSpec], limit: int) -> list[ToolSearchHit]:
        raise NotImplementedError


class KeywordToolSearchProvider(BaseToolSearchProvider):
    name = "keyword"

    def search(self, query: str, specs: list[ToolModelSpec], limit: int) -> list[ToolSearchHit]:
        tokens = _tokenize(query)
        hits: list[ToolSearchHit] = []
        for spec in specs:
            score, reasons = _score_keyword_spec(spec, tokens)
            if score > 0:
                hits.append(ToolSearchHit(name=spec.name, score=score, reasons=reasons[:3]))
        hits.sort(key=lambda item: (-item.score, item.name))
        return hits[:limit]


class VectorToolSearchProvider(BaseToolSearchProvider):
    name = "vector"

    def __init__(
        self,
        enabled: bool = False,
        embedder: EmbeddingProvider | None = None,
        *,
        min_score: float = 0.15,
    ):
        self.enabled = enabled
        self.embedder = embedder
        self.min_score = float(min_score)
        self._document_key: tuple[str, ...] = ()
        self._document_vectors: list[list[float]] = []
        self.last_error = ""

    def search(self, query: str, specs: list[ToolModelSpec], limit: int) -> list[ToolSearchHit]:
        if not self.enabled or self.embedder is None or not query.strip() or not specs:
            return []
        documents = tuple(_tool_semantic_document(spec) for spec in specs)
        try:
            hits = self._semantic_search(query, specs, documents)
        except Exception as exc:
            # Semantic retrieval is additive: an endpoint outage falls back to
            # keyword results, but the status remains machine-visible.
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []
        self.last_error = ""
        return hits[:limit]

    def _semantic_search(
        self,
        query: str,
        specs: list[ToolModelSpec],
        documents: tuple[str, ...],
    ) -> list[ToolSearchHit]:
        self._ensure_document_vectors(documents)
        query_vectors = self.embedder.embed([query])
        if len(query_vectors) != 1:
            raise ValueError("tool query embedding count mismatch")
        return _semantic_tool_hits(
            specs,
            query_vectors[0],
            self._document_vectors,
            min_score=self.min_score,
        )

    def _ensure_document_vectors(self, documents: tuple[str, ...]) -> None:
        if documents == self._document_key:
            return
        vectors = self.embedder.embed(list(documents))
        if len(vectors) != len(documents):
            raise ValueError(
                f"tool document embedding count mismatch expected={len(documents)} got={len(vectors)}"
            )
        self._document_key = documents
        self._document_vectors = vectors

    def status(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "configured": self.embedder is not None,
            "ready": bool(self.enabled and self.embedder is not None and not self.last_error),
            "provider": type(self.embedder).__name__ if self.embedder is not None else "",
            "last_error": self.last_error,
        }


class HybridToolRetriever:
    def __init__(self, providers: list[BaseToolSearchProvider]):
        self.providers = providers

    def search(self, query: str, specs: list[ToolModelSpec], limit: int) -> list[ToolSearchHit]:
        merged: dict[str, ToolSearchHit] = {}
        for provider in self.providers:
            for hit in provider.search(query, specs, limit):
                _merge_tool_hit(merged, provider.name, hit)
        ranked = sorted(merged.values(), key=lambda item: (-item.score, item.name))
        return ranked[:limit]

    def status(self) -> dict[str, Any]:
        providers: dict[str, Any] = {}
        for provider in self.providers:
            status = getattr(provider, "status", None)
            providers[provider.name] = status() if callable(status) else {"enabled": True}
        return {"mode": "hybrid", "providers": providers}


# LLM: 普通 handler 实现 execute；Registry 只调用 execute_scoped，默认委托是基类适配而非第二条执行链。
# 类用途: 定义工具执行、无副作用就绪检查和请求上下文调用三个稳定底层合同。
class BaseTool:
    model_spec: ToolModelSpec
    runtime_policy: ToolRuntimePolicy

    # LLM: 默认就绪避免为几十个纯本地工具写空检查；可选后端工具按结构化配置覆盖。
    # 函数用途: 返回不触发网络、进程或业务写入的当前就绪状态。
    def availability(self) -> ToolAvailability:
        return ToolAvailability.ready()

    # LLM: 只有需要请求快照的工具覆盖本方法；其余 handler 由这个唯一受权入口委托 execute。
    # 函数用途: 在统一调用入口传递请求快照，同时向后兼容现有工具实现。
    def execute_scoped(
        self,
        params: dict[str, Any],
        context: ToolInvocationContext,
    ) -> ToolHandlerOutcome:
        _ = context
        return self.execute(params)

    # LLM: business 幂等键必须来自可信运行事实和结构化参数，默认空值会让 business 工具执行前失败关闭。
    # 函数用途: 为跨调用仍代表同一外部动作的工具生成稳定业务身份。
    def business_idempotency_key(self, params: dict[str, Any]) -> str:
        _ = params
        return ""

    # LLM: 默认没有目标系统核对能力；绝不能因“看起来像成功”而把 unknown 改成可重试。
    # 函数用途: 允许少数能查询目标系统的工具在未知结果后提供结构化核对结论。
    def reconcile_operation(
        self,
        params: dict[str, Any],
        context: ToolOperationReconciliationContext,
    ) -> ToolOperationReconciliation:
        _ = (params, context)
        return ToolOperationReconciliation()

    # LLM: 只有执行写根不在 resource_scopes 参数里的工具覆盖本方法；默认无额外写根。
    # 函数用途: 结构化声明执行写根（已解析的绝对路径字符串元组），operation lock
    # 与写边界校验共用同一提取器（seq 253 #5 单一权威——不按工具名/内部参数名
    # 特判，特判覆盖不了 apply_patch 的 patch 文本目标与 controlled_exec 的
    # grant.path_scope 这类写根不在参数里的工具）。
    def effective_write_roots(
        self,
        arguments: dict[str, Any],
        write_boundary: dict[str, Any] | None,
        workspace_root: Path,
    ) -> tuple[str, ...]:
        _ = (arguments, write_boundary, workspace_root)
        return ()

    # LLM: 只有运行期才能确定资源目标（cancel 的 root 子树展开/status 过滤、
    # dispatch 的自动选池、audit 的当前 Audit）的工具覆盖本方法；默认无运行期资源。
    # 函数用途: 结构化声明 claim 前要补锁的资源 scope（逻辑域文本串元组），
    # executor 在 claim 前调用并合并（seq 266 #3 协议，BaseTool 正式默认——
    # 不做 getattr 半协议）。hook 内部解析失败必须自己保守锁稳定域（绝不空锁）；
    # 能抛异常 = hook 实现缺陷，由 executor fail-closed 拒绝执行（seq 269 #3）。
    def effective_resource_scopes(
        self,
        arguments: dict[str, Any],
        write_boundary: dict[str, Any] | None,
        workspace_root: Path,
    ) -> tuple[str, ...]:
        _ = (arguments, write_boundary, workspace_root)
        return ()

    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        raise NotImplementedError


def _truncate_rendered_tool_entry(text: str, *, max_chars: int, label: str) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + f"\n  ... 已按 {label} 截断"


def _tokenize(text: str) -> list[str]:

    lowered = (text or "").lower()
    # [^\W\u4e00-\u9fff]+ = \u4efb\u610f\u811a\u672c\u8bcd\u5b57\u7b26(\u9664 CJK)\u2192 \u975e\u4e2d\u82f1\u8bed\u8a00\u4e0d\u518d\u96f6 token(\u5ba1\u8ba1 #7);CJK \u4ecd\u5355\u5217\u8d70 subtoken
    tokens = re.findall(r"[^\W\u4e00-\u9fff]+|[\u4e00-\u9fff]+", lowered)
    expanded: list[str] = []
    for token in tokens:
        expanded.append(token)
        expanded.extend(_chinese_subtokens(token))
    seen: set[str] = set()
    unique: list[str] = []
    for token in expanded:
        if token not in seen:
            seen.add(token)
            unique.append(token)
    return unique


def _score_keyword_spec(spec: ToolModelSpec, tokens: list[str]) -> tuple[float, list[str]]:
    haystacks = _keyword_haystacks(spec)
    score = 0.0
    reasons: list[str] = []
    for token in tokens:
        token_score, token_reasons = _score_keyword_token(token, haystacks)
        score += token_score
        reasons.extend(token_reasons[:1])
    return score, reasons


def _merge_tool_hit(
    merged: dict[str, ToolSearchHit],
    provider_name: str,
    hit: ToolSearchHit,
) -> None:
    existing = merged.get(hit.name)
    provider_reason = f"{provider_name} 召回"
    if existing is None:
        merged[hit.name] = ToolSearchHit(
            name=hit.name,
            score=hit.score,
            reasons=[provider_reason, *hit.reasons][:4],
        )
        return
    existing.score += hit.score
    _append_unique_reasons(existing.reasons, [provider_reason, *hit.reasons])


def _append_unique_reasons(target: list[str], reasons: list[str]) -> None:
    for reason in reasons:
        if reason not in target:
            target.append(reason)
    del target[4:]


def _keyword_haystacks(spec: ToolModelSpec) -> dict[str, str]:
    return {
        "name": spec.name.lower(),
        "description": spec.description.lower(),
        "category": spec.category.lower(),
        "keywords": " ".join(spec.keywords).lower(),
        "use_cases": " ".join(spec.use_cases).lower(),
    }


def _tool_semantic_document(spec: ToolModelSpec) -> str:
    return "\n".join(
        (
            f"name: {spec.name}",
            f"category: {spec.category}",
            f"description: {spec.description}",
            "use cases: " + " | ".join(spec.use_cases),
            "avoid when: " + " | ".join(spec.avoid_when),
            "keywords: " + " | ".join(spec.keywords),
        )
    )


def _semantic_tool_hits(
    specs: list[ToolModelSpec],
    query_vector: list[float],
    document_vectors: list[list[float]],
    *,
    min_score: float,
) -> list[ToolSearchHit]:
    hits: list[ToolSearchHit] = []
    for spec, vector in zip(specs, document_vectors, strict=True):
        score = cosine(query_vector, vector)
        if score < min_score:
            continue
        hits.append(
            ToolSearchHit(
                name=spec.name,
                score=score * 8.0,
                reasons=[f"语义相似度 {score:.3f}"],
            )
        )
    return sorted(hits, key=lambda item: (-item.score, item.name))


def _score_keyword_token(token: str, haystacks: dict[str, str]) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    checks = [
        ("name", 6.0, f"命中工具名'{token}'"),
        ("keywords", 4.0, f"命中关键词'{token}'"),
        ("category", 2.5, f"命中类别'{token}'"),
    ]
    for key, weight, reason in checks:
        if _keyword_token_matches(token, haystacks[key]):
            score += weight
            reasons.append(reason)
    if _keyword_token_matches(token, haystacks["description"]) or _keyword_token_matches(
        token, haystacks["use_cases"]
    ):
        score += 1.5
        reasons.append(f"命中用途描述'{token}'")
    return score, reasons


def _keyword_token_matches(token: str, haystack: str) -> bool:
    """Do not let short ASCII extensions match arbitrary name substrings."""
    if re.fullmatch(r"[a-z0-9]{1,2}", token):
        return token in re.findall(r"[a-z0-9]+", haystack)
    return token in haystack


def _chinese_subtokens(token: str) -> list[str]:
    if not re.fullmatch(r"[\u4e00-\u9fff]+", token):
        return []
    return [
        token[idx : idx + size]
        for size in range(2, min(4, len(token)) + 1)
        for idx in range(0, len(token) - size + 1)
    ]
