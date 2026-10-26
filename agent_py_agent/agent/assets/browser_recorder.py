# LLM: 只持久化宿主/UI提交的动作，不暗中启动浏览器；秘密在写前剥离，编译不执行，实时采集由调用方接线。
# 模块用途: 管理浏览器动作记录，编译安全参数化Python脚本及可选工作流/Skill包装。
from __future__ import annotations

import ast
import ipaddress
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from ..common.nofollow_fs import list_names_beneath
from .descriptor import AssetDescriptor
from .script_store import ScriptStore
from .turn_extractor import (
    _asset_root,
    _read_record,
    _record_lock,
    _redact,
    _script_spec,
    _skill_markdown,
    _workflow_spec,
    _write_record,
)

_RECORDINGS = 'browser_recordings'
_ACTION_TYPES = frozenset({'goto', 'click', 'fill', 'select', 'press', 'wait_for_selector', 'extract_text', 'extract_table', 'screenshot'})
_SECRET_NAME = re.compile(r'password|passwd|secret|token|api[_-]?key|otp|authorization|cookie', re.I)
_SELECTOR_TOKEN = re.compile(r'''/\*[\s\S]*?\*/|\\.|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\[(?:\\.|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\]\\'"])*\]''', re.S)
_CSS_ESCAPE = re.compile(r'\\(?:([0-9a-fA-F]{1,6})(?:\r\n|[ \t\r\n\f])?|([^\r\n\f]))')
_CSS_IDENT = r'(?:\\(?:[0-9a-fA-F]{1,6}(?:\r\n|[ \t\r\n\f])?|[^\r\n\f])|[-\w])+'
_CSS_ATTRIBUTE_NAME = re.compile(r'\[\s*(?:(?:' + _CSS_IDENT + r'|\*)?\|)?(' + _CSS_IDENT + ')')


# LLM: 纯值动作只保存规范字段；query_parameters额外承载URL查询参数，原秘密永不进入value/default。
# 类用途: 描述一次带稳定顺序、可参数化的浏览器操作。
@dataclass
class RecordedBrowserAction:
    seq: int
    action_type: str
    selector: str = ''
    url: str = ''
    value: str = ''
    param_name: str = ''
    is_parameterized: bool = False
    is_secret: bool = False
    description: str = ''
    timestamp: str = ''
    query_parameters: list[dict[str, object]] = field(default_factory=list)


# LLM: 仅一个Owner内持久化，recording_id固定；headless是回放选项，不代表已启动或录到真实浏览器。
# 类用途: 保存录制状态、规范动作、参数及后续编译脚本ID。
@dataclass
class RecordingSessionRecord:
    recording_id: str
    start_url: str
    title: str = ''
    thread_id: str = ''
    workspace_root: str = ''
    headless: bool = True
    status: str = 'RECORDING'
    created_at: str = ''
    revision: int = 1
    action_trace: list[dict[str, object]] = field(default_factory=list)
    detected_parameters: list[dict[str, object]] = field(default_factory=list)
    compiled_script_id: str = ''


# LLM: scheme/authority及静态目标地址为硬门；拒绝本地域名和特殊用途IP，不以离线校验冒称DNS安全。
# 函数用途: 校验HTTP(S)录制URL，拒绝本机和内网目标并返回解析结构。
def _url(value):
    if not isinstance(value, str) or any(ord(char) < 32 for char in value) or '\\' in value:
        raise ValueError('RECORDER_UNSAFE_URL_SCHEME')
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme in {'http', 'https'} and parsed.hostname and parsed.port != 0
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('RECORDER_UNSAFE_URL_SCHEME')
    hostname = parsed.hostname.lower().rstrip('.')
    if hostname in {'localhost', 'localhost.localdomain'} or hostname.endswith(('.localhost', '.local')):
        raise ValueError('RECORDER_UNSAFE_URL')
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return parsed
    if address.is_loopback or address.is_private or address.is_link_local or address.is_unspecified or address.is_multicast or address.is_reserved:
        raise ValueError('RECORDER_UNSAFE_URL')
    return parsed


