# LLM: 共用原formal锁、writer与quota；根最后发布，GC只认本库分片及writer精确UUID暂存，不触碰未知文件。
# 模块用途: 管理同一长期记忆逻辑库的冷热代次、永久删除屏障和无损分层策略。
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from ..common.nofollow_fs import (
    ensure_private_dir,
    open_directory_beneath,
    read_bytes_beneath,
    read_text_beneath,
)
from .reference_pins import record_parsed_pin_bytes

GENERATION_SCHEMA = "memory-generation.v1"
TOMBSTONE_SCHEMA = "storage-tombstones.v1"


# LLM: 只解释唯一memory_policy.json，不写配置；未知扩展无损保留，库存0非法，注入0有效。
# 类用途: 为冷热库存与注入调用方提供同一份类型化预算。
@dataclass(frozen=True)
class MemoryPolicyV2:
    enabled: bool = True
    active_library_max_chars: int = 20000
    recall_max_chars: int = 4000
    hot_injection_max_chars: int = 5000
    _payload: dict = field(default_factory=dict, repr=False, compare=False)

    # LLM: v1只补默认值；未知schema和坏预算拒绝，不擅自把坏配置变成默认授权。
    # 函数用途: 校验并升读现有记忆政策，保留原排除名单和扩展。
    @classmethod
    def from_payload(cls, payload):
        if not isinstance(payload, dict) or payload.get("schema_version", "memory-policy.v1") not in {
                "memory-policy.v1", "memory-policy.v2"}:
            raise ValueError("MEMORY_POLICY_INVALID")
        budget = payload.get("budget", {})
        if not isinstance(budget, dict) or type(payload.get("enabled", True)) is not bool:
            raise ValueError("MEMORY_POLICY_INVALID")
        values = {name: budget.get(name, default) for name, default in (
            ("active_library_max_chars", 20000), ("recall_max_chars", 4000), ("hot_injection_max_chars", 5000))}
        if any(type(v) is not int or v < (1 if k == "active_library_max_chars" else 0)
               for k, v in values.items()):
            raise ValueError("MEMORY_POLICY_INVALID")
        return cls(enabled=payload.get("enabled", True), **values, _payload=deepcopy(payload))

    # LLM: 序列化不丢旧排除名单、修订号和未知字段，不把内部派生预算变成新旋钮。
    # 函数用途: 返回可交给原政策写入口的v2载荷。
    def to_payload(self):
        payload = deepcopy(self._payload)
        payload.update(schema_version="memory-policy.v2", enabled=self.enabled)
        payload["budget"] = {**payload.get("budget", {}), **{
            name: getattr(self, name) for name in
            ("active_library_max_chars", "recall_max_chars", "hot_injection_max_chars")}}
        return payload


# LLM: 只供既有prompt入口消费，0不删除事实；working agreement按有效recall的60%派生。
# 类用途: 区分每轮注入信封与长期库存预算。
@dataclass(frozen=True)
class ContextBudget:
    recall_max_chars: int
    hot_injection_max_chars: int

    # LLM: 模型窗口只能收紧已有预算，不能扩大；不读取模型或重新做授权。
    # 函数用途: 从同一政策派生本轮实际可用注入额度。
    @classmethod
    def from_policy(cls, policy, *, effective_recall_chars=None):
        effective = policy.recall_max_chars
        if effective_recall_chars is not None:
            if type(effective_recall_chars) is not int or effective_recall_chars < 0:
                raise ValueError("MEMORY_CONTEXT_BUDGET_INVALID")
            effective = min(effective, effective_recall_chars)
        return cls(effective, policy.hot_injection_max_chars)

    # LLM: 派生值使用整数运算，默认4000对应2400，不另持久化。
    # 函数用途: 为原工作约定注入入口提供字数子预算。
    @property
    def working_agreement_chars(self):
        return self.recall_max_chars * 3 // 5


# LLM: 不根据origin决定是否遗忘；管理员锁和pinned只固定位置，超长新条目可直接cold。
# 函数用途: 按字符预算与确定性热度分层，不改业务版本或正文。
def partition_records(records, max_chars, *, cold_ids=(), pinned_ids=()):
    if type(max_chars) is not int or max_chars < 1:
        raise ValueError("MEMORY_ACTIVE_BUDGET_INVALID")
    cold_ids, pinned_ids = set(cold_ids), set(pinned_ids)
    pinned_ids.update(r.entry_id for r in records if (r.attributes or {}).get("pinned") is True
                      or (r.attributes or {}).get("hot_placement") == "pinned")
    active = {r.entry_id: r for r in records if r.entry_id not in cold_ids or r.entry_id in pinned_ids}
    size = sum(len(r.content) for r in active.values())
    for record in sorted(active.values(), key=lambda r: (float(r.last_accessed_at or 0),
                         float(r.updated_at or 0), r.entry_id)):
        if size <= max_chars:
            break
        if record.entry_id not in pinned_ids:
            size -= len(record.content)
            del active[record.entry_id]
    return ([r for r in records if r.entry_id in active], [r for r in records if r.entry_id not in active])


