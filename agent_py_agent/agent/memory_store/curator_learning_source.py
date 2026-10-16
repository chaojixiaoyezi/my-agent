# LLM: 每条消息/工具/artifact引用独立核宿主来源，任何未知或自动化引用都不可借同批用户消息洗白。
# 模块用途: 为策展候选绑定完整引用集合的学习资格。
"""Curator候选引用的逐来源资格核对；资格不向同批未核对引用扩散。"""
from __future__ import annotations

import json
from dataclasses import replace

from ..conversation.learning_source import (
    facts_for_message,
    merge_message_sources,
    merge_source_projections,
    unknown_projection,
)
from .learning_source_authority import ConversationLearningSources


# LLM: audit来源冻结在模型调用前；只用当前owner权威线程/请求，不接受模型结果提供投影。
# 函数用途: 为本批每个运行事件补上精确请求的学习资格。
def attach_audit_sources(batch: object, store: object, owner: str) -> object:
    sources = ConversationLearningSources(store, owner)
    events = tuple(replace(event, learning_source=sources.request(event.thread_id, event.request_id) if owner else unknown_projection())
                   for event in batch.audit_events)
    return replace(batch, audit_events=events)


# LLM: 每个引用必须唯一定位本批权威消息/事件；任意缺失/歧义/不符都关闭，而非借另一条可学引用授权。
# 函数用途: 解析一条消息来源并保留其已证明的任务与运行归属。
def _message_source(ref: dict, batch: object, owner: str) -> tuple[dict, object | None]:
    matches = [message for message in batch.messages
               if message.message_id == ref.get("message_id") and message.thread_id == ref.get("thread_id")]
    if len(matches) != 1:
        return unknown_projection(), None
    message = matches[0]
    return merge_message_sources([facts_for_message(message.metadata, owner=owner, thread_id=message.thread_id,
                                                    thread_source=message.thread_source)]), message


# LLM: 工具与artifact各自精确定位其audit事件；消息A永远不能替audit B提供资格。
# 函数用途: 读取已冻结的工具或产物来源投影。
def _audit_source(ref: dict, batch: object, kind: str) -> tuple[dict, object | None]:
    matches = [event for event in getattr(batch, "audit_events", ()) if event.event_id == ref.get("event_id")]
    if len(matches) != 1:
        return unknown_projection(), None
    event = matches[0]
    keys = ("artifact_ref", "run_id", "task_id") if kind == "artifact" else ("operation_id", "tool_call_id", "run_id", "task_id", "thread_id", "request_id")
    if any(str(ref.get(key) or "") != str(getattr(event, key, "") or "") for key in keys):
        return unknown_projection(), None
    return getattr(event, "learning_source", None) or unknown_projection(), event


# LLM: evidence_refs是规范消息/工具/产物引用及它们的task/run投影；额外引用不能被总投影自动标成已核对。
# 函数用途: 核查通用证据与运行身份确实来自逐条核对过的来源。
def _references_covered(item: object, refs: list[dict], task_ids: set[str], run_ids: set[str]) -> bool:
    if not set(getattr(item, "source_task_ids", ()) or ()).issubset(task_ids):
        return False
    if not set(getattr(item, "source_run_ids", ()) or ()).issubset(run_ids):
        return False
    allowed = {json.dumps(ref, sort_keys=True) for ref in refs}
    allowed.update(json.dumps({"ref_id": f"task:{value}", "task_id": value}, sort_keys=True) for value in task_ids)
    allowed.update(json.dumps({"ref_id": f"run:{value}", "run_id": value}, sort_keys=True) for value in run_ids)
    return all(json.dumps(ref, sort_keys=True) in allowed for ref in getattr(item, "evidence_refs", ()) or ())


# LLM: 展平引用迭代，不过滤坏引用；聚合仅在所有来源已独立核对后进行，未知引用不能被其它来源授权。
# 函数用途: 为Curator候选生成覆盖全部引用的保守来源投影。
def candidate_learning_source(item: object, batch: object, owner: str) -> dict[str, object]:
    if not owner:
        return unknown_projection()
    projections: list[dict] = []
    refs: list[dict] = []
    task_ids: set[str] = set()
    run_ids: set[str] = set()
    for kind, ref in _candidate_source_refs(item):
        if not isinstance(ref, dict):
            return unknown_projection()
        projection, source = _message_source(ref, batch, owner) if kind == "message" else _audit_source(ref, batch, kind)
        if source is None:
            return unknown_projection()
        projections.append(projection)
        refs.append(ref)
        identities = source.metadata if kind == "message" else {"task_id": source.task_id, "run_id": source.run_id}
        if identities.get("task_id"):
            task_ids.add(str(identities["task_id"]))
        if identities.get("run_id"):
            run_ids.add(str(identities["run_id"]))
    if not projections or not _references_covered(item, refs, task_ids, run_ids):
        return unknown_projection()
    return merge_source_projections(projections)


# LLM: 顺序及空值语义与原双循环相同，不把非字典引用静默过滤。
# 函数用途: 展开候选三类来源供统一逐条核验。
def _candidate_source_refs(item: object):
    for kind, attr in (("message", "source_message_refs"), ("tool", "source_tool_refs"), ("artifact", "source_artifact_refs")):
        for ref in getattr(item, attr, ()) or ():
            yield kind, ref
