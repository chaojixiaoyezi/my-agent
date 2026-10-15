# LLM: 原模型卡投递是有外部副作用的准备操作；原记录dispatch CAS隔离网络与取消，私人子卡须复核原父决定。
# 模块用途: 在不持原卡锁发网络的前提下投递私人表单，取消赢先时阻止迟到子卡保存配置。
from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from uuid import uuid4

from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_private_text_file_atomic_unlocked,
)
from ..user_space.interaction_cancellation import already_handled_result


# LLM: 此结构只由宿主选择卡生成；父控制ID进入原签名和原行，不接受表单自称父权限。
# 类用途: 在已有模型投递参数中携带编辑目标与原父卡身份。
@dataclass(frozen=True)
class ModelFormOrigin:
    profile_id: str = ''
    parent_control_id: str = ''


# LLM: 只在子卡save前复核父卡终态；父取消/尚未提交不授予配置保存，原行锁不跨网络。
# 函数用途: 防止已取消准备操作的迟到私人子卡继续保存模型。
def model_form_parent_error(host,row):
    if not row.get('parent_control_id'):
        return None
    from .model_control_cards import _record_path
    path=_record_path(host.home_paths.owner_home_dir,row['parent_control_id'])
    with locked_json_path(path):
        parent=read_json_object_report(path,context='model.control.parent').payload
        if parent.get('status')=='completed' and parent.get('decision') in {'add','edit'}:
            return None
        return already_handled_result() if parent.get('status')=='cancelled' else _in_progress()


# LLM: 模型准备只接原selection add/edit；先持原卡锁记准确dispatch，网络在锁外，最后CAS不能覆盖成功取消。
# 函数用途: 投递一张绑定原父卡的私人模型表单，重复事件不重复发卡。
def dispatch_model_form(base,host,owner,payload,body):
    from .model_control_cards import _record_path, _selection_submit
    path=_record_path(host.home_paths.owner_home_dir,payload['control_id'])
    row,done=_prepare_dispatch(host,path,body)
    if done is not None:
        return done
    try:
        result=_selection_submit(base,host,owner,row,body['decision'],body.get('fields',{}))
    except (OSError,RuntimeError,ValueError,TypeError):
        return _finish_dispatch(path,row,None)
    return _finish_dispatch(path,row,result)


# LLM: 精确真实卡身份/目录代次/同逻辑摘要复核与原model入口一致；fields只含公开profile_id，无秘密落盘。
# 函数用途: 在原行锁内领取一次私人表单投递；已完成或竞争中的操作只返回原事实。
def _prepare_dispatch(host,path,body):
    from .model_control_cards import _model_submission_digest, _require_current
    with locked_json_path(path):
        row=read_json_object_report(path,context='model.control').payload
        if not row or any(row.get(key)!=body.get(key) for key in ('reference','message_id','chat_id')):
            raise ValueError('模型卡身份不匹配')
        fields=body.get('fields',{})
        if not isinstance(fields,dict) or set(fields)-{'profile_id'}:
            raise ValueError('选择字段无效')
        digest=_model_submission_digest(host.home_paths.owner_home_dir,row['control_id'],body['decision'],fields)
        if row.get('status')=='cancelled':
            return row,already_handled_result()
        if row.get('status')=='completed':
            if not hmac.compare_digest(str(row.get('submission_digest') or ''),digest):
                raise ValueError('模型卡已用不同参数处理')
            return row,row['result']
        if row.get('status')!='active':
            return row,_in_progress()
        _require_current(host,row)
        _validate_form_target(host,row,body['decision'],fields)
        row.update(status='dispatching',dispatch_id=str(uuid4()),decision=body['decision'],submission_digest=digest)
        write_private_text_file_atomic_unlocked(path,json.dumps(row,ensure_ascii=False))
        return row,None


# LLM: 网络后的结果只允许同dispatch_id且仍dispatching的原行提交；取消先赢保留其回执，未知发送不自动重投。
# 函数用途: 完成一次投递准备，不让迟到网络成功覆盖取消或其它代次。
def _finish_dispatch(path,original,result):
    with locked_json_path(path):
        latest=read_json_object_report(path,context='model.control.dispatch').payload
        if latest.get('status')=='cancelled':
            return latest['result']
        if latest.get('status')!='dispatching' or latest.get('dispatch_id')!=original['dispatch_id']:
            return already_handled_result()
        if result is None:
            latest.update(status='dispatch_failed')
            result={'ok':False,'error_code':'IM_INTERACTION_DISPATCH_UNCONFIRMED','message':'私人表单投递结果尚未确认，请关闭原卡并重新打开模型入口。'}
        else:
            latest.update(status='completed')
        latest['result']=result
        write_private_text_file_atomic_unlocked(path,json.dumps(latest,ensure_ascii=False))
        return result


# LLM: 未完成准备不允许子卡保存，不复制原参数/平台异常或假称未发送。
# 函数用途: 返回安全且可重试原卡查询的准备中事实。
def _in_progress():
    return {'ok':False,'error_code':'IM_INTERACTION_IN_PROGRESS','message':'原卡正在准备私人表单，请稍后重试；也可以取消原卡。'}


# LLM: 复用原当前授权模型目录，拒绝共享/default/不存在编辑目标；这里只读结构目录，不探测网络。
# 函数用途: 在领取投递前验证公开编辑目标，错误参数不伪装成投递结果未知。
def _validate_form_target(host,row,operation,fields):
    if operation!='edit':
        return
    from ..settings.model_profiles import execute_model_profile_operation
    listing=execute_model_profile_operation(host,'list',{},thread_id=row['thread_id'])
    choices={item['id']:item for item in listing['profiles'] if item.get('available',True)}
    selected=str(fields.get('profile_id') or '')
    if selected not in choices or selected=='default' or choices[selected].get('shared'):
        raise ValueError('共享模型不可编辑')
