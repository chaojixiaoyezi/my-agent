"""本人OAuth可见会话与历史；只复用原密文授权/刷新及受控CLI，不借租户身份。"""
from __future__ import annotations

import time
from urllib.parse import quote, urlencode

from .feishu_message_history import (
    ChannelMessageHistoryError,
    HistoryLimits,
    _decode_cursor,
    _encode_cursor,
    _identifier,
    _list_chat_messages,
    capability_config_for_agent,
    deployment_capability_config,
    resolve_effective_owner_policy,
    validate_history_query,
)


class UserHistoryIdentity:
    """每页重取同一owner/actor的原授权；token仅在本次调用栈，不进入结果或cursor。"""
    def __init__(self, agent):
        self.agent = agent
        self.identity = None
        self.binding = None

    def check(self):
        from ..conversation.tool_approval_scope import tool_approval_scope
        from ..user_space.feishu_oauth import _assert_network, oauth_actor, user_access_token
        from .feishu_cli import UserCloudIdentity

        home = self.agent.home_paths
        if getattr(home, 'owner_kind', '') == 'group':
            raise ChannelMessageHistoryError('FEISHU_USER_AUTH_REQUIRED')
        if (getattr(capability_config_for_agent(self.agent), 'feishu_chat_history_enabled', False) is not True
                or getattr(deployment_capability_config(home), 'feishu_chat_history_enabled', False) is not True):
            raise ChannelMessageHistoryError('CHANNEL_MESSAGES_DISABLED')
        if not resolve_effective_owner_policy(home).network_enabled:
            raise ChannelMessageHistoryError('NETWORK_DISABLED')
        if not deployment_capability_config(home).feishu_cli_backend_enabled:
            raise ChannelMessageHistoryError('FEISHU_USER_AUTH_REQUIRED')
        try:
            _assert_network(self.agent)
        except (OSError, ValueError, TypeError):
            raise ChannelMessageHistoryError('NETWORK_DISABLED') from None
        try:
            token = user_access_token(self.agent)
            current = self.agent._current_run_params
            scope = tool_approval_scope(self.agent, str(current.run_id or current.task_id))
            actor = oauth_actor(self.agent, scope)
            self.identity = UserCloudIdentity(self.agent.config.feishu_app_id, self.agent.config.feishu_app_secret, token, actor)
            binding = ('user', home.owner_id, self.identity.app_id, actor)
            if self.binding is not None and binding != self.binding:
                raise ChannelMessageHistoryError('FEISHU_USER_AUTH_REQUIRED')
            self.binding = binding
            return binding
        except (OSError, ValueError, TypeError, AttributeError):
            raise ChannelMessageHistoryError('FEISHU_USER_AUTH_REQUIRED') from None

    def read(self, path, remaining):
        from .feishu_cli import request_cli_cloud_api
        from .feishu_cloud import FeishuCloudError
        try:
            data = request_cli_cloud_api(self.identity, 'GET', path, user_token=self.identity.token, read_only=True, timeout=min(remaining, 25))
        except FeishuCloudError as error:
            if error.code in {'FEISHU_USER_AUTH_REQUIRED', 'FEISHU_CLOUD_AUTH_INVALID', 'FEISHU_CREDENTIAL_UNAVAILABLE'}:
                raise ChannelMessageHistoryError('FEISHU_USER_AUTH_REQUIRED') from None
            if error.code in {'FEISHU_CLOUD_SCOPE_REQUIRED', 'FEISHU_CLOUD_RESOURCE_FORBIDDEN'} or error.provider_code == 230027:
                raise ChannelMessageHistoryError('FEISHU_PROVIDER_SCOPE_REQUIRED') from None
            raise ChannelMessageHistoryError('FEISHU_PROVIDER_UNAVAILABLE') from None
        # 原CLI公开投影将非秘密page_token改名，内部reader恢复同一供应商续点，不当作授权。
        if 'next_cursor' in data:
            data = {**data, 'page_token': data['next_cursor']}
        return data


