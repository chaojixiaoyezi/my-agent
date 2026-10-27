# LLM: clean_json前核原字节；JSON先关联归一化敏感键与完整字符串值，再保留原有正文/YAML/已知秘密扫描，不执行表达式。
# 模块用途: 为附件和script_store_support提供唯一敏感键口径，硬拒绝秘密或保形替换，移除主机路径且无损处理二进制。
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

_SECRET_KEYS = frozenset(('secret', 'token', 'password', 'passwd', 'pwd', 'passphrase', 'pass_phrase',
    'apikey', 'api_key', 'access_key', 'access_token', 'refresh_token', 'client_secret',
    'private_key', 'auth_token', 'credential', 'webhook', 'web_hook', 'cookie', 'bearer', 'authorization'))
_KEY_NAME = r'[A-Za-z0-9_][A-Za-z0-9_.-]*'
_UNQUOTED_ASSIGNMENT = re.compile(
    rf'''(?im)^[ \t]*(?:-[ \t]+)?(?:export[ \t]+)?(?P<key>{_KEY_NAME})[ \t]*[:=][ \t]*(?P<literal>[^\s#"'\r\n,;{{}}()\[\]]+)[ \t]*(?:\#.*)?$''')
_PATTERNS = (
    ('openai_key', re.compile(r'sk-[A-Za-z0-9_-]{20,}')),
    ('aws_access_key', re.compile(r'AKIA[0-9A-Z]{16}')),
    ('github_token', re.compile(r'ghp_[A-Za-z0-9]{20,}')),
    ('slack_token', re.compile(r'xox[baprs]-[A-Za-z0-9-]{10,}')),
    ('jwt', re.compile(r'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}')),
    ('unquoted_secret_assignment', _UNQUOTED_ASSIGNMENT),
    ('secret_assignment', re.compile(
        rf'''(?i)(?=(?<![\w.-])[-.]*(?P<key>{_KEY_NAME})["']?\s*[:=]\s*(?P<quote>["'])(?P<literal>(?:\\.|(?!(?P=quote))[^\\\r\n])+)(?P=quote))''')),
)
_PLACEHOLDER = re.compile(r'\$\{[A-Za-z_][A-Za-z0-9_]*\}|<[A-Za-z_][A-Za-z0-9_.-]*>|\bYOUR_[A-Za-z0-9_]+|\bCHANGE_ME\b|\*\*\*REDACTED\*\*\*', re.I)
_ENV_REFERENCE = re.compile(
    r'''\$[A-Za-z_][A-Za-z0-9_]*|env\.[A-Za-z_][A-Za-z0-9_]*|os\.environ(?:\[[ \t]*(?P<bracket>["'])[A-Za-z_][A-Za-z0-9_]*(?P=bracket)[ \t]*\])?|(?:os\.environ\.get|os\.getenv|env\.get)\([ \t]*(?P<call>["'])[A-Za-z_][A-Za-z0-9_]*(?P=call)[ \t]*\)''')
_SECRET_METADATA = frozenset({'schema_version', 'credential_ref', 'datasource_id',
    'updated_at', 'created_at', 'kind', 'driver'})
_JSON_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
_JSON_STRUCTURE_TOKEN = re.compile(_JSON_STRING.pattern + r'|[{}\[\],]')
_YAML_HEADER = re.compile(rf'''(?i)^(?P<indent>[ \t]*)(?P<seq>-[ \t]+)?["']?(?P<key>{_KEY_NAME})["']?[ \t]*:[ \t]*(?P<tail>.*)$''')
_YAML_INLINE = re.compile(rf'''(?im)(?:^|(?<=[{{,\[\s]))["']?(?P<key>{_KEY_NAME})["']?[ \t]*:[ \t]*''')
_YAML_BLOCK = re.compile(r'[|>](?:[1-9][+-]?|[+-][1-9]?)?')


# LLM: 仅归一键名；词表同收passPhrase/webHook拆词别名，缩写及camelCase沿完整末尾片段匹配，不误伤token_count。
# 函数用途: 为JSON、YAML和env/代码赋值提供同一敏感键判定，支持业务前缀及下划线、横线、点连接。
def _sensitive_key(key):
    normalized = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', key)
    normalized = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', normalized)
    normalized = normalized.replace('-', '_').replace('.', '_').lower()
    return any(normalized == word or normalized.endswith('_' + word) for word in _SECRET_KEYS)


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


