# LLM: 必须在任何descriptor.clean_json前检查原字节；与Skill迁移共用redact_sensitive_text，不继承其8MiB迁移预算。
# 模块用途: 硬拒绝投递秘密或显式替换为占位符，移除发送方主机路径且无损处理二进制。
from __future__ import annotations

import json
import os
import re
from bisect import bisect_right
from itertools import islice
from pathlib import Path

from ..common.log_redaction import redact_sensitive_text
from ..common.nofollow_fs import list_names_beneath, read_bytes_beneath
from .delivery_contracts import MAX_BUNDLE_BYTES, AssetDeliveryError

_UNQUOTED_ASSIGNMENT = re.compile(
    r'''(?im)^[ \t]*(?:-[ \t]+)?(?:export[ \t]+)?(?:api_key|secret|token|password|passwd|private_key|access_key|client_secret|bearer)[ \t]*[:=][ \t]*(?P<literal>[^\s#"'\r\n,;{}()\[\]]+)[ \t]*(?:\#.*)?$''')
_PATTERNS = (
    ('openai_key', re.compile(r'sk-[A-Za-z0-9_-]{20,}')),
    ('aws_access_key', re.compile(r'AKIA[0-9A-Z]{16}')),
    ('github_token', re.compile(r'ghp_[A-Za-z0-9]{20,}')),
    ('slack_token', re.compile(r'xox[baprs]-[A-Za-z0-9-]{10,}')),
    ('jwt', re.compile(r'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}')),
    ('unquoted_secret_assignment', _UNQUOTED_ASSIGNMENT),
    ('secret_assignment', re.compile(
        r'''(?i)(?<!\w)(?:api_key|secret|token|password|passwd|private_key|access_key|client_secret|bearer)(?!\w)["']?\s*[:=]\s*(?P<quote>["'])(?P<literal>(?:\\.|(?!(?P=quote))[^\\\r\n])+)(?P=quote)''')),
)
_PLACEHOLDER = re.compile(r'\$\{[^}\n]+\}|<[^>\n]+>|\bYOUR_[A-Za-z0-9_]+|\bCHANGE_ME\b', re.I)
_SECRET_METADATA = frozenset({'schema_version', 'credential_ref', 'datasource_id',
    'updated_at', 'created_at', 'kind', 'driver'})
_JSON_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')


# LLM: 只遍历已由本Owner凭据读口认证的内存值；元数据整支排除，任意非空字符串均作为秘密而不按长度猜测。
# 函数用途: 从嵌套凭据字典和列表提取待拦截字面值，不记录键名或明文。
def _secret_strings(value):
    pending, result = [value], set()
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(child for key, child in item.items() if key not in _SECRET_METADATA)
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str) and item:
            result.add(item)
    return result


# LLM: 本Owner目录逐叶nofollow有界读取；原Vault解密只读且须先认证，不创建主密钥；坏库失败关闭且不暴露异常正文。
# 函数用途: 将已配置明文或认证密文中的真实秘密留在本次内存扫描，不复制凭据库到包裹。
def datasource_secret_values(owner_home):
    from .datasource_store import _unseal_secret

    home, result = Path(owner_home).absolute(), set()
    parts = ('data', 'datasource_secrets')
    try:
        for name in list_names_beneath(home, parts):
            if not name.endswith('.json'):
                continue
            raw = read_bytes_beneath(home, (*parts, name), max_bytes=MAX_BUNDLE_BYTES, require_dir_fd=True)
            if raw is None:
                raise ValueError('credential changed during scan')
            data = json.loads(raw)
            result.update(_secret_strings(_unseal_secret(home, data) if isinstance(data, dict) else data))
    except (OSError, ValueError, TypeError, RecursionError):
        raise AssetDeliveryError('ASSET_DELIVERY_SECRET_SCAN_FAILED', '已配置秘密无法安全读取，投递已停止') from None
    return tuple(sorted(result))


