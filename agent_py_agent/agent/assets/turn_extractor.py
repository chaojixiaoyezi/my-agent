# LLM: 使用原会话、nofollow与资产合同；历史正文是数据，不授执行/覆盖权，提案写入与正式发布分离。
# 模块用途: 按用户回合检索执行证据、生成可编辑草稿并确认发布；不调用模型或执行提取代码。
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from ..common.directory_lock import locked_private_directory
from ..common.json_io import locked_json_path
from ..common.log_redaction import redact_sensitive_value
from ..common.nofollow_fs import (
    ensure_private_dir,
    open_directory_beneath,
    read_bytes_beneath,
    unlink_file_beneath,
    write_text_atomic_beneath,
)
from ..conversation.native_history import canonical_native_messages_from_metadata
from ..observability.telemetry_ledger import ToolTelemetryLedger
from . import MAX_ASSET_BYTES, asset_root_lock, safe_asset_id
from .descriptor import AccessTarget, AssetDescriptor, IOContract
from .script_store import ScriptStore
from .workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
    with_workflow_step_refs,
)

_PROPOSALS = 'asset_extractions'
_CANDIDATES = {'skill': 'candidate_skills', 'script': 'candidate_scripts', 'workflow': 'candidate_workflows'}
_CLI_VALUE = r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s'";|&<>]+)'''
_CLI_SECRET = re.compile(r'(?i)(?<![\w-])(--(?:password|passwd|token|secret|api[-_]?key|access[-_]?key|auth[-_]?token)\s+)(' + _CLI_VALUE + ')')
_CLI_PASSWORD_CONTEXT = re.compile(r'(?i)(?:^|[\s/])(?:mysql|psql|sshpass|login)(?:\s|$)')
_CLI_SHORT_PASSWORD = re.compile(r'(?<![\w-])(-p\s+)(' + _CLI_VALUE + ')')


