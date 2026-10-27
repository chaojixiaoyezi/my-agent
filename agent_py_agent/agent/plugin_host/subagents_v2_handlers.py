# LLM: 内部适配仅接宿主context/agent；W4/W6须先核来源、AND能力、线程/任务范围及幂等，禁止payload注入manager。
# 模块用途: 冷读canonical子代理，沿原短事务/CAS投递指导和精确取消，不新建执行身份或影子状态。
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from types import SimpleNamespace

from ..common.log_redaction import redact_sensitive_value
from ..common.nofollow_fs import (
    open_directory_beneath,
    open_readonly_file_beneath,
)
from ..concurrency.exceptions import ConcurrencyConflictError
from ..conversation.display_checkpoint import is_display_checkpoint
from ..conversation.store_messages import _message_entries
from ..settings import model_profiles
from ..subagents.cancellation import (
    CancelSubagentTaskRequest,
    cleanup_subagent_tree,
    prepare_subagent_stops,
)
from ..subagents.services.persistence.service import SubAgentPersistenceService
from ..tooling.process_session_cleanup import ProcessSessionCleanupError, stop_process_session
from ..tooling.process_session_store import ProcessSessionStore, process_session_store_root
from ..user_space.home_layout import home_paths
from ..user_space.owner_resolver import home_paths_with_owner
from .errors import HostAPIError
from .tasks import resource_id

_TERMINAL = {"DONE", "FAILED", "CANCELLED", "ABANDONED", "TAKEN_OVER", "TIMEOUT", "CHANNEL_ERROR"}
# LLM: 原服务只读构造不创建manager/DB；注入agent须属同一owner；目录逐段nofollow，坏记录不降级空列表。
# 函数用途: 找出本owner原持久化读取口。
def _readers(context, agent):
    home = context.owner_home.home_dir
    if agent is not None:
        if agent.subagents.owner_id != context.owner_home.owner_id:
            raise HostAPIError("SUBAGENT_NOT_FOUND", 404)
        return [agent.subagents]
    base = home / "workspace" / "runtime" / "workspaces"
    readers = []
    for path in sorted(base.glob("*/subagents")):
        descriptor = open_directory_beneath(home, path.relative_to(home).parts, require_dir_fd=True)
        os.close(descriptor)
        readers.append(SubAgentPersistenceService(SimpleNamespace(workspace=path)))
    return readers
# LLM: owner、thread和root都是精确交集；空范围不扩大为全owner，旧CAS不能投递或取消。
# 函数用途: 核实一个canonical子代理及可选修订。
def _check(context, task, payload, *, cas=False):
    tid = (task.attributes or {}).get("conversation_thread_id") or task.runtime_identity.conversation_id or task.agent_thread_id
    if (task.owner != context.owner_home.owner_id or payload.get("thread_id") and payload["thread_id"] != tid
            or payload.get("root_task_id") and payload["root_task_id"] not in {task.root_id, task.id}):
        raise HostAPIError("SUBAGENT_NOT_FOUND", 404)
    revision = payload.get("expected_revision")
    if cas and revision is not None and (type(revision) is not int or revision != task.state_revision):
        raise HostAPIError("SUBAGENT_REVISION_CONFLICT", 409)
    return task
# LLM: ID在任何I/O前验证；查无记录和归属不符同404，不能用同owner的另一项目记录补缺。
# 函数用途: 精确加载并检查目标，不创建原任务。
def _load(context, payload, agent):
    identifier = resource_id(payload["subagent_id"])
    for reader in _readers(context, agent):
        try:
            return _check(context, reader.load(identifier), payload)
        except FileNotFoundError:
            continue
    raise HostAPIError("SUBAGENT_NOT_FOUND", 404)
# LLM: 只按认证context重建原owner目录，不信agent或payload提供的路径；读取不迁移落盘，坏目录显式503。
# 函数用途: 读取本次投影共用的当前用户模型档案。
def _owner_model_profiles(context):
    try:
        paths = home_paths_with_owner(home_paths(context.owner_home.root), context.owner_home)
        return model_profiles.read_model_profiles(model_profiles.model_profiles_path(paths))["profiles"]
    except (OSError, model_profiles.ModelProfileError):
        raise HostAPIError("STORE_UNAVAILABLE", 503) from None
# LLM: 只投影原记录名称或本owner档案名称；default才取注入配置，未知保持None，不宣称历史调用模型。
# 函数用途: 投影原任务公开摘要及原canonical修订。
def _project(task, profiles=None, *, agent=None):
    attrs, status = task.attributes or {}, task.status
    model = attrs.get("host_model_profile.v1") or {}
    profile_id, model_name = model.get("profile_id"), model.get("model_name")
    if not model_name and isinstance(profile_id, str):
        model_name = (profiles or {}).get(profile_id, {}).get("model_name")
    if not model_name and profile_id == "default":
        model_name = getattr(getattr(agent, "config", None), "model_name", None)
    effort = attrs.get("host_reasoning_effort.v1") or {}
    return {"subagent_id": task.id, "run_id": task.id, "parent_run_id": task.parent_id,
            "thread_id": attrs.get("conversation_thread_id") or task.runtime_identity.conversation_id or task.agent_thread_id,
            "name": task.agent_name, "role": task.role, "model_name": model_name,
            "reasoning_effort": effort.get("level"), "status": {"DONE": "completed", "TIMEOUT": "failed",
            "CHANNEL_ERROR": "failed"}.get(status, status.lower()), "current_action": task.current_tool or task.current_step,
            "started_at": task.created_at or None, "elapsed_seconds": max(0, (task.ended_at or time.time()) - task.created_at)
            if task.created_at else None, "revision": task.state_revision}
