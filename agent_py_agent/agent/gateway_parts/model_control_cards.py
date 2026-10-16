# LLM: 模型控制复用原 owner 目录、线程选择、签名和 Delivery；记录仅保存控制身份，不保存密钥或另建任务。
# 模块用途: 为飞书提供原会话模型选择卡及私人连接/输入模态表单，声明沿原校验和模型管理入口保存，不探测供应商。
from __future__ import annotations

import hmac
import json
import time
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_private_text_file_atomic_unlocked,
)
from ..delivery import DeliveryContext
from ..path_access_policy import PRIVATE_INTERACTIONS_OWNER_PARTS
from ..settings.embedding_selection import catalog_write_check
from ..settings.model_profiles import (
    execute_model_profile_operation,
    model_profile_write_check,
    model_profiles_path,
    read_model_profiles,
)
from ..settings.model_provider_schema import validate_input_modalities
from ..user_space.interaction_refs import interaction_key, sign_interaction_reference
from ..user_space.owner_resolver import OwnerIdentity, home_paths_with_owner, resolve_owner_home
from .owner_conversation_store import owner_conversation_store


# LLM: 控制路径仅来自签名 UUID 和原受保护目录；不能由表单选择路径。
# 函数用途: 定位一张有期限的模型控制记录，不创建第二模型目录。
def _record_path(home, control_id):
    control_id = str(UUID(control_id))
    return Path(home).joinpath(*PRIVATE_INTERACTIONS_OWNER_PARTS, 'model-' + control_id + '.json')


# LLM: 冷控制只组装原 owner store，不初始化后端、任务或运行器；owner 由已验签的结构身份给出。
# 函数用途: 在私人回调里取得原群或个人模型宿主。
def model_control_host(base, owner):
    home = home_paths_with_owner(base.home_paths, resolve_owner_home(base.home_paths.root, owner))
    return SimpleNamespace(home_paths=home, config=base.config,
                           conversation_store=owner_conversation_store(base, home))


# LLM: 版本取原目录随机代次与 canonical thread revision；公开目录和管理员源代次变化也须使旧卡失效。
# 函数用途: 捕获不含秘密的卡片版本，用于后续精确复核。
def _versions(host, thread_id):
    from ..settings.shared_model_catalog import _admin_profiles_path, shared_catalog_path
    paths = (model_profiles_path(host.home_paths), _admin_profiles_path(host.home_paths), shared_catalog_path(host.home_paths))
    generations = []
    for path in paths:
        data = json.loads(path.read_text()) if path.is_file() else {}
        generations.append(data.get('catalog_generation'))
    thread = host.conversation_store.threads.load(thread_id)
    if thread is None:
        raise ValueError('原会话不存在')
    return {'catalog_revisions': generations, 'selection_revision': thread.model_selection_revision}


# LLM: 归属只由原 OwnerIdentity.owner_kind 决定，文案不能成为授权来源；群与管理员目录的默认范围各守原合同。
# 函数用途: 明确卡片配置/默认值影响本人还是原群，避免私人表单被误认为个人配置。
def _model_scope_label(owner):
    if owner.owner_kind == 'group':
        return '配置归属：当前群。新增、编辑模型供该群执行使用；新会话默认只影响该群后续创建的会话。'
    if owner == OwnerIdentity.local_main():
        return '配置归属：你的管理员目录。这里只修改本人的模型目录与新会话默认；共享发布和全局初始模型由管理员入口另行管理。'
    return '配置归属：你本人。新增、编辑模型保存在本人的私人目录；新会话默认只影响本人后续创建的会话。'


