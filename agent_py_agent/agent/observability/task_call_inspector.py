# LLM: 任务 JSON 保存元数据/摘要，轮次只有追加源；索引是可重建投影，所有写者持原 owner 锁。
# 模块用途: 保存任务运行节点、模型轮次、Token 总量与工具时间线。
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from agent_py_agent.agent.common.nofollow_fs import (
    append_text_beneath,
    list_names_beneath,
    open_readonly_file_beneath,
    write_text_atomic_beneath,
)
from agent_py_agent.agent.observability.telemetry_ledger import (
    PARTS,
    ToolTelemetryLedger,
    _encode,
    _lock,
    _matches,
    _number,
    _page,
    _stats,
)

from ..local_storage.owner_read_index import OwnerReadUnavailable
from ..workflows.models import redact_bearer_values as redact_sensitive_value
from .task_rounds import (
    _append_task_round_unlocked,
    _validate_round,
    hydrate_task_rounds,
    missing_task_metadata,
    prepare_task_rounds,
)

SCHEMA = "task_call_telemetry.v1"
STATUSES = {"running", "succeeded", "failed", "paused", "cancelled"}
_MODEL_SCOPE_LOCK = threading.Lock()


# LLM: task_id 直接参与文件名，禁止任何路径分隔和特殊目录段。
# 函数用途: 生成安全的任务 JSON 相对路径。
def _path(task_id):
    if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", task_id):
        raise ValueError("invalid task_id")
    return (*PARTS, "task_calls", task_id + ".json")


# LLM: 规范字段优先于输入/输出/缓存别名；缓存是输入子集不重复累加，显式total仍保留供应商口径。
# 函数用途: 规范化一轮或整个任务的 Token 用量。
def _usage(data):
    aliases = dict(prompt_tokens="input_tokens", completion_tokens="output_tokens", cached_tokens="cached_input_tokens")
    usage = {key: data.get(key, data.get(alias, 0)) for key, alias in aliases.items()}
    usage["total_tokens"] = data.get("total_tokens", usage["prompt_tokens"] + usage["completion_tokens"])
    for value in usage.values():
        if type(value) is not int or _number(value) < 0:
            raise ValueError("invalid token count")
    return usage


# LLM: 轮次不截断、不去重；每次 append 表示一个模型请求，重放幂等由调用方保证。
# 函数用途: 从完整轮次列表重算任务累计 Token。
def _sum_usage(rounds):
    total = _usage({})
    for row in rounds:
        usage = _usage(row.get("token_usage", row))
        for key in total:
            total[key] += usage[key]
    return total


# LLM: schema 固定；任务运行节点和轮次保持调用者结构，列表默认值实例隔离。
# 类用途: 完整任务调用数据记录。
@dataclass
class TaskCallRecord:
    task_id: str
    owner_id: str = ""
    thread_id: str = ""
    trigger_kind: str = "interactive"
    title: str = ""
    status: str = "running"
    started_at: float = 0.0
    ended_at: float | None = None
    duration_ms: float = 0.0
    root_run_id: str = ""
    runs: list = field(default_factory=list)
    rounds: list = field(default_factory=list)
    token_usage: dict = field(default_factory=dict)
    mounted_assets: dict = field(default_factory=dict)
    artifacts: list = field(default_factory=list)
    goal: str = ""
    status_source: str = "unconfirmed"
    trigger_kind_source: str = "unconfirmed"

    # LLM: 返回深复制，避免调用者在锁外改变持久记录。
    # 函数用途: 输出带 schema 的字典。
    def to_dict(self):
        return {"schema": SCHEMA, **asdict(self)}