# LLM: 参数名始终是字典键而非Python标识符插值；校验只保证合同清晰，不替代AST转义。
# 函数用途: 拒绝空参数名、控制字符和不稳定名称。
def _parameter_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', value):
        raise ValueError('RECORDER_PARAMETER_NAME_INVALID')
    return value


# LLM: CSS续行、十六进制/单字符转义只作安全识别，不改变返回selector；非法码点按替换字符处理。
# 函数用途: 识别转义拼写中的敏感属性和密码类型。
def _decode_css_escapes(value):
    # LLM: 只解一个正则已界定的CSS转义，不执行表达式或读取外部资源。
    # 函数用途: 将转义码点转换为可比较的字符。
    def decode(match):
        if match[1] is None:
            return match[2]
        point = int(match[1], 16)
        return chr(point) if 0 < point <= 0x10FFFF and not 0xD800 <= point <= 0xDFFF else '\ufffd'

    return _CSS_ESCAPE.sub(decode, re.sub(r'\\(?:\r\n|[\n\r\f])', '', value))


# LLM: 只在临时属性名解析副本中忽略注释，绝不修改原token字符串；扫描保护引号/续行，支持命名空间。
# 函数用途: 返回属性的解码名称，普通字符串和伪类文本返回空。
def _selector_attribute_name(token):
    match = _CSS_ATTRIBUTE_NAME.match(re.sub(r'/\*[\s\S]*?\*/', '', token)) if token.startswith('[') else None
    return _decode_css_escapes(match[1]).casefold() if match else ''


# LLM: 密码标记、secret_key、输入type、解码selector及凭据形状共同判定，显式False不能取消其它证据。
# 函数用途: 识别必须由运行时安全输入的动作。
def _secret_action(raw):
    names = ' '.join(str(raw.get(key, '')) for key in ('selector', 'param_name', 'field_name', 'name', 'input_type'))
    return bool(raw.get('is_secret') or raw.get('secret') or raw.get('is_password') or raw.get('secret_key')
                or str(raw.get('type', '')).lower() == 'password' or _SECRET_NAME.search(_decode_css_escapes(names))
                or _redact(str(raw.get('value', ''))) != str(raw.get('value', '')))


# LLM: 只删除真实value属性token（含CSS转义），保留引号/其它属性/type语义；裸秘密失败关闭，不改结构键。
# 函数用途: 防止密码通过selector属性或裸值进入录制和编译常量。
def _safe_selector(raw, secret):
    selector = str(raw.get('selector', ''))
    if secret:
        selector = _SELECTOR_TOKEN.sub(lambda match: '' if _selector_attribute_name(match[0]) == 'value' else match[0], selector)
        value = str(raw.get('value') or '')
        structural = _SELECTOR_TOKEN.sub(lambda match: '' if _selector_attribute_name(match[0]) == 'type' else match[0], selector)
        structural = _decode_css_escapes(structural)
        if value and value in structural and (len(value) > 3 or re.search(r'(?<![\w-])' + re.escape(value) + r'(?![\w-])', structural)):
            raise ValueError('RECORDER_SELECTOR_CONTAINS_SECRET')
    return _redact(selector)


# LLM: 原始URL秘密在本函数内丢弃；每个查询位置独立参数化，重复query键保留各自位置。
# 函数用途: 将导航URL及查询参数转换为脱敏常量。
def _navigation(raw, seq):
    parsed = _url(raw.get('url', ''))
    authority = parsed.netloc.rsplit('@', 1)[-1]
    parameters, pairs = [], []
    for index, (name, value) in enumerate(parse_qsl(parsed.query, keep_blank_values=True), 1):
        secret = bool(_SECRET_NAME.search(name) or _redact(value) != value)
        slug = re.sub(r'[^A-Za-z0-9_]', '_', name)[:40] or str(index)
        param = f'query_{seq}_{slug}' + (f'_{index}' if any(p['query_name'] == name for p in parameters) else '')
        parameters.append({'param_name': param, 'type': 'string', 'default_value': '' if secret else value,
                           'secret': secret, 'source_action_seq': seq, 'query_name': name, 'query_index': index - 1})
        pairs.append((name, '' if secret else value))
    url = urlunsplit((parsed.scheme, authority, parsed.path, urlencode(pairs), ''))
    return url, parameters