# LLM: 此账不属于可回退根清单；只保存身份/hash/删除回执，所有恢复必须叠加当前账。
# 类用途: 给active、cold及任何派生候选提供永久删除屏障。
class StorageTombstones:
    # LLM: 路径与正式库basename绑定，owner身份由规范库路径摘要派生，不接外部owner自报。
    # 函数用途: 绑定当前逻辑库的独立删除账。
    def __init__(self, memory_path):
        self.memory_path = Path(memory_path)
        self.path = self.memory_path.with_name(self.memory_path.name + ".storage_tombstones.json")
        self.owner = hashlib.sha256(str(self.memory_path.absolute()).encode()).hexdigest()
        from .storage_tombstone_index import StorageTombstoneIndex

        self.index = StorageTombstoneIndex(self.memory_path, self.owner)

    # LLM: 冻结全量dict合同供retention使用；热召回走snapshot，不按历史删除数耗尽live预算。
    # 函数用途: 完整读取当前永久删除账，旧格式保持兼容且绝不把坏账当空。
    def load(self, *, max_bytes=None, on_read=None, _read=None, on_record=None):
        body, marker = self._marker(max_bytes=max_bytes, _read=_read)
        if marker.get("index_file") is None:
            return self._load_legacy(max_bytes=max_bytes, on_read=on_read,
                _read=lambda path: body if path == self.path else (_read(path) if _read else read_bytes_beneath(path.parent, (path.name,), max_bytes=max_bytes)),
                on_record=on_record)
        if on_read is not None:
            on_read(len(body or b""))
        with self.snapshot(_marker=marker) as snapshot:
            records = snapshot.full_dict()
        if on_record is not None:
            for _ in records:
                on_record()
        return records

    # LLM: 只读JSON小marker；其版本独立于可回退内容根，格式损坏不允许降级。
    # 函数用途: 校验旧账或指向永久索引的耐久标记。
    def _marker(self, *, max_bytes=None, _read=None, _allow_migration=False):
        body = _read(self.path) if _read else read_bytes_beneath(self.path.parent, (self.path.name,), max_bytes=max_bytes)
        record_parsed_pin_bytes(self.path, body)
        marker = json.loads(body) if body is not None else {"schema_version": TOMBSTONE_SCHEMA, "owner": self.owner, "records": {}}
        if (not isinstance(marker, dict) or marker.get("schema_version") != TOMBSTONE_SCHEMA
                or marker.get("owner") != self.owner or not isinstance(marker.get("records"), dict)):
            raise ValueError("MEMORY_TOMBSTONES_UNREADABLE")
        if "index_file" in marker and (marker["index_file"] != self.index.path.name
                or type(marker.get("index_revision")) is not int or marker["index_revision"] < 0 or marker["records"]):
            raise ValueError("MEMORY_TOMBSTONES_UNREADABLE")
        if "index_file" in marker and marker.get("legacy_stamp") != self._legacy_stamp():
            raise ValueError("MEMORY_TOMBSTONES_UNREADABLE")
        root_path = self.memory_path.with_name(self.memory_path.name + ".manifest.json")
        root_body = _read(root_path) if _read else read_bytes_beneath(root_path.parent, (root_path.name,), max_bytes=max_bytes)
        root = json.loads(root_body) if root_body is not None else {}
        if "barrier_revision" in root and "index_file" not in marker:
            raise ValueError("MEMORY_TOMBSTONES_UNREADABLE")
        # 迁移完成而marker改写失败时，只有回执证明索引恰为未变旧账的导入，才沿旧账读取；下一次正式写会补写marker。
        if ("index_file" not in marker and not _allow_migration
                and self.index.has_committed_authority()):
            receipt = self.index.migration_receipt()
            records = list(self._load_legacy(max_bytes=max_bytes,
                _read=lambda path: body if path == self.path else (_read(path) if _read else read_bytes_beneath(path.parent, (path.name,), max_bytes=max_bytes))).values())
            if (receipt is None or receipt.get("migrated_revision") != receipt["revision"]
                    or receipt.get("source_identity") != self._legacy_source_identity(body, records)):
                raise ValueError("MEMORY_TOMBSTONES_UNREADABLE")
        return body, marker

    # LLM: 迁移写入与marker回退核验共用唯一身份算法；只摘要旧JSON原字节和已严格解析的旧删除。
    # 函数用途: 计算一次旧格式永久账来源的稳定身份。
    def _legacy_source_identity(self, body, records) -> str:
        return hashlib.sha256((body or b"") + b"\0" + b"".join(r.to_json().encode() for r in records)).hexdigest()

    # LLM: 迁移后旧JSONL仍是冻结兼容来源；只核固定一个文件代次，变化不被误认为已导入索引。
    # 函数用途: 绑定旧账身份，避免升级后忽略被改坏的旧归档。
    def _legacy_stamp(self):
        from ._jsonl_indexing import _scan_stamp
        from .jsonl import _tombstone_path

        return json.loads(json.dumps(_scan_stamp(_tombstone_path(self.memory_path))))

    # LLM: 旧格式只在冷迁移或显式全量消费者读取；保留原预算、语义校验及pin字节绑定。
    # 函数用途: 升读升级前的JSON和JSONL永久账。
    def _load_legacy(self, *, max_bytes=None, on_read=None, _read=None, on_record=None):
        from ._jsonl_maintenance import require_maintenance_event
        from .jsonl import _memory_record_from_obj, _tombstone_path

        body = (_read(self.path) if _read is not None else
                read_bytes_beneath(self.path.parent, (self.path.name,), max_bytes=max_bytes))
        record_parsed_pin_bytes(self.path, body)
        used = len(body or b"")
        if on_read is not None:
            on_read(used)
        raw = body.decode("utf-8") if body is not None else None
        if raw is None:
            manifest = self.memory_path.with_name(self.memory_path.name + ".manifest.json")
            marker = (_read(manifest) if _read is not None else
                      read_text_beneath(self.path.parent, (manifest.name,)))
            if marker is not None:
                raise ValueError("MEMORY_TOMBSTONES_UNREADABLE")
        payload = json.loads(raw) if raw is not None else {
            "schema_version": TOMBSTONE_SCHEMA, "owner": self.owner, "records": {}}
        if (not isinstance(payload, dict) or payload.get("schema_version") != TOMBSTONE_SCHEMA
                or payload.get("owner") != self.owner or not isinstance(payload.get("records"), dict)):
            raise ValueError("MEMORY_TOMBSTONES_UNREADABLE")
        result = {}
        for entry_id, row in payload["records"].items():
            if on_record is not None:
                on_record()
            record = _memory_record_from_obj(row)
            require_maintenance_event(record)
            if record.action != "remove" or record.entry_id != entry_id or row.get("attributes"):
                raise ValueError("MEMORY_TOMBSTONES_UNREADABLE")
            result[entry_id] = record
        legacy = _tombstone_path(self.memory_path)
        body = (_read(legacy) if _read is not None else read_bytes_beneath(legacy.parent, (legacy.name,),
                max_bytes=None if max_bytes is None else max_bytes - used))
        record_parsed_pin_bytes(legacy, body)
        if on_read is not None:
            on_read(len(body or b""))
        text = body.decode("utf-8") if body is not None else None
        for line in (text or "").splitlines():
            if not line.strip():
                continue
            if on_record is not None:
                on_record()
            record = _memory_record_from_obj(json.loads(line))
            require_maintenance_event(record)
            if record.action != "remove":
                raise ValueError("MEMORY_TOMBSTONES_UNREADABLE")
            old = result.get(record.entry_id)
            selected = deepcopy(record if old is None or record.version >= old.version else old)
            selected.removed_content_hashes = tuple(sorted(set(record.removed_content_hashes)
                | set(old.removed_content_hashes if old else ())))
            result[record.entry_id] = selected
        return result

    # LLM: 调用方持原formal锁；仅合并删除，不能由旧备份清空新墓碑；返回现成字节供quota和writer。
    # 函数用途: 无损合并历史hash与最新删除身份，剥离一切正文扩展。
    def prepare(self, records):
        from .jsonl import _record_payload

        latest = self.load()
        for record in records:
            if record.action != "remove":
                continue
            old = latest.get(record.entry_id)
            hashes = set(record.removed_content_hashes)
            if old is not None:
                hashes.update(old.removed_content_hashes)
            selected = deepcopy(record if old is None or record.version >= old.version else old)
            selected.removed_content_hashes = tuple(sorted(hashes))
            latest[record.entry_id] = selected
        payload = {"schema_version": TOMBSTONE_SCHEMA, "owner": self.owner,
                   "records": {key: _record_payload(row) for key, row in sorted(latest.items())}}
        return latest, json.dumps(payload, ensure_ascii=False, sort_keys=True)

    # LLM: ID和正文hash均来自当前库删除账；lesson等跨线调用方可复用，不授读写权限。
    # 函数用途: 判断旧备份、候选或派生正文是否已被明确遗忘。
    def excludes(self, entry_id, content_hash="", *, records=None):
        if records is None:
            return entry_id in self.blocked([(entry_id, content_hash)])
        return entry_id in records or bool(content_hash and any(
            content_hash in row.removed_content_hashes for row in records.values()))

    # LLM: 索引不完整、丢失或低于已发表marker/根修订均失败关闭；旧小库可兼容只读快照。
    # 函数用途: 把同一次有界候选校验绑定到一个稳定屏障事务。
    @contextmanager
    def snapshot(self, *, max_bytes=None, _read=None, _marker=None):
        marker = _marker or self._marker(max_bytes=max_bytes, _read=_read)[1]
        if marker.get("index_file") is None:
            yield _LegacyBarrierSnapshot(self.load(max_bytes=max_bytes, _read=_read))
            return
        manifest = self.memory_path.with_name(self.memory_path.name + ".manifest.json")
        body = _read(manifest) if _read else read_bytes_beneath(manifest.parent, (manifest.name,), max_bytes=max_bytes)
        root = json.loads(body) if body is not None else {}
        minimum = max(marker["index_revision"], root.get("barrier_revision", 0))
        try:
            with self.index.snapshot(minimum_revision=minimum) as snapshot:
                yield snapshot
        except FileNotFoundError as exc:
            raise ValueError("MEMORY_TOMBSTONES_UNREADABLE") from exc

    # LLM: 读取不建索引，不扫描全账；每个候选只做两个主键查询。
    # 函数用途: 批量判断小集合的永久屏障。
    def blocked(self, pairs):
        with self.snapshot() as snapshot:
            return snapshot.blocked(pairs)

    # LLM: 只用于原quota→formal维护入口；游标分批耐久，可中断重启，最后才替换旧JSON为小marker。
    # 函数用途: 将旧账导入永久键索引并保留失败关闭和写前配额。
    def ensure_index(self, *, write, admission=None, commit=None):
        from ..user_space.owner_quota import OwnerQuotaChange

        body, marker = self._marker(_allow_migration=True)
        if marker.get("index_file") is not None:
            with self.snapshot(_marker=marker) as snapshot:
                return snapshot.revision
        records = list(self._load_legacy().values())
        if admission is not None:
            size = self.index.path.stat().st_size if self.index.path.exists() else 0
            admission.check([OwnerQuotaChange(self.index.path, size + 65536 + sum(len(r.to_json().encode()) * 3 for r in records))])
        identity = self._legacy_source_identity(body, records)
        state = self.index.migrate_step(records, source_identity=identity, commit=commit)
        while not state["complete"]:
            state = self.index.migrate_step(records, source_identity=identity, commit=commit)
        write(self.path, self.marker_text(state["revision"]))
        return state["revision"]

    # LLM: marker只包含版本和路径，不含正文或历史删除全集；每次屏障推进使原pin消费者看见代次变化。
    # 函数用途: 编码根之前发表的小型屏障回执。
    def marker_text(self, revision, *, deletion=False):
        payload = {"schema_version": TOMBSTONE_SCHEMA, "owner": self.owner, "records": {},
            "index_file": self.index.path.name, "index_revision": revision, "legacy_stamp": self._legacy_stamp()}
        if deletion:
            payload["last_committed_deletion"] = {"action": "remove", "revision": revision}
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