# LLM: 时间统一UTC ISO；数字来自原created_at，日期上界可扩到当日结束，不按显示时区猜归属。
# 函数用途: 将持久时间或筛选条件转换为可比较时间。
def _date(value, *, end=False):
    if type(value) in (int, float):
        return datetime.fromtimestamp(value, timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    parsed = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    return parsed + timedelta(days=1) if end and len(str(value)) == 10 else parsed


# LLM: 只比较替换前后并返回安全占位符；容器类型归一或原有占位文本不算命中，绝不收集原值。
# 函数用途: 递归识别实际发生的秘密替换。
def _redaction_markers(before, after):
    if isinstance(after, str):
        return ['<redacted>'] if before != after and '<redacted>' in after else []
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        return [marker for key in before for marker in _redaction_markers(before[key], after.get(key))]
    if isinstance(before, (list, tuple)) and isinstance(after, (list, tuple)):
        return [marker for old, new in zip(before, after, strict=True) for marker in _redaction_markers(old, new)]
    return []


# LLM: 只遮盖值并保留引号边界，不执行或重放命令；-p只有明确密码上下文时才匹配。
# 函数用途: 清理命令行中空格分隔的敏感参数。
def _redact_cli_flags(text):
    # 记录上下文要在替换前完成，避免参数值恰好包含命令名影响后续判断。
    short_password = bool(_CLI_PASSWORD_CONTEXT.search(text) or re.search(r'(?i)--password\s+', text))

    # LLM: 正则已将整个带引号的值分成独立组；原值不进入返回或占位清单。
    # 函数用途: 为一个标志生成同引号形式的安全占位符。
    def replace(match):
        value = match[2]
        quote = value[0] if value[0] in {'\"', "'"} else ''
        return match[1] + quote + '<redacted>' + quote

    text = _CLI_SECRET.sub(replace, text)
    return _CLI_SHORT_PASSWORD.sub(replace, text) if short_password else text


# LLM: 返回独立JSON副本，原脱敏口再补CLI和Bearer；可选清单仅记录实际替换后的占位符。
# 函数用途: 在分析器及草稿持久化边界清理敏感值并按需报告脱敏。
def _redact(value, *, redacted_secrets=None):
    before = value
    value = redact_sensitive_value(value)
    if isinstance(value, str):
        value = _redact_cli_flags(value)
        value = re.sub(r'(?i)(\bbearer\s+)[^\s,;\"\'<>]+', r'\1<redacted>', value)
    elif isinstance(value, dict):
        value = {key: _redact(item) for key, item in value.items()}
    elif isinstance(value, (list, tuple)):
        value = [_redact(item) for item in value]
    if redacted_secrets is not None:
        redacted_secrets.extend(_redaction_markers(before, value))
    return value


# LLM: 信任根只来自调用参数；不resolve符号链接，创建及所有后续叶IO使用nofollow原语。
# 函数用途: 准备私有数据目录并返回同一根。
def _record_lock(home, collection):
    home = Path(os.path.abspath(home))
    ensure_private_dir(home)
    ensure_private_dir(home / 'data' / collection, canonical_root=home)
    return locked_private_directory(home, relative_parts=('data', collection), lock_name='.records.lock')


# LLM: 公开ID只作单叶名，有界读取拒绝链接；不创建目录、不扫描其它Owner。
# 函数用途: 读取提案或录制JSON并核对存储身份。
def _read_record(home, collection, record_id, id_key):
    safe_asset_id(record_id)
    raw = read_bytes_beneath(home, ('data', collection, f'{record_id}.json'), max_bytes=MAX_ASSET_BYTES)
    if raw is None:
        raise ValueError('ASSET_RECORD_NOT_FOUND')
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get(id_key) != record_id or type(value.get('revision')) is not int:
        raise ValueError('ASSET_RECORD_INVALID')
    return value


# LLM: 调用方持记录集合锁；单叶原子替换，超限/非有限JSON在写前拒绝。
# 函数用途: 持久保存完整提案或录制状态。
def _write_record(home, collection, value, id_key):
    record_id = safe_asset_id(value[id_key])
    text = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + '\n'
    if len(text.encode('utf-8')) > MAX_ASSET_BYTES:
        raise ValueError('ASSET_RECORD_TOO_LARGE')
    write_text_atomic_beneath(home, ('data', collection, f'{record_id}.json'), text)


# LLM: thread_ids只在当前Store中查找；坏账上抛，不用空查询掩盖损坏或跨Owner扩查。
# 函数用途: 得到当前会话库中的线程快照。
def _threads(store, thread_ids=None):
    if thread_ids is None:
        rows, errors = store.threads.list_report(limit=0)
        if errors:
            raise ValueError('TURN_HISTORY_UNREADABLE')
        return rows
    rows = []
    for thread_id in dict.fromkeys(thread_ids):
        row, error = store.threads.load_report(thread_id)
        if error:
            raise ValueError('TURN_HISTORY_UNREADABLE')
        if row is None:
            raise ValueError('TURN_REF_NOT_FOUND: thread')
        rows.append(row)
    return rows


# LLM: 以真实user消息切分，系统/display不成为用户回合；原始日志顺序就是同线程顺序。
# 函数用途: 返回单线程零基编号的完整用户回合。
def _turns(store, thread_id):
    rows, errors = store.messages.recent_report(thread_id, limit=0)
    if errors:
        raise ValueError('TURN_HISTORY_UNREADABLE')
    turns = []
    for row in rows:
        if row.thread_id != thread_id:
            raise ValueError('TURN_HISTORY_IDENTITY_MISMATCH')
        if row.role == 'user':
            turns.append([row])
        elif turns and row.role in {'assistant', 'tool'}:
            turns[-1].append(row)
    return turns


# LLM: 只恢复结构化tool调用，字符串arguments必须是JSON对象；格式错误明确失败，不从正文猜工具。
# 函数用途: 统一原生块、旧tool_calls及遥测条目的调用字段。
def _invocation(raw):
    function = raw.get('function') or raw
    arguments = function.get('arguments', function.get('input', raw.get('args_summary', {})))
    if isinstance(arguments, str):
        arguments = json.loads(arguments)
    if not isinstance(arguments, dict):
        raise ValueError('TURN_TOOL_ARGUMENTS_INVALID')
    return {'call_id': str(raw.get('id') or raw.get('call_id') or ''),
            'tool_name': str(function.get('name') or raw.get('tool_name') or ''),
            'arguments': arguments, 'result_preview': str(raw.get('result_preview') or ''),
            'status': str(raw.get('status') or 'unknown')}


# LLM: 相同call_id是同一次调用，匿名相同正文仍是不同调用；原生信封优先，结果以tool_use_id准确配对。
# 函数用途: 聚合回合所有工具调用及结果，不重复累计展示副本。
def _tool_invocations(rows, home=None):
    calls, results, seen, ledger_ids = [], {}, set(), []
    for row in rows:
        metadata = row.metadata or {}
        native = canonical_native_messages_from_metadata(metadata)
        blocks = [block for message in native for block in message['content'] if isinstance(block, dict)]
        raw_calls = [block for block in blocks if block.get('type') == 'tool_use']
        raw_calls = raw_calls or metadata.get('tool_calls', [])
        for raw in raw_calls:
            call = _invocation(raw)
            if call['call_id'] and call['call_id'] in seen:
                continue
            seen.add(call['call_id']) if call['call_id'] else None
            calls.append(call)
        for block in blocks:
            if block.get('type') == 'tool_result':
                results[block.get('tool_use_id')] = (block.get('content', ''), 'failed' if block.get('is_error') else 'succeeded')
        if row.role == 'tool':
            results[metadata.get('tool_call_id')] = (row.content, metadata.get('status', 'unknown'))
        ledger_ids.extend(metadata.get('tool_call_ids', []))
    if home is not None:
        _merge_telemetry(home, ledger_ids, calls, seen)
    for call in calls:
        if call['call_id'] in results:
            content, status = results[call['call_id']]
            call.update(result_preview=(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False))[:4000], status=status)
    return calls


# LLM: 遥测只按当前回合显式call_id关联，禁止用时间相近或相同工具名跨线程拼接。
# 函数用途: 补齐没有原生信封的工具明细。
def _merge_telemetry(home, ids, calls, seen):
    ledger = ToolTelemetryLedger(home)
    for call_id in dict.fromkeys(ids):
        if call_id in seen:
            continue
        value = ledger.get_tool_call(call_id)
        if value is not None:
            calls.append(_invocation(value))
            seen.add(call_id)