# LLM: 未知动作和秘密裸selector在整批落盘前拒绝；只复制规范字段，秘密CSS值属性和额外认证字段不持久化。
# 函数用途: 将一个UI导入动作转换为安全动作记录。
def _normalize_action(raw, seq):
    action_type = raw.get('action_type', '')
    if action_type not in _ACTION_TYPES:
        raise ValueError('RECORDER_ACTION_TYPE_INVALID')
    secret = _secret_action(raw)
    selector = _safe_selector(raw, secret)
    parameterized = action_type in {'goto', 'fill', 'select'} or secret or bool(raw.get('is_parameterized'))
    param_name = raw.get('param_name') or ('start_url' if seq == 1 and action_type == 'goto' else f'url_{seq}' if action_type == 'goto' else f'input_{seq}')
    param_name = _parameter_name(param_name) if parameterized or raw.get('param_name') else ''
    url, query = _navigation(raw, seq) if action_type == 'goto' else ('', [])
    if action_type not in {'goto', 'screenshot'} and not selector:
        raise ValueError('RECORDER_SELECTOR_REQUIRED')
    return asdict(RecordedBrowserAction(
        seq=seq, action_type=action_type, selector=selector, url=url,
        value='' if secret else str(raw.get('value', '')), param_name=param_name,
        is_parameterized=parameterized, is_secret=secret,
        description=_strip_defaults(_redact(str(raw.get('description', ''))), [str(raw.get('value', ''))] if secret else []),
        timestamp=datetime.now(timezone.utc).isoformat(), query_parameters=query))


# LLM: 参数身份按动作显式名称保序，重复名称拒绝以免秘密和普通字段相互覆盖。
# 函数用途: 从安全动作轨迹生成可编辑的参数列表。
def _parameters(actions):
    parameters = []
    for action in actions:
        if action['is_parameterized']:
            parameters.append({'param_name': action['param_name'], 'type': 'string',
                               'default_value': '' if action['is_secret'] else action['url'] or action['value'],
                               'secret': action['is_secret'], 'source_action_seq': action['seq']})
        parameters.extend(action['query_parameters'])
    names = [item['param_name'] for item in parameters]
    if len(set(names)) != len(names) or 'dry_run' in names:
        raise ValueError('RECORDER_PARAMETER_DUPLICATE')
    return parameters


# LLM: 先全批规范化再修改副本，失败不保存半批动作；不执行网络/浏览器操作。
# 函数用途: 给活动记录追加动作并更新参数、修订号。
def _append(session, actions):
    if session['status'] != 'RECORDING':
        raise ValueError('RECORDER_STATE_NOT_RECORDING')
    offset = len(session['action_trace'])
    normalized = [_normalize_action(raw, offset + index) for index, raw in enumerate(actions, 1)]
    trace = [*session['action_trace'], *normalized]
    parameters = _parameters(trace)
    return {**session, 'action_trace': trace, 'detected_parameters': parameters, 'revision': session['revision'] + 1}


