"""逐轮固定方法软推荐：纯库存索引、确定性匹配和当前Owner的有界侧文件投影。

本模块不执行资产、不读取脚本代码、不调用模型或记忆；输出只能进入动态尾段。
"""
import hashlib
import json
import re
from copy import deepcopy
from dataclasses import fields
from functools import lru_cache
from pathlib import Path, PurePosixPath

from ..assets.descriptor import AssetDescriptor
from ..assets.topology import build_asset_topology
from ..common.nofollow_fs import read_bytes_beneath
from .inbound_indicators import extract_indicators

# Tier-2 每轮推荐段的估算 token 上限（UTF-8 字节 / 4）。
TURN_RECOMMENDATION_MAX_TOKENS = 800


# LLM: 文本只用于展示，去控制字符并按字节截断，不把展示值用于授权或路径解析。
# 函数用途: 生成可安全放入一行JSON的短文本。
def _short(value, *, max_bytes=192):
    if not isinstance(value, str):
        return ''
    return ' '.join(''.join(char for char in value if char.isprintable() or char.isspace()).split()).encode('utf-8')[:max_bytes].decode('utf-8', errors='ignore')


# LLM: 仅接受序列中的字符串，错误字段按缺失处理；首次出现顺序不变。
# 函数用途: 归一有界的标识符或字段名列表。
def _strings(value, *, limit=16, max_bytes=128):
    if not isinstance(value, (list, tuple)):
        return []
    return list(dict.fromkeys(text for item in value if (text := _short(item, max_bytes=max_bytes))))[:limit]


# LLM: 标准描述符负责安全ID与脱敏，额外存储字段不进入索引；损坏记录不影响其他候选。
# 函数用途: 取得只含匹配所需稳定字段的资产投影。
def _inventory_rows(values):
    allowed = {item.name for item in fields(AssetDescriptor)} | {'schema_version'}
    result = []
    for value in values if isinstance(values, (list, tuple)) else ():
        try:
            raw = value.to_dict() if isinstance(value, AssetDescriptor) else value
            if not isinstance(raw, dict):
                continue
            row = AssetDescriptor.from_dict({key: item for key, item in raw.items() if key in allowed}).to_dict()
        except (TypeError, ValueError):
            continue
        if row['enabled'] is not True:
            continue
        metadata = row['metadata']
        result.append({key: row[key] for key in ('kind', 'id', 'name', 'source_scope', 'revision', 'io_contract', 'refs')} | {
            'metadata': {key: metadata[key] for key in ('trigger_matchers', 'hidden', 'composition', 'alias', 'stable_id') if key in metadata}})
    found = {}
    for row in sorted(result, key=_order):
        found.setdefault((row['kind'], row['id']), row)
    return list(found.values())


# LLM: 排名只取种类、来源、名称和准确ID，不取时间、自然语言推断或调用状态。
# 函数用途: 为评分相同时提供完整确定的排序键。
def _order(row):
    return ({'workflow': 0, 'script': 1, 'skill': 2, 'monitor': 3}.get(row['kind'], 4),
            {'workspace': 0, 'owner': 1, 'learned': 2, 'shared': 3, 'global': 3, 'builtin': 4}.get(row.get('source_scope'), 5),
            row['name'].casefold(), row['metadata'].get('stable_id') or f"{row['kind']}:{row.get('source_scope', 'owner')}:{row['id']}")


# LLM: 当前已知枚举取自提取器对应合同，未知提示不授予权限也不改变匹配状态。
# 函数用途: 丢弃损坏触发器并预编译准确别名边界。
def _matchers(value):
    if not isinstance(value, dict):
        return {key: [] for key in ('indicator_types', 'monitor_ids', 'intent_keywords', 'alert_sources')}
    kinds = {'ipv4', 'ipv6', 'domain', 'url', 'email', 'md5', 'sha1', 'sha256', 'cve_id', 'hostname',
             'port', 'process_name', 'file_path', 'alert_id', 'monitor_alias', 'image_attachment'}
    return {key: [item.casefold() for item in _strings(value.get(key), limit=8 if key == 'alert_sources' else 16,
                                                   max_bytes=192 if key == 'alert_sources' else 96)
                  if key != 'indicator_types' or item in kinds]
            for key in ('indicator_types', 'monitor_ids', 'intent_keywords', 'alert_sources')}


# LLM: 别名逐字匹配，ASCII标识边界避免job48误命中job480；中文相邻不妨碍准确别名。
# 函数用途: 编译只供软推荐使用的准确名字模式。
def _alias_pattern(value):
    return re.compile(r'(?<![a-z0-9_.-])' + re.escape(value.casefold()) + r'(?![a-z0-9_.-])')