# LLM: 新绑定字段优先于旧metadata和cwd；只返回线程已有值，不创建或推断工作目录。
# 函数用途: 为摘要与筛选统一读取线程工作目录。
def _thread_workspace_root(thread):
    return getattr(thread, 'workspace_root', '') or (thread.metadata or {}).get('workspace_root') or getattr(thread, 'cwd', '')


# LLM: 公开预览保留工具计数与原user位置及权威工作目录；正文只截预览，原文保留在独立bundle读口。
# 函数用途: 为搜索结果生成一项回合摘要。
def _turn_summary(thread, index, rows):
    calls = _tool_invocations(rows)
    return {'thread_id': thread.thread_id, 'thread_title': thread.title,
            'workspace_root': str(_thread_workspace_root(thread)), 'message_id': rows[0].message_id,
            'turn_index': index, 'created_at': _date(rows[0].created_at).isoformat(),
            'user_prompt_preview': rows[0].content[:500],
            'assistant_reply_preview': '\n'.join(row.content for row in rows[1:] if row.role == 'assistant')[:1000],
            'tool_names': list(dict.fromkeys(call['tool_name'] for call in calls)),
            'tool_call_count': len(calls), 'has_tool_calls': bool(calls)}


# LLM: 支持任务规定的query/thread_ids/workspace_dir/date_from/date_to/has_tool_calls；其余键拒绝，分页无隐式截断。
# 函数用途: 跨当前Owner多个线程检索用户回合，返回稳定倒序分页；只读不修改历史。
def search_user_turns(conversation_store, *, query: str = '', limit: int = 50, cursor: int = 0, **filters) -> dict[str, object]:
    allowed = {'thread_ids', 'workspace_dir', 'date_from', 'date_to', 'has_tool_calls'}
    if filters.keys() - allowed or type(limit) is not int or limit <= 0 or type(cursor) is not int or cursor < 0:
        raise ValueError('TURN_SEARCH_INVALID_FILTER')
    found = []
    for thread in _threads(conversation_store, filters.get('thread_ids')):
        workspace = filters.get('workspace_dir')
        if workspace and os.path.abspath(workspace) != os.path.abspath(_thread_workspace_root(thread) or '.'):
            continue
        for index, rows in enumerate(_turns(conversation_store, thread.thread_id)):
            item = _turn_summary(thread, index, rows)
            if _matches_turn(item, rows, query, filters):
                found.append(_redact(item))
    found.sort(key=lambda row: (row['created_at'], row['thread_id'], row['turn_index']), reverse=True)
    return {'items': found[cursor:cursor + limit], 'total': len(found),
            'next_cursor': cursor + limit if cursor + limit < len(found) else None}


# LLM: 查询值大小写归一，日期比较UTC-aware；date_to日期包含整天，完整ISO为闭上界。
# 函数用途: 判断回合是否满足正文、工具与日期筛选。
def _matches_turn(item, rows, query, filters):
    text = '\n'.join([item['thread_title'], *(row.content for row in rows), *item['tool_names']])
    if query.casefold() not in text.casefold():
        return False
    if filters.get('has_tool_calls') is not None and item['has_tool_calls'] != filters['has_tool_calls']:
        return False
    created = _date(item['created_at'])
    if filters.get('date_from') and created < _date(filters['date_from']):
        return False
    upper = filters.get('date_to')
    return not upper or (created < _date(upper, end=True) if len(upper) == 10 else created <= _date(upper))


# LLM: 只接受精确用户message_id或零基turn_index；双引用冲突/负数/非用户消息拒绝；不执行历史工具。
# 函数用途: 依用户选择顺序打包多个线程的完整回合和已记录命令、写文件。
def collect_turn_execution_bundles(conversation_store, turn_refs: Sequence[Mapping[str, object]], *, owner_home: Path | None = None) -> list[dict[str, object]]:
    if owner_home is not None and not conversation_store.storage.root.absolute().is_relative_to(Path(owner_home).absolute()):
        raise ValueError('TURN_OWNER_SCOPE_MISMATCH')
    bundles, cached = [], {}
    for ref in turn_refs:
        thread_id = ref.get('thread_id', '')
        if not thread_id:
            raise ValueError('TURN_REF_NOT_FOUND: thread')
        if thread_id not in cached:
            _threads(conversation_store, [thread_id])
            cached[thread_id] = _turns(conversation_store, thread_id)
        index, rows = _selected_turn(cached[thread_id], ref)
        calls = _tool_invocations(rows, owner_home)
        bundles.append({'thread_id': thread_id, 'message_id': rows[0].message_id, 'turn_index': index,
                        'created_at': _date(rows[0].created_at).isoformat(), 'user_prompt': rows[0].content,
                        'assistant_messages': [row.content for row in rows[1:] if row.role == 'assistant'],
                        'tool_invocations': calls, 'commands_executed': _commands(calls),
                        'files_written': _written_paths(calls)})
    return bundles


