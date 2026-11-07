"""把已认证 owner 的独立聊天请求接入原模型目录、准入和调用账。"""
from __future__ import annotations

import hashlib
import time
import uuid
from contextvars import Context
from dataclasses import replace

from ..backends.base import ProviderRequestOptions
from ..backends.provider_headers import _SESSION
from ..conversation.auxiliary_model_call import (
    AuxiliaryModelCallRequest,
    generate_auxiliary_model_response,
)
from ..gateway_parts.request_worker import AdmissionLimits, admission
from ..plugin_host.errors import HostAPIError
from ..plugin_host.schemas import validate_object
from ..settings.model_profiles import (
    ModelProfileError,
    execute_model_profile_operation,
    selected_model_config,
)
from ..settings.model_scope import (
    ModelScopeDependencies,
    model_dependencies_scope,
    prepare_model_dependencies,
)

try:
    from .gap158_limits import (
        LLM_GATEWAY_TEST_MAX_OUTPUT_TOKENS,
        OPENAI_COMPAT_MAX_MESSAGES,
        OPENAI_COMPAT_STREAM_CHUNK_CHARS,
    )
except ImportError:
    # W0 尚未合入时使用冻结契约的默认值；合入后唯一来源自动恢复为 limits 模块。
    (LLM_GATEWAY_TEST_MAX_OUTPUT_TOKENS, OPENAI_COMPAT_MAX_MESSAGES,
     OPENAI_COMPAT_STREAM_CHUNK_CHARS) = (16, 512, 2048)


def affinity_key(owner_id, value):
    """客户端会话值只生成 owner 范围哈希，不授予会话或其他 owner 的访问权。"""
    if not isinstance(value, str) or len(value) > 128 or any(ord(c) < 32 for c in value):
        raise HostAPIError('INVALID_SCHEMA', 400)
    return hashlib.sha256((owner_id + ':' + value).encode()).hexdigest() if value else ''


def validate_request(body):
    """在任何模型调用前校验闭合 DTO；流式运输选项不进入业务参数。"""
    from ..gateway_parts.business_api_v13 import v13_schema
    from ..gateway_parts.business_api_v13_gap158_w1 import CHAT_PARAMS

    if not isinstance(body, dict):
        raise HostAPIError('INVALID_SCHEMA', 400)
    clean = dict(body)
    stream = clean.pop('stream', False)
    n = clean.pop('n', 1)
    if type(stream) is not bool or type(n) is not int or n != 1 or 'tools' in clean or 'functions' in clean:
        raise HostAPIError('INVALID_SCHEMA', 400)
    clean = validate_object(clean, v13_schema(CHAT_PARAMS))
    if not clean['messages'] or len(clean['messages']) > OPENAI_COMPAT_MAX_MESSAGES:
        raise HostAPIError('INVALID_SCHEMA', 400)
    return clean, stream


def resolve_profile(agent, requested=None):
    """仅从本人 /model 的可用聊天档案解析编号或唯一名称，不接受目录外供应商模型名。"""
    try:
        listing = execute_model_profile_operation(agent, 'list', {})
        requested = requested or listing.get('selected')
        rows = [row for row in listing.get('profiles', []) if row.get('available', False)]
        exact = [row for row in rows if row['id'] == requested]
        matches = exact or [row for row in rows if requested in (row.get('model_name'), row.get('display_name'))]
        if len(matches) != 1:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        profile_id = matches[0]['id']
        return profile_id, selected_model_config(agent, profile_id=profile_id)
    except ModelProfileError:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404) from None


class _CallBackend:
    """请求局部适配器只覆盖亲和头和输出上限，不改共享 backend 或其他会话绑定。"""
    def __init__(self, backend, affinity, output_cap):
        if not affinity and getattr(backend, 'session_header', ''):
            from copy import copy

            # 空会话必须连动态头名称一起关闭；只改本次副本，保留共享后端原配置和严格校验。
            backend = copy(backend)
            backend.session_header = ''
        self.backend, self.affinity, self.output_cap = backend, affinity, output_cap

    def __getattr__(self, name):
        return getattr(self.backend, name)

    def generate(self, prompt, *, messages=None, on_chunk=None, request_options=None):
        from ..conversation.auxiliary_model_call import _accepts_keyword

        # 本接口只接受普通消息生成的三个请求级选项，不扩张为任意后端参数入口。
        kwargs = {'messages': messages} if messages is not None else {}
        options = request_options or ProviderRequestOptions()
        if self.output_cap is not None:
            options = replace(options, max_output_tokens=self.output_cap)
        if _accepts_keyword(self.backend.generate, 'request_options'):
            kwargs['request_options'] = options
        if on_chunk is not None and _accepts_keyword(self.backend.generate, 'on_chunk'):
            kwargs['on_chunk'] = on_chunk
        # auxiliary 的通用 scope 会生成临时会话；在真实发送边界恢复本接口明确的有/无亲和语义。
        token = _SESSION.set(self.affinity)
        try:
            return self.backend.generate(prompt, **kwargs)
        finally:
            _SESSION.reset(token)


