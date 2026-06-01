# LLM: Skill guard scans skill directories for security threats before install/promotion.
# 模块用途: 参考 长期助手 skills_guard.py，在 skill 安装/启用前检查危险 import、shell 调用、网络访问、注入模式等。

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...settings.defaults import default_agent_config
from .models import GateDecision, GateFinding

# Trust levels sorted by privilege desc
TRUST_LEVELS = ("system", "manual", "agent_generated", "external")

INSTALL_POLICY: dict[str, dict[str, str]] = {
    #               safe       caution    dangerous
    "system":         {"safe": "allow", "caution": "allow", "dangerous": "allow"},
    "manual":         {"safe": "allow", "caution": "allow", "dangerous": "block"},
    "agent_generated": {"safe": "allow", "caution": "block", "dangerous": "block"},
    "external":       {"safe": "allow", "caution": "block", "dangerous": "block"},
}

_SKILL_THREAT_PATTERNS: list[tuple[str, str, str, str, str]] = [
    (r"curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD)", "env_exfil_curl", "critical", "exfiltration", "curl with secret env var"),
    (r"wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD)", "env_exfil_wget", "critical", "exfiltration", "wget with secret env var"),
    (r"os\.environ\b", "python_os_environ", "high", "exfiltration", "accesses os.environ"),
    (r"printenv|env\s*\|", "dump_all_env", "high", "exfiltration", "dumps all environment variables"),
    (r"cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass)", "read_secrets_file", "critical", "exfiltration", "reads known secrets file"),
    (r"ignore\s+(?:\w+\s+)*(previous|all|above|prior)\s+instructions", "prompt_injection_ignore", "critical", "injection", "prompt injection: ignore instructions"),
    (r"do\s+not\s+(?:\w+\s+)*tell\s+(?:\w+\s+)*the\s+user", "deception_hide", "critical", "injection", "hide information from user"),
    (r"system\s+prompt\s+override", "sys_prompt_override", "critical", "injection", "system prompt override"),
    (r"disregard\s+(?:\w+\s+)*(your|all|any)\s+(?:\w+\s+)*(instructions|rules|guidelines)", "disregard_rules", "critical", "injection", "disregard rules"),
    (r"output\s+(?:\w+\s+)*(system|initial)\s+prompt", "leak_system_prompt", "high", "injection", "extract system prompt"),
    (r"rm\s+-rf\s+/", "destructive_root_rm", "critical", "destructive", "recursive delete from root"),
    (r"rm\s+-rf\s+[^/\n]*\$HOME", "destructive_home_rm", "critical", "destructive", "recursive delete targeting home"),
    (r"chmod\s+777", "insecure_perms", "medium", "destructive", "world-writable permissions"),
    (r"\bmkfs\b", "format_filesystem", "critical", "destructive", "formats a filesystem"),
    (r"\bdd\s+.*if=.*of=/dev/", "disk_overwrite", "critical", "destructive", "raw disk write"),
    (r"shutil\.rmtree\s*\(\s*[\"\'/]", "python_rmtree", "high", "destructive", "Python rmtree on absolute path"),
    (r"\bcrontab\b", "persistence_cron", "medium", "persistence", "modifies cron jobs"),
    (r"authorized_keys", "ssh_backdoor", "critical", "persistence", "modifies SSH authorized keys"),
    (r"/etc/sudoers|visudo", "sudoers_mod", "critical", "persistence", "modifies sudoers"),
    (r"subprocess\.(run|call|Popen|check_output)\s*\(", "python_subprocess", "medium", "execution", "Python subprocess execution"),
    (r"os\.system\s*\(", "python_os_system", "high", "execution", "os.system() unguarded"),
    (r"os\.popen\s*\(", "python_os_popen", "high", "execution", "os.popen() shell pipe"),
    (r"\beval\s*\(\s*[\"']", "eval_string", "high", "obfuscation", "eval() with string"),
    (r"\bexec\s*\(\s*[\"']", "exec_string", "high", "obfuscation", "exec() with string"),
    (r"echo\s+[^\n]*\|\s*(bash|sh|python|perl|ruby|node)", "echo_pipe_exec", "critical", "obfuscation", "echo piped to interpreter"),
    (r"curl\s+[^\n]*\|\s*(ba)?sh", "curl_pipe_shell", "critical", "supply_chain", "curl piped to shell"),
    (r"wget\s+[^\n]*-O\s*-\s*\|\s*(ba)?sh", "wget_pipe_shell", "critical", "supply_chain", "wget piped to shell"),
    (r"\bsudo\b", "sudo_usage", "high", "privilege_escalation", "uses sudo"),
    (r"AGENTS\.md|CLAUDE\.md|\.cursorrules", "agent_config_mod", "critical", "persistence", "references agent config files"),
    (r"(api[_-]?key|token|secret|password)\s*[=:]\s*[\"'][A-Za-z0-9+/=_-]{20,}", "hardcoded_secret", "critical", "credential_exposure", "possible hardcoded secret"),
    (r"ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{80,}", "github_token_leaked", "critical", "credential_exposure", "GitHub token in skill"),
    (r"\bnc\s+-[lp]|ncat\s+-[lp]|\bsocat\b", "reverse_shell", "critical", "network", "reverse shell listener"),
    (r"\.\./\.\./\.\.", "path_traversal_deep", "high", "traversal", "deep relative path traversal"),
]

