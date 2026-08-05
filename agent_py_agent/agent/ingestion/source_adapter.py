from __future__ import annotations

"""Pure per-source request/response adapter for open-world Audit transports.

The adapter is source-local code written and verified during ``prepare``.  It
cannot perform I/O itself: the host gives it a committed opaque checkpoint,
the current time and a page budget, executes the planned HTTP request through
the existing network gate, then gives the JSON response back for framing.  The
same host still owns secrets, leases, durable records and checkpoint commit.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

from ..tooling.sandbox import SandboxSpec, SandboxUnavailable, build_bwrap_argv, find_bwrap
from .source_http import (
    SourceHttpRequest,
    public_source_http_request,
    render_source_adapter_request,
)

_PROTOCOL = "my-agent.audit-source-adapter.v1"
_SPEC_FIELDS = frozenset({"path", "sha256"})
_PLAN_FIELDS = frozenset({"request", "context"})
_ACCEPT_FIELDS = frozenset({"records", "checkpoint", "has_more", "record_keys"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_SCRIPT_BYTES = 256 * 1024
_MAX_INPUT_BYTES = 16 * 1024 * 1024
_MAX_OUTPUT_BYTES = 16 * 1024 * 1024
_MAX_CHECKPOINT_BYTES = 256 * 1024
_MAX_CONTEXT_BYTES = 256 * 1024
_MAX_RECORDS_PER_PAGE = 100_000
_TIMEOUT_SECONDS = 5.0


class SourceAdapterError(ValueError):
    """Typed adapter failure kept separate from HTTP/provider failures."""

    def __init__(self, message: str, code: str = "SOURCE_ADAPTER_INVALID") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AdapterPlan:
    request: SourceHttpRequest
    context: dict[str, Any]


@dataclass(frozen=True)
class AdapterPage:
    records: list[dict[str, Any]]
    checkpoint: dict[str, Any]
    has_more: bool
    record_keys: tuple[str, ...] = ()


def normalize_source_adapter(raw: object) -> dict[str, str]:
    """Canonicalize one pinned script reference; never accept inline code."""

    if not isinstance(raw, dict):
        raise SourceAdapterError("source_adapter 必须是对象")
    extras = sorted(str(key) for key in raw if key not in _SPEC_FIELDS)
    if extras:
        raise SourceAdapterError(f"source_adapter 包含未知字段: {extras}")
    selected = str(raw.get("path") or "").strip()
    if not selected or len(selected) > 512:
        raise SourceAdapterError("source_adapter.path 必须是 Audit 工作区内的相对路径")
    pure = PurePosixPath(selected.replace("\\", "/"))
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise SourceAdapterError("source_adapter.path 必须是 Audit 工作区内的安全相对路径")
    digest = str(raw.get("sha256") or "").strip().lower()
    if _SHA256_RE.fullmatch(digest) is None:
        raise SourceAdapterError("source_adapter.sha256 必须是 64 位小写十六进制")
    return {"path": pure.as_posix(), "sha256": digest}


def plan_source_request(
    *,
    owner_home: Path,
    audit_id: str,
    source_url: str,
    request_facts: dict[str, Any],
    adapter: dict[str, Any],
    checkpoint: object,
    page_limit: int,
    now_unix: float,
) -> AdapterPlan:
    """Ask the pure adapter how this one source should be queried next."""

    current = _checkpoint(checkpoint)
    payload = {
        "protocol": _PROTOCOL,
        "phase": "plan",
        "checkpoint": current,
        "now_unix": float(now_unix),
        "page_limit": max(1, int(page_limit)),
        "base_request": {
            "url": source_url,
            **public_source_http_request(request_facts),
        },
    }
    output = _invoke(owner_home, audit_id, adapter, payload)
    _reject_extras(output, _PLAN_FIELDS, phase="plan")
    context = _bounded_object(output.get("context"), "plan.context", _MAX_CONTEXT_BYTES)
    try:
        request = render_source_adapter_request(
            source_url,
            request_facts,
            output.get("request"),
        )
    except ValueError as exc:
        raise SourceAdapterError(str(exc), "SOURCE_REQUEST_INVALID") from exc
    return AdapterPlan(request=request, context=context)


def accept_source_response(
    *,
    owner_home: Path,
    audit_id: str,
    adapter: dict[str, Any],
    checkpoint: object,
    context: dict[str, Any],
    response: object,
    max_records: int,
) -> AdapterPage:
    """Frame a fetched response and return the next opaque source checkpoint."""

    current = _checkpoint(checkpoint)
    payload = {
        "protocol": _PROTOCOL,
        "phase": "accept",
        "checkpoint": current,
        "context": _bounded_object(context, "accept.context", _MAX_CONTEXT_BYTES),
        "response": response,
    }
    output = _invoke(owner_home, audit_id, adapter, payload)
    _reject_extras(output, _ACCEPT_FIELDS, phase="accept")
    record_limit = max(1, min(int(max_records), _MAX_RECORDS_PER_PAGE))
    raw_records = output.get("records")
    if not isinstance(raw_records, list) or len(raw_records) > record_limit:
        raise SourceAdapterError(
            f"accept.records 必须是最多 {record_limit} 项的数组",
            "SOURCE_PAGE_TOO_LARGE",
        )
    records = [item if isinstance(item, dict) else {"source_item": item} for item in raw_records]
    next_checkpoint = _checkpoint(output.get("checkpoint"))
    has_more = output.get("has_more")
    if not isinstance(has_more, bool):
        raise SourceAdapterError("accept.has_more 必须是布尔值")
    # ``has_more`` means the host will immediately request another page in the
    # same drain cycle, so a repeated checkpoint would spin without a
    # scheduling boundary.  A terminal page (has_more=False) is different: an
    # overlap-capable source may intentionally replay the last few source
    # positions while it waits at the live frontier.  The durable
    # source-position index filters those records before they reach the engine,
    # and the normal poll interval prevents a tight loop.  Requiring cursor
    # movement merely because that terminal page contains records would force
    # source adapters to invent fake checkpoints or drop the overlap.
    if has_more and _canonical_json(next_checkpoint) == _canonical_json(current):
        raise SourceAdapterError(
            "来源适配器声明本轮还有数据时必须推进 checkpoint",
            "SOURCE_CURSOR_STALLED",
        )
    record_keys = _record_keys(output.get("record_keys"), len(records))
    return AdapterPage(records, next_checkpoint, has_more, record_keys)


def _record_keys(raw: object, count: int) -> tuple[str, ...]:
    if raw is None:
        if count:
            raise SourceAdapterError(
                "accept.record_keys 必须为每条 records 提供来源位置键",
                "SOURCE_RECORD_KEYS_REQUIRED",
            )
        return ()
    if not isinstance(raw, list) or len(raw) != count:
        raise SourceAdapterError("accept.record_keys 必须与 records 一一对应")
    keys = tuple(str(item or "").strip() for item in raw)
    if any(not item or len(item) > 512 for item in keys):
        raise SourceAdapterError("accept.record_keys 含空值或超过 512 字符")
    if len(set(keys)) != len(keys):
        raise SourceAdapterError("accept.record_keys 在同一页内不能重复")
    return keys


def _checkpoint(value: object) -> dict[str, Any]:
    return _bounded_object(value if value is not None else {}, "checkpoint", _MAX_CHECKPOINT_BYTES)


def _bounded_object(value: object, label: str, maximum: int) -> dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise SourceAdapterError(f"{label} 必须是 JSON 对象")
    encoded = _canonical_json(value)
    if len(encoded) > maximum:
        raise SourceAdapterError(f"{label} 超过 {maximum} 字节")
    return json.loads(encoded.decode("utf-8"))


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SourceAdapterError("来源适配器数据必须是有限、可序列化的 JSON") from exc


def _reject_extras(payload: dict[str, Any], allowed: frozenset[str], *, phase: str) -> None:
    extras = sorted(str(key) for key in payload if key not in allowed)
    if extras:
        raise SourceAdapterError(f"{phase} 输出包含未知字段: {extras}")


def _invoke(
    owner_home: Path,
    audit_id: str,
    raw_adapter: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    adapter = normalize_source_adapter(raw_adapter)
    root, script = _resolved_script(owner_home, audit_id, adapter)
    encoded = _canonical_json(payload)
    if len(encoded) > _MAX_INPUT_BYTES:
        raise SourceAdapterError(
            f"来源适配器输入超过 {_MAX_INPUT_BYTES} 字节",
            "SOURCE_RECORD_TOO_LARGE",
        )
    bwrap = find_bwrap()
    if not bwrap:
        raise SourceAdapterError(
            "BWRAP_NOT_FOUND:来源适配器要求无网络 sandbox",
            "SANDBOX_UNAVAILABLE",
        )
    python = _system_python()
    try:
        argv = [
            *build_bwrap_argv(
                SandboxSpec(
                    owner_home=root,
                    workspace=script.parent,
                    write_roots=(),
                    bwrap_path=bwrap,
                    network_access=False,
                )
            ),
            "--",
            python,
            "-I",
            "-S",
            str(script),
        ]
    except SandboxUnavailable as exc:
        raise SourceAdapterError(str(exc), "SANDBOX_UNAVAILABLE") from exc
    env = {
        "HOME": str(root),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PYTHONIOENCODING": "utf-8",
    }
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            result = subprocess.run(
                argv,
                input=encoded,
                stdout=stdout,
                stderr=stderr,
                env=env,
                timeout=_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SourceAdapterError(
                "来源适配器执行超时",
                "SOURCE_ADAPTER_TIMEOUT",
            ) from exc
        stdout.flush()
        stderr.flush()
        output_size = os.fstat(stdout.fileno()).st_size
        error_size = os.fstat(stderr.fileno()).st_size
        if output_size > _MAX_OUTPUT_BYTES:
            raise SourceAdapterError("来源适配器输出超过安全上限")
        stdout.seek(0)
        stderr.seek(max(0, error_size - 2000))
        output = stdout.read()
        error_tail = stderr.read().decode("utf-8", "replace").strip()
    if result.returncode != 0:
        detail = f": {error_tail}" if error_tail else ""
        raise SourceAdapterError(f"来源适配器执行失败(exit={result.returncode}){detail}")
    try:
        decoded = json.loads(output.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceAdapterError("来源适配器必须只输出一个 UTF-8 JSON 对象") from exc
    if not isinstance(decoded, dict):
        raise SourceAdapterError("来源适配器输出必须是 JSON 对象")
    return decoded


def _resolved_script(
    owner_home: Path,
    audit_id: str,
    adapter: dict[str, str],
) -> tuple[Path, Path]:
    # Import lazily: conversation package initialization imports the source
    # binding schema, which in turn imports this validator.
    from ..conversation.workspace_paths import audit_workspace_path

    try:
        root = audit_workspace_path(owner_home, audit_id).resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise SourceAdapterError("来源适配器所属 Audit 工作区不可用") from exc
    relative = PurePosixPath(adapter["path"])
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        try:
            metadata = cursor.lstat()
        except OSError as exc:
            raise SourceAdapterError("来源适配器脚本不存在或不可读") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise SourceAdapterError("来源适配器路径不能包含符号链接")
    try:
        script = cursor.resolve(strict=True)
        script.relative_to(root)
    except (OSError, ValueError) as exc:
        raise SourceAdapterError("来源适配器脚本越过 Audit 工作区") from exc
    if not script.is_file():
        raise SourceAdapterError("来源适配器 path 不是普通文件")
    try:
        size = script.stat().st_size
        digest = sha256(script.read_bytes()).hexdigest()
    except OSError as exc:
        raise SourceAdapterError("来源适配器脚本当前不可读") from exc
    if size <= 0 or size > _MAX_SCRIPT_BYTES:
        raise SourceAdapterError(f"来源适配器脚本必须在 1–{_MAX_SCRIPT_BYTES} 字节之间")
    if digest != adapter["sha256"]:
        raise SourceAdapterError("来源适配器脚本哈希与已发布版本不一致")
    return root, script


def _system_python() -> str:
    candidate = shutil.which("python3", path="/usr/bin:/bin")
    if not candidate:
        raise SourceAdapterError("来源适配器运行节点缺少系统 Python", "SOURCE_ADAPTER_UNAVAILABLE")
    return candidate


__all__ = [
    "AdapterPage",
    "AdapterPlan",
    "SourceAdapterError",
    "accept_source_response",
    "normalize_source_adapter",
    "plan_source_request",
]
