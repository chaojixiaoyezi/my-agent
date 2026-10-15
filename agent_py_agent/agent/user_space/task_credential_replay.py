# LLM: 仅沿原凭据与输入账恢复同卡回执；不接受新秘密、不跨代次/提交者，也不把过期确认变成新工作。
# 模块用途: 在凭据已保存但输入发布或HTTP回执失败后，让原卡安全重试。
from __future__ import annotations

import time

from .task_credential_storage import read_credential_row


# LLM: 签名payload来自上游核验；重复提交仍核对精确owner/scope/代次，不只依赖可复制的引用字符串。
# 函数用途: 核对这次卡片事件对应原记录，拒绝旧卡、跨任务和不同真实消息。
def assert_credential_callback_identity(row, payload, body):
    if not row or any(row.get(key) != body.get(key) for key in ('reference', 'message_id', 'chat_id')):
        raise ValueError('私人卡身份不匹配')
    keys = ('owner', 'scope', 'credential_ref', 'expires_at')
    if any(row.get(key) != payload.get(key) for key in keys) or row.get('generation', 1) != payload.get('generation', 1):
        raise ValueError('原卡代次或任务不匹配')


# LLM: 同终态/同实际提交者才确认，fields从不参与重放；原输入pending只在原任务活跃且未过期时继续发布。
# 函数用途: 判断已保存决定能否恢复原发布；已过期但输入已结算的卡只确认旧事实，不产生Wake。
def credential_callback_replay(agent, row, body, actor):
    from ..conversation.input_dependencies import input_dependency_current, input_dependency_path
    expected = {'ready':'submit', 'cancelled':'cancel'}.get(row.get('status'))
    if not expected or body.get('decision') != expected or row.get('submitter_actor_id') != actor:
        raise ValueError('原卡决定或提交者不匹配')
    dependency = read_credential_row(input_dependency_path(agent, row['input_id']), stage='input_replay_read')
    same_scope = all(dependency.get('scope', {}).get(key) == value for key, value in row.get('scope', {}).items())
    if (not dependency or dependency.get('owner') != row.get('owner') or not same_scope
            or dependency.get('reference') != row['credential_ref'] or dependency.get('provider') != 'credential'
            or dependency.get('input_id') != row['input_id'] or not input_dependency_current(agent, dependency)):
        raise ValueError('原任务或输入绑定已失效')
    status = dependency.get('status')
    if status not in {'pending', row['status']}:
        raise ValueError('原输入已有不同决定')
    expired = time.time() >= row['expires_at']
    if status == 'pending' and expired:
        raise ValueError('原卡已过期，不能恢复新工作')
    return not expired
