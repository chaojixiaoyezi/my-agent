from __future__ import annotations

"""Managed background-process session records shared across agent processes."""

# LLM: This module is the only durable store for managed background shell sessions.
# Records live outside an owner sandbox when an owner home is available, because a
# model-controlled command must never be able to rewrite the PID that process_session
# will later signal. Keep schema validation and atomic writes in this module.
# 模块用途: 跨主代理、子代理和 Gateway 进程保存后台命令的 PID、归属与终态，且避免
# 被沙箱内命令篡改成任意 PID。

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_json_file_atomic_unlocked,
)
from ..runtime_errors import runtime_error_report

PROCESS_SESSION_SCHEMA = "managed_process_session.v1"
_SESSION_ID_RE = re.compile(r"^bg-[A-Za-z0-9][A-Za-z0-9-]{0,95}$")


# LLM: Load errors remain structured so the model-facing layer can distinguish a
# missing session from a damaged authority record without parsing exception text.
# 类用途: 返回一次后台会话记录读取结果，以及可供上层展示的结构化损坏信息。
@dataclass(frozen=True)
class ProcessSessionLoadReport:
    record: dict[str, object]
    load_error: dict[str, object] | None = None


# LLM: Owner-scoped records must be siblings of, not descendants of, owner_home.
# The bwrap policy mounts only the exact owner home, so this authority stays host-only.
# 函数用途: 计算后台会话唯一权威目录；多用户放在 owner 沙箱外，可信单机回落到工作区。
def process_session_store_root(
    workspace_root: str | Path,
    owner_scope_root: object = "",
) -> Path:
    workspace = Path(workspace_root).expanduser().resolve(strict=False)
    owner_text = str(owner_scope_root or "").strip()
    if not owner_text:
        return workspace / ".background_jobs" / "process_sessions"
    owner = Path(owner_text).expanduser().resolve(strict=False)
    digest = hashlib.sha256(str(owner).encode("utf-8")).hexdigest()[:20]
    return owner.parent / ".my-agent-runtime" / "process_sessions" / digest


# LLM: Each session has one JSON authority file. Filename validation is mandatory
# before joining paths because session_id is model-visible and therefore untrusted.
# 类用途: 原子写入、读取、枚举和裁剪某个受保护目录里的后台会话记录。
class ProcessSessionStore:
    # LLM: Construction only normalizes the authority root; directory creation is
    # delayed until a write so read-only list/status calls do not mutate the host.
    # 函数用途: 绑定一个后台会话权威目录。
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)

    # LLM: Never accept path separators, dots, or arbitrary filenames from callers.
    # 函数用途: 把合法 session_id 映射到唯一 JSON 文件；非法值直接拒绝。
    def record_path(self, session_id: str) -> Path:
        normalized = str(session_id or "").strip()
        if not _SESSION_ID_RE.fullmatch(normalized):
            raise ValueError("invalid managed process session id")
        return self.root / f"{normalized}.json"

    # LLM: A write replaces one complete schema record atomically and tightens host
    # permissions after replace. Callers must provide the full immutable scope.
    # 函数用途: 原子保存一条完整后台会话记录，并限制为当前系统用户可读写。
    def write(self, record: dict[str, object]) -> dict[str, object]:
        validated = _validated_record(record)
        path = self.record_path(str(validated["session_id"]))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        with locked_json_path(path):
            existing_report = read_json_object_report(
                path,
                context="process_session_store.transition_read",
            )
            existing = existing_report.payload
            if existing:
                existing = _validated_record(existing)
                _assert_same_process_authority(existing, validated)
                validated = _terminal_transition(existing, validated)
            write_json_file_atomic_unlocked(path, validated)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return validated

    # LLM: Missing and corrupt are different results. Corrupt records never become
    # partially trusted BackgroundProcess objects.
    # 函数用途: 读取并完整校验一个 session；缺失返回空，损坏返回结构化错误。
    def load(self, session_id: str) -> ProcessSessionLoadReport:
        try:
            path = self.record_path(session_id)
        except ValueError as exc:
            return ProcessSessionLoadReport({}, _store_error(exc, "process_session_store.invalid_id"))
        report = read_json_object_report(
            path,
            context="process_session_store.load",
        )
        if report.load_error is not None:
            return ProcessSessionLoadReport({}, report.load_error)
        if not report.payload:
            return ProcessSessionLoadReport({})
        try:
            return ProcessSessionLoadReport(_validated_record(report.payload))
        except (TypeError, ValueError) as exc:
            return ProcessSessionLoadReport(
                {},
                _store_error(exc, "process_session_store.validate", path=path),
            )

    # LLM: Enumeration validates each authority independently. One damaged record
    # must not hide healthy sessions, but its error must remain observable.
    # 函数用途: 枚举目录中的合法后台会话，并单独收集损坏文件错误。
    def list_records(self) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        if not self.root.is_dir():
            return [], []
        records: list[dict[str, object]] = []
        errors: list[dict[str, object]] = []
        try:
            paths = tuple(self.root.glob("bg-*.json"))
        except OSError as exc:
            return [], [_store_error(exc, "process_session_store.list", path=self.root)]
        for path in paths:
            report = self.load(path.stem)
            if report.record:
                records.append(report.record)
            elif report.load_error is not None:
                errors.append(report.load_error)
        return records, errors

    # LLM: Pruning removes only old terminal records; running records are never
    # inferred stale from age and never deleted here.
    # 函数用途: 只保留最近若干条已结束会话，防止长期 Gateway 的小记录无限增长。
    def prune_finished(self, max_finished: int) -> None:
        limit = max(0, int(max_finished))
        records, _errors = self.list_records()
        finished = [
            record
            for record in records
            if str(record.get("status") or "") in {"exited", "killed"}
        ]
        finished.sort(
            key=lambda item: float(item.get("finished_at") or item.get("started_at") or 0.0),
            reverse=True,
        )
        for record in finished[limit:]:
            try:
                self.record_path(str(record["session_id"])).unlink(missing_ok=True)
            except (OSError, ValueError):
                continue


