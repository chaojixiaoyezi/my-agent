"""回合内有界代码变更账；归档状态只留身份与哈希，收口才冻结已执行的字节。"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shlex
import stat
from datetime import datetime, timezone
from pathlib import Path

from ..common.log_redaction import redact_sensitive_text
from ..common.nofollow_fs import read_bytes_beneath, write_text_atomic_beneath
from . import safe_asset_id
from .descriptor import execution_definition_ref
from .revision_history import blob_put, get_version
from .script_store import ScriptStore


# LLM: 只解码对象或有界JSON，不解释工具正文的自然语言。
# 函数用途: 读取结构化回执。
def evolution_receipt(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and len(value) <= 1048576:
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except ValueError:
            pass
    return {}


# LLM: 限深限宽遍历已知回执容器，缺失或坏类型不阻断原工具。
# 函数用途: 判断本次查询是否被明确阻止分析。
def query_analysis_blocked(receipt, *, depth=0):
    if depth > 8:
        return False
    value = evolution_receipt(receipt)
    source = value.get('source_check')
    health = source.get('query_health') if isinstance(source, dict) else None
    if isinstance(health, dict) and health.get('analysis_blocked') is True:
        return True
    health = value.get('query_health')
    if isinstance(health, dict) and health.get('analysis_blocked') is True:
        return True
    for key in ('result', 'output', 'data', 'receipt', 'escalation_result'):
        if query_analysis_blocked(value.get(key), depth=depth + 1) if key in value else False:
            return True
    for key in ('steps', 'results', 'tool_results'):
        rows = value.get(key)
        if isinstance(rows, (list, tuple)) and any(query_analysis_blocked(row, depth=depth + 1) for row in rows[:64]):
            return True
    return False


# LLM: JSON输出身份不包含原始输入或输出；不接受非有限数值。
# 函数用途: 给执行快照计算可比较哈希。
def evolution_value_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


# LLM: 只提取限定种类且有界的字面值；文本不决定授权或晋升。
# 函数用途: 给确定性分类提供回合字面量集合。
def turn_literal_values(text, *, max_chars=65536, limit=128):
    pattern = r'(?<!\w)(?:[0-2]\d:[0-5]\d|job[\w-]+|(?:\d{1,3}\.){3}\d{1,3}|\d{4}-\d{2}-\d{2}|[\w.-]+\.[A-Za-z][A-Za-z0-9]{0,7})(?!\w)'
    return sorted(set(re.findall(pattern, str(text)[:max_chars])))[:limit]


# LLM: 解析预算由代码捕获帽限制，失败不影响工具；这里只缓存完整字符串的哈希而非原值。
# 函数用途: 记录后续新增字面量比较所需的有界身份。
def evolution_literal_hashes(raw, *, limit=128):
    try:
        tree = ast.parse(raw)
    except (SyntaxError, UnicodeError, ValueError, RecursionError):
        return []
    return sorted({hashlib.sha256(node.value.encode()).hexdigest() for node in ast.walk(tree)
                   if isinstance(node, ast.Constant) and type(node.value) is str})[:limit]


# LLM: 每次只读取刚被工具指向的一个普通文件；nofollow与根范围检查不可由工具参数关闭。
# 函数用途: 有界读取工作区脚本，拒绝链接及Owner固化库旁路。
def read_workspace_script(workspace_root, owner_home, path, *, max_bytes=262144):
    root = Path(os.path.abspath(workspace_root))
    target = Path(os.path.abspath(root / path))
    parts = target.relative_to(root).parts
    home = Path(os.path.abspath(owner_home))
    if any(target.is_relative_to(home / name) for name in ('scripts', 'workflows', 'data')):
        raise ValueError('host managed capture excluded')
    if not parts or target.suffix.casefold() not in ('.py', '.sh', '.js', '.mjs'):
        raise ValueError('not a script target')
    info = target.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('capture requires a regular file')
    if info.st_size > max_bytes:
        return None, info.st_size, str(target)
    raw = read_bytes_beneath(root, parts, max_bytes=max_bytes)
    return raw, len(raw) if raw is not None else 0, str(target)


# LLM: 所有状态属于单run的live_archive_state；无代码、输入、输出或秘密值存入该容器。
# 类用途: 在工具成功后记录有界变更与实际执行身份。
class AssetMutationLedger:
    max_targets = 32
    max_capture_bytes = 262144
    max_facts_per_target = 64

    # LLM: 外层保证任何观察错误不进入工具循环，绝不修改传入receipt。
    # 函数用途: 最佳努力观察一条工具调用。
    def observe(self, state, tool_name, arguments, receipt, *, owner_home, workspace_root,
                call_id='', observed_at=None, archive_ref=''):
        try:
            self._observe(state, tool_name, arguments, receipt, owner_home=owner_home,
                          workspace_root=workspace_root, call_id=call_id,
                          observed_at=observed_at, archive_ref=archive_ref)
        except Exception:
            return

    # LLM: 工具名、参数及结构化执行回执是唯一来源；每次只处理明确文件，补丁路径也有硬帽。
    # 函数用途: 分派四种捕获源并留下查询失败标记。
    def _observe(self, state, tool_name, arguments, receipt, *, owner_home, workspace_root,
                 call_id, observed_at, archive_ref):
        book = state.setdefault('asset_mutations', {'targets': {}, 'evidence_tags': []})
        state.setdefault('workspace_root', str(workspace_root))
        value = evolution_receipt(receipt)
        if query_analysis_blocked(value):
            book['evidence_tags'] = ['query_failed']
        name = tool_name.rsplit('.', 1)[-1]
        args = arguments if isinstance(arguments, dict) else {}
        timestamp = observed_at or datetime.now(timezone.utc).isoformat()
        if value.get('ok') is True:
            from .asset_metadata import normalize_member_of

            members = list(book.get('member_of', []))
            if name == 'skill_search' and args.get('action') == 'get' and isinstance(args.get('skill_id'), str):
                members.append({'kind': 'skill', 'id': args['skill_id'].partition(':')[2] or args['skill_id']})
            if name == 'workflows' and args.get('action') in ('run', 'get'):
                members.append({'kind': 'workflow', 'id': args.get('workflow_id')})
            book['member_of'] = normalize_member_of(members)
        if name == 'scripts':
            asset_id = args.get('script_id') or args.get('name') or args.get('id')
            if not asset_id:
                return
            safe_asset_id(asset_id)
            key = 'script:' + asset_id
            if args.get('action') in ('register', 'update') and value.get('ok') is True:
                code = args.get('code', args.get('entry_source'))
                if isinstance(code, str):
                    self._mutation(book, key, code.encode(), len(code.encode()), tool_name=name,
                                   call_id=call_id, observed_at=timestamp, asset_id=asset_id,
                                   existing=args['action'] == 'update')
            elif args.get('action') == 'run':
                if key not in book['targets'] and len(book['targets']) >= self.max_targets:
                    return
                raw, source = self._script_execution(owner_home, workspace_root, asset_id, args, value)
                book['targets'].setdefault(key, {'kind': 'script', 'asset_id': asset_id, 'path': None,
                                                 'existing': True, 'facts': []})
                self._execution(book, key, raw, value, call_id, timestamp, archive_ref,
                                args.get('inputs', {}), source=source)
            return
        if name in ('write_file', 'edit_file', 'apply_patch') and value.get('ok') is True:
            paths = [args['path']] if isinstance(args.get('path'), str) else []
            if name == 'apply_patch':
                paths = re.findall(r'^\*\*\* (?:Add|Update) File: (.+)$', args.get('patch', ''), re.M)
                paths += re.findall(r'^\*\*\* Move to: (.+)$', args.get('patch', ''), re.M)
            for path in dict.fromkeys(paths[:self.max_targets]):
                try:
                    raw, size, target = read_workspace_script(workspace_root, owner_home, path,
                                                            max_bytes=self.max_capture_bytes)
                    self._mutation(book, 'path:' + target, raw, size, tool_name=name,
                                   call_id=call_id, observed_at=timestamp, path=target)
                except (OSError, ValueError):
                    continue
        if name == 'run_command':
            tokens = shlex.split(args.get('command', ''))
            # 与回合提取的命令约定对齐；复合shell不是单脚本执行证明。
            if not tokens or len(tokens) > 2 or any(c in args.get('command', '') for c in (';', '|', '&', '<', '>')):
                return
            cwd = args.get('working_dir') or workspace_root
            for token in tokens[:2]:
                target = str(Path(os.path.abspath(Path(cwd) / token)))
                key = 'path:' + target
                if key not in book['targets']:
                    continue
                raw, _, _ = read_workspace_script(workspace_root, owner_home, target,
                                                 max_bytes=self.max_capture_bytes)
                if raw is not None:
                    self._execution(book, key, raw, value, call_id, timestamp, archive_ref, {})
                break

    # LLM: 只按本次显式版本读取一份不可变记录和两个有界blob；工作区覆盖优先，身份不符不借当前字节补齐。
    # 函数用途: 把实际执行回执绑定到同来源、同定义的代码快照。
    def _script_execution(self, owner_home, workspace_root, asset_id, args, receipt):
        from ..workflows.datasource_runtime import _historical_execution_record

        record = ScriptStore(owner_home, workspace_root=workspace_root).get_script(asset_id)
        source = {'source_scope': record.get('source_scope', 'owner') if record else '',
                  'requested_version': args.get('version')}
        if record is None:
            return None, source
        data = evolution_receipt(receipt.get('result', receipt.get('output', receipt)))
        actual = data.get('execution_definition_ref')
        if not actual:
            return None, source
        snapshot = record
        version = args.get('version')
        if version is not None:
            if source['source_scope'] != 'owner':
                return None, source
            history = get_version(owner_home, 'script', asset_id, version)
            if history is None:
                return None, source
            source.update(execution_version=history['version'],
                          published_definition_ref=history['execution_definition_ref'])
            resolution = data.get('source_check', {}).get('pin_resolution', {})
            if resolution.get('mode') == 'history':
                if resolution.get('version') != version or resolution.get('code_sha256') != history['code_sha256']:
                    return None, source
                blobs = []
                for digest, limit in ((history['code_sha256'], self.max_capture_bytes),
                                      (history['payload_blob'], 4194304)):
                    if not isinstance(digest, str) or re.fullmatch(r'[0-9a-f]{64}', digest) is None:
                        return None, source
                    raw = read_bytes_beneath(Path(owner_home),
                        ('data', 'asset_evolution', 'blobs', digest[:2], digest), max_bytes=limit)
                    if raw is None or hashlib.sha256(raw).hexdigest() != digest:
                        return None, source
                    blobs.append(raw)
                snapshot, _ = _historical_execution_record(record, json.loads(blobs[1]), blobs[0])
            elif (hashlib.sha256(record['code'].encode()).hexdigest() != history['code_sha256']
                  or actual != history['execution_definition_ref']):
                return None, source
        else:
            source['published_definition_ref'] = record['execution_definition_ref']
        raw = snapshot['code'].encode()
        if len(raw) > self.max_capture_bytes or actual != execution_definition_ref(snapshot, kind='script'):
            return None, source
        return raw, source

    # LLM: 历史条数与目标数都受帽控制，溢出只存一个计数事实而非继续缓存代码。
    # 函数用途: 追加代码哈希与大小变更。
    def _mutation(self, book, key, raw, size, *, tool_name, call_id, observed_at,
                  asset_id=None, path=None, existing=False):
        targets = book['targets']
        if key not in targets and len(targets) >= self.max_targets:
            book['oversize'] = {'schema_version': 'asset_mutation.v1', 'status': 'oversize', 'reason': 'target_cap'}
            return
        target = targets.setdefault(key, {'kind': 'script', 'asset_id': asset_id, 'path': path,
                                          'existing': existing, 'facts': []})
        over = raw is None or size > self.max_capture_bytes
        fact = {'schema_version': 'asset_mutation.v1', 'target_kind': 'script',
                'asset_id': asset_id, 'path': path,
                'sha256': None if over else hashlib.sha256(raw).hexdigest(), 'size_bytes': size,
                'tool_name': tool_name, 'call_id': call_id, 'observed_at': observed_at,
                'literal_hashes': [] if over else evolution_literal_hashes(raw),
                'status': 'oversize' if over else 'captured'}
        target['facts'] = [*target['facts'], fact][-self.max_facts_per_target:]
        target['last_mutation'] = fact

    # LLM: started或无退出证明不算执行通过；后台命令必须等真实exit回执，输入只存hash。
    # 函数用途: 记录同字节的执行结果及输出身份。
    def _execution(self, book, key, raw, receipt, call_id, timestamp, archive_ref, inputs, *, source=None):
        target = book['targets'].get(key)
        if target is None:
            return
        data = evolution_receipt(receipt.get('result', receipt.get('output', receipt)))
        status = data.get('status')
        ok = receipt.get('ok') is True and data.get('ok', True) is True
        if status == 'started' or data.get('exit_code', data.get('return_code', 0)) not in (None, 0):
            ok = False
        if receipt.get('handler_executed') is False:
            return
        output = data.get('output_data', data.get('parsed_output'))
        if output is None and isinstance(data.get('stdout'), str):
            try:
                output = json.loads(data['stdout'])
            except ValueError:
                output = data['stdout']
        fact = {'schema_version': 'asset_execution.v1', 'target': key,
                'sha256': hashlib.sha256(raw).hexdigest() if raw is not None else None, 'ok': ok,
                'verified': raw is not None,
                'literal_hashes': evolution_literal_hashes(raw) if raw is not None else [],
                'error_code': str(data.get('error_code') or receipt.get('error_code') or ''),
                'call_id': call_id, 'observed_at': timestamp,
                'execution_definition_ref': data.get('execution_definition_ref'),
                'input_hash': evolution_value_hash(inputs),
                'output_hash': evolution_value_hash(output) if output is not None else '',
                'archive_ref': archive_ref, **(source or {})}
        target['facts'] = [*target['facts'], fact][-self.max_facts_per_target:]
        target['last_execution'] = fact


# LLM: 只冻结最后一次变更之后同sha成功执行且仍可读回的代码；变更/执行次序按事实顺序，不按时间猜。
# 函数用途: 收口原子创建演进请求，重复run不改已有请求。
def freeze_asset_mutations(owner_home, state, *, run_id, owner_id='', thread_id='',
                           user_prompt='', member_of=(), records=()):
    safe_asset_id(run_id)
    book = state.get('asset_mutations', {})
    tagged = book.get('evidence_tags') == ['query_failed'] or any(query_analysis_blocked(row) for row in records)
    targets, executions = [], []
    for target in book.get('targets', {}).values():
        mutation = target.get('last_mutation', {})
        facts = target.get('facts', [])
        if target.get('asset_id'):
            executions.extend({'asset_id': target['asset_id'], 'execution': row} for row in facts
                              if row.get('schema_version') == 'asset_execution.v1')
        last_index = max((i for i, row in enumerate(facts) if row.get('schema_version') == 'asset_mutation.v1'), default=-1)
        execution = next((row for row in reversed(facts[last_index + 1:])
                          if row.get('schema_version') == 'asset_execution.v1'
                          and row.get('sha256') == mutation.get('sha256')), {})
        if tagged or mutation.get('status') != 'captured' or execution.get('ok') is not True:
            continue
        try:
            record = ScriptStore(owner_home, workspace_root=state.get('workspace_root')).get_script(
                target['asset_id']) if target.get('asset_id') else None
            if record and (execution.get('verified') is not True
                    or execution.get('execution_definition_ref') != record['execution_definition_ref']
                    or execution.get('source_scope') != record.get('source_scope', 'owner')):
                continue
            raw = record['code'].encode() if record else read_workspace_script(
                state.get('workspace_root', owner_home), owner_home, target['path'])[0]
            if raw is None or hashlib.sha256(raw).hexdigest() != mutation['sha256']:
                continue
            # 含裸秘密的代码不可进入学习blob；不修改代码来伪造原验证身份。
            text = raw.decode('utf-8')
            if redact_sensitive_text(text) != text:
                continue
            digest = blob_put(owner_home, raw)
            targets.append({**{key: target[key] for key in ('kind', 'asset_id', 'path', 'existing')},
                            'final_sha256': digest, 'verified': True,
                            'sha_history': [row['sha256'] for row in facts if row.get('schema_version') == 'asset_mutation.v1' and row.get('sha256')],
                            'facts': facts, 'literals': turn_literal_values(user_prompt),
                            'execution': execution,
                            'io_contract': record.get('io_contract', {}) if record else {},
                            'access_targets': record.get('access_targets', []) if record else []})
        except (OSError, ValueError, KeyError):
            continue
    if not targets and not tagged and not executions:
        return None
    from .asset_metadata import normalize_member_of
    request = {'schema_version': 'asset_evolution_request.v1', 'run_id': run_id,
               'owner_id': owner_id, 'thread_id': thread_id,
               'created_at': datetime.now(timezone.utc).isoformat(),
               'evidence_tags': ['query_failed'] if tagged else [], 'targets': targets,
               'executions': [] if tagged else executions,
               'member_of': normalize_member_of([*member_of, *book.get('member_of', [])])}
    parts = ('data', 'asset_evolution', 'requests', run_id + '.json')
    from ..common.json_io import locked_json_path
    from . import _verify_directory_beneath
    parent = _verify_directory_beneath(Path(owner_home), parts[:-1], create=True)
    with locked_json_path(parent / parts[-1]):
        previous = read_bytes_beneath(Path(owner_home), parts, max_bytes=4194304)
        if previous is not None:
            return json.loads(previous)
        write_text_atomic_beneath(Path(owner_home), parts, json.dumps(request, ensure_ascii=False, allow_nan=False))
    return request
