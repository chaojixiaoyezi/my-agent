# LLM: 有稳定去重键的发布以原 dedupe 文件冻结完整信号和观察；同锁先修原发布再准入下一代。
#   本模块只管理发布安装，不消费唤醒、不运行模型、不扫描另一个队列；查询绝不修账。
# 模块用途: 让唤醒与观察在文件半写后重试同一份内容，避免重发、丢观察或把坏账当空账。
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..common.json_io import write_json_file_atomic_unlocked
from ..common.opaque_id import validate_opaque_id
from ..gateway_parts.io import locked_file_transition, write_json_file_atomic
from ..io.jsonl import append_jsonl
from ..runtime_errors import DataCorruptionError
from .models import ObservationEvent, WakeSignal
from .store_io import read_jsonl_report
from .store_layout import ConversationStorage

_SCHEMA = "wake_dedupe.v2"
_LEGACY_SCHEMA = "wake_dedupe.v1"


# LLM: 原文件通过原子替换发布，查询可直接读完整快照；不创建锁文件，只有不存在才是空账。
# 函数用途: 严格读取发布或信号记录，损坏、权限或其它读取错误均向调用方报错。
def _read_record(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise DataCorruptionError("wake publication record is unreadable") from exc
    if not isinstance(payload, dict) or not payload:
        raise DataCorruptionError("wake publication record is invalid")
    return payload


# LLM: ID 必须符合原通用不透明身份合同，读取持久记录不能借路径字段跨出队列。
# 函数用途: 从结构化记录建立信号对象并核对基本身份与状态。
def _signal(payload: dict[str, Any]) -> WakeSignal:
    signal = WakeSignal.from_dict(payload)
    validate_opaque_id(signal.wake_signal_id, kind="wake_signal_id")
    if not signal.thread_id or signal.status not in {"pending", "handled"}:
        raise DataCorruptionError("wake publication signal identity is invalid")
    return signal


# LLM: 投递冻结只允许增加 owner_delivery，消费只改变 status/handled_at；其余信号内容不可漂移。
# 函数用途: 提取用于对账的信号不可变字段，不把处理时间当发布冲突。
def _stable_signal(signal: WakeSignal) -> dict[str, Any]:
    payload = signal.to_dict()
    payload.pop("status")
    payload.pop("handled_at")
    metadata = dict(payload["metadata"])
    metadata.pop("owner_delivery", None)
    payload["metadata"] = metadata
    return payload


# LLM: 先查 pending 再查 handled，消费先落 handled 后删 pending，不能把消费交接误当信号丢失。
# 函数用途: 严格读回固定信号，保留合法的已消费事实并拒绝同 ID 的内容冲突。
def _installed_signal(storage: ConversationStorage, frozen: WakeSignal) -> WakeSignal | None:
    paths = (storage.wake_signal_path(frozen), storage.wake_handled_dir / f"{frozen.wake_signal_id}.json")
    for path in paths:
        payload = _read_record(path)
        if payload is None:
            continue
        existing = _signal(payload)
        if _stable_signal(existing) != _stable_signal(frozen):
            raise DataCorruptionError("wake publication signal conflicts with frozen content")
        return existing
    return None


# LLM: 同一观察 ID 只能对应一行完整原负载；原账损坏时不能追加后假报修复成功。
# 函数用途: 从原观察账查固定事件，供安装重试和旧版回执迁移共同使用。
def _find_observation(storage: ConversationStorage, thread_id: str, observation_id: str) -> ObservationEvent | None:
    path = storage.observation_path(thread_id)
    if not path.exists():
        return None
    report = read_jsonl_report(path, context="conversation.wake_publication.observation")
    if report.load_errors:
        raise DataCorruptionError("wake publication observation ledger is unreadable")
    matching = [row for row in report.rows if row.get("observation_id") == observation_id]
    if len(matching) > 1:
        raise DataCorruptionError("wake publication observation identity is duplicated")
    return ObservationEvent.from_dict(matching[0]) if matching else None


# LLM: 发布记录冻结全部原内容；新请求不能给旧信号补新摘要，链接双方身份须完全一致。
# 函数用途: 校验 v2 发布记录并还原本次固定信号与可选配对观察。
def _frozen_pair(record: dict[str, Any], thread_id: str, key: str) -> tuple[ObservationEvent | None, WakeSignal]:
    if record.get("schema_version") != _SCHEMA or record.get("phase") not in {"prepared", "published"}:
        raise DataCorruptionError("wake publication schema or phase is invalid")
    if record.get("thread_id") != thread_id or record.get("dedupe_key") != key:
        raise DataCorruptionError("wake publication key identity conflicts")
    if not isinstance(record.get("signal"), dict) or type(record.get("retain_handled")) is not bool:
        raise DataCorruptionError("wake publication frozen signal or retention is invalid")
    signal = _signal(record["signal"])
    if (signal.thread_id, signal.dedupe_key, signal.wake_signal_id) != (thread_id, key, record.get("wake_signal_id")):
        raise DataCorruptionError("wake publication signal identity conflicts")
    raw_observation = record.get("observation")
    if raw_observation is None:
        return None, signal
    if not isinstance(raw_observation, dict):
        raise DataCorruptionError("wake publication frozen observation is invalid")
    observation = ObservationEvent.from_dict(raw_observation)
    if not observation.observation_id or (observation.thread_id, observation.observation_id, observation.wake_signal_id) != (
        thread_id, signal.observation_id, signal.wake_signal_id,
    ):
        raise DataCorruptionError("wake publication observation identity conflicts")
    return observation, signal


# LLM: 此记录仍位于原 dedupe 路径，prepared 是发布提交点；只有后续安装完成才可报告投递回执。
# 函数用途: 冻结本次信号和配对观察，生成可恢复的发布记录。
def _prepare(signal: WakeSignal, observation: ObservationEvent | None, retain_handled: bool) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA, "thread_id": signal.thread_id, "dedupe_key": signal.dedupe_key,
        "wake_signal_id": signal.wake_signal_id, "phase": "prepared", "retain_handled": retain_handled,
        "signal": replace(signal, status="pending", handled_at=0.0).to_dict(),
        "observation": replace(observation, wake_signal_id=signal.wake_signal_id).to_dict() if observation else None,
        "updated_at": signal.created_at,
    }