# LLM: 缺失索引不默认选首轮，bool不作整数索引；message_id与index同时给出时须指向同轮。
# 函数用途: 准确选取当前线程中的一个用户回合。
def _selected_turn(turns, ref):
    for index, rows in enumerate(turns):
        if 'message_id' in ref and rows[0].message_id != ref['message_id']:
            continue
        if 'turn_index' in ref and (type(ref['turn_index']) is not int or index != ref['turn_index']):
            continue
        if 'message_id' in ref or 'turn_index' in ref:
            return index, rows
    raise ValueError('TURN_REF_NOT_FOUND: user message')


# LLM: 仅结构化shell/python调用参数成为命令证据，正文中的命令不计；保留顺序与重复调用。
# 函数用途: 提取已调用工具中记录的命令。
def _commands(calls):
    return [call['arguments']['command'] for call in calls if call['tool_name'].rsplit('.', 1)[-1] in {'run_command', 'terminal_session'}
            and isinstance(call['arguments'].get('command'), str)]


# LLM: 只取写工具的明确path或结构化补丁文件头，不扫描磁盘或读取产物正文。
# 函数用途: 聚合回合写改文件的路径证据。
def _written_paths(calls):
    paths = []
    for call in calls:
        name, args = call['tool_name'].rsplit('.', 1)[-1], call['arguments']
        if name in {'write_file', 'edit_file'} and isinstance(args.get('path'), str):
            paths.append(args['path'])
        if name == 'apply_patch':
            paths.extend(re.findall(r'^\*\*\* (?:Add|Update|Delete) File: (.+)$', args.get('patch', ''), re.M))
    return list(dict.fromkeys(paths))


# LLM: scope来自显式调用，不读候选metadata决定写根；workspace必须是现存真实目录，后续IO仍nofollow。
# 函数用途: 校验发布范围并选择原Owner或项目.agent根。
def _asset_root(owner_home, scope, workspace_root):
    if scope == 'owner':
        return Path(os.path.abspath(owner_home))
    if scope != 'workspace' or workspace_root is None:
        raise ValueError('ASSET_SCOPE_INVALID')
    project = Path(os.path.abspath(workspace_root))
    os.close(open_directory_beneath(project, ()))
    return project / '.agent'


# LLM: 生成数据接口而非另一套ScriptStore；descriptor是原资产合同，schema额外保留为编译/预览元数据。
# 函数用途: 将可执行Python与参数表包装为任务要求的脚本草稿。
def _script_spec(script_id, title, source, input_schema, output_schema, network_targets=()):
    safe_asset_id(script_id)
    tree = ast.parse(source)
    if not any(isinstance(node, ast.FunctionDef) and node.name == 'main' for node in tree.body):
        raise ValueError('SCRIPT_MAIN_REQUIRED')
    source = _with_script_entrypoint(source, tree)
    descriptor = AssetDescriptor(id=script_id, kind='script', name=title,
                                 summary='从用户选择的执行记录提炼；执行须经原脚本运行权限门',
                                 access_targets=tuple(AccessTarget(f'network_{i}', 'http', target)
                                                      for i, target in enumerate(network_targets, 1)),
                                 io_contract=IOContract(inputs=tuple(input_schema.get('properties', {})),
                                                        outputs=tuple(output_schema.get('properties', {}))))
    return {'script_id': script_id, 'title': title, 'description': descriptor.summary, 'language': 'python',
            'entry_source': source, 'input_schema': input_schema, 'output_schema': output_schema,
            'access_contract': {'network_targets': list(network_targets)}, 'descriptor': descriptor.to_dict()}


# LLM: 原ScriptRunner执行文件并提供stdin，故只有main定义不够；已有标准main guard不重复追加。
# 函数用途: 为函数式脚本补上正式stdin/stdout执行入口，并再验证完整AST。
def _with_script_entrypoint(source, tree):
    has_guard = any(isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                    and isinstance(node.test.left, ast.Name) and node.test.left.id == '__name__'
                    and any(isinstance(item, ast.Constant) and item.value == '__main__' for item in node.test.comparators)
                    for node in tree.body)
    if not has_guard:
        source += '''
if __name__ == "__main__":
    import json
    import sys
    _script_inputs = json.load(sys.stdin)
    if not isinstance(_script_inputs, dict):
        raise ValueError("SCRIPT_INPUT_MUST_BE_OBJECT")
    print(json.dumps(main(_script_inputs), ensure_ascii=False, allow_nan=False))
'''
    ast.parse(source)
    return source


# LLM: 步骤绑定使用原WorkflowStepSpec与WorkflowDefinition；脚本先行、Skill分析最后，不在此执行。
# 函数用途: 为提取或浏览器编译构建可直接保存的工作流。
def _workflow_spec(workflow_id, title, scripts, skills=()):
    steps = [WorkflowStepSpec(step_id=f'step_{index}', name=script['title'], mode='deterministic_script',
                              script_id=script['script_id'], inputs_map={name: {'step_id': '$inputs', 'key': name}
                              for name in script['input_schema'].get('properties', {})})
             for index, script in enumerate(scripts, 1)]
    if skills:
        previous = steps[-1].step_id if steps else '$inputs'
        steps.append(WorkflowStepSpec(step_id='analysis', name='分析提取结果', mode='llm_skill_analysis',
                                       skill_ids=tuple(skill['name'] for skill in skills),
                                       inputs_map={'result': {'step_id': previous}}))
    return with_workflow_step_refs(WorkflowDefinition(
        AssetDescriptor(id=workflow_id, kind='workflow', name=title), tuple(steps), '执行前核对参数、访问声明及授权。')).to_dict()