# LLM: 只解析已限长JSON的明确source键，不读取事件正文做权限或来源裁决。
# 函数用途: 为alert_sources评分取得本轮显式事件来源提示。
def _event_sources(text):
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return set()
    if not isinstance(value, dict):
        return set()
    return {item.casefold() for key in ('source', 'alert_source', 'event_source')
            if isinstance((item := value.get(key)), str)}


# LLM: 拓扑只是描述符的纯投影，循环以seen截断，隐藏composition仅转向准确base_workflow_id。
# 函数用途: 建立可见子依赖及每个目标继承的监控别名。
def _topology_fields(rows):
    graph = build_asset_topology([AssetDescriptor(row['id'], row['kind'], row['name'], refs=row['refs']).to_dict() for row in rows])
    children = {f"{row['kind']}:{row['id']}": [] for row in rows}
    by_key = {f"{row['kind']}:{row['id']}": row for row in rows}
    for edge in graph['edges']:
        children.setdefault(edge['from'], []).append(edge['to'])
    aliases = {key: [] for key in by_key}
    for row in rows:
        if row['kind'] != 'monitor':
            continue
        alias = _short(row['metadata'].get('alias') or row['id'], max_bytes=96)
        pending, seen = list(children.get(f"monitor:{row['id']}", ())), set()
        while pending:
            key = pending.pop()
            if key in seen:
                continue
            seen.add(key)
            target = by_key.get(key)
            if target is None:
                continue
            if target['metadata'].get('hidden') is True:
                composition = target['metadata'].get('composition')
                base = composition.get('base_workflow_id') if isinstance(composition, dict) else None
                if isinstance(base, str):
                    pending.append(f'workflow:{base}')
                continue
            aliases[key].append((alias, row['id']))
            pending.extend(children.get(key, ()))
    return children, aliases


# LLM: 缓存对象只含纯元数据和编译模式；匹配返回副本，不把调用方修改带回其他回合或Owner。
# 类用途: 承载库存级索引，匹配阶段纯内存、零I/O。
class RecommendationIndex:
    # LLM: 构造时只跑一次拓扑，候选包含折叠子项；没有可调用脚本或流程时不推荐纯Skill。
    # 函数用途: 编译软触发条件和稳定排序键。
    def __init__(self, digest, rows):
        self.digest = digest
        children, aliases = _topology_fields(rows)
        self._rows = []
        if not any(row['kind'] in {'script', 'workflow'} and row['metadata'].get('hidden') is not True for row in rows):
            return
        for row in rows:
            if row['kind'] == 'monitor' or row['metadata'].get('hidden') is True:
                continue
            key = f"{row['kind']}:{row['id']}"
            matchers = _matchers(row['metadata'].get('trigger_matchers'))
            tokens = tuple((token, _alias_pattern(token)) for token in sorted(set(
                re.findall(r'[\w]+', f"{row['id']} {row['name']}".casefold()))))
            matchers['monitor_patterns'] = tuple((value, _alias_pattern(value)) for value in matchers['monitor_ids'])
            self._rows.append((row, matchers, tokens, tuple((alias, identifier, _alias_pattern(alias))
                for alias, identifier in sorted(set(aliases[key]))), tuple(children.get(key, ()))))

    # LLM: 提取与匹配只处理给定文本，扫描限64KiB；评分不读取侧文件、不调用模型、不改变授权。
    # 函数用途: 按合同权重返回最多5个命中的固定方法副本。
    def match(self, text, *, attachments=(), ocr_texts=()):
        prompt = text[:65536].encode('utf-8')[:65536].decode('utf-8', errors='ignore') if isinstance(text, str) else ''
        folded = prompt.casefold()
        kinds = {row['type'] for row in extract_indicators(prompt, attachments=attachments, ocr_texts=ocr_texts)}
        sources = _event_sources(prompt)
        matches = []
        for row, matchers, tokens, aliases, children in self._rows:
            reasons, score = [], 0
            for alias, identifier, pattern in aliases:
                if pattern.search(folded):
                    reasons.append(f'monitor_alias:{alias}')
                    score += 100
            for identifier, pattern in matchers['monitor_patterns']:
                if pattern.search(folded) and not any(reason.startswith('monitor_alias:') for reason in reasons):
                    reasons.append(f'monitor_alias:{identifier}')
                    score += 100
            for source in matchers['alert_sources']:
                if source in sources:
                    reasons.append(f'alert_source:{source}')
                    score += 40
            for kind in matchers['indicator_types']:
                if kind in kinds:
                    reasons.append(f'indicator:{kind}')
                    score += 30
            for word in matchers['intent_keywords']:
                if word in folded:
                    reasons.append(f'keyword:{word}')
                    score += 20
            for token, pattern in tokens:
                if token and pattern.search(folded):
                    reasons.append(f'name:{token}')
                    score += 10
            if score:
                result = {'kind': row['kind'], 'id': row['id'], 'name': _short(row['name'], max_bytes=96),
                    'score': score, 'matched_by': list(dict.fromkeys(reasons)), 'io_contract': deepcopy(row['io_contract']),
                    'bound_methods': list(children)[:5], 'monitor_aliases': sorted({alias for alias, _, _ in aliases})}
                matches.append((score, _order(row), result))
        return [row for _, _, row in sorted(matches, key=lambda item: (-item[0], item[1]))[:5]]


