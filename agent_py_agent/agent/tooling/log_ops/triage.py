
from __future__ import annotations

"""确定性初筛器(triage)—— 规则库(关键词/正则)+ 候选告警结构。

确定性、零 LLM:对每条采集到的原始日志行跑一遍规则库,命中即产候选告警。规则库是
安全监控常见可疑模式(注入/暴力破解/提权/反弹 shell/数据外泄/敏感文件读取/ALERT 标记等)。
命中可叠加(一行可命中多条),候选告警带:原始行全文 + 命中规则名列表 + 源标识 + 行号 +
时间戳 + 唯一指纹(便于 LLM 层去重/校验"一条不丢")。

注意:这是"初筛"不是"研判"。宁可多报(召回优先)交给 LLM 层精判,也不要确定性层漏放。
"""

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol


class SourceRefLike(Protocol):
    """triage 只需要源的三元身份(source_id/kind/locator)。SourceSpec 直接满足,测试也可用任意带这三个属性的对象。"""

    source_id: str
    kind: str
    locator: str


# 规则库:(规则名, 严重度, 编译后的正则)。大小写不敏感。
# 规则名稳定(候选告警里会带),改规则别改名,改名会影响下游去重/统计语义。
_RULE_DEFS: list[tuple[str, str, str]] = [
    # 显式 ALERT 标记(模拟器/上游已标注的告警行)。
    ("alert_marker", "high", r"\bALERT\b"),
    # 注入类。
    ("sql_injection", "high", r"(?:union\s+select|or\s+1\s*=\s*1|';\s*drop\s+table|sleep\s*\(\s*\d+\s*\)|xp_cmdshell)"),
    ("injection_generic", "high", r"\binjection\b"),
    ("command_injection", "high", r"(?:;\s*(?:cat|rm|wget|curl|nc)\b|\$\(|`[^`]+`|\|\s*(?:sh|bash)\b)"),
    ("xss", "medium", r"(?:<script\b|javascript:|onerror\s*=)"),
    ("path_traversal", "medium", r"(?:\.\./){2,}"),
    # 认证/暴力破解。
    ("brute_force", "high", r"brute[\s_-]*force"),
    ("auth_failure_burst", "medium", r"(?:failed\s+password|authentication\s+failure|invalid\s+user|login\s+failed)"),
    # 提权 / 横向。
    ("privilege_escalation", "high", r"(?:privilege\s+escalation|sudo\s+su\b|setuid|chmod\s+\+s\b|gtfobins)"),
    ("reverse_shell", "high", r"(?:reverse\s+shell|/dev/tcp/|bash\s+-i\b|nc\s+-e\b|mkfifo\b.*\|\s*(?:sh|bash))"),
    # 数据外泄 / 敏感文件。
    ("exfiltration", "high", r"(?:exfiltrat|data\s+exfil|curl\s+-T\b|scp\s+.*@)"),
    ("sensitive_file_read", "high", r"(?:/etc/shadow|/etc/passwd|\.ssh/id_rsa|/root/\.ssh)"),
    ("malware_drop", "high", r"(?:wget\s+http|curl\s+-o\b).*(?:\.sh|\.bin|\.elf)\b"),
    # 网络扫描 / 探测。
    ("port_scan", "medium", r"(?:nmap\b|masscan\b|port\s*scan)"),
]


@dataclass(frozen=True)
class TriageRule:
    name: str
    severity: str
    pattern: re.Pattern[str]


def _compile_rules() -> list[TriageRule]:
    rules: list[TriageRule] = []
    for name, severity, pattern in _RULE_DEFS:
        rules.append(TriageRule(name=name, severity=severity, pattern=re.compile(pattern, re.IGNORECASE)))
    return rules


# 进程内编译一次复用(daemon 长跑,别每行重编译)。
DEFAULT_RULES: list[TriageRule] = _compile_rules()

# 正则安全(防 LLM 生成的 per-源规则 ReDoS / 坏正则把 daemon 卡死或崩):
_MAX_PATTERN_LEN = 500
_MATCH_INPUT_CAP = 4096  # 匹配前把超长记录截断,大幅降低灾难性回溯风险(日志行通常远短于此)
# 嵌套量词(易 ReDoS):(x+)+ (x*)* (x{n,})+ 等,命中即拒绝该规则。
_REDOS_RISK_RE = re.compile(r"\([^)]*[+*][^)]*\)[+*?]|\([^)]*\{\d+,\}[^)]*\)[+*?]")


