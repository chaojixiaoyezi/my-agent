"""有界运行意图locator：只记录本事实根内的目录名及原运行身份。"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .common.json_io import locked_json_path, write_private_text_file_atomic_unlocked
from .common.nofollow_fs import open_readonly_file_beneath
from .common.safe_id import safe_id
from .runtime_errors import runtime_error_report


# LLM: locator不能提供任意路径；写者与读者都只接受单目录名。
# 函数用途: 验证候选仍位于同一owner或task-local事实根之下。
def _directory(value):
    return isinstance(value, str) and bool(value) and value not in {".", ".."} and "/" not in value and "\\" not in value


# LLM: 查询元数据与事实根并列放在同一owner/task-local的memory_archive内，不污染原有事实目录布局。
# 函数用途: 为同一事实根取得稳定的私有定位页路径，锁文件也留在事实根之外。
def _locator_path(facts_dir: Path) -> Path:
    return facts_dir.parent / f".{facts_dir.name}.latest-run-intent.json"


# LLM: 不枚举目录；读取固定私有文件且有字节预算，缺失、损坏均返回结构化unknown。
# 函数用途: 读取已发布的最近候选页。
def read_locator(facts_dir: Path, *, limit: int):
    path = _locator_path(facts_dir)
    try:
        descriptor = open_readonly_file_beneath(path.parent, (path.name,))
        with os.fdopen(descriptor, "rb") as handle:
            data = handle.read(262145)
        if len(data) > 262144:
            raise ValueError("run intent locator exceeds byte budget")
        payload = json.loads(data)
        if (not isinstance(payload, dict) or payload.get("schema_version") != "run_intent_locator.v1"
                or not isinstance(payload.get("entries"), list) or len(payload["entries"]) > limit
                or any(not isinstance(row, dict) or not _directory(row.get("directory")) for row in payload["entries"])):
            raise ValueError("invalid run intent locator")
        return payload["entries"], []
    except (OSError, UnicodeError, ValueError) as exc:
        error = runtime_error_report(exc, context="run_intent.runtime_fact.read")
        error.update(path=str(path), source_status="unknown",
                     error_code="RUN_INTENT_LOCATOR_MISSING" if isinstance(exc, FileNotFoundError) else "RUN_INTENT_LOCATOR_INVALID")
        return [], [error]


# LLM: 原事实文件已先原子发布；固定小页在同一locator锁下更新，失败不伪造事实未写入。
# 函数用途: 发布最近运行身份，后续反馈只读取少数精确文件。
def update_locator(facts_dir: Path, directory: str, payload: dict, *, limit: int):
    if not _directory(directory):
        raise ValueError("invalid runtime fact directory")
    path = _locator_path(facts_dir)
    with locked_json_path(path):
        rows, _errors = read_locator(facts_dir, limit=limit)
        row = {"directory": directory, "request_id": str(payload.get("request_id") or ""),
               "run_id": str(payload.get("run_id") or ""),
               "updated_at_ns": (facts_dir / directory / "task.json").stat().st_mtime_ns}
        rows = [row, *(old for old in rows if old["directory"] != directory)]
        rows.sort(key=lambda item: item.get("updated_at_ns", 0), reverse=True)
        write_private_text_file_atomic_unlocked(path, json.dumps({"schema_version":"run_intent_locator.v1", "entries":rows[:limit]}, ensure_ascii=False))


# LLM: 只在工具冷装配时迁移可证明完整的微型旧事实集；最多观察B+1目录项，大目录不建不完整locator。
# 函数用途: 接住旧版本少量事实而不把历史枚举放回write_file热miss。
def bootstrap_locator(facts_dir: Path, *, limit: int):
    if not facts_dir.is_dir() or _locator_path(facts_dir).exists():
        return
    path = _locator_path(facts_dir)
    with locked_json_path(path):
        if path.exists():
            return
        directories = []
        with os.scandir(facts_dir) as iterator:
            for _ in range(limit + 1):
                entry = next(iterator, None)
                if entry is None:
                    break
                directories.append(entry)
        if len(directories) > limit:
            return
        rows = []
        for entry in directories:
            if entry.is_dir(follow_symlinks=False) and _directory(entry.name):
                task = facts_dir / entry.name / "task.json"
                try:
                    stamp = task.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                rows.append({"directory":entry.name, "updated_at_ns":stamp.st_mtime_ns})
        rows.sort(key=lambda row: row["updated_at_ns"], reverse=True)
        write_private_text_file_atomic_unlocked(path, json.dumps({"schema_version":"run_intent_locator.v1", "entries":rows}, ensure_ascii=False))


# LLM: 宿主身份仅用于准确定位，不授予额外路径；有身份时绝不退回其他运行的最近意图。
# 函数用途: 返回直接请求目录及可由原run_id匹配的有界候选。
def intent_candidates(facts_dir: Path, *, limit: int, request_id: str = "", run_id: str = ""):
    if not facts_dir.exists():
        return [], []
    rows, errors = read_locator(facts_dir, limit=limit)
    names = []
    if request_id:
        names.append(safe_id(request_id, default="run"))
    for row in rows:
        if not request_id and not run_id or request_id and row.get("request_id") == request_id or run_id and row.get("run_id") == run_id:
            if row["directory"] not in names:
                names.append(row["directory"])
    return [facts_dir / name / "task.json" for name in names[:limit]], errors