_SKILL_SCAN_EXTENSIONS = frozenset({
    ".md", ".txt", ".py", ".sh", ".bash", ".js", ".ts", ".rb",
    ".yaml", ".yml", ".json", ".toml", ".cfg", ".ini", ".conf",
    ".html", ".css", ".xml",
})

_SUSPICIOUS_EXTENSIONS = frozenset({
    ".exe", ".dll", ".so", ".dylib", ".bin", ".com", ".msi", ".dmg",
})

# LLM: SkillGuardFinding records one detected threat in a skill file.
# 类用途: 承载模式ID、严重级别、类别、文件路径、行号、匹配内容和描述。
@dataclass(frozen=True)
class SkillGuardFinding:
    pattern_id: str
    severity: str
    category: str
    file: str
    line: int
    match: str
    description: str


# LLM: SkillScanResult bundles all findings and verdict from a skill scan.
# 类用途: 封装技能名、来源、信任级别、裁决和威胁发现列表。
@dataclass(frozen=True)
class SkillScanResult:
    skill_name: str
    source: str
    trust_level: str
    verdict: str
    findings: tuple[SkillGuardFinding, ...] = ()
    summary: str = ""

    # LLM: has_critical 快速判断是否存在 critical 级别威胁。
    # 函数用途: 供 install_decision 等调用方快速判断是否需要阻断。
    @property
    def has_critical(self) -> bool:
        return any(f.severity == "critical" for f in self.findings)

    # LLM: has_high 快速判断是否存在 critical 或 high 级别威胁。
    # 函数用途: 供 install_decision 等调用方快速判断风险等级。
    @property
    def has_high(self) -> bool:
        return any(f.severity in ("critical", "high") for f in self.findings)


# LLM: SkillGuardRequest bundles install-time policy knobs for one skill scan.
# 类用途: 保存 skill 来源、显示名、force 开关和配置对象，避免 gate 入口散参数膨胀。
@dataclass(frozen=True)
class SkillGuardRequest:
    source: str = "external"
    skill_name: str = ""
    force: bool = False
    config: object | None = None


# LLM: evaluate_skill_guard_gate scans a skill directory and returns structured allow/deny.
# 函数用途: 在 skill install/promote 前调用，扫描目录结构和文件内容中的威胁模式。
def evaluate_skill_guard_gate(
    skill_path: Path,
    request: SkillGuardRequest | None = None,
) -> GateDecision:
    guard_request = request or SkillGuardRequest()
    result = scan_skill(
        skill_path,
        source=guard_request.source,
        skill_name=guard_request.skill_name,
        config=guard_request.config,
    )
    allowed, reason = install_decision(result, force=guard_request.force)
    gate_findings: list[GateFinding] = []
    for f in result.findings:
        gate_findings.append(GateFinding(
            code=f"SKILL_{f.pattern_id.upper()}",
            severity="P0" if f.severity == "critical" else "P1" if f.severity == "high" else "P2",
            message=f"{f.file}:{f.line}: {f.description}",
            evidence={"pattern_id": f.pattern_id, "severity": f.severity, "category": f.category, "file": f.file, "line": f.line, "match": f.match[:120]},
        ))
    evidence = {"skill_name": result.skill_name, "source": result.source, "trust_level": result.trust_level, "verdict": result.verdict, "finding_count": len(result.findings), "reason": reason}
    if allowed:
        status = "ALLOW_WITH_FINDINGS" if gate_findings else "ALLOW"
        return GateDecision("skill_guard", status, True, tuple(gate_findings), evidence=evidence)
    return GateDecision("skill_guard", "DENY", False, tuple(gate_findings), "needs_approval", evidence=evidence)