# LLM: 支持title/thread_id/workspace_root/headless/initial_actions；此服务创建动作接收记录，不启动未接线的浏览器。
# 函数用途: 创建录制会话，加入起始导航并持久化安全初始动作。
def start_recording_session(owner_home: Path, *, start_url: str, title: str = '', **options) -> dict[str, object]:
    if options.keys() - {'thread_id', 'workspace_root', 'headless', 'initial_actions'}:
        raise ValueError('RECORDER_OPTIONS_INVALID')
    _url(start_url)
    actions = list(options.get('initial_actions') or [])
    if not actions or actions[0].get('action_type') != 'goto' or actions[0].get('url') != start_url:
        actions.insert(0, {'action_type': 'goto', 'url': start_url})
    safe_url, _ = _navigation({'url': start_url}, 1)
    session = asdict(RecordingSessionRecord(
        recording_id='rec_' + uuid4().hex[:12], start_url=safe_url, title=_redact(title),
        thread_id=str(options.get('thread_id', '')), workspace_root=str(options.get('workspace_root') or ''),
        headless=options.get('headless', True), created_at=datetime.now(timezone.utc).isoformat()))
    session = _append(session, actions)
    session['revision'] = 1
    with _record_lock(owner_home, _RECORDINGS):
        _write_record(owner_home, _RECORDINGS, session, 'recording_id')
    return session


# LLM: 记录ID单叶且持久状态严格校验；读口不创建目录、不恢复历史秘密。
# 函数用途: 获取当前Owner的一次录制详情。
def get_recording_session(owner_home: Path, recording_id: str) -> dict[str, object]:
    value = _read_record(owner_home, _RECORDINGS, recording_id, 'recording_id')
    if value.get('status') not in {'RECORDING', 'STOPPED', 'COMPILED'}:
        raise ValueError('RECORDER_STATE_INVALID')
    if not isinstance(value.get('action_trace'), list) or value['revision'] < 1:
        raise ValueError('RECORDER_RECORD_INVALID')
    return value


# LLM: 只列当前Owner规范目录，未知文件不作录制；坏记录明确上抛而非报告空列表。
# 函数用途: 列出持久化录制会话。
def list_recording_sessions(owner_home: Path) -> list[dict[str, object]]:
    try:
        names = list_names_beneath(owner_home, ('data', _RECORDINGS))
    except FileNotFoundError:
        return []
    return [get_recording_session(owner_home, name[:-5]) for name in sorted(names) if name.startswith('rec_') and name.endswith('.json')]


# LLM: 同一记录锁覆盖读改写，不能在STOPPED/COMPILED后继续追加，失败保原修订。
# 函数用途: 保存UI或受控浏览器提交的一批动作。
def append_recorded_actions(owner_home: Path, recording_id: str, actions: Sequence[Mapping[str, object]]) -> dict[str, object]:
    with _record_lock(owner_home, _RECORDINGS):
        value = _append(get_recording_session(owner_home, recording_id), actions)
        _write_record(owner_home, _RECORDINGS, value, 'recording_id')
    return value


# LLM: 最终动作与停止状态一次写入，重复无最终动作停止幂等；不声称采集器或浏览器已停止。
# 函数用途: 冻结动作轨迹，返回参数发现结果供预览。
def stop_recording_session(owner_home: Path, recording_id: str, *, final_actions: Sequence[Mapping[str, object]] | None = None) -> dict[str, object]:
    with _record_lock(owner_home, _RECORDINGS):
        value = get_recording_session(owner_home, recording_id)
        if value['status'] != 'RECORDING' and final_actions is None:
            return value
        value = _append(value, final_actions or [])
        value['status'] = 'STOPPED'
        _write_record(owner_home, _RECORDINGS, value, 'recording_id')
    return value


