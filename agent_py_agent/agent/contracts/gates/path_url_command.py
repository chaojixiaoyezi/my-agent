# LLM: Path/URL/command gates block unsafe structured tool inputs before execution.
# 模块用途: 对 path/url/command 机器字段做统一边界校验，覆盖 symlink 越界、file URL、私网 URL 和 shell 注入符。

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .models import GateDecision, GateFinding

_PATH_KEYS = {"path", "file_path", "target_path", "output_path", "working_dir", "cwd", "directory"}
_URL_KEYS = {"url", "endpoint", "webhook_url"}
_COMMAND_KEYS = {"command", "cmd", "argv"}
_PRIVATE_HOSTS = {"localhost"}
_SHELL_OPERATOR_MARKERS = ("&&", "||", ";", "|", "`", "$(", "\n", "\r", ">", "<")

# LLM: PathUrlCommandFacts is the trusted input bundle for path, URL, and command gates.
# 类用途: 保存工具 payload、workspace roots 和 gate policy，避免函数参数继续变宽。
@dataclass(frozen=True)
class PathUrlCommandFacts:
    payload: object
    workspace_root: Path
    workspace_roots: list[Path] | None = None
    allowed_private_hosts: Iterable[str] = ()
    allow_shell_operators: bool = False


# LLM: evaluate_path_url_command_gate checks structured path, URL, and command fields without reading prose.
# 函数用途: 在工具入口统一阻断越界路径、私网/file URL 和未声明允许的 shell 操作符。
def evaluate_path_url_command_gate(facts: PathUrlCommandFacts) -> GateDecision:
    data = facts.payload if isinstance(facts.payload, Mapping) else {}
    roots = _normalized_roots(facts.workspace_root, facts.workspace_roots)
    findings: list[GateFinding] = []
    _collect_path_findings(data, roots, findings)
    _collect_url_findings(data, {_normalize_host(item) for item in facts.allowed_private_hosts}, findings)
    _collect_command_findings(data, facts.allow_shell_operators, findings)
    if findings:
        return GateDecision("path_url_command", "DENY", False, tuple(findings), "repair_tool_call", {})
    return GateDecision.allow("path_url_command", evidence={"checked_fields": _checked_field_names(data)})


# LLM: _collect_path_findings validates only known path fields and follows symlinks via Path.resolve.
# 函数用途: 把路径字段归一到 workspace roots 内；解析后出界说明 symlink 或路径越权。
def _collect_path_findings(data: Mapping[object, object], roots: list[Path], findings: list[GateFinding]) -> None:
    allow_missing_escape = str(data.get("tool") or "").strip() in {"read_file", "list_files", "search_text"}
    for key, raw_path in _matching_values(data, _PATH_KEYS):
        finding = _path_finding(key, raw_path, roots, allow_missing_escape=allow_missing_escape)
        if finding:
            findings.append(finding)


# LLM: _collect_url_findings blocks local/private targets by parsing URL host fields.
# 函数用途: 对 url/endpoint/webhook_url 等结构字段校验 scheme 和 host，不发起网络请求。
def _collect_url_findings(
    data: Mapping[object, object],
    allowed_private_hosts: set[str],
    findings: list[GateFinding],
) -> None:
    for key, raw_url in _matching_values(data, _URL_KEYS | {"urls"}):
        finding = _url_finding(key, raw_url, allowed_private_hosts)
        if finding:
            findings.append(finding)


# LLM: _collect_command_findings treats command fields as executable payloads, not explanatory text.
# 函数用途: 阻断 shell 控制符，除非调用方显式声明该入口允许 shell 操作符。
def _collect_command_findings(
    data: Mapping[object, object],
    allow_shell_operators: bool,
    findings: list[GateFinding],
) -> None:
    for key, value in _matching_fields(data, _COMMAND_KEYS):
        finding = _command_finding(str(key), value, allow_shell_operators)
        if finding:
            findings.append(finding)