# LLM: 只豁免完整单行模板/变量引用；续行、拼接及字面量默认值不因变量前缀放行，布尔/空值仍仅限未引用值。
# 函数用途: 区分纯占位与敏感值，不求值或读取环境，数字PIN及混合值继续拒绝。
def _placeholder(value, *, unquoted=False):
    if '\n' in value or '\r' in value:
        return False
    return (_PLACEHOLDER.fullmatch(value) is not None or unquoted and (
        value.lower() in {'true', 'false', 'null', 'none', 'yes', 'no', '~'}
        or _ENV_REFERENCE.fullmatch(value) is not None))


# LLM: 不构造YAML对象、不执行tag/alias；只判定已定位标量，带引号的布尔/数字仍可是真秘密。
# 函数用途: 判断一个原始YAML标量是否需要替换，保留已有模板和空值。
def _yaml_secret(value):
    value = value.strip()
    quoted = value[:1] in {'"', "'"}
    if quoted and value[-1:] == value[:1]:
        value = value[1:-1]
    return bool(value) and not _placeholder(value, unquoted=not quoted)


# LLM: 引用标量的转义是词法状态，不构造值；单引号双写跳过两字符，双引号反斜线只影响下一字符。
# 函数用途: 推进一个引用字符并报告引用是否结束。
def _yaml_quoted_step(text, cursor, quote, escaped):
    if escaped:
        return quote, False, cursor + 1, False
    char = text[cursor]
    if char == '\\' and quote == '"':
        return quote, True, cursor + 1, False
    if char != quote:
        return quote, False, cursor + 1, False
    if quote == "'" and text[cursor + 1:cursor + 2] == "'":
        return quote, False, cursor + 2, False
    return '', False, cursor + 1, True


# LLM: 调用方传外层flow上下文；块式plain的逗号是正文，flow逗号才分值；引用与内部容器仍按闭合边界处理。
# 函数用途: 找到完整flow或引用标量边界，避免半替换遗留秘密或破坏集合闭合。
def _yaml_value_end(text, start, stop, *, in_flow=False):
    quote, depth, escaped, cursor = '', 0, False, start
    container = text[start:start + 1] in {'[', '{'}
    while cursor < stop:
        char = text[cursor]
        if quote:
            quote, escaped, cursor, closed = _yaml_quoted_step(text, cursor, quote, escaped)
            if closed and text[start:start + 1] in {'"', "'"} and depth == 0:
                return cursor
            continue
        if char in {'"', "'"}:
            quote = char
            cursor += 1
            continue
        if char in '[{':
            depth += 1
            cursor += 1
            continue
        if char in ']}':
            if not depth:
                return cursor
            depth -= 1
            if container and depth == 0:
                return cursor + 1
            cursor += 1
            continue
        if not depth and (in_flow and char == ',' or char == '#' and (cursor == start or text[cursor - 1].isspace())):
            return cursor
        cursor += 1
    return stop


# LLM: 块体是字符串，其中括号/引号不参与外部flow层级；遮罩保留长度与换行，不改变实际待扫描原文。
# 函数用途: 为词法上下文排除任意YAML块标量正文，避免文档代码影响后续键边界。
def _yaml_lexical_text(text):
    result, block_indent = [], None
    for line in text.splitlines(keepends=True):
        indent = len(line) - len(line.lstrip(' \t'))
        if block_indent is not None and (not line.strip() or indent > block_indent):
            result.append(re.sub(r'[^\r\n]', ' ', line))
            continue
        block_indent = None
        header = re.match(r'^(?P<indent>[ \t]*)(?P<seq>-[ \t]+)?[^:#\r\n]+:[ \t]*[|>][1-9+-]*[ \t]*(?:#.*)?$', line.rstrip('\r\n'))
        if header:
            block_indent = len(header['indent']) + len(header['seq'] or '')
        result.append(line)
    return ''.join(result)


# LLM: 状态仅用于flow层级，注释在换行清除，引用内括号不计层级；无I/O、无YAML对象构造。
# 函数用途: 推进flow扫描的单字符状态，保持主扫描平坦。
def _yaml_flow_step(state, char):
    depth, quote, escaped, comment = state
    if comment:
        return depth, quote, escaped, char not in '\r\n'
    if quote:
        if escaped:
            return depth, quote, False, False
        if char == '\\' and quote == '"':
            return depth, quote, True, False
        return depth, '' if char == quote else quote, False, False
    if char == '#':
        return depth, quote, escaped, True
    if char in {'"', "'"}:
        return depth, char, escaped, False
    if char in '[{':
        return depth + 1, quote, escaped, False
    if char in ']}':
        return max(0, depth - 1), quote, escaped, False
    return state


