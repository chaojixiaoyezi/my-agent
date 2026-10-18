# LLM: 只读原会话观察账及其唯一handled回执；不创建source账，不按summary/模型正文派工，坏源不当空成功。
# 原learning FIFO与唯一registry维护共用原来源/短写门；mtime仅CAS，维护不消费新FIFO/调用模型或确认候选。
# 模块用途: 将原无wake观察来源解析为有限的metadata事实，供原Gateway运输桥复读。
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path

from ..common.nofollow_fs import read_bytes_beneath
from ..common.strict_json import load_strict_json
from ..conversation.models import ObservationEvent


# LLM: 原handled.json是唯一观察确认源；revision只用于来源换代，不能取代原consumer/claim授权。
# 函数用途: 完整读取原观察确认账并返回其稳定字节身份。
def observation_handled(home, store_ref, max_bytes):
    relative = Path(store_ref).parts + ("observation_handled.json",)
    raw = read_bytes_beneath(home, relative, max_bytes=max_bytes, require_dir_fd=True)
    handled = {} if raw is None else load_strict_json(raw)
    if type(handled) is not dict or any(type(key) is not str or type(value) not in (int, float)
                                      or not math.isfinite(value) or value < 0 for key, value in handled.items()):
        raise ValueError("background_observation_handled_invalid")
    revision = hashlib.sha256(b"missing" if raw is None else b"present:" + raw).hexdigest()
    return handled, revision


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


# LLM: 原维护state缺失是尚未维护，不是任意损坏的空成功；已有state必须原schema/有限nonnegative时间。
# 函数用途: 校验原维护状态，并按原typed策略判断是否到期。
def maintenance_due(state, policy, now):
    if state:
        if state.get("schema_version") != "owner-maintenance.v1":
            raise ValueError("background_maintenance_state_invalid")
        last = state.get("last_attempt_at")
        if type(last) not in (int, float) or not math.isfinite(last) or last < 0:
            raise ValueError("background_maintenance_state_invalid")
    else:
        last = 0
    return (policy.maintenance_enabled and policy.maintenance_interval_seconds > 0
            and (last <= 0 or now - last >= policy.maintenance_interval_seconds))


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