def _public_task_metadata(row):
    """遥测默认头不是生命周期证明；没有显式来源时公开未知，绝不推导规范终态。"""
    if row is None:
        return None
    value = dict(row)
    value['status_source'] = row.get('status_source', 'legacy_record' if row.get('status') in STATUSES - {'running'} else 'unconfirmed')
    value['trigger_kind_source'] = row.get('trigger_kind_source', 'legacy_record' if row.get('trigger_kind') in {'monitor', 'workflow', 'plugin_api'} else 'unconfirmed')
    if value['status_source'] not in {'explicit_record', 'legacy_record', 'tool_calls'}:
        value['status'] = None
        value['status_source'] = 'unconfirmed'
    if value['trigger_kind_source'] not in {'explicit_record', 'legacy_record'}:
        value['trigger_kind'] = None
        value['trigger_kind_source'] = 'unconfirmed'
    fields = [key for key in ('status', 'trigger_kind') if value.get(key + '_source') == 'unconfirmed']
    if fields:
        value['lifecycle_notice'] = dict(
            code='TASK_TELEMETRY_LIFECYCLE_UNCONFIRMED', fields=fields,
            reason='这些字段是遥测头，未提供规范生命周期或触发来源证据；null 不表示工具执行失败。',
            next_action='取得对应原始 Gateway 请求的准确 request_id 后，调用 audit_records(topic="message_lifecycle", request_id="<准确请求ID>")；只有模型 attempt 标识时保持未确认，禁止重跑任务猜状态。')
    return value


# LLM: 分页走真实账本接口，不把任务文件缺失解释为无调用；不设置历史截断上限。
# 函数用途: 按 owner 范围读取全部或指定 task_id 的匹配工具调用。
def _calls(ledger, *, task_id=None):
    cursor = 0
    filters = {"task_id": task_id} if task_id is not None else {}
    while True:
        page = ledger.query_tool_calls(limit=10000, cursor=cursor, **filters)
        yield from page
        if len(page) < 10000:
            return
        cursor += len(page)


# LLM: 这是工具观察窗口而非调度器终态；明确标记推导来源，不覆盖持久任务的权威状态。
# 函数用途: 从工具起止和结果推导摘要字段。
def _observed(calls):
    start, end = min(c["started_at"] for c in calls), max(c["ended_at"] for c in calls)
    return dict(started_at=start, ended_at=end, duration_ms=round((end-start)*1000, 3),
                status="succeeded" if all(c["status"] == "ok" for c in calls) else "failed",
                status_source="tool_calls")


# LLM: 只从对应工具的稳定引用字段提取资产，不把任意参数误认为授权或挂载事实。
# 函数用途: 合并观察到的资产引用并去重。
def _merge_assets(assets, call):
    keys = {"scripts": ("script_id", "script_ids"), "workflows": ("workflow_id", "workflow_ids"),
            "skill_search": ("skill_id", "skill_ids"), "schedule": ("job_id", "monitor_ids")}
    if call["tool_name"] not in keys:
        return
    source, target = keys[call["tool_name"]]
    value = call["args_summary"].get(source)
    if not isinstance(value, str) or not value:
        return
    existing = assets.get(target, [])
    values = list(existing) if isinstance(existing, list) else [existing]
    assets[target] = values if value in values else [*values, value]


# LLM: 显式非空字段保留；旧工具观测重算，空占位补齐，状态的来源单独标识。
# 函数用途: 合并同一 run 的持久节点和当前工具观测。
def _merge_run(previous, calls):
    observed = _observed(calls)
    node = {**observed, **previous}
    for key, value in observed.items():
        if not node.get(key) or previous.get("status_source") == "tool_calls":
            node[key] = value
    if previous.get("status") and previous.get("status_source") != "tool_calls":
        node["status_source"] = "persisted"
    for key in ("parent_run_id", "root_run_id"):
        node[key] = node.get(key) or next((c[key] for c in calls if c.get(key)), "")
    return node


# LLM: 所有输入来自同 owner；保留显式任务字段，只补树节点和资产，不持久化合成视图。
# 函数用途: 将工具账本与可选任务快照合成完整详情。
def _merge_task(row, calls):
    if not calls:
        return {**row, "tool_calls": [], "tool_summary": {"total_calls": 0, "tools": {}}} if row else None
    calls = sorted(calls, key=lambda c: (c["started_at"], c["call_id"]))
    data = row or {**TaskCallRecord(calls[0]["task_id"], owner_id=calls[0]["owner_id"]).to_dict(), **_observed(calls)}
    data = redact_sensitive_value(data)
    runs = {r["run_id"]: r for r in data["runs"] if r.get("run_id")}
    by_run, by_tool = {}, {}
    for call in calls:
        by_tool.setdefault(call["tool_name"], []).append(call)
        if call["run_id"]:
            by_run.setdefault(call["run_id"], []).append(call)
        _merge_assets(data["mounted_assets"], call)
        data["root_run_id"] = data["root_run_id"] or call.get("root_run_id", "")
    for run_id, group in by_run.items():
        runs[run_id] = {"run_id": run_id, **_merge_run(runs.get(run_id, {}), group)}
    data["runs"] = [r for r in data["runs"] if not r.get("run_id")] + list(runs.values())
    data["tool_calls"] = calls
    data["tool_summary"] = {"total_calls": len(calls), "tools": {name: _stats(group) for name, group in by_tool.items()}}
    return data