# LLM: 一次扫描括号/引用/注释上下文；键先过统一归一化判定，不让普通键消费敏感值，不解析对象或执行标签。
# 函数用途: 为每个归一化敏感键标注是否位于跨行flow集合内。
def _yaml_inline_contexts(text):
    lexical = _yaml_lexical_text(text)
    cursor, state = 0, (0, '', False, False)
    result = []
    for match in _YAML_INLINE.finditer(text):
        if not _sensitive_key(match['key']):
            continue
        for char in lexical[cursor:match.start()]:
            state = _yaml_flow_step(state, char)
        result.append((match, state[0]))
        cursor = match.end()
    return result


# LLM: plain首值及续行是一组；序列包括独占行的dash，保留指示符，注释/空行不另开秘密字符串。
# 函数用途: 生成多行敏感值的保形替换片段。
def _yaml_multiline_spans(body, findings, *, plain=False):
    groups, pending_sequence = [], False
    pattern = r'(?P<prefix>[ \t]*)(?P<value>[^\r\n]*)' if plain else (
        r'(?P<prefix>[ \t]*(?P<seq>-(?:[ \t]+|(?=\r?\n|$)))?)(?P<value>[^\r\n]*)')
    for start, child in body:
        scalar = re.match(pattern, child)
        if not plain and scalar['seq']:
            pending_sequence = True
        value = scalar['value'].strip()
        if not value or value.startswith('#'):
            continue
        if not plain and (re.match(r'''["']?[^\s:]+["']?\s*:\s''', value) or value.startswith(('{', '['))):
            return []
        if pending_sequence or not groups:
            groups.append([start + scalar.start('value'), start + scalar.end('value'), value])
            pending_sequence = False
        else:
            groups[-1][1] = start + scalar.end('value')
            groups[-1][2] += '\n' + value
    spans = []
    for start, end, value in groups:
        if _yaml_secret(value):
            findings.add('yaml_multiline_secret')
            spans.append((start, end, '"***REDACTED***"'))
    return spans


# LLM: 注释不决定缩进边界；无首值时认同级序列，有plain首值时更深dash是同一字符串续行。
# 函数用途: 找出敏感键值所属的后续物理行，遇同级兄弟键立即停止。
def _yaml_body_lines(lines, index, indent, offset, *, indentless=False, plain=False):
    body, end = [], offset
    for child in islice(lines, index + 1, None):
        stripped = child.lstrip(' \t')
        child_indent = len(child) - len(stripped)
        sequence = re.match(r'-(?:\s|$)', stripped)
        if child.strip() and not stripped.startswith('#'):
            if child_indent < indent or child_indent == indent and not (indentless and sequence):
                break
            if plain and re.match(r'''(?:["'][^"']+["']|[^:]+):(?:\s|$)''', stripped):
                break
        body.append((end, child))
        end += len(child)
    return body, end


# LLM: 块体保留头及缩进；只处理统一敏感键，plain首值和续行一起替换；容器/引用仍沿原flow扫描器。
# 函数用途: 定位归一化敏感键的块标量、折行标量及无缩进项列表。
def _yaml_indented_spans(text, findings, flow_keys):
    lines, spans, offset = text.splitlines(keepends=True), [], 0
    covered_until = 0
    for index, line in enumerate(lines):
        match = _YAML_HEADER.match(line.rstrip('\r\n'))
        if match is None or not _sensitive_key(match['key']) or offset < covered_until or offset + match.start('key') in flow_keys:
            offset += len(line)
            continue
        tail = match['tail'].split('#', 1)[0].strip()
        block = bool(_YAML_BLOCK.fullmatch(tail))
        if tail and not block and tail.startswith(('[', '{', '"', "'")):
            offset += len(line)
            continue
        indent = len(match['indent']) + len(match['seq'] or '')
        body, end = _yaml_body_lines(lines, index, indent, offset + len(line),
            indentless=not tail, plain=bool(tail) and not block)
        if block and any(child.strip() for _, child in body):
            value = '\n'.join(child.strip() for _, child in body if child.strip())
            if _yaml_secret(json.dumps(value)):
                findings.add('yaml_block_secret')
                nonempty = next(child for _, child in body if child.strip())
                padding = nonempty[:len(nonempty) - len(nonempty.lstrip(' \t'))]
                spans.append((offset + len(line), end, padding + '***REDACTED***\n'))
                covered_until = end
        elif not tail:
            spans.extend(_yaml_multiline_spans(body, findings))
        elif not block and any(child.strip() and not child.lstrip().startswith('#') for _, child in body):
            spans.extend(_yaml_multiline_spans([(offset + match.start('tail'), line[match.start('tail'):]), *body], findings, plain=True))
        offset += len(line)
    return spans