def compile_profile_rules(rule_defs: list[dict[str, Any]]) -> list[TriageRule]:
    """把 profile 里 LLM 写的规则 dict 编译成 TriageRule,带正则安全校验。

    跳过(不崩 daemon)的情形:缺 name/pattern、pattern 超长、含 ReDoS 风险嵌套量词、正则非法。
    pattern 对整条记录(可能多行)做大小写不敏感匹配。severity 缺省 medium。
    """
    rules: list[TriageRule] = []
    for item in rule_defs or []:
        rule = _compile_one_profile_rule(item)
        if rule is not None:
            rules.append(rule)
    return rules


def _compile_one_profile_rule(item: Any) -> TriageRule | None:
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or "").strip()
    pattern = str(item.get("pattern") or "")
    if not name or not pattern or len(pattern) > _MAX_PATTERN_LEN:
        return None
    if _REDOS_RISK_RE.search(pattern):
        return None  # 危险量词嵌套,拒绝(防 ReDoS 灾难性回溯)
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error:
        return None
    severity = str(item.get("severity") or "medium").strip() or "medium"
    return TriageRule(name=name, severity=severity, pattern=compiled)


# ALERT-NNNNNN 形式的唯一告警 ID(模拟器格式),抽出来放进候选,便于"一条不丢"校验。
_ALERT_ID_RE = re.compile(r"\bALERT-\d{4,}\b")

_SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1, "": 0}


def line_fingerprint(source_id: str, line_no: int, raw_line: str) -> str:
    """候选告警唯一指纹:源 + 行号 + 行内容哈希。同一行重复采集会得同一指纹(便于去重校验)。"""
    digest = hashlib.sha1(f"{source_id}|{line_no}|{raw_line}".encode()).hexdigest()[:16]
    return f"cand-{digest}"


def extract_alert_id(raw_line: str) -> str:
    match = _ALERT_ID_RE.search(raw_line)
    return match.group(0) if match else ""


def match_rules(raw_line: str, rules: list[TriageRule] | None = None) -> list[TriageRule]:
    """返回该行命中的所有规则(可叠加)。无命中返回空列表。超长记录先截断,降 ReDoS 风险。"""
    active = rules if rules is not None else DEFAULT_RULES
    capped = raw_line[:_MATCH_INPUT_CAP]
    return [rule for rule in active if rule.pattern.search(capped)]


def triage_line(
    source: SourceRefLike,
    *,
    line_no: int,
    raw_line: str,
    rules: list[TriageRule] | None = None,
) -> dict[str, Any] | None:
    """对单行初筛:命中任一规则就产候选告警 dict,否则返回 None。

    候选结构(下游 log_alert_poll 原样给 LLM 研判):
      fingerprint    唯一指纹(去重/不丢校验)
      source_id/source_kind/source_locator  源标识(交叉验证用)
      line_no        在该源存档里的行号(1-based,可回查)
      raw_line       原始日志行全文(研判证据,不截断)
      matched_rules  命中的规则名列表
      severity       命中规则里的最高严重度
      alert_id       若行内有 ALERT-NNNNNN 则带上(便于一条不丢校验)
      timestamp      行内 ISO 时间(若能抽到)
      detected_at    被初筛器处理的 epoch 秒
    """
    stripped = raw_line.rstrip("\n")
    if not stripped.strip():
        return None
    matched = match_rules(stripped, rules)
    if not matched:
        return None
    severity = max((rule.severity for rule in matched), key=lambda sev: _SEVERITY_RANK.get(sev, 0))
    return {
        "fingerprint": line_fingerprint(source.source_id, line_no, stripped),
        "source_id": source.source_id,
        "source_kind": source.kind,
        "source_locator": source.locator,
        "line_no": line_no,
        "raw_line": stripped,
        "matched_rules": [rule.name for rule in matched],
        "severity": severity,
        "alert_id": extract_alert_id(stripped),
        "timestamp": _extract_iso_timestamp(stripped),
        "detected_at": time.time(),
    }


_ISO_TS_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?)")


def _extract_iso_timestamp(raw_line: str) -> str:
    match = _ISO_TS_RE.match(raw_line)
    return match.group(1) if match else ""


def rule_catalog() -> list[dict[str, str]]:
    """对外暴露规则库目录(规则名/严重度/正则源串),供工具/文档展示。"""
    return [{"name": name, "severity": severity, "pattern": pattern} for name, severity, pattern in _RULE_DEFS]


__all__ = [
    "DEFAULT_RULES",
    "TriageRule",
    "extract_alert_id",
    "line_fingerprint",
    "match_rules",
    "rule_catalog",
    "triage_line",
]
