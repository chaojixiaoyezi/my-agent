"""只读投影部署应用的授权入口；申请入口不是已获权限的证明。"""
from __future__ import annotations

import re
from urllib.parse import urlencode

MESSAGE_HISTORY_DOC = 'https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/list'
CHAT_LIST_DOC = 'https://open.feishu.cn/document/server-docs/group/chat/list'

def permission_requirements(agent, *, required_scopes=(), identity='user',
                            required_events=(), official_api_docs=()):
    """从宿主 App ID 生成官方申请入口，不读取密钥、令牌或发起授权。"""
    if identity not in {'user', 'bot'}:
        raise ValueError('飞书授权身份无效。')
    scopes, events, docs = (list(dict.fromkeys(values)) for values in
                            (required_scopes, required_events, official_api_docs))
    if any(not isinstance(value, str) or not value or len(value) > 512
           or any(char.isspace() for char in value) for value in scopes + events + docs):
        raise ValueError('飞书授权需求无效。')
    app_id = getattr(agent.config, 'feishu_app_id', '')
    app_id = app_id if isinstance(app_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', app_id) else ''
    base = f'https://open.feishu.cn/app/{app_id}' if app_id else ''
    query = urlencode({'q': ','.join(scopes), 'op_from': 'openapi'}) if scopes else ''
    result = {
        'app_id': app_id, 'permission_status': 'not_verified', 'identity': identity,
        'required_scopes': scopes, 'required_events': events,
        'permission_settings_url': base + '/auth' + ('?' + query if query else '') if base else '',
        'event_settings_url': base + '/event' if base else '',
        'official_api_docs': docs, 'user_authorization_required': identity == 'user',
        'application_setup_required': not bool(app_id),
        'application_permission_steps': ['打开权限管理申请所列权限', '按租户要求审核并发布应用版本'],
    }
    if identity == 'user':
        result['oauth_action'] = {'tool': 'feishu_auth', 'arguments': {'action': 'authorize'}}
    return result


def authorization_guidance(agent):
    """让 Agent 在尚未配置 OAuth 时仍能说明申请路径和准确阻塞项。"""
    from ..capability.runtime_config_reload import (
        capability_config_for_agent,
        deployment_capability_config,
    )
    from . import feishu_oauth

    features = {
        'list_own_chats': permission_requirements(agent,
            required_scopes=['im:chat:read'], official_api_docs=[CHAT_LIST_DOC]),
        'read_own_private_history': permission_requirements(agent,
            required_scopes=['im:message:readonly', 'im:message.p2p_msg:get_as_user'],
            official_api_docs=[MESSAGE_HISTORY_DOC]),
        'read_own_group_history': permission_requirements(agent,
            required_scopes=['im:message:readonly', 'im:message.group_msg:get_as_user'],
            official_api_docs=[MESSAGE_HISTORY_DOC]),
        'verify_own_group_membership': permission_requirements(agent,
            required_scopes=['im:chat:readonly'],
            official_api_docs=['https://open.feishu.cn/document/server-docs/group/chat/get']),
        'receive_group_messages': permission_requirements(agent, identity='bot',
            required_scopes=['im:message:readonly', 'im:message.group_msg',
                             'im:chat:readonly'],
            required_events=['im.message.receive_v1'],
            official_api_docs=['https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/events/receive']),
        'receive_other_bot_messages': permission_requirements(agent, identity='bot',
            required_scopes=['im:message:readonly', 'im:message.group_msg.include_bot:read',
                             'im:chat:readonly'],
            required_events=['im.message.receive_v1']),
        'edit_spreadsheet': permission_requirements(agent, identity='bot',
            required_scopes=['sheets:spreadsheet'],
            official_api_docs=['https://open.feishu.cn/document/server-docs/docs/sheets-v3/overview']),
    }
    required = sorted({scope for feature in features.values() if feature['identity'] == 'user'
                       for scope in feature['required_scopes']})
    result = permission_requirements(agent, required_scopes=required)
    result['capabilities'] = features
    result['oauth_status'] = 'not_configured'
    result['configuration_status'] = 'not_configured'
    result['missing_requested_scopes'] = []
    # 帮助不能把“提供了接口”说成开关已开启；保留用户覆盖与配置损坏的未知状态。
    switches = ('feishu_chat_history_enabled', 'feishu_user_subscriptions_enabled',
                'feishu_bot_subscriptions_enabled', 'feishu_cloud_tools_enabled')
    result['feature_switches'] = {}
    for label, read in (('deployment', deployment_capability_config), ('owner', capability_config_for_agent)):
        try:
            config = read(agent.home_paths if label == 'deployment' else agent)
        except (OSError, ValueError, TypeError, AttributeError):
            config = None
        result['feature_switches'][label] = {key: value if type(value := getattr(config, key, None)) is bool else None
                                           for key in switches}
    result['notes'] = [
        '后台权限审核发布与用户本人 OAuth 是两步，申请链接不代表已经开通。',
        '只能读取当前已授权本人可见的数据；群历史仍受飞书机器人在群和历史可见性限制。',
        '首次本人授权需要用户点击登录卡；完成后沿宿主原授权自动刷新。',
    ]
    try:
        _uri, scopes = feishu_oauth.oauth_configuration(agent)
        feishu_oauth._application_credentials(agent)
    except (OSError, ValueError, TypeError):
        result['required_action'] = 'configure_application_oauth'
        result['setup_instructions'] = '部署者配置应用凭据与准确的 HTTPS OAuth 回调，并在开发者后台登记回调；随后发起本人授权。'
        return result
    result['configuration_status'] = 'configured'
    result['missing_requested_scopes'] = sorted(set(required) - set(scopes))
    if result['missing_requested_scopes']:
        result['required_action'] = 'configure_requested_scopes'
    else:
        result['required_action'] = 'authorize_current_user'
    try:
        status = feishu_oauth.oauth_status(agent)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        result['oauth_status'] = 'current_personal_identity_required'
        return result
    result['oauth_status'] = status['status']
    result['granted_scopes'] = status['granted_scopes'].split()
    if status['status'] == 'ready' and not result['missing_requested_scopes']:
        result['required_action'] = ('none' if set(required).issubset(result['granted_scopes'])
                                     else 'reauthorize_current_user')
    return result
