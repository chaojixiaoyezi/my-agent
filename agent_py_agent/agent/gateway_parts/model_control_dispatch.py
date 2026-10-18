# LLM: C真实分段Delivery在门外，三个原依赖不构成新权威；prepare/finish均核原完整签名投影，不能从漂移行改派目标；原模型卡投递是有外部副作用的准备操作；原记录dispatch CAS隔离网络与取消，私人子卡须复核原父决定。 C显式非阻塞模式只复用原锁，默认调用与权限不变。
# 模块用途: 在不持原卡锁发网络的前提下投递私人表单，取消赢先时阻止迟到子卡保存配置。
from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from uuid import uuid4

from ..backends.bounded_call import BoundedCallExitUnconfirmedError
from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_private_text_file_atomic_unlocked,
)
from ..common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    RequestExecutionOutcomeUncertainError,
)
from ..common.tool_write_context import ToolWriteContextUnavailable
from ..user_space.interaction_cancellation import already_handled_result
from .multiprocess_global_write import GlobalWriteError


# LLM: 此结构只由宿主选择卡生成；父控制ID进入原签名和原行，不接受表单自称父权限。
# 类用途: 在已有模型投递参数中携带编辑目标与原父卡身份。
@dataclass(frozen=True)
class ModelFormOrigin:
    profile_id: str = ''
    parent_control_id: str = ''


# LLM: 仅携带原Delivery和已验证producer的三个对象引用，不含owner/role/账/新权限；调用方仍逐次消费同一原source。
# 类用途: 给分段模型卡真实外发传依赖，避免往模型配置host或C伪造Agent属性。
@dataclass(frozen=True)
class ModelCardDelivery:
    service: object
    perform: object
    require_current: object


# LLM: C父行锁只透传原事务非阻塞模式；不在锁内再取T或外发。
# LLM: 只在子卡save前复核父卡终态；父取消/尚未提交不授予配置保存，原行锁不跨网络。
# 函数用途: 防止已取消准备操作的迟到私人子卡继续保存模型。
def model_form_parent_error(host,row,*,blocking=True):
    if not row.get('parent_control_id'):
        return None
    from .model_control_cards import _record_path
    path=_record_path(host.home_paths.owner_home_dir,row['parent_control_id'])
    with locked_json_path(path, **({} if blocking else {"blocking": False})):
        parent=read_json_object_report(path,context='model.control.parent').payload
        if parent.get('status')=='completed' and parent.get('decision') in {'add','edit'}:
            return None
        return already_handled_result() if parent.get('status')=='cancelled' else _in_progress()


# LLM: C原GlobalWriteError与actualtyped同对象穿透，不以失败收尾重复消费失活源；分段先原T再锁，Delivery在门外；原MAC载荷直接交prepare，不从存储行重新推导目标或身份。模型准备只接原selection add/edit；先持原卡锁记准确dispatch，网络在锁外，最后CAS不能覆盖成功取消。
# 函数用途: 投递一张绑定原父卡的私人模型表单，重复事件不重复发卡。
def dispatch_model_form(base,host,owner,payload,body,*,delivery=None):
    from .model_control_cards import _ModelCardDeliveryFailed, _record_path, _selection_submit
    path=_record_path(host.home_paths.owner_home_dir,payload['control_id'])
    lock_options = {} if delivery is None else {"blocking": False}

    # LLM: 原prepare本体含行锁与目标检查，真实C先T再锁；None只保Count0原直接调用，不代表许可。
    # 函数用途: 在同一原source中领取原dispatch_id。
    def prepare():
        return _prepare_dispatch(host,path,payload,body,**lock_options)
    row,done = prepare() if delivery is None else delivery.perform(prepare)
    if done is not None:
        return done

    # LLM: 完成/失败只提交同dispatch_id的原行；C退出则保dispatching，不签发普通失败或重发。
    # 函数用途: 把已知原运输结果交回同一短提交。
    def finish(result,receipt=None):
        # LLM: 不在原卡锁内反向取T，回调由实际producer同步执行且再核原source。
        # 函数用途: 保存这一次原dispatch结果。
        def commit():
            return _finish_dispatch(path,row,result,**lock_options,**({} if receipt is None else {"delivery_receipt":receipt}))
        return commit() if delivery is None else delivery.perform(commit)
    try:
        result=_selection_submit(base,host,owner,row,body['decision'],body.get('fields',{}),
                                 **({} if delivery is None else {"blocking":False,"delivery":delivery}))
    except (GlobalWriteError, RequestExecutionContextUnavailable, RequestExecutionOutcomeUncertainError, BoundedCallExitUnconfirmedError, ToolWriteContextUnavailable):
        raise
    except _ModelCardDeliveryFailed as error:
        return finish(None,error.receipt)
    except (OSError,RuntimeError,ValueError,TypeError):
        return finish(None)
    return finish(result)

