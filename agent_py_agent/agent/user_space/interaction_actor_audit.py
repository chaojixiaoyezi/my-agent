# LLM: 点击者身份只接受上游已认证SDK/签名裁决，姓名只读原身份索引作展示；不读表单、签名引用、正文或秘密。
# 模块用途: 为各类卡片保存统一真实点击者/时间，并沿原owner审计日志记录已核验点击事实，不代替原状态CAS。
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from ..agent_core.audit_dispatch import _logger_for
from ..audit.records import LogParams
from ..common.log_redaction import redact_sensitive_text
from .home_layout import home_paths
from .identity_store import lookup_provider_identity
from .owner_resolver import OwnerIdentity, home_paths_with_owner, resolve_owner_home


# LLM: adapter没有Agent时只做纯规范路径投影，不初始化owner/会话、不读取配置密钥，审计仍落原owner位置。
# 函数用途: 给密码/人设卡构造轻量审计宿主，保留唯一身份索引和审计账。
def interaction_audit_host(root, owner):
    paths=home_paths_with_owner(home_paths(root),resolve_owner_home(root,OwnerIdentity(**owner)))
    return SimpleNamespace(home_paths=paths,config=SimpleNamespace(audit_enabled=True,audit_log_path=str(paths.owner_audit_log_jsonl)))


# LLM: 必须在准确原卡赢得CAS后或持其锁时调用；日志只声明已核验点击，不虚报业务已完成，返回字段可存原记录。
# 函数用途: 读取点击人的可信展示名并记录ID/姓名/UTC时间；无姓名用稳定ID，永不以姓名授权。
def record_interaction_actor(agent,payload,*,decision,actor):
    if not isinstance(actor,str) or not actor or decision not in {'cancel','approved','denied','confirm','decline','save','select','set','pwd_set','pwd_unlock','pwd_change'}:
        raise ValueError('点击身份或动作无效')
    channel=str(payload.get('channel') or 'feishu')
    name=''
    try:
        record=lookup_provider_identity(agent.home_paths,provider=channel,provider_subject_id=actor)
        name=record.display_name if record else ''
    except (OSError,TypeError,ValueError,AttributeError):
        pass
    name=redact_sensitive_text(' '.join(str(name).split())[:80])
    facts={'actor_id':actor,'actor_name':name or actor,'clicked_at':datetime.now(timezone.utc).isoformat(timespec='seconds')}
    logger=_logger_for(agent.config)
    if logger is not None:
        logger.log(LogParams(action='IM_CARD_CLICK',user_id=actor,channel=channel,target_type='interaction',
            target_id=str(payload.get('input_id') or payload.get('permission_id') or payload.get('control_id') or payload.get('nonce') or '')[:200],
            details={'kind':str(payload.get('kind') or ''),'owner':payload.get('owner') or {},
                     'decision':decision,'phase':'verified_click',**facts}))
    return facts


# LLM: 此展示不承担机器裁决；只消费已保存安全字段，不引用用户表单或卡片正文。
# 函数用途: 为群/私人原卡回执说明实际点击人和时间。
def interaction_actor_text(facts):
    return f"{facts['actor_name']}（{facts['actor_id']}）于 {facts['clicked_at']}"
