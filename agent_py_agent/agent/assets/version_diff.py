"""按不可变历史快照生成有界差异与确定性摘要；不执行资产。"""
from __future__ import annotations

import difflib
import json

from . import revision_history as history


# LLM: 只比较结构化键；缺失以None展示，不改输入。
# 函数用途: 列出确定性字段变化。
def field_changes(previous, current):
    return {key: {'before': previous.get(key), 'after': current.get(key)}
            for key in sorted(previous.keys() | current.keys()) if previous.get(key) != current.get(key)}


# LLM: 发布计数、运行摘要和私有摘要凭据不属于定义变化。
# 函数用途: 提取可比较的描述符字段。
def _descriptor(payload):
    value = {key: item for key, item in payload.get('descriptor', payload).items()
             if key not in history.VOLATILE_DESCRIPTOR_KEYS and key not in ('steps', 'body_markdown')}
    if isinstance(value.get('metadata'), dict):
        value['metadata'] = {key: item for key, item in value['metadata'].items()
                             if key not in ('_run_summary_spool', '_run_summary_generation')}
    return value


# LLM: 文本仅展示，去掉换行控制字符并限制长度。
# 函数用途: 规范一行版本摘要。
def single_line(text):
    return history._single_line(text, limit=120)


# LLM: 行数统计来自完整opcodes，展示diff单独截断，不把部分展示当全量统计。
# 函数用途: 对比脚本字段和代码，返回最多四百行统一diff。
def script_diff(previous, current, *, previous_code=b'', current_code=b'', max_lines=400):
    old = previous_code.decode('utf-8') if isinstance(previous_code, bytes) else previous_code
    new = current_code.decode('utf-8') if isinstance(current_code, bytes) else current_code
    before, after = old.splitlines(), new.splitlines()
    added, removed = 0, 0
    for tag, i, j, k, l in difflib.SequenceMatcher(None, before, after).get_opcodes():
        if tag in ('insert', 'replace'):
            added += l - k
        if tag in ('delete', 'replace'):
            removed += j - i
    lines, truncated = [], False
    for line in difflib.unified_diff(before, after, fromfile='before', tofile='after', lineterm=''):
        if len(lines) == max_lines:
            truncated = True
            break
        lines.append(line)
    fields = field_changes(_descriptor(previous), _descriptor(current))
    pieces = ([f'代码 +{added}/-{removed} 行'] if old != new else [])
    if fields:
        pieces.append('字段 ' + '、'.join(fields))
    return {'descriptor_changes': fields, 'code_diff': '\n'.join(lines), 'truncated': truncated,
            'summary_line': single_line('；'.join(pieces) or '内容无变化')}


# LLM: step_id是身份，不按数组下标猜同一步；输入来自已校验发布定义。
# 函数用途: 对比工作流步骤并折叠所有未改步骤。
def workflow_diff(previous, current):
    before = {step['step_id']: step for step in previous.get('steps', [])}
    after = {step['step_id']: step for step in current.get('steps', [])}
    added = [after[key] for key in sorted(after.keys() - before.keys())]
    removed = [before[key] for key in sorted(before.keys() - after.keys())]
    changed = {key: field_changes(before[key], after[key]) for key in sorted(before.keys() & after.keys())
               if before[key] != after[key]}
    unchanged = len(before.keys() & after.keys()) - len(changed)
    fields = field_changes(_descriptor(previous), _descriptor(current))
    summary = f'步骤 +{len(added)} ~{len(changed)} -{len(removed)}'
    if fields:
        summary += '；字段 ' + '、'.join(fields)
    if previous.get('body_markdown', '') != current.get('body_markdown', ''):
        summary += '；正文变化'
    return {'added': added, 'removed': removed, 'changed': changed, 'unchanged_count': unchanged,
            'folded': f'其余 {unchanged} 个步骤无变化', 'descriptor_changes': fields,
            'summary_line': single_line(summary)}


# LLM: 精确版本和blob缺失明确失败，不使用最新asset兜底。
# 函数用途: 读取一个版本的完整定义、代码及侧文件。
def load_snapshot(owner_home, kind, asset_id, version=None):
    record = (history.latest_version(owner_home, kind, asset_id) if version is None
              else history.get_version(owner_home, kind, asset_id, version))
    if record is None:
        raise history.AssetHistoryError('ASSET_VERSION_NOT_FOUND')
    payload = json.loads(history._required_blob(owner_home, record['payload_blob']))
    code = history._required_blob(owner_home, record['code_sha256']) if kind == 'script' else None
    sides = {key: history._required_blob(owner_home, sha).decode('utf-8') for key, sha in record['side_blobs'].items()}
    return record, payload, code, sides


# LLM: 两端都来自W1历史，工具不重建另一套历史读取协议。
# 函数用途: 按种类生成指定版本间的差异。
def diff_versions(owner_home, kind, asset_id, from_version, to_version=None):
    _, previous, code_before, _ = load_snapshot(owner_home, kind, asset_id, from_version)
    _, current, code_after, _ = load_snapshot(owner_home, kind, asset_id, to_version)
    return (script_diff(previous, current, previous_code=code_before, current_code=code_after)
            if kind == 'script' else workflow_diff(previous, current))
