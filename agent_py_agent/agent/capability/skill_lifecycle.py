from __future__ import annotations

# LLM: Skill lifecycle keeps learned skills as drafts before promotion into the active catalog.
# 模块用途: 管理 skill 草稿、正式版本、禁用和回滚，让自学习不会直接污染正式 skill。
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path


# LLM: SkillDraftRequest is the command bundle for creating or replacing one draft skill.
# 类用途: 保存 skill 草稿名称、Markdown 正文和创建原因。
@dataclass(frozen=True)
class SkillDraftRequest:
    name: str
    markdown: str
    reason: str = ""


# LLM: SkillLifecycleResult reports the materialized lifecycle state after one command.
# 类用途: 保存 skill 生命周期操作后的名称、状态、版本和文件路径。
@dataclass(frozen=True)
class SkillLifecycleResult:
    name: str
    status: str
    version: int
    path: Path


# LLM: SkillLifecycleEvent is an append-only audit record for skill lifecycle transitions.
# 类用途: 保存一次 skill 生命周期事件，供审计、调试和回滚记录读取。
@dataclass(frozen=True)
class SkillLifecycleEvent:
    action: str
    name: str
    version: int
    path: str
    reason: str = ""
    created_at: float = 0.0


# LLM: SkillLifecycleStore owns draft, active, versioned, and disabled skill files.
# 类用途: 用文件系统实现 skill 草稿、晋级、禁用和回滚闭环，不直接改现有 registry 行为。
class SkillLifecycleStore:
    # LLM: SkillLifecycleStore.__init__ sets stable lifecycle directories under one root.
    # 函数用途: 初始化 skill 生命周期目录和事件 ledger 路径。
    def __init__(self, root: Path):
        self.root = root
        self.drafts_dir = root / "drafts"
        self.active_dir = root / "active"
        self.versions_dir = root / "versions"
        self.disabled_dir = root / "disabled"
        self.events_path = root / "events.jsonl"

    # LLM: SkillLifecycleStore.create_draft writes a draft SKILL.md without exposing it to active registry.
    # 函数用途: 创建或覆盖 skill 草稿，并记录 draft_created 事件。
    def create_draft(self, request: SkillDraftRequest) -> SkillLifecycleResult:
        path = self.drafts_dir / _safe_name(request.name) / "SKILL.md"
        _write_text(path, request.markdown)
        result = SkillLifecycleResult(request.name, "draft", self._next_version(request.name), path)
        self._append_event("draft_created", result, request.reason)
        return result

    # LLM: SkillLifecycleStore.promote copies a draft into active skills and immutable versions.
    # 函数用途: 将草稿晋级为正式 skill，写入 active 目录和版本归档。
    def promote(self, name: str, *, reason: str = "") -> SkillLifecycleResult:
        source = self.drafts_dir / _safe_name(name) / "SKILL.md"
        if not source.exists():
            raise KeyError(f"unknown draft skill: {name}")
        version = self._next_version(name)
        version_path = self.versions_dir / _safe_name(name) / f"v{version}" / "SKILL.md"
        active_path = self.active_dir / _safe_name(name) / "SKILL.md"
        _copy_file(source, version_path)
        _copy_file(source, active_path)
        result = SkillLifecycleResult(name, "active", version, active_path)
        self._append_event("promoted", result, reason)
        return result

    # LLM: SkillLifecycleStore.disable removes a skill from active scan while preserving history.
    # 函数用途: 禁用正式 skill，把当前 active 文件移入 disabled 目录并记录事件。
    def disable(self, name: str, *, reason: str = "") -> SkillLifecycleResult:
        active_path = self.active_dir / _safe_name(name) / "SKILL.md"
        if not active_path.exists():
            raise KeyError(f"unknown active skill: {name}")
        version = self._latest_version(name)
        disabled_path = self.disabled_dir / _safe_name(name) / f"v{version}" / "SKILL.md"
        _copy_file(active_path, disabled_path)
        active_path.unlink()
        result = SkillLifecycleResult(name, "disabled", version, disabled_path)
        self._append_event("disabled", result, reason)
        return result

    # LLM: SkillLifecycleStore.rollback restores a previous immutable version into active skills.
    # 函数用途: 回滚 skill 到指定历史版本，并记录 rolled_back 事件。
    def rollback(self, name: str, *, version: int, reason: str = "") -> SkillLifecycleResult:
        version_path = self.versions_dir / _safe_name(name) / f"v{version}" / "SKILL.md"
        if not version_path.exists():
            raise KeyError(f"unknown skill version: {name} v{version}")
        active_path = self.active_dir / _safe_name(name) / "SKILL.md"
        _copy_file(version_path, active_path)
        result = SkillLifecycleResult(name, "active", version, active_path)
        self._append_event("rolled_back", result, reason)
        return result

    # LLM: SkillLifecycleStore.events returns append-only lifecycle events in file order.
    # 函数用途: 读取 skill 生命周期 ledger，坏行会被跳过。
    def events(self) -> list[SkillLifecycleEvent]:
        if not self.events_path.exists():
            return []
        events: list[SkillLifecycleEvent] = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            event = _event_from_json(line)
            if event is not None:
                events.append(event)
        return events

    # LLM: SkillLifecycleStore._next_version derives the next version number from existing archives.
    # 函数用途: 计算某个 skill 下一次晋级应使用的版本号。
    def _next_version(self, name: str) -> int:
        return self._latest_version(name) + 1

    # LLM: SkillLifecycleStore._latest_version scans immutable version directories.
    # 函数用途: 获取某个 skill 当前最大版本号，没有版本时返回 0。
    def _latest_version(self, name: str) -> int:
        version_root = self.versions_dir / _safe_name(name)
        versions = [_version_number(path.name) for path in version_root.glob("v*")] if version_root.exists() else []
        return max(versions or [0])

    # LLM: SkillLifecycleStore._append_event writes one lifecycle audit event as JSONL.
    # 函数用途: 追加记录 skill 生命周期事件，保持 ledger 可审计。
    def _append_event(self, action: str, result: SkillLifecycleResult, reason: str) -> None:
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        event = SkillLifecycleEvent(action, result.name, result.version, str(result.path), reason, time.time())
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.__dict__, ensure_ascii=False, sort_keys=True) + "\n")


# LLM: _write_text writes parent directories and UTF-8 content for skill files.
# 函数用途: 写入 skill Markdown 文件。
def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# LLM: _copy_file preserves skill Markdown while creating missing parent directories.
# 函数用途: 复制 skill 文件到 active、version 或 disabled 目录。
def _copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


# LLM: _event_from_json parses one lifecycle event line best-effort.
# 函数用途: 从 JSONL 行恢复 SkillLifecycleEvent，解析失败时返回 None。
def _event_from_json(line: str) -> SkillLifecycleEvent | None:
    try:
        payload = json.loads(line)
        return SkillLifecycleEvent(**payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


# LLM: _safe_name keeps lifecycle paths under predictable per-skill directories.
# 函数用途: 清理 skill 名称为路径段，拒绝空名称。
def _safe_name(name: str) -> str:
    safe = str(name or "").strip().replace("/", "-").replace("\\", "-")
    if not safe:
        raise ValueError("skill name is required")
    return safe


# LLM: _version_number parses vN directory names.
# 函数用途: 从版本目录名提取数字版本，失败时返回 0。
def _version_number(name: str) -> int:
    try:
        return int(name.removeprefix("v"))
    except ValueError:
        return 0