# LLM: 运行代码固定，录制数据仅通过repr常量嵌入；所有导航包括运行时参数和重定向均限定原origin。
# 模块用途: 生成脚本共用的确定性参数解析、干跑与Playwright执行体。
_RUNTIME_SOURCE = '''
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, quote, quote_plus
import ipaddress
import re
import socket

# LLM: DNS返回值也按地址语义检查，不用字符串前缀猜内网；CGN等非全球地址同样不作为公网。
# 函数用途: 识别真实连接不可使用的特殊用途地址。
def _unsafe_address(value):
    address = ipaddress.ip_address(value)
    return (not address.is_global or address.is_loopback or address.is_private or address.is_link_local
            or address.is_unspecified or address.is_multicast or address.is_reserved)

# LLM: 全部解析结果均须安全，空结果/格式错误/DNS错误都失败关闭；不永久缓存安全结论。
# 函数用途: 在真实请求边界取得通过公网校验的IP集合。
def _resolved_public_addresses(host, port):
    try:
        rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(row[4][0] for row in rows))
        if not addresses or any(_unsafe_address(address) for address in addresses):
            raise ValueError("RECORDER_UNSAFE_URL")
    except (OSError, ValueError, TypeError, IndexError):
        raise ValueError("RECORDER_UNSAFE_URL") from None
    return addresses

# LLM: origin和字面IP每次验证；network=True才解析DNS，只有内存参数解析和干跑明确使用False。
# 函数用途: 拒绝跨域、认证URL、特殊IP及运行时解析到内网的目标。
def _check_url(value, *, network=True):
    try:
        parts = urlsplit(value)
        origin = (parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
        safe = (isinstance(value, str) and parts.scheme in {"http", "https"} and parts.port != 0
                and parts.hostname and not parts.username and not parts.password
                and not any(ord(c) < 32 for c in value) and "\\\\" not in value and origin in ALLOWED_ORIGINS)
    except (TypeError, ValueError):
        safe = False
    if not safe:
        raise ValueError("RECORDER_UNSAFE_URL")
    host = parts.hostname.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local")):
        raise ValueError("RECORDER_UNSAFE_URL")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and _unsafe_address(literal):
        raise ValueError("RECORDER_UNSAFE_URL")
    if network:
        _resolved_public_addresses(host, origin[2])
    return value

# LLM: 浏览器DNS固定到已核公网地址并禁代理，避免检查后由Chromium第二次解析连到新内网IP；route仍逐次复核。
# 函数用途: 为允许域名生成固定解析规则，未知域名默认不可解析。
def _network_args():
    pins = {}
    for _, host, port in ALLOWED_ORIGINS:
        address = _resolved_public_addresses(host, port)[0]
        try:
            ipaddress.ip_address(host)
        except ValueError:
            canonical = host.rstrip(".").encode("idna").decode("ascii")
            if not re.fullmatch(r"[A-Za-z0-9.-]+", canonical):
                raise ValueError("RECORDER_UNSAFE_URL")
            pins[canonical] = "[" + address + "]" if ":" in address else address
    rules = ["MAP " + host + " " + address for host, address in pins.items()]
    rules.append("MAP * ~NOTFOUND")
    return ["--host-resolver-rules=" + ", ".join(rules), "--no-proxy-server", "--disable-quic"]

# LLM: 秘密必须来自inputs，默认值不能替代；内存动作解析只验静态边界，真实DNS在执行前另验。
# 函数用途: 解析参数化动作并校验动态URL。
def _resolve(inputs):
    values = {}
    for param in PARAMETERS:
        name = param["param_name"]
        if param["secret"] and (name not in inputs or not isinstance(inputs[name], str) or not inputs[name]):
            raise ValueError("SECRET_INPUT_REQUIRED: " + name)
        values[name] = str(inputs.get(name, param["default_value"]))
    steps = []
    for original in STEPS:
        step = dict(original)
        if step["is_parameterized"]:
            key = "url" if step["action_type"] == "goto" else "value"
            step[key] = values[step["param_name"]]
        if step["action_type"] == "goto":
            parts = urlsplit(_check_url(step["url"], network=False))
            pairs = list(parse_qsl(parts.query, keep_blank_values=True))
            for query in step["query_parameters"]:
                pair = (query["query_name"], values[query["param_name"]])
                index = query["query_index"]
                if index < len(pairs):
                    pairs[index] = pair
                else:
                    pairs.append(pair)
            step["url"] = _check_url(urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), "")), network=False)
        steps.append(step)
    return steps, [values[p["param_name"]] for p in PARAMETERS if p["secret"]]

# LLM: 字面秘密在所有公开结果中剥离，避免页面反射、干跑或visited_urls泄漏。
# 函数用途: 递归清理编译脚本输出。
def _public(value, secrets):
    if isinstance(value, dict):
        return {key: _public(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_public(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in sorted(secrets, key=len, reverse=True):
            if secret:
                for form in sorted({secret, quote(secret, safe=""), quote_plus(secret)}, key=len, reverse=True):
                    value = value.replace(form, "***REDACTED***")
        value = re.sub(r"(?i)([?&](?:password|token|secret|api_key|otp)=)[^&#]*", r"\\1***REDACTED***", value)
    return value

# LLM: route在每次HTTP请求发出前重验origin和全部DNS地址；失败只abort，不把旧安全结果作为永久票据。
# 函数用途: 限制重定向、子资源及页面脚本请求的目标范围。
def _route(route):
    try:
        _check_url(route.request.url)
    except ValueError:
        route.abort()
    else:
        route.continue_()

# LLM: Playwright route不能保证覆盖重定向后续请求；CDP在当前页面每一跳真正发送前暂停并重新核origin/DNS。
# 函数用途: 对允许请求继续发送，对跨域或私网请求发出协议级阻断。
def _request_paused(client, event):
    request_id = event["requestId"]
    try:
        _check_url(event["request"]["url"])
    except (ValueError, TypeError, KeyError):
        client.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"})
    else:
        client.send("Fetch.continueRequest", {"requestId": request_id})

# LLM: 仅附着本次回放页面的Chromium会话，首次导航前启用请求阶段暂停；不以导航后的page.url代替前置门。
# 函数用途: 安装独立于Playwright重定向路由行为的HTTP请求安全门。
def _install_request_guard(context, page):
    client = context.new_cdp_session(page)
    client.on("Fetch.requestPaused", lambda event: _request_paused(client, event))
    client.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
    return client

# LLM: 所有录制字段只作为Playwright参数，不eval动作正文；截图仅返回摘要，不写任意路径。
# 函数用途: 执行一项支持的浏览器操作。
def _perform(page, step, data):
    action, selector = step["action_type"], step["selector"]
    if action == "goto":
        page.goto(_check_url(step["url"]))
    elif action == "click":
        page.locator(selector).click()
    elif action == "fill":
        page.locator(selector).fill(step["value"])
    elif action == "select":
        page.locator(selector).select_option(step["value"])
    elif action == "press":
        page.locator(selector).press(step["value"])
    elif action == "wait_for_selector":
        page.wait_for_selector(selector)
    elif action == "extract_text":
        data[step["param_name"] or "text_" + str(step["seq"])] = page.locator(selector).inner_text()
    elif action == "extract_table":
        data[step["param_name"] or "table_" + str(step["seq"])] = page.locator(selector).evaluate(
            "node => Array.from(node.querySelectorAll('tr'), row => Array.from(row.querySelectorAll('th,td'), cell => cell.innerText))")
    elif action == "screenshot":
        image = page.screenshot()
        data["screenshot_" + str(step["seq"])] = {"bytes": len(image)}
    else:
        raise ValueError("RECORDER_ACTION_TYPE_INVALID")

# LLM: 干跑不解析DNS/导入浏览器；真实启动先固定安全DNS，浏览器异常不回传秘密。
# 函数用途: 以参数回放动作，返回已完成步数及脱敏提取数据。
def main(inputs: dict) -> dict:
    steps, secrets = _resolve(inputs)
    result = {"ok": True, "visited_urls": [], "extracted_data": {}, "steps_completed": 0}
    if inputs.get("dry_run", False):
        return _public({**result, "planned_steps": steps}, secrets)
    network_args = _network_args()
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=HEADLESS, args=network_args)
        try:
            context = browser.new_context(service_workers="block", accept_downloads=False)
            context.route("**/*", _route)
            page = context.new_page()
            _install_request_guard(context, page)
            for step in steps:
                _perform(page, step, result["extracted_data"])
                if isinstance(page.url, str):
                    _check_url(page.url)
                if step["action_type"] == "goto":
                    result["visited_urls"].append(step["url"])
                result["steps_completed"] += 1
        except Exception:
            raise RuntimeError("RECORDER_PLAYBACK_FAILED") from None
        finally:
            browser.close()
    return _public(result, secrets)
'''