# LLM: 可选非阻塞参数只复用原锁；C已先原T。原payload来自已验证MAC；完整投影比较先于任何重放/dispatch写。精确真实卡身份/目录代次/同逻辑摘要复核与原model入口一致；fields只含公开profile_id，无秘密落盘。
# 函数用途: 在原行锁内领取一次私人表单投递；已完成或竞争中的操作只返回原事实。
def _prepare_dispatch(host,path,payload,body,*,blocking=True):
    from .model_control_cards import (
        _model_submission_digest,
        _require_current,
        _require_model_control_record,
    )
    with locked_json_path(path, **({} if blocking else {"blocking":False})):
        row=read_json_object_report(path,context='model.control').payload
        _require_model_control_record(row,payload,body)
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
        _validate_form_target(host,row,body['decision'],fields,**({} if blocking else {'blocking':False}))
        row.update(status='dispatching',dispatch_id=str(uuid4()),decision=body['decision'],submission_digest=digest)
        write_private_text_file_atomic_unlocked(path,json.dumps(row,ensure_ascii=False))
        return row,None


# LLM: 原receipt失败保effect/error/provider事实，未发送不能伪unknown；C先原T再非阻塞锁。网络后先核同次prepare已验证的完整签名投影，再只允许同dispatch_id且仍dispatching的原行提交；取消先赢保留其回执，未知发送不自动重投。
# 函数用途: 完成一次投递准备，不让迟到网络成功覆盖取消或其它代次。
def _finish_dispatch(path,original,result,*,blocking=True,delivery_receipt=None):
    from .model_control_cards import _model_control_payload, _require_model_control_record
    with locked_json_path(path, **({} if blocking else {"blocking":False})):
        latest=read_json_object_report(path,context='model.control.dispatch').payload
        _require_model_control_record(latest,_model_control_payload(original),original)
        if latest.get('status')=='cancelled':
            return latest['result']
        if latest.get('status')!='dispatching' or latest.get('dispatch_id')!=original['dispatch_id']:
            return already_handled_result()
        if delivery_receipt is not None:
            latest.update(status='dispatch_failed')
            result=_model_delivery_failure(delivery_receipt)
        elif result is None:
            latest.update(status='dispatch_failed')
            result={'ok':False,'error_code':'IM_INTERACTION_DISPATCH_UNCONFIRMED','effect_outcome':'unknown','message':'私人表单投递结果尚未确认，请关闭原卡并重新打开模型入口。'}
        else:
            latest.update(status='completed')
        latest['result']=result
        write_private_text_file_atomic_unlocked(path,json.dumps(latest,ensure_ascii=False))
        return result


# LLM: 只投影原receipt的effect/error/安全provider数字，不推断是否发出或创建第二运输协议；failed同dispatch永不自动重发。
# 函数用途: 向用户和原卡结果保留准确未发送或未知事实。
def _model_delivery_failure(receipt):
    result={'ok':False,'error_code':receipt.error_code or 'IM_INTERACTION_DISPATCH_UNCONFIRMED',
            'effect_outcome':receipt.effect_outcome,
            'message':('模型卡未发送，请检查原通道状态后重新打开模型入口。' if receipt.effect_outcome=='not_started'
                       else '私人表单投递结果尚未确认，请关闭原卡并重新打开模型入口。')}
    code=receipt.provider_code
    if type(code) is int:
        result['provider_code']=code
    return result


# LLM: 未完成准备不允许子卡保存，不复制原参数/平台异常或假称未发送。
# 函数用途: 返回安全且可重试原卡查询的准备中事实。
def _in_progress():
    return {'ok':False,'error_code':'IM_INTERACTION_IN_PROGRESS','message':'原卡正在准备私人表单，请稍后重试；也可以取消原卡。'}


# LLM: C目标检查可能原线程懒物化，仍在原T内且透传非阻塞；复用原当前授权模型目录，拒绝共享/default/不存在编辑目标；这里只读结构目录，不探测网络。
# 函数用途: 在领取投递前验证公开编辑目标，错误参数不伪装成投递结果未知。
def _validate_form_target(host,row,operation,fields,*,blocking=True):
    if operation!='edit':
        return
    from ..settings.model_profiles import execute_model_profile_operation
    listing=execute_model_profile_operation(host,'list',{},thread_id=row['thread_id'],**({} if blocking else {'blocking':False}))
    choices={item['id']:item for item in listing['profiles'] if item.get('available',True)}
    selected=str(fields.get('profile_id') or '')
    if selected not in choices or selected=='default' or choices[selected].get('shared'):
        raise ValueError('共享模型不可编辑')
