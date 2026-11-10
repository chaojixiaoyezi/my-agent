"""本人规则的后台来源读取：规范 job/代次授予有限读权，不伪造活动执行上下文。"""
from __future__ import annotations

from ..adapter import feishu_provider_facts
from ..adapter.feishu_provider_facts import FeishuProviderFactError, group_authority_snapshot
from ..user_space.owner_policy import resolve_effective_owner_policy
from ..user_space.owner_resolver import owner_identity_from_config
from .event_sources import event_rule_views


class SourceAuthorityDenied(RuntimeError):
    def __init__(self, reason_code):
        self.reason_code = reason_code
        super().__init__(reason_code)


def matching_source_views(repository, event, refs, *, probe_only=False):
    from .event_sources import event_matches
    from .repository import SchedulerNotFoundError

    result, jobs = [], {}
    for job_id, rule_id, generation in refs:
        if job_id not in jobs:
            try:
                jobs[job_id] = repository.get_job(job_id)
            except SchedulerNotFoundError:
                jobs[job_id] = None
        job = jobs[job_id]
        if job is None or job.get('status') != 'active':
            continue
        for view in event_rule_views(job):
            key = str(view.get('rule_id') or '')
            state = (job.get('event_source_state') or {}).get(key) or {}
            if key != rule_id or state.get('generation') != generation:
                continue
            source = view['schedule']['source']
            matches = (all(source[key] == event.get(key) for key in
                ('channel', 'event_type', 'app_id', 'chat_id', 'sender_type', 'sender_id_type'))
                and event.get('sender_id') in source['sender_ids']) if probe_only else event_matches(view['schedule'], event)
            if view.get('enabled', True) and state.get('active') and matches:
                result.append((job, key, generation, source))
    return result


def personal_source_revalidator(agent, repository, job, rule_id, generation, source):
    from ..common.json_io import read_json_object_report
    from ..user_space.feishu_oauth import _assert_actor, _configuration_facts, oauth_path

    actor = source['subscriber_actor_id']
    owner = owner_identity_from_config(agent.config)
    if owner.owner_kind == 'group':
        raise ValueError('EVENT_SOURCE_OWNER_MISMATCH')
    job_id = job['job_id']
    def revalidate():
        current = repository.get_job(job_id)
        if current.get('status') != 'active' or current.get('creator') != {'channel': 'feishu', 'actor_id': actor}:
            raise SourceAuthorityDenied('EVENT_SOURCE_RULE_RETIRED')
        if (not resolve_effective_owner_policy(agent.home_paths).network_enabled
                or getattr(getattr(agent, 'owner_policy', None), 'network_enabled', True) is False):
            raise SourceAuthorityDenied('EVENT_SOURCE_NETWORK_REVOKED')
        if getattr(agent.config, 'feishu_app_id', None) != source['app_id']:
            raise SourceAuthorityDenied('EVENT_SOURCE_APPLICATION_CHANGED')
        _assert_actor(agent, actor)
        credential = read_json_object_report(oauth_path(agent, actor), context='oauth.source_authority').payload
        if credential and credential.get('status') == 'revoked':
            raise SourceAuthorityDenied('EVENT_SOURCE_OAUTH_REVOKED')
        if not credential or credential.get('status') != 'ready' or credential.get('actor_id') != actor:
            raise ValueError('EVENT_SOURCE_OAUTH_AUTHORITY_CHANGED')
        configuration = _configuration_facts(agent)
        if any(credential.get(key) != value for key, value in configuration.items()):
            raise SourceAuthorityDenied('EVENT_SOURCE_CONFIGURATION_CHANGED')
        for view in event_rule_views(current):
            state = (current.get('event_source_state') or {}).get(rule_id) or {}
            if (str(view.get('rule_id') or '') == rule_id and view.get('enabled', True)
                    and state.get('active') and state.get('generation') == generation
                    and view['schedule']['source'] == source):
                return
        raise SourceAuthorityDenied('EVENT_SOURCE_RULE_RETIRED')
    return revalidate


def authorize_personal_source(agent, repository, job, rule_id, generation, source):
    from ..user_space.feishu_oauth import _user_access_token_with_authority

    revalidate = personal_source_revalidator(agent, repository, job, rule_id, generation, source)
    actor = source['subscriber_actor_id']
    token = _user_access_token_with_authority(agent, actor, revalidate=revalidate)
    # token 不保存到事件、模型或投影；本人 Get 和 bot 成员事实必须指向同一准确群。
    from urllib.parse import quote

    try:
        chat = feishu_provider_facts._read(token, '/im/v1/chats/' + quote(source['chat_id'], safe='') + '?user_id_type=open_id', stage='chat_info')
    except FeishuProviderFactError as exc:
        from ..adapter.feishu_cloud import _http_error_details, _provider_error_details

        details = (_http_error_details(exc.provider_code) if exc.reason_code == 'FEISHU_PROVIDER_HTTP_REJECTED'
            and exc.provider_code is not None else _provider_error_details(exc.provider_code))
        if details and details[1] in {'credential_invalid', 'application_scope_required', 'resource_permission_required'}:
            raise SourceAuthorityDenied('EVENT_SOURCE_PROVIDER_AUTHORIZATION_DENIED') from exc
        raise
    snapshot = group_authority_snapshot(agent, source['chat_id'])
    revalidate()
    if (chat.get('chat_mode') not in {'group', 'topic'} or chat.get('tenant_key') != source['tenant_key']
            or snapshot.get('tenant_key') != source['tenant_key'] or snapshot.get('app_id') != source['app_id']
            or snapshot.get('bot_is_member') is not True or snapshot.get('members_complete') is not True
):
        raise FeishuProviderFactError('FEISHU_OBSERVATION_SCOPE_MISMATCH')
    if actor not in snapshot.get('member_ids', ()):
        raise SourceAuthorityDenied('EVENT_SOURCE_SUBSCRIBER_LEFT')
    return revalidate