# LLM: 只兼容未迁移小库；维护写入口会将它转换成永久键索引，不作为长期热路径缓存。
# 类用途: 为旧全量屏障提供与SQLite一致的批量查询形状。
class _LegacyBarrierSnapshot:
    # LLM: 只持本次已严格解析的旧账；首次迁移后热路径改用SQLite，不在此保留跨请求缓存。
    # 函数用途: 为未升级的冷格式提供同形快照。
    def __init__(self, records):
        self.records, self.revision = records, 0
        self.hashes = {digest for row in records.values() for digest in row.removed_content_hashes}

    # LLM: 只对本批候选查既有集合；不影响live召回预算。
    # 函数用途: 判断升级前永久账是否阻止候选复活。
    def blocked(self, pairs):
        return {entry_id for entry_id, digest in pairs if entry_id in self.records or digest in self.hashes}

    # LLM: 只返回指定身份的已有删除回执，保持旧格式和索引格式语义一致。
    # 函数用途: 读取本批涉及的永久删除。
    def entries(self, ids):
        return {entry_id: self.records[entry_id] for entry_id in ids if entry_id in self.records}

    # LLM: 全量冷维护消费者取得副本，不得写回或裁掉永久屏障。
    # 函数用途: 保留完整load合同。
    def full_dict(self):
        return dict(self.records)