def list_user_messages(agent, chat_id, **query):
    if not _identifier(chat_id):
        raise ChannelMessageHistoryError('TOOL_INVALID_ARGUMENTS')
    start, end, senders, cursor, limit = validate_history_query(**query)
    identity = UserHistoryIdentity(agent)
    deadline = time.monotonic() + HistoryLimits.BUDGET_SECONDS
    mode = None

    def check():
        nonlocal mode
        from .feishu_provider_facts import FeishuProviderFactError, _provider, _read

        authority = identity.check()
        def remaining():
            amount = deadline - time.monotonic()
            if amount <= 0:
                raise ChannelMessageHistoryError('FEISHU_PROVIDER_UNAVAILABLE')
            return amount
        path = '/im/v1/chats/' + quote(chat_id, safe='')
        chat = identity.read(path + '?user_id_type=open_id', remaining())
        current_mode = chat.get('chat_mode')
        if current_mode not in {'p2p', 'group', 'topic'} or mode is not None and mode != current_mode:
            raise ChannelMessageHistoryError('FEISHU_PROVIDER_RESPONSE_INVALID')
        mode = current_mode
        if mode != 'p2p':
            own = identity.read(path + '/members/is_in_chat', remaining())
            if own.get('is_in_chat') is not True:
                raise ChannelMessageHistoryError('FEISHU_USER_AUTH_REQUIRED')
            try:
                adapter, token = _provider(agent)
                if adapter.app_id != identity.identity.app_id:
                    raise ChannelMessageHistoryError('FEISHU_PROVIDER_AUTH_UNAVAILABLE')
                bot = _read(token, path + '/members/is_in_chat', timeout=min(5.0, remaining()), stage='bot_membership')
            except FeishuProviderFactError as error:
                raise ChannelMessageHistoryError(error.reason_code) from None
            if bot.get('is_in_chat') is not True:
                raise ChannelMessageHistoryError('FEISHU_BOT_GROUP_MEMBERSHIP_REQUIRED')
        if identity.check() != authority:
            raise ChannelMessageHistoryError('FEISHU_USER_AUTH_REQUIRED')
        return (*authority, mode)

    authority = check()
    return _list_chat_messages(chat_id, start=start, end=end, senders=senders, cursor=cursor, limit=limit,
        authority=authority, check_authority=check, read_page=identity.read, deadline=deadline)


def validate_user_chat_query(*, cursor='', limit=50, chat_types=('p2p', 'group')):
    if (type(limit) is not int or not 1 <= limit <= HistoryLimits.MAX_MESSAGES or type(cursor) is not str
            or len(cursor) > HistoryLimits.MAX_CURSOR_CHARS or not isinstance(chat_types, (list, tuple))
            or not chat_types or any(type(value) is not str or value not in {'p2p', 'group'} for value in chat_types)):
        raise ChannelMessageHistoryError('TOOL_INVALID_ARGUMENTS')
    return sorted(set(chat_types))


def list_user_chats(agent, *, cursor='', limit=50, chat_types=('p2p', 'group')):
    types = validate_user_chat_query(cursor=cursor, limit=limit, chat_types=chat_types)
    identity = UserHistoryIdentity(agent)
    authority = identity.check()
    binding = {'selector': 'user', 'action': 'list_chats', 'authority': list(authority), 'chat_types': types}
    token = _decode_cursor(cursor, binding)
    result, seen, tokens, reasons = [], set(), {token}, []
    deadline = time.monotonic() + HistoryLimits.BUDGET_SECONDS
    more, fetched, pages = True, 0, 0
    for _ in range(HistoryLimits.MAX_PAGES):
        if identity.check() != authority:
            raise ChannelMessageHistoryError('CHANNEL_MESSAGES_SCOPE_UNVERIFIED')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reasons.append('time_budget')
            break
        size = min(HistoryLimits.PAGE_SIZE, limit - len(result))
        if not size:
            reasons.append('result_limit')
            break
        query = {'types': ','.join(types), 'user_id_type': 'open_id', 'page_size': size}
        if token:
            query['page_token'] = token
        data = identity.read('/im/v1/chats?' + urlencode(query), remaining)
        rows, more, following = data.get('items'), data.get('has_more'), data.get('page_token', '')
        if (not isinstance(rows, list) or len(rows) > size or type(more) is not bool
                or type(following) is not str or len(following) > HistoryLimits.MAX_PROVIDER_CURSOR_CHARS
                or more and (not following or following in tokens)):
            raise ChannelMessageHistoryError('FEISHU_PROVIDER_RESPONSE_INVALID')
        pages += 1
        fetched += len(rows)
        for row in rows:
            if not isinstance(row, dict) or not _identifier(row.get('chat_id')):
                raise ChannelMessageHistoryError('FEISHU_PROVIDER_RESPONSE_INVALID')
            item = {'chat_id': row['chat_id']}
            for field in ('name', 'description', 'chat_mode', 'p2p_target_id', 'p2p_target_type'):
                value = row.get(field, '')
                if type(value) is not str:
                    raise ChannelMessageHistoryError('FEISHU_PROVIDER_RESPONSE_INVALID')
                item[field] = value[:HistoryLimits.MAX_TEXT_CHARS]
                if len(value) > HistoryLimits.MAX_TEXT_CHARS:
                    reasons.append('text_truncated')
            if item['chat_id'] not in seen:
                result.append(item)
                seen.add(item['chat_id'])
        token = following if more else ''
        if not more:
            break
        tokens.add(token)
    else:
        reasons.append('page_budget')
    if identity.check() != authority:
        raise ChannelMessageHistoryError('CHANNEL_MESSAGES_SCOPE_UNVERIFIED')
    return {'schema_version': 'channel_chat_page.v1', 'selector': 'user', 'items': result,
        'next_cursor': _encode_cursor(binding, token) if more else '', 'has_more': more,
        'pages_read': pages, 'fetched_count': fetched,
        'coverage': {'scope': 'provider_visible_user_chats', 'status': 'partial' if more or reasons else 'complete',
                     'chat_types': types, 'reasons': list(dict.fromkeys(reasons))}}
