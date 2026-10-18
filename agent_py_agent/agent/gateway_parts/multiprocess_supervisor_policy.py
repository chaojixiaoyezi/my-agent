# LLM: P7只运输原RuntimeDB阻塞投影与请求字节，不拥有unknown处置或恢复计数。
# 模块用途: 在原恢复链外增加失败关闭的只读门，任何输出都不授权重放工具。
from dataclasses import dataclass

from ..common.strict_json import load_strict_json
from .multiprocess_records import MultiprocessRecordError
from .recovery import (
    _gateway_processing_failure_count,
    _unplanned_resume_count,
    provider_resume_count,
)


# LLM: block字节由同owner原运行库成功查询取得，null不是未查询/读失败；身份与时效由原调用方保证。
# 类用途: 冻结原请求和恢复阻塞读回，防止可变对象被当作可缓存许可。
@dataclass(frozen=True)
class RecoveryPolicyReadback:
    request_bytes: bytes
    runtime_block_bytes: bytes


# LLM: 原运行库任意阻塞（包括unknown/坏投影）都不自动续跑；计数由原recovery解析，不重置/增加或算第二套退避。
# 函数用途: 返回未改动的请求供原链继续判预算/退避，成功不是工具重放、显式处置或新执行授权。
def require_resume_policy(readback):
    if type(readback) is not RecoveryPolicyReadback or any(type(value) is not bytes for value in
                                                        (readback.request_bytes, readback.runtime_block_bytes)):
        raise MultiprocessRecordError("recovery_policy_readback_invalid", "readback")
    try:
        request = load_strict_json(readback.request_bytes)
        block = load_strict_json(readback.runtime_block_bytes)
    except (TypeError, ValueError, UnicodeError) as error:
        raise MultiprocessRecordError("recovery_policy_readback_invalid", "readback") from error
    if type(request) is not dict:
        raise MultiprocessRecordError("recovery_policy_request_invalid", "request")
    if block is not None:
        raise MultiprocessRecordError("runtime_recovery_blocked", "runtime_block")
    _gateway_processing_failure_count(request)
    _unplanned_resume_count(request)
    provider_resume_count(request)
    return request