# LLM: frontmatter字符串通过JSON转义避免标题注入YAML；正文仅说明步骤，不授执行或发布权。
# 函数用途: 生成符合Skill字段与章节要求的中文说明。
def _skill_markdown(name, title, procedure):
    safe_asset_id(name)
    header = '\n'.join(f'{key}: {json.dumps(value, ensure_ascii=False)}' for key, value in {
        'name': name, 'description': title, 'when_to_use': '重用已确认的相同流程时', 'tags': ['提取', '自动化']}.items())
    return f'---\n{header}\n---\n\n## 适用场景\n{title}\n\n## 输入参数\n按脚本输入合同提供参数；秘密由运行时安全注入。\n\n## 标准执行步骤\n{procedure}\n\n## 验收条件\n核对真实返回、输出文件和访问范围；干跑不代表执行成功。\n'


# LLM: 离线结构合成不重放历史shell；argv及dry_run的None不覆盖安全默认，执行仍须原ScriptRunner授权。
# 函数用途: 将每个回合真实命令记录变成默认干跑的可复用脚本草稿。
def _bundle_script(bundle, script_id, title):
    commands = bundle['commands_executed']
    source = f'''# LLM: 常量只保存脱敏历史证据，只有显式argv输入才执行；不使用shell=True。
# 模块用途: 预览历史命令，或在原宿主授权下执行调用方确认的参数化命令。
import subprocess
COMMAND_EVIDENCE = {commands!r}

# LLM: 默认或dry_run=None只输出计划，执行argv须调用方提供，返回值不伪造历史执行。
# 函数用途: 干跑预览或运行一个显式参数化命令。
def main(inputs: dict) -> dict:
    argv = inputs.get("argv")
    if argv is None:
        argv = []
    if not isinstance(argv, list) or any(not isinstance(item, str) for item in argv):
        raise ValueError("argv必须是字符串数组")
    dry_run = inputs.get("dry_run", True)
    if dry_run is None:
        dry_run = True
    if dry_run:
        return {{"ok": True, "commands": COMMAND_EVIDENCE, "argv": argv, "stdout": "", "executed": False}}
    if not argv:
        raise ValueError("执行前必须填写已确认的argv")
    result = subprocess.run(argv, check=True, capture_output=True, text=True, timeout=inputs.get("timeout", 60))
    return {{"ok": True, "commands": COMMAND_EVIDENCE, "argv": argv, "stdout": result.stdout, "executed": True}}
'''
    return _script_spec(script_id, title, source,
                        {'type': 'object', 'properties': {'argv': {'type': 'array', 'items': {'type': 'string'}},
                                                         'dry_run': {'type': 'boolean', 'default': True},
                                                         'timeout': {'type': 'number', 'default': 60}}},
                        {'type': 'object', 'properties': {'ok': {'type': 'boolean'}, 'stdout': {'type': 'string'},
                                                         'executed': {'type': 'boolean'}}})


# LLM: target_types是有限资产种类，workflow自动包含脚本依赖，all只展开当前三类，不猜监控语义。
# 函数用途: 校验并展开用户选择的提炼类型。
def _target_types(values):
    if isinstance(values, str) or not values or set(values) - {*_CANDIDATES, 'all'}:
        raise ValueError('EXTRACTION_TARGET_TYPES_INVALID')
    kinds = tuple(_CANDIDATES) if 'all' in values else tuple(dict.fromkeys(values))
    return (*kinds, 'script') if 'workflow' in kinds and 'script' not in kinds else kinds


# LLM: asset_types兼容别名不能与target_types冲突；预览不取锁/落盘，workflow连带依赖且拒绝空候选。
# 函数用途: 从选中回合生成脱敏草稿，默认保存DRAFT，显式预览仅返回内存结果。
def propose_assets_from_turns(conversation_store, *, owner_home: Path, turn_refs: Sequence[Mapping[str, object]],
                              target_types: Sequence[str] = ('all',), **options) -> dict[str, object]:
    if options.keys() - {'scope', 'workspace_root', 'title_hint', 'analyzer', 'preview_only', 'asset_types'} or type(options.get('preview_only', False)) is not bool:
        raise ValueError('EXTRACTION_OPTIONS_INVALID')
    kinds = _target_types(target_types)
    if 'asset_types' in options:
        alias = _target_types(options['asset_types'])
        if tuple(target_types) != ('all',) and set(kinds) != set(alias):
            raise ValueError('EXTRACTION_TARGET_TYPES_INVALID')
        kinds = alias
    scope, project = options.get('scope', 'owner'), options.get('workspace_root')
    _asset_root(owner_home, scope, project)
    redacted_secrets = []
    bundles = _redact(collect_turn_execution_bundles(conversation_store, turn_refs, owner_home=owner_home), redacted_secrets=redacted_secrets)
    if not bundles:
        raise ValueError('EXTRACTION_EMPTY_SELECTION')
    proposal_id = 'exp_' + uuid4().hex[:16]
    title = _redact(str(options.get('title_hint') or bundles[0]['user_prompt'][:100] or '回合提炼'), redacted_secrets=redacted_secrets)
    analyzer: Callable | None = options.get('analyzer')
    candidates = analyzer(bundles, kinds) if analyzer is not None else _synthesize(bundles, kinds, proposal_id, title)
    candidates = _redact(candidates, redacted_secrets=redacted_secrets)
    proposal = {key: candidates.get(key, []) if kind in kinds else [] for kind, key in _CANDIDATES.items()}
    if 'workflow' in kinds and 'skill' not in kinds and not proposal['candidate_workflows']:
        raise ValueError('TURN_EXTRACTION_EMPTY_CANDIDATES')
    _validate_candidates(proposal)
    proposal.update(proposal_id=proposal_id, status='PREVIEW' if options.get('preview_only') is True else 'DRAFT', revision=1, scope=scope,
                    redacted_secrets=sorted(set(redacted_secrets)),
                    workspace_root=str(Path(project).absolute()) if project is not None else '',
                    source_turn_refs=[{'thread_id': b['thread_id'], 'message_id': b['message_id']} for b in bundles],
                    created_at=datetime.now(timezone.utc).isoformat(),
                    bundle_summary=candidates.get('bundle_summary', {'turn_count': len(bundles),
                                   'tool_call_count': sum(len(b['tool_invocations']) for b in bundles)}))
    if options.get('preview_only') is True:
        return proposal
    with _record_lock(owner_home, _PROPOSALS):
        _write_record(owner_home, _PROPOSALS, proposal, 'proposal_id')
    return proposal


