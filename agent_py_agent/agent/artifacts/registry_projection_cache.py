# LLM: 仅缓存已经安全读取的精确raw解析；不提供文件读取、授权、操作提交或内容未变的证明。
# 模块用途: 在8MiB进程内LRU里复用不可变登记表投影，锁不跨IO或JSON解析。
from __future__ import annotations

import json
import os
import sys
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType

from ..common.nofollow_fs import open_directory_beneath

PARSER_VERSION = 3
CACHE_MAX_BYTES = 8 * 1024 * 1024
_CACHE = OrderedDict()
_LOCK = threading.Lock()
_CACHE_BYTES = 0
_EMPTY_PROXY = MappingProxyType({})


# LLM: 错误只存不可变构造参数，不保留异常traceback；所有payload递归冻结，无共享可变列表或字典。
# 类用途: 表达一条非空JSONL的原行号、解析结果或解析错误。
@dataclass(frozen=True)
class RegistryRow:
    line: int
    payload: object = None
    error: tuple | None = None
    record: object = None
    artifact_id: str = ""
    record_error: tuple | None = None

    # LLM: 还原原JSON/ValueError类型和消息供两种业务口径分别裁决，不共享可变异常实例。
    # 函数用途: 创建本次独立的解析异常。
    def failure(self) -> Exception:
        kind, args = self.error
        classes = {"json": json.JSONDecodeError, "value": ValueError, "type": TypeError}
        return classes[kind](*args)


# LLM: raw是完整已验证文本，records包含全部历史行和坏行；latest_records与diagnostic_rows预聚合加速最新报告读取。
# 类用途: 保存一个解析版本的不可变登记表投影。
@dataclass(frozen=True)
class RegistryProjection:
    raw: str
    records: tuple[RegistryRow, ...]
    parse_error: bool
    parser_version: int
    latest_records: Mapping[str, object] = _EMPTY_PROXY
    diagnostic_rows: tuple[RegistryRow, ...] = ()
    line_count: int = 0


# LLM: root由宿主调用范围传入，逐段no-follow打开验证身份；身份仅作缓存分区，不替代每次完整raw读取。
# 函数用途: 用规范词法路径和当前目录设备/inode隔离缓存。
def trusted_root_identity(root) -> tuple:
    descriptor = open_directory_beneath(root, ())
    try:
        info = os.fstat(descriptor)
        return str(root), info.st_dev, info.st_ino
    finally:
        os.close(descriptor)


