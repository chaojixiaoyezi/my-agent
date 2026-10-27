# LLM: 内部适配仅接宿主context/agent；W4/W6须先核来源、AND能力、线程/任务范围及幂等，禁止payload注入manager。
# 模块用途: 冷读canonical子代理，沿原短事务/CAS投递指导和精确取消，不新建执行身份或影子状态。
from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

from ..common.nofollow_fs import open_directory_beneath, read_bytes_beneath
from ..concurrency.exceptions import ConcurrencyConflictError
from ..subagents.cancellation import (
    CancelSubagentTaskRequest,
    cleanup_subagent_tree,
    prepare_subagent_stops,
)
from ..subagents.services.persistence.service import SubAgentPersistenceService
from ..tooling.process_session_cleanup import ProcessSessionCleanupError, stop_process_session
from ..tooling.process_session_store import ProcessSessionStore, process_session_store_root
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
# LLM: 展示状态映射不回写；未知时间/模型保持None，不从profile ID冒充模型名。
# 函数用途: 投影原任务公开摘要及原canonical修订。
def _project(task):
    attrs, status = task.attributes or {}, task.status
    model = attrs.get("host_model_profile.v1") or {}
    effort = attrs.get("host_reasoning_effort.v1") or {}
    return {"subagent_id": task.id, "run_id": task.id, "parent_run_id": task.parent_id,
            "thread_id": attrs.get("conversation_thread_id") or task.runtime_identity.conversation_id or task.agent_thread_id,
            "name": task.agent_name, "role": task.role, "model_name": model.get("model_name"),
            "reasoning_effort": effort.get("level"), "status": {"DONE": "completed", "TIMEOUT": "failed",
            "CHANNEL_ERROR": "failed"}.get(status, status.lower()), "current_action": task.current_tool or task.current_step,
            "started_at": task.created_at or None, "elapsed_seconds": max(0, (task.ended_at or time.time()) - task.created_at)
            if task.created_at else None, "revision": task.state_revision}
# LLM: 列表只公开已核行，错误显式拒绝；未结束过滤按原状态集合，不按中文摘要猜测。
# 函数用途: 列出指定线程/根任务的子代理。
def list_subagents(context, payload, *, agent=None, store=None):
    if not payload.get("thread_id"):
        raise HostAPIError("INVALID_SCHEMA")
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
                rows.append(_project(task))
    return {"items": rows}
# LLM: 只从任务原日志字段nofollow有界读取；消息来自宿主注入的原ConversationStore，不从请求接受文件地址。
# 函数用途: 返回目标指令、真实时间线、消息和结果摘要。
def get_subagent(context, payload, *, agent=None, store=None):
    task = _load(context, payload, agent)
    steps = []
    if task.agent_run_timeline_jsonl:
        parts = Path(task.agent_run_timeline_jsonl).relative_to(context.owner_home.home_dir).parts
        raw = read_bytes_beneath(context.owner_home.home_dir, parts, max_bytes=262144)
        steps = (raw or b"").decode("utf-8").splitlines()[-100:]
    messages, errors = store.messages.recent_report(task.agent_thread_id, limit=100) if store and task.agent_thread_id else ([], [])
    if errors:
        raise HostAPIError("STORE_UNAVAILABLE", 503)
    return {**_project(task), "instruction": task.goal, "steps": steps,
            "messages": [row.to_dict() for row in messages], "output_summary": task.result or task.latest_summary}
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