# LLM: scan_skill walks skill directory files and runs pattern and structure checks.
# 函数用途: 递归扫描 skill 目录的所有文本文件，收集威胁发现。
def scan_skill(
    skill_path: Path,
    source: str = "external",
    skill_name: str = "",
    *,
    config: object | None = None,
) -> SkillScanResult:
    if not skill_name:
        skill_name = skill_path.name
    trust_level = _resolve_trust_level(source)
    all_findings: list[SkillGuardFinding] = []

    limits = _skill_guard_limits(config)
    if skill_path.is_dir():
        all_findings.extend(_check_skill_structure(skill_path, limits))
        files = [f for f in sorted(skill_path.rglob("*")) if f.is_file()]
        for f in files:
            rel = str(f.relative_to(skill_path))
            all_findings.extend(_scan_skill_file(f, rel))
    elif skill_path.is_file():
        all_findings.extend(_scan_skill_file(skill_path, skill_path.name))

    verdict = _determine_verdict(all_findings)
    return SkillScanResult(
        skill_name=skill_name,
        source=source,
        trust_level=trust_level,
        verdict=verdict,
        findings=tuple(all_findings),
        summary=_build_summary(skill_name, trust_level, verdict, all_findings),
    )


# LLM: install_decision maps scan result and trust level to allow/block decision.
# 函数用途: 根据信任级别和扫描裁决决定是否允许安装。
def install_decision(result: SkillScanResult, force: bool = False) -> tuple[bool, str]:
    policy = INSTALL_POLICY.get(result.trust_level, INSTALL_POLICY["external"])
    decision = policy.get(result.verdict, "block")
    if decision == "allow":
        return True, f"allowed ({result.trust_level} source, {result.verdict} verdict)"
    if force:
        return True, f"force-installed ({len(result.findings)} findings)"
    if decision == "ask":
        return False, f"requires confirmation ({result.trust_level} source, {result.verdict} verdict)"
    return False, f"blocked ({result.trust_level} source, {result.verdict} verdict, {len(result.findings)} findings)"


# LLM: _resolve_trust_level maps source string to canonical trust level.
# 函数用途: 将来源标识(system/manual/agent_generated/external)标准化为信任级别。
def _resolve_trust_level(source: str) -> str:
    if source in ("system", "builtin"):
        return "system"
    if source in ("manual", "user"):
        return "manual"
    if source in ("agent_generated", "agent-created", "auto"):
        return "agent_generated"
    return "external"


# LLM: _SkillGuardLimits carries resolved directory scan budgets for one skill guard run.
# 类用途: 保存技能目录最大文件数和总大小上限，0 表示关闭对应预算判断。
@dataclass(frozen=True)
class _SkillGuardLimits:
    max_files: int
    max_size_kb: int


# LLM: _skill_guard_limits resolves skill guard budgets from AgentConfig.
# 函数用途: 从配置对象读取 skill guard 扫描预算；未传配置时只回退到 schema 默认。
def _skill_guard_limits(config: object | None) -> _SkillGuardLimits:
    if config is None:
        config = default_agent_config()
    return _SkillGuardLimits(
        max_files=_config_int(config, "skill_guard_max_files"),
        max_size_kb=_config_int(config, "skill_guard_max_size_kb"),
    )


# LLM: _config_int normalizes one skill guard integer budget.
# 函数用途: 读取单个配置字段并归一为非负整数，非法值按 0 处理。
def _config_int(config: object, key: str) -> int:
    try:
        return max(0, int(getattr(config, key)))
    except (TypeError, ValueError):
        return 0


