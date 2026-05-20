# LLM: Runtime config contracts validate startup safety from structured config keys.
# 模块用途: 校验运行配置的必填字段、数字类型和危险组合，供 doctor/离线测试在启动前发现问题。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


# LLM: RuntimeConfigValidation is the machine-readable doctor result for runtime settings.
# 类用途: 返回配置是否通过、错误码和逐项 finding，方便 CLI/测试直接消费。
@dataclass(frozen=True)
class RuntimeConfigValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, str], ...]


# LLM: validate_runtime_config checks required fields and dangerous switches without reading prompt prose.
# 函数用途: 校验 workspace_root、artifact_dir、tool_timeout、max_steps 和高危配置组合。
def validate_runtime_config(config: dict[str, Any]) -> RuntimeConfigValidation:
    findings: list[dict[str, str]] = []
    _validate_required_text(config, "workspace_root", "CONFIG_WORKSPACE_ROOT_MISSING", findings)
    _validate_required_text(config, "artifact_dir", "CONFIG_ARTIFACT_DIR_MISSING", findings)
    _validate_positive_number(config, "tool_timeout", "CONFIG_TOOL_TIMEOUT", findings)
    _validate_positive_integer(config, "max_steps", "CONFIG_MAX_STEPS", findings)
    _validate_dangerous_combinations(config, findings)
    return RuntimeConfigValidation(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(item["code"] for item in findings)),
        findings=tuple(findings),
    )


# LLM: _validate_required_text enforces non-empty string-like config values.
# 函数用途: 检查必填文本配置是否存在且非空。
def _validate_required_text(
    config: dict[str, Any],
    field: str,
    missing_code: str,
    findings: list[dict[str, str]],
) -> None:
    if str(config.get(field) or "").strip():
        return
    findings.append(_finding(missing_code, field, "required"))


# LLM: _validate_positive_number handles timeout-like settings with int or float values.
# 函数用途: 校验配置字段必须是正数；缺失和类型错误用不同错误码。
def _validate_positive_number(
    config: dict[str, Any],
    field: str,
    code_prefix: str,
    findings: list[dict[str, str]],
) -> None:
    if field not in config or config.get(field) is None:
        findings.append(_finding(f"{code_prefix}_MISSING", field, "required"))
        return
    value = config.get(field)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        findings.append(_finding(f"{code_prefix}_INVALID", field, "positive_number_required"))


# LLM: _validate_positive_integer handles step-budget settings before execution loops start.
# 函数用途: 校验配置字段必须是正整数；缺失和类型错误用不同错误码。
def _validate_positive_integer(
    config: dict[str, Any],
    field: str,
    code_prefix: str,
    findings: list[dict[str, str]],
) -> None:
    if field not in config or config.get(field) is None:
        findings.append(_finding(f"{code_prefix}_MISSING", field, "required"))
        return
    value = config.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        findings.append(_finding(f"{code_prefix}_INVALID", field, "positive_integer_required"))


# LLM: _validate_dangerous_combinations flags settings that widen write or side-effect scope.
# 函数用途: 检查根目录工作区、shell 开关、无审批高危动作这些通用危险组合。
def _validate_dangerous_combinations(config: dict[str, Any], findings: list[dict[str, str]]) -> None:
    root = str(config.get("workspace_root") or "").strip()
    if root and Path(root).expanduser() == Path("/"):
        findings.append(_finding("CONFIG_WORKSPACE_ROOT_DANGEROUS", "workspace_root", "root_workspace_forbidden"))
    if bool(config.get("allow_shell")):
        findings.append(_finding("CONFIG_ALLOW_SHELL_ENABLED", "allow_shell", "shell_requires_explicit_review"))
    if bool(config.get("allow_dangerous_actions")) and config.get("approval_required") is False:
        findings.append(
            _finding(
                "CONFIG_DANGEROUS_ACTIONS_WITHOUT_APPROVAL",
                "approval_required",
                "dangerous_actions_need_approval",
            )
        )


# LLM: _finding keeps config diagnostics stable and compact.
# 函数用途: 生成 code/field/detail 结构化 finding。
def _finding(code: str, field: str, detail: str) -> dict[str, str]:
    return {"code": code, "field": field, "detail": detail}


__all__ = ["RuntimeConfigValidation", "validate_runtime_config"]
