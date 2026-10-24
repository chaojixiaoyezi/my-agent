# LLM: 目录投影只读白名单元信息，绝非包完整性/安装许可；纯订单投影复用原seq和异sha规则。
# 模块用途: 分离learnpack目录显示与原构建校验、确认安装权威。
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..common.nofollow_fs import read_text_beneath
from .learnpack_store import _MAX_BUILD_SCAN_COUNT, _modified_time


# LLM: rows为未核验白名单，不带ZIP内容、私有run身份或安装许可；扫描/解码遗漏明确partial。
# 类用途: 携带一次构建目录元信息观察与完整性事实。
@dataclass(frozen=True)
class BuildMetadataReport:
    rows: tuple[dict, ...]
    completeness: str = 'complete'
    errors: tuple[str, ...] = ()


# LLM: 每个事件仅来自同次已读订单/安装表；无关坏安装包名不污染正常字符串组，保留原整数seq判据。
# 函数用途: 建立单次调用的订单取代事件分组，不读磁盘。
def order_events(orders, installs):
    groups = {}
    for order in orders:
        groups.setdefault(order.package_id, []).append((order.seq, order.version, order.sha256))
    for row in installs:
        if isinstance(row.get('package_id'), str) and isinstance(row.get('seq'), int):
            groups.setdefault(row.get('package_id'), []).append((row['seq'], str(row.get('version') or ''), str(row.get('sha256') or '')))
    return groups


# LLM: 与原_superseded_by的tuple max及严格seq/异sha等价，不受时钟倒退或最新同sha事件遮蔽。
# 函数用途: 从本次事件组判断候选订单被哪一版取代。
def superseded_by_events(order, events):
    later = [event for event in events if event[2] != order.sha256 and event[0] > order.seq]
    if not later:
        return ''
    _seq, version, sha256 = max(later)
    return f'{version}（{sha256[:12]}）'


# LLM: 扫描顺序沿原mtime最新优先/2000上限；只读JSON不读ZIP，损坏/越界节点不伪装完整空库。
# 函数用途: 返回构建库存的轻量元信息，显式说明尚未执行完整性校验。
def build_metadata_report(store):
    rows, errors = [], []
    try:
        with os.scandir(store.root / 'builds') as entries:
            paths = sorted((Path(entry.path) for entry in entries if entry.name.endswith('.json')),
                           key=_modified_time, reverse=True)
    except FileNotFoundError:
        return BuildMetadataReport(())
    except OSError:
        return BuildMetadataReport((), 'partial', ('SELF_MAP_BUILD_SCAN_UNAVAILABLE',))
    if len(paths) >= _MAX_BUILD_SCAN_COUNT:
        errors.append('SELF_MAP_BUILD_SCAN_BOUNDED')
    for path in paths[:_MAX_BUILD_SCAN_COUNT]:
        try:
            raw = read_text_beneath(store.root, ('builds', path.name))
            rows.append(_build_metadata(json.loads(raw), path.stem))
        except (OSError, UnicodeError, TypeError, ValueError, KeyError):
            errors.append('SELF_MAP_BUILD_METADATA_UNREADABLE')
    return BuildMetadataReport(tuple(sorted(rows, key=lambda row: row['built_at'])),
                               'partial' if errors else 'complete', tuple(sorted(set(errors))))


# LLM: 文件名必须为同sha，字段严格白名单和基本类型验证；integrity_state固定未核验，不宣称可安装。
# 函数用途: 抽取一条构建显示元信息，丢掉任意附加字段。
def _build_metadata(raw, sha):
    fields = ('sha256', 'kind', 'package_id', 'version', 'built_at')
    if not isinstance(raw, dict) or not all(type(raw.get(key)) is str for key in fields):
        raise ValueError('invalid build metadata')
    if re.fullmatch('[0-9a-f]{64}', sha) is None or raw['sha256'] != sha:
        raise ValueError('invalid build identity')
    if type(raw.get('file_count')) is not int or raw['file_count'] < 0:
        raise ValueError('invalid file count')
    return {key: raw[key] for key in (*fields, 'file_count')} | {'integrity_state': 'not_verified'}