def execute(owner, body, headers=None, *, request_id='', admission_key='', quota_config=None):
    """在全新 Context 中使用准确 owner 的原账本；所有退出路径释放原 Gateway 准入槽。"""
    clean, _stream = validate_request(body)
    request_id = request_id or uuid.uuid4().hex
    headers = headers or {}
    value = headers.get('X-Session-Id', clean.get('thread_affinity', ''))
    affinity = affinity_key(owner.home_paths.owner_id, value)
    limits = AdmissionLimits.from_config(quota_config or owner.config)
    key = admission_key or owner.home_paths.owner_id
    reason = admission.try_acquire_report(key, limits.user_inflight, limits.global_inflight,
        owner_limit=limits.owner_inflight)
    if reason:
        raise HostAPIError('RATE_LIMITED', 429)
    try:
        return Context().run(_generate, owner, clean, affinity, request_id)
    finally:
        admission.release(key)


def _generate(agent, body, affinity, request_id):
    """冻结档案及本次采样参数，复用原辅助调用记账；不创建/选择/改写聊天线程。"""
    profile_id, config = resolve_profile(agent, body['model'])
    overrides = {name: body[name] for name in ('temperature', 'top_p') if name in body}
    cap = body.get('max_tokens')
    if cap is not None:
        configured = getattr(config, 'max_tokens', 0)
        cap = min(cap, configured) if configured and configured > 0 else cap
        overrides['max_tokens'] = cap
    if overrides:
        config = replace(config, **overrides)
    dependencies = prepare_model_dependencies(agent, config)
    if dependencies is None:
        # 轻量宿主可通过调用局部视图使用同一 ledger；不向原对象热写 config/backend。
        from copy import copy
        local = copy(agent)
        local.config = config
        local.backend = _CallBackend(agent.backend, affinity, cap)
        return _invoke(local, body, profile_id, request_id)
    values = dict(dependencies.values)
    values['backend'] = _CallBackend(values['backend'], affinity, cap)
    bound = ModelScopeDependencies(agent, tuple(values.items()), dependencies.selection)
    with model_dependencies_scope(agent, bound):
        return _invoke(agent, body, profile_id, request_id)


def _invoke(agent, body, profile_id, request_id):
    from ..agent_core.model.call_runtime import (
        input_token_usage,
        output_token_usage,
        reported_cache_read_token_usage,
    )

    response = generate_auxiliary_model_response(AuxiliaryModelCallRequest(
        agent=agent, prompt='', messages=body['messages'], request_id=request_id, purpose='openai_compat'))
    text = str(getattr(response, 'text', '') or '')
    for stop in body.get('stop', []):
        position = text.find(stop)
        if position >= 0:
            text = text[:position]
    prompt_tokens = max(0, int(input_token_usage(response) or 0))
    output_tokens = max(0, int(output_token_usage(response) or 0))
    cached = max(0, int(reported_cache_read_token_usage(response) or 0))
    finish = ('length' if getattr(response, 'truncated', False)
              or getattr(response, 'stop_reason', '') in {'max_tokens', 'length'} else 'stop')
    return {'id': 'chatcmpl-' + request_id, 'object': 'chat.completion', 'created': int(time.time()),
        'model': body['model'], 'model_profile_id': profile_id,
        'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': text}, 'finish_reason': finish}],
        'usage': {'prompt_tokens': prompt_tokens, 'completion_tokens': output_tokens,
                  'total_tokens': prompt_tokens + output_tokens, 'cached_tokens': cached}}


def test_gateway(owner, params=None, *, headers=None, request_id='', admission_key='', quota_config=None):
    """HTTP 和业务动作共用的微小连接测试；正文只随响应返回，不写消息或诊断 metadata。"""
    params = params or {}
    profile_id, config = resolve_profile(owner, params.get('model_profile_id'))
    started = time.monotonic()
    result = execute(owner, {'model': profile_id, 'messages': [{'role': 'user', 'content': 'Reply OK.'}],
        'max_tokens': LLM_GATEWAY_TEST_MAX_OUTPUT_TOKENS}, headers,
        request_id=request_id, admission_key=admission_key, quota_config=quota_config)
    return {'ok': True, 'model': config.model_name, 'model_profile_id': profile_id,
        'latency_ms': max(0, int((time.monotonic() - started) * 1000)),
        'output_preview': result['choices'][0]['message']['content'][:500]}


def stream_frames(result):
    """辅助调用无增量文本回调，因此只对完成结果做有界分块，运输层必须标明 buffered。"""
    import json

    base = {key: result[key] for key in ('id', 'created', 'model')}
    base['object'] = 'chat.completion.chunk'
    choice = result['choices'][0]
    deltas = [{'role': 'assistant'}]
    text = choice['message']['content']
    deltas.extend({'content': text[i:i + OPENAI_COMPAT_STREAM_CHUNK_CHARS]}
        for i in range(0, len(text), OPENAI_COMPAT_STREAM_CHUNK_CHARS))
    for delta in deltas:
        yield ('data: ' + json.dumps({**base, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
            ensure_ascii=False) + '\n\n').encode()
    yield ('data: ' + json.dumps({**base, 'choices': [{'index': 0, 'delta': {},
        'finish_reason': choice['finish_reason']}]}, ensure_ascii=False) + '\n\n').encode()
    yield b'data: [DONE]\n\n'