# LLM: Validation is deliberately closed-world for authority fields while command
# text remains opaque data. Add schema migrations explicitly instead of accepting aliases.
# 函数用途: 校验后台会话记录的版本、身份、PID、状态和访问范围是否完整可信。
def _validated_record(record: object) -> dict[str, object]:
    if not isinstance(record, dict):
        raise TypeError("managed process session record must be an object")
    payload = dict(record)
    if str(payload.get("schema") or "") != PROCESS_SESSION_SCHEMA:
        raise ValueError("unsupported managed process session schema")
    session_id = str(payload.get("session_id") or "").strip()
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError("invalid managed process session id")
    try:
        pid = int(payload.get("pid") or 0)
        started_at = float(payload.get("started_at") or 0.0)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid managed process numeric fields") from exc
    if pid <= 0 or started_at <= 0:
        raise ValueError("managed process pid and started_at are required")
    pid_birth_token = str(payload.get("pid_birth_token") or "").strip()
    if not pid_birth_token:
        raise ValueError("managed process pid_birth_token is required")
    status = str(payload.get("status") or "")
    if status not in {"running", "exited", "killed"}:
        raise ValueError("invalid managed process status")
    scope = payload.get("access_scope")
    if not isinstance(scope, dict):
        raise ValueError("managed process access_scope is required")
    owner_id = str(scope.get("owner_id") or "").strip()
    conversation_id = str(scope.get("conversation_id") or "").strip()
    if not owner_id or not conversation_id:
        raise ValueError("managed process access_scope is not bound")
    payload.update(
        {
            "schema": PROCESS_SESSION_SCHEMA,
            "session_id": session_id,
            "pid": pid,
            "pid_birth_token": pid_birth_token,
            "started_at": started_at,
            "status": status,
            "access_scope": {
                "owner_id": owner_id,
                "conversation_id": conversation_id,
                "owner_home": str(scope.get("owner_home") or ""),
            },
        }
    )
    return payload


# LLM: PID, birth token, session, and access scope are immutable authority. A
# second process may update lifecycle only for that exact same hosted command.
# 函数用途: 阻止并发写把一条 session 偷换成另一个 PID 或另一个用户会话。
def _assert_same_process_authority(
    existing: dict[str, object],
    incoming: dict[str, object],
) -> None:
    immutable_keys = ("session_id", "pid", "pid_birth_token", "access_scope")
    if any(existing.get(key) != incoming.get(key) for key in immutable_keys):
        raise ValueError("managed process session immutable authority conflict")


# LLM: Terminal state is monotonic. Explicit killed outranks a racing exited
# refresh, and no later running cache may reopen either terminal state.
# 函数用途: 合并并发生命周期更新，保证 running 只能单向进入 exited/killed。
def _terminal_transition(
    existing: dict[str, object],
    incoming: dict[str, object],
) -> dict[str, object]:
    old_status = str(existing.get("status") or "")
    new_status = str(incoming.get("status") or "")
    if old_status == "killed":
        return existing
    if old_status == "exited" and new_status == "running":
        return existing
    if old_status == "exited" and new_status == "killed":
        return incoming
    return incoming


# LLM: Store diagnostics use the shared runtime error taxonomy and may expose only
# the authority path, never record contents.
# 函数用途: 把路径或 schema 异常转换成统一结构化错误。
def _store_error(
    exc: BaseException,
    context: str,
    *,
    path: Path | None = None,
) -> dict[str, object]:
    report = runtime_error_report(exc, context=context)
    if path is not None:
        report["path"] = str(path)
    return report


__all__ = [
    "PROCESS_SESSION_SCHEMA",
    "ProcessSessionLoadReport",
    "ProcessSessionStore",
    "process_session_store_root",
]
