# LLM: 共用原formal锁、writer与quota；根最后发布，GC只认本库分片及writer精确UUID暂存，不触碰未知文件。
# 模块用途: 管理同一长期记忆逻辑库的冷热代次、永久删除屏障和无损分层策略。
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from ..common.nofollow_fs import (
    ensure_private_dir,
    open_directory_beneath,
    read_bytes_beneath,
    read_text_beneath,
)

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

    # LLM: 永久账叠加旧墓碑且不回滚hash；可共用调用方的字节/记录预算，未读完整不得返回部分屏障。
    # 函数用途: 读取并验证永久删除记录。
    def load(self, *, max_bytes=None, on_read=None, _read=None, on_record=None):
        from ._jsonl_maintenance import require_maintenance_event
        from .jsonl import _memory_record_from_obj, _tombstone_path

        body = (_read(self.path) if _read is not None else
                read_bytes_beneath(self.path.parent, (self.path.name,), max_bytes=max_bytes))
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
        records = self.load() if records is None else records
        return entry_id in records or bool(content_hash and any(
            content_hash in row.removed_content_hashes for row in records.values()))


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

    # LLM: nofollow读取且强校验owner/schema/分片相对路径；降级可复用同一预算reader，不接受外部owner清单。
    # 函数用途: 读取同代根清单，首次旧库返回None。
    def manifest(self, *, max_bytes=None, on_read=None, _read=None):
        body = (_read(self.manifest_path) if _read is not None else
                read_bytes_beneath(self.path.parent, (self.manifest_path.name,), max_bytes=max_bytes))
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

    # LLM: 每个分片最多1000条；校验整个分片hash后才反序列化，未发表分片从不进入读取。
    # 函数用途: 读取并验证根清单精确引用的一个分片。
    def segment_rows(self, segment):
        raw = read_text_beneath(self.path.parent, tuple(Path(segment["path"]).parts))
        if raw is None or hashlib.sha256(raw.encode("utf-8")).hexdigest() != segment["sha256"]:
            raise ValueError("MEMORY_GENERATION_UNREADABLE")
        rows = [json.loads(line) for line in raw.split("\n") if line]
        if len(rows) != segment["count"]:
            raise ValueError("MEMORY_GENERATION_UNREADABLE")
        return rows

    # LLM: 旧根遇合法发表/GC才最多重读两次；根未变的损坏保持原异常，绝不吞完整性错误。
    # 函数用途: 还原完整冷热事件快照并应用永久删除屏障。
    def events(self, root=None, *, tier=None):
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
                blocked = self.tombstones.load()
                return [*(r for r in records if not self.tombstones.excludes(
                    r.entry_id, _content_hash(r.content), records=blocked)), *blocked.values()]
            root = latest
            if root is None:
                raise ValueError("MEMORY_GENERATION_UNREADABLE")
        raise ValueError("MEMORY_GENERATION_CHANGED")

    # LLM: 原formal锁内固定新分片、预期根与永久账CAS；额外保存真实前像可见ID以准确报告部分删除。
    # 函数用途: 生成可逐路径检查峰值配额的同代提交计划。
    def prepare(self, records, *, max_chars, pinned_ids=(), cold_ids=None, quarantine=None):
        from .jsonl import (
            _materialize_memory_events,
            _memory_events_text,
            _strict_memory_events,
            _with_legacy_identity,
        )

        records = [_with_legacy_identity(record) for record in records]
        old = self.manifest()
        prior_deleted, expected_tombstones = self.tombstones.prepare(())
        visible_before = set()
        old_cold = set()
        if old is not None:
            for segment in old["segments"]:
                ids = {row["entry_id"] for row in self.segment_rows(segment)}
                visible_before.update(ids - prior_deleted.keys())
                if segment["tier"] == "cold":
                    old_cold.update(ids)
        else:
            visible_before.update(record.entry_id for record in _materialize_memory_events(
                _strict_memory_events(self.path), include_expired=True))
        if cold_ids is None:
            cold_ids = old_cold
        removed, tombstone_text = self.tombstones.prepare(records)
        live = [r for r in records if r.action != "remove" and not self.tombstones.excludes(
            r.entry_id, _content_hash(r.content), records=removed)]
        active, cold = partition_records(live, max_chars, cold_ids=cold_ids, pinned_ids=pinned_ids)
        operation = uuid.uuid4().hex
        files, segments = {}, []
        reusable = {(item["tier"], item["sha256"], item["count"]): item for item in (old or {}).get("segments", [])}
        index_material = []
        for tier, rows in (("active", active), ("cold", cold)):
            for offset in range(0, len(rows), 1000):
                batch = rows[offset:offset + 1000]
                text = _memory_events_text(batch)
                relative = f"{self.directory.name}/{operation}/{tier}-{offset // 1000:06d}.jsonl"
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                segment = reusable.get((tier, digest, len(batch)))
                if segment is None:
                    files[self.path.parent / relative] = text
                    segment = {"path": relative, "tier": tier, "count": len(batch), "sha256": digest}
                segments.append(segment)
                from ..retrieval.text_vector_cache import index_text

                index_material.append([(r.entry_id, r.version, _content_hash(r.content), index_text(r.content, r.attributes)) for r in batch])
        root = {"schema_version": GENERATION_SCHEMA, "owner": self.tombstones.owner,
                "generation": 1 if old is None else old["generation"] + 1,
                "operation_id": operation, "segments": segments, "tombstone_file": self.tombstones.path.name,
                "active_chars": sum(len(r.content) for r in active), "cold_chars": sum(len(r.content) for r in cold),
                "active_total": len(active), "cold_total": len(cold), "total_records": len(live),
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
        return {"root": root, "files": files, "root_text": root_text, "tombstone_text": tombstone_text,
                "has_tombstones": True, "expected_root": old,
                "expected_tombstones": expected_tombstones,
                "newly_removed": visible_before.intersection(removed)}

    # LLM: 原锁内双CAS根/墓碑再核峰值；永久账字节未变则不重写其身份，屏障先行失败保准确partial且不回滚。
    # 函数用途: 提交准备好的同代字节；中断留下的未引用分片不可见，删除屏障失败关闭。
    def publish(self, plan, *, write, admission=None, commit=None, redact=None):
        from ..user_space.owner_quota import OwnerQuotaChange

        if (self.manifest() != plan["expected_root"]
                or self.tombstones.prepare(())[1] != plan["expected_tombstones"]):
            raise ValueError("MEMORY_GENERATION_CONFLICT")
        changes = {**plan["files"], self.manifest_path: plan["root_text"]}
        if plan["has_tombstones"]:
            changes[self.tombstones.path] = plan["tombstone_text"]
        # 所有新分片均为额外峰值，不以未来回收的净差额抵扣临时空间。
        if admission is not None:
            admission.check([OwnerQuotaChange(path, len(text.encode("utf-8"))) for path, text in changes.items()])
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
            if plan["has_tombstones"] and read_text_beneath(
                    self.path.parent, (self.tombstones.path.name,)) != plan["tombstone_text"]:
                write(self.tombstones.path, plan["tombstone_text"])
            write(self.manifest_path, plan["root_text"])
        except Exception as exc:
            from ._jsonl_indexing import _NATIVE_MEMORY_ERRORS, RemovalCleanupError

            if isinstance(exc, _NATIVE_MEMORY_ERRORS):
                raise
            try:
                changed = bool(plan["newly_removed"].intersection(self.tombstones.load()))
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