# LLM: 本owner档案每次请求只读一次，列表只公开已核行；未结束过滤按原状态集合，不按中文摘要猜测。
# 函数用途: 列出指定线程/根任务的子代理。
def list_subagents(context, payload, *, agent=None, store=None):
    if not payload.get("thread_id"):
        raise HostAPIError("INVALID_SCHEMA")
    profiles = _owner_model_profiles(context)
    rows = []
    for reader in _readers(context, agent):
        report = reader.list_runs_report()
        if report.load_errors:
            raise HostAPIError("STORE_UNAVAILABLE", 503)
        for task in report.runs:
            try:
                _check(context, task, payload)
            except HostAPIError:
                continue
            if payload.get("include_finished", True) or task.status not in _TERMINAL:
                rows.append(_project(task, profiles, agent=agent))
    return {"items": rows}
# LLM: 原FD固定尾窗按JSONL的LF分行、兼容CRLF，Unicode正文不拆开；超窗首行保守丢弃，绝不按路径重开。
# 函数用途: 从同一个流取得有界末尾行。
def _tail_lines(stream, max_bytes):
    size = os.fstat(stream.fileno()).st_size
    stream.seek(max(0, size - max_bytes))
    lines = stream.read(max_bytes).decode("utf-8", "replace").split("\n")
    if lines[-1] == "":
        lines.pop()
    lines = [line.removesuffix("\r") for line in lines]
    return lines[1:] if size > max_bytes else lines
# LLM: 只接受JSON对象；坏JSON/过深结构不成为记录，时间线原字符串可由调用方独立保留。
# 函数用途: 安全解析一行JSONL对象。
def _json_row(line):
    try:
        row = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return row if isinstance(row, dict) else None
# LLM: 线程双归属核验后只读原nofollow FD的512KiB尾窗，不调用按路径重开的recent_report；至多50条本线程非display消息。
# 函数用途: 在已验证文件上直接解析原消息类型，缺文件为空并跳过损坏行。
def _messages(context, task, store):
    if store is None or not task.agent_thread_id:
        return []
    home = context.owner_home.home_dir
    thread, error = store.threads.load_report(task.agent_thread_id)
    if error:
        raise HostAPIError("STORE_UNAVAILABLE", 503)
    if thread is not None and (thread.owner_id != context.owner_home.owner_id or thread.owner_home != str(home)):
        raise HostAPIError("SUBAGENT_NOT_FOUND", 404)
    try:
        parts = store.storage.message_path(task.agent_thread_id).relative_to(home).parts
        with os.fdopen(open_readonly_file_beneath(home, parts), "rb") as stream:
            rows = [_json_row(line) for line in _tail_lines(stream, 524288)]
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        raise HostAPIError("SUBAGENT_NOT_FOUND", 404) from None
    messages, _errors = _message_entries([row for row in rows if row is not None])
    return [row for row in messages if row.thread_id == task.agent_thread_id and not is_display_checkpoint(row)][-50:]
# LLM: 数字按原单位解释，ISO有时区则保留、无时区固定UTC；布尔/非有限/损坏值未知，不借宿主时区猜测。
# 函数用途: 解析一个可比较的时间值，未知返回排序哨兵但不改原记录。
def _timestamp(value):
    if isinstance(value, bool):
        return float("inf")
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            value = (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).timestamp()
        except (AttributeError, TypeError, ValueError, OverflowError, OSError):
            return float("inf")
    return value if isfinite(value) else float("inf")
# LLM: 优先毫秒/秒/ISO时间字段，统一为秒；未知排末尾，稳定sort负责保留同值及未知项的文件次序。
# 函数用途: 为时间线记录提取统一时间排序键。
def _step_sort_ts(row):
    if not isinstance(row, dict):
        return float("inf")
    for key, divisor in (("ts_ms", 1000), ("ts", 1), ("timestamp", 1), ("created_at", 1)):
        value = _timestamp(row.get(key))
        if isfinite(value):
            return value / divisor
    return float("inf")
# LLM: 仍只读256KiB/末100行；JSON对象按时间稳定排序，但保留原JSON字符串、seq及旧非JSON行回执。
# 函数用途: 在同一nofollow FD上读时间线并按时间返回原始行。
def _steps(home, path):
    if not path:
        return []
    parts = Path(path).relative_to(home).parts
    try:
        with os.fdopen(open_readonly_file_beneath(home, parts), "rb") as stream:
            lines = _tail_lines(stream, 262144)[-100:]
    except FileNotFoundError:
        return []
    return sorted(lines, key=lambda line: _step_sort_ts(_json_row(line)))
