"""线程详细档位唯一存储；客户端默认来源只影响展示，不改变模型或授权。"""


def tui_streaming_client(request):
    capabilities = request.get('client_capabilities') if isinstance(request, dict) else None
    return isinstance(capabilities, dict) and capabilities.get('tui_streaming') is True


def initial_verbose_settings(config, request):
    if not tui_streaming_client(request):
        return {}
    level = getattr(config, 'tui_default_verbose_level', 'stream')
    if level not in {'off', 'on', 'full', 'stream'}:
        raise ValueError('tui_default_verbose_level 必须是 off/on/full/stream')
    return {'verbose_level': level, 'verbose_level_source': 'tui_default'}


def effective_verbose_level(thread, request):
    if (getattr(thread, 'metadata', {}).get('verbose_level_source') == 'tui_default'
            and not tui_streaming_client(request)):
        return 'off'
    return str(getattr(thread, 'verbose_level', 'off') or 'off')


def inherited_verbose_metadata(thread):
    return ({'verbose_level_source': 'tui_default'}
            if thread.metadata.get('verbose_level_source') == 'tui_default' else {})