# LLM: 外层flow深度显式交给边界扫描，plain逗号不截断；旧赋值以完整literal闭引号边界接管，零宽定位不改变替换合同。
# 函数用途: 沿统一敏感键保形扫描YAML flow、多行及块标量，不把块头符号当密码。
def _scan_and_redact_yaml_structure(text: str, *, findings: set[str], redact: bool) -> str:
    pairs = _yaml_inline_contexts(text)
    spans = _yaml_indented_spans(text, findings, {match.start() for match, depth in pairs if depth})
    covered = sorted((start, end) for start, end, _ in spans)
    starts = [start for start, _ in covered]
    for match, depth in pairs:
        index = bisect_right(starts, match.end()) - 1
        if index >= 0 and match.end() < covered[index][1]:
            continue
        start = match.end()
        if depth:
            while start < len(text) and text[start].isspace():
                start += 1
        stop = text.find('\n', start)
        stop = len(text) if stop < 0 else stop
        if depth or text[start:start + 1] in {'[', '{', '"', "'"}:
            stop = len(text)
        end = _yaml_value_end(text, start, stop, in_flow=bool(depth))
        value = text[start:end].strip()
        if not value or _YAML_BLOCK.fullmatch(value):
            continue
        # 只让确实完整覆盖本值的旧行规则接管，不能将含空格/容器/引用转义的值误当作已处理。
        line_start = text.rfind('\n', 0, match.start()) + 1
        prefix = text[line_start:match.start()]
        if not depth and (not prefix.strip() or prefix.strip() == '-'):
            legacy = _UNQUOTED_ASSIGNMENT.match(text, line_start)
            quoted = _PATTERNS[-1][1].search(text, match.start(), end)
            if legacy and _sensitive_key(legacy['key']) and legacy.end('literal') == end or quoted and _sensitive_key(quoted['key']) and quoted.end('literal') + 1 == end:
                continue
        if _yaml_secret(value):
            findings.add('yaml_flow_secret')
            spans.append((start, end - (len(text[start:end]) - len(text[start:end].rstrip())), '"***REDACTED***"'))
    if not redact:
        return text
    result, cursor = [], 0
    for start, end, replacement in sorted(spans):
        if start < cursor:
            continue
        result.extend((text[cursor:start], replacement))
        cursor = end
    return ''.join((*result, text[cursor:]))


# LLM: 环境值仅本次内存匹配，不回传变量名或原值，不持久化环境。
# 函数用途: 取得需要额外拦截的非平凡环境秘密。
def _environment_secrets():
    return tuple({value for key, value in os.environ.items() if re.search(r'KEY|TOKEN|SECRET|PASSWORD', key, re.I)
        and len(value) >= 8 and value.strip() and not _placeholder(value)})


# LLM: 原文位置保留；赋值沿完整键边界统一判定，零宽引用匹配不吞内部赋值或反复扫描长键后缀；形状/已知秘密规则不变。
# 函数用途: 收集归一化凭据键、密钥形状和当前已知秘密的原始位置。
def _matches(text, secrets, *, limit=None):
    matches = []
    for name, pattern in _PATTERNS:
        group = 'literal' if 'literal' in pattern.groupindex else 0
        candidates = (match for match in pattern.finditer(text)
            if ('key' not in pattern.groupindex or _sensitive_key(match['key']))
            and not (name == 'unquoted_secret_assignment' and _YAML_BLOCK.fullmatch(match.group(group)))
            and not _placeholder(match.group(group), unquoted=name == 'unquoted_secret_assignment'))
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


