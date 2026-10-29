# LLM: 本模块是R1跨域共用合同，领域仍持有原文件/数据库；后续高代际不能由旧写者覆盖。
# 模块用途: 统一存储代际拒写和未知字段保真接口。
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field


# LLM: 明确机器错误码供原写入口传播，不按异常自然语言决定恢复或降级。
# 类用途: 表示当前代码不能安全写入更高代际存储。
class StoreSchemaNewerError(RuntimeError):
    code = 'STORE_SCHEMA_NEWER'


# LLM: 已知字段由领域声明，扩展字段保持原样，不形成第二业务状态来源。
# 类用途: 保存解码后的已知字段和未来字段。
@dataclass
class VersionedRecord:
    fields: dict = field(default_factory=dict)
    _extra_fields: dict = field(default_factory=dict)

    # LLM: 已知字段优先但保留未理解的未来字段，深复制避免序列化结果反改本记录。
    # 函数用途: 返回待持久化的完整记录，不丢扩展字段。
    def to_record(self) -> dict:
        return deepcopy({**self._extra_fields, **self.fields})


# LLM: 持锁的原写入口须在任何替换前检查，不从文件名或展示状态猜代际。
# 函数用途: 校验写者支持的存储代际。
def require_schema_generation(payload: dict, *, supported: int = 1) -> int:
    generation = payload.get('schema_generation', 0)
    if type(generation) is not int or generation < 0:
        raise ValueError('STORE_SCHEMA_INVALID')
    if generation > supported:
        raise StoreSchemaNewerError('STORE_SCHEMA_NEWER')
    return generation


# LLM: 反序列化必须显式保留未来字段，不修改输入对象。
# 函数用途: 按领域已知字段解码记录。
def decode_versioned_record(payload: dict, known_fields) -> VersionedRecord:
    require_schema_generation(payload)
    keys = frozenset(known_fields)
    return VersionedRecord(deepcopy({key: value for key, value in payload.items() if key in keys}),
                           deepcopy({key: value for key, value in payload.items() if key not in keys}))
