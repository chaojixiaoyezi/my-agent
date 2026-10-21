# LLM: 每个任务 JSON 是权威，索引只是可重建投影；所有读改写受 owner 下同一进程锁保护。
# 模块用途: 保存任务运行节点、模型轮次、Token 总量与工具时间线。
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from agent_py_agent.agent.common.nofollow_fs import (
    list_names_beneath,
    read_text_beneath,
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

from ..workflows.models import redact_bearer_values as redact_sensitive_value

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

    # LLM: 返回深复制，避免调用者在锁外改变持久记录。
    # 函数用途: 输出带 schema 的字典。
    def to_dict(self):
        return {"schema": SCHEMA, **asdict(self)}


# LLM: 分页走真实账本接口，不把任务文件缺失解释为无调用；不设置历史截断上限。
# 函数用途: 按 owner 范围读取全部匹配工具调用。
def _calls(ledger, **filters):
    cursor = 0
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
    # 函数用途: 加载单任务权威记录。
    def _read(self, task_id):
        text = read_text_beneath(self._home, _path(task_id))
        if text is None:
            return None
        row = json.loads(text)
        if row.get("schema") != SCHEMA or row.get("task_id") != task_id:
            raise ValueError("invalid task record")
        if self._owner_id and row["owner_id"] != self._owner_id:
            raise ValueError("owner mismatch")
        return row

    # LLM: 无目录返回空；按固定安全文件名读取权威 JSON，不以索引遗漏解释为没有任务。
    # 函数用途: 列举所有任务的当前状态。
    def _records(self):
        try:
            names = list_names_beneath(self._home, (*PARTS, "task_calls"))
        except FileNotFoundError:
            return []
        return [self._read(name[:-5]) for name in sorted(names) if name.endswith(".json")]

    # LLM: 保存前验证并递归脱敏；JSON 原子提交后重建索引，索引失败时权威可能已提交。
    # 函数用途: 在调用方持有锁时保存完整记录及索引投影。
    def _save(self, payload):
        data = dict(payload)
        if data.pop("schema", SCHEMA) != SCHEMA:
            raise ValueError("unsupported schema")
        item = TaskCallRecord(**data)
        _path(item.task_id)
        if self._owner_id and item.owner_id not in ("", self._owner_id):
            raise ValueError("owner mismatch")
        item.owner_id = item.owner_id or self._owner_id
        if item.status not in STATUSES or item.trigger_kind not in {"interactive", "monitor", "workflow", "plugin_api"}:
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
        row = redact_sensitive_value(item.to_dict())
        write_text_atomic_beneath(self._home, _path(item.task_id), _encode(row))
        keys = ("task_id", "owner_id", "title", "status", "trigger_kind", "started_at", "ended_at")
        index = "".join(_encode({k: r[k] for k in keys}) for r in self._records())
        write_text_atomic_beneath(self._home, (*PARTS, "task_calls_index.jsonl"), index)
        return row

    # LLM: 字典是部分更新，dataclass 是完整快照；锁内先读后合并，防止覆盖并发追加轮次。
    # 函数用途: 创建或更新任务记录。
    def upsert_task_call(self, record):
        payload = record.to_dict() if isinstance(record, TaskCallRecord) else dict(record)
        _path(payload.get("task_id"))
        with _lock(self._home):
            existing = self._read(payload["task_id"]) or {}
            return self._save({**existing, **payload})

    # LLM: 默认必须已有任务；仅显式create在同一锁内建记录，轮次和树节点一起原子发表，不能覆盖已持久终态。
    # 函数用途: 追加模型轮次、补齐对应运行节点并刷新累计用量。
    def append_round(self, task_id, round_data, *, create_if_missing: bool = False):
        _path(task_id)
        with _lock(self._home):
            row = self._read(task_id)
            if row is None:
                if not create_if_missing:
                    raise KeyError(task_id)
                row = TaskCallRecord(task_id=task_id, owner_id=self._owner_id,
                                     thread_id=str(round_data.get("thread_id") or ""),
                                     started_at=float(round_data.get("started_at") or time.time()),
                                     root_run_id=str(round_data.get("root_run_id") or "")).to_dict()
            data = dict(round_data)
            if "duration_ms" in data:
                _number(data["duration_ms"])
            _append_model_run(row, data)
            row["rounds"].append(data)
            return self._save(row)

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
            row.update(status=status, ended_at=time.time() if ended_at is None else ended_at)
            return self._save(row)

    # LLM: 合并当前任务快照和全部账本任务，排序过滤后分页；不把自动发现的摘要落盘。
    # 函数用途: 分页查询任务记录。
    def list_task_calls(self, *, limit=50, cursor=0, **filters):
        if filters.keys() - {"status", "trigger_kind", "since", "until"}:
            raise ValueError("unknown filter")
        records = {r["task_id"]: r for r in self._records()}
        groups = {}
        for call in _calls(self.ledger):
            if call["task_id"]:
                groups.setdefault(call["task_id"], []).append(call)
        rows = [_merge_task(records.get(key), groups.get(key, [])) for key in records.keys() | groups.keys()]
        rows.sort(key=lambda r: (r["started_at"], r["task_id"]))
        return _page((r for r in rows if _matches(r, filters)), limit, cursor)

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
            return _merge_task(None, calls)
        return _merge_task(self._read(task_id), calls)


# LLM: 已有节点非空事实优先，轮次仅补空字段，不把模型调用终态冒充整个运行结束。
# 函数用途: 为新轮次补齐运行树节点和任务根编号。
def _append_model_run(row, data):
    run_id = str(data.get("run_id") or "")
    row["root_run_id"] = row.get("root_run_id") or str(data.get("root_run_id") or "")
    if not run_id:
        return
    node = dict(run_id=run_id, parent_run_id=str(data.get("parent_run_id") or ""),
                root_run_id=str(data.get("root_run_id") or ""), model_name=str(data.get("model") or ""),
                status="running", started_at=float(data.get("started_at") or row.get("started_at") or 0.0),
                duration_ms=float(data.get("duration_ms") or 0.0))
    previous = next((item for item in row["runs"] if item.get("run_id") == run_id), None)
    if previous is None:
        row["runs"].append(node)
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
        home = getattr(paths, "home_dir", None)
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


# LLM: 首终态消费绑定且只投影数值/身份，不存prompt或response；原metadata和终态不变，IO失败只丢可选遥测。
# 函数用途: 将完成、失败或超时的实际模型调用自动写入任务详情。
def record_model_call_telemetry(ledger: object, record: object) -> None:
    try:
        if getattr(record, "status", "") not in {"finished", "failed", "timed_out"}:
            return
        with _MODEL_SCOPE_LOCK:
            scope = getattr(ledger, "_telemetry_call_scopes", {}).pop(record.call_id, None)
        if scope is None or record.run_id != scope["run_id"]:
            return
        data = dict(round_id=record.call_id, run_id=record.run_id, parent_run_id=scope["parent_run_id"],
                    root_run_id=scope["root_run_id"], thread_id=scope["thread_id"], model=record.model, backend=record.backend,
                    purpose=str(record.metadata.get("purpose") or "main"), status=record.status,
                    started_at=float(scope["wall_started_at"]), duration_ms=round(max(0.0, float(record.total_latency_seconds or 0.0)) * 1000, 3),
                    prompt_tokens=max(0, int(record.accounted_input_tokens if record.status == "finished" else record.input_tokens)),
                    completion_tokens=max(0, int(record.output_tokens)), cached_tokens=max(0, int(record.cached_input_tokens)),
                    error_code=str(record.error_code or ""), stop_reason=str(record.stop_reason or ""))
        TaskCallInspector(scope["home_dir"], owner_id=scope["owner_id"]).append_round(scope["task_id"], data, create_if_missing=True)
    except Exception:
        return
