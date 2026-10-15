# LLM: 表单元数据与秘密字段分离，目标只在结构校验后绑定；不发网络、不推断认证或授予网络权限。
# 模块用途: 校验简单凭据卡提交，供原 TaskCredential 加密保存使用。
from __future__ import annotations


# LLM: 只接收原卡明确字段，名称备注不充当身份；缺目标时仅从这次表单绑定，既有目标不得静默改写。
# 函数用途: 将提交拆成待加密秘密和可保存元数据，拒绝内嵌密码 URL 与额外字段。
def credential_submission_values(row, fields, origin_for):
    names = {field['name'] for field in row['fields']}
    optional = {'credential_name', 'credential_note'}
    needs_url = not row.get('target_url', row.get('origin'))
    required = names | ({'target_url'} if needs_url else set())
    if not isinstance(fields, dict) or not required <= set(fields) or set(fields) - required - optional:
        raise ValueError('输入字段无效')
    if any(not isinstance(value, str) or len(value) > (8192 if name in names else 2048)
           or '\r' in value or '\n' in value or (name in required and not value)
           for name, value in fields.items()):
        raise ValueError('输入字段无效')
    target = fields['target_url'] if needs_url else row.get('target_url', row['origin'])
    metadata = {'target_url': target, 'origin': origin_for(target)}
    metadata.update({name: fields[name] for name in optional if name in fields})
    return {name: fields[name] for name in names}, metadata


# LLM: 群成员提供权限来自可信运输事实，取消只终止本卡允许当前成员，覆盖仍限原请求人或既有提供者；不认表单姓名。
# 函数用途: 在写入凭据前核验当前提交者并返回应记账的飞书身份。
def credential_submitter(row, body):
    presentation = row.get('presentation', {})
    if presentation.get('kind') != 'group':
        return row['actor_id']
    submitter = body.get('submitter', {})
    actor = submitter.get('actor_id')
    if not actor or submitter.get('chat_id') != presentation['chat_id'] or submitter.get('current_group_member') is not True:
        raise ValueError('群凭据提交者无法核验')
    if body.get('decision') != 'cancel' and row.get('replaces_ref') and actor not in {row['actor_id'], row.get('previous_submitter_actor_id')}:
        raise ValueError('当前成员没有替换原凭据的权限')
    return actor


# LLM: configure 只绑定已收集字段的声明式用法，不接收秘密，不扩大 origin；调用方仍必须经过原 owner/task 授权。
# 函数用途: 给已保存但未指定认证方式的凭据补上受控认证方案。
def configure_credential(agent, reference, *, authentication, auth_config=None, header_name='Authorization'):
    from ..common.json_io import read_json_object_report
    from ..conversation.tool_approval_scope import (
        tool_approval_scope,
        tool_approval_scope_is_current,
    )
    from .task_credential_schema import (
        credential_auth_config,
        credential_fields,
        credential_header_name,
    )
    from .task_credentials import (
        _assert_credential_scope,
        _credential_mutation,
        _save_credential_row,
        credential_origin,
        credential_path,
    )
    current = agent._current_run_params
    scope = tool_approval_scope(agent, str(current.run_id or current.task_id))
    if not scope.active or not tool_approval_scope_is_current(agent, scope):
        raise ValueError('原任务执行已失效')
    path = credential_path(agent.home_paths.owner_home_dir, reference)
    with _credential_mutation(agent, path) as admission:
        row = read_json_object_report(path, context='credentials.configure').payload
        _assert_credential_scope(agent, row, scope, row.get('origin', ''))
        if row['authentication'] != 'collected' or authentication == 'collected':
            raise ValueError('仅待配置凭据可以绑定认证方式')
        fields = credential_fields(authentication, row['fields'])
        config = credential_auth_config(authentication, auth_config, fields)
        if authentication == 'login' and credential_origin(config['url']) != row['origin']:
            raise ValueError('登录地址必须属于同一接口 origin')
        row.update(authentication=authentication, auth_config=config, header_name=credential_header_name(header_name))
        _save_credential_row(admission, path, row)
    return {'credential_ref':reference, 'status':row['status'], 'origin':row['origin'],
            'authentication':authentication, 'verification_status':row.get('verification_status', 'unverified')}


