# LLM: 取消仅在调用方已验签/actor/chat及原记录锁内执行；不改模型目录、有效凭据、任务状态或其它卡。
# 模块用途: 保存原卡取消终态与安全操作者回执，为原toast/就地更新共用同一事实。
from __future__ import annotations

import json

from ..common.json_io import write_private_text_file_atomic_unlocked


# LLM: audit来自宿主身份索引与时间，不复制表单字段；群重复点击只展示第一个获锁者，不覆盖原决定。
# 函数用途: 将已记录取消者的安全字段变成中文用户回执。
def cancellation_result(audit,subject):
    actor = audit.get('actor_id','')
    name = audit.get('actor_name') or actor or '原操作者'
    when = audit.get('clicked_at','')
    return {'ok':True,'decision':'cancelled','cancelled_by':audit,'clicked_by':audit,
            'message':f'已由{name}（{actor}）取消{subject}。时间：{when}。'}


# LLM: 凭据取消不包含密文/secret或目标URL，不从卡片展示字段决定身份。
# 函数用途: 返回准确原凭据卡已取消的安全事实。
def credential_cancel_result(row):
    audit = row.get('cancelled_by') or {'actor_id':row.get('submitter_actor_id',row.get('actor_id','')),'actor_name':'','clicked_at':''}
    return cancellation_result(audit,'凭据输入')


# LLM: 原model记录锁由调用方持有；仅active可取消，completed不撤回配置，cancelled重放不重审计、不写秘密或新任务。
# 函数用途: 原子关闭准确原模型选择/配置卡，记录首次真实取消者。
def cancel_model_interaction(agent,path,row,payload,actor):
    if row.get('status')=='cancelled':
        return row['result']
    if row.get('status') not in {'active','dispatching','dispatch_failed'} or not actor:
        return already_handled_result()
    from .interaction_actor_audit import interaction_audit_host, record_interaction_actor
    audit_host=interaction_audit_host(agent.home_paths.root,payload["owner"])
    audit = record_interaction_actor(audit_host,payload,decision='cancel',actor=actor)
    result = cancellation_result(audit,'模型卡')
    row.update(status='cancelled',decision='cancel',cancelled_by=audit,result=result)
    write_private_text_file_atomic_unlocked(path,json.dumps(row,ensure_ascii=False))
    return result


# LLM: 已完成状态单调，取消只反馈原卡已处理，不回滚任何完成操作或授予新权限。
# 函数用途: 返回不含原配置和秘密的取消竞争失败回执。
def already_handled_result():
    return {'ok':False,'decision':'already_handled','error_code':'IM_INTERACTION_ALREADY_HANDLED',
            'message':'原卡已处理，取消没有撤销或修改已完成的操作。'}