# LLM: 模板先遮成短标记并恢复；只保护统一敏感键下完整引用，code_file模式仅供共享形状扫描，不能代替JSON关联检查。
# 函数用途: 兼容原模板合同地保留既有公共秘密扫描，不提前clean_json或弱化已知秘密检测。
def _shared_scan(text):
    placeholders = {}

    # LLM: 标记只存内存并采用共享扫描器可认的纯变量形状；避开原文冲突，不让authorization/cookie将保护标记误判为秘密。
    # 函数用途: 临时保护调用者完整模板并在扫描后原样恢复，不改变原占位正文。
    def shield(match):
        variable = f'DELIVERY_PLACEHOLDER_{len(placeholders)}'
        while '${' + variable + '}' in text:
            variable += '_'
        marker = '${' + variable + '}'
        placeholders[marker] = match.group()
        return marker

    protected = _PLACEHOLDER.sub(shield, text)
    protected = _UNQUOTED_ASSIGNMENT.sub(lambda match: shield(match)
        if _sensitive_key(match['key']) and _placeholder(match.group('literal'), unquoted=True) else match.group(), protected)
    scanned = redact_sensitive_text(protected, code_file=True)
    changed = scanned != protected
    scanned = re.sub(r'<redacted[^>]*>|\[redacted\]', '${REDACTED_SECRET}', scanned, flags=re.I)
    for marker, original in placeholders.items():
        scanned = scanned.replace(marker, original)
    return changed, scanned


# LLM: 仅接受已确认合法的JSON；平坦词法栈保留容器敏感关联及重复键，解码判占位后只替换完整token，不重建字段。
# 函数用途: 递归关联敏感键与其dict/list内字符串值；短值和转义值同样拒绝，显式替换使用固定字面值。
def _json_structure_scan(text, *, replace=False):
    matches, pieces, cursor = [], [], 0
    # 每层保存继承敏感性和当前键敏感性；列表及根标量不凭空产生键关联。
    stack = [[False, False]]
    for token in _JSON_STRUCTURE_TOKEN.finditer(text):
        raw = token.group()
        if raw in {'{', '['}:
            stack.append([any(stack[-1]), False])
            continue
        if raw in {'}', ']'}:
            stack.pop()
            continue
        if raw == ',':
            stack[-1][1] = False
            continue
        value = json.loads(raw)
        following = token.end()
        while following < len(text) and text[following].isspace():
            following += 1
        if text[following:following + 1] == ':':
            stack[-1][1] = _sensitive_key(value)
        elif any(stack[-1]) and value and not _placeholder(value):
            matches.append((*token.span(), 'json_sensitive_key'))
            if not replace:
                return matches, text
            pieces.extend((text[cursor:token.start()], '"<REDACTED_SECRET>"'))
            cursor = token.end()
    return matches, ''.join((*pieces, text[cursor:])) if replace else text


# LLM: JSON字符串解码后同样扫描YAML结构；只重编码完整token，跨语法或非字符串命中仍失败关闭。
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
        yaml_findings = set()
        yaml_cleaned = _scan_and_redact_yaml_structure(decoded, findings=yaml_findings, redact=replace)
        if not found and not shared and not raw_hit and not yaml_findings:
            continue
        matches.append((*token.span(), 'json_encoded_secret'))
        if not replace:
            return matches, text
        cleaned = '${REDACTED_SECRET}' if raw_hit else _replace_matches(yaml_cleaned, _matches(yaml_cleaned, secrets))
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


# LLM: 原字节先核秘密；合法JSON先关联归一化键值并整值替换，继续原形状/已知秘密/内嵌YAML扫描；路径秘密始终拒绝。
# 函数用途: 在清洗和暂存前扫描整包附件与元数据，保留合法JSON结构、普通字段及原二进制合同。
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
        valid_json = False
        structured = []
        if name.lower().endswith('.json'):
            try:
                json.loads(text)
                valid_json = True
                preserve_json = allow_secret_placeholder_replace
            except (ValueError, RecursionError):
                pass
        if valid_json:
            structured, text = _json_structure_scan(text, replace=allow_secret_placeholder_replace)
        yaml_findings = set()
        if not valid_json:
            text = _scan_and_redact_yaml_structure(text, findings=yaml_findings, redact=allow_secret_placeholder_replace)
        matches = _matches(text, secrets, limit=None if allow_secret_placeholder_replace else 100)
        encoded = (_json_string_scan(text, secrets)[0]
            if name.lower().endswith('.json') and not allow_secret_placeholder_replace else [])
        shared, _ = _shared_scan(text)
        if (structured or matches or encoded or shared or yaml_findings) and not allow_secret_placeholder_replace:
            newlines = [index for index, char in enumerate(text) if char == '\n']
            safe_name = _replace_matches(_paths(name, owner_home), _matches(_paths(name, owner_home), secrets))
            safe_name = redact_sensitive_text(safe_name, code_file=True)
            diagnostics = [{'file_path': safe_name, 'line_number': bisect_right(newlines, start) + 1,
                'pattern_name': pattern, 'masked_snippet': '<redacted>'} for start, _, pattern in (structured + matches + encoded)[:100]]
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