# LLM: 重发仅退休准确旧输入，不产生取消Wake；新代次沿原登记/截止账本，旧卡引用不再可提交。
# 函数用途: 在重发或精确取消后释放原待填输入槽并清理原截止调度，避免迟到或重复卡占住任务容量。
def retire_credential_input(agent, previous):
    import time

    from ..conversation.input_dependencies import (
        _mutate,
        _schedule_input_deadline,
        update_task_input_refs,
    )
    input_id = previous['input_id']

    # LLM: 输入锁内只退休准确pending/cancelled卡输入并标resolution_delivery=retire；不覆盖ready，泛用取消不受此静默标记影响。
    # 函数用途: 在原输入记录上退休重发前的待填卡。
    def retire(row):
        return {**row,'status':'cancelled','armed':False,'resolved_at':row.get('resolved_at') or time.time(),'resolution_delivery':'retire'} if row and row['status'] in {'pending','cancelled'} else row

    _mutate(agent, input_id, retire)
    update_task_input_refs(agent, previous['scope']['root_task_id'], input_id, remove=True)
    _schedule_input_deadline(agent,previous['scope']['root_task_id'])


# LLM: 重发授权独立于读取授权，仅按当前真实claim/原线程/owner/原请求人或提供者核验；绝不读取密文或放宽HTTP root/job限制。
# 函数用途: 允许同会话下一条真实请求重新收集旧卡，拒绝跨线程、冒名及伪造旧卡。
def assert_credential_reissue_scope(agent, row, scope):
    import base64
    import hmac
    import json
    from dataclasses import asdict

    from ..conversation.im_tool_approval import approval_recipient
    from ..conversation.tool_approval_scope import tool_approval_scope_is_current
    from .interaction_refs import interaction_key
    from .owner_resolver import owner_identity_from_config

    if not row or not scope.active or not tool_approval_scope_is_current(agent, scope):
        raise ValueError('当前重发请求执行已失效')
    owner = asdict(owner_identity_from_config(agent.config))
    channel, actor = approval_recipient(agent, scope)
    original_scope = row.get('scope', {})
    if (row.get('owner') != owner or row.get('channel') != channel
            or actor not in {row.get('actor_id'), row.get('submitter_actor_id')}
            or not row.get('message_id') or not row.get('chat_id')):
        raise ValueError('无权重新收集此凭据')
    # 子代理scope.thread_id是child线程，比较原任务的canonical来源线程，而非展示线程。
    original, error = agent.conversation_store.tasks.load_report(original_scope.get('root_task_id', ''))
    current, current_error = agent.conversation_store.tasks.load_report(scope.root_task_id)
    if error or current_error or original is None or current is None or original.thread_id != current.thread_id:
        raise ValueError('凭据重发不属于原会话')
    try:
        prefix, encoded, signature = row['reference'].split('.')
        raw = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
        payload = json.loads(raw)
        expected = hmac.digest(interaction_key(agent.home_paths.owner_home_dir), b'im-interaction-v1\0' + raw, 'sha256').hex()
        keys = ('owner', 'channel', 'actor_id', 'scope', 'credential_ref', 'expires_at')
        valid = isinstance(payload, dict) and prefix == 'ir1' and hmac.compare_digest(signature, expected) and payload.get('kind') == 'task_credential'
        valid = valid and all(payload.get(key) == row.get(key) for key in keys)
        valid = valid and payload.get('generation', 1) == row.get('generation', 1)
    except (KeyError, TypeError, ValueError, UnicodeError):
        valid = False
    if not valid:
        raise ValueError('原凭据卡身份无法核验')