# LLM: 原 owner 保护 key 派生带用途/卡编号的 HMAC，不保存明文表单或可离线字典猜测的裸摘要。
# 函数用途: 为完整逻辑动作和字段生成受保护的重放比较值，JSON 键顺序不改变逻辑身份。
def _model_submission_digest(home, control_id, decision, fields):
    raw = json.dumps({'decision': decision, 'fields': fields}, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hmac.digest(interaction_key(home), b'model-control-submission.v1\0' + control_id.encode() + b'\0' + raw, 'sha256').hex()


# LLM: scope_label 仅来自签名原 owner 的结构身份；卡无密钥/API 地址/探测，按钮只携带签名引用与稳定编号。
# 函数用途: 渲染当前会话与未来默认选择以及取消；私人表单按钮不在群里展示输入框。
def model_selection_card(listing, reference, scope_label):
    from .model_profile_service import _selectable_choices, render_model_choices
    elements = [{'tag': 'div', 'text': {'tag': 'plain_text', 'content': scope_label + '\n\n' + render_model_choices(listing)}}]
    for row in _selectable_choices(listing):
        actions = [_button('本会话使用', reference, 'select', row['id']),
                   _button('设为新会话默认', reference, 'set_default', row['id'])]
        if row['id'] != 'default' and not row.get('shared'):
            actions.append(_button('私聊编辑', reference, 'edit', row['id']))
        elements.extend([{'tag': 'div', 'text': {'tag': 'plain_text', 'content': str(row['model_name'])}},
                         {'tag': 'action', 'actions': actions}])
    elements.append({'tag': 'action', 'actions': [_button('私聊新增模型', reference, 'add'),_button('取消',reference,'cancel')]})
    return {'config': {'wide_screen_mode': True, 'update_multi': True, 'enable_forward': False}, 'elements': elements}


# LLM: 动作和编号作为 fields 送宿主；不得携带 provider 秘密或让正文决定控制类型。
# 函数用途: 构造一枚模型控制按钮。
def _button(label, reference, operation, profile_id='', *, submit=False):
    result = {'tag': 'button', 'text': {'tag': 'plain_text', 'content': label}, 'type': 'primary',
              'value': {'kind': 'my_agent_model_configuration', 'reference': reference,
                        'decision': operation, 'profile_id': profile_id}}
    if submit:
        result.update(name='submit', action_type='form_submit')
    return result


# LLM: 私人表单仍属原群/本人 owner；公开模态只取原档案声明，空为未知，不从模型名/native工具协议推断；密码不预填。
# 函数用途: 渲染原操作者的连接和输入模态表单；不探测模型，取消放在form外，不被必填校验拦住。
def model_configuration_card(reference, scope_label, public=None):
    public = public or {}
    inputs = []
    for name, label in [('model_backend', '后端（openai_compatible/openai_responses/anthropic_compatible）'),
                        ('api_base', 'API 基础地址'), ('model_name', '模型名称'),
                        ('model_context_window_tokens', '上下文窗口 tokens'),
                        ('input_modalities', '输入模态（如 text, image；留空表示未知）'),
                        ('api_key', 'API Key（编辑留空保留）')]:
        row = {'tag': 'input', 'name': name, 'label': {'tag': 'plain_text', 'content': label},
               'input_type': 'password' if name == 'api_key' else 'text'}
        if name != 'api_key':
            row['default_value'] = (', '.join(validate_input_modalities(public.get(name)))
                                    if name == 'input_modalities' else str(public.get(name, '')))
        inputs.append(row)
    return {'config': {'wide_screen_mode': True, 'update_multi': True, 'enable_forward': False},
            'elements': [{'tag': 'div', 'text': {'tag': 'plain_text', 'content': scope_label + '\n密钥不进入聊天、模型或记忆。'
                         '\n请按模型实际能力声明输入模态：text 文字、image 图像、video 视频；留空表示未知，工具调用协议不代表图像能力。'}},
                         {'tag': 'form', 'name': 'model_configuration', 'elements': [*inputs, _button('保存', reference, 'save', submit=True)]},
                         {'tag':'action','actions':[_button('取消',reference,'cancel')]}]}


# LLM: 签名冻结原 actor/owner/thread/版本/期限/idempotency；发送沿唯一 Delivery，失败不登记无限输入等待。
# 函数用途: 创建并投递模型选择或私人表单，保存真实卡身份；群selection仅为取消单独签当前成员范围。
def send_model_control_card(base, host, owner, *, actor_id, thread_id, target, listing=None, form_origin=None):
    from .model_control_dispatch import ModelFormOrigin
    origin=form_origin or ModelFormOrigin()
    profile_id=origin.profile_id
    control_id = str(uuid4())
    payload = {'kind': 'model_configuration', 'owner': asdict(owner), 'channel': 'feishu', 'actor_id': actor_id,
               'thread_id': thread_id, 'control_id': control_id, 'expires_at': time.time() + 1800,
               'mode': 'selection' if listing is not None else 'form', 'profile_id': profile_id,
               'parent_control_id':origin.parent_control_id,
               **_versions(host, thread_id)}
    if owner.owner_kind=='group' and listing is not None and target==owner.owner_id:
        payload.update(cancellation_presentation={'kind':'group','chat_id':owner.owner_id},cancellation_submitter_policy='current_group_member')
    reference = sign_interaction_reference(host.home_paths.owner_home_dir, payload)
    public = {}
    if profile_id:
        data = read_model_profiles(model_profiles_path(host.home_paths))
        model = data['profiles'].get(profile_id)
        if not model or model.get('capability') != 'agentic':
            raise ValueError('模型不可编辑')
        provider = data['providers'][model['provider_id']]
        if provider.get('auth'):
            raise ValueError('OAuth 模型请使用原终端管理')
        public = {**model, 'api_base': provider['api_base']}
    scope_label = _model_scope_label(owner)
    card = model_selection_card(listing, reference, scope_label) if listing is not None else model_configuration_card(reference, scope_label, public)
    context = DeliveryContext(channel='feishu', target=target, thread_id=thread_id, mode='reply', idempotency_key=control_id)
    receipt = base.delivery_service.deliver_control_card(context, card, summary='模型配置控制卡')
    if not receipt.ok:
        raise ValueError('模型卡未送达')
    row = {**payload, 'reference': reference, 'message_id': receipt.message_id, 'chat_id': receipt.chat_id, 'status': 'active'}
    path = _record_path(host.home_paths.owner_home_dir, control_id)
    with locked_json_path(path):
        write_private_text_file_atomic_unlocked(path, json.dumps(row, ensure_ascii=False))
    return {'ok': True, 'message': '模型卡片已发送。'}


# LLM: 宿主先验签/真实actor/解锁；非取消恢复原群 actor 并核配置 grant，最终 save 再核；cancel 不需 grant，completed 仅同摘要重放。
# 函数用途: 将原操作者卡片回调应用到原 owner 模型目录或原会话选择。
def submit_model_control(base, host, owner, payload, body, *, actor_id=""):
    from ..user_space.group_authorization_context import (
        bind_group_interaction_host,
        group_capability_guard,
    )
    from .model_control_dispatch import dispatch_model_form

    if body["decision"] != "cancel":
        bind_group_interaction_host(base, host, payload, actor_id)
        with group_capability_guard(host, "assistant.configure", thread_id=payload.get("thread_id", "")):
            pass  # 表单准备前短核验；真正保存会在原写事务内再次持锁复核。
    if payload.get('mode')=='selection' and body['decision'] in {'add','edit'}:
        return dispatch_model_form(base,host,owner,payload,body)
    path = _record_path(host.home_paths.owner_home_dir, payload['control_id'])
    with locked_json_path(path):
        row = read_json_object_report(path, context='model.control').payload
        if (not row or row.get('reference') != body['reference'] or row.get('message_id') != body['message_id']
                or row.get('chat_id') != body['chat_id']):
            raise ValueError('模型卡身份不匹配')
        if body['decision']=='cancel':
            from ..user_space.interaction_cancellation import cancel_model_interaction
            return cancel_model_interaction(base,path,row,payload,actor_id)
        fields = body.get('fields', {})
        if not isinstance(fields, dict):
            raise ValueError('模型字段无效')
        submission_digest = _model_submission_digest(host.home_paths.owner_home_dir, row['control_id'], body['decision'], fields)
        if row.get('status') == 'completed':
            if not hmac.compare_digest(str(row.get('submission_digest') or ''), submission_digest):
                raise ValueError('模型卡已用不同参数处理，请重新打开卡片')
            return row['result']
        if row.get('status') != 'active':
            raise ValueError('模型卡已处理')
        if _versions(host, row['thread_id']) != {key: row[key] for key in ('catalog_revisions', 'selection_revision')}:
            raise ValueError('模型卡版本已过期')
        operation = body['decision']
        if row['mode'] == 'selection':
            result = _selection_submit(base, host, owner, row, operation, fields)
        elif operation == 'save':
            result = _save_model_form(host, row, fields)
        else:
            raise ValueError('模型动作无效')
        if result.get('ok') is not True:
            return result
        row.update(status='completed', decision=operation, submission_digest=submission_digest, result=result)
        write_private_text_file_atomic_unlocked(path, json.dumps(row, ensure_ascii=False))
        return result


# LLM: 选择只认原允许列表稳定 ID；新增编辑表单重新签原 owner/thread，目标私聊固定原 actor。
# 函数用途: 处理模型选择或向操作者发私人表单。
def _selection_submit(base, host, owner, row, operation, fields):
    if set(fields) - {'profile_id'}:
        raise ValueError('选择字段无效')
    profile_id = str(fields.get('profile_id') or '')
    listing = execute_model_profile_operation(host, 'list', {}, thread_id=row['thread_id'])
    choices = {item['id']: item for item in listing['profiles'] if item.get('available', True)}
    if operation == 'add' or operation == 'edit':
        if operation == 'edit' and (profile_id not in choices or profile_id == 'default' or choices[profile_id].get('shared')):
            raise ValueError('共享模型不可编辑')
        from .model_control_dispatch import ModelFormOrigin
        return send_model_control_card(base, host, owner, actor_id=row['actor_id'], thread_id=row['thread_id'],
                                       target=row['actor_id'],form_origin=ModelFormOrigin(profile_id if operation=='edit' else '',row['control_id']))
    if operation not in {'select', 'set_default'} or profile_id not in choices:
        raise ValueError('模型选择无效')
    if operation == 'select':
        from ..settings.shared_model_catalog import _admin_profiles_path, shared_catalog_path
        paths = (model_profiles_path(host.home_paths), _admin_profiles_path(host.home_paths), shared_catalog_path(host.home_paths))
        with ExitStack() as stack:
            for path in dict.fromkeys(paths):
                stack.enter_context(locked_json_path(path))
            _require_current(host, row)
            execute_model_profile_operation(host, operation, {'profile_id': profile_id, 'expected_revision': row['selection_revision']},
                                            thread_id=row['thread_id'])
    else:
        # LLM: 原目录锁内再核对卡版本；默认设置不重入同一目录锁。
        # 函数用途: 防止保存前目录/会话变化被旧默认卡覆盖。
        def check(before, after):
            _require_current(host, row)
        with model_profile_write_check(check):
            execute_model_profile_operation(host, operation, {'profile_id': profile_id}, thread_id=row['thread_id'])
    return {'ok': True, 'message': '模型选择已保存；下一次模型请求采用新选择。'}


# LLM: 调用方持原目录或控制记录锁；校验仅读随机代次和线程，不产生网络或新选择。
# 函数用途: 在真正提交前拒绝已变化的模型卡。
def _require_current(host, row):
    if _versions(host, row['thread_id']) != {key: row[key] for key in ('catalog_revisions', 'selection_revision')}:
        raise ValueError('模型卡版本已过期')


# LLM: 写前复核原父卡/目录代次和群授权；输入模态复用原validator，显式空清未知，尚有效的旧五字段卡不覆盖原声明。
# 函数用途: 原子保存原owner的模型连接和显式模态；空key仅保留同endpoint原凭据，不发网络，密钥不进入返回值。
def _save_model_form(host, row, fields):
    from .model_control_dispatch import model_form_parent_error
    parent_error=model_form_parent_error(host,row)
    if parent_error is not None:
        return parent_error
    allowed = {'model_backend', 'api_base', 'model_name', 'model_context_window_tokens', 'api_key'}
    if (set(fields) not in (allowed, allowed | {'input_modalities'})
            or any(not isinstance(value, str) for value in fields.values())):
        raise ValueError('模型表单字段无效')
    guard = catalog_write_check(host)
    # LLM: 原目录锁内复核 frozen 版本并调用原向量保护，不靠预读授权。
    # 函数用途: 保存事务提交前拒绝陈旧目录或受保护端点变更。
    def check(before, after):
        if before['catalog_generation'] != row['catalog_revisions'][0]:
            raise ValueError('模型目录已变化')
        guard(before, after)
    profile_id = row['profile_id'] or str(uuid4())
    form = dict(fields)
    if 'input_modalities' in form:
        form['input_modalities'] = validate_input_modalities(form['input_modalities'])
    data = read_model_profiles(model_profiles_path(host.home_paths))
    if row['profile_id']:
        previous = data['profiles'].get(profile_id)
        if previous is None:
            raise ValueError('模型已不存在')
        provider = data['providers'][previous['provider_id']]
        form = {**previous, **form}
        if form['api_base'].rstrip('/') == provider['api_base']:
            if provider.get('custom_headers'):
                form['model_custom_headers'] = provider['custom_headers']
            if provider.get('session_header'):
                form['model_session_header'] = provider['session_header']
        if not form['api_key']:
            if form['api_base'].rstrip('/') != provider['api_base']:
                raise ValueError('修改接口地址必须重新输入密钥')
            form['api_key'] = provider['api_key']
    with model_profile_write_check(check):
        execute_model_profile_operation(host, 'save_profile' if row['profile_id'] else 'add',
                                        {'profile_id': profile_id, 'profile': form, 'editing': bool(row['profile_id'])},
                                        thread_id=row['thread_id'])
    return {'ok': True, 'message': '模型配置已保存；请重新打开 /model 选择。'}
