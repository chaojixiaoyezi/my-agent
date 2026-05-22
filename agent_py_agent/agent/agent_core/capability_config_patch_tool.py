# LLM: capability_config_patch gives the model a safe config self-healing tool instead of raw YAML edits.
# 模块用途: 提供模型可调用的 capability_config 补丁工具，自动处理 allowlist、版本、审计和热加载。

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from ..capability.runtime_config import (
    CapabilityConfigPatch,
    CapabilityConfigPatchRequest,
    apply_capability_config_patch,
    default_capability_config_path,
    load_capability_config_snapshot,
)
from ..tools import BaseTool, ToolExecutionResult, ToolSpec
from .parameters import _bool_param

if TYPE_CHECKING:
    from ..core import SimpleAgent


_TOOL_NAME = "capability_config_patch"


# LLM: CapabilityConfigPatchTool is the model-facing lane for safe runtime config adjustments.
# 类用途: 允许主代理在发现配置明显不合理时提交结构化补丁；危险字段只返回建议。
class CapabilityConfigPatchTool(BaseTool):

    # LLM: __init__ stores the agent facade and builds a stable model-visible spec.
    # 函数用途: 初始化配置补丁工具，不读取或写入配置文件。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_capability_config_patch_spec()

    # LLM: execute validates patch bundles, writes safe fields, and refreshes future config snapshots.
    # 函数用途: 执行 capability_config_patch 工具调用；只自动应用 allowlist 中的安全字段。
    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        normalized = _patch_tool_params(params)
        patches = _patches_from_params(normalized)
        if not patches:
            return ToolExecutionResult(_TOOL_NAME, False, "缺少 patches；请传 field/value 或 patches 列表。")
        config_path = _config_path(self.agent, normalized)
        result = apply_capability_config_patch(
            CapabilityConfigPatchRequest(
                config_path=config_path,
                patches=patches,
                apply=_bool_param(normalized.get("apply"), default=False),
                expected_version=str(normalized.get("expected_version") or "").strip(),
                actor=str(normalized.get("actor") or "agent:capability_config_patch").strip(),
                reason=str(normalized.get("reason") or "").strip(),
                audit_path=_audit_path(self.agent, normalized),
                notice_path=_notice_path(self.agent, normalized),
                scope=_object_dict(normalized.get("scope")),
            )
        )
        _refresh_agent_capability_config(self.agent, config_path, result.applied)
        return ToolExecutionResult(_TOOL_NAME, result.ok, json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


# LLM: build_capability_config_patch_spec teaches the model when to use config patching.
# 函数用途: 生成 capability_config_patch 的工具说明，避免模型要求用户手改 YAML。
def build_capability_config_patch_spec() -> ToolSpec:
    return ToolSpec(
        name=_TOOL_NAME,
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description=(
            "安全调整 capability_config.yaml 的结构化补丁工具；"
            "适合配置明显阻塞子代理调度、超时、能力包大小或候选数量时使用。"
        ),
        use_cases=[
            "子代理调度因安全字段配置过小而反复误判，需要后续 dispatch/watch 读取新值",
            "需要给用户一条可审计的配置建议，而不是让模型直接编辑 YAML",
        ],
        avoid_when=[
            "只是普通任务失败，先用 dispatch/board/测试定位问题",
            "全局行为开关或未知字段不会自动写入，只会返回建议",
        ],
        keywords=["config", "capability_config", "hot reload", "配置", "热加载", "超时", "能力路由"],
        parameters={
            "patches": "列表，每项包含 field/value/reason；推荐一次提交相关字段",
            "field": "单字段快捷写法；和 value 配套使用",
            "value": "单字段快捷写法的目标值",
            "apply": "false 只预览；true 才写入安全字段",
            "expected_version": "可选；配置文件内容 hash，不匹配时拒绝覆盖",
            "reason": "本次变更的原因，写入审计",
            "scope": "可选 JSON 对象，记录 run_id/task_id 等来源",
        },
        parameter_details={
            "patches": "JSON 数组，例如 [{\"field\":\"subagent_run_timeout\",\"value\":1800,\"reason\":\"真实 runner 超时\"}]。",
            "apply": "默认 false。apply=true 也只会自动应用 allowlist 安全字段；危险字段返回 manual_approval_required。",
            "expected_version": "用于并发保护。拿不到版本时可以省略，但工具仍会写审计记录。",
            "scope": "例如 {\"run_id\":\"root-1\",\"task_id\":\"task-1\"}，方便后续复盘是哪次任务触发的。",
        },
        examples=[
            (
                '{"tool":"capability_config_patch","apply":true,'
                '"patches":[{"field":"subagent_run_timeout","value":1800,'
                '"reason":"runner 真实 E2E 超过默认时间"}],"scope":{"run_id":"root-1"}}'
            )
        ],
    )


# LLM: _patch_tool_params accepts flat calls plus request/config_patch bundles.
# 函数用途: 兼容模型按 bundle 规范传参；top-level 字段优先，wrapper 字段补齐缺省。
def _patch_tool_params(params: dict[str, object]) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for key in ("orchestration", "capability_config_patch", "request"):
        value = params.get(key)
        if isinstance(value, dict):
            normalized.update(value)
    for key, value in params.items():
        if key not in {"orchestration", "capability_config_patch", "request", "filesystem"}:
            normalized[key] = value
    return normalized


# LLM: _patches_from_params normalizes list, dict-map, and single field/value calls.
# 函数用途: 把模型传入的补丁格式转换成 CapabilityConfigPatch 列表。
def _patches_from_params(params: dict[str, object]) -> list[CapabilityConfigPatch]:
    raw_patches = params.get("patches")
    if isinstance(raw_patches, list):
        return _patch_list_from_items(raw_patches)
    raw_changes = params.get("changes")
    if isinstance(raw_changes, dict):
        return [
            CapabilityConfigPatch(field=str(field), value=value, reason=str(params.get("reason") or ""))
            for field, value in raw_changes.items()
        ]
    field = str(params.get("field") or "").strip()
    if field and "value" in params:
        return [CapabilityConfigPatch(field=field, value=params.get("value"), reason=str(params.get("reason") or ""))]
    return []


# LLM: _patch_list_from_items flattens model list input without nesting the main parser.
# 函数用途: 把 patches 数组逐条解析，忽略缺字段或坏形态条目。
def _patch_list_from_items(items: list[object]) -> list[CapabilityConfigPatch]:
    patches: list[CapabilityConfigPatch] = []
    for item in items:
        patch = _patch_from_item(item)
        if patch is not None:
            patches.append(patch)
    return patches


# LLM: _patch_from_item keeps malformed patch entries out of the service layer.
# 函数用途: 解析单条 patch 字典；缺 field 的条目直接忽略。
def _patch_from_item(item: object) -> CapabilityConfigPatch | None:
    if not isinstance(item, dict):
        return None
    field = str(item.get("field") or "").strip()
    if not field:
        return None
    return CapabilityConfigPatch(
        field=field,
        value=item.get("value"),
        reason=str(item.get("reason") or ""),
    )


# LLM: _config_path resolves explicit paths first, then agent state, then project defaults.
# 函数用途: 找到本次要补丁的 capability_config.yaml，用户不用记仓库路径。
def _config_path(agent: SimpleAgent, params: dict[str, object]) -> Path:
    raw = str(params.get("config_path") or "").strip()
    if raw:
        return Path(raw).expanduser()
    existing = getattr(agent, "capability_config_path", None)
    if existing:
        return Path(existing)
    return default_capability_config_path(getattr(agent, "root", Path.cwd()))


# LLM: _audit_path places runtime audit files in subagent workspace by default.
# 函数用途: 解析审计日志路径；未传时写到当前 agent 的 subagent 工作区，避免污染配置文件。
def _audit_path(agent: SimpleAgent, params: dict[str, object]) -> Path | None:
    raw = str(params.get("audit_path") or "").strip()
    if raw:
        return Path(raw).expanduser()
    workspace = getattr(getattr(agent, "subagents", None), "workspace", None)
    return Path(workspace) / "capability_config_audit.jsonl" if workspace else None


# LLM: _notice_path writes a lightweight notification trail next to subagent reports.
# 函数用途: 解析配置变化通知路径；后续可接 shared board/broadcast，不影响已运行子代理。
def _notice_path(agent: SimpleAgent, params: dict[str, object]) -> Path | None:
    raw = str(params.get("notice_path") or "").strip()
    if raw:
        return Path(raw).expanduser()
    workspace = getattr(getattr(agent, "subagents", None), "workspace", None)
    return Path(workspace) / "CAPABILITY_CONFIG_CHANGES.md" if workspace else None


# LLM: _refresh_agent_capability_config stores the new snapshot for later dispatch/watch calls.
# 函数用途: 安全补丁成功后刷新 agent 上的配置快照；不回写已经启动的 runner 上下文。
def _refresh_agent_capability_config(agent: SimpleAgent, config_path: Path, applied: bool) -> None:
    if not applied:
        return
    agent.capability_config_path = config_path
    try:
        agent._capability_config_runtime_snapshot = load_capability_config_snapshot(config_path)
    except (FileNotFoundError, OSError, ValueError):
        return


# LLM: _object_dict accepts only JSON-object-like scope values.
# 函数用途: 解析 scope 字段，避免把大段文本塞进审计记录。
def _object_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return {str(key): item for key, item in parsed.items()}
    return {}
