# LLM: 来源秘密唯一在受权 HTTP 出口注入；raw env 永不读取，文件使用当前 owner 与原只读上下文/严格 FD，TaskCredential 沿原 scope 校验。
# 模块用途: 运输一次来源请求的宿主授权、注入已获准秘密并清理回显；不创建凭据账、缓存或新的权限名单。
from __future__ import annotations

import json
from copy import copy
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..common.nofollow_fs import read_bytes_beneath
from ..tooling.models import ToolHandlerOutcome, ToolInvocationContext
from ..tooling.web_fetch_runtime import RawResponseParts
from ..tooling.web_fetch_tools import _redact_secret_echo, _secret_echo_variants
from ..tooling.web_http_helpers import normalize_headers
from ..user_space.task_credentials import credential_headers, credential_origin
from .source_http import SourceHttpRequest, _apply_value_binding, _normalize_secret_bindings


# LLM: 仅正式 execute_scoped 构造，原执行回调与只读范围保持同次快照；agent 副本固定原 root/actor，不借最新前台身份。
# 类用途: 保存来源请求本次已授权的宿主上下文，供同步请求和原收割 closure 复核。
@dataclass(frozen=True)
class SourceRequestAuthority:
    agent: object
    owner_root: Path
    context: ToolInvocationContext
    network_enabled: bool = True

    # LLM: scope 副本只运输原事实，不创建/修复 TaskLink；未知当前执行不产生私人凭据权。
    # 函数用途: 从正式调用冻结准确 owner 和当前执行，不读取任何秘密。
    @classmethod
    def from_invocation(cls, agent, context, *, network_enabled=True):
        if not isinstance(context, ToolInvocationContext) or context.workspace_read_context is None:
            raise ValueError('来源缺少宿主读取授权')
        root = Path(agent.home_paths.owner_home_dir).resolve(strict=True)
        scoped = copy(agent)
        current = getattr(agent, '_current_run_params', None)
        if current is not None:
            current = copy(current)
            current.task_attributes = dict(getattr(current, 'task_attributes', None) or {})
        scoped._current_run_params = current
        return cls(scoped, root, context, network_enabled)

    # LLM: 当前执行权可能在读取后失效；每个真实请求和每一跳都复核原回调，不从 source 文本补授权。
    # 函数用途: 阻止过期、停止或已换代的执行继续使用旧秘密。
    def recheck(self):
        if not self.network_enabled:
            raise ValueError('来源网络未获原父级授权')
        if self.context.cancellation_token is not None and self.context.cancellation_token.cancelled:
            raise ValueError('来源原执行已取消')
        if self.context.execution_authority_check is not None:
            try:
                self.context.execution_authority_check()
            except Exception as exc:  # noqa: BLE001 - 原权威门任何故障均拒绝，错误正文不输出
                raise ValueError('来源原执行授权不可用') from exc


# LLM: 未解析 DTO 自身不能提供 owner/root/scope；所有引用在首个 HTTP 前整体核验，任一失败不发部分请求。
# 函数用途: 给合法请求注入本人文件或原任务凭据，返回需要清理的回显值，不写盘。
def authorize_source_request(request: SourceHttpRequest, authority: SourceRequestAuthority | None):
    if not request.secret_bindings and not request.credential_ref:
        return request, ()
    if not isinstance(authority, SourceRequestAuthority):
        raise ValueError('来源 SecretRef 缺少原宿主授权，请在当前任务重新绑定私人凭据')
    authority.recheck()
    headers = dict(request.headers)
    query = parse_qsl(urlsplit(request.url).query, keep_blank_values=True)
    body = json.loads(request.data) if request.data is not None else None
    secrets = []
    for binding in _normalize_secret_bindings(list(request.secret_bindings)):
        value = _authorized_file_value(authority, binding['secret_ref'])
        secrets.append(value)
        if binding['location'] == 'header':
            if binding['name'].casefold() in {name.casefold() for name in headers}:
                raise ValueError('来源认证绑定与普通请求头冲突')
            headers[binding['name']] = value
        else:
            query, body = _apply_value_binding(query, body, binding, value)
    if request.credential_ref:
        current = getattr(authority.agent, '_current_run_params', None)
        if current is None:
            raise ValueError('来源凭据没有原活动任务授权')
        if str(current.run_id or current.task_id) != authority.context.runtime_snapshot.run_id:
            raise ValueError('来源凭据与原工具执行身份不一致')
        private, values = credential_headers(authority.agent, request.credential_ref, request.url)
        if {key.casefold() for key in private} & {key.casefold() for key in headers}:
            raise ValueError('来源凭据与请求头冲突')
        headers.update(private)
        secrets.extend(values)
    parts = urlsplit(request.url)
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), parts.fragment))
    data = None if body is None else json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()
    authority.recheck()
    return replace(request, url=url, headers=normalize_headers(headers), data=data,
        secret_bindings=(), credential_ref=''), tuple(secrets)


# LLM: 文件 refs 不是特权读取；必须位于当前真实 owner 和原调用只读上界，FD 逐段不跟链接、最多8KiB，env 不在此提供者支持范围。
# 函数用途: 有界读取本用户已获准的凭据文件，任何 Full Access、raw root 或重放都不能读宿主环境变量。
def _authorized_file_value(authority, reference):
    if not reference.startswith('file:'):
        raise ValueError('来源不能读取宿主 env SecretRef，请改用 request_credentials 私人引用')
    context = authority.context.workspace_read_context
    raw = Path(reference[5:]).expanduser()
    target = (raw if raw.is_absolute() else context.cwd / raw).resolve(strict=True)
    if not context.check(target).allowed:
        raise ValueError('来源凭据文件不在原授权读取范围内')
    relative = target.relative_to(authority.owner_root)
    payload = read_bytes_beneath(authority.owner_root, relative.parts, max_bytes=8192, require_dir_fd=True)
    value = payload.decode('utf-8').strip() if payload is not None else ''
    if not value:
        raise ValueError('来源凭据文件为空或不可读取')
    return value


# LLM: 同一秘密只允许原 origin；认证来源不因重定向取得第二个目标授权，网络闸仍由原调用方逐跳执行。
# 函数用途: 对带秘密请求的每一跳复核 origin 及原执行权，不解析业务内容。
def authorized_source_hop(authority, original_url, candidate_url):
    authority.recheck()
    if credential_origin(original_url) != credential_origin(candidate_url):
        raise ValueError('来源凭据不能发送到其它 origin')


# LLM: 复用原 web_fetch 回显净化；必须在错误展示、JSON解析、spool与模型之前清理，不记录秘密或缓存原响应。
# 函数用途: 去除本次授权秘密在成功正文、响应头和错误中的常见编码回显。
def redact_source_response(response, secrets):
    if not secrets:
        return response
    if isinstance(response, ToolHandlerOutcome):
        return replace(response, output=_redact_secret_echo(response.output, secrets),
            result_envelope=_redact_secret_echo(response.result_envelope, secrets))
    if isinstance(response, RawResponseParts):
        body = response.body
        for value in _secret_echo_variants(secrets):
            body = body.replace(value.encode(), b'<redacted>')
        return replace(response, body=body,
            headers={key: _redact_secret_echo(value, secrets) for key, value in response.headers.items()})
    return response
