# LLM: B2 仅定义请求合同，不接现有写入口；身份来自 P0/P3/P5，网关另从认证上下文裁决。
# 模块用途: 严格解析 worker 的全局写请求，不把请求正文当授权。
import copy

from ..common.strict_json import load_strict_json
from ..user_space.owner_resolver import OwnerIdentity
from .multiprocess_records import (
    MultiprocessRecordError,
    _canonical_bytes,
    _canonical_owner,
    _expect_schema,
    _identifier,
    _integer,
    _sha,
    _shape,
    validate_record,
)

# 全局写请求字段全集，不携带角色、目录、凭据或其它可自报的授权。
_GLOBAL_WRITE_FIELDS = ("schema_version", "c_run_id", "worker", "of", "instance_id", "process_identity", "owner",
                        "request_id", "execution_attempt_id", "lease_epoch", "lease_owner", "request_binding",
                        "operation_id", "idempotency_key", "resource", "action", "payload")
# 单份全局写请求至多一 MiB；超过即拒绝，不截断或落盘。
_GLOBAL_WRITE_MAX_BYTES = 1024 * 1024


# LLM: 复用 P0 结构化错误信封，不回显参数或敏感正文。
# 类用途: 报告全局写请求被拒的原因。
class GlobalWriteError(MultiprocessRecordError):
    error_code = "GLOBAL_WRITE_REJECTED"


# LLM: 纯校验入口，不读写文件或认证身份。
# 函数用途: 验证请求并返回独立副本。
def validate_global_write(payload):
    try:
        _shape(payload, _GLOBAL_WRITE_FIELDS, "request")
        _expect_schema(payload, "multiprocess_global_write.v1")
        _validate_write_identity(payload)
        if type(payload["payload"]) is not dict:
            raise GlobalWriteError("payload_not_object", "payload")
        _json_types(payload["payload"])
        if len(_canonical_bytes(payload)) > _GLOBAL_WRITE_MAX_BYTES:
            raise GlobalWriteError("request_too_large", "request")
        return copy.deepcopy(payload)
    except MultiprocessRecordError as error:
        raise GlobalWriteError(error.reason, error.field) from error
    except (RecursionError, ValueError, TypeError, UnicodeError) as error:
        raise GlobalWriteError("invalid_json", "payload") from error


# LLM: 复用 P0 的实例、owner 和标识符规范，不产生第二套身份或接手证明。
# 函数用途: 检查全局写信封中完整的请求、分区、原执行轮及操作身份。
def _validate_write_identity(payload):
    for field in ("c_run_id", "instance_id", "request_id", "execution_attempt_id", "lease_owner",
                  "operation_id", "idempotency_key", "resource", "action"):
        _identifier(payload[field], field)
    for field in ("of", "lease_epoch"):
        _integer(payload[field], field)
    _integer(payload["worker"], "worker", 0)
    if payload["worker"] >= payload["of"]:
        raise GlobalWriteError("partition_out_of_range", "worker")
    _sha(payload["request_binding"], "request_binding")
    if _canonical_owner(payload["owner"]) == OwnerIdentity.local_main():
        raise GlobalWriteError("worker_owner_required", "owner")
    _expect_schema(payload["process_identity"], "multiprocess_process_identity.v1")
    validate_record(payload["process_identity"])


# LLM: json.dumps 会把数字键和 tuple 悄悄转换；按原顺序拒绝非 JSON 形态，对象循环抽平但不修改输入。
# 函数用途: 保证指令正文规范编码前后不变形。
def _json_types(value):
    if type(value) is dict:
        _json_object(value)
        return
    if type(value) is list:
        for item in value:
            _json_types(item)
        return
    if value is not None and type(value) not in (str, int, float, bool):
        raise GlobalWriteError("non_json_value", "payload")


# LLM: 只由正文类型校验调用；键先于值检查，保留原错误码、顺序及递归失败关闭语义。
# 函数用途: 检查对象字符串键及每个值，不让深层条件堆在入口函数。
def _json_object(value):
    for key, item in value.items():
        if type(key) is not str:
            raise GlobalWriteError("non_json_key", "payload")
        _json_types(item)


# LLM: 编码不执行请求，不生成身份或幂等键。
# 函数用途: 返回全局写请求的规范字节。
def encode_global_write(payload):
    return _canonical_bytes(validate_global_write(payload))


# LLM: 解码不补字段，不授予权限。
# 函数用途: 从严格 JSON 读取请求。
def decode_global_write(content):
    if type(content) is not bytes or len(content) > _GLOBAL_WRITE_MAX_BYTES:
        raise GlobalWriteError("invalid_request_bytes", "request")
    try:
        payload = load_strict_json(content)
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise GlobalWriteError("invalid_json", "request") from error
    return validate_global_write(payload)
