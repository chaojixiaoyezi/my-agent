"""业务观察只读入口；原任务、执行、消息和事件仍是唯一事实源。

额外reader由宿主绑定原账本，不接受请求提供的路径、owner或可执行回调。
本模块不取claim、不确认观察、不启动runner，也不建立第二份事件/任务状态。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import time
from collections import deque
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..common.log_redaction import redact_sensitive_value
from ..plugin_host.business_action_specs import business_defs
from ..runtime_db.recovery_guard import RuntimeDatabaseDeferred
from ..runtime_db.repository import AGENT_RUN_TERMINAL_STATUSES, RuntimeRepository
from ..runtime_errors import DataCorruptionError
from .artifact_access import artifact_query_access
from .background_scheduling_view import task_result_view, task_scheduling_view
from .background_team_lifecycle import background_identity
from .business_access import (
    BusinessError,
    require_access,
    require_actor,
    validate_params,
    validate_result,
)
from .business_contracts import BusinessExecutionContext
from .session_tasks import SESSION_TASK_TERMINAL_STATUSES

OBSERVATION_ACTIONS = frozenset({
    'tasks.list', 'tasks.get', 'agent_tree.get', 'subagents.list', 'subagents.get', 'messages.list',
    'executions.list', 'executions.get', 'tool_calls.list', 'tool_calls.get', 'logs.read',
    'artifacts.list', 'artifacts.get', 'changes.get', 'approvals.list', 'approvals.get',
    'permissions.get', 'usage.get', 'events.list', 'operations.get',
})

_observation_request_data = ContextVar('business_observation_request_data', default=None)

# 状态页只核验既有引用；单个登记簿最多读取 256 KiB，超限保留引用并报告部分覆盖。
_RESULT_REGISTRY_MAX_BYTES = 256 * 1024
# 同一观察请求的所有任务共享 4 MiB（包括超限检测的一个字节），避免大页读放大。
_RESULT_REGISTRY_TOTAL_MAX_BYTES = 4 * 1024 * 1024
# 解析对象与坏行诊断也限额，不能只限制raw字节后再生成成千上万错误对象。
_RESULT_REGISTRY_MAX_ROWS = 1024
_RESULT_REGISTRY_TOTAL_MAX_ROWS = 16384
_RESULT_REGISTRY_MAX_DIAGNOSTICS = 8
_RESULT_REGISTRY_TOTAL_MAX_DIAGNOSTICS = 32


@dataclass
class _ResultRegistryBudget:
    remaining: int = _RESULT_REGISTRY_TOTAL_MAX_BYTES
    remaining_rows: int = _RESULT_REGISTRY_TOTAL_MAX_ROWS
    remaining_diagnostics: int = _RESULT_REGISTRY_TOTAL_MAX_DIAGNOSTICS
    exhausted_reason: str = ''

    def limits(self, max_bytes, max_rows, max_diagnostics):
        for exhausted, code in ((self.remaining <= 1, 'ARTIFACT_REGISTRY_READ_BUDGET_EXCEEDED'),
                (self.remaining_rows <= 1, 'ARTIFACT_REGISTRY_ROW_BUDGET_EXCEEDED'),
                (self.remaining_diagnostics <= 0, 'ARTIFACT_REGISTRY_DIAGNOSTIC_BUDGET_EXCEEDED')):
            if exhausted and not self.exhausted_reason:
                self.exhausted_reason = code
        if self.exhausted_reason:
            return None
        return (min(max_bytes, self.remaining - 1), min(max_rows, self.remaining_rows - 1),
                min(max_diagnostics, self.remaining_diagnostics))

    def observe(self, report):
        self.remaining_rows -= report.rows_read or int(bool(report.errors))
        self.remaining_diagnostics -= len(report.errors)

    def read(self, workspace):
        from ..artifacts.registry import latest_artifact_records_report

        limits = self.limits(_RESULT_REGISTRY_MAX_BYTES, _RESULT_REGISTRY_MAX_ROWS, _RESULT_REGISTRY_MAX_DIAGNOSTICS)
        if limits is None:
            return None
        limit, rows, diagnostics = limits
        report = latest_artifact_records_report(workspace, max_bytes=limit, max_rows=rows, max_diagnostics=diagnostics)
        self.remaining -= report.bytes_read or (limit + 1 if report.errors else 0)
        self.observe(report)
        return report


@dataclass
class _ObservationRequestData:
    values: dict = field(default_factory=dict)
    registry_budget: _ResultRegistryBudget = field(default_factory=_ResultRegistryBudget)


class ObservationSourceError(BusinessError):
    """可信只读来源的脱敏固定错误；保留原因和恢复指引，不扩大授权。"""


# LLM: 只复用当前调用栈的数据，不保存actor或授权裁决；异常退出也必须丢弃。
# 函数用途: 为嵌套冻结调用和直接观察查询建立请求局部读缓存。
@contextmanager
def observation_request():
    if _observation_request_data.get() is not None:
        yield
        return
    token = _observation_request_data.set(_ObservationRequestData())
    try:
        yield
    finally:
        _observation_request_data.reset(token)


# LLM: None和读失败也在本请求复用；无作用域时不缓存，下一请求必须重新读事实。
# 函数用途: 在当前请求内复用一次规范读取。
def _observation_cached(key, read):
    request = _observation_request_data.get()
    if request is None:
        return read()
    cache = request.values
    if key not in cache:
        if len(cache) >= 1024:
            cache.pop(next(iter(cache)))
        try:
            cache[key] = True, read()
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as error:
            cache[key] = False, error
    succeeded, value = cache[key]
    if not succeeded:
        raise value
    return value


# LLM: 以原Store组件和准确ID为键，不将同名其它owner数据复用。
# 函数用途: 对当前请求的distinct线程只执行一次require。
def _observation_thread(store, thread_id):
    return _observation_cached((store.threads, 'record', thread_id), lambda: store.threads.require(thread_id))


# LLM: 缺失任务同样只读一次，但不能跨请求保存缺失或身份事实。
# 函数用途: 对当前请求的distinct任务只执行一次load。
def _public_history_text(value, *, label):
    """预览与公开过程共用字节预算；先有界脱敏，再在UTF-8完整码点处收束。"""
    from ..plugin_host.schemas import REQUEST_MAX_BYTES

    # maxLength 是字符上限；未声明x-max-bytes时沿原Schema默认字节门，不扩大响应额度。
    limit = min(262144, REQUEST_MAX_BYTES)
    original = str(value or '')
    # 原脱敏器支持终态截断的秘密字段/PEM；只解析有界头部，超长原文仍留在canonical。
    preview = original[:limit + 512]
    encoded = str(redact_sensitive_value(preview)).encode('utf-8', errors='replace')
    if len(preview) == len(original) and len(encoded) <= limit:
        return encoded.decode('utf-8')
    suffix = f"\n...[{label}过长已截断预览，原长度 {len(original)} 字符]..."
    head = encoded[:limit - len(suffix.encode('utf-8'))].decode('utf-8', errors='ignore')
    return head + suffix


def _message_execution_links(service, messages, thread, actor):
    """本页显式ID按原索引定位；关联是执行上下文，不推断单个工具的语义归属。"""
    from ..common.nofollow_fs import read_bytes_beneath
    from ..io.sqlite_scope import connect_projection_sqlite
    from ..runtime_errors import DataCorruptionError
    from .store_guidance_records import GuidanceOnceReceipt

    output = {row.message_id: {'input_ref': None, 'execution_refs': [],
              'association_coverage': 'unknown', 'association_reason': 'MESSAGE_EXECUTION_ID_NOT_VERIFIED'} for row in messages}
    runtime = getattr(service, 'runtime', None)
    if runtime is None or not callable(getattr(runtime, '_runtime_connection', None)):
        for link in output.values():
            link['association_reason'] = 'RUNTIME_EXECUTION_SOURCE_UNAVAILABLE:NOT_BOUND'
        return output
    inputs = {row.message_id: row.metadata['guidance_id'] for row in messages
              if row.role in {'user', 'host'} and isinstance(row.metadata.get('guidance_id'), str)}
    receipts = {}
    storage = service.store.storage
    index = storage.root / '.guidance-pending.sqlite3'
    if inputs and index.is_file():
        try:
            with closing(connect_projection_sqlite(index, read_only=True, timeout=0.1)) as conn:
                conn.row_factory = sqlite3.Row
                marks = ','.join('?' for _ in inputs)
                indexed = conn.execute('SELECT id,dedupe AS dedupe_key FROM entries WHERE id IN (' + marks + ')',
                                       tuple(inputs.values())).fetchall()
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, DataCorruptionError, RuntimeDatabaseDeferred):
            indexed = []
        for ref in indexed:
            try:
                path = storage.guidance_dedupe_path(ref['dedupe_key'])
                raw = read_bytes_beneath(storage.root, path.relative_to(storage.root).parts, max_bytes=262144)
                if raw is None:
                    continue
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    continue
                receipt = GuidanceOnceReceipt.from_dict(payload)
                metadata = receipt.entry.metadata
                if (receipt.entry.guidance_id == ref['id'] and receipt.dedupe_key == ref['dedupe_key']
                        and metadata.get('thread_id') == thread.thread_id
                        and receipt.status in {'submitted', 'consumed'} and receipt.attempt_id):
                    receipts[ref['id']] = receipt
            except (OSError, ValueError, TypeError, KeyError, DataCorruptionError):
                # 坏回执只影响自己；观察不修投递账，不按正文或时间补关联。
                continue
    attempts = tuple(dict.fromkeys(receipt.attempt_id for receipt in receipts.values()))
    message_runs = {}
    for row in messages:
        if row.role not in {'user', 'assistant'}:
            continue
        # 原后台最终投递的机器幂等引用编码wake身份；背景显示turn不是执行run。
        # 解码只提供查询键，后续仍须同owner/thread及规范执行权限核验。
        key = row.metadata.get('dedupe_key')
        prefix = 'owner_delivery:background-wake:'
        identity = (key[len(prefix):] if isinstance(key, str) and key.startswith(prefix)
                    else row.metadata.get('conversation_request_id'))
        if isinstance(identity, str) and identity:
            message_runs[row.message_id] = identity
    runs = tuple(dict.fromkeys(message_runs.values()))
    clauses, values = [], []
    for column, identifiers in (('aa.attempt_id', attempts), ('ar.run_id', runs)):
        if identifiers:
            marks = ','.join('?' for _ in identifiers)
            clauses.append(('aa.attempt_id IN (SELECT r.current_attempt_id FROM agent_runs r WHERE r.run_id IN ('
                            + marks + ') AND NOT EXISTS (SELECT 1 FROM agent_attempts other '
                            'WHERE other.agent_run_id=r.agent_run_id AND other.attempt_id!=r.current_attempt_id))')
                           if column == 'ar.run_id' else column + ' IN (' + marks + ')')
            values.extend(identifiers)
    if not clauses:
        return output
    try:
        with runtime._runtime_connection() as conn:
            raw = conn.execute('SELECT NOT EXISTS (SELECT 1 FROM agent_attempts other '
                'WHERE other.agent_run_id=ar.agent_run_id AND other.attempt_id!=aa.attempt_id) AS _single_attempt,'
                'ar.run_id,ar.current_attempt_id,ar.parent_agent_run_id,pa.run_id AS parent_run_id,'
                't.thread_id,t.task_id AS runtime_task_id,tr.metadata_json,aa.attempt_id,aa.status,aa.started_at,aa.ended_at,'
                'aa.metadata_json AS attempt_metadata FROM agent_attempts aa JOIN agent_runs ar ON ar.agent_run_id=aa.agent_run_id '
                'JOIN task_runs tr ON tr.task_run_id=ar.task_run_id JOIN tasks t ON t.task_id=tr.task_id '
                'LEFT JOIN agent_runs pa ON pa.agent_run_id=ar.parent_agent_run_id WHERE t.owner_id=? AND t.thread_id=? AND ('
                + ' OR '.join(clauses) + ') ORDER BY aa.rowid LIMIT 101', [service.owner_id, thread.thread_id, *values]).fetchall()
    except (OSError, sqlite3.Error, RuntimeDatabaseDeferred) as error:
        for link in output.values():
            link['association_reason'] = 'RUNTIME_EXECUTION_SOURCE_UNAVAILABLE:' + type(error).__name__
        return output
    single_attempt_runs = {row['run_id'] for row in raw[:100] if row['_single_attempt']}
    records = service._execution_page(raw[:100]).records
    by_attempt, by_run = {}, {}
    for record in records:
        if service._visible(record, 'executions.get', {}, actor):
            by_attempt[record.attempt_id] = record
            by_run.setdefault(record.run_id, []).append(record)
    for message in messages:
        receipt = receipts.get(inputs.get(message.message_id))
        execution = by_attempt.get(receipt.attempt_id) if receipt else None
        candidates = by_run.get(message_runs.get(message.message_id), [])
        refs = ([execution] if execution and execution.run_id == receipt.entry.metadata.get('expected_turn_id')
                else candidates if message.role in {'user', 'assistant'}
                and message_runs.get(message.message_id) in single_attempt_runs else [])
        if message.message_id in message_runs and not refs:
            output[message.message_id]['association_reason'] = 'RUN_EXECUTION_ATTEMPT_NOT_UNIQUE_OR_UNAVAILABLE'
        if refs:
            output[message.message_id] = {'input_ref': (receipt.entry.guidance_id if receipt else message.message_id if message.role == 'user' else None),
                'execution_refs': [{'run_id': record.run_id, 'attempt_id': record.attempt_id} for record in refs],
                'association_coverage': 'partial' if len(raw) > 100 else 'complete',
                'association_reason': 'ATTEMPT_CONTEXT_NOT_EXCLUSIVE_TOOL_OWNERSHIP'}
    return output


def _observation_task(store, task_id):
    return _observation_cached((store.session_tasks, 'record', task_id), lambda: store.session_tasks.load(task_id))


def _copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value else None


def _view(kind, row):
    props = business_defs()[kind]['properties']
    result = {}
    for key, spec in props.items():
        if key in row:
            val = row[key]
            if val == '' and spec.get('$ref') == '#/$defs/NullableId':
                val = None
            result[key] = _copy(val)
    return redact_sensitive_value(result)


# LLM: 仅当交付元数据携带合法64位十六进制sha256摘要时返回小写规范值；缺失或格式异常返回None，不伪造版本摘要。
# 函数用途: 校验并规范化交付项记录的版本摘要钉（sha256）。
def _valid_sha256_pin(value: object) -> str | None:
    if isinstance(value, str) and len(value) == 64:
        lowered = value.lower()
        if all(ch in '0123456789abcdef' for ch in lowered):
            return lowered
    return None


# LLM: 按父目录有界回溯最近的工作区产物登记簿，复用请求级只读缓存避免重复IO；无登记簿时不报错，有可读登记簿且当前记录缺失、损坏或sha256已变时如实报告漂移。
# 函数用途: 检测可读本地产物登记簿上当前记录是否已偏离任务终态冻结的sha256版本摘要。
def _result_source_link_root(task, store, owner_root, identifier):
    """精确TaskLink只印证同目标线程/Owner内的原登记根，不按同目录推任务归属。"""
    if store is None or owner_root is None or task is None or not identifier:
        return None

    def read():
        try:
            owner = Path(owner_root).resolve(strict=False)
            thread = _observation_thread(store, task.target_thread_id)
            sender = _observation_thread(store, task.sender_thread_id)
            if (not thread.owner_id or sender.owner_id != thread.owner_id
                    or any(Path(row.owner_home).resolve(strict=False) != owner for row in (thread, sender))):
                return None
            context = (getattr(task, 'metadata', None) or {}).get('business_context')
            if context is not None and (not isinstance(context, dict) or context.get('owner_id') != thread.owner_id):
                return None
            link, error = store.tasks.link_report(task.target_thread_id, identifier)
            if error or link is None or link.task_id != identifier or not link.task_path:
                return None
            actor = getattr(link, 'host_channel_actor', None)
            if actor is not None and (not isinstance(actor, dict) or actor.get('owner_id') != thread.owner_id):
                return None
            root = Path(link.task_path).resolve(strict=False)
            return root if root != owner and root.is_relative_to(owner) else None
        except (OSError, ValueError, TypeError, AttributeError, KeyError, sqlite3.Error,
                DataCorruptionError, RuntimeDatabaseDeferred):
            return None

    return _observation_cached((store.tasks, 'result_source_root', task.target_thread_id, identifier, str(owner_root)), read)


def _result_scope_ref(value):
    return value if (isinstance(value, str) and value and value == value.strip()
                     and len(value.encode('utf-8')) <= 128 and '/' not in value and '\\' not in value) else None


def _registry_pin_issue(path: str, artifact_id: str, pinned_sha: str, dir_cache: dict[str, Path | None],
                        registry_root: Path | None = None, *, task=None, store=None, owner_root=None,
                        source_task_id=None, source_run_id=None) -> str | None:
    from ..artifacts.registry import REGISTRY_RELATIVE_PATH

    candidate = Path(path)
    if not candidate.is_absolute():
        return 'RESULT_ARTIFACT_SOURCE_UNPROVEN'
    allowed_roots = {value for value in (getattr(task, 'task_id', None),
                                       getattr(task, 'conversation_request_id', None)) if _result_scope_ref(value)}
    if source_task_id is not None and source_task_id not in allowed_roots:
        return 'RESULT_ARTIFACT_SCOPE_MISMATCH'
    if source_run_id is not None and source_run_id not in allowed_roots and source_task_id is None:
        return 'RESULT_ARTIFACT_SOURCE_UNPROVEN'
    parent = str(candidate.parent)
    if registry_root is not None:
        workspace = registry_root
    elif parent in dir_cache:
        workspace = dir_cache[parent]
    else:
        workspace = None
        visited: list[str] = []
        for anc in list(candidate.parents)[:24]:
            anc_str = str(anc)
            if anc_str in dir_cache:
                workspace = dir_cache[anc_str]
                break
            visited.append(anc_str)
            if anc.parent == anc:
                break
            try:
                has_registry = os.path.lexists(anc / REGISTRY_RELATIVE_PATH)
            except OSError:
                has_registry = False
            if has_registry:
                workspace = anc
                break
        for anc_str in visited:
            dir_cache[anc_str] = workspace
    # 新收口保留原host registry的source IDs，child可读自己的canonical登记根。
    # task_id可以是宿主bind_turn的根run；不能把它强行改成SessionTask编号。
    if source_task_id is not None and source_run_id is not None:
        task_root = _result_source_link_root(task, store, owner_root, source_task_id)
        if task_root is None:
            return 'RESULT_ARTIFACT_SOURCE_UNPROVEN'
        run_root = _result_source_link_root(task, store, owner_root, source_run_id)
        if run_root is not None:
            if not run_root.is_relative_to(task_root):
                return 'RESULT_ARTIFACT_SCOPE_MISMATCH'
            workspace = run_root
        elif source_run_id in allowed_roots:
            workspace = task_root
        else:
            # 显式child身份必须有原TaskLink正向印证；根登记里的同名字符串不是运行证明。
            return 'RESULT_ARTIFACT_SOURCE_UNPROVEN'
    if workspace is None:
        return 'RESULT_ARTIFACT_SOURCE_UNPROVEN'
    budget = _observation_request_data.get().registry_budget
    report = _observation_cached(('bounded_result_registry', str(workspace)), lambda: budget.read(workspace))
    if report is None:
        return budget.exhausted_reason or 'ARTIFACT_REGISTRY_READ_BUDGET_EXCEEDED'
    for row in report.errors:
        code = row.get('error_code')
        if code in {'ARTIFACT_REGISTRY_READ_BUDGET_EXCEEDED', 'ARTIFACT_REGISTRY_ROW_BUDGET_EXCEEDED',
                    'ARTIFACT_REGISTRY_DIAGNOSTIC_BUDGET_EXCEEDED'}:
            return code
    if report.errors:
        return 'ARTIFACT_REGISTRY_UNREADABLE'
    record = report.records.get(artifact_id)
    if record is None:
        return ('ARTIFACT_REGISTRY_RECORD_UNAVAILABLE' if source_task_id is not None or source_run_id is not None
                else 'RESULT_ARTIFACT_SOURCE_UNPROVEN')
    if record.path != path:
        return 'RESULT_ARTIFACT_PIN_MISMATCH'
    if record.status != 'ready':
        return 'RESULT_ARTIFACT_NOT_READY'
    if str(record.sha256 or '').lower() != pinned_sha:
        return 'RESULT_ARTIFACT_PIN_MISMATCH'
    if ((source_task_id is not None and record.task_id != source_task_id)
            or (source_run_id is not None and record.run_id != source_run_id)):
        return 'RESULT_ARTIFACT_SCOPE_MISMATCH'
    # 旧四字段不丢ID：原登记task或root-run须能由原TaskLink/显式接单turn印证。
    identifier = record.task_id or record.run_id
    if identifier not in allowed_roots:
        return 'RESULT_ARTIFACT_SCOPE_MISMATCH' if identifier else 'RESULT_ARTIFACT_SOURCE_UNPROVEN'
    task_root = _result_source_link_root(task, store, owner_root, identifier)
    if task_root is None:
        return 'RESULT_ARTIFACT_SOURCE_UNPROVEN'
    if source_run_id is None and record.run_id and record.run_id not in allowed_roots:
        # 四字段旧快照未固定child run；目录相邻不能补造原执行归属。
        return 'RESULT_ARTIFACT_SOURCE_UNPROVEN'
    if workspace != task_root and (source_run_id is None or not workspace.is_relative_to(task_root)):
        return 'RESULT_ARTIFACT_SCOPE_MISMATCH'
    # 仅在原Owner内检查正规文件存在；不为状态页读取大产物或重算payload摘要。
    from ..common.nofollow_fs import regular_file_exists_beneath

    try:
        owner = Path(owner_root).resolve(strict=False)
        parts = candidate.relative_to(owner).parts
    except (ValueError, TypeError):
        return 'RESULT_ARTIFACT_BODY_UNAVAILABLE'
    if not regular_file_exists_beneath(owner, parts):
        return 'RESULT_ARTIFACT_BODY_UNAVAILABLE'
    return None


# LLM: 公开引用只给交付项自带的 owner 内产物登记 ID（最多前100个唯一ID，保持交付路径顺序），绝不回退为绝对路径、截断内部账本或补造 ID；
#   unresolved_count与omitted_count均不设人为上限100，任一为正、版本摘要未知或可读登记簿已发生sha256漂移均记 partial，绝不把漂移/未知冒充 complete。
# 函数用途: 把任务内部结果路径有界投影成公开 result_refs（<=100）与诚实覆盖说明；无结果时覆盖说明为 None。
def _task_artifact_registry_root(task, store, owner_root) -> tuple[Path | None, bool]:
    # 登记簿属于原运行工作区；缺失/坏link都不能借目录邻居替任务签完整。旧ID仍保留。
    if store is None or owner_root is None:
        return None, False
    try:
        link, error = store.tasks.link_report(task.target_thread_id, task.task_id)
        if error:
            return None, True
        if link is None:
            return None, True
        if not link.task_path:
            return None, True
        root = Path(link.task_path).resolve(strict=False)
        owner = Path(owner_root).resolve(strict=False)
        return (root, False) if root != owner and root.is_relative_to(owner) else (None, True)
    except (OSError, ValueError, TypeError, AttributeError):
        return None, True


@observation_request()
def public_result_refs(task, *, store=None, owner_root=None):
    from .session_tasks import (
        DELIVERY_ARTIFACT_DIAGNOSTICS_KEY,
        SESSION_TASK_RESULT_ARTIFACTS_KEY,
        public_delivery_artifact_diagnostics,
    )

    diagnostics = public_delivery_artifact_diagnostics((task.metadata or {}).get(DELIVERY_ARTIFACT_DIAGNOSTICS_KEY))
    paths = task.result_refs
    if not paths:
        return [], ({'state': 'partial', 'unresolved_count': 0, 'omitted_count': 0, 'diagnostics': diagnostics}
                    if diagnostics else None)
    raw = (task.metadata or {}).get(SESSION_TASK_RESULT_ARTIFACTS_KEY)
    if raw is None:
        return [], {'state': 'legacy_unresolved', 'unresolved_count': len(paths), 'omitted_count': 0,
                    **({'diagnostics': diagnostics} if diagnostics else {})}
    if not isinstance(raw, list):
        return [], {'state': 'unreadable', 'unresolved_count': len(paths), 'omitted_count': 0,
                    **({'diagnostics': diagnostics} if diagnostics else {})}
    known: dict[str, set[str]] = {}
    pins: dict[tuple[str, str], set[str | None]] = {}
    sources, invalid_sources = {}, set()
    for item in raw:
        artifact = item.get('artifact_id') if isinstance(item, dict) else None
        if (isinstance(item, dict) and isinstance(item.get('path'), str) and isinstance(artifact, str)
                and 0 < len(artifact) <= 128 and len(artifact.encode('utf-8')) <= 128
                and '/' not in artifact and '\\' not in artifact):
            known.setdefault(item['path'], set()).add(artifact)
            key = (item['path'], artifact)
            pins.setdefault(key, set()).add(_valid_sha256_pin(item.get('sha256')))
            source = tuple(_result_scope_ref(item.get(field)) for field in ('task_id', 'run_id'))
            sources.setdefault(key, set()).add(source)
            if any(field in item and item[field] not in (None, '') and _result_scope_ref(item[field]) is None
                   for field in ('task_id', 'run_id')):
                invalid_sources.add(key)
    refs, seen, unresolved = [], set(), 0
    checked = set()
    budget_exceeded = set()
    source_issues = set()
    dir_cache: dict[str, Path | None] = {}
    registry_root, registry_unavailable = _task_artifact_registry_root(task, store, owner_root)
    pin_incomplete = registry_unavailable
    for path in paths:
        path = str(path)
        # 同一路径对应多个不同登记ID即身份不明：如实记未解析，不按元数据顺序挑选（G7-R1）；完全相同的重复项已由集合去重。
        ids = known.get(path, set())
        if len(ids) != 1:
            unresolved += 1
            continue
        artifact = next(iter(ids))
        if artifact not in seen:
            seen.add(artifact)
            if len(refs) < 100:
                refs.append(artifact)
        pin_set = pins.get((path, artifact), set())
        if registry_unavailable or len(pin_set) != 1 or None in pin_set:
            pin_incomplete = True
        elif (path, artifact) not in checked:
            if len(checked) >= 100:
                pin_incomplete = True
                continue
            checked.add((path, artifact))
            source_set = sources.get((path, artifact), set())
            if len(source_set) != 1 or (path, artifact) in invalid_sources:
                pin_incomplete = True
                source_issues.add('RESULT_ARTIFACT_SOURCE_UNPROVEN')
                continue
            source_task, source_run = next(iter(source_set))
            issue = _registry_pin_issue(path, artifact, next(iter(pin_set)), dir_cache, registry_root,
                task=task, store=store, owner_root=owner_root, source_task_id=source_task, source_run_id=source_run)
            if issue:
                pin_incomplete = True
                if issue in {'ARTIFACT_REGISTRY_READ_BUDGET_EXCEEDED', 'ARTIFACT_REGISTRY_ROW_BUDGET_EXCEEDED',
                             'ARTIFACT_REGISTRY_DIAGNOSTIC_BUDGET_EXCEEDED'}:
                    budget_exceeded.add(issue)
                if issue in {'RESULT_ARTIFACT_SOURCE_UNPROVEN', 'RESULT_ARTIFACT_SCOPE_MISMATCH',
                             'RESULT_ARTIFACT_NOT_READY', 'ARTIFACT_REGISTRY_UNREADABLE',
                             'RESULT_ARTIFACT_BODY_UNAVAILABLE', 'ARTIFACT_REGISTRY_RECORD_UNAVAILABLE'}:
                    source_issues.add(issue)
    # FLAG-7 B（MSG-029）：公开投影最多给前100个唯一ID（按交付路径稳定顺序），其余已解析ID计omitted_count；
    #   内部路径与映射完整保留；任一计数为正或版本摘要未知/漂移即记partial，不将未知或漂移混同为完整无遗漏。
    omitted = len(seen) - len(refs)
    state = 'partial' if (unresolved or omitted or pin_incomplete or diagnostics) else 'complete'
    coverage = {'state': state, 'unresolved_count': unresolved, 'omitted_count': omitted}
    for code in sorted(budget_exceeded):
        if len(diagnostics) >= 8:
            break
        diagnostics.append({'error_code': code, 'coverage': 'partial',
                            'stage': 'delivery_observation', 'effect_outcome': 'not_started',
                            'source_effect_outcome': 'unknown',
                            'next_action': '本次观察的登记读取或解析达到预算；未覆盖内容仍未知。用artifacts.get核验原精确ID与版本。'})
    for code in sorted(source_issues):
        if len(diagnostics) >= 8:
            break
        diagnostics.append({'error_code': code, 'coverage': 'partial', 'stage': 'delivery_observation',
            'effect_outcome': 'not_started', 'source_effect_outcome': 'unknown',
            'next_action': '保留原产物ID；用artifacts.get核验原任务归属、ready状态和版本，未核清前不声明完整交付。'})
    if diagnostics:
        coverage['diagnostics'] = diagnostics
    return refs, coverage


def _coverage(values):
    if 'expired' in values:
        return 'expired'
    if all(value == 'complete' for value in values):
        return 'complete'
    return 'unknown' if all(value == 'unknown' for value in values) else 'partial'


@dataclass(frozen=True)
class ObservationRecord:
    value: dict
    owner_id: str
    space_id: str | None = None
    project_ref: str | None = None
    task_id: str | None = None
    thread_id: str | None = None
    run_id: str | None = None
    attempt_id: str | None = None
    source_cursor: int = 0


@dataclass(frozen=True)
class ObservationPage:
    records: tuple[ObservationRecord, ...] = ()
    coverage: str = 'complete'
    revision: str = ''
    source_paged: bool = False
    next_position: dict | None = None


@dataclass(frozen=True)
class ObservationSourcePage:
    """宿主验证后的源分页参数；不属于公开请求Schema。"""
    position: dict | None = None
    limit: int = 50

    def __post_init__(self):
        if type(self.limit) is not int or not 1 <= self.limit <= 100 or (self.position is not None and type(self.position) is not dict):
            raise ValueError('invalid observation source page')


def bounded_observation_reader(read):
    """宿主显式登记支持源分页的reader，旧自定义reader参数合同保持不变。"""
    read.observation_source_paging_version = 1
    return read


def runtime_source_revision(path):
    """主库及WAL版本约束同一次读窗，不创建来源或扫描历史。"""
    values = []
    for suffix in ('', '-wal'):
        try:
            info = Path(str(path) + suffix).stat()
        except FileNotFoundError:
            values.append(None)
        else:
            if suffix == '-wal' and info.st_size == 0:
                values.append(None)
                continue
            values.append((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
    return _digest(values)


def source_after(request, revision):
    if request is None or request.position is None:
        return 0
    position = request.position
    if (type(position) is not dict or set(position) != {'revision', 'after'} or position.get('revision') != revision
            or type(position.get('after')) is not int or position['after'] < 0):
        raise BusinessError('CURSOR_EXPIRED')
    return position['after']


class BusinessObservationService:
    # LLM: 观察只借原mode=ro连接及其有界busy等待，不复用C短写的existing_only零等待模式；不建库或迁移。
    # 函数用途: 绑定同owner原事实源，让并发事件写入的短锁不会中断查询与SSE复核。
    def __init__(self, owner_root, *, owner_id, store=None, runtime=None, readers=None, cursor_key=None, scope_provider=None):
        self.owner_id = owner_id
        self.owner_root, self.store = Path(owner_root).absolute(), store
        self.runtime = RuntimeRepository(runtime.db_path, read_only=True) if runtime is not None else None
        self.readers = dict(readers or {})
        self.scope_provider = scope_provider
        self._cursor_key = cursor_key if cursor_key is not None else os.urandom(32)
        if type(self._cursor_key) is not bytes or len(self._cursor_key) < 32:
            raise ValueError('cursor key requires 32 host bytes')

    def authorize_call(self, action, params, *, actor):
        require_actor(actor, self)
        filters = params.get('filter', {})
        require_access(actor, action, service=self, **filters,
            **{key: value for key, value in params.items() if key not in {'filter', 'cursor', 'limit'}})

    def scope_digest(self, action, params, actor):
        return _digest([self.owner_id, actor.subject_id, action,
            self.scope_provider(actor) if self.scope_provider is not None else None,
            {key: value for key, value in params.items() if key not in {'cursor', 'limit'}}])

    def cursor(self, scope, position):
        raw = json.dumps({'scope': scope, 'position': position, 'expires': time.time() + 3600}, separators=(',', ':')).encode()
        token = base64.urlsafe_b64encode(raw + hmac.digest(self._cursor_key, raw, 'sha256')).decode().rstrip('=')
        if len(token) > 2048:
            raise BusinessError('CURSOR_EXPIRED')
        return token

    def cursor_position(self, cursor, scope):
        if cursor is None:
            return None
        try:
            if type(cursor) is not str or not 1 <= len(cursor) <= 2048:
                raise ValueError('cursor shape')
            value = base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True)
            raw, signature = value[:-32], value[-32:]
            if not hmac.compare_digest(signature, hmac.digest(self._cursor_key, raw, 'sha256')):
                raise ValueError('cursor signature')
            state = json.loads(raw)
            if state['scope'] != scope:
                raise BusinessError('CURSOR_EXPIRED', details={'reason': 'scope_changed'})
            if state['expires'] <= time.time():
                raise BusinessError('CURSOR_EXPIRED', details={'reason': 'expired'})
            return state['position']
        except BusinessError:
            raise
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise BusinessError('CURSOR_EXPIRED', details={'reason': 'invalid_cursor'}) from None

    def _visible(self, record, action, params, actor):
        if not isinstance(record, ObservationRecord) or record.owner_id != self.owner_id:
            return False
        facts = {key: getattr(record, key) for key in ('space_id', 'project_ref', 'task_id', 'thread_id', 'run_id', 'attempt_id')}
        filters = params.get('filter', {})
        for key in ('space_id', 'project_ref', 'task_id', 'run_id', 'attempt_id'):
            selected = filters.get(key, params.get(key))
            if selected is not None and facts[key] != selected:
                return False
        if filters.get('agent_id') is not None and record.thread_id != filters['agent_id']:
            return False
        if filters.get('workflow_run_id') is not None and record.value.get('workflow_run_id') != filters['workflow_run_id']:
            return False
        for key in ('since', 'until'):
            if filters.get(key):
                stamp = record.value.get('occurred_at') or record.value.get('started_at')
                if stamp is None or (key == 'since' and stamp < filters[key]) or (key == 'until' and stamp > filters[key]):
                    return False
        if filters.get('include_completed') is False and record.value.get('status') in {'done', 'failed', 'cancelled', 'succeeded'}:
            return False
        return actor.authorize(action, {'owner_id': self.owner_id, **facts}) is True

    def _source(self, name, params, *, source_page=None):
        try:
            if name in self.readers:
                reader = self.readers[name]
                page = (reader(_copy(params), source_page=source_page) if source_page is not None
                        and getattr(reader, 'observation_source_paging_version', None) == 1 else reader(_copy(params)))
            elif name == 'tasks':
                page = self._tasks(params, source_page=source_page)
            elif name == 'executions':
                page = self._executions(params, source_page=source_page)
            else:
                return ObservationPage(coverage='unknown')
            if (not isinstance(page, ObservationPage) or page.coverage not in {'complete', 'partial', 'expired', 'unknown'}
                    or any(not isinstance(row, ObservationRecord) for row in page.records)
                    or type(page.source_paged) is not bool or type(page.revision) is not str
                    or (page.next_position is not None and type(page.next_position) is not dict)):
                raise BusinessError('OBSERVATION_SOURCE_INVALID')
            return page
        except BusinessError as error:
            if isinstance(error, ObservationSourceError) or error.code == 'CURSOR_EXPIRED':
                raise
            return ObservationPage(coverage='unknown')
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RuntimeDatabaseDeferred):
            # 缺来源、坏账和短暂读不到不冒充空的完整查询；公开响应不携底层路径。
            return ObservationPage(coverage='unknown')

    def _paged_source(self, name, action, params, actor):
        native = name in {'tasks', 'executions'} and name not in self.readers
        reader = self.readers.get(name)
        if not native and getattr(reader, 'observation_source_paging_version', None) != 1:
            return self._source(name, params)
        scope = self.scope_digest(action, params, actor)
        position = self.cursor_position(params.get('cursor'), scope)
        request = ObservationSourcePage(position, params.get('limit', 50))
        return self._source(name, params, source_page=request)

    def _paginate(self, records, page, action, params, actor):
        scope = self.scope_digest(action, params, actor)
        if page.source_paged:
            chosen = records[:params.get('limit', 50)]
            cursor = self.cursor(scope, page.next_position) if page.next_position is not None else None
            return chosen, cursor
        position = self.cursor_position(params.get('cursor'), scope)
        revision = page.revision or _digest([row.value for row in records])
        offset = 0
        if position is not None:
            if position.get('revision') != revision or type(position.get('offset')) is not int or position['offset'] < 0:
                raise BusinessError('CURSOR_EXPIRED')
            offset = position['offset']
        limit = params.get('limit', 50)
        chosen = records[offset:offset + limit]
        next_cursor = self.cursor(scope, {'revision': revision, 'offset': offset + limit}) if offset + limit < len(records) else None
        return chosen, next_cursor

    # LLM: 直接观察调用也有独立作用域；已在业务调用内时共享数据但仍逐次活授权。
    # 函数用途: 验证并分派一次原观察查询。
    @observation_request()
    @artifact_query_access
    def query(self, action: str, params: dict, *, actor: object) -> dict:
        if action not in OBSERVATION_ACTIONS:
            raise BusinessError('UNSUPPORTED_ACTION')
        params = validate_params(action, params)
        self.authorize_call(action, params, actor=actor)
        if action == 'events.list':
            result = self._events(params, actor)
        elif action in self.readers:
            result = self._detail_port(action, params, actor)
        elif action in {'subagents.list', 'subagents.get'}:
            result = self._subagents(action, params, actor)
        elif action == 'tasks.get':
            result = self._task_detail(params, actor)
        elif action == 'messages.list':
            result = self._messages(params, actor)
        elif action == 'agent_tree.get':
            result = self._tree(params, actor)
        elif action == 'usage.get':
            result = self._usage(params, actor)
        else:
            routes = {'tasks.list': ('tasks', 'TaskView'), 'executions.list': ('executions', 'ExecutionView'),
                'tool_calls.list': ('tool_calls', 'ToolCallView'), 'artifacts.list': ('artifacts', 'ArtifactView'),
                'approvals.list': ('approvals', 'ApprovalView')}
            if action in routes:
                source, kind = routes[action]
                page = self._paged_source(source, action, params, actor)
                records = [row for row in page.records if self._visible(row, action, params, actor)]
                chosen, cursor = self._paginate(records, page, action, params, actor)
                items = [_view(kind, row.value) for row in chosen]
                if action == 'tool_calls.list':
                    from ..tooling.result_display import tool_invocation_summary

                    for target, row in zip(items, chosen):
                        operation = target['operation_id']
                        detail_params = {'operation_id': operation}
                        if (actor.authorize('tool_calls.get', {'owner_id': self.owner_id, **detail_params}) is True
                                and self._visible(row, 'tool_calls.get', detail_params, actor)):
                            label = tool_invocation_summary(target['tool_name'], row.value.get('_input'))
                            if label:
                                target['invocation'] = label
                result = {'items': items, 'next_cursor': cursor,
                    'coverage': 'partial' if cursor and page.coverage == 'complete' else page.coverage}
            elif action == 'executions.get':
                page = self._paged_source('executions', action, params, actor)
                rows = [row for row in page.records if self._visible(row, action, params, actor)
                        and (params.get('attempt_id') is not None or row.value.get('_is_current') is True)]
                if len(rows) != 1:
                    raise BusinessError('OBSERVATION_NOT_FOUND' if page.coverage == 'complete' else 'HOST_NOT_READY')
                execution = _view('ExecutionView', rows[0].value)
                result = {'execution': execution, 'actual_outcome': execution['effect_outcome'],
                          'retry_refs': execution['retry_refs'], 'allowed_actions': []}
                if self.runtime is not None and 'executions' not in self.readers:
                    from .business_execution_observation import execution_lifecycle
                    result['lifecycle'], error, retries = execution_lifecycle(self, execution)
                    if error is not None:
                        execution['error'] = error
                    result['retry_refs'] = execution['retry_refs'] = retries
            else:
                raise BusinessError('HOST_NOT_READY')
        self.authorize_call(action, params, actor=actor)
        return validate_result(action, result)

    def _detail_port(self, action, params, actor):
        """宿主原读口保留自己的源游标/retention证据；闭合schema拒绝混入内部字段。"""
        page = self._source(action, params)
        rows = [row for row in page.records if self._visible(row, action, params, actor)]
        if len(rows) != 1:
            details = {}
            if action in {'logs.read', 'tool_calls.get'}:
                details = {'field_path': '$.params.source_ref' if action == 'logs.read' else '$.params.operation_id',
                    'reason': '当前授权范围没有此观察记录，缺记录不能证明调用成功、失败或尚未执行。',
                    'next_action': '先在当前授权范围调用 tool_calls.list，使用其原 operation_id 读取 tool_calls.get；日志再使用详情的 data.result_ref，不猜编号或追加后缀。',
                    'retry_same_request': False}
            raise BusinessError('OBSERVATION_NOT_FOUND' if page.coverage == 'complete' else 'HOST_NOT_READY', details=details)
        result = redact_sensitive_value(_copy(rows[0].value))
        if page.coverage != 'complete':
            if 'coverage' not in result:
                raise BusinessError('HOST_NOT_READY')
            result['coverage'] = _coverage((page.coverage, result['coverage']))
        return result

    # LLM: 使用本请求列表已读的canonical任务，不重复读取或缓存访问裁决。
    # 函数用途: 返回已授权任务详情和原关联。
    def _task_detail(self, params, actor):
        page = self._source('tasks', params)
        rows = [row for row in page.records if self._visible(row, 'tasks.get', params, actor)]
        if len(rows) != 1:
            raise BusinessError('OBSERVATION_NOT_FOUND' if page.coverage == 'complete' else 'HOST_NOT_READY')
        task = _observation_task(self.store, params['task_id'])
        if task is None:
            raise BusinessError('OBSERVATION_NOT_FOUND')
        workflow = task.metadata.get('workflow_ref')
        # 普通规范任务没有Workflow关联；保留null，不捏造引用或修改任务元数据。
        refs, ref_coverage = public_result_refs(task, store=self.store, owner_root=self.owner_root)
        detail = {'task': _view('TaskView', rows[0].value), 'assignments': [{
            'assignment_ref': task.task_id, 'task_id': task.task_id, 'sender_thread_id': task.sender_thread_id,
            'target_thread_id': task.target_thread_id, 'status': task.status, 'result_refs': list(refs)}],
            'workflow_ref': workflow, 'result_refs': list(refs), 'allowed_actions': [],
            'scheduling': task_scheduling_view(self.store, task,
                running=bool(rows[0].value.get('current_run_id')), runtime=self.runtime, owner_id=self.owner_id)}
        if rows[0].value.get('_execution_error') and task.status not in SESSION_TASK_TERMINAL_STATUSES:
            detail['scheduling'].update(state=('waiting_child' if detail['scheduling']['state'] == 'waiting_child' else 'unknown'), coverage='partial',
                                        reason_code=rows[0].value['_execution_error'])
        if ref_coverage is not None:
            detail['result_ref_coverage'] = ref_coverage
        result = task_result_view(self.store, task)
        if result is not None:
            detail['result'] = result
        return detail

    # LLM: 同一线程/任务在本页只读一次，消息行仍逐条活授权且沿原归档边界。
    # 函数用途: 返回有界规范消息页及真实历史边界。
    def _messages(self, params, actor):
        scope = self.scope_digest('messages.list', params, actor)
        position = self.cursor_position(params.get('cursor'), scope)
        target = params['target']
        from .subagent_message_observation import (
            SubagentTranscriptUnavailable,
            public_message_origin,
            resolve_subagent_transcript,
        )
        if self.store is None:
            raise BusinessError('HOST_NOT_READY')
        child = None
        if target['kind'] == 'subagent':
            try:
                thread, child = resolve_subagent_transcript(self, target['id'], actor)
            except SubagentTranscriptUnavailable as error:
                if position is not None:
                    raise BusinessError('CURSOR_EXPIRED') from error
                return {'items': [], 'next_cursor': None, 'coverage': 'unknown', 'history_boundary': {
                    'first_message_id': None, 'last_message_id': None, 'coverage': 'unknown', 'reason_code': str(error)}}
        else:
            thread = _observation_thread(self.store, target['id'])
        if thread.owner_id != self.owner_id:
            raise BusinessError('BUSINESS_ACCESS_DENIED')
        identity = background_identity(thread)
        if target['kind'] == 'controller' and thread.metadata.get('business_controller') is not True:
            raise BusinessError('OBSERVATION_NOT_FOUND')
        if target['kind'] == 'background_main' and identity is None:
            raise BusinessError('OBSERVATION_NOT_FOUND')
        path = self.store.storage.message_path(thread.thread_id)
        info = path.stat() if path.exists() else None
        generation = _digest([info.st_dev, info.st_ino, child.attempt_id if child else None]) if info is not None else 'unavailable'
        if position is not None and position.get('generation') != generation:
            raise BusinessError('CURSOR_EXPIRED')
        facts = ObservationRecord({}, self.owner_id, child.space_id if child else identity.space_id if identity else None,
            child.project_ref if child else identity.project_ref if identity else None,
            child.task_id if child else None, thread_id=thread.thread_id)
        require_access(actor, 'messages.list', service=self, owner_id=facts.owner_id,
            space_id=facts.space_id, project_ref=facts.project_ref, thread_id=facts.thread_id)
        before, offset, bound_limit = None, 0, params.get('limit', 50)
        if position is not None:
            before, offset, bound_limit = position.get('before'), position.get('offset'), position.get('bound_limit')
            if position.get('order') != 'older_pages_v2':
                raise BusinessError('CURSOR_EXPIRED', details={'reason': 'MESSAGE_CURSOR_ORDER_CHANGED',
                    'side_effect_state': 'none', 'next_action': '此游标来自旧分页方向；不再续用它，重新读取当前目标的首个历史页。'})
            if (type(before) is not int or before < 0
                    or type(offset) is not int or offset < 0
                    or type(bound_limit) is not int or not 1 <= bound_limit <= 100):
                raise BusinessError('CURSOR_EXPIRED')
        page = self.store.messages.history_page_report(thread.thread_id, before=before, limit=bound_limit)
        if page.errors:
            if child and position is None:
                return {'items': [], 'next_cursor': None, 'coverage': 'unknown', 'history_boundary': {
                    'first_message_id': None, 'last_message_id': None, 'coverage': 'unknown',
                    'reason_code': 'SUBAGENT_TRANSCRIPT_UNREADABLE'}}
            raise BusinessError('CURSOR_EXPIRED' if position else 'HOST_NOT_READY', details={
                'reason': 'CANONICAL_HISTORY_UNREADABLE', 'side_effect_state': 'none',
                'error_code': page.errors[0].get('error_code', 'CLIENT_HISTORY_LOAD_FAILED'),
                'error_type': page.errors[0].get('error_type', 'UNKNOWN_SOURCE_ERROR'),
                'error_count': len(page.errors),
                'next_action': '保留原历史记录，核对或修复源记录/存储可读性后重新读取；不要将此故障当作空历史。'})
        if offset > len(page.rows):
            raise BusinessError('CURSOR_EXPIRED')
        # next_cursor 始终表示更早记录；合页工作片内部也从尾向前切，页内保原canonical顺序。
        end = len(page.rows) - offset
        selected = page.rows[max(0, end - params.get('limit', 50)):end]
        items, display_events = [], []
        execution_links = _message_execution_links(self, selected, thread, actor)
        filtered = False
        for row in selected:
            if child and row.metadata.get('agent_run_id') != child.run_id:
                filtered = True
                continue
            if child and row.role not in {'user', 'assistant', 'host', 'display'}:
                continue
            if row.role not in {'user', 'assistant', 'tool', 'host', 'display'}:
                continue
            if row.role == 'display' and params.get('include_display') is not True:
                continue
            task_id, context = row.metadata.get('task_id'), None
            task = _observation_task(self.store, task_id) if task_id else None
            if task and task.metadata.get('business_context'):
                context = BusinessExecutionContext.from_dict(task.metadata['business_context'])
                if context.owner_id != self.owner_id or context.task_id != task.task_id:
                    continue
            message_facts = ObservationRecord({}, self.owner_id, context.space_id if context else facts.space_id,
                context.project_ref if context else facts.project_ref, task.task_id if task else facts.task_id, thread.thread_id)
            if not self._visible(message_facts, 'messages.list', params, actor):
                continue
            if row.role == 'display':
                if params.get('include_display') is True:
                    from .background_transcript import public_workflow_step_display
                    from .display_checkpoint import display_checkpoint_event

                    event = display_checkpoint_event(row)
                    step_display = (public_workflow_step_display(event['payload'].get('workflow_step'))
                        if event and event['kind'] == 'system_message' and event['phase'] == 'completed' else None)
                    if event and event['phase'] == 'completed' and (event['kind'] == 'assistant_completed' or step_display):
                        payload = event['payload']
                        text = _public_history_text(step_display['text'] if step_display else payload.get('text'),
                                                    label='公开过程块')
                        public = {**step_display, 'text': text} if step_display else {'text': text}
                        canonical = payload.get('canonical_message_id')
                        if not step_display and isinstance(canonical, str) and 0 < len(canonical.encode('utf-8')) <= 128:
                            public['canonical_message_id'] = canonical
                        display_events.append({key: event[key] for key in ('schema', 'kind', 'phase', 'request_id', 'block_id')}
                            | {'payload': public, 'source_message_id': row.message_id, 'occurred_at': _timestamp(row.created_at)}
                            | ({'thread_id': event['thread_id'], 'task_id': event['task_id'] or None} if step_display else {}))
                continue
            text = _public_history_text(row.content, label='单条历史消息')
            items.append({'message_id': row.message_id, 'thread_id': thread.thread_id,
                'task_id': message_facts.task_id, 'space_id': message_facts.space_id, 'role': row.role,
                'text': text, 'occurred_at': _timestamp(row.created_at),
                'source': public_message_origin(self, row, actor), 'artifact_refs': [],
                **execution_links[row.message_id]})
        offset += len(selected)
        next_position = ({'before': page.after, 'offset': offset, 'bound_limit': bound_limit} if offset < len(page.rows)
            else {'before': page.before, 'offset': 0, 'bound_limit': bound_limit} if page.before else None)
        if next_position is not None:
            next_position.update(generation=generation, order='older_pages_v2')
        coverage = 'unknown' if child and info is None else 'partial' if next_position or filtered else 'complete'
        return {**({'display_events': display_events} if params.get('include_display') is True else {}),
            'items': items, 'next_cursor': self.cursor(scope, next_position) if next_position else None,
            'coverage': coverage, 'history_boundary': {
                'first_message_id': items[0]['message_id'] if items else None,
                'last_message_id': items[-1]['message_id'] if items else None,
                'coverage': coverage, 'reason_code': ('SUBAGENT_TRANSCRIPT_NOT_RECORDED' if child and info is None
                    else 'CANONICAL_HISTORY_NOT_RECORDED' if info is None
                    else 'CANONICAL_HISTORY_EMPTY' if not page.rows
                    else 'PUBLIC_ONLY_FILTERED' if filtered
                    else 'NO_PUBLIC_MESSAGES_IN_PAGE' if not items else 'CANONICAL_PAGE')}}

    def _subagents(self, action, params, actor):
        page = self._paged_source('executions', action, params, actor)
        records = []
        for record in page.records:
            value = record.value
            if not value.get('_is_current') or not value.get('_parent_run_id') or not self._visible(record, action, params, actor):
                continue
            if params.get('parent_run_id') is not None and value['_parent_run_id'] != params['parent_run_id']:
                continue
            if params.get('include_completed') is False and value['status'] in {'done', 'failed', 'cancelled'}:
                continue
            records.append(record)
        if action == 'subagents.get' and len(records) != 1:
            raise BusinessError('OBSERVATION_NOT_FOUND' if page.coverage == 'complete' else 'HOST_NOT_READY')
        chosen, cursor = self._paginate(records, page, action, params, actor)
        items = [{'run_id': row.run_id, 'parent_run_id': row.value['_parent_run_id'], 'thread_id': row.thread_id,
            'task_id': row.task_id, 'space_id': row.space_id, 'status': row.value['status'],
            'model_profile_id': None, 'result_refs': []} for row in chosen]
        if action == 'subagents.list':
            return {'items': items, 'next_cursor': cursor, 'coverage': page.coverage}
        if not items[0]['thread_id']:
            raise BusinessError('HOST_NOT_READY')
        return {'subagent': items[0], 'thread_ref': items[0]['thread_id'],
            'children_ref': items[0]['run_id'], 'allowed_actions': []}

    # LLM: 列表已经读取的任务用于本请求点读；线程身份按distinct ID读取，不缓存授权。
    # 函数用途: 投影规范任务并保留坏上下文的partial标志。
    def _tasks(self, params=None, *, source_page=None):
        if self.store is None:
            return ObservationPage(coverage='unknown')
        params = params or {}
        selected = params.get('task_id') or params.get('filter', {}).get('task_id')
        next_position, coverage = None, 'complete'
        if selected:
            task = _observation_task(self.store, selected)
            tasks, errors = ([task] if task is not None else []), []
        else:
            request = source_page or ObservationSourcePage()
            cursor = request.position.get('cursor') if request.position else None
            page = self.store.session_tasks.list_page(limit=request.limit, cursor=cursor,
                target_thread_id=params.get('filter', {}).get('agent_id'))
            if page['coverage'] == 'expired':
                raise BusinessError('CURSOR_EXPIRED')
            tasks, errors, coverage = page['items'], list(page['load_errors']), page['coverage']
            if page['next_cursor'] is not None:
                next_position = {'cursor': page['next_cursor']}
        rows = []
        for task in tasks:
            _observation_cached((self.store.session_tasks, 'record', task.task_id), lambda: task)
            target = _observation_thread(self.store, task.target_thread_id)
            sender = _observation_thread(self.store, task.sender_thread_id)
            if target.owner_id != self.owner_id or sender.owner_id != self.owner_id:
                continue
            raw = task.metadata.get('business_context')
            context = BusinessExecutionContext.from_dict(raw) if raw else None
            if context and (context.owner_id != self.owner_id or context.task_id != task.task_id):
                errors.append({'code': 'CONTEXT_MISMATCH'})
                continue
            refs, ref_coverage = public_result_refs(task, store=self.store, owner_root=self.owner_root)
            execution, execution_error = self._task_execution(task, target)
            if execution_error:
                errors.append({'code': execution_error})
            value = {'task_id': task.task_id, 'thread_id': task.target_thread_id,
                'space_id': context.space_id if context else None, 'project_ref': context.project_ref if context else None,
                'status': task.status, 'current_run_id': execution['run_id'] if execution else None,
                '_execution_error': execution_error, 'result_refs': refs,
                'revision': int(task.updated_at * 1000000)}
            if ref_coverage is not None:
                value['result_ref_coverage'] = ref_coverage
            rows.append(ObservationRecord(value, self.owner_id, value['space_id'], value['project_ref'], task.task_id, task.target_thread_id))
        return ObservationPage(tuple(rows), 'partial' if errors else coverage,
                               next_position=next_position, source_paged=not bool(selected))

    def _task_execution(self, task, thread):
        """任务只引用准确活动执行；异常保留 unknown，绝不领取或修补原租约。"""
        if task.status in SESSION_TASK_TERMINAL_STATUSES:
            return None, ''
        from .background_execution_view import ExecutionViewError, thread_execution_view
        from .run_claim import detached_task_claim_scope_id

        try:
            link, error = self.store.tasks.link_report(thread.thread_id, task.task_id)
            if error:
                return None, 'TASK_LINK_UNREADABLE'
            scope = (detached_task_claim_scope_id(thread.thread_id, task.task_id)
                     if link is not None and link.cancellation_scope == 'detached' else thread.thread_id)
            execution = _observation_cached((self.store.claims, 'execution', scope),
                lambda: thread_execution_view(self.store, self.runtime, thread, claim_scope_id=scope))
            return (execution if execution and execution['task_id'] == task.task_id else None), ''
        except ExecutionViewError as error:
            return None, error.code
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RuntimeDatabaseDeferred):
            return None, 'EXECUTION_AUTHORITY_UNAVAILABLE'


    # LLM: 列表分页按规范rowid有界批次扫描，过滤空间时跨批推进避免假空页；超出扫描预算如实保留partial。
    # 函数用途: 返回已授权规范执行页，支持空间过滤游标扫描。
    def _executions(self, params=None, *, source_page=None):
        if self.runtime is None:
            return ObservationPage(coverage='unknown')
        params = params or {}
        request = source_page or ObservationSourcePage(limit=100)
        revision = runtime_source_revision(self.runtime.db_path)
        if params.get('parent_run_id'):
            revision = _digest(['parent_run_children.v1', revision])
        after = source_after(request, revision)
        limit = min(100, request.limit)
        selected_run = params.get('run_id')
        selected_attempt = params.get('attempt_id')
        filter_space = (params.get('filter') or {}).get('space_id')
        with self.runtime._runtime_connection() as conn:
            if params.get('parent_run_id'):
                # 父执行是原run_id，不是后台线程ID；从该父的子索引翻页，不消耗陌生任务的首页。
                raw = conn.execute('SELECT ar.rowid AS _rowid,ar.run_id,ar.current_attempt_id,ar.parent_agent_run_id,'
                    'pa.run_id AS parent_run_id,t.thread_id,t.task_id AS runtime_task_id,tr.metadata_json,'
                    'aa.attempt_id,aa.status,aa.started_at,aa.ended_at,aa.metadata_json AS attempt_metadata '
                    'FROM agent_runs pa JOIN task_runs ptr ON ptr.task_run_id=pa.task_run_id '
                    'JOIN tasks pt ON pt.task_id=ptr.task_id JOIN agent_runs ar ON ar.parent_agent_run_id=pa.agent_run_id '
                    'JOIN agent_attempts aa ON aa.attempt_id=ar.current_attempt_id AND aa.agent_run_id=ar.agent_run_id '
                    'JOIN task_runs tr ON tr.task_run_id=ar.task_run_id JOIN tasks t ON t.task_id=tr.task_id '
                    'WHERE pa.run_id=? AND pt.owner_id=? AND t.owner_id=? AND ar.rowid>? ORDER BY ar.rowid LIMIT ?',
                    (params['parent_run_id'], self.owner_id, self.owner_id, after, limit + 1)).fetchall()
                if _digest(['parent_run_children.v1', runtime_source_revision(self.runtime.db_path)]) != revision:
                    raise BusinessError('CURSOR_EXPIRED')
                more = len(raw) > limit
                position = {'revision': revision, 'after': raw[limit - 1]['_rowid']} if more else None
                return self._execution_page(raw[:limit], more=more, revision=revision,
                                            source_paged=True, next_position=position)
            if selected_run is not None:
                where = 'ar.run_id=? AND t.owner_id=?'
                values = [selected_run, self.owner_id]
                if selected_attempt is not None:
                    where += ' AND aa.attempt_id=?'
                    values.append(selected_attempt)
                else:
                    where += ' AND aa.attempt_id=ar.current_attempt_id'
                raw = conn.execute('SELECT ar.run_id,ar.current_attempt_id,ar.parent_agent_run_id,pa.run_id AS parent_run_id,'
                    't.thread_id,t.task_id AS runtime_task_id,tr.metadata_json,aa.attempt_id,aa.status,aa.started_at,aa.ended_at,aa.metadata_json AS attempt_metadata '
                    'FROM agent_attempts aa JOIN agent_runs ar ON ar.agent_run_id=aa.agent_run_id '
                    'JOIN task_runs tr ON tr.task_run_id=ar.task_run_id JOIN tasks t ON t.task_id=tr.task_id '
                    'LEFT JOIN agent_runs pa ON pa.agent_run_id=ar.parent_agent_run_id WHERE ' + where +
                    ' ORDER BY aa.rowid LIMIT 2', values).fetchall()
                if runtime_source_revision(self.runtime.db_path) != revision:
                    raise BusinessError('CURSOR_EXPIRED')
                return self._execution_page(raw, more=False, revision=revision, source_paged=True)
            if not filter_space:
                positions = conn.execute('SELECT rowid FROM agent_attempts WHERE rowid>? ORDER BY rowid LIMIT ?',
                                         (after, limit + 1)).fetchall()
                more = len(positions) > limit
                positions = positions[:limit]
                next_position = {'revision': revision, 'after': positions[-1][0]} if more else None
                if not positions:
                    if runtime_source_revision(self.runtime.db_path) != revision:
                        raise BusinessError('CURSOR_EXPIRED')
                    return ObservationPage(coverage='complete', revision=revision, source_paged=True)
                where = 'aa.rowid IN (' + ','.join('?' for _ in positions) + ') AND t.owner_id=?'
                values = [row[0] for row in positions] + [self.owner_id]
                raw = conn.execute('SELECT ar.run_id,ar.current_attempt_id,ar.parent_agent_run_id,pa.run_id AS parent_run_id,'
                    't.thread_id,t.task_id AS runtime_task_id,tr.metadata_json,aa.attempt_id,aa.status,aa.started_at,aa.ended_at,aa.metadata_json AS attempt_metadata '
                    'FROM agent_attempts aa JOIN agent_runs ar ON ar.agent_run_id=aa.agent_run_id '
                    'JOIN task_runs tr ON tr.task_run_id=ar.task_run_id JOIN tasks t ON t.task_id=tr.task_id '
                    'LEFT JOIN agent_runs pa ON pa.agent_run_id=ar.parent_agent_run_id WHERE ' + where +
                    ' ORDER BY aa.rowid LIMIT ?', [*values, limit]).fetchall()
                if runtime_source_revision(self.runtime.db_path) != revision:
                    raise BusinessError('CURSOR_EXPIRED')
                return self._execution_page(raw, more=more, revision=revision, source_paged=True, next_position=next_position)

            collected_records = []
            source_incomplete = False
            max_scan = 1000
            total_scanned = 0
            hit_cap = False
            batch_size = max(50, limit)
            current_after = after
            has_more = False
            next_pos = None
            last_scanned_rowid = current_after
            more_in_batch = False
            while len(collected_records) <= limit and total_scanned < max_scan:
                positions = conn.execute('SELECT rowid FROM agent_attempts WHERE rowid>? ORDER BY rowid LIMIT ?',
                                         (current_after, batch_size + 1)).fetchall()
                if not positions:
                    more_in_batch = False
                    break
                more_in_batch = len(positions) > batch_size
                positions = positions[:batch_size]
                total_scanned += len(positions)
                last_scanned_rowid = positions[-1][0]
                current_after = last_scanned_rowid
                where = 'aa.rowid IN (' + ','.join('?' for _ in positions) + ') AND t.owner_id=?'
                values = [row[0] for row in positions] + [self.owner_id]
                raw = conn.execute('SELECT aa.rowid AS _rowid, ar.run_id,ar.current_attempt_id,ar.parent_agent_run_id,pa.run_id AS parent_run_id,'
                    't.thread_id,t.task_id AS runtime_task_id,tr.metadata_json,aa.attempt_id,aa.status,aa.started_at,aa.ended_at,aa.metadata_json AS attempt_metadata '
                    'FROM agent_attempts aa JOIN agent_runs ar ON ar.agent_run_id=aa.agent_run_id '
                    'JOIN task_runs tr ON tr.task_run_id=ar.task_run_id JOIN tasks t ON t.task_id=tr.task_id '
                    'LEFT JOIN agent_runs pa ON pa.agent_run_id=ar.parent_agent_run_id WHERE ' + where +
                    ' ORDER BY aa.rowid LIMIT ?', [*values, batch_size]).fetchall()
                batch_page = self._execution_page(raw)
                source_incomplete = source_incomplete or batch_page.coverage != 'complete'
                for rec in batch_page.records:
                    if rec.space_id != filter_space:
                        continue
                    collected_records.append(rec)
                    if len(collected_records) > limit:
                        has_more = True
                        next_pos = {'revision': revision, 'after': collected_records[limit - 1].source_cursor}
                        break
                if has_more:
                    break
                if not more_in_batch:
                    break
            if not has_more:
                if total_scanned >= max_scan and more_in_batch:
                    hit_cap = True
                    has_more = True
                    next_pos = {'revision': revision, 'after': last_scanned_rowid}
                else:
                    has_more = False
                    next_pos = None
        if runtime_source_revision(self.runtime.db_path) != revision:
            raise BusinessError('CURSOR_EXPIRED')
        cov = 'partial' if source_incomplete or hit_cap or (has_more and not collected_records) else 'complete'
        return ObservationPage(tuple(collected_records[:limit]), coverage=cov,
                               revision=revision, source_paged=True, next_position=next_pos if has_more else None)

    def _event_execution_page(self, conn, events):
        """事件只查本页显式attempt；与事件行共用读事务，不借历史页版本拒绝无关append。"""
        identifiers = tuple(dict.fromkeys(row['attempt_id'] for row in events if row['attempt_id']))
        if len(identifiers) > 100:
            raise BusinessError('OBSERVATION_SOURCE_INVALID')
        if not identifiers:
            return ObservationPage()
        marks = ','.join('?' for _ in identifiers)
        raw = conn.execute('SELECT ar.run_id,ar.current_attempt_id,ar.parent_agent_run_id,pa.run_id AS parent_run_id,'
            't.thread_id,t.task_id AS runtime_task_id,tr.metadata_json,aa.attempt_id,aa.status,aa.started_at,aa.ended_at,aa.metadata_json AS attempt_metadata '
            'FROM agent_attempts aa JOIN agent_runs ar ON ar.agent_run_id=aa.agent_run_id '
            'JOIN task_runs tr ON tr.task_run_id=ar.task_run_id JOIN tasks t ON t.task_id=tr.task_id '
            'LEFT JOIN agent_runs pa ON pa.agent_run_id=ar.parent_agent_run_id AND pa.task_run_id=ar.task_run_id '
            'WHERE aa.attempt_id IN (' + marks + ') AND t.owner_id=? ORDER BY aa.rowid',
            [*identifiers, self.owner_id]).fetchall()
        return self._execution_page(raw)

    # LLM: 列表分页及事件点查共用原字段/owner/context校验，来源选择不改变公开执行投影。
    # 函数用途: 投影规范执行页，未绑定线程的内部记录如实忽略或解析并保持partial标记。
    def _execution_page(self, raw, *, more=False, revision='', source_paged=False, next_position=None):
        """列表分页及事件点查共用原字段/owner/context校验，来源选择不改变公开执行投影。"""
        rows, incomplete = [], False
        for row in raw:
            metadata = json.loads(row['metadata_json'])
            context = BusinessExecutionContext.from_dict(metadata['business_context']) if metadata.get('business_context') else None
            if context and (context.owner_id != self.owner_id or metadata.get('session_task_ref') != context.task_id):
                incomplete = True
                continue
            facts = json.loads(row['attempt_metadata'])
            outcome = facts.get('effect_outcome', 'running' if row['status'] == 'running' else 'unknown')
            if outcome not in {'none', 'not_started', 'committed', 'running', 'unknown'}:
                outcome = 'unknown'
            thread_id = row['thread_id']
            if not thread_id:
                if context and context.task_id and self.store is not None:
                    session_task = _observation_task(self.store, context.task_id)
                    if session_task and session_task.target_thread_id:
                        thread_id = session_task.target_thread_id
                if not thread_id and metadata.get('session_task_ref') and self.store is not None:
                    session_task = _observation_task(self.store, metadata['session_task_ref'])
                    if session_task and session_task.target_thread_id:
                        thread_id = session_task.target_thread_id
                if not thread_id:
                    incomplete = True
                    continue
            # 无 SessionTask 的原生后台消息仍有 canonical runtime task/thread；空间只从同 owner 的真实后台身份读取。
            identity = None
            if context is None and self.store is not None:
                try:
                    thread = _observation_thread(self.store, thread_id)
                except (KeyError, OSError, ValueError, TypeError):
                    # 缺失/损坏线程不授予空间归属，也不吞掉本页其他已核验执行。
                    incomplete = True
                    continue
                if thread is None or thread.owner_id != self.owner_id:
                    incomplete = True
                    continue
                identity = background_identity(thread)
            space_id = context.space_id if context else identity.space_id if identity else None
            project_ref = context.project_ref if context else identity.project_ref if identity else None
            value = {'run_id': row['run_id'], 'attempt_id': row['attempt_id'],
                'task_id': context.task_id if context else row['runtime_task_id'],
                'thread_id': thread_id, 'space_id': space_id, 'status': row['status'],
                'effect_outcome': outcome, 'started_at': _timestamp(row['started_at']), 'ended_at': _timestamp(row['ended_at']),
                'error': None, 'retry_refs': [], '_is_current': row['current_attempt_id'] == row['attempt_id'],
                '_parent_run_id': row['parent_run_id'] or None}
            rowid = row['_rowid'] if '_rowid' in row.keys() else 0
            rows.append(ObservationRecord(value, self.owner_id, value['space_id'], project_ref,
                value['task_id'], value['thread_id'], value['run_id'], value['attempt_id'], source_cursor=rowid))
        return ObservationPage(tuple(rows), 'partial' if incomplete or more else 'complete',
                               revision, source_paged, next_position)

    def _tree_threads(self, params, executions):
        """复用后台身份有界索引，并点读本页执行和管理关系引用的线程。"""
        if self.store is None:
            return (), 'unknown', False
        from .background_agents import background_agent_index
        from .models import ConversationThread

        page = background_agent_index(self.store).page(limit=100, filter_key=self.owner_id)
        threads = {value['thread_id']: ConversationThread.from_dict(value) for value in page['items']}
        incomplete = bool(page['load_errors']) or page['coverage'] != 'complete'
        requested = {row.thread_id for row in executions if row.thread_id}
        if params.get('root_agent_id'):
            requested.add(params['root_agent_id'])
        for identifier in requested:
            try:
                thread = _observation_thread(self.store, identifier)
            except KeyError:
                continue  # root_agent_id 也允许精确 run_id，不要求存在同名线程。
            except (OSError, ValueError, TypeError):
                incomplete = True
                continue
            if thread.owner_id == self.owner_id:
                threads[identifier] = thread
        managers = {identity.managed_by_thread_id for thread in threads.values()
                    if (identity := background_identity(thread)) is not None}
        for identifier in managers - threads.keys():
            try:
                thread = _observation_thread(self.store, identifier)
            except (KeyError, OSError, ValueError, TypeError):
                incomplete = True
                continue
            if thread.owner_id == self.owner_id:
                threads[identifier] = thread
        more = page['next_cursor'] is not None
        return threads.values(), 'partial' if incomplete or more else 'complete', more

    # LLM: 线程原对象与其它本请求观察共用读取，节点/边仍逐条授权，不跨请求保存。
    # 函数用途: 从规范执行和线程生成准确范围的观察树。
    def _tree(self, params, actor):
        execution_params = {**params, 'filter': {**params.get('filter', {}), 'space_id': params['space_id']}}
        execution_page = self._source('executions', execution_params)
        execution_coverage = ('partial' if execution_page.next_position is not None
                              else execution_page.coverage)
        executions = [row for row in execution_page.records if row.value.get('_is_current')
                      and row.space_id == params['space_id']
                      and self._visible(row, 'agent_tree.get', params, actor)]
        records, edges, controllers = {}, [], {}
        threads, thread_coverage, more_threads = self._tree_threads(params, executions)
        if self.store is not None:
            for thread in threads:
                _observation_cached((self.store.threads, 'record', thread.thread_id), lambda: thread)
                if thread.owner_id != self.owner_id:
                    continue
                identity = background_identity(thread)
                if thread.metadata.get('business_controller') is True:
                    controllers[thread.thread_id] = thread
                if identity is None or identity.space_id != params['space_id']:
                    continue
                value = {'kind': 'background_main', 'id': thread.thread_id, 'thread_id': thread.thread_id,
                    'parent_run_id': None, 'managed_by_thread_id': identity.managed_by_thread_id,
                    'space_id': identity.space_id, 'task_id': None, 'run_id': None,
                    'model_profile_id': thread.model_profile_id or None, 'status': identity.identity_state,
                    'has_children': False}
                record = ObservationRecord(value, self.owner_id, identity.space_id, identity.project_ref, thread_id=thread.thread_id)
                if self._visible(record, 'agent_tree.get', params, actor):
                    records[thread.thread_id] = record
        controller_refs = {row.thread_id for row in executions}
        controller_refs.update(row.value['managed_by_thread_id'] for row in records.values())
        controller_refs.add(params.get('root_agent_id'))
        for identifier, thread in controllers.items():
            if identifier in controller_refs:
                value = {'kind': 'controller', 'id': identifier, 'thread_id': identifier, 'parent_run_id': None,
                    'managed_by_thread_id': None, 'space_id': None, 'task_id': None, 'run_id': None,
                    'model_profile_id': thread.model_profile_id or None, 'status': thread.status, 'has_children': False}
                control = ObservationRecord(value, self.owner_id, params['space_id'], thread_id=identifier)
                if self._visible(control, 'agent_tree.get', params, actor):
                    records[identifier] = control
        for row in list(records.values()):
            identifier = row.value['managed_by_thread_id']
            if identifier in records:
                edges.append({'from_id': identifier, 'to_id': row.value['id'], 'relation': 'manages'})
        for row in executions:
            value = {'kind': 'subagent' if row.value.get('_parent_run_id') else 'background_main',
                'id': row.run_id, 'thread_id': row.thread_id, 'parent_run_id': row.value.get('_parent_run_id'),
                'managed_by_thread_id': None, 'space_id': row.space_id, 'task_id': row.task_id,
                'run_id': row.run_id, 'model_profile_id': None, 'status': row.value['status'], 'has_children': False}
            records[row.run_id] = ObservationRecord(value, row.owner_id, row.space_id, row.project_ref,
                row.task_id, row.thread_id, row.run_id, row.attempt_id)
        for row in executions:
            parent = row.value.get('_parent_run_id')
            source, relation = (parent, 'child_of') if parent else (row.thread_id, 'executes')
            if source in records:
                edges.append({'from_id': source, 'to_id': row.run_id, 'relation': relation})
        if params.get('include_completed') is False:
            records = {key: value for key, value in records.items()
                       if value.value['status'] not in {'done', 'failed', 'cancelled', 'succeeded'}}
        edges = [edge for edge in edges if edge['from_id'] in records and edge['to_id'] in records]
        children = {identifier: [] for identifier in records}
        for edge in edges:
            children[edge['from_id']].append(edge['to_id'])
        for identifier, record in records.items():
            record.value['has_children'] = bool(children[identifier])
        root = params.get('root_agent_id')
        if root is not None and root not in records:
            complete = execution_coverage == thread_coverage == 'complete'
            raise BusinessError('OBSERVATION_NOT_FOUND' if complete else 'HOST_NOT_READY', details={
                'reason_code': 'TREE_ROOT_NOT_FOUND' if complete else 'TREE_ROOT_OUTSIDE_OBSERVED_WINDOW',
                'root_agent_id': root,
                'execution_coverage': execution_coverage,
                'thread_coverage': thread_coverage,
                'next_action': '核对当前空间及根节点 ID。来源未读全时可用原执行分页或准确运行详情继续查询。',
            })
        roots = [root] if root else sorted(set(records) - {edge['to_id'] for edge in edges})
        frontier, selected, seen, has_more = deque((item, 0) for item in roots), [], set(), False
        depth = params.get('max_depth', 20)
        while frontier:
            identifier, level = frontier.popleft()
            if identifier in seen:
                continue
            seen.add(identifier)
            selected.append(records[identifier])
            if level < depth:
                frontier.extend((child, level + 1) for child in sorted(children[identifier]))
            elif children[identifier]:
                has_more = True
        page = ObservationPage(tuple(selected), _coverage((thread_coverage, execution_coverage)))
        chosen, cursor = self._paginate(selected, page, 'agent_tree.get', params, actor)
        ids = {record.value['id'] for record in chosen}
        return {'nodes': [_view('NodeView', row.value) for row in chosen],
            'edges': [edge for edge in edges if edge['to_id'] in ids], 'next_cursor': cursor,
            'coverage': _coverage((page.coverage, 'partial' if has_more or cursor else 'complete')),
            'has_more_descendants': has_more or more_threads or cursor is not None or execution_page.next_position is not None}

    # LLM: 内部非catalog端口不放宽usage授权；每组及其所有成员仍先核与末核，缓存不含裁决。
    # 函数用途: 对单窗口的多个准确子范围读取用量，可将已整窗授权的事件供模型次数聚合复用。
    @observation_request()
    def usage_groups(self, params, selectors, *, actor, include_events=False):
        params = validate_params('usage.get', params)
        requests = []
        for selector in selectors:
            if (not isinstance(selector, dict) or set(selector) - {'task_id', 'agent_id', 'space_id'}
                    or any(key in params.get('filter', {}) and value != params['filter'][key]
                        for key, value in selector.items())):
                raise BusinessError('INVALID_SCHEMA')
            request = validate_params('usage.get', {'filter': {**params.get('filter', {}), **selector}})
            self.authorize_call('usage.get', request, actor=actor)
            requests.append(request)
        if include_events and (not selectors or selectors[0] != {}):
            raise BusinessError('INVALID_SCHEMA')
        if not requests:
            return [], None
        reader = self.readers.get('usage.batch')
        if not callable(reader):
            # 聚合DTO没有分组事实时不能倒推子范围，也不恢复N次全窗口读取。
            raise BusinessError('HOST_NOT_READY')
        try:
            pages, events = reader(_copy(params), _copy(requests))
            if (not isinstance(pages, (tuple, list)) or len(pages) != len(requests)
                    or not isinstance(events, dict)):
                raise ValueError('usage groups source shape')
            values = []
            for request, page in zip(requests, pages):
                if (not isinstance(page, ObservationPage)
                        or page.coverage not in {'complete', 'partial'} or len(page.records) != 1
                        or not self._visible(page.records[0], 'usage.get', request, actor)):
                    raise BusinessError('HOST_NOT_READY')
                values.append(validate_result('usage.get', redact_sensitive_value(_copy(page.records[0].value))))
        except BusinessError:
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RuntimeDatabaseDeferred):
            raise BusinessError('HOST_NOT_READY') from None
        for request, page in zip(requests, pages):
            self.authorize_call('usage.get', request, actor=actor)
            if not self._visible(page.records[0], 'usage.get', request, actor):
                raise BusinessError('HOST_NOT_READY')
        return values, events if include_events else None

    def _usage(self, params, actor):
        measured = self._source('usage', params)
        rows = [row for row in measured.records if self._visible(row, 'usage.get', params, actor)]
        if rows:
            if len(rows) != 1:
                raise BusinessError('OBSERVATION_SOURCE_INVALID')
            return redact_sensitive_value(_copy(rows[0].value))
        resources = self._source('resource_usage', params)
        rows = [row for row in resources.records if self._visible(row, 'usage.get', params, actor)]
        if len(rows) != 1:
            # 冻结ResourceUsage计数字段不接受null；没有真实观察时明确拒绝，不能编造0。
            raise BusinessError('HOST_NOT_READY', details={'coverage': 'unknown', 'source': 'resource_usage'})
        unknown = dict.fromkeys(('input_tokens', 'cached_input_tokens', 'uncached_input_tokens', 'output_tokens', 'cost', 'currency'))
        unknown['coverage'] = 'unknown'
        return {'actual': dict(unknown), 'estimated': dict(unknown), 'unknown': ['usage_source_unavailable'],
            'resource_usage': _view('ResourceUsage', rows[0].value)}

    def _event_generation(self):
        if self.runtime is None:
            return 'unavailable'
        try:
            info = self.runtime.db_path.stat()
        except OSError:
            return 'unavailable'
        return _digest([info.st_dev, info.st_ino])

    def initial_event_cursor(self, filters, *, actor):
        params = {'filter': filters}
        self.authorize_call('events.list', params, actor=actor)
        generation, after = self._event_generation(), 0
        if self.runtime is not None and generation != 'unavailable':
            with self.runtime._runtime_connection() as conn:
                first, last = self._event_bounds(conn)
            after = first - 1 if first is not None else last
        return self.cursor(self.scope_digest('events.list', params, actor),
            {'generation': generation, 'after': after})

    def _event_bounds(self, conn):
        """保留期清空后仍读取规范自增水位，区分新快照与清理前的零位点游标。"""
        # min 和 max 分开写，才能各自走索引端点，O(log N)。
        return conn.execute('SELECT (SELECT min(seq) FROM runtime_events),'
            'COALESCE((SELECT max(seq) FROM runtime_events),'
            '(SELECT seq FROM sqlite_sequence WHERE name=\'runtime_events\'),0)').fetchone()

    # LLM: 事件复核复用本帧distinct身份读取，不复用任何授权裁决。
    # 函数用途: 用本次TaskRun规范关联投影无attempt生命周期层级。
    def _event_without_attempt(self, source):
        """缺省 attempt 是合法生命周期层级，只用本次 TaskRun 的规范关联构造公开事实。"""
        try:
            metadata = json.loads(source['metadata_json'])
            context = BusinessExecutionContext.from_dict(metadata['business_context']) if metadata.get('business_context') else None
            if context and (context.owner_id != source['owner_id'] or metadata.get('session_task_ref') != context.task_id):
                return None
            task_id = context.task_id if context else source['canonical_task_id']
            thread_id = source['thread_id'] or None
            task = None
            if self.store is not None and task_id:
                task = _observation_task(self.store, task_id)
                if not thread_id and task and task.target_thread_id:
                    thread_id = task.target_thread_id
            if context and self.store is not None:
                if (task is None
                        or (source['thread_id'] and task.target_thread_id != source['thread_id'])
                        or (task.task_run_id and task.task_run_id != source['canonical_task_run_id'])
                        or task.metadata.get('business_context') != metadata['business_context']
                        or any(_observation_thread(self.store, tid).owner_id != self.owner_id
                            for tid in (task.sender_thread_id, task.target_thread_id))):
                    return None
            space_id, project_ref = (context.space_id, context.project_ref) if context else (None, None)
            if context is None and self.store is not None and thread_id:
                thread = _observation_thread(self.store, thread_id)
                if thread.owner_id != self.owner_id:
                    return None
                identity = background_identity(thread)
                if identity is not None:
                    space_id, project_ref = identity.space_id, identity.project_ref
            if source['run_id'] is not None:
                value = {'kind': 'subagent' if source['parent_run_id'] else 'background_main',
                    'id': source['run_id'], 'thread_id': thread_id or None,
                    'parent_run_id': source['parent_run_id'], 'managed_by_thread_id': None,
                    'space_id': space_id, 'task_id': task_id, 'run_id': source['run_id'],
                    'model_profile_id': None, 'status': source['agent_status'],
                    'has_children': bool(source['has_children']), '_parent_run_id': source['parent_run_id']}
            else:
                status, updated_at = source['task_run_status'], source['task_run_updated_at']
                if source['event_type'] == 'task_run.closed':
                    # TaskRun 可合法重开；关闭事件只投影原生产者的终态字段，其余原始payload不公开。
                    closed = json.loads(source['payload_json'])
                    if not isinstance(closed, dict) or closed.get('status') not in AGENT_RUN_TERMINAL_STATUSES:
                        return None
                    status, updated_at = closed['status'], source['created_at']
                if not thread_id:
                    return None
                value = {'task_id': task_id, 'thread_id': thread_id, 'space_id': space_id,
                    'project_ref': project_ref, 'status': status, 'current_run_id': None,
                    'result_refs': [], 'revision': int(updated_at * 1000000)}
            return ObservationRecord(value, source['owner_id'], space_id, project_ref, task_id,
                thread_id, source['run_id'], None)
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def _event_has_detached_thread(self, thread_id):
        """仅用于已核同 owner 的运行事件；临时读失败或尚待发表的线程留给重连。"""
        def detached():
            try:
                path = self.store.storage.thread_path(thread_id)
                thread, error = self.store.threads.load_report(thread_id)
            except (ValueError, TypeError):
                return True
            if error:
                return error.get('category') in {'data_corruption', 'data_parse', 'data_encoding'}
            if thread is not None:
                return thread.owner_id != self.owner_id
            try:
                path.stat()
            except FileNotFoundError:
                return True
            return False

        try:
            return _observation_cached((self.store.threads, 'detached-event', thread_id), detached)
        except OSError:
            return False

    # LLM: 列表分页按自增seq游标扫描，非匹配空间行批次推进避免假空页；超上限如实保留partial。
    # 函数用途: 返回已授权的流式事件页并支持空间过滤分页。
    def _events(self, params, actor):
        scope = self.scope_digest('events.list', params, actor)
        generation = self._event_generation()
        position = self.cursor_position(params.get('cursor'), scope)
        after = 0
        if position is not None:
            if (position.get('generation') != generation or type(position.get('after')) is not int or position['after'] < 0):
                raise BusinessError('CURSOR_EXPIRED')
            after = position['after']
        events, coverage = [], 'unknown'
        native_execution = 'executions' not in self.readers
        limit = params.get('limit', 50)
        filter_space = (params.get('filter') or {}).get('space_id')
        scan_limit = max(50, limit) if filter_space else limit
        if self.runtime is not None and generation != 'unavailable':
            with self.runtime._runtime_connection() as conn:
                if native_execution:
                    conn.execute('BEGIN')
                first, last = self._event_bounds(conn)
                retained_after = first - 1 if first is not None else last
                if position is not None and not retained_after <= after <= last:
                    raise BusinessError('CURSOR_EXPIRED')
                if position is None:
                    after = retained_after
                raw = conn.execute('SELECT e.*,ar.run_id,ar.agent_run_id AS canonical_agent_run_id,'
                    'aa.agent_run_id AS attempt_agent_run_id,ar.task_run_id AS agent_task_run_id,ar.status AS agent_status,'
                    'pa.run_id AS parent_run_id,tr.task_run_id AS canonical_task_run_id,tr.task_id AS canonical_task_id,'
                    'tr.metadata_json,tr.status AS task_run_status,tr.closed_at AS task_run_closed_at,'
                    'tr.updated_at AS task_run_updated_at,t.owner_id,t.thread_id,'
                    'agent_task.owner_id AS agent_owner_id,attempt_task.owner_id AS attempt_owner_id,'
                    'EXISTS(SELECT 1 FROM agent_runs child WHERE child.parent_agent_run_id=ar.agent_run_id '
                    'AND child.task_run_id=tr.task_run_id) AS has_children '
                    'FROM runtime_events e LEFT JOIN agent_attempts aa ON aa.attempt_id=e.attempt_id '
                    'LEFT JOIN agent_runs ar ON ar.agent_run_id=COALESCE(NULLIF(e.agent_run_id,\'\'),aa.agent_run_id) '
                    'LEFT JOIN task_runs agent_tr ON agent_tr.task_run_id=ar.task_run_id '
                    'LEFT JOIN tasks agent_task ON agent_task.task_id=agent_tr.task_id '
                    'LEFT JOIN agent_runs attempt_ar ON attempt_ar.agent_run_id=aa.agent_run_id '
                    'LEFT JOIN task_runs attempt_tr ON attempt_tr.task_run_id=attempt_ar.task_run_id '
                    'LEFT JOIN tasks attempt_task ON attempt_task.task_id=attempt_tr.task_id '
                    'LEFT JOIN agent_runs pa ON pa.agent_run_id=ar.parent_agent_run_id AND pa.task_run_id=ar.task_run_id '
                    'LEFT JOIN task_runs tr ON tr.task_run_id=COALESCE(NULLIF(e.task_run_id,\'\'),ar.task_run_id) '
                    'LEFT JOIN tasks t ON t.task_id=tr.task_id WHERE e.seq>? ORDER BY e.seq LIMIT ?',
                    (after, scan_limit)).fetchall()
                if native_execution:
                    execution_page = self._event_execution_page(conn, raw)
            if self._event_generation() != generation:
                raise BusinessError('CURSOR_EXPIRED')
            if not native_execution:
                execution_page = self._source('executions', params)
            execution = {(row.run_id, row.attempt_id): row for row in execution_page.records}
            coverage = execution_page.coverage
            for source in raw:
                # 明确属于其它owner的行不披露，也不能用其坏引用阻塞本owner；跨owner矛盾仍保留为缺口。
                # 非空引用必须已解析才能证明外部归属，缺省的optional引用则不等待后继记录。
                if (source['owner_id'] not in (None, self.owner_id)
                        and (not source['agent_run_id'] or source['agent_owner_id'] not in (None, self.owner_id))
                        and (not source['attempt_id'] or source['attempt_owner_id'] not in (None, self.owner_id))):
                    after = source['seq']
                    continue
                if (source['canonical_task_run_id'] is None or source['owner_id'] is None
                        or source['attempt_id'] and source['attempt_agent_run_id'] != source['canonical_agent_run_id']
                        or (source['agent_run_id'] or source['attempt_id'])
                        and source['agent_task_run_id'] != source['canonical_task_run_id']):
                    coverage = 'partial'
                    break
                if source['owner_id'] != self.owner_id:
                    after = source['seq']
                    continue
                row = (execution.get((source['run_id'], source['attempt_id'])) if source['attempt_id']
                    else self._event_without_attempt(source))
                if row is None:
                    # 已终结、从未绑定会话的内部运行没有可公开的业务身份；只推进有界游标，保留覆盖缺口。
                    # 活跃、缺引用、业务绑定未知或SessionTask仍存在的记录继续等待，不按正文猜归属。
                    metadata = json.loads(source['metadata_json'])
                    settled_unbound = (
                        not source['thread_id']
                        and source['task_run_status'] in AGENT_RUN_TERMINAL_STATUSES
                        and source['task_run_closed_at'] > 0
                        and (not source['canonical_agent_run_id'] or source['agent_status'] in AGENT_RUN_TERMINAL_STATUSES)
                        and isinstance(metadata, dict) and metadata.get('business_context') is None
                        and metadata.get('session_task_ref') in (None, '')
                        and self.store is not None
                        and _observation_task(self.store, source['canonical_task_id']) is None
                    )
                    coverage = 'partial'
                    # 运行/尝试已确认属于本 owner，显式会话文件却已删除、损坏或属于他人。
                    # 不暴露该行；跳过其序号，否则下一次订阅还会被相同的记录堵住。
                    detached_thread = (native_execution and self.store is not None and source['thread_id']
                        and isinstance(metadata, dict) and metadata.get('business_context') is None
                        and not metadata.get('session_task_ref') and self._event_has_detached_thread(source['thread_id']))
                    if settled_unbound or detached_thread:
                        after = source['seq']
                        continue
                    # 真正缺失的非空引用保留重连机会；空attempt不会等待并不存在的后继attempt。
                    break
                after = source['seq']
                event_facts = ObservationRecord({**row.value, 'occurred_at': _timestamp(source['created_at'])},
                    row.owner_id, row.space_id, row.project_ref, row.task_id, row.thread_id, row.run_id, row.attempt_id)
                if not self._visible(event_facts, 'events.list', params, actor):
                    continue
                cursor = self.cursor(scope, {'generation': generation, 'after': after})
                node = ({'kind': 'subagent' if row.value.get('_parent_run_id') else 'background_main', 'id': row.run_id}
                    if row.run_id is not None else None)
                kind = 'ExecutionView' if row.attempt_id is not None else 'NodeView' if node else 'TaskView'
                events.append({'stream_item': 'event', 'event_id': source['event_id'], 'cursor': cursor,
                    'event_type': source['event_type'], 'occurred_at': _timestamp(source['created_at']) or '', 'node': node,
                    'space_id': row.space_id, 'project_ref': row.project_ref, 'task_id': row.task_id,
                    'run_id': row.run_id, 'attempt_id': row.attempt_id, 'operation_id': None,
                    'payload': _view(kind, row.value), 'coverage': execution_page.coverage})
                if len(events) >= limit:
                    break
        return {'events': events, 'next_cursor': self.cursor(scope, {'generation': generation, 'after': after}),
            'coverage': coverage, 'reset_required': False}

    # LLM: 重连基线的嵌套查询共用当前快照作用域，返回后丢弃身份读取，不跨帧缓存。
    # 函数用途: 生成沿原授权和游标边界的重连基线。
    @observation_request()
    def snapshot(self, filters, *, actor):
        """重连基线与补拉沿同一读门；分页未覆盖部分明确保留partial与原游标。"""
        tasks = self.query('tasks.list', {'filter': filters, 'limit': 100}, actor=actor)
        executions = self.query('executions.list', {'filter': filters, 'limit': 100}, actor=actor)
        if filters.get('space_id'):
            tree = self.query('agent_tree.get', {'space_id': filters['space_id'], 'limit': 100}, actor=actor)
            # 其它filter只能收窄snapshot，不能用tree忽略task/run限制扩大可见数据。
            nodes = [node for node in tree['nodes'] if not filters.get('run_id') or node['run_id'] == filters['run_id']]
            nodes = [node for node in nodes if not filters.get('task_id') or node['task_id'] == filters['task_id']]
        else:
            nodes, coverages = [], []
            for sid in sorted({row['space_id'] for row in tasks['items'] + executions['items'] if row.get('space_id')}):
                tree = self.query('agent_tree.get', {'space_id': sid, 'limit': 100}, actor=actor)
                coverages.append(tree['coverage'])
                nodes.extend(tree['nodes'])
            tree = {'coverage': _coverage(coverages) if coverages else 'unknown', 'next_cursor': None}
            nodes = list({(node['kind'], node['id']): node for node in nodes}.values())
        # snapshot中的树节点同样与全部ReadFilter取交集，不能只有事件页收窄。
        source = self._source('executions', {'filter': filters})
        by_run = {row.run_id: row for row in source.records if row.value.get('_is_current')}
        narrowed = []
        for node in nodes:
            if node['run_id']:
                facts = by_run.get(node['run_id'])
            else:
                identity = None
                if self.store is not None:
                    thread = _observation_thread(self.store, node['thread_id'])
                    identity = background_identity(thread)
                facts = ObservationRecord(node, self.owner_id, node['space_id'] or filters.get('space_id'),
                    identity.project_ref if identity else None, node['task_id'], node['thread_id'])
            if facts is not None and self._visible(facts, 'events.list', {'filter': filters}, actor):
                narrowed.append(node)
        nodes = narrowed
        coverage = _coverage((tasks['coverage'], executions['coverage'], tree['coverage'], 'partial' if len(nodes) > 100 else 'complete'))
        return {'nodes': nodes[:100], 'tasks': tasks['items'], 'executions': executions['items'],
            'next_cursor': tasks['next_cursor'] or executions['next_cursor'] or tree['next_cursor'], 'coverage': coverage}
