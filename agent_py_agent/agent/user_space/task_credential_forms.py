# LLM: 静默取消共用原输入退休实现，冷C只消费其真实输入服务末门，不新建Wake或恢复任务。
# 模块用途: 让原Agent和冷C保持相同的凭据取消回执及输入清理。
# LLM: 输入值与安全元数据分离；配置/重发复用原动作授权并在凭据锁外核成员，锁内仅CAS，不增加网络/管理权。
# 模块用途: 校验原凭据卡与认证配置，群共享使用和管理保持分权，秘密只在原加密链保存。
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


# LLM: 配置仅供原请求人/实际提供者，群授权检查在锁外；锁内复读代次和授权事实，不因共享使用取得管理权。
# 函数用途: 给已保存凭据绑定声明式认证，保持准确origin和原秘密字段，不在锁内请求飞书。
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
        _assert_credential_unchanged,
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
    observed = read_json_object_report(path, context='credentials.configure_preflight').payload
    _assert_credential_scope(agent, observed, scope, observed.get('origin', ''), operation='configure')
    with _credential_mutation(agent, path) as admission:
        row = read_json_object_report(path, context='credentials.configure').payload
        _assert_credential_unchanged(agent, observed, row, scope)
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
# LLM: 静默退休规则只保留一个实现，原Agent只提供原输入依赖，不改reissue/cancel的Wake语义。
# 函数用途: 沿原资源关闭准确待填卡。
def retire_credential_input(agent, previous):
    from ..conversation.input_dependencies import input_dependency_access
    return retire_credential_input_from_access(input_dependency_access(agent), previous)


# LLM: group更新只放开同群管理者的新任务，仍限原请求人/填写人并核原MAC；task继续同来源线程，不读取明文。
# 函数用途: 为原提供者重新收集凭据；调用方必须在凭据锁外执行，不能把共享使用当重发权限。
def assert_credential_reissue_scope(agent, row, scope):
    from dataclasses import asdict

    from ..conversation.im_tool_approval import approval_recipient
    from ..conversation.tool_approval_scope import tool_approval_scope_is_current
    from .owner_resolver import owner_identity_from_config

    if not row or not scope.active or not tool_approval_scope_is_current(agent, scope):
        raise ValueError('当前重发请求执行已失效')
    owner = asdict(owner_identity_from_config(agent.config))
    channel, actor = approval_recipient(agent, scope)
    original_scope = row.get('scope', {})
    shared = row.get('access_scope') == 'group'
    if shared:
        from .task_credential_groups import group_credential_access
        group_credential_access(agent, row, scope, operation='update')
    if (row.get('owner') != owner or row.get('channel') != channel
            or actor not in {row.get('actor_id'), row.get('submitter_actor_id')}
            or not row.get('message_id') or not row.get('chat_id')):
        raise ValueError('无权重新收集此凭据')
    # 子代理scope.thread_id是child线程，比较原任务的canonical来源线程，而非展示线程。
    original, error = agent.conversation_store.tasks.load_report(original_scope.get('root_task_id', ''))
    current, current_error = agent.conversation_store.tasks.load_report(scope.root_task_id)
    if error or current_error or original is None or current is None or (not shared and original.thread_id != current.thread_id):
        raise ValueError('凭据重发不属于原会话')
    assert_credential_record_signature(agent, row)


# LLM: 只核原row的宿主MAC和完整身份，不解密、不读取模型字段、不改变授权；旧缺access_scope严格按task。
# 函数用途: 为更新及旧群卡显式共享复用同一原卡来源证明。
def assert_credential_record_signature(agent, row):
    import base64
    import hmac
    import json

    from .interaction_refs import interaction_key

    try:
        prefix, encoded, signature = row['reference'].split('.')
        raw = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
        payload = json.loads(raw)
        expected = hmac.digest(interaction_key(agent.home_paths.owner_home_dir), b'im-interaction-v1\0' + raw, 'sha256').hex()
        keys = ('owner', 'channel', 'actor_id', 'scope', 'credential_ref', 'expires_at')
        valid = isinstance(payload, dict) and prefix == 'ir1' and hmac.compare_digest(signature, expected) and payload.get('kind') == 'task_credential'
        valid = valid and all(payload.get(key) == row.get(key) for key in keys)
        valid = valid and payload.get('generation', 1) == row.get('generation', 1)
        valid = valid and payload.get('access_scope', 'task') == row.get('access_scope', 'task')
    except (KeyError, TypeError, ValueError, UnicodeError):
        valid = False
    if not valid:
        raise ValueError('原凭据卡身份无法核验')