# LLM: 原时间线有界nofollow尾读并稳定时间排序；消息沿同一FD读取/脱敏，模型名仅解析本owner原档案。
# 函数用途: 返回目标指令、真实时间线、消息和结果摘要。
def get_subagent(context, payload, *, agent=None, store=None):
    task = _load(context, payload, agent)
    steps = _steps(context.owner_home.home_dir, task.agent_run_timeline_jsonl)
    messages = _messages(context, task, store)
    return {**_project(task, _owner_model_profiles(context), agent=agent), "instruction": task.goal, "steps": steps,
            "messages": redact_sensitive_value([row.to_dict() for row in messages]), "output_summary": task.result or task.latest_summary}
# LLM: mutation用原canonical CAS；有副作用后异常保unknown，不把队列已写伪装未执行。
# 函数用途: 在原创建锁内执行一个窄修改，映射原修订冲突。
def _mutate(context, payload, agent, reducer):
    if agent is None:
        raise HostAPIError("HOST_NOT_READY", 503)
    task = _check(context, _load(context, payload, agent), payload, cas=True)
    if task.status in _TERMINAL:
        raise HostAPIError("SUBAGENT_NOT_RUNNING", 409)
    try:
        return agent.subagents.mutate(task.id, reducer, expected_revision=task.state_revision)
    except ConcurrencyConflictError:
        raise HostAPIError("SUBAGENT_REVISION_CONFLICT", 409) from None
# LLM: 只入原指导队列，不称已送达；调用方幂等回执负责重放，canonical CAS先于正文入队。
# 函数用途: 向准确子代理追加一条中断指导。
def steer_subagent(context, payload, *, agent=None, store=None):
    text = payload.get("message", payload.get("text"))
    if not isinstance(text, str) or not text.strip() or len(text) > 20000:
        raise HostAPIError("INVALID_SCHEMA")
    if agent is None or store is None:
        raise HostAPIError("HOST_NOT_READY", 503)
    # LLM: reducer仅追加当前目标指导并触碰其原修订；不等待执行器或网络。
    # 函数用途: 在原CAS锁内持久接受指导。
    def append(task):
        store.guidance.append({"target_type": "agent_run", "target_id": task.id, "message": text, "delivery": "interrupt"})
        task.updated_at = time.time()
    try:
        with agent.subagents.creation_guard():
            task = _mutate(context, payload, agent, append)
    except HostAPIError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown") from None
    return {"subagent_id": task.id, "steered": True, "revision": task.state_revision}
# LLM: 原creation guard内CAS并冻结单个run；清理必须在所有控制锁外，不扩到后代/兄弟或父线程。
# 函数用途: 请求精确取消并返回原资源清理确认，而不是仅改展示状态。
def stop_subagent(context, payload, *, agent=None, store=None):
    if agent is None:
        raise HostAPIError("HOST_NOT_READY", 503)
    # LLM: CAS的提交点是原canonical停止意图，不是空写；之后清理故障保unknown，不自动重放。
    # 函数用途: 持久接受当前版本的一次停止请求，不替代原native取消权威。
    def accept(current):
        current.attributes = {**current.attributes, "plugin_host_stop": {"requested_at": time.time()}}
    try:
        with agent.subagents.creation_guard():
            task = _mutate(context, payload, agent, accept)
            request = CancelSubagentTaskRequest(task, payload.get("reason", "stopped_by_plugin_host"), source="plugin_host")
            batch = prepare_subagent_stops(agent, [request], include_descendants=False)
        result = cleanup_subagent_tree(batch, task.id)
        current = agent.subagents.load(task.id)
    except HostAPIError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown") from None
    return {"subagent_id": task.id, "stopped": result["ok"] and current.status == "CANCELLED",
            "status": _project(current)["status"], "revision": current.state_revision, "confirmed": result["ok"]}
# LLM: 只消费原Store验证过的版本/出生身份；force不绕owner或确认，缺权威与未确认分别保留。
# 函数用途: 停止本owner准确后台会话，绝不把PID当句柄或直接kill。
def stop_background_process(context, payload):
    home = context.owner_home.home_dir
    store = ProcessSessionStore(process_session_store_root(home, home))
    report = store.load(payload.get("session_id", payload.get("process_id")))
    if report.load_error:
        raise HostAPIError("STORE_UNAVAILABLE", 503)
    record, owner = report.record, context.owner_home.owner_id
    scope = record.get("access_scope", {})
    if not record or scope.get("owner_id") != owner or scope.get("owner_home") != str(home):
        raise HostAPIError("PROCESS_NOT_FOUND", 404)
    try:
        result = stop_process_session(store, record)
    except ProcessSessionCleanupError:
        raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown") from None
    if result.authority_missing:
        raise HostAPIError("PROCESS_NOT_FOUND", 404)
    return {"session_id": record["session_id"], "stopped": result.confirmed,
            "status": result.record["status"], "confirmed": result.confirmed}