# LLM: 参数编辑不可降secret；改名与值槽定向同步，禁止按秘密子串遍历结构键、动作类型或参数身份。
# 函数用途: 生成编译用轨迹、参数列表和输入schema。
def _compile_parameters(session, overrides):
    actions = json.loads(json.dumps(session['action_trace']))
    original = _parameters(actions)
    if overrides.keys() - {item['param_name'] for item in original}:
        raise ValueError('RECORDER_PARAMETER_NOT_FOUND')
    parameters, properties, required, names = [], {'dry_run': {'type': 'boolean', 'default': False}}, [], {}
    for param in original:
        changes = overrides.get(param['param_name'], {})
        name = _parameter_name(changes.get('param_name', param['param_name']))
        default = str(changes.get('default_value', param['default_value']))
        secret = bool(param['secret'] or changes.get('secret') or _SECRET_NAME.search(name) or _redact(default) != default)
        value = '' if secret else default
        if name in properties:
            raise ValueError('RECORDER_PARAMETER_DUPLICATE')
        parameters.append({**param, 'param_name': name, 'default_value': value, 'secret': secret})
        properties[name] = {'type': 'string', 'secret': secret, **({} if secret else {'default': value})}
        if secret:
            required.append(name)
        names[param['param_name']] = name
    _rebind_actions(actions, names, parameters)
    for param in parameters:
        if not param['secret']:
            properties[param['param_name']]['default'] = param['default_value']
    return actions, parameters, {'type': 'object', 'properties': properties, 'required': required}