# LLM: 同摘要还以规范投影作碰撞/Owner隔离键，不缓存侧文件、运行摘要或正文；缓存容量固定。
# 函数用途: 复用库存不变时的已编译索引。
@lru_cache(maxsize=8)
def _cached_index(digest, payload):
    return RecommendationIndex(digest, json.loads(payload))


# LLM: 摘要来自排序的kind/id/revision及监控ID；完整投影排除运行状态，重排输入不会改变索引。
# 函数用途: 从显式库存取得有界缓存索引。
def build_recommendation_index(descriptors, *, monitors=()):
    rows = _inventory_rows([*(descriptors if isinstance(descriptors, (list, tuple)) else ()),
                            *(monitors if isinstance(monitors, (list, tuple)) else ())])
    signature = {'assets': sorted((row['kind'], row['id'], row['revision']) for row in rows),
                 'monitors': sorted(row['id'] for row in rows if row['kind'] == 'monitor')}
    digest = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return _cached_index(digest, payload)


# LLM: 只读已认证owner根内固定相对路径，nofollow拒绝链接，坏JSON及缺文件按缺失处理。
# 函数用途: 有界取得推荐辅助侧文件，不创建目录或锁。
def _sidecar(owner_home, parts):
    if owner_home is None:
        return {}
    try:
        raw = read_bytes_beneath(owner_home, parts, max_bytes=65536)
        value = json.loads(raw) if raw is not None else {}
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError, TypeError):
        return {}


# LLM: 精确kind/id挂接，仅取最新两条；文本不参与任何机器权限、恢复或来源判断。
# 函数用途: 给一个已命中的方法附加已知坑。
def _pitfalls(owner_home, row):
    value = _sidecar(owner_home, ('data', 'asset_evolution', 'pitfalls', row['kind'], row['id'] + '.json'))
    if value.get('kind', row['kind']) != row['kind'] or value.get('asset_id', row['id']) != row['id']:
        return []
    entries = value.get('entries')
    if not isinstance(entries, list):
        return []
    return [text[:120] for item in reversed(entries[-20:]) if isinstance(item, dict)
            and (text := _short(item.get('text'), max_bytes=360))][:2]


# LLM: 便签账本限定当前owner；绝对、穿越及链接逃逸目标均不展示，不打开附件正文。
# 函数用途: 返回最近最多五份合法入站资料的短便签。
def recent_inbound_notes(owner_home):
    value = _sidecar(owner_home, ('media', 'recent_inbound_notes.json'))
    entries = value.get('entries')
    if not isinstance(entries, list) or owner_home is None:
        return []
    home, result = Path(owner_home).resolve(), []
    for item in reversed(entries[-30:]):
        if not isinstance(item, dict):
            continue
        relative = item.get('relative_path')
        if not isinstance(relative, str) or not relative or '\\' in relative:
            continue
        path = PurePosixPath(relative)
        if path.is_absolute() or '..' in path.parts or re.match(r'^[a-z]:', relative, re.IGNORECASE):
            continue
        try:
            if not (home / relative).resolve().is_relative_to(home):
                continue
        except (OSError, ValueError, RuntimeError):
            continue
        result.append({'file_name': _short(item.get('file_name'), max_bytes=96),
                       'relative_path': _short(relative, max_bytes=192),
                       'text_excerpt': _short(item.get('text_excerpt'), max_bytes=120)})
        if len(result) >= 5:
            break
    return result


# LLM: 输入输出仅展示字段名，不带默认值、凭据或schema正文；保持原字段顺序。
# 函数用途: 从不同合法合同字段形状中选取最多八个名字。
def _io_names(value):
    if not isinstance(value, (list, tuple, dict)):
        return []
    return _strings([item.get('name') if isinstance(item, dict) else item for item in value], limit=8, max_bytes=192)


# LLM: 一段文本总以同一固定指令开头，JSON项保持完整；无内容时不产生空标题。
# 函数用途: 拼接固定方法、在用名单和入站便签。
def _section(items, using, notes):
    if not items and not using and not notes:
        return ''
    lines = ['## 本轮推荐的固定方法', '优先调用匹配的现有固定方法，不要重写等价代码；推荐不增加授权，调用仍须遵守当前工具权限。']
    for row in items:
        lines.append('- ' + json.dumps(row, ensure_ascii=False, separators=(',', ':')))
        if row['monitor_aliases']:
            lines.append('  ' + ' · '.join(f'⏰ {alias}' for alias in row['monitor_aliases']))
    if using:
        lines.append('在用: ' + ', '.join(using))
    if notes:
        lines.append('最近收到的文件:')
        lines.extend('  - ' + json.dumps(note, ensure_ascii=False, separators=(',', ':')) for note in notes)
    return '\n'.join(lines)