# LLM: 常规浅层metadata走快速递归转换，极端深层JSON回退显式栈，兼顾吞吐与递归深度安全。
# 函数用途: 将JSON容器转换为不可变MappingProxyType/tuple。
def _freeze_tree(value):
    if type(value) in (str, int, float, bool) or value is None:
        return value
    if isinstance(value, Mapping):
        if not value:
            return _EMPTY_PROXY
        return MappingProxyType({key: _freeze_tree(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        if not value:
            return ()
        return tuple(_freeze_tree(child) for child in value)
    return value


# LLM: 将缓存内不可变容器还原为全新dict/list，调用方任意修改不影响缓存投影。
# 函数用途: 快速解冻已缓存的metadata或payload树。
def _thaw_tree(value):
    if type(value) in (str, int, float, bool) or value is None:
        return value
    if isinstance(value, Mapping):
        return {key: _thaw_tree(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_tree(child) for child in value]
    return value


# LLM: 显式栈保留深层JSON原语义，避免冻结/解冻比原JSON解析更早耗尽Python递归深度。
# 函数用途: 在不可变Mapping/tuple与独立dict/list之间转换整棵JSON树。
def _map_tree(value, *, freeze: bool):
    try:
        return _freeze_tree(value) if freeze else _thaw_tree(value)
    except RecursionError:
        pass
    stack, result = [(value, False)], {}
    while stack:
        current, expanded = stack.pop()
        mapping = isinstance(current, Mapping)
        sequence = isinstance(current, (list, tuple))
        if not mapping and not sequence:
            result[id(current)] = current
            continue
        children = tuple(current.values()) if mapping else current
        if not expanded:
            stack.append((current, True))
            stack.extend((child, False) for child in children)
            continue
        if mapping:
            converted = {key: result[id(child)] for key, child in current.items()}
            result[id(current)] = MappingProxyType(converted) if freeze else converted
        else:
            converted = [result[id(child)] for child in current]
            result[id(current)] = tuple(converted) if freeze else converted
    return result[id(value)]


# LLM: 对外返回递归独立副本，不允许调用方改坏缓存metadata。
# 函数用途: 解冻一条已解析payload用于原业务记录转换。
def detached_payload(value):
    return _map_tree(value, freeze=False)


# LLM: 一次splitlines和每非空行一次loads；坏行保留原异常构造参数，预聚合最新有效记录与诊断行。
# 函数用途: 从完整raw生成不可变JSONL投影。
def parse_registry_projection(raw: str) -> RegistryProjection:
    lines = raw.splitlines()
    rows, latest, diagnostics = [], {}, []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        row = _parse_row(number, line)
        rows.append(row)
        if row.error is not None or row.record_error is not None or row.record is None:
            diagnostics.append(row)
        elif row.record.artifact_id:
            latest[row.record.artifact_id] = row.record
    return RegistryProjection(
        raw,
        tuple(rows),
        any(row.error for row in rows),
        PARSER_VERSION,
        MappingProxyType(latest),
        tuple(diagnostics),
        len(lines),
    )


# LLM: 仅在旧raw以换行结尾且是新raw严格前缀时增量解析新增行；结果与全量重解析完全一致。
# 函数用途: 让追加写场景只解析新增尾部行而不重扫历史行。
def _append_registry_projection(base: RegistryProjection, raw: str) -> RegistryProjection:
    suffix = raw[len(base.raw):]
    lines = suffix.splitlines()
    if not lines:
        return base
    new_rows = []
    latest = dict(base.latest_records)
    diagnostics = list(base.diagnostic_rows)
    parse_error = base.parse_error
    start_line = base.line_count + 1
    for offset, line in enumerate(lines):
        if not line.strip():
            continue
        row = _parse_row(start_line + offset, line)
        new_rows.append(row)
        if row.error is not None:
            parse_error = True
        if row.error is not None or row.record_error is not None or row.record is None:
            diagnostics.append(row)
        elif row.record.artifact_id:
            latest[row.record.artifact_id] = row.record
    return RegistryProjection(
        raw,
        base.records + tuple(new_rows),
        parse_error,
        PARSER_VERSION,
        MappingProxyType(latest),
        tuple(diagnostics),
        base.line_count + len(lines),
    )


# LLM: 先在原JSON容器上执行唯一record转换，再只冻结metadata树；历史payload只保留不可变metadata映射供恢复扫描。
# 函数用途: 只解析/转换一次并保存不可变历史payload与独立可复制的最新记录。
def _parse_row(number: int, line: str) -> RegistryRow:
    from .registry import _record_from_payload

    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        return RegistryRow(number, error=("json", (exc.msg, exc.doc, exc.pos)))
    except (TypeError, ValueError) as exc:
        kind = "type" if isinstance(exc, TypeError) else "value"
        return RegistryRow(number, error=(kind, (str(exc),)))
    record, record_error = None, None
    try:
        record = _record_from_payload(payload)
    except (OverflowError, RecursionError) as exc:
        record_error = (type(exc).__name__, str(exc))
    if isinstance(payload, dict):
        artifact_id = str(payload.get("artifact_id") or "")
        if record is not None:
            frozen_meta = _map_tree(record.metadata, freeze=True)
            record = replace(record, metadata=frozen_meta)
            frozen_payload = MappingProxyType({"metadata": frozen_meta}) if "metadata" in payload else _EMPTY_PROXY
        else:
            frozen_payload = (
                MappingProxyType({"metadata": _map_tree(payload["metadata"], freeze=True)})
                if "metadata" in payload
                else _EMPTY_PROXY
            )
    else:
        artifact_id = ""
        frozen_payload = _map_tree(payload, freeze=True)
    return RegistryRow(number, frozen_payload, record=record, artifact_id=artifact_id, record_error=record_error)


# LLM: 用O(1)结构化字节估算代替逐节点对象图遍历，消除缓存未命中时的递归sizeof与临时dict分配风暴。
# 函数用途: 计算缓存驻留预算，超大投影不插入而不截断解析。
def _memory_cost(value) -> int:
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], RegistryProjection):
        key, projection = value
        return (
            sys.getsizeof(key)
            + sys.getsizeof(projection)
            + sys.getsizeof(projection.raw)
            + sys.getsizeof(projection.records)
            + len(projection.records) * 128
        )
    return sys.getsizeof(value)


# LLM: 缓存锁只包字典LRU取放，解析/体积计算在锁外；支持严格换行前缀的增量追加解析。
# 函数用途: 获取本次安全读取文本的精确解析投影。
def registry_projection(identity: tuple, raw: str) -> RegistryProjection:
    key = identity, PARSER_VERSION
    previous: RegistryProjection | None = None
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            if cached[0].raw == raw:
                _CACHE.move_to_end(key)
                return cached[0]
            previous = cached[0]
    if (
        previous is not None
        and len(raw) > len(previous.raw)
        and raw.startswith(previous.raw)
        and (not previous.raw or previous.raw.endswith("\n"))
    ):
        projection = _append_registry_projection(previous, raw)
    else:
        projection = parse_registry_projection(raw)
    cost = _memory_cost((key, projection))
    _put(key, projection, cost)
    return projection


# LLM: 每root/version只留最近一版；新raw超预算时也移除旧版，LRU全局驻留不超过8MiB。
# 函数用途: 有界保存解析结果，大raw仍原样交回调用方而不进入缓存。
def _put(key, projection: RegistryProjection, cost: int) -> None:
    global _CACHE_BYTES
    with _LOCK:
        old = _CACHE.pop(key, None)
        if old is not None:
            _CACHE_BYTES -= old[1]
        if cost > CACHE_MAX_BYTES:
            return
        while _CACHE and _CACHE_BYTES + cost > CACHE_MAX_BYTES:
            _, (_, evicted_cost) = _CACHE.popitem(last=False)
            _CACHE_BYTES -= evicted_cost
        _CACHE[key] = projection, cost
        _CACHE_BYTES += cost
