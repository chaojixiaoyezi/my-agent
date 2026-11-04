"""Owner资产的内容寻址历史；发布记录不可变，索引可修复，不执行代码。"""
from __future__ import annotations

import hashlib
import json
import re
import stat
import unicodedata
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import (
    MAX_ASSET_BYTES,
    _verify_directory_beneath,
    list_names_beneath,
    locked_json_path,
    read_bytes_beneath,
    safe_asset_id,
    unlink_file_beneath,
    write_text_atomic_beneath,
)
from .asset_metadata import strip_host_managed_metadata

VOLATILE_DESCRIPTOR_KEYS: tuple[str, ...] = (
    'revision', 'scope_layer', 'source_scope', 'workspace_root', 'runtime_summary',
    'last_run_at', 'last_status', 'last_duration_ms', 'run_count',
    'consecutive_failures', 'last_error', 'created_at', 'updated_at',
    'published_at', 'recorded_at',
)
_HISTORY_KINDS = ('script', 'workflow', 'skill', 'monitor')
_HISTORY_PARTS = ('data', 'asset_evolution')
_INDEX_FIELDS = ('version', 'fingerprint', 'code_sha256', 'execution_definition_ref',
                 'published_at', 'legacy', 'restored_from', 'summary_line')


class AssetHistoryError(ValueError):
    """历史存储的结构化失败；不回显载荷或代码。"""

    # LLM: 错误码属于W1合同，调用方按code分支而非解析说明文字。
    # 函数用途: 保存机器可读错误码。
    def __init__(self, code, message=''):
        self.code = code
        super().__init__(code + (': ' + message if message else ''))


# LLM: kind和ID是唯一外部路径段，不清洗成另一个身份。
# 函数用途: 校验历史资产身份。
def _identity(kind, asset_id):
    if kind not in _HISTORY_KINDS:
        raise ValueError('invalid asset history kind')
    safe_asset_id(asset_id)