# LLM: v1 只能从其已发布信号和原观察升级；缺失内容不准从新请求伪造，查询不执行迁移。
# 函数用途: 在原锁内显式迁移健康的旧回执，保留旧信号身份及处理状态。
def _migrate_legacy(storage: ConversationStorage, record: dict[str, Any], incoming: WakeSignal,
                    *, paired: bool, retain_handled: bool) -> dict[str, Any]:
    if record.get("thread_id") != incoming.thread_id or record.get("dedupe_key") != incoming.dedupe_key:
        raise DataCorruptionError("legacy wake receipt identity conflicts")
    identity = str(record.get("wake_signal_id") or "")
    validate_opaque_id(identity, kind="wake_signal_id")
    existing = _read_existing_identity(storage, identity)
    if existing is None or (existing.thread_id, existing.dedupe_key) != (incoming.thread_id, incoming.dedupe_key):
        raise DataCorruptionError("legacy wake receipt has no matching signal")
    observation = _find_observation(storage, existing.thread_id, existing.observation_id) if paired else None
    if paired and (observation is None or observation.wake_signal_id != identity):
        raise DataCorruptionError("legacy wake receipt has no complete paired observation")
    migrated = {**_prepare(existing, observation, retain_handled), "phase": "published"}
    migrated["migration"] = {"from_schema": _LEGACY_SCHEMA, "wake_signal_id": identity}
    return migrated


# LLM: 旧版回执和只读查询按原两个队列及 handled 查精确 ID，不全局猜同名或内容。
# 函数用途: 读取一个旧信号身份，缺失与损坏保持不同结果。
def _read_existing_identity(storage: ConversationStorage, identity: str) -> WakeSignal | None:
    validate_opaque_id(identity, kind="wake_signal_id")
    paths = [storage.wake_queue_dir / kind / f"{identity}.json" for kind in ("urgent", "normal", "handled")]
    for path in paths:
        payload = _read_record(path)
        if payload is not None:
            signal = _signal(payload)
            if signal.wake_signal_id != identity:
                raise DataCorruptionError("wake receipt signal ID conflicts")
            return signal
    return None