# LLM: _check_skill_structure checks directory-level issues: symlinks, binaries, file count, size.
# 函数用途: 扫描技能目录结构，检测符号链接、二进制文件、文件数超限和总大小超限。
def _check_skill_structure(skill_dir: Path, limits: _SkillGuardLimits) -> list[SkillGuardFinding]:
    findings: list[SkillGuardFinding] = []
    file_count = 0
    total_size = 0
    for f in skill_dir.rglob("*"):
        if not f.is_file() and not f.is_symlink():
            continue
        rel = str(f.relative_to(skill_dir))
        file_count += 1
        if f.is_symlink():
            findings.append(SkillGuardFinding(
                "symlink_detected", "high", "structural", rel, 0,
                f"symlink -> {f.resolve()}", "symlink in skill directory",
            ))
            continue
        try:
            size = f.stat().st_size
            total_size += size
        except OSError:
            continue
        ext = f.suffix.lower()
        if ext in _SUSPICIOUS_EXTENSIONS:
            findings.append(SkillGuardFinding(
                "binary_file", "critical", "structural", rel, 0,
                f"binary: {ext}", f"binary/executable file ({ext}) in skill",
            ))
    if limits.max_files > 0 and file_count > limits.max_files:
        findings.append(SkillGuardFinding(
            "too_many_files", "medium", "structural", "(directory)", 0,
            f"{file_count} files", f"skill has {file_count} files (limit: {limits.max_files})",
        ))
    if limits.max_size_kb > 0 and total_size > limits.max_size_kb * 1024:
        findings.append(SkillGuardFinding(
            "oversized_skill", "high", "structural", "(directory)", 0,
            f"{total_size // 1024}KB", f"skill total {total_size // 1024}KB (limit: {limits.max_size_kb}KB)",
        ))
    return findings


# LLM: _scan_skill_file scans a single text file against threat pattern registry.
# 函数用途: 对单个文件逐行匹配 32 个威胁正则，记录匹配到的所有威胁发现。
def _scan_skill_file(file_path: Path, rel_path: str) -> list[SkillGuardFinding]:
    if file_path.suffix.lower() not in _SKILL_SCAN_EXTENSIONS and file_path.name != "SKILL.md":
        return []
    try:
        content = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    findings: list[SkillGuardFinding] = []
    lines = content.split("\n")
    for pattern, pid, severity, category, description in _SKILL_THREAT_PATTERNS:
        findings.extend(_match_pattern_in_lines((pattern, pid, severity, category, description), lines, rel_path))
    return findings


# LLM: _match_pattern_in_lines scans all lines for one threat pattern, deduping by line number.
# 函数用途: 对单个正则模式逐行匹配，跳过已记录的行，返回该模式的所有 SkillGuardFinding。
def _match_pattern_in_lines(
    pattern_info: tuple[str, str, str, str, str],
    lines: list[str], rel_path: str,
) -> list[SkillGuardFinding]:
    pattern, pid, severity, category, description = pattern_info
    findings: list[SkillGuardFinding] = []
    seen: set[int] = set()
    for i, line in enumerate(lines, start=1):
        if i in seen or not re.search(pattern, line, re.IGNORECASE):
            continue
        seen.add(i)
        matched = line.strip()
        if len(matched) > 120:
            matched = matched[:117] + "..."
        findings.append(SkillGuardFinding(pid, severity, category, rel_path, i, matched, description))
    return findings


# LLM: _determine_verdict maps findings to safe/caution/dangerous verdict.
# 函数用途: 根据威胁发现的最高严重级别决定扫描裁决。
def _determine_verdict(findings: list[SkillGuardFinding]) -> str:
    if not findings:
        return "safe"
    if any(f.severity == "critical" for f in findings):
        return "dangerous"
    if any(f.severity == "high" for f in findings):
        return "caution"
    return "caution"


# LLM: _build_summary produces a human-readable one-line scan summary.
# 函数用途: 拼接技能名、裁决、发现数和涉及类别，供日志和报告使用。
def _build_summary(name: str, trust: str, verdict: str, findings: list[SkillGuardFinding]) -> str:
    if not findings:
        return f"{name}: clean scan"
    categories = sorted({f.category for f in findings})
    return f"{name}: {verdict} — {len(findings)} finding(s) in {', '.join(categories)}"


__all__ = [
    "SkillGuardFinding",
    "SkillScanResult",
    "evaluate_skill_guard_gate",
    "install_decision",
    "scan_skill",
]