# LLM: 身份由宿主传入，索引不是授权来源；不在构造时创建任何文件。
# 类用途: 管理任务明细、轮次累加与工具明细关联。
class TaskCallInspector:
    # LLM: owner_home 是受信根，owner_id 非空时写入不允许声明别的归属。
    # 函数用途: 绑定 owner 存储。
    def __init__(self, owner_home, *, owner_id=""):
        self._home = Path(owner_home).absolute()
        self._owner_id = owner_id
        self.ledger = ToolTelemetryLedger(self._home, owner_id=owner_id)

    # LLM: 从固定文件安全读取，不相信索引携带的路径或身份。
    # 函数用途: 读取任务元数据；只有显式完整详情才组装追加源。
    def _read(self, task_id, *, include_rounds=False, _max_bytes=None):
        from ..local_storage.owner_read_index import _OWNER_INDEX_MAX_DOCUMENT_BYTES

        try:
            descriptor = open_readonly_file_beneath(self._home, _path(task_id))
        except FileNotFoundError:
            text = None
        else:
            try:
                ceiling = _OWNER_INDEX_MAX_DOCUMENT_BYTES if _max_bytes is None else min(_max_bytes, _OWNER_INDEX_MAX_DOCUMENT_BYTES)
                raw = os.pread(descriptor, ceiling + 1, 0)
                if len(raw) > ceiling:
                    raise ValueError('task metadata exceeds document byte budget')
                text = raw.decode('utf-8')
            finally:
                os.close(descriptor)
        if text is None:
            return missing_task_metadata(self._home, task_id, self._owner_id) if include_rounds else None
        row = json.loads(text)
        if not isinstance(row, dict) or row.get("schema") != SCHEMA or row.get("task_id") != task_id:
            raise ValueError("invalid task record")
        if self._owner_id and row["owner_id"] != self._owner_id:
            raise ValueError("owner mismatch")
        if (not isinstance(row.get('status'), str) or row['status'] not in STATUSES
                or not isinstance(row.get('trigger_kind'), str)
                or row['trigger_kind'] not in {'interactive', 'monitor', 'workflow', 'plugin_api'}):
            raise ValueError('invalid task status or trigger field')
        for key in ('status_source', 'trigger_kind_source'):
            if key in row and not isinstance(row[key], str):
                raise ValueError('invalid task metadata source field: ' + key)
        return hydrate_task_rounds(self._home, row) if include_rounds else row

    # LLM: 无目录返回空；列表后文件消失只过滤None，损坏/越权仍沿原异常，不把索引遗漏解释为没有任务。
    # 函数用途: 安全列举仍存在的任务，避开并发清理留下的空记录。
    def _records(self, *, diagnostics=None):
        try:
            names = list_names_beneath(self._home, (*PARTS, "task_calls"))
        except FileNotFoundError:
            return []
        rows = []
        for name in sorted(names):
            if name.endswith(".json"):
                try:
                    loaded = self._read(name[:-5], include_rounds=True)
                    if loaded is not None:
                        if (not isinstance(loaded.get('runs'), list)
                                or any(not isinstance(run, dict) for run in loaded['runs'])
                                or not isinstance(loaded.get('mounted_assets'), dict)
                                or not isinstance(loaded.get('root_run_id'), str)):
                            raise ValueError('invalid task collection fields')
                        _number(loaded['started_at'])
                        rows.append(loaded)
                except (OSError, ValueError, TypeError, KeyError, RecursionError, OwnerReadUnavailable) as error:
                    if diagnostics is not None:
                        # 不复制坏 JSON/异常正文；诊断数量有界，累计遗漏数仍准确。
                        diagnostics['skipped_tasks'] += 1
                        if len(diagnostics['errors']) < 16:
                            diagnostics['errors'].append(dict(code='TASK_TELEMETRY_RECORD_UNREADABLE',
                                task_id=name[:-5], error_type=type(error).__name__, effect_outcome='not_started',
                                next_action='核实该 task_id 的任务元数据或轮次源；其它任务可继续查询'))
        return rows

    # LLM: 增量更新索引行，缺失或目录文件名不一致时只补读缺失任务，内容未变时不重写索引文件。
    # 函数用途: 维护任务索引投影，避免每次追加模型轮次时重读全部历史任务JSON。
    def _write_index(self, row):
        keys = ("task_id", "owner_id", "title", "status", "trigger_kind", "started_at", "ended_at")
        summary = redact_sensitive_value({key: row[key] for key in keys})
        from ..local_storage.owner_read_index import _OWNER_INDEX_MAX_ROW_BYTES
        blob = _encode(summary)
        if len(blob.encode('utf-8')) > _OWNER_INDEX_MAX_ROW_BYTES:
            raise ValueError('task summary exceeds index row budget')
        from .task_call_retention import rotate_task_summary

        rotate_task_summary(self._home)
        append_text_beneath(self._home, (*PARTS, "task_calls_index.jsonl"), blob)

    # LLM: 保存前验证并递归脱敏；JSON 原子提交后按需同步索引，追加轮次且索引已存在时不重扫目录。
    #   元数据是遥测投影不是权威：原子替换但不刷盘（durable=False），读者对坏/缺文件已按缺失处理。
    #   redact_payload=False 只给轮次追加：已存元数据在首次保存时已脱敏，新增内容只来自已脱敏的新轮次行。
    # 函数用途: 在原锁内验证并保存元数据，轮次的单一追加源按需迁移与组装。
    def _save(self, payload, *, sync_index: bool = True, include_rounds: bool = True, replace_rounds: bool = False,
              redact_payload: bool = True, write: bool = True):
        data = dict(payload)
        rounds_state = data.pop("_rounds", None)
        if data.pop("schema", SCHEMA) != SCHEMA:
            raise ValueError("unsupported schema")
        # 旧非默认值只能来自显式记录；后续只追加轮次不能抹掉这份来源事实。
        for key, known in (('status', STATUSES - {'running'}),
                           ('trigger_kind', {'monitor', 'workflow', 'plugin_api'})):
            if key + '_source' not in data:
                data[key + '_source'] = 'legacy_record' if isinstance(data.get(key), str) and data[key] in known else 'unconfirmed'
        item = TaskCallRecord(**data)
        _path(item.task_id)
        if self._owner_id and item.owner_id not in ("", self._owner_id):
            raise ValueError("owner mismatch")
        item.owner_id = item.owner_id or self._owner_id
        if (not isinstance(item.status_source, str) or item.status_source not in {'unconfirmed', 'explicit_record', 'legacy_record'}
                or not isinstance(item.trigger_kind_source, str) or item.trigger_kind_source not in {'unconfirmed', 'explicit_record', 'legacy_record'}):
            raise ValueError('invalid task metadata source')
        if (not isinstance(item.status, str) or item.status not in STATUSES
                or not isinstance(item.trigger_kind, str) or item.trigger_kind not in {"interactive", "monitor", "workflow", "plugin_api"}):
            raise ValueError("invalid task status or trigger")
        _number(item.started_at)
        _number(item.duration_ms)
        if item.ended_at is not None:
            if _number(item.ended_at) < item.started_at:
                raise ValueError("ended before started")
            item.duration_ms = round((item.ended_at - item.started_at) * 1000, 3)
        if any(not isinstance(v, list) for v in (item.runs, item.rounds, item.artifacts)) or not isinstance(item.mounted_assets, dict):
            raise ValueError("invalid task collections")
        item.token_usage = _sum_usage(item.rounds) if item.rounds else _usage(item.token_usage)
        row = redact_sensitive_value(item.to_dict()) if redact_payload else item.to_dict()
        if rounds_state is not None:
            row["_rounds"] = rounds_state
        if not write:
            return row
        if item.rounds or replace_rounds:
            row = prepare_task_rounds(self._home, row, replace=replace_rounds)
        write_text_atomic_beneath(self._home, _path(item.task_id), _encode(row), durable=False)
        if sync_index:
            self._write_index(row)
        return hydrate_task_rounds(self._home, row) if include_rounds else None

    def _index_missing(self):
        path = self._home.joinpath(*PARTS, "task_calls_index.jsonl")
        return not path.is_file() or path.stat().st_size <= 0

    # LLM: 字典是部分更新，dataclass 是完整快照；锁内先读后合并，防止覆盖并发追加轮次。
    # 函数用途: 创建或更新任务记录。
    def upsert_task_call(self, record):
        payload = record.to_dict() if isinstance(record, TaskCallRecord) else dict(record)
        payload.pop("_rounds", None)
        payload.pop("rounds_coverage", None)
        payload.pop("lifecycle_notice", None)
        if 'status' in payload:
            payload['status_source'] = 'explicit_record'
        if 'trigger_kind' in payload:
            payload['trigger_kind_source'] = 'explicit_record'
        _path(payload.get("task_id"))
        with _lock(self._home):
            existing = self._read(payload["task_id"]) or {}
            return self._save({**existing, **payload}, replace_rounds="rounds" in payload)

    # LLM: 原 owner 锁串行源与摘要；两者不是跨文件原子事务，详情明确披露未追平的摘要。
    #   已读到的元数据直接交给追加（不再二次读与解析）；新任务先只在内存规范化/脱敏，由追加在建好轮次源后
    #   第一次写入带水位的元数据（省掉一次不带水位的中间写），中断后仍按原确定性代次复用同一轮次源。
    # 函数用途: 追加模型轮次、补齐对应运行节点并刷新累计用量。
    def append_round(self, task_id, round_data, *, create_if_missing: bool = False, include_rounds: bool = True) -> dict | None:
        _path(task_id)
        with _lock(self._home):
            row = self._read(task_id)
            data = dict(round_data)
            _validate_round(data)
            created = row is None
            if created:
                if not create_if_missing:
                    raise KeyError(task_id)
                row = TaskCallRecord(task_id=task_id, owner_id=self._owner_id,
                                     thread_id=str(round_data.get("thread_id") or ""),
                                     started_at=float(round_data.get("started_at") or time.time()),
                                     root_run_id=str(round_data.get("root_run_id") or "")).to_dict()
                row = self._save(row, sync_index=False, include_rounds=False, write=False)
            _append_task_round_unlocked(self._home, task_id, data, metadata=row)
            if include_rounds and (created or self._index_missing()):
                self._write_index(self._read(task_id))
            return self._read(task_id, include_rounds=True) if include_rounds else None

    # LLM: completed 状态不含 running；结束时间未指定时取当前时间，负耗时由保存校验拒绝。
    # 函数用途: 更新任务终态及总耗时。
    def complete_task_call(self, task_id, status, ended_at=None):
        _path(task_id)
        if status not in STATUSES - {"running"}:
            raise ValueError("invalid completion status")
        with _lock(self._home):
            row = self._read(task_id)
            if row is None:
                raise KeyError(task_id)
            row.update(status=status, status_source="explicit_record", ended_at=time.time() if ended_at is None else ended_at)
            return self._save(row)

    def get_task_call_page(self, task_id: str, *, limit: int = 20, cursor: str = '') -> dict:
        """只读一个任务及有限轮次，不自动关联全量工具历史。"""
        from .task_rounds import read_task_rounds_page

        row = self._read(task_id)
        if row is None:
            return dict(task=None, rows=[], rounds=[], next_cursor=None, total_count=None,
                        coverage=dict(status='unavailable', reason='task_metadata_missing'))
        page = read_task_rounds_page(self._home, task_id, limit=limit, cursor=cursor)
        return {**page, 'task': redact_sensitive_value(_public_task_metadata(row)), 'rounds': page['rows']}

    # LLM: 合并当前任务快照和全部账本任务，排序过滤后分页；不把自动发现的摘要落盘。
    # 函数用途: 分页查询任务记录。
    def list_task_calls(self, *, limit=50, cursor=0, **filters):
        """保留旧列表合同；损坏项只影响自己的结果，不阻断健康任务。"""
        return self.list_task_calls_report(limit=limit, cursor=cursor, **filters)['rows']

    def list_task_calls_report(self, *, limit=50, cursor=0, **filters):
        """报告页显式携带部分覆盖与有界诊断；查询不修改或隔离移动源文件。"""
        if filters.keys() - {"status", "trigger_kind", "since", "until"}:
            raise ValueError("unknown filter")
        if type(limit) is not int or type(cursor) is not int or min(limit, cursor) < 0:
            raise ValueError('invalid pagination')
        diagnostics = dict(skipped_tasks=0, errors=[])
        records = {r['task_id']: r for r in self._records(diagnostics=diagnostics)}
        groups = {}
        tool_coverage = None
        try:
            for call in _calls(self.ledger):
                if call['task_id']:
                    groups.setdefault(call['task_id'], []).append(call)
        except OwnerReadUnavailable as error:
            tool_coverage = dict(status=error.coverage.get('status'), reason=error.coverage.get('reason'))
        rows = [_public_task_metadata(_merge_task(records.get(key), groups.get(key, [])))
                for key in records.keys() | groups.keys()]
        rows.sort(key=lambda r: (r['started_at'], r['task_id']))
        partial_rounds = sum(1 for row in rows if row.get('rounds_coverage', {}).get('status', 'complete') != 'complete')
        partial = diagnostics['skipped_tasks'] > 0 or partial_rounds > 0 or tool_coverage is not None
        coverage = dict(status='partial' if partial else 'complete',
                        reason='task_sources_incomplete' if partial else 'covered',
                        skipped_tasks=diagnostics['skipped_tasks'], partial_round_tasks=partial_rounds)
        if tool_coverage is not None:
            coverage['tool_calls'] = tool_coverage
        return dict(rows=_page((r for r in rows if _matches(r, filters)), limit, cursor),
                    coverage=coverage, diagnostics=diagnostics['errors'])

    # LLM: 拒绝跨 home/owner 账本；不透明 ID 只查账本、绝不拼路径，合法文件 ID 才读任务 JSON。
    # 函数用途: 返回任务详情及按开始时间排序的工具调用。
    def get_task_call_detail(self, task_id, ledger: ToolTelemetryLedger | None = None):
        ledger = ledger or self.ledger
        if ledger._home != self._home or ledger._owner_id != self._owner_id:
            raise ValueError("ledger scope mismatch")
        calls = list(_calls(ledger, task_id=task_id)) if isinstance(task_id, str) and task_id else []
        try:
            _path(task_id)
        except ValueError:
            if not calls:
                raise
            return _public_task_metadata(_merge_task(None, calls))
        return _public_task_metadata(_merge_task(self._read(task_id, include_rounds=True), calls))