# LLM: 版本和容量不接受bool，不从文字推导版本。
# 函数用途: 校验有下界的整数参数。
def _integer(value, *, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError('invalid history integer')
    return value


# LLM: digest只认完整小写十六进制，不允许路径或前缀别名。
# 函数用途: 生成安全blob相对路径。
def _blob_parts(digest):
    if not isinstance(digest, str) or re.fullmatch(r'[0-9a-f]{64}', digest) is None:
        raise ValueError('invalid blob digest')
    return (*_HISTORY_PARTS, 'blobs', digest[:2], digest)


# LLM: 与_AssetStore复用同一线程/进程锁原语；各侧账本共用该资产锁，配额另锁。
# 函数用途: 在受控目录内串行化一个资产的历史操作。
@contextmanager
def _asset_lock(owner_home, kind, asset_id):
    _identity(kind, asset_id)
    home = Path(owner_home)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    parts = (*_HISTORY_PARTS, 'versions', kind)
    parent = _verify_directory_beneath(home, parts, create=True)
    with locked_json_path(parent / f'{asset_id}.json'):
        yield


# LLM: JSON序列化唯一且拒绝非有限数值，指纹和blob使用相同字节。
# 函数用途: 生成规范JSON。
def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


# LLM: 原子读取整份快照；缺失返回None，权限和范围错误不吞掉。
# 函数用途: 读取历史JSON对象。
def _read_json(owner_home, parts):
    raw = read_bytes_beneath(Path(owner_home), parts, max_bytes=MAX_ASSET_BYTES * 16)
    return None if raw is None else json.loads(raw)


# LLM: 调用方持领域锁，沿datasource原有portable原子写，先刷文件再替换。
# 函数用途: 原子保存一份历史JSON。
def _write_json(owner_home, parts, value):
    write_text_atomic_beneath(Path(owner_home), parts, _canonical(value) + '\n')


# LLM: 通过既有assets符号复用同一二进制writer；延迟导入避免wave2 store hook初始化环。
# 函数用途: 原子保存原始字节，不做编码转换。
def _write_bytes(owner_home, parts, data):
    from .workspace_assets import write_bytes_atomic_beneath

    write_bytes_atomic_beneath(Path(owner_home), parts, data)


# LLM: 只枚举指定集合，缺失视为空；链接及其他IO错误沿原nofollow边界失败。
# 函数用途: 读取一个受控目录的成员名。
def _names(owner_home, parts):
    try:
        return list_names_beneath(Path(owner_home), parts)
    except FileNotFoundError:
        return []


# LLM: 时间只用于审计和TTL，不决定资产权限；统一持久化UTC。
# 函数用途: 生成或规范化ISO时间。
def _iso_time(value=None):
    instant = datetime.now(timezone.utc) if value is None else datetime.fromisoformat(value)
    if instant.tzinfo is None:
        raise ValueError('history timestamp requires a timezone')
    return instant.astimezone(timezone.utc).isoformat()


# LLM: 控制和换行字符移除，Unicode行分隔符也不保留；不解释文本含义。
# 函数用途: 清洗有界单行展示文字。
def _single_line(text, *, limit):
    if not isinstance(text, str):
        raise ValueError('history text must be a string')
    return ' '.join(''.join(char for char in text
                           if not unicodedata.category(char).startswith('C')
                           and char not in '\u2028\u2029').split())[:limit].strip()


# LLM: 内部空行和缩进保持，只有行尾空白、换行表示和末尾空行不影响版本。
# 函数用途: 规范化代码文本。
def normalize_code_text(text: str) -> str:
    return '\n'.join(line.rstrip(' \t') for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n')).rstrip('\n')


# LLM: 内容指纹不是raw blob身份；surrogateescape保持任意原始字节无损区分。
# 函数用途: 为规范化代码计算指纹。
def code_fingerprint(code: bytes | str) -> str:
    text = code.decode('utf-8', errors='surrogateescape') if isinstance(code, bytes) else code
    return 'sha256:' + hashlib.sha256(normalize_code_text(text).encode('utf-8', errors='surrogateescape')).hexdigest()


# LLM: workflow嵌套/平铺传输统一为canonical平铺定义；剥离运行投影，正文和用户字段仍参与版本语义。
# 函数用途: 生成跨Store和显式发布入口一致的可冻结定义副本。
def _payload_copy(payload):
    if not isinstance(payload, Mapping):
        raise ValueError('history payload must be an object')
    descriptor = payload.get('descriptor')
    if isinstance(descriptor, Mapping) and descriptor.get('kind') == 'workflow':
        payload = {**descriptor, **{key: value for key, value in payload.items() if key != 'descriptor'}}
    clean = {key: value for key, value in payload.items() if key not in VOLATILE_DESCRIPTOR_KEYS}
    if clean.get('kind') == 'script':
        for key in ('code', 'entrypoint_path', 'execution_definition_ref'):
            clean.pop(key, None)
    if 'metadata' in clean:
        clean['metadata'] = strip_host_managed_metadata(clean['metadata'])[0]
        for key in ('_run_summary_generation', '_run_summary_spool'):
            clean['metadata'].pop(key, None)
    return json.loads(_canonical(clean))


# LLM: 规范载荷与payload_blob共用序列化，不含运行统计和宿主metadata。
# 函数用途: 为资产定义计算稳定指纹。
def payload_fingerprint(payload: Mapping) -> str:
    return 'sha256:' + hashlib.sha256(_canonical(_payload_copy(payload)).encode('utf-8')).hexdigest()


# LLM: 侧文件是安全单叶名，None表示不存在；大小写别名不允许覆盖同一个物理叶。
# 函数用途: 校验并复制侧文件快照。
def _side_files(side_files):
    if side_files is None:
        return {}
    if not isinstance(side_files, Mapping):
        raise ValueError('side files must be an object')
    clean = {}
    names = set()
    for name, text in side_files.items():
        safe_asset_id(name)
        if name.casefold() in names or text is not None and not isinstance(text, str):
            raise ValueError('invalid side file')
        names.add(name.casefold())
        if text is not None:
            clean[name] = text
    return clean


# LLM: 组合格式是跨分片合同；侧文件排序，删除标记不成为实体内容。
# 函数用途: 计算一份完整资产快照的去重指纹。
def asset_fingerprint(*, payload: Mapping, code: bytes | str | None = None,
                      side_files: Mapping[str, str | None] | None = None) -> str:
    sides = _side_files(side_files)
    code_fp = code_fingerprint(code) if code is not None else ''
    raw = f'code={code_fp}\npayload={payload_fingerprint(payload)}\nside='
    raw += ','.join(f'{name}:{code_fingerprint(sides[name])}' for name in sorted(sides))
    return 'sha256:' + hashlib.sha256(raw.encode('utf-8')).hexdigest()


# LLM: 只统计固定blob集合中的规范键，不枚举Owner其他状态；调用方持Owner配额锁。
# 函数用途: 计算Owner已有blob字节数。
def _blob_total_bytes(owner_home):
    total = 0
    for prefix in _names(owner_home, (*_HISTORY_PARTS, 'blobs')):
        if re.fullmatch(r'[0-9a-f]{2}', prefix) is None:
            continue
        parts = (*_HISTORY_PARTS, 'blobs', prefix)
        parent = _verify_directory_beneath(Path(owner_home), parts)
        for name in _names(owner_home, parts):
            if re.fullmatch(r'[0-9a-f]{64}', name) is None or not name.startswith(prefix):
                continue
            info = (parent / name).lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('invalid blob leaf')
            total += info.st_size
    return total


# LLM: 配额检查和写入共用Owner锁；已存在的相同字节不再次占配额。
# 函数用途: 按原始字节SHA保存blob。
def blob_put(owner_home, data: bytes, *, max_total_bytes: int = 268435456) -> str:
    if not isinstance(data, bytes):
        raise ValueError('blob data must be bytes')
    _integer(max_total_bytes)
    digest = hashlib.sha256(data).hexdigest()
    home = Path(owner_home)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = _verify_directory_beneath(home, _HISTORY_PARTS, create=True)
    with locked_json_path(root / '.blob-quota'):
        previous = blob_get(home, digest)
        if previous is not None:
            return digest
        if _blob_total_bytes(home) + len(data) > max_total_bytes:
            raise AssetHistoryError('ASSET_HISTORY_QUOTA_EXCEEDED')
        _write_bytes(home, _blob_parts(digest), data)
    return digest


# LLM: blob损坏和缺失都不能作为另一份代码；缺失返回None，身份损坏明确报错。
# 函数用途: 按完整SHA读取原始blob。
def blob_get(owner_home, sha256: str) -> bytes | None:
    raw = read_bytes_beneath(Path(owner_home), _blob_parts(sha256), max_bytes=MAX_ASSET_BYTES * 16)
    if raw is not None and hashlib.sha256(raw).hexdigest() != sha256:
        raise AssetHistoryError('ASSET_HISTORY_BLOB_MISSING', 'blob digest mismatch')
    return raw


# LLM: 内容寻址对象不可变，直接复用完整身份校验。
# 函数用途: 判断指定blob是否存在。
def blob_exists(owner_home, sha256: str) -> bool:
    return blob_get(owner_home, sha256) is not None


# LLM: 发布记录只通过固定kind、ID和正整数版本寻址，不扫描其他资产。
# 函数用途: 读取并验证一个不可变版本记录。
def _record(owner_home, kind, asset_id, version):
    parts = (*_HISTORY_PARTS, 'versions', kind, asset_id, f'v{version}.json')
    record = _read_json(owner_home, parts)
    if record is not None and (not isinstance(record, dict)
            or record.get('schema_version') != 'asset_version.v1'
            or record.get('kind') != kind or record.get('asset_id') != asset_id
            or type(record.get('version')) is not int or record['version'] != version
            or not all(key in record for key in _INDEX_FIELDS)):
        raise ValueError('invalid immutable version record')
    return record


# LLM: 索引行只保留合同元信息，不把完整代码或描述符加载到列表。
# 函数用途: 从发布记录投影索引行。
def _index_row(record):
    return {key: record[key] for key in _INDEX_FIELDS}


# LLM: 重建只在索引缺失、损坏或存在未索引后继时执行；记录按数字而非文件名排序。
# 函数用途: 确定性重建版本索引。
def _rebuild_index(owner_home, kind, asset_id, *, deleted_at=None):
    versions = sorted(int(match[1]) for name in _names(owner_home, (*_HISTORY_PARTS, 'versions', kind, asset_id))
                      if (match := re.fullmatch(r'v([1-9][0-9]*)\.json', name)))
    rows = [_index_row(_record(owner_home, kind, asset_id, version)) for version in versions]
    index = {'schema_version': 'asset_version_index.v1', 'kind': kind, 'asset_id': asset_id,
             'latest': versions[-1] if versions else 0, 'deleted_at': deleted_at, 'versions': rows}
    if rows or deleted_at is not None:
        _write_json(owner_home, (*_HISTORY_PARTS, 'versions', kind, f'{asset_id}.json'), index)
    return index


# LLM: 结构和身份校验不采信索引中的外部路径或版本别名。
# 函数用途: 校验索引形状。
def _valid_index(index, kind, asset_id):
    if (not isinstance(index, dict) or index.get('schema_version') != 'asset_version_index.v1'
            or index.get('kind') != kind or index.get('asset_id') != asset_id
            or type(index.get('latest')) is not int or index['latest'] < 0
            or not isinstance(index.get('versions'), list)):
        return False
    versions = index['versions']
    return (all(isinstance(row, dict) and type(row.get('version')) is int and row['version'] > 0
                and all(key in row for key in _INDEX_FIELDS) for row in versions)
            and [row['version'] for row in versions] == sorted({row['version'] for row in versions})
            and index['latest'] == (versions[-1]['version'] if versions else 0))


# LLM: 调用者持资产锁；补写索引失败后留下的记录必须先被认领，绝不覆盖其版本号。
# 函数用途: 取得可信索引，必要时从不可变记录修复。
def _load_index(owner_home, kind, asset_id):
    parts = (*_HISTORY_PARTS, 'versions', kind, f'{asset_id}.json')
    try:
        index = _read_json(owner_home, parts)
    except (OSError, ValueError, UnicodeDecodeError):
        index = None
    if _valid_index(index, kind, asset_id):
        latest = index['latest']
        record = _record(owner_home, kind, asset_id, latest) if latest else None
        successor = _record(owner_home, kind, asset_id, latest + 1)
        if successor is None and (not latest or record is not None and _index_row(record) == index['versions'][-1]):
            return index
    deleted_at = index.get('deleted_at') if isinstance(index, dict) else None
    return _rebuild_index(owner_home, kind, asset_id, deleted_at=deleted_at)


# LLM: 快照所有blob先落盘，缺省code不是空代码；侧文件的删除标记不存blob。
# 函数用途: 冻结定义、代码和侧文件为内容寻址对象。
def _snapshot_blobs(owner_home, payload, code, side_files):
    return {'payload_blob': blob_put(owner_home, _canonical(_payload_copy(payload)).encode('utf-8')),
            'code_sha256': blob_put(owner_home, code) if code is not None else None,
            'side_blobs': {name: blob_put(owner_home, text.encode('utf-8')) for name, text in sorted(side_files.items())}}


# LLM: 发布版本与storage_revision独立；仅显式WORKFLOW.md、同存储修订或未指定修订、同定义且无正文/侧文件的workflow可补全；新修订、pin变化和回滚仍追加版本，CAS不变。
# 函数用途: 在跨片保存入口之间去重、受限补全或追加完整正式版本。
def record_version(owner_home, kind, asset_id, *, payload: Mapping, code: bytes | None = None,
                   side_files: Mapping[str, str | None] | None = None,
                   execution_definition_ref: str | None = None, storage_revision: int | None = None,
                   pins: Mapping | None = None, summary_line: str = '', published_at: str | None = None,
                   legacy: bool = False, restored_from: int | None = None,
                   expected_latest: int | None = None) -> dict:
    _identity(kind, asset_id)
    if code is not None and not isinstance(code, bytes) or type(legacy) is not bool:
        raise ValueError('invalid version snapshot')
    if pins is not None and not isinstance(pins, Mapping):
        raise ValueError('pins must be an object')
    for value in (storage_revision, expected_latest):
        if value is not None:
            _integer(value)
    if restored_from is not None:
        _integer(restored_from, minimum=1)
    sides = _side_files(side_files)
    payload = _payload_copy(payload)
    if kind == 'workflow':
        if 'WORKFLOW.md' in sides:
            payload['body_markdown'] = sides['WORKFLOW.md']
        elif side_files is None and payload.get('body_markdown'):
            sides['WORKFLOW.md'] = payload['body_markdown']
    fingerprint = asset_fingerprint(payload=payload, code=code, side_files=sides)
    published = _iso_time(published_at)
    summary = _single_line(summary_line, limit=120)
    frozen_pins = json.loads(_canonical(dict(pins))) if pins is not None else {'scripts': {}, 'skills': {}, 'workflows': {}}
    base_pin = payload.get('metadata', {}).get('base_workflow_pin') if kind == 'workflow' else None
    if base_pin is not None:
        if not isinstance(base_pin, Mapping):
            raise ValueError('invalid base workflow pin')
        base_id = safe_asset_id(base_pin.get('id'))
        base_version = _integer(base_pin.get('version'), minimum=1)
        if get_version(owner_home, 'workflow', base_id, base_version) is None:
            raise AssetHistoryError('ASSET_VERSION_NOT_FOUND')
        workflows = frozen_pins.setdefault('workflows', {})
        if not isinstance(workflows, dict) or base_id in workflows and workflows[base_id] != {'version': base_version}:
            raise ValueError('conflicting base workflow pin')
        workflows[base_id] = {'version': base_version}
    with _asset_lock(owner_home, kind, asset_id):
        index = _load_index(owner_home, kind, asset_id)
        latest = index['latest']
        if expected_latest is not None and expected_latest != latest:
            raise AssetHistoryError('ASSET_VERSION_CONFLICT')
        previous = _record(owner_home, kind, asset_id, latest) if latest else None
        blobs = _snapshot_blobs(owner_home, payload, code, sides)
        if (kind == 'workflow' and previous is not None and not previous.get('side_blobs')
                and restored_from is None
                and (storage_revision is None or storage_revision == previous.get('storage_revision'))
                and side_files is not None and 'WORKFLOW.md' in sides):
            prev_payload = _payload_copy(json.loads(_required_blob(owner_home, previous['payload_blob'])))
            if (not prev_payload.get('body_markdown')
                    and {**prev_payload, 'body_markdown': ''} == {**payload, 'body_markdown': ''}):
                record = {**previous, 'fingerprint': fingerprint, **blobs,
                          'pins': {**previous.get('pins', {}), **frozen_pins}}
                if record != previous:
                    _write_json(owner_home, (*_HISTORY_PARTS, 'versions', kind, asset_id, f'v{latest}.json'), record)
                    index = {**index, 'versions': [*index['versions'][:-1], _index_row(record)]}
                    _write_json(owner_home, (*_HISTORY_PARTS, 'versions', kind, f'{asset_id}.json'), index)
                return {'created': False, 'version': latest, 'record': record}
        if (previous is not None and previous['fingerprint'] == fingerprint
                and (kind != 'workflow' or pins is None or previous.get('pins', {}) == frozen_pins)):
            # 原始换行不同仍保留blob，供按raw code_sha256冻结的在途读取者使用。
            return {'created': False, 'version': latest, 'record': previous}
        version = latest + 1
        record = {'schema_version': 'asset_version.v1', 'kind': kind, 'asset_id': asset_id,
                  'version': version, 'fingerprint': fingerprint, **blobs,
                  'execution_definition_ref': execution_definition_ref, 'storage_revision': storage_revision,
                  'pins': frozen_pins, 'summary_line': summary, 'published_at': published,
                  'legacy': legacy, 'restored_from': restored_from}
        record_parts = (*_HISTORY_PARTS, 'versions', kind, asset_id, f'v{version}.json')
        if _read_json(owner_home, record_parts) is not None:
            raise AssetHistoryError('ASSET_VERSION_CONFLICT')
        _write_json(owner_home, record_parts, record)
        index = {**index, 'latest': version, 'deleted_at': None,
                 'versions': [*index['versions'], _index_row(record)]}
        _write_json(owner_home, (*_HISTORY_PARTS, 'versions', kind, f'{asset_id}.json'), index)
        return {'created': True, 'version': version, 'record': record}


# LLM: 最新版本只来自已落盘记录，索引投影损坏时先修复。
# 函数用途: 读取最新发布记录。
def latest_version(owner_home, kind, asset_id) -> dict | None:
    with _asset_lock(owner_home, kind, asset_id):
        latest = _load_index(owner_home, kind, asset_id)['latest']
        return _record(owner_home, kind, asset_id, latest) if latest else None


# LLM: 不使用当前代码兜底，删除资产后仍可按原编号读取。
# 函数用途: 精确读取一个发布版本。
def get_version(owner_home, kind, asset_id, version: int) -> dict | None:
    _identity(kind, asset_id)
    _integer(version, minimum=1)
    return _record(owner_home, kind, asset_id, version)


# LLM: 所有显式过滤条件同时成立；先在索引筛选，只读取最新命中的一份记录。
# 函数用途: 通过代码SHA、执行身份或指纹定位历史。
def find_version(owner_home, kind, asset_id, *, code_sha256: str | None = None,
                 execution_definition_ref: str | None = None, fingerprint: str | None = None) -> dict | None:
    filters = {'code_sha256': code_sha256, 'execution_definition_ref': execution_definition_ref, 'fingerprint': fingerprint}
    with _asset_lock(owner_home, kind, asset_id):
        rows = _load_index(owner_home, kind, asset_id)['versions']
        match = next((row for row in reversed(rows) if all(value is None or row[key] == value
                                                          for key, value in filters.items())), None)
        return _record(owner_home, kind, asset_id, match['version']) if match else None


# LLM: 返回有界元信息窗口，不加载所有历史正文。
# 函数用途: 按新到旧列出发布版本。
def list_versions(owner_home, kind, asset_id, *, limit: int = 20) -> list[dict]:
    _identity(kind, asset_id)
    _integer(limit)
    if limit == 0:
        return []
    with _asset_lock(owner_home, kind, asset_id):
        return list(reversed(_load_index(owner_home, kind, asset_id)['versions'][-limit:]))


# LLM: 仅软删除索引，不触及记录、代码或其他被固定的blob。
# 函数用途: 标记原资产已删除。
def mark_deleted(owner_home, kind, asset_id, *, deleted_at: str) -> None:
    deleted = _iso_time(deleted_at)
    with _asset_lock(owner_home, kind, asset_id):
        index = _load_index(owner_home, kind, asset_id)
        _write_json(owner_home, (*_HISTORY_PARTS, 'versions', kind, f'{asset_id}.json'),
                    {**index, 'deleted_at': deleted})


# LLM: blob必须精确存在且hash匹配，不能从当前资产补齐缺失快照。
# 函数用途: 读取一个历史快照必需对象。
def _required_blob(owner_home, digest):
    if digest is None:
        raise AssetHistoryError('ASSET_HISTORY_BLOB_MISSING')
    raw = blob_get(owner_home, digest)
    if raw is None:
        raise AssetHistoryError('ASSET_HISTORY_BLOB_MISSING')
    return raw


# LLM: 先读取并核验全部对象再写staging；不执行代码，不用当前版本兜底。
# 函数用途: 将指定脚本快照落成独立执行目录。
def materialize_script(owner_home, record: Mapping, staging_root) -> Path:
    if not isinstance(record, Mapping) or record.get('kind') != 'script':
        raise ValueError('materialization requires a script record')
    asset_id, version = record.get('asset_id'), record.get('version')
    _identity('script', asset_id)
    _integer(version, minimum=1)
    if record.get('schema_version') != 'asset_version.v1':
        raise AssetHistoryError('ASSET_VERSION_NOT_FOUND')
    payload = json.loads(_required_blob(owner_home, record.get('payload_blob')))
    if not isinstance(payload, dict):
        raise ValueError('invalid materialization payload')
    entrypoint = safe_asset_id(payload.get('entrypoint', 'main.py'))
    side_blobs = record.get('side_blobs', {})
    if not isinstance(side_blobs, Mapping):
        raise ValueError('invalid side blob map')
    files = {entrypoint: _required_blob(owner_home, record.get('code_sha256'))}
    names = {entrypoint.casefold()}
    for name, digest in side_blobs.items():
        safe_asset_id(name)
        if name.casefold() in names:
            raise ValueError('duplicate materialized filename')
        names.add(name.casefold())
        files[name] = _required_blob(owner_home, digest)
    staging = Path(staging_root)
    staging.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, raw in files.items():
        _write_bytes(staging, (asset_id, f'v{version}', name), raw)
    return staging / asset_id / f'v{version}'


# LLM: 草稿可覆盖但不占正式版本；code和side仍只存内容寻址对象。
# 函数用途: 在资产锁内保存一份草稿。
def save_draft(owner_home, kind, asset_id, *, payload: Mapping, code: bytes | None = None,
               side_files: Mapping[str, str | None] | None = None, base_version: int | None = None) -> dict:
    _identity(kind, asset_id)
    if code is not None and not isinstance(code, bytes):
        raise ValueError('draft code must be bytes')
    if base_version is not None:
        _integer(base_version, minimum=1)
    sides = _side_files(side_files)
    fingerprint = asset_fingerprint(payload=payload, code=code, side_files=sides)
    with _asset_lock(owner_home, kind, asset_id):
        previous = get_draft(owner_home, kind, asset_id)
        now = _iso_time()
        draft = {'schema_version': 'asset_draft.v1', 'kind': kind, 'asset_id': asset_id,
                 'base_version': base_version, 'fingerprint': fingerprint,
                 **_snapshot_blobs(owner_home, payload, code, sides),
                 'created_at': previous['created_at'] if previous else now, 'updated_at': now}
        _write_json(owner_home, (*_HISTORY_PARTS, 'drafts', kind, f'{asset_id}.json'), draft)
        return draft


# LLM: 原子读取草稿，不隐式发布或加载任何当前代码。
# 函数用途: 读取一个资产的草稿。
def get_draft(owner_home, kind, asset_id) -> dict | None:
    _identity(kind, asset_id)
    draft = _read_json(owner_home, (*_HISTORY_PARTS, 'drafts', kind, f'{asset_id}.json'))
    if draft is not None and (not isinstance(draft, dict) or draft.get('schema_version') != 'asset_draft.v1'
                              or draft.get('kind') != kind or draft.get('asset_id') != asset_id):
        raise ValueError('invalid asset draft')
    return draft


# LLM: 只删草稿叶，不删blob或正式版本，读取和删除共用资产锁。
# 函数用途: 删除指定草稿并返回是否存在。
def delete_draft(owner_home, kind, asset_id) -> bool:
    with _asset_lock(owner_home, kind, asset_id):
        if get_draft(owner_home, kind, asset_id) is None:
            return False
        unlink_file_beneath(Path(owner_home), (*_HISTORY_PARTS, 'drafts', kind, f'{asset_id}.json'))
        return True


# LLM: 无法确认时间或结构的对象保留，不把未知当过期。
# 函数用途: 判定一个辅助对象是否超过TTL。
def _expired(value, now, days):
    if not isinstance(value, Mapping):
        return False
    timestamp = value.get('updated_at') or value.get('created_at') or value.get('recorded_at')
    try:
        instant = datetime.fromisoformat(_iso_time(timestamp)) if isinstance(timestamp, str) else None
    except (ValueError, TypeError):
        return False
    return instant is not None and instant + timedelta(days=days) <= now


# LLM: 非草稿对象沿其文件锁重读，只有确定过期才清除；不读取versions或blobs集合。
# 函数用途: 删除一个过期请求或候选。
def _prune_leaf(owner_home, parts, now, days):
    with locked_json_path(Path(owner_home).joinpath(*parts)):
        try:
            value = _read_json(owner_home, parts)
        except (ValueError, UnicodeDecodeError):
            return False
        if not _expired(value, now, days):
            return False
        unlink_file_beneath(Path(owner_home), parts)
        return True


# LLM: 只遍历合同固定层级的辅助JSON；所有正式版本和所有blob保留，避免误收在途引用。
# 函数用途: 惰性清理过期草稿、演进请求与候选。
def prune_history(owner_home, *, now: str, draft_ttl_days: int = 14,
                  request_ttl_days: int = 14, candidate_ttl_days: int = 30) -> dict:
    instant = datetime.fromisoformat(_iso_time(now))
    for days in (draft_ttl_days, request_ttl_days, candidate_ttl_days):
        _integer(days)
    result = {'drafts_removed': 0, 'requests_removed': 0, 'candidates_removed': 0}
    for kind in _HISTORY_KINDS:
        for name in _names(owner_home, (*_HISTORY_PARTS, 'drafts', kind)):
            if not name.endswith('.json'):
                continue
            asset_id = safe_asset_id(name[:-5])
            with _asset_lock(owner_home, kind, asset_id):
                if _expired(get_draft(owner_home, kind, asset_id), instant, draft_ttl_days):
                    unlink_file_beneath(Path(owner_home), (*_HISTORY_PARTS, 'drafts', kind, name))
                    result['drafts_removed'] += 1
        for asset_id in _names(owner_home, (*_HISTORY_PARTS, 'candidates', kind)):
            safe_asset_id(asset_id)
            parent = (*_HISTORY_PARTS, 'candidates', kind, asset_id)
            for name in _names(owner_home, parent):
                if name.endswith('.json'):
                    safe_asset_id(name[:-5])
                    result['candidates_removed'] += int(_prune_leaf(owner_home, (*parent, name), instant, candidate_ttl_days))
    for name in _names(owner_home, (*_HISTORY_PARTS, 'requests')):
        if name.endswith('.json'):
            safe_asset_id(name[:-5])
            result['requests_removed'] += int(_prune_leaf(owner_home, (*_HISTORY_PARTS, 'requests', name), instant, request_ttl_days))
    return result


# LLM: 兼容账本只存结构化回放结论，读取不触发升级或执行。
# 函数用途: 读取原顺序兼容边。
def read_compat_edges(owner_home, kind, asset_id) -> list[dict]:
    _identity(kind, asset_id)
    ledger = _read_json(owner_home, (*_HISTORY_PARTS, 'compat', kind, f'{asset_id}.json'))
    if ledger is None:
        return []
    if not isinstance(ledger, dict) or ledger.get('schema_version') != 'asset_compat.v1' or not isinstance(ledger.get('edges'), list):
        raise ValueError('invalid compatibility ledger')
    return ledger['edges']


# LLM: 回放结论只接受三个枚举，锁内重读后追加观察；相同边的最后观察决定后续查询。
# 函数用途: 记录一次版本兼容性回放结论。
def record_compat_edge(owner_home, kind, asset_id, *, from_version: int, to_version: int,
                       verdict: str, replayed: int = 0, evidence_ref: str = '',
                       recorded_at: str | None = None) -> dict:
    _identity(kind, asset_id)
    _integer(from_version, minimum=1)
    _integer(to_version, minimum=1)
    _integer(replayed)
    if to_version <= from_version or verdict not in ('replay_passed', 'replay_failed', 'inconclusive'):
        raise ValueError('invalid compatibility edge')
    edge = {'from_version': from_version, 'to_version': to_version, 'verdict': verdict,
            'replayed': replayed, 'evidence_ref': evidence_ref, 'recorded_at': _iso_time(recorded_at)}
    with _asset_lock(owner_home, kind, asset_id):
        edges = read_compat_edges(owner_home, kind, asset_id)
        _write_json(owner_home, (*_HISTORY_PARTS, 'compat', kind, f'{asset_id}.json'),
                    {'schema_version': 'asset_compat.v1', 'edges': [*edges, edge]})
    return edge


# LLM: 每次只沿数字相邻且最后结论通过的边前进，失败或缺口立即停止。
# 函数用途: 返回可验证的最高兼容后继，没有通过边时返回None。
def compatible_successor(owner_home, kind, asset_id, from_version: int) -> int | None:
    _integer(from_version, minimum=1)
    edges = {(edge['from_version'], edge['to_version']): edge['verdict']
             for edge in read_compat_edges(owner_home, kind, asset_id)}
    current = from_version
    while edges.get((current, current + 1)) == 'replay_passed':
        current += 1
    return current if current > from_version else None
