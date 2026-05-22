# LLM: Error classification rules rank legacy raw-error text into stable codes.
# 模块用途: 将错误文本 fallback 的匹配规则从错误合同字典中分离，保持 taxonomy 文件小而稳定。

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorClassificationRule:
    code: str
    specificity: int
    patterns: tuple[re.Pattern[str], ...]


# LLM: matched_error_codes returns all matching codes with specificity scores for deterministic ranking.
# 函数用途: 收集精确错误码和 fallback 文本规则命中，不在第一个宽匹配处提前返回。
def matched_error_codes(text: str, contract_codes: Iterable[str]) -> list[tuple[int, str]]:
    payload = str(text or "")
    return [
        *_exact_code_matches(payload, contract_codes),
        *(
            (rule.specificity, rule.code)
            for rule in CLASSIFICATION_RULES
            if any(pattern.search(payload) for pattern in rule.patterns)
        ),
    ]


# LLM: _exact_code_matches trusts machine-like error code tokens but not ordinary phrase variants.
# 函数用途: 识别 PATH_INVALID/path-invalid 这类结构化错误码，避免 "path invalid" 普通正文抢优先级。
def _exact_code_matches(text: str, contract_codes: Iterable[str]) -> list[tuple[int, str]]:
    lowered = text.lower()
    matches: list[tuple[int, str]] = []
    for code in contract_codes:
        variants = (code.lower(), code.lower().replace("_", "-"))
        if any(re.search(rf"(?<![a-z0-9]){re.escape(variant)}(?![a-z0-9])", lowered) for variant in variants):
            matches.append((1000, code))
    return matches


# LLM: _patterns compiles one rule's accepted raw-error fallback patterns.
# 函数用途: 统一编译正则，保持规则表只描述错误信号和优先级。
def _patterns(items: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(item, re.IGNORECASE) for item in items)


CLASSIFICATION_RULES: tuple[ErrorClassificationRule, ...] = (
    ErrorClassificationRule(
        "PATH_OUTSIDE_WORKSPACE",
        95,
        _patterns((r"\bpath[_ -]?outside[_ -]?workspace\b", r"\boutside workspace\b", r"路径.*工作区外")),
    ),
    ErrorClassificationRule(
        "APPROVAL_REQUIRED",
        90,
        _patterns((r"\bapproval[_ -]?required\b", r"\brequires approval\b", r"需要审批", r"需要.*批准")),
    ),
    ErrorClassificationRule(
        "NO_PROGRESS",
        90,
        _patterns((r"\bno[_ -]?progress\b", r"\bwithout progress\b", r"没有进展", r"无进展")),
    ),
    ErrorClassificationRule(
        "WRITE_FORBIDDEN",
        88,
        _patterns((
            r"\bpermission denied\b",
            r"\boperation not permitted\b",
            r"\baccess denied\b",
            r"\bwrite[_ -]?forbidden\b",
            r"\bforbidden\b",
            r"权限不足",
            r"无权限",
            r"没有权限",
            r"写入.*禁止",
        )),
    ),
    ErrorClassificationRule(
        "TOOL_UNAVAILABLE",
        88,
        _patterns((
            r"\btool unavailable\b",
            r"\bunknown tool\b",
            r"\btool not registered\b",
            r"\bnot found tool\b",
            r"工具不存在",
            r"未注册工具",
            r"工具不可用",
        )),
    ),
    ErrorClassificationRule(
        "RATE_LIMITED",
        87,
        _patterns((r"\brate[_ -]?limit(?:ed)?\b", r"\bhttp 429\b", r"\b429\b", r"速率限制", r"请求过于频繁")),
    ),
    ErrorClassificationRule(
        "QUOTA_EXCEEDED",
        87,
        _patterns((r"\bquota exceeded\b", r"\bquota[_ -]?exceeded\b", r"配额.*(?:耗尽|超限|不足)")),
    ),
    ErrorClassificationRule(
        "MAINTENANCE",
        86,
        _patterns((r"\bmaintenance\b", r"\bmaintenance window\b", r"维护窗口", r"系统维护")),
    ),
    ErrorClassificationRule(
        "MODEL_UPSTREAM_FAILED",
        80,
        _patterns((r"\bprovider\b", r"\bupstream\b", r"\banthropic\b", r"\bmodel\b", r"模型上游", r"模型服务")),
    ),
    ErrorClassificationRule("TOOL_TIMEOUT", 75, _patterns((r"\btimeout\b", r"\btimed out\b", r"超时"))),
    ErrorClassificationRule(
        "ARTIFACT_MISSING",
        70,
        _patterns((
            r"\bartifact[_ -]?(?:missing|not[_ -]?found)\b",
            r"\b(?:missing|not found)\s+(?:required\s+)?artifact\b",
            r"\bartifact\s+(?:missing|not found)\b",
            r"产物.*(?:缺失|不存在)",
        )),
    ),
    ErrorClassificationRule(
        "ACCEPTANCE_FAILED",
        70,
        _patterns((r"\bacceptance\b.{0,40}\b(?:failed|not passed)\b", r"验收.*(?:失败|未通过)")),
    ),
    ErrorClassificationRule(
        "COMPACT_REF_MISSING",
        70,
        _patterns((r"\bcompact\b.{0,40}\b(?:missing|ref)\b", r"压缩.*引用.*(?:缺失|不存在)")),
    ),
    ErrorClassificationRule(
        "TOOL_INVALID_ARGUMENTS",
        65,
        _patterns((
            r"\binvalid argument\b",
            r"\binvalid[_ -]?parameters\b",
            r"\bschema validation\b",
            r"\binvalid schema\b",
            r"\bschema[_ -]?error\b",
            r"参数.*(?:错误|不合法|无效)",
        )),
    ),
    ErrorClassificationRule(
        "PATH_INVALID",
        62,
        _patterns((
            r"\bpath[_ -]?invalid\b",
            r"\binvalid path\b",
            r"\bpath missing\b",
            r"\bmissing path\b",
            r"文件不存在",
            r"路径不存在",
            r"目标不是文件",
        )),
    ),
)

__all__ = ["CLASSIFICATION_RULES", "ErrorClassificationRule", "matched_error_codes"]