# LLM: 已有节点非空事实优先，轮次仅补空字段，不把模型调用终态冒充整个运行结束。
# 函数用途: 为新轮次补齐运行树节点和任务根编号。
def _append_model_run(row, data, *, runs_by_id=None):
    run_id = str(data.get("run_id") or "")
    row["root_run_id"] = row.get("root_run_id") or str(data.get("root_run_id") or "")
    if not run_id:
        return
    node = dict(run_id=run_id, parent_run_id=str(data.get("parent_run_id") or ""),
                root_run_id=str(data.get("root_run_id") or ""), model_name=str(data.get("model") or ""),
                status="running", started_at=float(data.get("started_at") or row.get("started_at") or 0.0),
                duration_ms=float(data.get("duration_ms") or 0.0))
    previous = (runs_by_id.get(run_id) if runs_by_id is not None
                else next((item for item in row["runs"] if item.get("run_id") == run_id), None))
    if previous is None:
        row["runs"].append(node)
        if runs_by_id is not None:
            runs_by_id[run_id] = node
    else:
        previous.update({key: value for key, value in node.items() if not previous.get(key)})


# LLM: 只读结构化身份；来源必须run匹配，再按优先级补不冲突的空父根字段，不能拼接相互矛盾的链。
# 函数用途: 取得当前模型调用的完整父根关系和线程编号。
def _model_scope_identity(agent, params):
    attrs = getattr(params, "task_attributes", None) or {}
    current = getattr(agent, "_current_run_params", None)
    run_id = str(getattr(params, "run_id", "") or "")
    sources = ((params, run_id), (attrs, run_id), (getattr(agent, "_current_run_scope", None), ""),
               (current, ""), (getattr(current, "task_attributes", None), str(getattr(current, "run_id", "") or "")))
    identity = {}
    for source, inherited_run in sources:
        values = {key: str((source.get(key) if isinstance(source, dict) else getattr(source, key, "")) or "")
                  for key in ("run_id", "parent_run_id", "root_run_id")}
        if ((values["run_id"] or inherited_run) != run_id
                or any(identity.get(key) and values[key] and identity[key] != values[key]
                       for key in ("parent_run_id", "root_run_id"))):
            continue
        identity.update({key: value for key, value in values.items() if value and not identity.get(key)})
    return dict(run_id=run_id, parent_run_id=identity.get("parent_run_id", ""), root_run_id=identity.get("root_run_id", ""),
                thread_id=str(getattr(params, "thread_id", "") or attrs.get("conversation_thread_id") or attrs.get("thread_id") or ""))


