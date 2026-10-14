# LLM: 回执配置只从精确调度 run 的冻结 delivery 装配，模型 attrs 或当前 job 更新不能替代；普通后台不使用该合同。
# 模块用途: 连接已有调度通知策略和主模型完成合同，不拥有调度状态、投递队列或用户身份。
from __future__ import annotations

from ..contracts.background_public_receipt import (
    CONTRACT_KEY,
    RECEIPT_FAILURE_CODES,
    RESULT_SCHEMA,
    BackgroundPublicReceiptError,
)
from ..scheduler.delivery_policy import normalize_delivery
from ..scheduler.execution import execution_run


# LLM: 只读本 request 精确活动run，缺省always；local/none不引入公开表达成本。
# 函数用途: 判定本片是否已明确使用条件公开回执。
def scheduled_explicit_public_receipt(agent: object, request: object) -> bool:
    run = execution_run(agent, request)
    if run is None:
        return False
    delivery = normalize_delivery(run.get('delivery'))
    return delivery['mode'] == 'origin' and delivery.get('reply_policy', 'always') == 'explicit'


# LLM: 预先移除同名可携带旧字段，只有当前冻结explicit+origin能够装配；关开关不能悄悄退回原始final外发。
# 函数用途: 在后台真实执行前准备完成合同，保留现有其它交付要求。
def configure_background_public_receipt(agent: object, request: object, params: object) -> None:
    contract = dict(getattr(params, 'delivery_contract', None) or {})
    contract.pop(CONTRACT_KEY, None)
    if scheduled_explicit_public_receipt(agent, request):
        if not getattr(agent.config, 'background_public_receipt_contract_enabled', True):
            raise BackgroundPublicReceiptError('BACKGROUND_RECEIPT_CONTRACT_DISABLED')
        contract[CONTRACT_KEY] = {'schema': RESULT_SCHEMA, 'reply_policy': 'explicit'}
    params.delivery_contract = contract or None


# LLM: 新业务投递只接受explicit完成投影，冷重投沿原冻结信封不重新解释；附件和非空公开正文不能被静默声明取消。
# 函数用途: 在原外发入口判定本次公开回执是否明确静默或违反完成合同。
def public_receipt_delivery_block(
    agent: object, request: object, receipt: object, content: str, *, has_attachments: bool, frozen_retry: object,
) -> str:
    if frozen_retry is not None or not scheduled_explicit_public_receipt(agent, request):
        return ''
    if not isinstance(receipt, dict) or receipt.get('schema') != RESULT_SCHEMA:
        return 'failed'
    if receipt.get('notification_intent') == 'internal_only':
        return 'failed' if content.strip() or has_attachments else 'suppressed'
    return '' if receipt.get('notification_intent') == 'publish' else 'failed'


# LLM: 仅消费已登记的来源/完成协议失败状态；不能依body分类失败，也不重试刚执行过的业务工具。
# 函数用途: 让后台不可交付终态沿原异常结算，而不是按缺投递回执释放并再次执行。
def raise_background_receipt_failure(result: object, *, persist_result: object = None) -> None:
    if str(getattr(result, 'runtime_status', '') or '') != 'failed':
        return
    code = str(getattr(result, 'runtime_reason', '') or '')
    if code in RECEIPT_FAILURE_CODES:
        if callable(persist_result):
            persist_result(result)
        raise BackgroundPublicReceiptError(code)
