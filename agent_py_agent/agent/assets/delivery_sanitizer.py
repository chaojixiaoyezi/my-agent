# LLM: 必须在任何descriptor.clean_json前检查原字节；与Skill迁移共用redact_sensitive_text，不继承其8MiB迁移预算。
# 模块用途: 硬拒绝投递秘密或显式替换为占位符，移除发送方主机路径且无损处理二进制。
from __future__ import annotations

import os
import re
from bisect import bisect_right
from pathlib import Path

from ..common.log_redaction import redact_sensitive_text
from .delivery_contracts import AssetDeliveryError

_PATTERNS = (
    ('openai_key', re.compile(r'sk-[A-Za-z0-9_-]{20,}')),
    ('aws_access_key', re.compile(r'AKIA[0-9A-Z]{16}')),
    ('github_token', re.compile(r'ghp_[A-Za-z0-9]{20,}')),
    ('slack_token', re.compile(r'xox[baprs]-[A-Za-z0-9-]{10,}')),
    ('jwt', re.compile(r'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}')),
    ('secret_assignment', re.compile(
        r'''(?i)(?:password|secret|api_key|token|bearer)["']?\s*[:=]\s*["'](?P<literal>[^"'\s]{8,})["']''')),
)
_PLACEHOLDER = re.compile(r'\$\{[^}\n]+\}|<[^>\n]+>|\bYOUR_[A-Za-z0-9_]+|\bexample[A-Za-z0-9_]*|\*{3,}', re.I)


# LLM: 只识别声明的模板占位形状，不据内容猜凭据有效性或访问任何秘密来源。
# 函数用途: 避免把待配置模板和星号掩码当成真实秘密。
def _placeholder(value):
    return _PLACEHOLDER.fullmatch(value) is not None


# LLM: 环境值仅本次内存匹配，不回传变量名或原值，不持久化环境。
# 函数用途: 取得需要额外拦截的非平凡环境秘密。
def _environment_secrets():
    return tuple({value for key, value in os.environ.items() if re.search(r'KEY|TOKEN|SECRET|PASSWORD', key, re.I)
        and len(value) >= 8 and value.strip() and not _placeholder(value)})


# LLM: 位置始终属于原文本，literal只替换引号中的值；环境按精确原值匹配，所有诊断在外层屏蔽。
# 函数用途: 收集要求的密钥形状和环境秘密位置。
def _matches(text, secrets):
    matches = []
    for name, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            group = 'literal' if 'literal' in match.re.groupindex else 0
            if not _placeholder(match.group(group)):
                matches.append((*match.span(group), name))
    for secret in secrets:
        matches.extend((*match.span(), 'environment_secret') for match in re.finditer(re.escape(secret), text))
    return matches


# LLM: 合并重叠区间后一次重建文本，不把密钥或上下文写日志；占位符保持原引用处的引号。
# 函数用途: 将命中的原字面值替换成统一待配置模板。
def _replace_matches(text, matches):
    pieces, cursor = [], 0
    for start, end, _ in sorted(matches):
        if end <= cursor:
            continue
        if start >= cursor:
            pieces.extend((text[cursor:start], '${REDACTED_SECRET}'))
        cursor = max(cursor, end)
    return ''.join((*pieces, text[cursor:]))


# LLM: 模板先遮成短非秘密标记，扫描后恢复；复用Skill迁移同一个公共秘密检查器，不能用clean_json先遮原秘密。
# 函数用途: 兼容模板占位符地调用既有秘密扫描和脱敏实现。
def _shared_scan(text):
    placeholders = {}

    # LLM: 标记只存内存且短于秘密阈值；恢复按完整标记替换，不修改原占位正文。
    # 函数用途: 临时保护调用者已有的配置模板。
    def shield(match):
        marker = f'~{len(placeholders)}~'
        placeholders[marker] = match.group()
        return marker

    protected = _PLACEHOLDER.sub(shield, text)
    scanned = redact_sensitive_text(protected, code_file=True)
    changed = scanned != protected
    scanned = re.sub(r'<redacted[^>]*>|\[redacted\]', '${REDACTED_SECRET}', scanned, flags=re.I)
    for marker, original in placeholders.items():
        scanned = scanned.replace(marker, original)
    return changed, scanned


# LLM: 指定Owner前缀先替换，再移除其他系统账号目录；只替换home前缀，保留资源相对后缀以便接收方重绑定。
# 函数用途: 将本机绝对家目录改成可配置的OWNER_HOME模板。
def _paths(text, owner_home):
    home = str(Path(owner_home).absolute())
    text = text.replace(home, '${OWNER_HOME}')
    return re.sub(r'/(?:Users/[^/\s"\'<>]+|home/[^/\s"\'<>]+|root)(?=/|\b)', '${OWNER_HOME}', text)


# LLM: 所有文件先按原字节扫描才返回结果，默认命中即拒绝且无IO；允许替换也不回显原秘密，surrogateescape保持非UTF8原字节。
# 函数用途: 在任何暂存或发送前完成整包秘密检查、可选占位替换和宿主路径清理。
def sanitize_bundle_files(files, *, owner_home, allow_secret_placeholder_replace=False):
    if type(allow_secret_placeholder_replace) is not bool:
        raise ValueError('allow_secret_placeholder_replace必须是布尔值')
    secrets, result = _environment_secrets(), {}
    for name, raw in files.items():
        if not isinstance(name, str) or not isinstance(raw, bytes):
            raise ValueError('投递文件必须是相对文件名到原始bytes的映射')
        text = raw.decode('utf-8', errors='surrogateescape')
        matches = _matches(text, secrets)
        shared, _ = _shared_scan(text)
        if (matches or shared) and not allow_secret_placeholder_replace:
            newlines = [index for index, char in enumerate(text) if char == '\n']
            safe_name = _replace_matches(_paths(name, owner_home), _matches(_paths(name, owner_home), secrets))
            safe_name = redact_sensitive_text(safe_name, code_file=True)
            diagnostics = [{'file_path': safe_name, 'line_number': bisect_right(newlines, start) + 1,
                'pattern_name': pattern, 'masked_snippet': '<redacted>'} for start, _, pattern in matches[:100]]
            if not diagnostics:
                diagnostics = [{'file_path': safe_name, 'line_number': 1, 'pattern_name': 'shared_secret',
                    'masked_snippet': '<redacted>'}]
            raise AssetDeliveryError('ASSET_DELIVERY_SECRET_DETECTED', '投递文件包含疑似秘密', redacted_matches=diagnostics)
        text = _replace_matches(text, matches)
        if shared:
            _, text = _shared_scan(text)
        result[name] = _paths(text, owner_home).encode('utf-8', errors='surrogateescape')
    return result
