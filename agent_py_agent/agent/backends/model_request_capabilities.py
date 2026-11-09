"""请求能力与effort正交；只消费显式档案及已核对的端点/协议/模型ID，不解析报错或展示名。"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

THINKING_REQUIREMENTS = ('auto', 'optional', 'required_adaptive')
# 官方模型调用与Anthropic兼容文档（2026-11-09）：M3.1不可关闭思考；兼容Messages只支持auto选择。
# https://platform.minimax.io/docs/guides/text-generation
# https://platform.minimax.io/docs/api-reference/text-anthropic-api
_REQUIRED_ADAPTIVE_IDS = frozenset({
    ('api.minimax.io', 'MiniMax-M3.1-Flash'),
    ('api.minimaxi.com', 'MiniMax-M3.1-Flash'),
    ('api.minimax.cn', 'MiniMax-M3.1-Flash'),
})
_ALL_TOOL_CHOICES = ('auto', 'none', 'required', 'specific')


@dataclass(frozen=True)
class ModelRequestCapabilities:
    thinking_requirement: str = 'optional'
    native_tool_choices: tuple[str, ...] = _ALL_TOOL_CHOICES


def normalize_thinking_requirement(value: object) -> str:
    text = str(value or 'auto').strip().lower()
    return text if text in THINKING_REQUIREMENTS else ''


def resolve_model_request_capabilities(declared: object, api_base: str, model: str, backend: str) -> ModelRequestCapabilities:
    from .errors import ProviderModelCapabilityError

    requirement = normalize_thinking_requirement(declared)
    if not requirement:
        raise ProviderModelCapabilityError('invalid_thinking_requirement')
    known_required = ((urlsplit(api_base).hostname or '').lower(), model) in _REQUIRED_ADAPTIVE_IDS
    if known_required and requirement == 'optional':
        raise ProviderModelCapabilityError('thinking_requirement_conflict')
    if requirement == 'auto':
        requirement = 'required_adaptive' if known_required else 'optional'
    choices = ('auto',) if known_required and backend == 'anthropic_compatible' else _ALL_TOOL_CHOICES
    return ModelRequestCapabilities(requirement, choices)


def requires_adaptive_thinking(backend: object) -> bool:
    return getattr(backend, 'thinking_requirement', 'optional') == 'required_adaptive'


def validate_thinking_request(backend: object, *, disabled: bool, effort: str = '') -> None:
    from .errors import ProviderModelCapabilityError

    if not requires_adaptive_thinking(backend):
        return
    if disabled or str(effort or '').strip().lower() in {'off', 'none', 'minimal'}:
        raise ProviderModelCapabilityError('thinking_disable_not_supported')
    if getattr(backend, 'reasoning_control', 'none') == 'budget':
        raise ProviderModelCapabilityError('adaptive_budget_not_supported')


def required_thinking_fields(backend: object, *, disabled: bool, effort: str = '', protocol: str) -> dict:
    validate_thinking_request(backend, disabled=disabled, effort=effort)
    if requires_adaptive_thinking(backend) and protocol in {'anthropic', 'openai'}:
        return {'thinking': {'type': 'adaptive'}}
    return {}