# LLM: 纯workflow请求仅以有真实可执行命令证据的回合生成伴随脚本；纯文字或仅非命令工具可单独提Skill，不编造空命令流水线。
# 函数用途: 确定性合成三类资产的最小结构。
def _synthesize(bundles, kinds, proposal_id, title):
    prefix = proposal_id.replace('exp_', 'extracted_')
    script_bundles = bundles
    if 'workflow' in kinds and 'skill' not in kinds:
        script_bundles = [bundle for bundle in bundles if bundle['commands_executed']]
        if not script_bundles:
            raise ValueError('TURN_EXTRACTION_EMPTY_CANDIDATES')
    scripts = [_bundle_script(bundle, f'{prefix}_{i}', f'{title} · {i}') for i, bundle in enumerate(script_bundles, 1)] if 'script' in kinds else []
    procedure = '\n'.join(f'{i}. {b["user_prompt"]}\n   检查工具证据：' + ', '.join(c['tool_name'] for c in b['tool_invocations'])
                          for i, b in enumerate(bundles, 1))
    skills = [{'name': prefix, 'title': title, 'body': _skill_markdown(prefix, title, procedure)}] if 'skill' in kinds else []
    workflows = [_workflow_spec(prefix, title, scripts, skills)] if 'workflow' in kinds else []
    return dict(candidate_scripts=scripts, candidate_skills=skills, candidate_workflows=workflows)


# LLM: 校验候选完整性后才创建草稿或正式叶；ID单段、Python AST与原workflow合同都是硬门。
# 函数用途: 拒绝分析器或编辑选项生成的非法资产。
def _validate_candidates(proposal):
    for kind, key in _CANDIDATES.items():
        if not isinstance(proposal[key], list):
            raise ValueError('EXTRACTION_CANDIDATES_INVALID')
        identities = []
        for item in proposal[key]:
            identities.append(_validate_asset(kind, item))
        if len(set(identities)) != len(identities):
            raise ValueError('EXTRACTION_DUPLICATE_ASSET')


# LLM: 固定Python/Skill沿原合同校验；workflow须有步骤，旧空定义同样不得发布或当有效草稿返回。
# 函数用途: 校验单个可发布候选并返回安全ID。
def _validate_asset(kind, item):
    if kind == 'workflow':
        definition = WorkflowDefinition.from_any(item)
        if not definition.steps:
            raise ValueError('TURN_EXTRACTION_EMPTY_CANDIDATES')
        return definition.descriptor.id
    if kind == 'script':
        if item.get('language') != 'python':
            raise ValueError('EXTRACTION_SCRIPT_LANGUAGE_INVALID')
        _script_spec(item['script_id'], item['title'], item['entry_source'], item['input_schema'], item['output_schema'])
        return safe_asset_id(item['script_id'])
    name, body = safe_asset_id(item['name']), item['body']
    if not body.startswith('---\n') or not all(key + ':' in body for key in ('name', 'description', 'when_to_use', 'tags')):
        raise ValueError('EXTRACTION_SKILL_FRONTMATTER_INVALID')
    if not all('## ' + heading in body for heading in ('适用场景', '输入参数', '标准执行步骤', '验收条件')):
        raise ValueError('EXTRACTION_SKILL_BODY_INVALID')
    return name


# LLM: 公开读取不修复、不发布；验证状态与候选形状，不以磁盘metadata授权其它Owner。
# 函数用途: 读取持久提炼草稿及提交结果。
def get_extraction_proposal(owner_home: Path, proposal_id: str) -> dict[str, object]:
    proposal = _read_record(owner_home, _PROPOSALS, proposal_id, 'proposal_id')
    if proposal.get('status') not in {'DRAFT', 'COMMITTED'} or proposal['revision'] < 1:
        raise ValueError('EXTRACTION_PROPOSAL_INVALID')
    _validate_candidates(proposal)
    return proposal