# LLM: _path_finding checks one path-like field value.
# 函数用途: 把路径解析、symlink 判断和 workspace 边界判断封装成单值校验。
def _path_finding(
    field: str,
    raw_path: object,
    roots: list[Path],
    *,
    allow_missing_escape: bool = False,
) -> GateFinding | None:
    text = _text(raw_path)
    if not text:
        return None
    target = Path(text)
    candidate = target if target.is_absolute() else roots[0] / target
    resolved = _resolve_path(candidate)
    if resolved is None:
        return GateFinding("PATH_RESOLUTION_FAILED", evidence={"field": field})
    if _under_any_root(resolved, roots):
        return None
    if allow_missing_escape and not resolved.exists():
        return None
    code = "PATH_SYMLINK_ESCAPE_BLOCKED" if _under_any_root_lexical(candidate.absolute(), roots) else "PATH_WORKSPACE_ESCAPE_BLOCKED"
    return GateFinding(code, evidence={"field": field, "resolved_path": str(resolved)})


# LLM: _url_finding checks one URL-like field value.
# 函数用途: 拒绝 file URL 和未 allowlist 的本机/私网地址。
def _url_finding(field: str, raw_url: object, allowed_private_hosts: set[str]) -> GateFinding | None:
    parsed = urlparse(_text(raw_url))
    if parsed.scheme == "file":
        return GateFinding("NETWORK_FILE_URL_BLOCKED", evidence={"field": field})
    host = _normalize_host(parsed.hostname)
    if host and host not in allowed_private_hosts and _is_private_host(host):
        return GateFinding("NETWORK_PRIVATE_HOST_BLOCKED", evidence={"field": field, "host": host})
    return None


# LLM: _command_finding checks command fields as executable payloads.
# 函数用途: argv 只允许非空无 NUL 字符，string command 默认拒绝 shell 控制符。
def _command_finding(field: str, value: object, allow_shell_operators: bool) -> GateFinding | None:
    if isinstance(value, list):
        return None if _valid_argv(value) else GateFinding("COMMAND_ARGV_INVALID", evidence={"field": field})
    command = _text(value)
    if not command:
        return None
    if "\x00" in command:
        return GateFinding("COMMAND_ARGV_INVALID", evidence={"field": field})
    if not allow_shell_operators and any(marker in command for marker in _SHELL_OPERATOR_MARKERS):
        return GateFinding("COMMAND_SHELL_OPERATOR_BLOCKED", evidence={"field": field})
    return None


# LLM: _normalized_roots resolves workspace roots once so every field uses the same path policy.
# 函数用途: 生成去重后的 workspace root 列表，缺省时只使用 workspace_root。
def _normalized_roots(workspace_root: Path, workspace_roots: list[Path] | None) -> list[Path]:
    raw = [workspace_root, *(workspace_roots or [])]
    roots: list[Path] = []
    for root in raw:
        resolved = Path(root).resolve(strict=False)
        if resolved not in roots:
            roots.append(resolved)
    return roots or [Path.cwd().resolve(strict=False)]


# LLM: _matching_fields yields only known machine fields from the payload.
# 函数用途: 将字段过滤与各类 gate 逻辑分离，避免 collector 函数嵌套过深。
def _matching_fields(data: Mapping[object, object], names: set[str]) -> list[tuple[object, object]]:
    return [(key, value) for key, value in data.items() if str(key) in names]


# LLM: _matching_values flattens list-valued fields before gate-specific checks.
# 函数用途: 让 path/url collector 保持单层循环，避免边界校验函数继续变厚。
def _matching_values(data: Mapping[object, object], names: set[str]) -> list[tuple[str, object]]:
    return [(str(key), item) for key, value in _matching_fields(data, names) for item in _iter_values(value)]