# LLM: 安装只重放原 prepared 内容；已被消费的信号不重建，观察追加在原去重锁下按固定 ID 幂等。
# 函数用途: 补齐信号与观察后再保存 published，任何失败保留原发布供调用方重试。
def _install(storage: ConversationStorage, path: Path, record: dict[str, Any]) -> tuple[ObservationEvent | None, WakeSignal]:
    observation, frozen = _frozen_pair(record, record["thread_id"], record["dedupe_key"])
    installed = _installed_signal(storage, frozen)
    if installed is None:
        write_json_file_atomic(storage.wake_signal_path(frozen), frozen.to_dict())
        installed = frozen
    if observation is not None:
        existing = _find_observation(storage, frozen.thread_id, observation.observation_id)
        if existing is None:
            append_jsonl(storage.observation_path(frozen.thread_id), observation.to_dict(), sort_keys=True)
        elif existing.to_dict() != observation.to_dict():
            raise DataCorruptionError("wake publication observation conflicts with frozen content")
    write_json_file_atomic_unlocked(path, {**record, "phase": "published"})
    return observation, installed


# LLM: 相同键的判断、预留和安装共用原 dedupe 锁；prepared 重试永不跨代，published handled 才按策略新发。
# 函数用途: 发布或恢复一个有稳定键的信号及观察，返回原内容，不新增后台恢复服务。
def publish_deduped(storage: ConversationStorage, signal: WakeSignal, *, observation: ObservationEvent | None = None,
                     retain_handled: bool = False) -> tuple[ObservationEvent | None, WakeSignal]:
    if type(retain_handled) is not bool:
        raise ValueError("wake retain_handled must be boolean")
    path = storage.wake_dedupe_path(signal.thread_id, signal.dedupe_key)
    with locked_file_transition(path):
        record = _read_record(path)
        if record is not None and record.get("schema_version") == _LEGACY_SCHEMA:
            record = _migrate_legacy(storage, record, signal, paired=observation is not None, retain_handled=retain_handled)
            write_json_file_atomic_unlocked(path, record)
        if record is not None:
            previous_observation, frozen = _frozen_pair(record, signal.thread_id, signal.dedupe_key)
            if record["retain_handled"] != retain_handled or (previous_observation is None) != (observation is None):
                raise DataCorruptionError("wake publication retention or pair contract conflicts")
            if record["phase"] == "prepared":
                return _install(storage, path, record)
            existing = _installed_signal(storage, frozen)
            if existing is None:
                raise DataCorruptionError("published wake signal is missing")
            if existing.status == "pending" or retain_handled:
                return previous_observation, existing
        migration = record.get("migration") if record is not None else None
        record = _prepare(signal, observation, retain_handled)
        if migration is not None:
            record["migration"] = migration
        _frozen_pair(record, signal.thread_id, signal.dedupe_key)
        write_json_file_atomic_unlocked(path, record)
        return _install(storage, path, record)


# LLM: 回执只读原子快照，不取写锁、不迁移或补安装；prepared 不算完整交付，坏账不能退化为空。
# 函数用途: 查询固定键是否已完整发布并处于 pending/handled，供观察界面与诊断使用。
def publication_receipt(storage: ConversationStorage, thread_id: str, key: str) -> str:
    if not thread_id or not key:
        return ""
    path = storage.wake_dedupe_path(thread_id, key)
    record = _read_record(path)
    if record is None:
        return ""
    if record.get("schema_version") == _LEGACY_SCHEMA:
        if record.get("thread_id") != thread_id or record.get("dedupe_key") != key:
            raise DataCorruptionError("legacy wake receipt identity conflicts")
        signal = _read_existing_identity(storage, str(record.get("wake_signal_id") or ""))
        if signal is None or (signal.thread_id, signal.dedupe_key) != (thread_id, key):
            raise DataCorruptionError("legacy wake receipt has no matching signal")
        return signal.status
    _, frozen = _frozen_pair(record, thread_id, key)
    if record["phase"] == "prepared":
        return ""
    signal = _installed_signal(storage, frozen)
    if signal is None:
        raise DataCorruptionError("published wake signal is missing")
    return signal.status