# LLM: 选择必须匹配原候选；编辑仅改资产字段，不允许恢复scope或路径权限。
# 函数用途: 应用用户勾选和编辑，记录依赖重命名映射。
def _select_assets(proposal, selected):
    available = {(kind, _validate_asset(kind, item)): item
                 for kind, key in _CANDIDATES.items() for item in proposal[key]}
    requests = ([{'kind': kind, 'id': asset_id} for kind, asset_id in available] if selected is None else selected)
    chosen, remap, seen = [], {}, set()
    for request in requests:
        kind, source = request.get('kind', request.get('type')), request.get('source_id', request.get('id'))
        if (kind, source) not in available or (kind, source) in seen:
            raise ValueError('EXTRACTION_SELECTION_INVALID')
        seen.add((kind, source))
        item = json.loads(json.dumps(available[kind, source]))
        edits = dict(request.get('overrides', request.get('edits', {})))
        edits.update({key: request[key] for key in ('title', 'body', 'entry_source', 'script_id', 'workflow_id', 'name') if key in request})
        new_id = request.get('new_id', edits.pop('id', source))
        item = _redact(_edit_candidate(kind, item, edits, new_id))
        remap[kind, source] = _validate_asset(kind, item)
        chosen.append((kind, item))
    if not chosen:
        raise ValueError('EXTRACTION_EMPTY_SELECTION')
    identities = [(kind, _validate_asset(kind, item)) for kind, item in chosen]
    if len(set(identities)) != len(identities):
        raise ValueError('EXTRACTION_DUPLICATE_ASSET')
    return _remap_workflows(chosen, remap)


# LLM: ID变化同步Skill frontmatter和workflow描述符，编辑结果仍经原值合同验证。
# 函数用途: 在内存中更新单个选中候选。
def _edit_candidate(kind, item, edits, new_id):
    if kind == 'workflow':
        descriptor = {**item['descriptor'], 'id': safe_asset_id(edits.pop('workflow_id', new_id))}
        descriptor['name'] = edits.pop('title', descriptor['name'])
        if 'body' in edits:
            edits['body_markdown'] = edits.pop('body')
        return {**item, **edits, 'descriptor': descriptor}
    if kind == 'script':
        if 'body' in edits:
            edits['entry_source'] = edits.pop('body')
        return {**item, **edits, 'script_id': safe_asset_id(edits.get('script_id', new_id))}
    name = safe_asset_id(edits.get('name', new_id))
    item.update(edits, name=name)
    item['body'] = re.sub(r'^name:.*$', 'name: ' + json.dumps(name), item['body'], count=1, flags=re.M)
    return item


# LLM: 依赖只按显式旧新ID映射重写，不按名称相似度猜；steps仍走原WorkflowDefinition验证。
# 函数用途: 同步选中资产重命名后的工作流引用。
def _remap_workflows(chosen, remap):
    for kind, item in chosen:
        if kind != 'workflow':
            continue
        for step in item['steps']:
            step['script_id'] = remap.get(('script', step.get('script_id')), step.get('script_id', ''))
            step['skill_ids'] = [remap.get(('skill', value), value) for value in step.get('skill_ids', [])]
        item.update(with_workflow_step_refs(WorkflowDefinition.from_any(item)).to_dict())
    return chosen


# LLM: 新资产正式布局与原ScriptStore/WorkflowStore一致，不创造第二份注册表；所有内容在写前验证。
# 函数用途: 为整批资产准备全部目标文件。
def _asset_files(chosen):
    files, assets = {}, []
    for kind, item in sorted(chosen, key=lambda pair: pair[0] == 'workflow'):
        asset_id = _validate_asset(kind, item)
        assets.append({'kind': kind, 'id': asset_id})
        if kind == 'script':
            spec = _script_spec(asset_id, item['title'], item['entry_source'], item['input_schema'],
                                item['output_schema'], item.get('access_contract', {}).get('network_targets', []))
            payload = {**spec['descriptor'], 'entrypoint': 'main.py', 'interpreter': 'python3', 'timeout_seconds': 120}
            files['scripts', asset_id, 'script.json'] = json.dumps(payload, ensure_ascii=False)
            files['scripts', asset_id, 'main.py'] = spec['entry_source']
        elif kind == 'workflow':
            definition = with_workflow_step_refs(WorkflowDefinition.from_any(item))
            payload = {**definition.descriptor.to_dict(), 'steps': [step.to_dict() for step in definition.steps], 'body_markdown': definition.body_markdown}
            files['workflows', asset_id, 'workflow.json'] = json.dumps(payload, ensure_ascii=False)
            if definition.body_markdown:
                files['workflows', asset_id, 'WORKFLOW.md'] = definition.body_markdown
        else:
            files['skills', 'learned', asset_id, 'SKILL.md'] = item['body']
    return files, assets


