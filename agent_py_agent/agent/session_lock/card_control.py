# LLM: 密码卡nonce/状态放原SessionLockStore同一用户行，仅控制输入卡生命周期；不是密码权限账，取消不触碰hash/活跃/消息队列。
# 模块用途: 为密码首设、解锁、改密提供签名原卡及跨进程串行的取消/提交CAS，密码仍只交原UnlockService。
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

from ..common.json_io import locked_json_path
from ..user_space.interaction_actor_audit import (
    interaction_actor_text,
    interaction_audit_host,
    record_interaction_actor,
)
from ..user_space.interaction_refs import sign_interaction_reference, verify_interaction_reference
from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home

_COLUMNS={'card_nonce':"TEXT NOT NULL DEFAULT ''",'card_mode':"TEXT NOT NULL DEFAULT ''",
    'card_status':"TEXT NOT NULL DEFAULT ''",'card_expires_at':'REAL NOT NULL DEFAULT 0',
    'card_message_id':"TEXT NOT NULL DEFAULT ''",'card_chat_id':"TEXT NOT NULL DEFAULT ''",'card_actor_json':"TEXT NOT NULL DEFAULT '{}'"}


# LLM: 原数据库事务内显式迁移同一行UI字段，不另造授权表；既有密码/活跃数据保持不变。
# 函数用途: 兼容已有会话锁数据库，使每张输入卡有独立nonce和终态。
def initialize_card_columns(conn):
    conn.execute('BEGIN IMMEDIATE')
    existing={row[1] for row in conn.execute('PRAGMA table_info(private_chat_locks)')}
    for name,definition in _COLUMNS.items():
        if name not in existing:
            conn.execute('ALTER TABLE private_chat_locks ADD COLUMN '+name+' '+definition)
    conn.commit()


# LLM: 锁名只来自稳定user哈希，覆盖开卡/回执/提交/取消，服务内短SQL另开连接不会和长SQLite写锁互锁。
# 函数用途: 跨进程串行同用户密码卡控制，不改变认证锁状态。
def _lock(store,user):
    return locked_json_path(Path(store._path+'.card-'+hashlib.sha256(user.encode()).hexdigest()+'.json'))


# LLM: 新卡替换同用户旧nonce，签名固定个人owner/actor/mode；不自动解锁、首设或记录活跃。
# 函数用途: 在原用户行登记一张待输入卡并返回签名，不发送网络请求。
def prepare_password_card(adapter,user,mode):
    if mode not in {'set','unlock','change'} or not user:
        raise ValueError('密码卡模式无效')
    store=adapter._unlock.store
    owner={'provider':'feishu','owner_kind':'user','owner_id':user}
    home=resolve_owner_home(adapter.my_agent_home,OwnerIdentity(**owner)).home_dir
    payload={'kind':'session_password','channel':'feishu','owner':owner,'actor_id':user,
             'nonce':uuid.uuid4().hex,'mode':mode,'expires_at':time.time()+600}
    reference=sign_interaction_reference(home,payload)
    with _lock(store,user),store._connect() as conn:
        conn.execute("INSERT INTO private_chat_locks(user_id) VALUES(?) ON CONFLICT(user_id) DO NOTHING",(user,))
        conn.execute("UPDATE private_chat_locks SET card_nonce=?,card_mode=?,card_status='pending',card_expires_at=?,card_message_id='',card_chat_id='',card_actor_json='{}' WHERE user_id=?",
            (payload['nonce'],mode,payload['expires_at'],user))
        conn.commit()
    return reference,payload


# LLM: 回执只绑定本nonce，异步旧卡回执不能覆盖新卡；发送失败不声称卡片真实送达。
# 函数用途: 保存原密码卡实际message/chat身份或失败状态。
def bind_password_card(adapter,payload,receipt):
    store=adapter._unlock.store
    with _lock(store,payload['actor_id']),store._connect() as conn:
        conn.execute('UPDATE private_chat_locks SET card_message_id=?,card_chat_id=?,card_status=? WHERE user_id=? AND card_nonce=?',
            (str(receipt.get('message_id') or ''),str(receipt.get('chat_id') or ''),
             'pending' if receipt.get('message_id') and receipt.get('chat_id') else 'failed',payload['actor_id'],payload['nonce']))
        conn.commit()


# LLM: SDK身份、MAC及真实卡回执先核对；取消可过期且只退休本nonce，提交与取消同锁，不能取消已完成密码动作。
# 函数用途: 处理密码卡一次明确动作，密码值仅瞬时交原服务，不落状态/审计/回复。
def resolve_password_card(adapter,norm,value):
    from .feishu_cards import PasswordActionOutcome, password_action_outcome
    actor=str(norm.get('operator_open_id') or '')
    decision='cancel' if value.get('session_lock_action')=='pwd_cancel' else 'submit'
    payload=verify_interaction_reference(Path(adapter.my_agent_home),str(value.get('reference') or ''),
        channel='feishu',actor_id=actor,decision=decision)
    if payload.get('kind')!='session_password' or value.get('user_id')!=actor:
        raise ValueError('密码卡身份不匹配')
    expected={'set':'pwd_set','unlock':'pwd_unlock','change':'pwd_change'}.get(payload.get('mode'))
    if decision!='cancel' and value.get('session_lock_action')!=expected:
        raise ValueError('密码动作不匹配')
    store=adapter._unlock.store
    with _lock(store,actor):
        row=_original_row(store,actor,payload,norm)
        if row['card_status']=='cancelled' and decision!='cancel':
            return PasswordActionOutcome(False,'这张输入卡已取消，未解锁或修改密码。')
        if row['card_status']=='cancelled':
            facts=json.loads(row['card_actor_json'])
            return PasswordActionOutcome(True,interaction_actor_text(facts)+' 已取消这张输入卡；锁和密码未改变。')
        if row['card_status']!='pending':
            return PasswordActionOutcome(False,'原卡已处理，取消没有撤销或修改已完成的操作。')
        host=interaction_audit_host(adapter.my_agent_home,payload['owner'])
        facts=record_interaction_actor(host,payload,decision='cancel' if decision=='cancel' else expected,actor=actor)
        result=(PasswordActionOutcome(True,interaction_actor_text(facts)+' 已取消这张输入卡；锁和密码未改变。')
            if decision=='cancel' else password_action_outcome(adapter._unlock,value,norm.get('form_value') or {}))
        status='cancelled' if decision=='cancel' else 'completed' if result.ok else 'pending'
        with store._connect() as conn:
            conn.execute('UPDATE private_chat_locks SET card_status=?,card_actor_json=? WHERE user_id=? AND card_nonce=?',
                (status,json.dumps(facts,ensure_ascii=False),actor,payload['nonce']))
            conn.commit()
        return result


# LLM: 一张卡必须同时匹配原nonce/mode和飞书实际message/chat；他人、转发或旧代次不修改原行。
# 函数用途: 只读核验原密码输入卡，缺回执不能被当成真实送达。
def _original_row(store,actor,payload,norm):
    with store._connect() as conn:
        row=conn.execute('SELECT * FROM private_chat_locks WHERE user_id=?',(actor,)).fetchone()
    if (row is None or row['card_nonce']!=payload.get('nonce') or row['card_mode']!=payload.get('mode')
            or not row['card_message_id'] or row['card_message_id']!=norm.get('message_id')
            or not row['card_chat_id'] or row['card_chat_id']!=norm.get('chat_id')):
        raise ValueError('密码卡已失效或不是原私人卡')
    return row
