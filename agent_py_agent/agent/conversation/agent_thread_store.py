# LLM: 子代理精确线程通过 ThreadStore 写入原目录；已有线程补齐必须在原线程锁内读最新状态，不能覆盖并发模型选择或 Compact。
# 模块用途: 按稳定运行身份创建或恢复子代理线程，保留原 owner、谱系和目录冲突校验。
"""Agent ConversationThread exact-id materialization and collision checks."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from ..gateway_parts.io import locked_file_transition
from ..runtime_errors import DataCorruptionError
from ..settings.thread_model_selection import SUBAGENT_MODEL_ADVICE_KEY, PendingSubagentModelAdvice
from .models import ConversationThread
from .store_io import now, read_json_object_report, safe_file_stem
from .store_threads import (
    normalized_runtime_workspace_roots,
)

# 仅真正新建且携带宿主建议的 child 获得资格；旧 thread / 旧 pending 绝不补发。
SUBAGENT_FIRST_REQUEST_KEY = "host_subagent_first_request.v1"

if TYPE_CHECKING:
    from .store_threads import ThreadStore


# LLM: Agent threads use a host-generated exact id and no channel/user indexes. Existing
# records are re-read under the original thread transaction before filling missing identity;
# the transition lock only serializes materialization and cannot guard model/Compact updates.
# LLM: typed advice 与首次请求资格仅初始化新 thread；existing 不重放 pending/资格；资格不等于尚未发送证明，发送须另走原子栅栏。
# 函数用途: 为确定运行物化独立线程及待验证建议，已有记录只在原线程锁内补缺失身份。
def ensure_agent_thread_record(
    store: ThreadStore,
    request: dict[str, Any],
) -> ConversationThread:
    thread_id = str(request.get("thread_id") or "").strip()
    agent_run_id = str(request.get("agent_run_id") or "").strip()
    if not thread_id or safe_file_stem(thread_id) != thread_id:
        raise ValueError("agent thread_id must be a safe exact identifier")
    if not agent_run_id:
        raise ValueError("agent_run_id is required")
    path = store.storage.thread_path(thread_id)
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
        payload, error = read_json_object_report(
            path,
            context="conversation.agent_thread.read",
        )
        if error is not None:
            raise DataCorruptionError(str(error))
        if payload:
            return store.update_atomic(
                thread_id,
                lambda latest: _ensure_existing_agent_thread(
                    latest,
                    request=request,
                    expected_metadata=expected_metadata,
                    current=current,
                ),
            )
        advice = request.get("model_advice")
        if advice is not None:
            if not isinstance(advice, PendingSubagentModelAdvice):
                raise ValueError("subagent model advice requires a host preparation")
            expected_metadata[SUBAGENT_MODEL_ADVICE_KEY] = advice.pending_metadata(run_id=agent_run_id, thread_id=thread_id)
            expected_metadata[SUBAGENT_FIRST_REQUEST_KEY] = {
                "schema": SUBAGENT_FIRST_REQUEST_KEY, "status": "unsubmitted",
                "operation_id": advice.operation_id, "child_run_id": agent_run_id,
                "child_thread_id": thread_id,
            }
        thread = ConversationThread(
            thread_id=thread_id,
            canonical_user_id=str(
                request.get("canonical_user_id") or agent_run_id
            ).strip(),
            owner_id=str(request.get("owner_id") or "").strip(),
            owner_home=str(request.get("owner_home") or "").strip(),
            model_profile_id=str(request.get("model_profile_id") or "default"),
            model_selection_revision=1,
            model_selection_source="inherited",
            model_selection_last_explicit_revision=0,
            # 子代理档位只在物化新线程时写入一次；已有线程（恢复、重放）保持创建时的档位。
            reasoning_effort=str(request.get("reasoning_effort") or ""),
            title=str(request.get("title") or agent_run_id).strip()[:240],
            created_at=current,
            updated_at=current,
            cwd=str(request.get("cwd") or "").strip(),
            runtime_workspace_roots=normalized_runtime_workspace_roots(
                request.get("runtime_workspace_roots")
            ),
            metadata=expected_metadata,
        )
        store.write(thread)
        return thread


# LLM: Called only with the current record from ThreadStore.update_atomic; preserve model,
# Compact, pending decision metadata and unknown extension fields. Changed lineage is a collision.
# 函数用途: 在原线程文件锁中补缺失身份，确需补空模型时同次前进继承版本；不补首次资格、不自行写盘。
def _ensure_existing_agent_thread(
    thread: ConversationThread,
    *,
    request: dict[str, Any],
    expected_metadata: dict[str, object],
    current: float,
) -> ConversationThread:
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
        model_selection_revision=thread.model_selection_revision + (not thread.model_profile_id),
        model_selection_source=thread.model_selection_source if thread.model_profile_id else "inherited",
        title=thread.title or str(request.get("title") or "").strip()[:240],
        cwd=thread.cwd or requested_cwd,
        runtime_workspace_roots=thread.runtime_workspace_roots or requested_roots,
        metadata=metadata,
        updated_at=max(thread.updated_at, current),
    )
    return updated
