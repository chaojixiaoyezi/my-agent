# LLM: 迁移只在原配置锁内操作同一权威；高代际拒绝、备份精确前像、交换失败复核后回滚，不修改运行时许可。
# 模块用途: 为显式配置升级提供可审计的私有备份、验证和原子替换事务。
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

from ..common.json_io import locked_json_path
from ..common.nofollow_fs import read_bytes_beneath, write_bytes_atomic_beneath
from ..contracts.optimization_contracts import decode_versioned_record, require_schema_generation

_MAX_CONFIGURATION_BYTES = 1_048_576


# LLM: 交换后无法确认回滚时必须保留备份位置和原异常链，不能报告原配置未变。
# 类用途: 表示原配置的提交/恢复结果仍待人工复核。
class ConfigurationRollbackUnconfirmed(RuntimeError):
    code = 'CONFIG_MIGRATION_ROLLBACK_UNCONFIRMED'


# LLM: 缺失、坏JSON、链接与超限均拒绝，不能把未知前像当空配置后覆盖。
# 函数用途: 有界读取原配置的准确字节。
def _configuration_bytes(path: Path) -> bytes:
    value = read_bytes_beneath(path.parent, (path.name,), max_bytes=_MAX_CONFIGURATION_BYTES,
                               require_dir_fd=True)
    if value is None:
        raise FileNotFoundError(path)
    return value


# LLM: 显式调用方提供纯transform/validate；原锁覆盖前像、备份与交换，未知字段由R1合同保留。
# 函数用途: 验证后升级一份JSON配置，出错时保留或恢复原权威字节。
def migrate_configuration(path, transform, validate, *, known_fields=None, target_generation: int = 1) -> dict:
    path = Path(path).absolute()
    if type(target_generation) is not int or target_generation != 1:
        raise ValueError('unsupported_configuration_generation')
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('configuration_symlink_forbidden')
    with locked_json_path(path):
        before = _configuration_bytes(path)
        original = json.loads(before)
        if not isinstance(original, dict):
            raise ValueError('configuration_object_required')
        generation = require_schema_generation(original)
        if generation == target_generation:
            return {'applied': False, 'schema_generation': generation, 'backup_name': None}
        backup_name = f'{path.name}.migration-{hashlib.sha256(before).hexdigest()}.bak'
        backup = read_bytes_beneath(path.parent, (backup_name,), max_bytes=_MAX_CONFIGURATION_BYTES,
                                   require_dir_fd=True)
        if backup is None:
            write_bytes_atomic_beneath(path.parent, (backup_name,), before)
        elif backup != before:
            raise ValueError('configuration_backup_conflict')
        keys = set() if known_fields is None else set(known_fields)
        record = decode_versioned_record(original, keys)
        updated = transform(deepcopy(original))
        if not isinstance(updated, dict):
            raise ValueError('configuration_transform_object_required')
        record.fields = deepcopy(updated)
        payload = record.to_record()
        payload['schema_generation'] = target_generation
        require_schema_generation(payload)
        if validate(deepcopy(payload)) is not True:
            raise ValueError('configuration_validation_failed')
        encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n').encode()
        if len(encoded) > _MAX_CONFIGURATION_BYTES:
            raise ValueError('configuration_output_too_large')
        if _configuration_bytes(path) != before:
            raise ValueError('configuration_source_changed')
        try:
            write_bytes_atomic_beneath(path.parent, (path.name,), encoded)
            if _configuration_bytes(path) != encoded:
                raise OSError('configuration_swap_unconfirmed')
        except Exception:
            try:
                current = _configuration_bytes(path)
                if current == encoded:
                    write_bytes_atomic_beneath(path.parent, (path.name,), before)
                elif current != before:
                    raise ValueError('configuration_changed_before_rollback')
                if _configuration_bytes(path) != before:
                    raise ValueError('configuration_rollback_unconfirmed')
            except Exception as rollback_error:
                raise ConfigurationRollbackUnconfirmed(backup_name) from rollback_error
            raise
        return {'applied': True, 'schema_generation': target_generation, 'backup_name': backup_name}
