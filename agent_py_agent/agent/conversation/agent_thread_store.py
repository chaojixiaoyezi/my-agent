"""Agent ConversationThread exact-id materialization and collision checks."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from ..gateway_parts.io import locked_file_transition
from ..runtime_errors import DataCorruptionError
from .models import ConversationThread
from .store import (
    _read_json_object_report,
    normalized_runtime_workspace_roots,
    now,
    safe_file_stem,
)

if TYPE_CHECKING:
    from .store import ConversationThreadStore


# LLM: This module owns exact-id agent thread creation separately from the channel-bound
# ConversationThreadStore class. Keep lineage/owner/workspace collision checks fail-closed;
# never create channel or user indexes for child/grandchild agent threads.
# 模块用途: 按稳定运行身份创建或恢复子代理独立会话线程，并阻止它与用户聊天线程串线。


# LLM: Agent threads use a host-generated exact id and no channel/user indexes. Existing
# ids must match the same run lineage; this is the only durable materializer used by
# child/grandchild transcript Compact and resume.
# 函数用途: 为一个确定的子代理运行创建或校验独立会话线程，不把它绑定成用户聊天会话。
def ensure_agent_thread_record(
    store: ConversationThreadStore,
    request: dict[str, Any],
) -> ConversationThread:
    thread_id = str(request.get("thread_id") or "").strip()
    agent_run_id = str(request.get("agent_run_id") or "").strip()
    if not thread_id or safe_file_stem(thread_id) != thread_id:
        raise ValueError("agent thread_id must be a safe exact identifier")
    if not agent_run_id:
        raise ValueError("agent_run_id is required")
    path = store._thread_path(thread_id)
    transition = path.with_name(f".{path.name}.agent-thread")
    current = now(request.get("now"))
    expected_metadata = {
        "thread_kind": "agent",
        "agent_run_id": agent_run_id,
        "parent_agent_thread_id": str(
            request.get("parent_agent_thread_id") or ""
        ).strip(),
        "root_agent_thread_id": str(
            request.get("root_agent_thread_id") or thread_id
        ).strip(),
        "agent_depth": max(0, int(request.get("agent_depth") or 0)),
    }
    with locked_file_transition(transition):
        payload, error = _read_json_object_report(
            path,
            context="conversation.agent_thread.read",
        )
        if error is not None:
            raise DataCorruptionError(str(error))
        if payload:
            return _ensure_existing_agent_thread(
                store,
                payload,
                request=request,
                expected_metadata=expected_metadata,
                current=current,
            )
        thread = ConversationThread(
            thread_id=thread_id,
            canonical_user_id=str(
                request.get("canonical_user_id") or agent_run_id
            ).strip(),
            owner_id=str(request.get("owner_id") or "").strip(),
            owner_home=str(request.get("owner_home") or "").strip(),
            model_profile_id=str(request.get("model_profile_id") or "default"),
            title=str(request.get("title") or agent_run_id).strip()[:240],
            created_at=current,
            updated_at=current,
            cwd=str(request.get("cwd") or "").strip(),
            runtime_workspace_roots=normalized_runtime_workspace_roots(
                request.get("runtime_workspace_roots")
            ),
            metadata=expected_metadata,
        )
        store._write_thread(thread)
        return thread


# LLM: Idempotent migration may fill only missing descriptive scope on the same exact
# agent thread. A channel binding, owner mismatch, run mismatch, or changed lineage is a
# collision and must never be silently converted into the requested child thread.
# 函数用途: 校验已存在的子代理线程身份，并只补齐旧记录缺失的目录和元数据。
def _ensure_existing_agent_thread(
    store: ConversationThreadStore,
    payload: dict[str, Any],
    *,
    request: dict[str, Any],
    expected_metadata: dict[str, object],
    current: float,
) -> ConversationThread:
    thread = ConversationThread.from_dict(payload)
    expected_id = str(request.get("thread_id") or "").strip()
    if thread.thread_id != expected_id:
        raise DataCorruptionError(f"agent thread identity is invalid: {expected_id}")
    if thread.channel_bindings:
        raise DataCorruptionError(
            f"agent thread collides with a channel-bound thread: {expected_id}"
        )
    metadata = dict(thread.metadata or {})
    existing_kind = str(metadata.get("thread_kind") or "").strip()
    existing_run_id = str(metadata.get("agent_run_id") or "").strip()
    if existing_kind not in {"", "agent"} or existing_run_id not in {
        "",
        str(expected_metadata["agent_run_id"]),
    }:
        raise DataCorruptionError(f"agent thread run identity conflicts: {expected_id}")
    for key in ("parent_agent_thread_id", "root_agent_thread_id"):
        existing = str(metadata.get(key) or "").strip()
        expected = str(expected_metadata.get(key) or "").strip()
        if existing and expected and existing != expected:
            raise DataCorruptionError(
                f"agent thread lineage conflicts: {expected_id}:{key}"
            )
    expected_owner = str(request.get("owner_id") or "").strip()
    expected_owner_home = str(request.get("owner_home") or "").strip()
    if thread.owner_id and expected_owner and thread.owner_id != expected_owner:
        raise DataCorruptionError(f"agent thread owner conflicts: {expected_id}")
    if (
        thread.owner_home
        and expected_owner_home
        and thread.owner_home != expected_owner_home
    ):
        raise DataCorruptionError(f"agent thread owner home conflicts: {expected_id}")
    requested_cwd = str(request.get("cwd") or "").strip()
    requested_roots = normalized_runtime_workspace_roots(
        request.get("runtime_workspace_roots")
    )
    if thread.cwd and requested_cwd and thread.cwd != requested_cwd:
        raise DataCorruptionError(f"agent thread cwd conflicts: {expected_id}")
    if (
        thread.runtime_workspace_roots
        and requested_roots
        and thread.runtime_workspace_roots != requested_roots
    ):
        raise DataCorruptionError(
            f"agent thread workspace roots conflict: {expected_id}"
        )
    metadata.update(
        {
            key: value
            for key, value in expected_metadata.items()
            if value not in {"", None}
        }
    )
    updated = replace(
        thread,
        canonical_user_id=(
            thread.canonical_user_id
            or str(request.get("canonical_user_id") or "").strip()
        ),
        owner_id=thread.owner_id or expected_owner,
        owner_home=thread.owner_home or expected_owner_home,
        model_profile_id=thread.model_profile_id or str(request.get("model_profile_id") or "default"),
        title=thread.title or str(request.get("title") or "").strip()[:240],
        cwd=thread.cwd or requested_cwd,
        runtime_workspace_roots=thread.runtime_workspace_roots or requested_roots,
        metadata=metadata,
        updated_at=max(thread.updated_at, current),
    )
    if updated != thread:
        store._write_thread(updated)
    return updated
