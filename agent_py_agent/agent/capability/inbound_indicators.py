"""有界入站指标提取；只产生推荐提示，不读取附件内容或执行动作。"""
import ipaddress
import re
from functools import lru_cache


# LLM: 正则仅匹配候选，地址仍需标准库完整校验；缓存不持有任何用户输入。
# 函数用途: 复用固定解析模式，避免每轮重新编译。
@lru_cache(maxsize=1)
def _indicator_patterns():
    return tuple((kind, re.compile(pattern, re.IGNORECASE)) for kind, pattern in (
        ('url', r'https?://[^\s<>"\x27]+'),
        ('email', r'(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63}\b'),
        ('ipv4', r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])'),
        ('ipv6', r'(?<![\w:])(?:[0-9a-f]{0,4}:){2,}[0-9a-f:.]*(?![\w:])'),
        ('domain', r'(?<![\w.-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}(?![\w.-])'),
        ('md5', r'\b[0-9a-f]{32}\b'), ('sha1', r'\b[0-9a-f]{40}\b'),
        ('sha256', r'\b[0-9a-f]{64}\b'), ('cve_id', r'\bCVE-\d{4}-\d{4,}\b'),
        ('monitor_alias', r'(?<![a-z0-9_.-])job\d+(?![a-z0-9_.-])'),
        ('file_path', r'(?:[A-Z]:\\|/)(?:[^\s<>"\x27,;]+)'),
    ))


# LLM: 预算只编码至多remaining个字符，避免为截断超大正文先复制全部UTF-8字节。
# 函数用途: 返回完整字符边界的前缀及其已用字节数。
def _scan_prefix(value, remaining):
    if not isinstance(value, str) or remaining <= 0:
        return '', 0
    raw = value[:remaining].encode('utf-8', errors='replace')[:remaining]
    return raw.decode('utf-8', errors='ignore'), len(raw)


# LLM: 文件后缀排除只影响推荐线索，不决定执行权限；IPv4与IPv6均经完整解析且标出非公网范围。
# 函数用途: 清洗候选值并否定版本号、文件名等误报。
def _candidate(kind, value, prefix):
    if kind in {'ipv4', 'ipv6'}:
        if kind == 'ipv4' and re.search(r'(?:\bversion|\bver|\bv)\s*[:=]?\s*$', prefix, re.IGNORECASE):
            return None
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            return None
        if address.version != (4 if kind == 'ipv4' else 6):
            return None
        return {'value': str(address), 'reserved': not address.is_global}
    if kind == 'domain':
        if value.rsplit('.', 1)[-1].casefold() in {
            'json', 'py', 'md', 'txt', 'csv', 'js', 'ts', 'yaml', 'yml', 'toml', 'xml',
            'png', 'jpg', 'jpeg', 'gif', 'svg', 'pdf', 'docx', 'xlsx', 'zip', 'gz', 'tar',
            'sh', 'exe', 'dll', 'log', 'html', 'css', 'sql', 'rs', 'go', 'java', 'mp4',
        }:
            return None
        return {'value': value.casefold()}
    return {'value': value.rstrip(').，。') if kind == 'url' else value.upper() if kind == 'cve_id' else value}


# LLM: 各种候选合并后按原字符位置输出，类型优先级只打破同位置并列，不按类型重排整篇。
# 函数用途: 从一个已限长的文本片段产生有序候选。
def _text_indicators(text, source):
    matches = []
    for rank, (kind, pattern) in enumerate(_indicator_patterns()):
        for match in pattern.finditer(text):
            value = _candidate(kind, match.group(), text[max(0, match.start() - 24):match.start()])
            if value is not None:
                matches.append((match.start(), rank, {'type': kind, **value, 'source': source}))
    # 只消费准确的结构化标签，不按普通词语猜测hostname、端口或事件ID。
    labels = re.compile(r'\b(hostname|port|process_name|file_path|alert_id|monitor_alias)["\x27]?\s*[:=]\s*["\x27]?([^\s"\x27,;}]+)', re.IGNORECASE)
    for match in labels.finditer(text):
        kind, value = match.group(1).casefold(), match.group(2)
        if kind != 'port' or value.isdigit() and 0 < int(value) < 65536:
            matches.append((match.start(2), -1, {'type': kind, 'value': value, 'source': source}))
    return [row for _, _, row in sorted(matches, key=lambda item: item[:2])]


# LLM: 纯值接口，不进行I/O；所有文本共享扫描预算。
# 函数用途: 从本轮文本与显式附件元数据提取去重指标。
def extract_indicators(text: str, *, attachments=(), ocr_texts=(), max_scan_bytes: int = 65536) -> list[dict]:
    if type(max_scan_bytes) is not int or max_scan_bytes < 0:
        raise ValueError('max_scan_bytes must be a non-negative integer')
    fragment, used = _scan_prefix(text, max_scan_bytes)
    candidates = _text_indicators(fragment, 'text')
    remaining = max_scan_bytes - used
    if isinstance(attachments, (list, tuple)):
        for number, attachment in enumerate(attachments):
            if not isinstance(attachment, dict):
                continue
            media_type = attachment.get('media_type', attachment.get('mime_type', ''))
            if isinstance(media_type, str) and media_type.startswith('image/'):
                value = attachment.get('sha256') or attachment.get('name') or str(number)
                candidates.append({'type': 'image_attachment', 'value': str(value)[:128], 'source': f'attachment:{number}'})
            fragment, used = _scan_prefix(attachment.get('ocr_text'), remaining)
            remaining -= used
            candidates.extend(_text_indicators(fragment, f'attachment:{number}:ocr'))
    if isinstance(ocr_texts, (list, tuple)):
        for number, value in enumerate(ocr_texts):
            fragment, used = _scan_prefix(value, remaining)
            remaining -= used
            candidates.extend(_text_indicators(fragment, f'ocr:{number}'))
    result, seen = [], set()
    for row in candidates:
        key = (row['type'], row['value'])
        if key not in seen:
            result.append(row)
            seen.add(key)
    return result