# LLM: secret升级后原普通默认值也必须从URL和其它常量清除，不将丢弃值写进生成代码。
# 函数用途: 清理编译结构中已被用户声明为秘密的旧默认值。
def _strip_defaults(value, discarded):
    if isinstance(value, str):
        for secret in discarded:
            if secret:
                for form in sorted({secret, quote(secret, safe=''), quote_plus(secret)}, key=len, reverse=True):
                    value = value.replace(form, '')
        return value
    if isinstance(value, list):
        return [_strip_defaults(item, discarded) for item in value]
    if isinstance(value, dict):
        return {key: _strip_defaults(item, discarded) for key, item in value.items()}
    return value


# LLM: 只处理明确查询位置和原键，保留路径、其它查询值及名称；不做全字符串秘密子串替换。
# 函数用途: 将URL中已声明秘密的查询槽位清空。
def _blank_secret_queries(url, queries):
    if not url:
        return url
    parsed = urlsplit(url)
    pairs = list(parse_qsl(parsed.query, keep_blank_values=True))
    for query in queries:
        index = query['query_index']
        if query['secret'] and index < len(pairs) and pairs[index][0] == query['query_name']:
            pairs[index] = (pairs[index][0], '')
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(pairs), parsed.fragment))


# LLM: 仅按参数身份改名、清值；secret升级重清旧selector/说明，导航查询按位置清理且同步URL默认值。
# 函数用途: 将编辑后的参数定向绑定回动作和默认值，不破坏结构字段。
def _rebind_actions(actions, names, parameters):
    mapped = {param['param_name']: param for param in parameters}
    for action in actions:
        if action['is_parameterized']:
            name = names[action['param_name']]
            action.update(param_name=name, is_secret=mapped[name]['secret'])
            if action['is_secret']:
                action['selector'] = _safe_selector(action, True)
                action['description'] = _strip_defaults(action['description'], [action['value']])
                action['value'] = ''
        for query in action['query_parameters']:
            name = names[query['param_name']]
            query.update(mapped[name])
        if action['action_type'] == 'goto':
            action['url'] = '' if action['is_secret'] else _blank_secret_queries(action['url'], action['query_parameters'])
            param = mapped[action['param_name']]
            if not param['secret']:
                param['default_value'] = _blank_secret_queries(param['default_value'], action['query_parameters'])


