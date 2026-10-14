# LLM: 最终调用来源只由原模型调用账和真实生成出口签发；模型正文/metadata 不拥有签发权，不新增身份账。
# 模块用途: 把模型响应与产生它的请求、运行及用途绑定，防止辅助调用或别的运行被误当成主回复。
from __future__ import annotations

from dataclasses import dataclass, field

_HOST_SEAL = object()


# LLM: 只读来源来自真实完成调用；私有 seal 不序列化，字典或模型声称的来源不能恢复它。
# 类用途: 保存本进程最终响应的准确调用身份；公开投影只含身份，不含提示词或正文。
@dataclass(frozen=True)
class ResponseProvenance:
    call_id: str
    logical_call_id: str
    request_id: str
    run_id: str
    task_id: str
    thread_id: str
    attempt_id: str
    purpose: str
    _seal: object = field(default=None, repr=False, compare=False)

    # LLM: 展示投影不能用于恢复签发权或授权；新增身份需同步调用账与回执冻结测试。
    # 函数用途: 提供可归档的来源字段，排除宿主私有签发标记。
    def to_dict(self) -> dict[str, str]:
        return {key: str(getattr(self, key)) for key in (
            'call_id', 'logical_call_id', 'request_id', 'run_id', 'task_id', 'thread_id', 'attempt_id', 'purpose',
        )}


# LLM: 请求用途仅由本 run 原 pending 回执阶段决定，不能从响应内容或任务自然语言推断。
# 函数用途: 区分主业务调用和已登记的零工具公开回执修复。
def response_call_purpose(params: object) -> str:
    state = getattr(params, 'live_archive_state', None)
    phase = state.get('_pending_natural_user_reply') if isinstance(state, dict) else None
    if isinstance(phase, dict):
        return 'background_receipt_repair' if phase.get('kind') == 'background_final_receipt' else 'natural_user_reply'
    return 'main'


# LLM: 只有真实生成完成且原 ledger 已 finished 才签发；provider 自带同名字段必须覆盖，不信 payload metadata。
# 函数用途: 为本次真实响应盖上宿主来源，把当前调用身份带到收口与交付。
def stamp_response_provenance(params: object, ledger: object, call_id: str, response: object) -> None:
    response.response_provenance = None
    response.internal_response = ''
    response.public_receipt = None
    record = next((item for item in ledger.records() if item.call_id == call_id), None)
    if record is None or record.status != 'finished':
        return
    metadata = record.metadata
    origin = ResponseProvenance(
        call_id=record.call_id, logical_call_id=str(metadata.get('logical_call_id') or ''),
        request_id=record.request_id, run_id=record.run_id,
        task_id=str(metadata.get('task_id') or ''), attempt_id=str(metadata.get('attempt_id') or ''),
        thread_id=str(metadata.get('thread_id') or ''),
        purpose=str(metadata.get('purpose') or 'main'), _seal=_HOST_SEAL,
    )
    response.response_provenance = origin
    state = getattr(params, 'live_archive_state', None)
    if isinstance(state, dict):
        state['_latest_response_call_id'] = call_id


# LLM: 来源核验只守客观身份/用途，不保证正文相关性或质量；缺少私有 seal 的持久字典必须拒绝。
# 函数用途: 核对最终响应是否来自当前运行和准许的调用用途。
def response_provenance_error(params: object, response: object, *, purposes: tuple[str, ...] = ('main',)) -> str:
    origin = getattr(response, 'response_provenance', None)
    if not isinstance(origin, ResponseProvenance) or origin._seal is not _HOST_SEAL:
        return 'MODEL_RESPONSE_PROVENANCE_MISSING'
    for key in ('request_id', 'run_id', 'task_id', 'attempt_id'):
        if str(getattr(params, key, '') or '') != str(getattr(origin, key)):
            return 'MODEL_RESPONSE_PROVENANCE_MISMATCH'
    attributes = getattr(params, 'task_attributes', None)
    expected_thread = str(attributes.get('conversation_thread_id') or '') if isinstance(attributes, dict) else ''
    if expected_thread != origin.thread_id:
        return 'MODEL_RESPONSE_PROVENANCE_MISMATCH'
    state = getattr(params, 'live_archive_state', None)
    if isinstance(state, dict) and str(state.get('_latest_response_call_id') or '') != origin.call_id:
        return 'MODEL_RESPONSE_PROVENANCE_MISMATCH'
    if origin.purpose not in purposes:
        return 'MODEL_RESPONSE_PURPOSE_MISMATCH'
    return ''


# LLM: 公开来源投影只接受本进程宿主签发对象；不从 dict 重建可信调用身份。
# 函数用途: 给结果及现有投递信封提供无正文来源事实。
def response_provenance_projection(response: object) -> dict[str, str] | None:
    origin = getattr(response, 'response_provenance', None)
    return origin.to_dict() if isinstance(origin, ResponseProvenance) and origin._seal is _HOST_SEAL else None