# LLM: 普通模板可豁免；未加引号凭据键仅允许布尔/空值/变量引用，数字PIN和带引号的真实短秘密不能放行。
# 函数用途: 区分模板与YAML/env非秘密表达式，不访问变量或凭据来源。
def _placeholder(value, *, unquoted=False):
    return (_PLACEHOLDER.fullmatch(value) is not None or unquoted and (
        value.lower() in {'true', 'false', 'null', 'none', 'yes', 'no', '~'}
        or value.startswith(('$', 'os.', 'env.'))))


# LLM: 环境值仅本次内存匹配，不回传变量名或原值，不持久化环境。
# 函数用途: 取得需要额外拦截的非平凡环境秘密。
def _environment_secrets():
    return tuple({value for key, value in os.environ.items() if re.search(r'KEY|TOKEN|SECRET|PASSWORD', key, re.I)
        and len(value) >= 8 and value.strip() and not _placeholder(value)})


# LLM: 位置属于原文本，literal只替换原引号内值；非替换扫描有界收集即足以硬拒绝，不因单字符凭据积累巨量诊断。
# 函数用途: 收集高危凭据键、密钥形状和当前已知秘密的原始位置。
def _matches(text, secrets, *, limit=None):
    matches = []
    for name, pattern in _PATTERNS:
        group = 'literal' if 'literal' in pattern.groupindex else 0
        candidates = (match for match in pattern.finditer(text)
            if not _placeholder(match.group(group), unquoted=name == 'unquoted_secret_assignment'))
        matches.extend((*match.span(group), name) for match in islice(candidates,
            None if limit is None else max(0, limit - len(matches))))
    for secret in secrets:
        iterator = islice(re.finditer(re.escape(secret), text), None if limit is None else max(0, limit - len(matches)))
        matches.extend((*match.span(), 'known_secret') for match in iterator)
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
    protected = _UNQUOTED_ASSIGNMENT.sub(lambda match: shield(match)
        if _placeholder(match.group('literal'), unquoted=True) else match.group(), protected)
    scanned = redact_sensitive_text(protected, code_file=True)
    changed = scanned != protected
    scanned = re.sub(r'<redacted[^>]*>|\[redacted\]', '${REDACTED_SECRET}', scanned, flags=re.I)
    for marker, original in placeholders.items():
        scanned = scanned.replace(marker, original)
    return changed, scanned


# LLM: JSON替换只能覆盖完整字符串token；原始命中跨语法或非字符串则失败关闭，不能在Unicode转义内插入占位符。
# 函数用途: 解码检查并安全重编码JSON正文，保留拒绝诊断的原token位置。
def _json_string_scan(text, secrets, *, replace=False, raw_matches=()):
    matches, pieces, cursor = [], [], 0
    spans, index = sorted((start, end) for start, end, _ in raw_matches), 0
    for token in _JSON_STRING.finditer(text):
        raw_hit = False
        while index < len(spans) and spans[index][0] < token.end():
            start, end = spans[index]
            if start < token.start() or end > token.end():
                raise AssetDeliveryError('ASSET_DELIVERY_SECRET_DETECTED', 'JSON秘密位置不能安全替换')
            raw_hit, index = True, index + 1
        try:
            decoded = json.loads(token.group())
        except ValueError:
            continue
        found = _matches(decoded, secrets, limit=None if replace else 1)
        shared, _ = _shared_scan(decoded)
        if not found and not shared and not raw_hit:
            continue
        matches.append((*token.span(), 'json_encoded_secret'))
        if not replace:
            return matches, text
        cleaned = '${REDACTED_SECRET}' if raw_hit else _replace_matches(decoded, found)
        if shared:
            _, cleaned = _shared_scan(cleaned)
        pieces.extend((text[cursor:token.start()], json.dumps(cleaned, ensure_ascii=False)))
        cursor = token.end()
    if index < len(spans):
        raise AssetDeliveryError('ASSET_DELIVERY_SECRET_DETECTED', 'JSON秘密位置不能安全替换')
    return matches, ''.join((*pieces, text[cursor:])) if replace else text