# LLM: 原始录制URL决定网络上界，编辑或运行时参数不能扩域；常量repr安全转义后完整AST检查。
# 函数用途: 从安全轨迹生成可独立运行的Python脚本。
def _compile_spec(session, script_id, title, overrides):
    actions, parameters, schema = _compile_parameters(session, overrides)
    origins = list(dict.fromkeys((p.scheme, p.hostname, p.port or (443 if p.scheme == 'https' else 80))
                                for action in session['action_trace'] if action['action_type'] == 'goto' for p in [_url(action['url'])]))
    source = ('# LLM: 所有动作都是安全常量，不是代码插值；运行时仍须原宿主权限门。\n'
              '# 模块用途: 参数化回放用户确认的浏览器动作。\n'
              f'STEPS = {actions!r}\nPARAMETERS = {parameters!r}\nALLOWED_ORIGINS = {origins!r}\nHEADLESS = {session["headless"]!r}\n' + _RUNTIME_SOURCE)
    ast.parse(source)
    targets = [urlunsplit((scheme, f'[{host}]:{port}' if ':' in host else f'{host}:{port}', '', '', '')) for scheme, host, port in origins]
    output = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}, 'visited_urls': {'type': 'array', 'items': {'type': 'string'}},
                                             'extracted_data': {'type': 'object'}, 'steps_completed': {'type': 'integer'}}}
    return _script_spec(script_id, title, source, schema, output, targets)


# LLM: 输入是已读取的内存快照；状态、schema、AST沿原合同验证，此函数不取锁、不写盘或启动浏览器。
# 函数用途: 纯内存构建脚本和可选工作流、Skill预览。
def _compile_preview(session, script_id, title, overrides, options):
    if session['status'] not in {'STOPPED', 'COMPILED'}:
        raise ValueError('RECORDER_STATE_NOT_STOPPED')
    script_id = script_id or 'browser_' + session['recording_id'][4:]
    title = _redact(title or session['title'] or '浏览器录制回放')
    spec = _compile_spec(session, script_id, title, overrides)
    result = {'script_spec': spec}
    if options.get('generate_workflow'):
        result['workflow_spec'] = _workflow_spec(script_id, title, [spec])
    if options.get('generate_skill'):
        result['skill_markdown'] = _skill_markdown(script_id, title, f'1. 核对录制动作与访问范围。\n2. 提供参数及安全凭据后执行脚本 {script_id}。')
    return result


# LLM: 默认或显式save_to_store=False只读原记录并纯内存编译；只有显式True才锁内保存资产和COMPILED修订。
# 函数用途: 预览录制编译结果，或经明确保存选项提交脚本和会话状态。
def compile_recording_to_script(owner_home: Path, *, recording_id: str, script_id: str = '', title: str = '',
                                parameter_overrides: Mapping[str, Mapping[str, object]] | None = None, **options) -> dict[str, object]:
    if options.keys() - {'save_to_store', 'scope', 'workspace_root', 'generate_workflow', 'generate_skill'} or type(options.get('save_to_store', False)) is not bool:
        raise ValueError('RECORDER_OPTIONS_INVALID')
    if not options.get('save_to_store', False):
        return _compile_preview(get_recording_session(owner_home, recording_id), script_id, title, parameter_overrides or {}, options)
    with _record_lock(owner_home, _RECORDINGS):
        session = get_recording_session(owner_home, recording_id)
        result = _compile_preview(session, script_id, title, parameter_overrides or {}, options)
        spec = result['script_spec']
        root = _asset_root(owner_home, options.get('scope', 'owner'), options.get('workspace_root'))
        ScriptStore(root).save_script(AssetDescriptor.from_dict(spec['descriptor']), spec['entry_source'])
        session.update(status='COMPILED', compiled_script_id=spec['script_id'], revision=session['revision'] + 1)
        _write_record(owner_home, _RECORDINGS, session, 'recording_id')
    return result
