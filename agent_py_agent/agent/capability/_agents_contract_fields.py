# LLM: 仅供 AGENTS 第一期的纯合同使用；不读权限、文件、网络或宿主请求，不接入旧路径。
# 模块用途: 提供三个冻结接口共同的结构化校验错误。

import math
from dataclasses import fields
from types import UnionType
from typing import get_args, get_origin


# LLM: 错误只保留码与字段名，不回显可能含私人来源的输入；接口调用方按 code 判断。
# 类用途: 让接口校验失败可以按稳定错误码处理，而不是解析异常文字。
class AgentsContractError(ValueError):
    # LLM: 创建异常没有外部副作用；字段必须是合同中的字段名而非输入正文。
    # 函数用途: 记录校验拒绝的原因与位置。
    def __init__(self, code: str, field: str):
        self.code = code
        self.field = field
        super().__init__(f"{code}:{field}")


# LLM: 只接受完整对象与已声明字段；不补旧记录，不将模型附带字段变成权限。
# 函数用途: 从 JSON 对象提取合同字段，缺失或多余字段明确拒绝。
def decode_fields(record_type, payload: object) -> dict[str, object]:
    if type(payload) is not dict:
        raise AgentsContractError("AGENTS_CONTRACT_OBJECT_REQUIRED", "payload")
    names = tuple(field.name for field in fields(record_type))
    missing = next((name for name in names if name not in payload), None)
    if missing is not None:
        raise AgentsContractError("AGENTS_CONTRACT_FIELD_MISSING", missing)
    if set(payload) != set(names):
        raise AgentsContractError("AGENTS_CONTRACT_FIELD_UNKNOWN", "payload")
    return dict(payload)


# LLM: 标量、精确类型元组和可空宿主事实；bool不冒充int，JSON不冒充宿主类。
# 函数用途: 核验不可变字段声明，不做宽松转换，不把类实例本身视为可信授权。
def scalar_matches(value: object, declared_type) -> bool:
    if get_origin(declared_type) is tuple:
        return _tuple_matches(value, get_args(declared_type))
    if get_origin(declared_type) is UnionType:
        return any(scalar_matches(value, member) for member in get_args(declared_type))
    if declared_type is float:
        return _finite_number(value)
    return type(value) is declared_type


# LLM: 可变列表只在显式JSON解码处转换；CAS每个位置均严格验型，无I/O。
# 函数用途: 区分字符串序列与异型固定CAS元组，排除bool/float的相等别名。
def _tuple_matches(value, declared_types) -> bool:
    if type(value) is not tuple:
        return False
    if declared_types[-1] is Ellipsis:
        return all(scalar_matches(item, declared_types[0]) for item in value)
    return len(value) == len(declared_types) and all(
        scalar_matches(item, kind) for item, kind in zip(value, declared_types))


# LLM: 大整数转浮点会溢出；合同必须返回非法事实而非泄出无结构异常，无外部副作用。
# 函数用途: 检查可以表示的有限数值，拒绝 bool、NaN、Inf 与溢出。
def _finite_number(value) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


# LLM: 每个合同有独立的 v1 完整 JSON；未知版本拒绝，不隐式升级。
# 函数用途: 核对标量类型和协议版本，错误只报告声明字段。
def validate_scalars(record) -> None:
    for field in fields(record):
        if not scalar_matches(getattr(record, field.name), field.type):
            raise AgentsContractError("AGENTS_CONTRACT_FIELD_INVALID", field.name)
    if record.schema_version != 1:
        raise AgentsContractError("AGENTS_CONTRACT_SCHEMA_UNSUPPORTED", "schema_version")


# LLM: 身份和 refs 不解析自然语言；空值只在合同明确允许的字段中接受。
# 函数用途: 拒绝空身份、首尾空白和控制字符。
def require_text(value: str, field: str, allow_empty: bool = False) -> None:
    if type(value) is not str or value != value.strip():
        raise AgentsContractError("AGENTS_CONTRACT_FIELD_INVALID", field)
    if (not value and not allow_empty) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise AgentsContractError("AGENTS_CONTRACT_FIELD_INVALID", field)


# LLM: revision 允许首次为零，但版本必须正整数；不让布尔和负值进入 CAS。
# 函数用途: 检查非负或正整数版本。
def require_counter(value: int, field: str, allow_zero: bool = False) -> None:
    if type(value) is not int or value < (0 if allow_zero else 1):
        raise AgentsContractError("AGENTS_CONTRACT_FIELD_INVALID", field)


# LLM: 摘要固定使用小写 SHA256；这里只验证格式，不证明内容真实或有权读取。
# 函数用途: 核对内容和版本绑定摘要的序列化格式。
def require_digest(value: str, field: str) -> None:
    if type(value) is not str or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise AgentsContractError("AGENTS_CONTRACT_FIELD_INVALID", field)