# LLM: 指定Owner前缀先替换，再移除其他系统账号目录；只替换home前缀，保留资源相对后缀以便接收方重绑定。
# 函数用途: 将本机绝对家目录改成可配置的OWNER_HOME模板。
def _paths(text, owner_home):
    home = str(Path(owner_home).absolute())
    text = text.replace(home, '${OWNER_HOME}')
    return re.sub(r'/(?:Users/[^/\s"\'<>]+|home/[^/\s"\'<>]+|root)(?=/|\b)', '${OWNER_HOME}', text)


# LLM: 原字节及输出文件名都先核秘密；文件名命中始终拒绝以免改名破坏依赖，正文才允许显式替换，不回显明文。
# 函数用途: 在清洗和暂存前扫描整包，防止秘密经正文或资源清单流出。
def sanitize_bundle_files(files, *, owner_home, allow_secret_placeholder_replace=False, extra_secrets=()):
    if type(allow_secret_placeholder_replace) is not bool:
        raise ValueError('allow_secret_placeholder_replace必须是布尔值')
    secrets = tuple(set(_environment_secrets()) | {value for value in extra_secrets if isinstance(value, str) and value})
    result = {}
    for name, raw in files.items():
        if not isinstance(name, str) or not isinstance(raw, bytes):
            raise ValueError('投递文件必须是相对文件名到原始bytes的映射')
        if _matches(name, secrets, limit=1) or _shared_scan(name)[0]:
            safe_name = _replace_matches(_paths(name, owner_home), _matches(_paths(name, owner_home), secrets))
            raise AssetDeliveryError('ASSET_DELIVERY_SECRET_DETECTED', '投递资源路径包含疑似秘密，不能安全改名',
                redacted_matches=[{'file_path': redact_sensitive_text(safe_name, code_file=True),
                    'line_number': 1, 'pattern_name': 'secret_resource_path', 'masked_snippet': '<redacted>'}])
        text = raw.decode('utf-8', errors='surrogateescape')
        preserve_json = False
        if name.lower().endswith('.json') and allow_secret_placeholder_replace:
            try:
                json.loads(text)
                preserve_json = True
            except (ValueError, RecursionError):
                pass
        matches = _matches(text, secrets, limit=None if allow_secret_placeholder_replace else 100)
        encoded = (_json_string_scan(text, secrets)[0]
            if name.lower().endswith('.json') and not allow_secret_placeholder_replace else [])
        shared, _ = _shared_scan(text)
        if (matches or encoded or shared) and not allow_secret_placeholder_replace:
            newlines = [index for index, char in enumerate(text) if char == '\n']
            safe_name = _replace_matches(_paths(name, owner_home), _matches(_paths(name, owner_home), secrets))
            safe_name = redact_sensitive_text(safe_name, code_file=True)
            diagnostics = [{'file_path': safe_name, 'line_number': bisect_right(newlines, start) + 1,
                'pattern_name': pattern, 'masked_snippet': '<redacted>'} for start, _, pattern in (matches + encoded)[:100]]
            if not diagnostics:
                diagnostics = [{'file_path': safe_name, 'line_number': 1, 'pattern_name': 'shared_secret',
                    'masked_snippet': '<redacted>'}]
            raise AssetDeliveryError('ASSET_DELIVERY_SECRET_DETECTED', '投递文件包含疑似秘密', redacted_matches=diagnostics)
        if preserve_json:
            _, text = _json_string_scan(text, secrets, replace=True, raw_matches=matches)
        else:
            text = _replace_matches(text, matches)
            if name.lower().endswith('.json') and allow_secret_placeholder_replace:
                _, text = _json_string_scan(text, secrets, replace=True)
        if shared:
            _, text = _shared_scan(text)
        text = _paths(text, owner_home)
        if preserve_json:
            try:
                json.loads(text)
            except (ValueError, RecursionError):
                raise AssetDeliveryError('ASSET_DELIVERY_SECRET_DETECTED', 'JSON秘密不能在保留格式时替换') from None
        result[name] = text.encode('utf-8', errors='surrogateescape')
    return result