# LLM: 只允许原群卡的原请求人/实际提供者显式升级，私聊不迁移；成员查询锁外、原row CAS锁内，不复制密文或放开管理权。
# 函数用途: 将已保存旧群任务凭据公开给本群助手，无须重新填写；调用方仍按原工具账审计。
def share_credential(agent, reference):
    from ..common.json_io import read_json_object_report
    from ..conversation.im_tool_approval import approval_recipient
    from ..conversation.tool_approval_scope import (
        tool_approval_scope,
        tool_approval_scope_is_current,
    )
    from .interaction_refs import sign_interaction_reference
    from .task_credential_groups import group_credential_access
    from .task_credentials import (
        _assert_credential_unchanged,
        _credential_mutation,
        _save_credential_row,
        credential_path,
    )

    current = getattr(agent, '_current_run_params', None)
    if current is None:
        raise ValueError('当前任务无效')
    scope = tool_approval_scope(agent, str(current.run_id or current.task_id))
    if not scope.active or not tool_approval_scope_is_current(agent, scope):
        raise ValueError('当前任务无效')
    path = credential_path(agent.home_paths.owner_home_dir, reference)
    observed = read_json_object_report(path, context='credentials.share_preflight').payload
    if observed.get('status') != 'ready' or observed.get('presentation', {}).get('kind') != 'group':
        raise ValueError('只有已保存的原群卡凭据可供本群共享')
    assert_credential_record_signature(agent, observed)
    group_credential_access(agent, {**observed, 'access_scope': 'group'}, scope, operation='share')
    import base64
    import json
    import time

    _, actor = approval_recipient(agent, scope)
    with _credential_mutation(agent, path) as admission:
        row = read_json_object_report(path, context='credentials.share').payload
        _assert_credential_unchanged(agent, observed, row, scope)
        if row.get('access_scope', 'task') not in {'task', 'group'}:
            raise ValueError('凭据使用范围无效')
        if row.get('access_scope') != 'group':
            encoded = row['reference'].split('.')[1]
            payload = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
            payload['access_scope'] = 'group'
            row.update(access_scope='group', reference=sign_interaction_reference(agent.home_paths.owner_home_dir, payload),
                       shared_by=actor, shared_at=time.time())
            _save_credential_row(admission, path, row)
    return {'credential_ref': reference, 'origin': row['origin'], 'access_scope': 'group',
            'status': row['status'], 'verification_status': row.get('verification_status', 'unverified')}


# LLM: 原pending/cancelled输入只原位退休，refs/deadline各自消费同一原提交门；不造取消Wake或任务终态。
# 函数用途: 为原Agent和冷C静默回收同卡输入。
def retire_credential_input_from_access(inputs, previous):
    import time

    input_id = previous.get('input_id',previous['credential_ref'])

    # LLM: 输入锁内只退休准确pending/cancelled卡输入并标resolution_delivery=retire；不覆盖ready，泛用取消不受此静默标记影响。
    # 函数用途: 在原输入记录上退休重发前的待填卡。
    def retire(row):
        return {**row,'status':'cancelled','armed':False,'resolved_at':row.get('resolved_at') or time.time(),'resolution_delivery':'retire'} if row and row['status'] in {'pending','cancelled'} else row

    inputs.mutate(input_id, retire)
    inputs.update_refs(previous['scope']['root_task_id'], input_id, remove=True)
    inputs.schedule(previous['scope']['root_task_id'])
