"""资产元数据纯函数；不读取存储，归属和触发提示不授予执行权限。"""
from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from copy import deepcopy

from . import safe_asset_id

INDICATOR_TYPES: tuple[str, ...] = (
    'ipv4', 'ipv6', 'domain', 'url', 'email', 'md5', 'sha1', 'sha256',
    'cve_id', 'hostname', 'port', 'process_name', 'file_path', 'alert_id',
    'monitor_alias', 'image_attachment',
)
HOST_MANAGED_METADATA_KEYS: tuple[str, ...] = (
    'used_by', 'known_pitfalls', 'version', 'versions', 'version_info', 'draft',
    'evolution', 'hidden', 'composition', 'query_health', 'runtime_summary',
)


# LLM: 纯函数，只认明确的两类父引用；错误项丢弃，不修改调用者输入。
# 函数用途: 安全归一化归属提示，去重排序后最多保留十六条。
def normalize_member_of(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    members = set()
    for item in value:
        if isinstance(item, str) and item.count(':') == 1:
            kind, asset_id = item.split(':')
        elif isinstance(item, Mapping):
            kind, asset_id = item.get('kind'), item.get('id')
        else:
            continue
        if kind not in ('skill', 'workflow'):
            continue
        try:
            safe_asset_id(asset_id)
        except ValueError:
            continue
        members.add((kind, asset_id))
    return [{'kind': kind, 'id': asset_id} for kind, asset_id in sorted(members)[:16]]


# LLM: 所有控制字符变为空格，casefold在截断前；清洗结果再次归一化不变。
# 函数用途: 将匹配词压成有界单行文本。
def _matcher_text(value, *, limit, casefold=False):
    if not isinstance(value, str):
        return ''
    text = ' '.join(''.join(' ' if unicodedata.category(char).startswith('C') else char
                            for char in value).split())
    return (text.casefold() if casefold else text)[:limit].strip()


# LLM: 仅消费列表中的字符串；按输出去重保序，安全ID不被清洗成另一身份。
# 函数用途: 规范化一个触发字段并应用独立容量。
def _matcher_values(value, field):
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if not isinstance(item, str):
            continue
        if field == 'indicator_types':
            normalized = item if item in INDICATOR_TYPES else ''
        elif field == 'monitor_ids':
            try:
                normalized = safe_asset_id(item)
            except ValueError:
                continue
        else:
            normalized = _matcher_text(item, limit=32 if field == 'intent_keywords' else 64,
                                       casefold=field == 'intent_keywords')
        if normalized and normalized not in result:
            result.append(normalized)
        if len(result) >= (8 if field == 'alert_sources' else 16):
            break
    return result


# LLM: 输出固定四键；未知类型和字段被丢弃，绝不解释为运行或认证权限。
# 函数用途: 规范化触发匹配提示。
def normalize_trigger_matchers(value) -> dict:
    value = value if isinstance(value, Mapping) else {}
    return {field: _matcher_values(value.get(field), field)
            for field in ('indicator_types', 'monitor_ids', 'intent_keywords', 'alert_sources')}


# LLM: source_system保留；返回深拷贝和排序剥离键名，供工具保存前使用。
# 函数用途: 从模型提供的元数据中剥离宿主维护字段。
def strip_host_managed_metadata(metadata) -> tuple[dict, list[str]]:
    if not isinstance(metadata, Mapping):
        return {}, []
    return (deepcopy({key: value for key, value in metadata.items() if key not in HOST_MANAGED_METADATA_KEYS}),
            sorted(key for key in metadata if key in HOST_MANAGED_METADATA_KEYS))