# LLM: 只绑定既有Owner和合法任务ID；有界私有作用域不改ModelCallRecord.metadata、不建目录，异常不得影响主链。
# 函数用途: 为准确call_id冻结Owner、任务、父根关系和墙钟开始时间。
def bind_model_call_telemetry(ledger: object, call_id: str, agent: object, params: object) -> None:
    try:
        paths = getattr(agent, "home_paths", None)
        home = getattr(paths, "owner_home_dir", None)
        task_id = str(getattr(params, "task_id", "") or "")
        if not home or not Path(home).is_dir():
            return
        _path(task_id)
        scope = dict(home_dir=str(Path(home).absolute()), owner_id=str(getattr(paths, "owner_id", "") or ""),
                     task_id=task_id, wall_started_at=time.time(), **_model_scope_identity(agent, params))
        with _MODEL_SCOPE_LOCK:
            scopes = getattr(ledger, "_telemetry_call_scopes", None)
            if scopes is None:
                ledger._telemetry_call_scopes = scopes = {}
            scopes[call_id] = scope
            while len(scopes) > 256:
                scopes.pop(next(iter(scopes)))
    except Exception:
        return


# LLM: 首终态消费绑定且只投影数值/身份；估算与供应商用量分开，首轮间隔保持None，不存正文或改变原终态。
# 函数用途: 将完成、失败或超时的调用及组装/轮间耗时写入任务详情，IO失败只丢可选遥测。
def record_model_call_telemetry(ledger: object, record: object) -> None:
    try:
        if getattr(record, "status", "") not in {"finished", "failed", "timed_out"}:
            return
        with _MODEL_SCOPE_LOCK:
            scope = getattr(ledger, "_telemetry_call_scopes", {}).pop(record.call_id, None)
        if scope is None or record.run_id != scope["run_id"]:
            return
        prompt_tokens = max(0, int(record.accounted_input_tokens if record.status == "finished" else record.input_tokens))
        cached_tokens = max(0, int(record.cached_input_tokens))
        data = dict(round_id=record.call_id, request_id=str(getattr(record, "request_id", "") or ""), run_id=record.run_id, parent_run_id=scope["parent_run_id"],
                    root_run_id=scope["root_run_id"], thread_id=scope["thread_id"], model=record.model, backend=record.backend,
                    purpose=str(record.metadata.get("purpose") or "main"), status=record.status,
                    started_at=float(scope["wall_started_at"]), duration_ms=round(max(0.0, float(record.total_latency_seconds or 0.0)) * 1000, 3),
                    context_assembly_ms=round(max(0.0, float(record.metadata.get("context_assembly_ms") or 0.0)), 3),
                    inter_round_gap_ms=record.metadata.get("inter_round_gap_ms"),
                    estimated_input_tokens=record.metadata.get("estimated_input_tokens", record.input_tokens),
                    first_token_wait_ms=round(max(0.0, float(record.first_token_latency_seconds or 0.0)) * 1000, 3),
                    prompt_tokens=prompt_tokens,
                    completion_tokens=max(0, int(record.output_tokens)), cached_tokens=cached_tokens,
                    uncached_tokens=max(0, prompt_tokens - cached_tokens),
                    error_code=str(record.error_code or ""), stop_reason=str(record.stop_reason or ""))
        for key in ('request_prepare_ms', 'pre_call_housekeeping_ms', 'supervision_sweep_ms'):
            value = record.metadata.get(key)
            data[key] = round(max(0.0, _number(value)), 3) if type(value) in (int, float) else None
        stages = record.metadata.get('stages_ms', {})
        if isinstance(stages, dict):
            data['stages_ms'] = {key: max(0.0, _number(value)) for key, value in stages.items()
                                 if isinstance(key, str) and type(value) in (int, float)}
        TaskCallInspector(scope["home_dir"], owner_id=scope["owner_id"]).append_round(
            scope["task_id"], data, create_if_missing=True, include_rounds=False,
        )
    except Exception:
        return
