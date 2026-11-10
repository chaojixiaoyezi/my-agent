"""线程详细档位唯一存储；客户端默认来源只影响展示，不改变模型或授权。"""

VERBOSE_LEVEL_SOURCES = frozenset({'tui_default', 'feishu_default', 'user'})


def tui_streaming_client(request):
    capabilities = request.get('client_capabilities') if isinstance(request, dict) else None
    return isinstance(capabilities, dict) and capabilities.get('tui_streaming') is True


def _request_channel(request, channel):
    if channel:
        return channel
    if not isinstance(request, dict):
        return ''
    conversation = request.get('conversation')
    return request.get('channel') or (conversation.get('channel') if isinstance(conversation, dict) else '')


def initial_verbose_settings(config, request, *, channel=''):
    if tui_streaming_client(request):
        level = getattr(config, 'tui_default_verbose_level', 'stream')
        if level not in {'off', 'on', 'full', 'stream'}:
            raise ValueError('tui_default_verbose_level 必须是 off/on/full/stream')
        return {'verbose_level': level, 'verbose_level_source': 'tui_default'}
    if _request_channel(request, channel) == 'feishu':
        return {'verbose_level': 'stream', 'verbose_level_source': 'feishu_default'}
    return {}


def effective_verbose_level(thread, request, *, channel=''):
    source = getattr(thread, 'metadata', {}).get('verbose_level_source')
    if source == 'tui_default' and not tui_streaming_client(request):
        return 'off'
    if source == 'feishu_default' and _request_channel(request, channel) != 'feishu':
        return 'off'
    level = str(getattr(thread, 'verbose_level', 'off') or 'off')
    if source is None and level == 'off' and _request_channel(request, channel) == 'feishu':
        return 'stream'
    return level


def inherited_verbose_metadata(thread):
    source = thread.metadata.get('verbose_level_source')
    return {'verbose_level_source': source} if source in VERBOSE_LEVEL_SOURCES else {}