# LLM: 预算先减坑、再减绑定、再减最低排名项；可选名单最后裁剪，绝不输出半条JSON。
# 函数用途: 渲染仅进入动态尾段的确定性软推荐。
def render_turn_recommendations(text, descriptors, *, owner_home=None, monitors=(), in_use_method_ids=(), attachments=(), ocr_texts=()):
    ranked = build_recommendation_index(descriptors, monitors=monitors).match(text, attachments=attachments, ocr_texts=ocr_texts)
    items = [{'kind': row['kind'], 'id': row['id'], 'name': row['name'],
              'matched_by': _strings(row['matched_by'], limit=8),
              'inputs': _io_names(row['io_contract'].get('inputs')),
              'outputs': _io_names(row['io_contract'].get('outputs')),
              'bound_methods': row['bound_methods'], 'known_pitfalls': _pitfalls(owner_home, row),
              'monitor_aliases': _strings(row['monitor_aliases'], limit=8)} for row in ranked]
    using = _strings(in_use_method_ids, limit=8, max_bytes=512)
    notes = recent_inbound_notes(owner_home)
    rendered = _section(items, using, notes)
    for field in ('known_pitfalls', 'bound_methods'):
        if len(rendered.encode('utf-8')) <= 4 * TURN_RECOMMENDATION_MAX_TOKENS:
            break
        for row in items:
            row[field] = []
        rendered = _section(items, using, notes)
    while len(rendered.encode('utf-8')) > 4 * TURN_RECOMMENDATION_MAX_TOKENS:
        if items:
            items.pop()
        elif notes:
            notes.pop()
        elif using:
            using.pop()
        else:
            return ''
        rendered = _section(items, using, notes)
    return rendered


# LLM: 快照和allowed_tools只减不增；展示资格不替代原执行授权，未知运行对象不给挂载资格。
# 函数用途: 取得本轮可展示的资产工具名称。
def authorized_asset_tools(snapshot, allowed_tools=None):
    result = []
    runtime_for = getattr(snapshot, 'runtime', None)
    for name in ('scripts', 'workflows'):
        if allowed_tools is not None and name not in allowed_tools:
            continue
        if getattr(snapshot, 'allowed_tools', None) is not None and name not in snapshot.allowed_tools:
            continue
        runtime = runtime_for(name) if callable(runtime_for) else None
        if runtime is not None and runtime.availability.available and runtime.exposure.model_visible:
            result.append(name)
    return tuple(result)


# LLM: 根只来自宿主home_paths，目录仅读描述符；Skill只消费当前快照名卡，不预读正文或补扫其他Owner。
# 函数用途: 为推荐及首轮挂载共用一份现有资产元数据发现入口。
def inventory_for_agent(agent, *, runtime_snapshot, allowed_tools=None):
    from ..assets.monitor_adapter import list_monitor_descriptors
    from ..assets.workspace_assets import WorkspaceAssetOverlay

    home = getattr(getattr(agent, 'home_paths', None), 'owner_home_dir', None)
    names = authorized_asset_tools(runtime_snapshot, allowed_tools)
    if home is None or not names:
        return [], [], home
    overlay = WorkspaceAssetOverlay(getattr(getattr(agent, 'tools', None), 'workspace_root', None), owner_home=home)
    rows = []
    try:
        for name in names:
            rows.extend(overlay.list_assets('script' if name == 'scripts' else 'workflow'))
        monitors = list_monitor_descriptors(home)
    except (OSError, ValueError, TypeError):
        return [], [], home
    if not any(row.get('enabled') is True and isinstance(row.get('metadata'), dict)
               and row['metadata'].get('hidden') is not True for row in rows):
        return [], monitors, home
    snapshot_factory = getattr(agent, 'current_skill_snapshot', None)
    if callable(snapshot_factory) and (allowed_tools is None or 'skill_search' in allowed_tools):
        snapshot = snapshot_factory()
        for entry in snapshot.enabled_entries():
            rows.append(AssetDescriptor(entry.stable_id.rsplit(':', 1)[-1] if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', entry.stable_id.rsplit(':', 1)[-1]) else
                'skill-' + hashlib.sha256(entry.stable_id.encode()).hexdigest()[:24], 'skill', entry.name,
                summary=entry.description, metadata={'stable_id': entry.stable_id},
                source_scope='owner' if entry.source in {'owner', 'learned'} else 'builtin' if entry.source == 'builtin' else 'workspace' if entry.source == 'workspace' else 'global').to_dict())
    return rows, monitors, home
