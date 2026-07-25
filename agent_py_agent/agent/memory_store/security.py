from __future__ import annotations

"""Security scanning shared by durable memory and persona persistence.

This module intentionally lives beside the durable stores instead of one
tool implementation: remember, persona tools, repository reloads, and guarded
file writes must all apply the same rule set.
"""

# LLM: 本模块是 Memory/Persona 持久内容扫描的唯一规则源；禁止在各工具旁复制不同扫描表。
# 模块用途: 长期内容落盘或重新载入前，用同一套安全规则隔离注入和凭据外泄载荷。

import re
from dataclasses import dataclass

_MEMORY_THREAT_PATTERNS: tuple[tuple[re.Pattern[str], str, str, str], ...] = (
    (
        re.compile(
            r"ignore\s+(?:\w+\s+){0,3}(?:previous|prior|above|all|the\s+above)\s+"
            r"(?:instructions?|prompts?|rules?)",
            re.IGNORECASE,
        ),
        "inject_ignore_previous",
        "injection",
        "prompt injection: ignore previous instructions",
    ),
    (
        re.compile(
            r"disregard\s+(?:\w+\s+){0,3}(?:your|all|any|the)\s+"
            r"(?:\w+\s+){0,2}(?:instructions?|rules?|guidelines?|prompts?)",
            re.IGNORECASE,
        ),
        "inject_disregard_rules",
        "injection",
        "prompt injection: disregard rules",
    ),
    (
        re.compile(
            r"(?:system\s+prompt\s+override|override\s+(?:the\s+)?system\s+prompt)",
            re.IGNORECASE,
        ),
        "inject_sys_prompt_override",
        "injection",
        "system prompt override",
    ),
    (
        re.compile(
            r"(?:reveal|output|print|leak|repeat|show)\s+(?:\w+\s+){0,3}"
            r"(?:system|initial|developer|hidden)\s+(?:prompt|instructions?|message)",
            re.IGNORECASE,
        ),
        "inject_leak_system_prompt",
        "injection",
        "extract system prompt",
    ),
    (
        re.compile(
            r"you\s+are\s+now\s+(?:a\s+|an\s+|in\s+)?"
            r"(?:dan|developer\s+mode|jailbroken|unrestricted|do\s+anything)",
            re.IGNORECASE,
        ),
        "inject_persona_override",
        "injection",
        "persona/jailbreak override",
    ),
    (
        re.compile(
            r"do\s+not\s+(?:\w+\s+){0,3}tell\s+(?:\w+\s+){0,2}the\s+user",
            re.IGNORECASE,
        ),
        "inject_hide_from_user",
        "injection",
        "instruction to hide info from user",
    ),
    (
        re.compile(r"(?:curl|wget)\s+[^\n]*\|\s*(?:ba)?sh\b", re.IGNORECASE),
        "exfil_curl_pipe_shell",
        "exfiltration",
        "curl/wget piped to shell",
    ),
    (
        re.compile(
            r"(?:curl|wget)\s+[^\n]*\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)",
            re.IGNORECASE,
        ),
        "exfil_curl_secret_env",
        "exfiltration",
        "curl/wget exfiltrating secret env var",
    ),
    (
        re.compile(r"\bprintenv\b|\benv\s*\|", re.IGNORECASE),
        "exfil_dump_env",
        "exfiltration",
        "dumps environment variables",
    ),
    (
        re.compile(r"\bos\.environ\b", re.IGNORECASE),
        "exfil_os_environ",
        "exfiltration",
        "references os.environ for secret access",
    ),
    (
        re.compile(
            r"\bcat\s+[^\n]*(?:\.env\b|/\.netrc\b|/\.pgpass\b|"
            r"/\.aws/credentials|id_rsa\b|/\.ssh/)",
            re.IGNORECASE,
        ),
        "exfil_read_secret_file",
        "exfiltration",
        "reads a known secrets/key file",
    ),
    (
        re.compile(
            r"(?:send|post|upload|exfiltrate|leak|forward)\s+(?:\w+\s+){0,4}"
            r"(?:secret|token|api[_-]?key|password|credential|env)\b",
            re.IGNORECASE,
        ),
        "exfil_send_secret",
        "exfiltration",
        "instruction to send secrets externally",
    ),
    (
        re.compile(
            r"\bghp_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{60,}\b|"
            r"\bsk-[A-Za-z0-9]{32,}\b|\bAKIA[0-9A-Z]{16}\b"
        ),
        "exfil_hardcoded_token",
        "exfiltration",
        "hardcoded credential/token in memory",
    ),
)


# LLM: finding 只携带有界命中片段，不能把完整敏感载荷带入错误出口。
# 类用途: 表示一条持久内容安全命中，供工具和 repository 生成结构化诊断。
@dataclass(frozen=True)
class MemoryThreatFinding:
    pattern_id: str
    category: str
    description: str
    match: str


# LLM: 扫描结果是纯数据，不拥有写入或权限决定；调用方只按 safe/findings 消费。
# 类用途: 汇总一次安全扫描是否可持久化以及命中的规则。
@dataclass(frozen=True)
class MemoryThreatScanResult:
    safe: bool
    findings: tuple[MemoryThreatFinding, ...] = ()

    @property
    def primary(self) -> MemoryThreatFinding | None:
        return self.findings[0] if self.findings else None

    def reason(self) -> str:
        if not self.findings:
            return ""
        parts = [f"{finding.category}:{finding.pattern_id}" for finding in self.findings[:4]]
        return "记忆写入被拒(命中注入/外泄特征): " + ", ".join(parts)


# LLM: 唯一扫描入口必须保持纯函数和永不抛异常；不能扩展成业务状态或自然语言路由器。
# 函数用途: 检查一段准备长期保存或注入的文字是否包含已知长效注入、外泄载荷。
def scan_memory_content(content: str) -> MemoryThreatScanResult:
    """Scan persisted context without raising or interpreting business state."""

    text = str(content or "")
    if not text.strip():
        return MemoryThreatScanResult(True, ())
    findings: list[MemoryThreatFinding] = []
    seen: set[str] = set()
    for pattern, pattern_id, category, description in _MEMORY_THREAT_PATTERNS:
        match = pattern.search(text)
        if match is None or pattern_id in seen:
            continue
        seen.add(pattern_id)
        snippet = match.group(0).strip()
        if len(snippet) > 80:
            snippet = snippet[:77] + "..."
        findings.append(
            MemoryThreatFinding(pattern_id, category, description, snippet)
        )
    return MemoryThreatScanResult(not findings, tuple(findings))


__all__ = [
    "MemoryThreatFinding",
    "MemoryThreatScanResult",
    "scan_memory_content",
]
