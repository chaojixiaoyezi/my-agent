# LLM: 通知只投影已经正式提交的规则，不签晋升、不改人格/冻结系统前缀；owner内锁和逐线程回执负责幂等。
# 模块用途: 持久保存增量规则及各线程已见身份，给下一请求追加有界提醒。
"""Non-authoritative, owner-local rule reminders after formal promotion."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from ..common.json_io import locked_json_path, write_private_text_file_atomic_unlocked
from ..common.nofollow_fs import read_bytes_beneath


class RuleNoticeStore:
    # LLM: 构造只绑定当前owner，不创建文件或读取正式记忆；所有读写沿同一owner根的无链接路径。
    # 函数用途: 绑定规则通知和逐线程回执的位置及有界窗口。
    def __init__(self, owner_root: str | Path, *, max_notices: int = 128):
        self.root = Path(owner_root).absolute()
        self.path = self.root / "memory" / "rule_notices.json"
        self.max_notices = max(1, max_notices)

    # LLM: 同memory/version幂等，拒绝整数旧版本；替换条目同次清除旧提醒，数量与UTF8落盘预算均有界。
    # 函数用途: 在owner通知锁内发布已经提交的规则快照。
    def publish(self, *, memory_id: str, version: int | str, text: str, source_threads=(),
                scope: dict | None = None, max_chars: int = 1200, supersedes=()):
        if not memory_id or not version or not text or max_chars <= 0:
            return None
        identity = json.dumps([memory_id, version], ensure_ascii=False, separators=(",", ":"))
        notice_id = "rule-notice-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        with locked_json_path(self.path):
            data = self._read(self.path)
            rows = data.get("notices", [])
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("invalid rule notices")
            for row in rows:
                if row.get("notice_id") == notice_id:
                    return row
                if (row.get("memory_id") == memory_id and type(version) is int
                        and type(row.get("version")) is int and row["version"] > version):
                    return row
            notice = {"notice_id": notice_id, "memory_id": memory_id, "version": version,
                "text": text[:max_chars], "created_at": datetime.now(timezone.utc).isoformat(),
                "source_threads": sorted(set(source_threads)), "scope": dict(scope or {})}
            replaced = {memory_id, *supersedes}
            rows = [row for row in rows if row.get("memory_id") not in replaced] + [notice]
            rows = rows[-self.max_notices:]
            while len((json.dumps({"notices": rows}, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")) > 2_097_152:
                if len(rows) == 1:
                    raise ValueError("single rule notice exceeds storage budget")
                rows.pop(0)
            self._write(self.path, {"notices": rows})
            return notice

    # LLM: 仅已装入尾部的通知记已见；剩余预算含分隔符，耗尽先退出，避免负切片越界或消费下一条。
    # 函数用途: 原子取得当前线程未见且适用的有界提醒正文；重复请求不再返回。
    def take(self, thread_id: str, *, max_chars: int = 1200, allows=None) -> str:
        if not thread_id or max_chars <= 0 or not self.path.exists():
            return ""
        key = hashlib.sha256(thread_id.encode()).hexdigest()
        seen_path = self.root / "memory" / "rule_notices_seen" / f"{key}.json"
        with locked_json_path(self.path), locked_json_path(seen_path):
            rows = self._read(self.path).get("notices", [])
            seen = set(self._read(seen_path).get("seen", []))
            active = {row["notice_id"] for row in rows}
            selected, used = [], 0
            for row in rows:
                scope = row.get("scope", {})
                if (row["notice_id"] in seen or thread_id in row.get("source_threads", ())
                        or (allows is not None and not allows(scope.get("scope_type"), scope.get("scope_key")))):
                    continue
                # 每条按剩余预算截取；未装下的后续通知仍留到下一轮，不能预先记已见。
                remaining = max_chars - used
                if remaining <= 0:
                    break
                text = str(row["text"])[:remaining]
                if not text:
                    break
                selected.append(text)
                used += len(text) + 1
                seen.add(row["notice_id"])
            if selected:
                self._write(seen_path, {"seen": sorted(seen & active)})
            return "\n".join(selected)

    # LLM: 上限限制实际读取字节；坏JSON和越界链接原样失败，不把坏回执当空而重新发送。
    # 函数用途: 从当前owner无链接读取一个有界通知/已见对象。
    def _read(self, path: Path, *, max_bytes: int = 2_097_152) -> dict:
        raw = read_bytes_beneath(self.root, path.relative_to(self.root).parts,
            max_bytes=max_bytes, require_dir_fd=True)
        data = json.loads(raw) if raw else {}
        if not isinstance(data, dict):
            raise ValueError("invalid rule notice object")
        return data

    # LLM: 已持规范路径锁；私有原子替换不改owner之外文件，通知不是正式记忆第二权威。
    # 函数用途: 写入通知或线程已见回执。
    def _write(self, path: Path, data: dict):
        write_private_text_file_atomic_unlocked(path, json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n")


# LLM: 仅由成功晋升收尾调用；版本/正文同取正式记录，USER/HOT读取和通知提交持源锁，拒绝旧收尾复活已替换规则。
# 函数用途: 为偏好、工作规则及HOT正式结果创建幂等通知，删除只通知撤销不复述旧规则。
def publish_promoted_rule(service, candidate, reference: str, *, max_chars: int = 1200):
    if candidate.candidate_type not in {"user_preference", "working_agreement", "hot_rule"}:
        return None
    root = Path(service.persona.owner_home).absolute()
    memory_id = reference.removesuffix(":removed")
    if memory_id.startswith("USER.md#"):
        return _publish_user_rule(service, candidate, memory_id, max_chars=max_chars)
    if memory_id.startswith("memory-hot.md#"):
        return _publish_hot_rule(service, candidate, memory_id, max_chars=max_chars)
    if not memory_id.startswith("memory/long_term/memory.jsonl#"):
        return None
    entry_id = memory_id.split("#", 1)[1]
    record = next((row for row in service.long_term.all() if row.entry_id == entry_id), None)
    if candidate.proposed_action == "remove":
        if record is not None:
            return None
        path = Path(service.long_term.path)
        raw = read_bytes_beneath(root, path.absolute().relative_to(root).parts,
            max_bytes=2_097_152, require_dir_fd=True)
        if raw is None:
            return None
        version = "sha256:" + hashlib.sha256(raw).hexdigest()
        text = f"规则已撤销：{memory_id}；不再沿用其旧版本。"
    else:
        if record is None:
            return None
        version, text = record.version, record.content
    return RuleNoticeStore(root).publish(memory_id=memory_id, version=version, text=text,
        source_threads=[str(ref["thread_id"]) for ref in candidate.source_message_refs if ref.get("thread_id")],
        scope=candidate.scope, max_chars=max_chars)


# LLM: 沿原target→source→versions锁序读完整正式快照，再在target锁内发布；不重入flock，C模式复用原生锁出生叶。
# 函数用途: 只通知USER里仍存在的精确条目，删除通知仅在确已删除时发布，替换清除旧内容ID提醒。
def _publish_user_rule(service, candidate, memory_id: str, *, max_chars: int):
    from ..capability.persona_repository import _persona_birth_commit

    persona = service.persona
    options = {"birth_commit": _persona_birth_commit} if persona.coordinator_io else {}
    with locked_json_path(persona.path_for("user"), **options):
        snapshot = persona._list_entries_locked("user")
        entry_id = memory_id.split("#", 1)[1]
        entry = next((row for row in snapshot["entries"] if row["entry_id"] == entry_id), None)
        version = snapshot["version"]
        if type(version) is not int or version < 1:
            return None
        if candidate.proposed_action == "remove":
            if entry is not None:
                return None
            text = f"规则已撤销：{memory_id}；不再沿用其旧版本。"
        elif entry is None or entry.get("blocked"):
            return None
        else:
            text = entry["content"]
        supersedes = ()
        if candidate.proposed_action in {"replace", "merge"} and candidate.target_entry_id:
            supersedes = (f"USER.md#{candidate.target_entry_id}",)
        return RuleNoticeStore(persona.owner_home).publish(memory_id=memory_id, version=version, text=text,
            source_threads=[str(ref["thread_id"]) for ref in candidate.source_message_refs if ref.get("thread_id")],
            scope=candidate.scope, max_chars=max_chars, supersedes=supersedes)


# LLM: HOT稳定ID的正文不可覆盖；在原源锁内核验owner边界、精确正式记录并发布，离开HOT的旧回执不新增提醒。
# 函数用途: 通知实际HOT规则而非候选副本，版本摘要只依赖该规则，不受其它规则追加影响。
def _publish_hot_rule(service, candidate, memory_id: str, *, max_chars: int):
    from .lessons import _coordinator_lesson_write

    root = Path(service.persona.owner_home).absolute()
    path = Path(service.hot.path).absolute()
    options = {"birth_commit": _coordinator_lesson_write} if service.hot.coordinator_io else {}
    with locked_json_path(path, **options):
        if read_bytes_beneath(root, path.relative_to(root).parts,
                max_bytes=2_097_152, require_dir_fd=True) is None:
            return None
        hot_id = memory_id.split("#", 1)[1]
        record = next((row for row in service.hot.list() if row.hot_id == hot_id), None)
        if record is None:
            return None
        version = "sha256:" + hashlib.sha256(record.rule.encode("utf-8")).hexdigest()
        return RuleNoticeStore(root).publish(memory_id=memory_id, version=version, text=record.rule,
            source_threads=[str(ref["thread_id"]) for ref in candidate.source_message_refs if ref.get("thread_id")],
            scope=candidate.scope, max_chars=max_chars)
