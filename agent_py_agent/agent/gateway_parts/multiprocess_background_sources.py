# LLM: 只读原会话观察账及其唯一handled回执；不创建source账，不按summary/模型正文派工，坏源不当空成功。
# 原learning FIFO与唯一registry维护共用原来源/短写门；mtime仅CAS，维护不消费新FIFO/调用模型或确认候选。
# 模块用途: 将原无wake观察来源解析为有限的metadata事实，供原Gateway运输桥复读。
from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from pathlib import Path

from ..common.nofollow_fs import open_directory_beneath, read_bytes_beneath
from ..common.strict_json import load_strict_json
from ..conversation.models import ObservationEvent
from ..conversation.observation_index import ObservationIndex
from ..conversation.store_layout import ConversationStorage


# LLM: legacy与原ack journal共同确认；只查当前分片ID，投影同步不迁移legacy或初始化会话目录。
# 函数用途: 合并观察确认并冻结原legacy字节与journal身份，revision不替代consumer/claim授权。
def observation_handled(home, store_ref, max_bytes, *, observation_ids=()):
    relative = Path(store_ref).parts + ("observation_handled.json",)
    raw = read_bytes_beneath(home, relative, max_bytes=max_bytes, require_dir_fd=True)
    handled = {} if raw is None else load_strict_json(raw)
    if type(handled) is not dict or any(type(key) is not str or type(value) not in (int, float)
                                      or not math.isfinite(value) or value < 0 for key, value in handled.items()):
        raise ValueError("background_observation_handled_invalid")
    root = Path(home) / store_ref
    if not root.resolve().is_relative_to(Path(home).resolve()):
        raise ValueError("background_observation_store_outside_home")
    identity = _observation_journal_identity(home, Path(store_ref))
    if observation_ids:
        index = ObservationIndex(ConversationStorage(root, initialize=False))
        for key, value in index.handled(list(observation_ids), include_legacy=False).items():
            handled[key] = max(handled.get(key, 0), value)
    revision = hashlib.sha256(b"legacy:" + (raw if raw is not None else b"missing")
                              + b"\0journal:" + json.dumps(identity).encode()).hexdigest()
    return handled, revision


# LLM: journal的父链与叶均no-follow；只读stat且所有出口关闭FD，缺失不创建来源。
# 函数用途: 捕获原确认日志的文件身份，不跟随home内外的链接。
def _observation_journal_identity(home, store):
    try:
        parent = open_directory_beneath(home, store.parent.parts, require_dir_fd=True)
    except FileNotFoundError:
        return None
    try:
        try:
            info = os.stat(f".{store.name}-observation-acks.jsonl", dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("background_observation_journal_invalid")
        return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
    finally:
        os.close(parent)


# LLM: 整份原JSONL每行都核原ObservationEvent身份与显式bool；有限预算超限由读者拒绝，不能过滤坏行得到假完整源。
# 函数用途: 提取一个线程原观察分片中尚需主代理处理的观察。
def observation_payload(raw, thread_id, handled):
    rows = []
    for line in raw.splitlines():
        value = load_strict_json(line)
        if type(value) is not dict or any(type(value.get(key, False)) is not bool
                                        for key in ("requires_main_agent", "requires_llm_report")):
            raise ValueError("background_observation_invalid")
        event = ObservationEvent.from_dict(value)
        if event.thread_id != thread_id or not event.observation_id or not math.isfinite(event.handled_at):
            raise ValueError("background_observation_identity_invalid")
        confirmed = max(event.handled_at, handled.get(event.observation_id, 0))
        if confirmed <= 0 and (event.requires_main_agent or event.requires_llm_report):
            rows.append(event.to_dict())
    return {"observations": rows, "thread_id": thread_id}


# LLM: policy只从原owner retention.json严格v2解析；revision冻结完整字节，不从summary或新policy复活旧计划。
# 函数用途: 读取维护来源的原策略及原输入字节身份。
def maintenance_policy(home, max_bytes):
    from ..memory_store.retention_models import MemoryRetentionPolicy

    raw = read_bytes_beneath(home, ("retention.json",), max_bytes=max_bytes, require_dir_fd=True)
    if raw is None:
        raise ValueError("background_maintenance_policy_missing")
    policy = MemoryRetentionPolicy.from_payload(load_strict_json(raw))
    return policy, hashlib.sha256(raw).hexdigest()


# LLM: 原维护state缺失是尚未维护，不是任意损坏的空成功；已有state必须原schema/有限nonnegative时间，
#   且同时识别已提交的maintenance_partial与有限配额账本待续作状态，使生产C发现层与领域执行层共用同一结构化到期语义。
# 函数用途: 校验原维护状态，并按原typed策略及未完成片/配额待对账事实判断是否到期。
def maintenance_due(state, policy, now, *, owner_home=None, quota_pending=False):
    from ..user_space import owner_quota
    from ..user_space.home_retention import maintenance_fence

    if state:
        if state.get("schema_version") != "owner-maintenance.v1":
            raise ValueError("background_maintenance_state_invalid")
        last = state.get("last_attempt_at")
        if type(last) not in (int, float) or not math.isfinite(last) or last < 0:
            raise ValueError("background_maintenance_state_invalid")
        partial = state.get("maintenance_partial", False)
        if type(partial) is not bool:
            raise ValueError("background_maintenance_state_invalid")
        storage = state.get("storage_maintenance", {})
        if type(storage) is not dict:
            raise ValueError("background_maintenance_state_invalid")
        maintenance_fence(state)
        maintenance_fence(storage)
    else:
        last = 0
        partial = False
    if not (policy.maintenance_enabled and policy.maintenance_interval_seconds > 0):
        return False
    if last <= 0 or now - last >= policy.maintenance_interval_seconds:
        return True
    pending = (
        bool(quota_pending)
        if owner_home is None
        else owner_quota.owner_quota_reconcile_pending(owner_home, require_ledger=True)
    )
    return (partial or pending) and now > last and owner_quota.time.time() > last


# LLM: 复用原严格no-follow普通单链接fd；同一次读取绑定完整字节及mtime，竞态/超预算拒绝，不跟链接或创建来源文件。
# 函数用途: 为原学习registry捕获可复核的维护代次，mtime只限运输CAS不授owner/执行权。
def learning_registry_snapshot(home, max_bytes):
    from ..common.nofollow_fs import open_readonly_file_beneath

    descriptor = open_readonly_file_beneath(home, ("data", "skill_learning", "registry.json"))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        raw = stream.read(max_bytes + 1)
        after = os.fstat(stream.fileno())
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if (len(raw) > max_bytes or len(raw) != after.st_size
            or tuple(getattr(before, key) for key in fields) != tuple(getattr(after, key) for key in fields)):
        raise ValueError("background_learning_registry_changed")
    return raw, str(after.st_mtime_ns)


# LLM: 原RuntimeDB只读投影；任一健康任务仍可使混合观察候选入队，具体执行由claim同事务再次核验。
# 函数用途: 为C发现与W执行共用恢复资格，全部待接手时只保留原来源。
def source_recovery_block(home, kind, payload):
    from ..runtime_db.repository import RuntimeRepository

    if not (Path(home) / "runtime.db").is_file():
        return None
    rows = payload.get("observations", ()) if kind == "observation" else [payload]
    tasks = {str(row.get("root_task_id") or row.get("task_id") or "") for row in rows}
    repository = RuntimeRepository(str(Path(home) / "runtime.db"), read_only=True)
    blocked = []
    for task in tasks:
        block = repository.main_agent_recovery_block_for_task(task)
        if block is None:
            return None
        blocked.append(block)
    return blocked[0] if blocked else None