# LLM: 分片同时约束物理行数与序列化字节；单个超大事实仍独占一片，由读取预算显式拒读而非无限整读。
# 函数用途: 为普通迁移与维护产生有界正文批次。
def _generation_batches(rows):
    from .jsonl import _AUTHORITY_SCAN_MAX_BYTES, _memory_events_text

    batch, size = [], 0
    limit = max(1, _AUTHORITY_SCAN_MAX_BYTES // 2)
    for record in rows:
        cost = len(_memory_events_text([record]).encode("utf-8"))
        if batch and (len(batch) >= 1000 or size + cost > limit):
            yield batch
            batch, size = [], 0
        batch.append(record)
        size += cost
    if batch:
        yield batch


# LLM: 根清单只选择本逻辑库已提交的不可变文件；不存在根时仍读旧库，坏根绝不回退旧副本。
# 类用途: 通过commit-last发布冷热快照并提供带摘要校验的读取。
class MemoryGenerationStore:
    # LLM: 构造纯绑定，不读正文、不建索引、不迁移真实库。
    # 函数用途: 固定当前库的代次目录和永久墓碑位置。
    def __init__(self, memory_path):
        self.path = Path(memory_path)
        self.manifest_path = self.path.with_name(self.path.name + ".manifest.json")
        self.directory = self.path.with_name(self.path.name + ".generations")
        self.tombstones = StorageTombstones(self.path)

    # LLM: nofollow读取且强校验owner/schema/分片相对路径；降级可复用同一预算reader，不接受外部owner清单；引用快照内解析字节须等于读前观察。
    # 函数用途: 读取同代根清单，首次旧库返回None。
    def manifest(self, *, max_bytes=None, on_read=None, _read=None):
        body = (_read(self.manifest_path) if _read is not None else
                read_bytes_beneath(self.path.parent, (self.manifest_path.name,), max_bytes=max_bytes))
        record_parsed_pin_bytes(self.manifest_path, body)
        if on_read is not None:
            on_read(len(body or b""))
        raw = body.decode("utf-8") if body is not None else None
        if raw is None:
            return None
        root = json.loads(raw)
        if (not isinstance(root, dict) or root.get("schema_version") != GENERATION_SCHEMA
                or root.get("owner") != self.tombstones.owner or type(root.get("generation")) is not int
                or root["generation"] < 1 or not isinstance(root.get("segments"), list)):
            raise ValueError("MEMORY_GENERATION_UNREADABLE")
        names = set()
        for item in root["segments"]:
            parts = Path(str(item.get("path", ""))).parts
            if (len(parts) != 3 or parts[0] != self.directory.name or len(parts[1]) != 32
                    or any(c not in "0123456789abcdef" for c in parts[1])
                    or parts[2] in {"", ".", ".."} or item.get("tier") not in {"active", "cold"}
                    or type(item.get("count")) is not int or not 0 < item["count"] <= 1000
                    or item["path"] in names):
                raise ValueError("MEMORY_GENERATION_UNREADABLE")
            names.add(item["path"])
        return root

    # LLM: 每个分片最多1000条；校验整个分片hash后才反序列化，未发表分片从不进入读取；引用快照内解析字节须等于读前观察。
    # 函数用途: 读取并验证根清单精确引用的一个分片。
    def segment_rows(self, segment):
        body = read_bytes_beneath(self.path.parent, tuple(Path(segment["path"]).parts))
        record_parsed_pin_bytes(self.path.parent / segment["path"], body)
        raw = body.decode("utf-8") if body is not None else None
        if raw is None or hashlib.sha256(raw.encode("utf-8")).hexdigest() != segment["sha256"]:
            raise ValueError("MEMORY_GENERATION_UNREADABLE")
        rows = [json.loads(line) for line in raw.split("\n") if line]
        if len(rows) != segment["count"]:
            raise ValueError("MEMORY_GENERATION_UNREADABLE")
        return rows

    # LLM: 旧根遇合法发表/GC才最多重读两次；根未变的损坏保持原异常，绝不吞完整性错误。
    # 函数用途: 还原完整冷热事件快照并应用永久删除屏障。
    def events(self, root=None, *, tier=None, include_tombstones=True):
        from ._jsonl_maintenance import require_maintenance_event
        from .jsonl import _memory_record_from_obj

        root = root or self.manifest()
        if root is None:
            return None
        for attempt in range(3):
            records, seen = [], set()
            try:
                for segment in root["segments"]:
                    if tier is not None and segment["tier"] != tier:
                        continue
                    for payload in self.segment_rows(segment):
                        record = _memory_record_from_obj(payload)
                        require_maintenance_event(record)
                        if record.action not in {"add", "replace"} or record.entry_id in seen:
                            raise ValueError("MEMORY_GENERATION_UNREADABLE")
                        records.append(record)
                        seen.add(record.entry_id)
            except (OSError, ValueError):
                latest = self.manifest()
                if attempt == 2 or latest is None or latest["operation_id"] == root["operation_id"]:
                    raise
                root = latest
                continue
            latest = self.manifest()
            if latest is not None and latest["operation_id"] == root["operation_id"]:
                records.sort(key=lambda r: (r.created_at, r.entry_id))
                with self.tombstones.snapshot() as snapshot:
                    excluded = snapshot.blocked((r.entry_id, _content_hash(r.content)) for r in records)
                    blocked = snapshot.full_dict() if include_tombstones else {}
                return [*(r for r in records if r.entry_id not in excluded), *blocked.values()]
            root = latest
            if root is None:
                raise ValueError("MEMORY_GENERATION_UNREADABLE")
        raise ValueError("MEMORY_GENERATION_CHANGED")

    # LLM: 原formal锁内固定新分片、预期根与永久账CAS；额外保存真实前像可见ID以准确报告部分删除。
    # 函数用途: 生成可逐路径检查峰值配额的同代提交计划。
    def prepare(self, records, *, max_chars, pinned_ids=(), cold_ids=None, quarantine=None):
        import time

        from .jsonl import (
            _materialize_memory_events,
            _memory_events_text,
            _record_payload,
            _strict_memory_events,
            _with_legacy_identity,
        )

        records = [_with_legacy_identity(record) for record in records]
        old = self.manifest()
        with self.tombstones.snapshot() as snapshot:
            expected_tombstones = snapshot.revision
        visible_before = set()
        old_cold = set()
        if old is not None:
            for segment in old["segments"]:
                ids = {row["entry_id"] for row in self.segment_rows(segment)}
                visible_before.update(ids - self.tombstones.blocked((entry_id, "") for entry_id in ids))
                if segment["tier"] == "cold":
                    old_cold.update(ids)
        else:
            visible_before.update(record.entry_id for record in _materialize_memory_events(
                _strict_memory_events(self.path, include_tombstones=False), include_expired=True))
        if cold_ids is None:
            cold_ids = old_cold
        removed = {r.entry_id: r for r in records if r.action == "remove"}
        with self.tombstones.snapshot() as snapshot:
            previous_removed = snapshot.entries(removed)
        changed_removed = []
        for entry_id, record in removed.items():
            previous = previous_removed.get(entry_id)
            before = _record_payload(previous) if previous else None
            selected = record if previous is None or record.version >= previous.version else previous
            after = _record_payload(selected)
            after["removed_content_hashes"] = sorted(set(record.removed_content_hashes) | set(previous.removed_content_hashes if previous else ()))
            if before is not None:
                before["removed_content_hashes"] = sorted(previous.removed_content_hashes)
            if before != after:
                changed_removed.append(record)
        removed_hashes = {digest for r in removed.values() for digest in r.removed_content_hashes}
        blocked = self.tombstones.blocked((r.entry_id, _content_hash(r.content)) for r in records if r.action != "remove")
        live = [r for r in records if r.action != "remove" and r.entry_id not in blocked
                and r.entry_id not in removed and _content_hash(r.content) not in removed_hashes]
        now = time.time()
        expired = [r for r in live if r.expires_at and r.expires_at <= now]
        visible = [r for r in live if not r.expires_at or r.expires_at > now]
        active, cold = partition_records(visible, max_chars, cold_ids=cold_ids, pinned_ids=pinned_ids)
        operation = uuid.uuid4().hex
        files, segments = {}, []
        reusable = {(item["tier"], item["sha256"], item["count"]): item for item in (old or {}).get("segments", [])}
        index_material = []
        segment_numbers = {"active": 0, "cold": 0}
        # 未来TTL按精确到期边界成片；不同期限混片会在较短TTL到期后再次让过期行挤占物理预算。
        # 已经到期的历史仍可共片，未来任何时刻都能凭expires_before整片跳过。
        groups = []
        for tier, rows in (("active", active), ("cold", cold)):
            groups.append((tier, [r for r in rows if not r.expires_at]))
            expiry_groups = {}
            for record in rows:
                if record.expires_at:
                    expiry_groups.setdefault(record.expires_at, []).append(record)
            groups.extend((tier, group) for group in expiry_groups.values())
        groups.append(("cold", expired))
        for tier, rows in groups:
            for batch in _generation_batches(rows):
                text = _memory_events_text(batch)
                relative = f"{self.directory.name}/{operation}/{tier}-{segment_numbers[tier]:06d}.jsonl"
                segment_numbers[tier] += 1
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                segment = reusable.get((tier, digest, len(batch)))
                if segment is None:
                    files[self.path.parent / relative] = text
                    segment = {"path": relative, "tier": tier, "count": len(batch), "sha256": digest}
                if all(r.expires_at for r in batch):
                    segment = {**segment, "expires_before": max(r.expires_at for r in batch)}
                segments.append(segment)
                from ..retrieval.text_vector_cache import index_text

                index_material.append([(r.entry_id, r.version, _content_hash(r.content), index_text(r.content, r.attributes), r.expires_at) for r in batch])
        root = {"schema_version": GENERATION_SCHEMA, "owner": self.tombstones.owner,
                "generation": 1 if old is None else old["generation"] + 1,
                "operation_id": operation, "segments": segments, "tombstone_file": self.tombstones.path.name,
                "active_chars": sum(len(r.content) for r in active), "cold_chars": sum(len(r.content) for r in [*cold, *expired]),
                "active_total": len(active), "cold_total": len(cold) + len(expired), "total_records": len(live),
                "active_budget_chars": max_chars, "pinned_overflow_chars": max(0, sum(len(r.content) for r in active) - max_chars)}
        if old and "legacy_quarantine" in old:
            root["legacy_quarantine"] = old["legacy_quarantine"]
        if quarantine:
            relative = f"{self.directory.name}/{operation}/legacy-quarantine.jsonl"
            files[self.path.parent / relative] = quarantine
            root["legacy_quarantine"] = {"path": relative, "reason": "legacy_visibility_unknown",
                                         "count": sum(bool(line.strip()) for line in quarantine.split("\n"))}
        root["index_signature"] = hashlib.sha256(json.dumps(index_material, ensure_ascii=False).encode()).hexdigest()
        root_text = json.dumps(root, ensure_ascii=False, sort_keys=True)
        return {"root": root, "files": files, "root_text": root_text,
                "tombstone_text": read_text_beneath(self.tombstones.path.parent, (self.tombstones.path.name,)),
                "barrier_records": tuple(changed_removed),
                "has_tombstones": True, "expected_root": old,
                "expected_tombstones": expected_tombstones,
                "newly_removed": visible_before.intersection(removed)}

    # LLM: 原锁内双CAS根/墓碑再核峰值；永久账字节未变则不重写其身份，屏障先行失败保准确partial且不回滚。
    # 函数用途: 提交准备好的同代字节；中断留下的未引用分片不可见，删除屏障失败关闭。
    def publish(self, plan, *, write, admission=None, commit=None, redact=None):
        from ..user_space.owner_quota import OwnerQuotaChange

        with self.tombstones.snapshot() as snapshot:
            revision = snapshot.revision
        if (self.manifest() != plan["expected_root"] or revision != plan["expected_tombstones"]):
            raise ValueError("MEMORY_GENERATION_CONFLICT")
        changes = {**plan["files"], self.manifest_path: plan["root_text"]}
        if plan["has_tombstones"]:
            changes[self.tombstones.path] = plan["tombstone_text"]
        # 所有新分片均为额外峰值，不以未来回收的净差额抵扣临时空间。
        if admission is not None:
            admission.check([*[OwnerQuotaChange(path, len(text.encode("utf-8")) + 256) for path, text in changes.items()],
                OwnerQuotaChange(self.tombstones.index.path, self.tombstones.index.path.stat().st_size + 65536
                    + sum(len(r.to_json().encode()) * 3 for r in plan["barrier_records"]))])
        if redact is not None:
            redact()
        for path, raw in plan["files"].items():
            if commit is None:
                ensure_private_dir(path.parent)
            else:
                commit(lambda parent=path.parent: ensure_private_dir(parent))
            write(path, raw)
        # 独立屏障先于正文清理；根发表失败也不得从旧代召回已明确遗忘的内容。
        try:
            if plan["barrier_records"]:
                # 原屏障写入口仍是准入边界；失败发生在任何新删除生效之前。
                write(self.tombstones.path, self.tombstones.marker_text(revision))
            revision = self.tombstones.index.publish(plan["barrier_records"], expected_revision=revision, commit=commit)
            plan["root"]["barrier_revision"] = revision
            plan["root_text"] = json.dumps(plan["root"], ensure_ascii=False, sort_keys=True)
            if plan["barrier_records"]:
                plan["tombstone_text"] = self.tombstones.marker_text(revision, deletion=True)
            if plan["has_tombstones"] and read_text_beneath(
                    self.path.parent, (self.tombstones.path.name,)) != plan["tombstone_text"]:
                write(self.tombstones.path, plan["tombstone_text"])
            write(self.manifest_path, plan["root_text"])
        except Exception as exc:
            from ._jsonl_indexing import _NATIVE_MEMORY_ERRORS, RemovalCleanupError

            if isinstance(exc, _NATIVE_MEMORY_ERRORS):
                raise
            try:
                changed = bool(plan["newly_removed"].intersection(self.tombstones.blocked(
                    (entry_id, "") for entry_id in plan["newly_removed"])))
            except (OSError, ValueError, TypeError):
                changed = False
            if changed:
                raise RemovalCleanupError("REMOVE_CLEANUP_PENDING", formal_memory_changed=True) from exc
            raise
        try:
            self.collect_unreferenced(plan["root"], commit=commit)
        except Exception as exc:
            from ._jsonl_indexing import _NATIVE_MEMORY_ERRORS, RemovalCleanupError

            if isinstance(exc, _NATIVE_MEMORY_ERRORS):
                raise
            raise RemovalCleanupError("MEMORY_GENERATION_COMMITTED_CLEANUP_PENDING", formal_memory_changed=True) from exc
        return plan["root"]

    # LLM: 调用方持formal锁且根已提交；保留当前引用，按原writer协议清.tmp，目录与叶均nofollow/单链接拒绝。
    # 函数用途: 清理无引用JSONL和中断暂存及空代目录，不碰未知文件、永久墓碑或其它逻辑库。
    def collect_unreferenced(self, root, *, commit=None):
        retained = {item["path"] for item in root["segments"]}
        if root.get("legacy_quarantine"):
            retained.add(root["legacy_quarantine"]["path"])
        try:
            directories = list(self.directory.iterdir())
        except FileNotFoundError:
            return
        for directory in directories:
            if len(directory.name) != 32 or any(ch not in "0123456789abcdef" for ch in directory.name):
                continue
            fd = open_directory_beneath(self.path.parent, (self.directory.name, directory.name), require_dir_fd=True)
            try:
                for name in os.listdir(fd):
                    relative = f"{self.directory.name}/{directory.name}/{name}"
                    temporary = re.fullmatch(
                        r"\.(?:(?:active|cold)-[0-9]{6,}|legacy-quarantine)\.jsonl\.[0-9a-f]{32}\.tmp", name)
                    if relative in retained or not (name.endswith(".jsonl") or temporary):
                        continue
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise ValueError("MEMORY_GENERATION_UNREADABLE")
                    leaf = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        opened = os.fstat(leaf)
                        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                                or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)):
                            raise ValueError("MEMORY_GENERATION_UNREADABLE")
                    finally:
                        os.close(leaf)
                    if commit is None:
                        os.unlink(name, dir_fd=fd)
                    else:
                        commit(lambda leaf=name, parent=fd: os.unlink(leaf, dir_fd=parent))
                empty = not os.listdir(fd)
            finally:
                os.close(fd)
            if empty:
                parent = open_directory_beneath(self.path.parent, (self.directory.name,), require_dir_fd=True)
                try:
                    if commit is None:
                        os.rmdir(directory.name, dir_fd=parent)
                    else:
                        commit(lambda leaf=directory.name, parent=parent: os.rmdir(leaf, dir_fd=parent))
                finally:
                    os.close(parent)


# LLM: 复用唯一正式正文hash，不在代次层引入另一套规范化。
# 函数用途: 统一永久遗忘及索引语义身份的正文摘要。
def _content_hash(content):
    from .operations import memory_content_hash

    return memory_content_hash(content)