# LLM: _resolve_path wraps Path.resolve failures as None for gate findings.
# 函数用途: 路径解析失败时不抛异常，交给调用方转成结构化 finding。
def _resolve_path(path: Path) -> Path | None:
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


# LLM: _valid_argv validates argv-style command fields without shell parsing.
# 函数用途: 确保数组命令每一段都是非空且不含 NUL 字符。
def _valid_argv(value: list[object]) -> bool:
    return all(_text(item) and "\x00" not in _text(item) for item in value)


# LLM: _is_private_host handles localhost, IPv4 shorthand, IPv6, private, loopback, and link-local hosts.
# 函数用途: 本地判断 host 是否指向内网或本机地址，不依赖 DNS 解析。
def _is_private_host(host: str) -> bool:
    normalized = _normalize_host(host)
    if normalized in _PRIVATE_HOSTS:
        return True
    numeric = _numeric_ipv4_host(normalized)
    if numeric:
        normalized = numeric
    shorthand = _shorthand_ipv4_host(normalized)
    if shorthand:
        normalized = shorthand
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified


# LLM: _numeric_ipv4_host converts decimal IPv4 host notation into dotted quad for policy checks.
# 函数用途: 覆盖 2130706433 这类 localhost 绕过写法。
def _numeric_ipv4_host(host: str) -> str:
    if not host.isdigit():
        return ""
    try:
        value = int(host)
    except ValueError:
        return ""
    if not 0 <= value <= 0xFFFFFFFF:
        return ""
    return ".".join(str((value >> shift) & 0xFF) for shift in (24, 16, 8, 0))


# LLM: _shorthand_ipv4_host handles inet_aton-style dotted shorthand such as 127.1.
# 函数用途: 将 1-3 段 IPv4 简写归一为四段，防止 localhost 私网绕过。
def _shorthand_ipv4_host(host: str) -> str:
    parts = host.split(".")
    if not 1 < len(parts) < 4 or not all(part.isdigit() for part in parts):
        return ""
    nums = [int(part) for part in parts]
    if any(num < 0 or num > 255 for num in nums):
        return ""
    if len(nums) == 2:
        nums = [nums[0], 0, 0, nums[1]]
    elif len(nums) == 3:
        nums = [nums[0], nums[1], 0, nums[2]]
    return ".".join(str(num) for num in nums)


# LLM: _checked_field_names returns machine field names only for compact gate evidence.
# 函数用途: 给通过结果记录检查过的字段名，避免把参数正文塞进审计。
def _checked_field_names(data: Mapping[object, object]) -> list[str]:
    keys = _PATH_KEYS | _URL_KEYS | _COMMAND_KEYS | {"urls"}
    return sorted(str(key) for key in data.keys() if str(key) in keys)


# LLM: _iter_values normalizes scalar/list payload fields.
# 函数用途: 让 path/url gate 统一遍历单值和数组值字段。
def _iter_values(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else [value]


# LLM: _text converts structured scalar fields into trimmed strings.
# 函数用途: 只做字段值归一化，不扫描普通说明文本。
def _text(value: object) -> str:
    return str(value or "").strip()


# LLM: _normalize_host canonicalizes host text for private-address checks.
# 函数用途: 统一大小写和 IPv6 方括号，供 URL gate 判断 allowlist。
def _normalize_host(value: object) -> str:
    return _text(value).lower().strip("[]")


# LLM: _under_any_root checks resolved paths against allowed roots.
# 函数用途: 判断路径是否在任一授权 workspace/root 内。
def _under_any_root(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            path.resolve(strict=False).relative_to(root)
            return True
        except ValueError:
            continue
    return False


# LLM: _under_any_root_lexical detects symlink escapes from an otherwise allowed lexical path.
# 函数用途: 区分普通 workspace 外路径和“字面在 root 下但 resolve 后逃逸”的 symlink。
def _under_any_root_lexical(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


__all__ = ["PathUrlCommandFacts", "evaluate_path_url_command_gate"]