# LLM: workspace Skill使用.agent/skills/name，Owner使用skills/learned/name；拒绝覆盖任何已存在资产目录。
# 函数用途: 调整范围布局并在发布前确认新资产没有重名。
def _prepare_files(root, files, scope):
    files = {(parts[0], *parts[2:]) if scope == 'workspace' and parts[0] == 'skills' else parts: text
             for parts, text in files.items()}
    for parts, text in files.items():
        if len(text.encode('utf-8')) > MAX_ASSET_BYTES:
            raise ValueError('ASSET_RECORD_TOO_LARGE')
        try:
            descriptor = open_directory_beneath(root, parts[:-1])
        except FileNotFoundError:
            continue
        os.close(descriptor)
        raise FileExistsError('EXTRACTION_ASSET_ALREADY_EXISTS')
    return files


# LLM: 调用方已持根锁和scripts集合锁；依赖只用_get读取本scope，禁止公开方法再次取根锁。
# 函数用途: 防止只选择工作流却留下悬空脚本或Skill。
def _check_dependencies(root, chosen, scripts):
    selected = {(kind, _validate_asset(kind, item)) for kind, item in chosen}
    for kind, item in chosen:
        if kind != 'workflow':
            continue
        definition = WorkflowDefinition.from_any(item)
        for script_id in definition.descriptor.refs.scripts:
            if ('script', script_id) not in selected and scripts._get(script_id) is None:
                raise ValueError('EXTRACTION_DEPENDENCY_MISSING: script')
        for skill in definition.descriptor.refs.skills:
            if ('skill', skill) not in selected and not _skill_exists(root, skill):
                raise ValueError('EXTRACTION_DEPENDENCY_MISSING: skill')


# LLM: 只在当前资产根查两个规范Skill位置，nofollow读取，不跨Owner枚举。
# 函数用途: 确认工作流引用的已有技能确实存在。
def _skill_exists(root, name):
    return any(read_bytes_beneath(root, parts, max_bytes=MAX_ASSET_BYTES) is not None
               for parts in [('skills', name, 'SKILL.md'), ('skills', 'learned', name, 'SKILL.md')])


# LLM: 只回滚锁内证实不存在的新资产；恢复失败不得报成功，不删除既有文件。
# 函数用途: 删除本批已写的叶及空资产目录。
def _rollback_files(root, files):
    for parts in reversed(tuple(files)):
        unlink_file_beneath(root, parts)
    for parts in dict.fromkeys(parts[:-1] for parts in reversed(tuple(files))):
        try:
            parent = open_directory_beneath(root, parts[:-1])
        except FileNotFoundError:
            continue
        try:
            os.rmdir(parts[-1], dir_fd=parent)
        except FileNotFoundError:
            pass
        finally:
            os.close(parent)


# LLM: proposal锁跨读取/CAS/发布，原集合锁阻断并发同库写；仅相同选项幂等，不重放不同编辑。
# 函数用途: 用户确认后按依赖顺序发布资产，记录提交回执。
def commit_extracted_assets(owner_home: Path, *, proposal_id: str, selected_assets: Sequence[Mapping[str, object]] | None = None,
                             scope: str | None = None, workspace_root: Path | None = None, expected_revision: int | None = None) -> dict[str, object]:
    with _record_lock(owner_home, _PROPOSALS):
        proposal = get_extraction_proposal(owner_home, proposal_id)
        scope = scope or proposal['scope']
        project = workspace_root if workspace_root is not None else proposal.get('workspace_root') or None
        root = _asset_root(owner_home, scope, project)
        chosen = _select_assets(proposal, selected_assets)
        key = hashlib.sha256(json.dumps([scope, str(root), chosen], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if proposal['status'] == 'COMMITTED':
            if proposal.get('commit_key') != key:
                raise ValueError('EXTRACTION_ALREADY_COMMITTED')
            return proposal['commit_result']
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision != proposal['revision']):
            raise ValueError('EXTRACTION_REVISION_CONFLICT')
        return _commit_batch(owner_home, root, proposal, chosen, scope, key)


# LLM: 根锁仅取一次，再按scripts/workflows固定顺序取集合锁；锁内只用私有读取，失败回滚不吞中断。
# 函数用途: 执行一次无覆盖的批量写入并持久保存COMMITTED回执。
def _commit_batch(home, root, proposal, chosen, scope, key):
    ensure_private_dir(root)
    scripts, workflows = ScriptStore(root), WorkflowStore(root)
    for collection in (scripts.collection, workflows.collection):
        os.close(open_directory_beneath(root, (collection,), create=True))
    with (
        asset_root_lock(root),
        locked_json_path(root / scripts.collection / '.assets'),
        locked_json_path(root / workflows.collection / '.assets'),
    ):
        files, assets = _asset_files(chosen)
        files = _prepare_files(root, files, scope)
        _check_dependencies(root, chosen, scripts)
        receipt = {'ok': True, 'proposal_id': proposal['proposal_id'], 'status': 'COMMITTED', 'committed_assets': assets}
        updated = {**proposal, 'status': 'COMMITTED', 'revision': proposal['revision'] + 1,
                   'committed_assets': assets, 'commit_key': key, 'commit_result': receipt}
        try:
            for parts, text in files.items():
                write_text_atomic_beneath(root, parts, text)
            _write_record(home, _PROPOSALS, updated, 'proposal_id')
        except BaseException:
            _rollback_files(root, files)
            _write_record(home, _PROPOSALS, proposal, 'proposal_id')
            raise
    return receipt
