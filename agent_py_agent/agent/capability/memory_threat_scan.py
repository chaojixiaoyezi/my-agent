# LLM: 长期记忆写入前的威胁扫描。对标 长期助手 tools/memory_tool.py:63-80 + threat_patterns.py
#   (写入前按 scope 做注入/外泄扫描),并参考本仓 contracts/gates/skill_guard.py 的 injection/
#   exfiltration 模式族。设计裁决:
#   ① 只扫【注入 + 外泄】两类(prompt injection / 凭证外泄 / 远程取码执行 / 外发 secret),
#      不扫 skill_guard 里的 destructive/execution/persistence 等——那些是对"可执行 skill 文件"
#      的判定,对一句要记的偏好/事实是噪声,且极易误伤(例如记"我负责 crontab 运维"不该被拦)。
#   ② 防误伤中文:这是头号约束。命中条件全部锚定 ASCII 攻击语料(英文注入套话、curl/wget/
#      env/secret 等 shell 与凭证关键字、http(s) 危险 URL),正常中文记忆(纯中文偏好/事实/约定)
#      永不命中——中文里不会出现 "ignore previous instructions" 或 "curl ...| sh"。中文注入句式
#      ("忽略以上指令")只在【同时】伴随英文 exfil/override 关键字时才计,单独中文不拦。
#   ③ 命中即拒绝(remember 是执行用户直接指令,宁可让用户改写也不把攻击载荷落进跨会话记忆),
#      返回 MEMORY_INJECTION_BLOCKED(retryable=False, MANUAL_REVIEW)。
#   改动时同步 tests/test_memory_threat_scan.py 与 capability/memory_tool.py 的调用点。
# 模块用途: 给"写长期记忆"加一道注入/外泄威胁闸,堵住"把恶意指令/凭证外泄载荷写进
#   跨会话记忆,未来会话被检索回来当可信上下文执行"的长效攻击面。
from __future__ import annotations

import re
from dataclasses import dataclass

# 每条: (compiled_pattern, pattern_id, category, description)。
# 全部锚定 ASCII 攻击语料,确保纯中文记忆零命中(防误伤中文是硬约束)。
_MEMORY_THREAT_PATTERNS: tuple[tuple[re.Pattern[str], str, str, str], ...] = (
    # —— prompt injection:英文注入套话(攻击载荷几乎只用英文) ——
    (re.compile(r"ignore\s+(?:\w+\s+){0,3}(?:previous|prior|above|all|the\s+above)\s+(?:instructions?|prompts?|rules?)", re.IGNORECASE),
     "inject_ignore_previous", "injection", "prompt injection: ignore previous instructions"),
    (re.compile(r"disregard\s+(?:\w+\s+){0,3}(?:your|all|any|the)\s+(?:\w+\s+){0,2}(?:instructions?|rules?|guidelines?|prompts?)", re.IGNORECASE),
     "inject_disregard_rules", "injection", "prompt injection: disregard rules"),
    (re.compile(r"(?:system\s+prompt\s+override|override\s+(?:the\s+)?system\s+prompt)", re.IGNORECASE),
     "inject_sys_prompt_override", "injection", "system prompt override"),
    (re.compile(r"(?:reveal|output|print|leak|repeat|show)\s+(?:\w+\s+){0,3}(?:system|initial|developer|hidden)\s+(?:prompt|instructions?|message)", re.IGNORECASE),
     "inject_leak_system_prompt", "injection", "extract system prompt"),
    (re.compile(r"you\s+are\s+now\s+(?:a\s+|an\s+|in\s+)?(?:dan|developer\s+mode|jailbroken|unrestricted|do\s+anything)", re.IGNORECASE),
     "inject_persona_override", "injection", "persona/jailbreak override"),
    (re.compile(r"do\s+not\s+(?:\w+\s+){0,3}tell\s+(?:\w+\s+){0,2}the\s+user", re.IGNORECASE),
     "inject_hide_from_user", "injection", "instruction to hide info from user"),
    # —— exfiltration:凭证/环境变量外泄 + 远程取码执行(shell 语义,ASCII) ——
    (re.compile(r"(?:curl|wget)\s+[^\n]*\|\s*(?:ba)?sh\b", re.IGNORECASE),
     "exfil_curl_pipe_shell", "exfiltration", "curl/wget piped to shell"),
    (re.compile(r"(?:curl|wget)\s+[^\n]*\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.IGNORECASE),
     "exfil_curl_secret_env", "exfiltration", "curl/wget exfiltrating secret env var"),
    (re.compile(r"\bprintenv\b|\benv\s*\|", re.IGNORECASE),
     "exfil_dump_env", "exfiltration", "dumps environment variables"),
    (re.compile(r"\bos\.environ\b", re.IGNORECASE),
     "exfil_os_environ", "exfiltration", "references os.environ for secret access"),
    (re.compile(r"\bcat\s+[^\n]*(?:\.env\b|/\.netrc\b|/\.pgpass\b|/\.aws/credentials|id_rsa\b|/\.ssh/)", re.IGNORECASE),
     "exfil_read_secret_file", "exfiltration", "reads a known secrets/key file"),
    (re.compile(r"(?:send|post|upload|exfiltrate|leak|forward)\s+(?:\w+\s+){0,4}(?:secret|token|api[_-]?key|password|credential|env)\b", re.IGNORECASE),
     "exfil_send_secret", "exfiltration", "instruction to send secrets externally"),
    # 硬编码凭证 / 已泄露 token 直接写进记忆(凭证不该进长期记忆)。
    (re.compile(r"\bghp_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{60,}\b|\bsk-[A-Za-z0-9]{32,}\b|\bAKIA[0-9A-Z]{16}\b"),
     "exfil_hardcoded_token", "exfiltration", "hardcoded credential/token in memory"),
)


@dataclass(frozen=True)
class MemoryThreatFinding:
    pattern_id: str
    category: str
    description: str
    match: str


@dataclass(frozen=True)
class MemoryThreatScanResult:
    safe: bool
    findings: tuple[MemoryThreatFinding, ...] = ()

    @property
    def primary(self) -> MemoryThreatFinding | None:
        return self.findings[0] if self.findings else None

    def reason(self) -> str:
        # 模型/用户可读的拦截理由:列前几条命中类别,不回显完整攻击载荷。
        if not self.findings:
            return ""
        parts = [f"{f.category}:{f.pattern_id}" for f in self.findings[:4]]
        return "记忆写入被拒(命中注入/外泄特征): " + ", ".join(parts)


def scan_memory_content(content: str) -> MemoryThreatScanResult:
    # LLM: 唯一扫描入口。纯函数,永不抛异常。空/纯中文/正常偏好 → safe(zero finding)。
    text = str(content or "")
    if not text.strip():
        return MemoryThreatScanResult(True, ())
    findings: list[MemoryThreatFinding] = []
    seen: set[str] = set()
    for pattern, pid, category, description in _MEMORY_THREAT_PATTERNS:
        match = pattern.search(text)
        if match is None or pid in seen:
            continue
        seen.add(pid)
        snippet = match.group(0).strip()
        if len(snippet) > 80:
            snippet = snippet[:77] + "..."
        findings.append(MemoryThreatFinding(pid, category, description, snippet))
    return MemoryThreatScanResult(not findings, tuple(findings))


__all__ = [
    "MemoryThreatFinding",
    "MemoryThreatScanResult",
    "scan_memory_content",
]
