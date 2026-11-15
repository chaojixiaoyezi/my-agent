"""Read-only telemetry, usage slicing, home isolation, and bg-* census helpers for Live Lab.

这个模块只从隔离目录和显式运行账本提取结构化观测事实，
不修改产品状态、不触碰真实宿主 ~/.my-agent、不触发任何网络调用。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_USAGE_EVENT_SCHEMA = "thread_model_usage_event.v1"
_COMPACT_CHECKPOINT_V3_SCHEMA = "conversation_compact_checkpoint.v3"
_PURPOSE_BUCKETS = ("main", "auxiliary", "decision", "probe:tool_capability")
_TERMINAL_PROCESS_STATUSES = frozenset({"exited", "killed", "not_started"})
_DEFAULT_NO_PROGRESS_LOOP_LIMIT = 3
_READ_CHUNK_BYTES = 65536


class LiveLabError(RuntimeError):
    """Raised when a Live Lab isolation rule, measurement contract, or command fails."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "",
        target_path: str = "",
        side_effects: str = "not_started",
        recommended_action: str = "",
    ) -> None:
        if error_code or target_path or recommended_action:
            code = error_code or "LIVE_LAB_ERROR"
            next_step = recommended_action or "Inspect the isolated Live Lab run directory and retry."
            super().__init__(
                f"{code}: {message} (target={target_path or '<unset>'}, "
                f"side_effects={side_effects}, next={next_step})"
            )
        else:
            super().__init__(message)
        self.error_code = error_code or "LIVE_LAB_ERROR"
        self.target_path = target_path
        self.side_effects = side_effects
        self.recommended_action = recommended_action


class LiveLabIsolationError(LiveLabError):
    """Raised when Live Lab paths or environment variables violate home isolation."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "HOME_ISOLATION_VIOLATION",
        target_path: str = "",
        side_effects: str = "not_started",
        recommended_action: str = "Use an isolated temp/run directory via MY_AGENT_HOME and HOME.",
    ) -> None:
        super().__init__(
            message,
            error_code=error_code,
            target_path=target_path,
            side_effects=side_effects,
            recommended_action=recommended_action,
        )


@dataclass(frozen=True)
class UsageFileCursor:
    """Byte and line offset cursor pinned to file identity."""

    path: str
    dev: int
    ino: int
    byte_offset: int
    line_offset: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_obj(cls, value: UsageFileCursor | Mapping[str, Any]) -> UsageFileCursor:
        if isinstance(value, UsageFileCursor):
            return value
        return cls(
            path=str(value.get("path") or ""),
            dev=int(value.get("dev") or 0),
            ino=int(value.get("ino") or 0),
            byte_offset=max(0, int(value.get("byte_offset") or 0)),
            line_offset=max(0, int(value.get("line_offset") or 0)),
        )


def real_user_home_dir() -> Path | None:
    """Return the host OS user home directory from account database when available."""
    try:
        import pwd

        entry = pwd.getpwuid(os.getuid())
        if entry and entry.pw_dir:
            return Path(entry.pw_dir).expanduser().resolve(strict=False)
    except Exception:
        return None
    return None


def default_host_agent_homes() -> tuple[Path, ...]:
    """Return candidate paths corresponding to the host real ~/.my-agent directory."""
    candidates: list[Path] = []
    pw_home = real_user_home_dir()
    if pw_home is not None:
        candidates.append((pw_home / ".my-agent").resolve(strict=False))
    user_env = os.environ.get("USER") or os.environ.get("LOGNAME")
    if user_env:
        expanded = Path(f"~{user_env}").expanduser()
        if str(expanded) != f"~{user_env}":
            candidates.append((expanded / ".my-agent").resolve(strict=False))
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            deduped.append(path)
    return tuple(deduped)


def is_host_agent_home_path(candidate: str | Path | None) -> tuple[bool, str]:
    """Check whether a path points at the host user's bare home or real ~/.my-agent."""
    if candidate is None:
        return False, ""
    raw = str(candidate).strip()
    if not raw:
        return False, ""
    resolved = Path(raw).expanduser().resolve(strict=False)
    pw_home = real_user_home_dir()
    if pw_home is not None and resolved == pw_home:
        return True, f"points directly to host user HOME ({pw_home})"
    for host_agent_home in default_host_agent_homes():
        if resolved == host_agent_home or resolved.is_relative_to(host_agent_home):
            return True, f"resolves inside host agent home ({host_agent_home})"
    return False, ""


def assert_isolated_home_path(candidate: str | Path, *, label: str = "MY_AGENT_HOME") -> Path:
    """Fail closed if a target home directory resolves to host HOME or ~/.my-agent."""
    raw = str(candidate).strip()
    if not raw:
        raise LiveLabIsolationError(
            f"{label} is empty",
            error_code="MISSING_ISOLATED_HOME",
            recommended_action=f"Pass a dedicated temporary/run directory for {label}.",
        )
    resolved = Path(raw).expanduser().resolve(strict=False)
    violates, reason = is_host_agent_home_path(resolved)
    if violates:
        raise LiveLabIsolationError(
            f"{label} {reason}",
            error_code="HOST_HOME_FORBIDDEN",
            target_path=str(resolved),
            recommended_action=f"Set {label} to an isolated temp or run directory outside real ~/.my-agent.",
        )
    return resolved


def prepare_isolated_subprocess_env(
    *,
    isolated_home: str | Path | None = None,
    run_root: str | Path | None = None,
    base_env: Mapping[str, str] | None = None,
    refuse_host_env: bool = False,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Build a strictly isolated child environment without touching host ~/.my-agent."""
    env = dict(os.environ if base_env is None else base_env)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")

    overridden: list[str] = []
    detected_host_refs: dict[str, str] = {}

    for key in ("MY_AGENT_HOME", "HOME", "TMPDIR"):
        flag, reason = is_host_agent_home_path(env.get(key))
        if flag:
            detected_host_refs[key] = reason

    if refuse_host_env and any(key in detected_host_refs for key in ("MY_AGENT_HOME", "HOME")):
        first_key = "MY_AGENT_HOME" if "MY_AGENT_HOME" in detected_host_refs else "HOME"
        raise LiveLabIsolationError(
            f"caller env {first_key} {detected_host_refs[first_key]}",
            error_code="CALLER_HOST_HOME_ENV_FORBIDDEN",
            target_path=str(env.get(first_key) or ""),
            recommended_action=f"Export isolated {first_key} under /private/tmp or pass isolated_home.",
        )

    if isolated_home is not None:
        safe_home = assert_isolated_home_path(isolated_home, label="isolated_home")
        safe_root = Path(run_root).expanduser().resolve(strict=False) if run_root is not None else safe_home.parent
        assert_isolated_home_path(safe_root, label="run_root")

        process_home = safe_root / "process_home"
        tmp_dir = safe_root / "tmp"
        process_home.mkdir(parents=True, exist_ok=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        if env.get("MY_AGENT_HOME") != str(safe_home):
            overridden.append("MY_AGENT_HOME")
        env["MY_AGENT_HOME"] = str(safe_home)

        current_home = env.get("HOME", "")
        home_unsafe, _ = is_host_agent_home_path(current_home)
        if not current_home or home_unsafe:
            overridden.append("HOME")
            env["HOME"] = str(process_home)

        current_tmp = env.get("TMPDIR", "")
        tmp_unsafe, _ = is_host_agent_home_path(current_tmp)
        if not current_tmp or tmp_unsafe:
            overridden.append("TMPDIR")
            env["TMPDIR"] = str(tmp_dir)
    else:
        for key in ("MY_AGENT_HOME", "HOME"):
            if key in env:
                assert_isolated_home_path(env[key], label=key)
        if "MY_AGENT_HOME" not in env and "HOME" not in env:
            raise LiveLabIsolationError(
                "neither isolated_home nor isolated HOME/MY_AGENT_HOME is configured",
                error_code="MISSING_ISOLATED_HOME",
                recommended_action="Configure isolated_home or export isolated HOME and MY_AGENT_HOME.",
            )

    provenance = {
        "isolated": True,
        "my_agent_home": env.get("MY_AGENT_HOME", ""),
        "home": env.get("HOME", ""),
        "tmpdir": env.get("TMPDIR", ""),
        "overridden_keys": overridden,
        "caller_host_refs_replaced": sorted(detected_host_refs),
    }
    return env, provenance


def resolve_live_lab_home_context(config_path: str | Path) -> dict[str, Any]:
    """Resolve owner home, compact dir, policy path, and effective compact policy via CLI rules."""
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.model.call_runtime import (
        compact_summary_output_reserve_tokens,
    )
    from agent_py_agent.agent.agent_core.model.context_pressure import model_request_input_ceiling
    from agent_py_agent.agent.agent_core.model.context_window import (
        resolve_model_context_window_tokens,
    )
    from agent_py_agent.agent.conversation.compact_threshold_policy import (
        resolve_thread_compact_policy,
    )
    from agent_py_agent.cli.common import make_lightweight_cli_agent_context

    resolved_cfg = Path(config_path).expanduser().resolve()
    context = make_lightweight_cli_agent_context(argparse.Namespace(config=str(resolved_cfg)))
    assert_isolated_home_path(context.home_paths.root, label="resolved_home_root")
    assert_isolated_home_path(context.home_paths.owner_home_dir, label="resolved_owner_home")

    agent_stub = SimpleNamespace(config=context.config, home_paths=context.home_paths)
    policy = resolve_thread_compact_policy(agent_stub, use_frozen=False)
    context_window = resolve_model_context_window_tokens(agent_stub)
    output_reserve = compact_summary_output_reserve_tokens(agent_stub)
    input_ceiling = model_request_input_ceiling(agent_stub, context_window) if context_window > 0 else 0

    conversation_workspace = Path(context.runtime_paths["conversation_workspace"])
    owner_home_dir = Path(context.home_paths.owner_home_dir)
    workspace_root = Path(
        getattr(context, "workspace", None)
        or getattr(context, "root", None)
        or (context.workspace_roots[0] if getattr(context, "workspace_roots", None) else ".")
    )
    process_store_root = _resolve_process_store_root(workspace_root, owner_home_dir)

    config_sources = getattr(context.config, "config_sources", None) or {}
    return {
        "config_path": str(resolved_cfg),
        "isolated_home_root": str(context.home_paths.root),
        "owner_home_dir": str(owner_home_dir),
        "owner_compact_dir": str(context.home_paths.owner_compact_dir),
        "owner_tool_policy_json": str(context.home_paths.owner_tool_policy_json),
        "owner_workspace_dir": str(context.home_paths.owner_workspace_dir),
        "workspace_root": str(workspace_root),
        "conversation_workspace": str(conversation_workspace),
        "model_usage_dir": str(conversation_workspace / "model_usage"),
        "threads_dir": str(conversation_workspace / "threads"),
        "process_store_root": str(process_store_root),
        "compact_policy": {
            "effective_trigger_percent": int(policy.get("effective_trigger_percent", 80)),
            "effective_source": str(policy.get("effective_source") or ""),
            "admin_default_percent": policy.get("admin_default_percent"),
            "user_default_percent": policy.get("user_default_percent"),
            "thread_override_percent": policy.get("thread_override_percent"),
            "force_locked": bool(policy.get("force_locked", False)),
            "auto_trigger_max_tokens": int(getattr(context.config, "memory_compact_auto_trigger_max_tokens", 0) or 0),
            "recovery_target_percent": int(getattr(context.config, "memory_compact_recovery_target_percent", 60) or 60),
            "context_window_tokens": int(context_window),
            "summary_max_output_tokens": int(getattr(context.config, "memory_compact_summary_max_output_tokens", 0) or 0),
            "output_reserve_tokens": int(output_reserve),
            "request_input_ceiling_tokens": int(input_ceiling),
            "compact_reasoning_level": str(getattr(context.config, "memory_compact_reasoning_level", "") or ""),
            "config_source": dict(config_sources.get("memory_compact_trigger_percent") or {}),
            "config_warnings": list(getattr(context.config, "config_warnings", ()) or ()),
        },
    }


def _resolve_process_store_root(workspace_root: Path, owner_home_dir: Path) -> Path:
    from agent_py_agent.agent.gateway_parts.background_resource_report import background_store_root

    return Path(background_store_root(workspace_root, owner_home_dir))


def discover_usage_ledger_files(
    *,
    model_usage_dir: str | Path | None = None,
    llm_usage_path: str | Path | None = None,
    search_roots: Sequence[str | Path] = (),
) -> list[Path]:
    """Discover existing usage JSONL files without scanning unrelated archives."""
    found: list[Path] = []
    seen: set[str] = set()

    def _add_file(path: Path) -> None:
        if not path.is_file():
            return
        resolved = str(path.resolve(strict=False))
        if resolved not in seen:
            seen.add(resolved)
            found.append(Path(resolved))

    if llm_usage_path is not None:
        _add_file(Path(llm_usage_path))
    if model_usage_dir is not None:
        usage_dir = Path(model_usage_dir)
        if usage_dir.is_dir():
            for entry in sorted(usage_dir.glob("*.jsonl")):
                _add_file(entry)
    for root in search_roots:
        root_path = Path(root)
        _add_file(root_path / "llm_usage.jsonl")
        _add_file(root_path / "model_usage" / "llm_usage.jsonl")
        sub_model_usage = root_path / "model_usage"
        if sub_model_usage.is_dir():
            for entry in sorted(sub_model_usage.glob("*.jsonl")):
                _add_file(entry)
    return found


def _complete_newline_end(path: Path, start_offset: int, file_size: int) -> tuple[int, int]:
    """Return byte offset immediately after the last newline inside [start_offset, file_size)."""
    if file_size <= start_offset:
        return start_offset, 0
    with path.open("rb") as handle:
        handle.seek(max(0, file_size - 1))
        if handle.read(1) == b"\n":
            return file_size, 0
        # Scan backwards in bounded chunks so incomplete tail lines are never dropped or half-parsed.
        cursor = file_size
        while cursor > start_offset:
            chunk_start = max(start_offset, cursor - _READ_CHUNK_BYTES)
            handle.seek(chunk_start)
            data = handle.read(cursor - chunk_start)
            pos = data.rfind(b"\n")
            if pos >= 0:
                complete_end = chunk_start + pos + 1
                return complete_end, file_size - complete_end
            cursor = chunk_start
    return start_offset, file_size - start_offset


def _count_newlines_between(path: Path, start_offset: int, end_offset: int) -> int:
    if end_offset <= start_offset:
        return 0
    count = 0
    with path.open("rb") as handle:
        handle.seek(start_offset)
        remaining = end_offset - start_offset
        while remaining > 0:
            chunk = handle.read(min(_READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            count += chunk.count(b"\n")
            remaining -= len(chunk)
    return count


def snapshot_usage_cursors(files: Sequence[str | Path]) -> dict[str, UsageFileCursor]:
    """Capture byte and line cursors at the end of complete lines for each existing usage file."""
    cursors: dict[str, UsageFileCursor] = {}
    for item in files:
        path = Path(item)
        if not path.is_file():
            continue
        resolved = str(path.resolve(strict=False))
        st = path.stat()
        complete_end, _pending = _complete_newline_end(path, 0, st.st_size)
        line_count = _count_newlines_between(path, 0, complete_end)
        cursors[resolved] = UsageFileCursor(
            path=resolved,
            dev=int(st.st_dev),
            ino=int(st.st_ino),
            byte_offset=complete_end,
            line_offset=line_count,
        )
    return cursors


def read_usage_delta_since(
    files: Sequence[str | Path],
    before_cursors: Mapping[str, UsageFileCursor | Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, UsageFileCursor], dict[str, Any]]:
    """Read only complete JSONL rows appended since before_cursors."""
    prior_map: dict[str, UsageFileCursor] = {}
    for key, val in (before_cursors or {}).items():
        cursor = UsageFileCursor.from_obj(val)
        resolved_key = str(Path(cursor.path or key).resolve(strict=False))
        prior_map[resolved_key] = UsageFileCursor(
            path=resolved_key,
            dev=cursor.dev,
            ino=cursor.ino,
            byte_offset=cursor.byte_offset,
            line_offset=cursor.line_offset,
        )

    rows: list[dict[str, Any]] = []
    after_cursors: dict[str, UsageFileCursor] = dict(prior_map)
    parse_errors: list[dict[str, Any]] = []
    pending_tail_bytes_by_file: dict[str, int] = {}
    reset_files: list[str] = []

    for item in files:
        path = Path(item)
        if not path.is_file():
            continue
        resolved = str(path.resolve(strict=False))
        st = path.stat()
        prior = prior_map.get(resolved)
        start_offset = 0
        start_line = 0
        if prior is not None:
            same_inode = prior.dev == int(st.st_dev) and prior.ino == int(st.st_ino)
            if same_inode and st.st_size >= prior.byte_offset:
                start_offset = prior.byte_offset
                start_line = prior.line_offset
            else:
                reset_files.append(resolved)

        complete_end, pending_bytes = _complete_newline_end(path, start_offset, st.st_size)
        if pending_bytes > 0:
            pending_tail_bytes_by_file[resolved] = pending_bytes

        delta_lines = 0
        if complete_end > start_offset:
            with path.open("rb") as handle:
                handle.seek(start_offset)
                raw_chunk = handle.read(complete_end - start_offset)
            for idx, raw_line in enumerate(raw_chunk.splitlines(), start=1):
                delta_lines += 1
                text = raw_line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError as exc:
                    parse_errors.append({
                        "path": resolved,
                        "line": start_line + idx,
                        "error": f"JSONDecodeError: {exc.msg}",
                    })
                    continue
                if not isinstance(parsed, dict):
                    parse_errors.append({
                        "path": resolved,
                        "line": start_line + idx,
                        "error": f"Expected JSON object, got {type(parsed).__name__}",
                    })
                    continue
                tagged = dict(parsed)
                tagged.setdefault("_source_file", resolved)
                tagged.setdefault("_source_line", start_line + idx)
                rows.append(tagged)

        after_cursors[resolved] = UsageFileCursor(
            path=resolved,
            dev=int(st.st_dev),
            ino=int(st.st_ino),
            byte_offset=complete_end,
            line_offset=start_line + delta_lines,
        )

    metadata = {
        "parse_errors": parse_errors,
        "pending_tail_bytes": pending_tail_bytes_by_file,
        "reset_files": reset_files,
    }
    return rows, after_cursors, metadata


def _opt_nonneg_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float) and value.is_integer():
        return max(0, int(value))
    return None


def normalize_usage_record(row: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a canonical thread_model_usage_event.v1 or flat llm_usage.jsonl row."""
    if str(row.get("schema_version") or "") == _USAGE_EVENT_SCHEMA or isinstance(row.get("model_calls"), dict):
        return _normalize_model_usage_event(row)
    return _normalize_flat_usage_row(row)


def _normalize_model_usage_event(row: Mapping[str, Any]) -> dict[str, Any]:
    calls = row.get("model_calls") if isinstance(row.get("model_calls"), Mapping) else {}
    breakdown = calls.get("usage_breakdown") if isinstance(calls.get("usage_breakdown"), Mapping) else {}
    provider = breakdown.get("provider") if isinstance(breakdown.get("provider"), Mapping) else {}
    estimated = breakdown.get("estimated") if isinstance(breakdown.get("estimated"), Mapping) else {}

    provider_calls = _opt_nonneg_int(provider.get("call_count")) or 0
    estimated_calls = _opt_nonneg_int(estimated.get("call_count")) or 0
    call_count = provider_calls + estimated_calls
    if call_count == 0:
        call_count = _opt_nonneg_int(calls.get("logical_model_turn_count")) or 1

    input_reported = _opt_nonneg_int(provider.get("input_tokens_reported_call_count"))
    output_reported = _opt_nonneg_int(provider.get("output_tokens_reported_call_count"))
    cache_reported = _opt_nonneg_int(provider.get("cache_read_input_tokens_reported_call_count"))

    inp = _opt_nonneg_int(provider.get("input_tokens"))
    if inp is None:
        inp = _opt_nonneg_int(calls.get("accounted_input_tokens"))
    elif (input_reported == 0 or (input_reported is None and provider_calls == 0)) and (
        (_opt_nonneg_int(estimated.get("input_tokens")) or 0) > 0
    ):
        inp = _opt_nonneg_int(estimated.get("input_tokens"))
    elif input_reported == 0 and inp == 0 and provider_calls > 0:
        inp = None

    out = _opt_nonneg_int(provider.get("output_tokens"))
    if out is None:
        out = _opt_nonneg_int(calls.get("output_tokens"))
    elif (output_reported == 0 or (output_reported is None and provider_calls == 0)) and (
        (_opt_nonneg_int(estimated.get("output_tokens")) or 0) > 0
    ):
        out = _opt_nonneg_int(estimated.get("output_tokens"))
    elif output_reported == 0 and out == 0 and provider_calls > 0:
        out = None

    cached = _opt_nonneg_int(provider.get("cache_read_input_tokens"))
    if cached is None:
        cached = _opt_nonneg_int(calls.get("cached_input_tokens"))
    if cache_reported is not None and cache_reported == 0 and cached == 0:
        cached = None

    purposes_raw = calls.get("purpose_breakdown")
    sub_purposes: dict[str, dict[str, Any]] = {}
    if isinstance(purposes_raw, Mapping):
        for p_key, p_val in purposes_raw.items():
            if str(p_key) == "schema" or not isinstance(p_val, Mapping):
                continue
            sub_purposes[str(p_key)] = _normalize_model_usage_event({"model_calls": p_val, "source": str(p_key)})

    purpose = str(row.get("purpose") or row.get("source") or "main").strip() or "main"
    return {
        "purpose": purpose,
        "input_tokens": inp,
        "output_tokens": out,
        "cached_tokens": cached,
        "call_count": call_count,
        "physical_call_count": _opt_nonneg_int(calls.get("physical_model_attempt_count")) or call_count,
        "http_attempt_count": _opt_nonneg_int(calls.get("provider_http_attempt_count")) or call_count,
        "unfinished_call_count": _opt_nonneg_int(estimated.get("unfinished_call_count")) or 0,
        "input_reporting_calls": input_reported if input_reported is not None else (call_count if inp is not None else 0),
        "output_reporting_calls": output_reported if output_reported is not None else (call_count if out is not None else 0),
        "cache_reporting_calls": cache_reported if cache_reported is not None else (call_count if cached is not None else 0),
        "purpose_breakdown": sub_purposes,
        "thread_id": str(row.get("thread_id") or ""),
        "request_id": str(row.get("request_id") or ""),
        "run_id": str(row.get("run_id") or ""),
        "compaction_occurred": bool(row.get("compaction_occurred") or False),
    }


def _normalize_flat_usage_row(row: Mapping[str, Any]) -> dict[str, Any]:
    nested = row.get("usage") if isinstance(row.get("usage"), Mapping) else {}
    inp = _first_opt_int(
        row.get("input_tokens"),
        row.get("prompt_tokens"),
        row.get("accounted_input_tokens"),
        nested.get("input_tokens"),
        nested.get("prompt_tokens"),
    )
    out = _first_opt_int(
        row.get("output_tokens"),
        row.get("completion_tokens"),
        nested.get("output_tokens"),
        nested.get("completion_tokens"),
    )
    cached = _first_opt_int(
        row.get("cached_tokens"),
        row.get("cached_input_tokens"),
        row.get("cache_read_input_tokens"),
        nested.get("cached_tokens"),
        nested.get("cached_input_tokens"),
        nested.get("cache_read_input_tokens"),
    )
    status = str(row.get("status") or "").strip().lower()
    call_count = _opt_nonneg_int(row.get("call_count"))
    if call_count is None:
        call_count = 1
    unfinished = 1 if status in {"failed", "timed_out", "error", "unfinished"} else 0
    purpose = str(row.get("purpose") or row.get("source") or "main").strip() or "main"
    return {
        "purpose": purpose,
        "input_tokens": inp,
        "output_tokens": out,
        "cached_tokens": cached,
        "call_count": call_count,
        "physical_call_count": _opt_nonneg_int(row.get("physical_call_count")) or call_count,
        "http_attempt_count": _opt_nonneg_int(row.get("http_attempt_count")) or call_count,
        "unfinished_call_count": _opt_nonneg_int(row.get("unfinished_call_count")) or unfinished,
        "input_reporting_calls": call_count if inp is not None else 0,
        "output_reporting_calls": call_count if out is not None else 0,
        "cache_reporting_calls": call_count if cached is not None else 0,
        "purpose_breakdown": {},
        "thread_id": str(row.get("thread_id") or ""),
        "request_id": str(row.get("request_id") or ""),
        "run_id": str(row.get("run_id") or ""),
        "compaction_occurred": bool(row.get("compaction_occurred") or False),
    }


def _first_opt_int(*candidates: Any) -> int | None:
    for item in candidates:
        value = _opt_nonneg_int(item)
        if value is not None:
            return value
    return None


def _empty_bucket_metrics() -> dict[str, Any]:
    return {
        "call_count": 0,
        "physical_call_count": 0,
        "http_attempt_count": 0,
        "unfinished_call_count": 0,
        "input_tokens": None,
        "output_tokens": None,
        "cached_tokens": None,
        "input_reporting_calls": 0,
        "output_reporting_calls": 0,
        "cache_reporting_calls": 0,
        "paired_cache_calls": 0,
        "paired_input_tokens": 0,
        "paired_cached_tokens": 0,
        "observed_cached_ratio": None,
        "coverage": "complete",
    }


def _accumulate_bucket(bucket: dict[str, Any], item: Mapping[str, Any]) -> None:
    calls = max(0, int(item.get("call_count") or 0))
    bucket["call_count"] += calls
    bucket["physical_call_count"] += max(0, int(item.get("physical_call_count") or 0))
    bucket["http_attempt_count"] += max(0, int(item.get("http_attempt_count") or 0))
    bucket["unfinished_call_count"] += max(0, int(item.get("unfinished_call_count") or 0))

    inp = item.get("input_tokens")
    out = item.get("output_tokens")
    cached = item.get("cached_tokens")
    if inp is not None:
        bucket["input_tokens"] = (bucket["input_tokens"] or 0) + int(inp)
        bucket["input_reporting_calls"] += max(0, int(item.get("input_reporting_calls") or calls))
    if out is not None:
        bucket["output_tokens"] = (bucket["output_tokens"] or 0) + int(out)
        bucket["output_reporting_calls"] += max(0, int(item.get("output_reporting_calls") or calls))
    if cached is not None:
        bucket["cached_tokens"] = (bucket["cached_tokens"] or 0) + int(cached)
        bucket["cache_reporting_calls"] += max(0, int(item.get("cache_reporting_calls") or calls))
    if inp is not None and cached is not None:
        bucket["paired_cache_calls"] += max(0, min(
            int(item.get("input_reporting_calls") or calls),
            int(item.get("cache_reporting_calls") or calls),
        ) or calls)
        bucket["paired_input_tokens"] += int(inp)
        bucket["paired_cached_tokens"] += int(cached)


def _finalize_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    paired_calls = bucket["paired_cache_calls"]
    paired_inp = bucket["paired_input_tokens"]
    paired_cached = bucket["paired_cached_tokens"]
    if paired_calls > 0 and paired_inp > 0:
        bucket["observed_cached_ratio"] = round(paired_cached / paired_inp, 6)
    else:
        bucket["observed_cached_ratio"] = None

    total_calls = bucket["call_count"]
    if total_calls == 0:
        bucket["input_tokens"] = 0
        bucket["output_tokens"] = 0
        bucket["cached_tokens"] = 0
        bucket["coverage"] = "complete"
    elif (
        bucket["input_reporting_calls"] >= total_calls
        and bucket["output_reporting_calls"] >= total_calls
        and bucket["cache_reporting_calls"] >= total_calls
    ):
        bucket["coverage"] = "complete"
    elif (
        bucket["input_reporting_calls"] == 0
        and bucket["output_reporting_calls"] == 0
        and bucket["cache_reporting_calls"] == 0
    ):
        bucket["coverage"] = "unknown"
    else:
        bucket["coverage"] = "partial"
    return bucket


def aggregate_usage_records(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate normalized or raw usage rows with strict missing-counter preservation."""
    total = _empty_bucket_metrics()
    by_purpose: dict[str, dict[str, Any]] = {}

    for raw in rows:
        item = normalize_usage_record(raw)
        _accumulate_bucket(total, item)
        sub = item.get("purpose_breakdown") or {}
        if isinstance(sub, Mapping) and sub:
            for p_key, p_item in sub.items():
                target = by_purpose.setdefault(str(p_key), _empty_bucket_metrics())
                _accumulate_bucket(target, p_item)
        else:
            p_key = str(item.get("purpose") or "main")
            target = by_purpose.setdefault(p_key, _empty_bucket_metrics())
            _accumulate_bucket(target, item)

    _finalize_bucket(total)
    finalized_purposes = {
        key: _finalize_bucket(val)
        for key, val in sorted(by_purpose.items())
        if val["call_count"] > 0 or key in _PURPOSE_BUCKETS
    }
    return {
        "schema": "live_lab_turn_usage.v1",
        **total,
        "purpose_breakdown": finalized_purposes,
    }


def slice_usage_jsonl_turns(
    path: str | Path,
    turn_boundaries: Sequence[tuple[int, int] | Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Slice a single usage JSONL file by explicit line or byte ranges for offline analysis."""
    file_path = Path(path)
    if not file_path.is_file():
        return []
    lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    if turn_boundaries is None:
        parsed_rows = _parse_json_lines(lines)
        grouped: dict[Any, list[dict[str, Any]]] = {}
        for row in parsed_rows:
            if "turn" in row and row.get("turn") is not None:
                turn_key = row["turn"]
            elif row.get("turn_id") is not None:
                turn_key = row["turn_id"]
            elif row.get("request_id") is not None:
                turn_key = row["request_id"]
            else:
                turn_key = "unattributed"
            grouped.setdefault(turn_key, []).append(row)
        return [
            {"turn": key, **aggregate_usage_records(items)}
            for key, items in grouped.items()
        ]

    sliced: list[dict[str, Any]] = []
    for idx, boundary in enumerate(turn_boundaries):
        if isinstance(boundary, tuple):
            start, end = boundary
            label = f"turn_{idx + 1}"
            subset = lines[max(0, start):max(0, end)]
        else:
            start = int(boundary.get("start_line") or 0)
            end = int(boundary.get("end_line") or len(lines))
            label = str(boundary.get("label") or f"turn_{idx + 1}")
            subset = lines[max(0, start):max(0, end)]
        sliced.append({"turn": label, **aggregate_usage_records(_parse_json_lines(subset))})
    return sliced


def _parse_json_lines(lines: Sequence[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in lines:
        text = raw.strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def compute_pre_post_compaction_cache_ratios(
    turn_usages: Sequence[Mapping[str, Any]],
    *,
    compaction_turn_indices: Sequence[int] = (),
) -> dict[str, Any]:
    """Compute observed-subset cached_tokens ratio before and after compaction turns."""
    split_index: int | None = min(compaction_turn_indices) if compaction_turn_indices else None
    if split_index is None:
        for idx, turn in enumerate(turn_usages):
            if turn.get("compaction_occurred"):
                split_index = idx
                break

    if split_index is None:
        pre_turns = list(turn_usages)
        post_turns: list[Mapping[str, Any]] = []
    else:
        pre_turns = list(turn_usages[:split_index])
        post_turns = list(turn_usages[split_index:])

    first_post = post_turns[:1]
    later_post = post_turns[1:]

    return {
        "split_turn_index": split_index,
        "pre_compaction": _summarize_turn_slice_cache(pre_turns),
        "post_compaction": _summarize_turn_slice_cache(post_turns),
        "first_post_compaction": _summarize_turn_slice_cache(first_post),
        "later_post_compaction": _summarize_turn_slice_cache(later_post),
    }


def _summarize_turn_slice_cache(turns: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    paired_input = 0
    paired_cached = 0
    paired_calls = 0
    total_calls = 0
    for turn in turns:
        total_calls += max(0, int(turn.get("call_count") or 0))
        if "paired_input_tokens" in turn and "paired_cached_tokens" in turn:
            p_calls = max(0, int(turn.get("paired_cache_calls") or 0))
            if p_calls > 0:
                paired_calls += p_calls
                paired_input += max(0, int(turn.get("paired_input_tokens") or 0))
                paired_cached += max(0, int(turn.get("paired_cached_tokens") or 0))
        else:
            inp = _opt_nonneg_int(turn.get("input_tokens"))
            cached = _opt_nonneg_int(turn.get("cached_tokens"))
            if inp is not None and cached is not None:
                paired_calls += max(1, int(turn.get("call_count") or 1))
                paired_input += inp
                paired_cached += cached

    ratio = round(paired_cached / paired_input, 6) if paired_calls > 0 and paired_input > 0 else None
    return {
        "turn_count": len(turns),
        "call_count": total_calls,
        "paired_cache_calls": paired_calls,
        "paired_input_tokens": paired_input,
        "paired_cached_tokens": paired_cached,
        "observed_cached_ratio": ratio,
    }


def snapshot_compaction_baseline(
    *,
    compact_dir: str | Path,
    threads_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Snapshot existing committed compact checkpoints before running a stress case."""
    c_dir = Path(compact_dir)
    t_dir = Path(threads_dir) if threads_dir is not None else None
    threads = _load_thread_records(t_dir)
    generations: dict[str, int] = {}
    checkpoint_ids: dict[str, str] = {}
    for thread_id, thread in threads.items():
        generations[thread_id] = max(0, int(thread.get("compact_generation") or 0))
        checkpoint_ids[thread_id] = str(thread.get("compact_checkpoint_id") or "")

    # Also capture any raw JSONL files without thread metadata for standalone dir reuse detection.
    convs_dir = c_dir / "conversations"
    max_gen_by_file: dict[str, int] = {}
    if convs_dir.is_dir():
        for entry in sorted(convs_dir.glob("*.jsonl")):
            tid = entry.stem
            if tid not in generations:
                rows, _errs = _read_checkpoint_rows(entry)
                max_gen_by_file[tid] = max((int(r.get("generation") or 0) for r in rows), default=0)

    return {
        "sampled_at": time.time(),
        "thread_generations": generations,
        "thread_checkpoint_ids": checkpoint_ids,
        "orphan_ledger_max_generations": max_gen_by_file,
    }


def _load_thread_records(threads_dir: Path | None) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    if threads_dir is None or not threads_dir.is_dir():
        return records
    for path in sorted(threads_dir.glob("*.json")):
        if path.name.startswith("."):
            continue
        try:
            with path.open("r", encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(record, dict):
            tid = str(record.get("thread_id") or path.stem).strip()
            if tid:
                records[tid] = record
    return records


def _read_checkpoint_rows(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    if not path.is_file():
        return rows, errors
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, raw in enumerate(handle, start=1):
            text = raw.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError as exc:
                errors.append({"path": str(path), "line": line_number, "error": f"JSONDecodeError: {exc.msg}"})
                continue
            if not isinstance(obj, dict):
                errors.append({"path": str(path), "line": line_number, "error": "checkpoint row must be dict"})
                continue
            rows.append(obj)
    return rows, errors


def _cold_checkpoint_copy(compact_dir: Path, thread_id: str, checkpoint_id: str) -> dict[str, Any] | None:
    digest = hashlib.sha256(checkpoint_id.encode("utf-8")).hexdigest() + ".checkpoint.json"
    candidate = compact_dir / "archive" / thread_id / digest
    if not candidate.is_file():
        return None
    try:
        with candidate.open("r", encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return record if isinstance(record, dict) else None


def load_canonical_compaction_checkpoints(
    *,
    compact_dir: str | Path,
    threads_dir: str | Path | None = None,
    thread_id: str | None = None,
    baseline: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Read canonical committed checkpoint chains and separate failed/superseded candidates."""
    c_dir = Path(compact_dir)
    t_dir = Path(threads_dir) if threads_dir is not None else None
    threads = _load_thread_records(t_dir)
    baseline_gens = dict((baseline or {}).get("thread_generations") or {})

    convs_dir = c_dir / "conversations"
    ledger_files = sorted(convs_dir.glob("*.jsonl")) if convs_dir.is_dir() else []
    candidate_threads: list[str] = []
    seen_tids: set[str] = set()
    for tid in sorted(threads):
        if thread_id is not None and tid != thread_id:
            continue
        seen_tids.add(tid)
        candidate_threads.append(tid)
    for path in ledger_files:
        tid = path.stem
        if thread_id is not None and tid != thread_id:
            continue
        if tid not in seen_tids:
            seen_tids.add(tid)
            candidate_threads.append(tid)

    committed_cycles: list[dict[str, Any]] = []
    uncommitted_candidates: list[dict[str, Any]] = []
    chain_errors: list[dict[str, Any]] = []

    for tid in candidate_threads:
        thread = threads.get(tid)
        ledger_path = convs_dir / f"{tid}.jsonl"
        all_rows, read_errs = _read_checkpoint_rows(ledger_path)
        chain_errors.extend(read_errs)

        by_id: dict[str, dict[str, Any]] = {}
        for row in all_rows:
            cid = str(row.get("checkpoint_id") or "").strip()
            if cid:
                by_id[cid] = row

        committed_ids: set[str] = set()
        chain_rows: list[dict[str, Any]] = []
        if thread is not None:
            head_id = str(thread.get("compact_checkpoint_id") or "").strip()
            head_gen = max(0, int(thread.get("compact_generation") or 0))
            if head_id and head_gen > 0:
                current_id = head_id
                expected_gen = head_gen
                visited: set[str] = set()
                while current_id:
                    if current_id in visited:
                        chain_errors.append({"thread_id": tid, "error": "checkpoint chain cycle detected"})
                        break
                    visited.add(current_id)
                    row = by_id.get(current_id) or _cold_checkpoint_copy(c_dir, tid, current_id)
                    if row is None:
                        chain_errors.append({
                            "thread_id": tid,
                            "checkpoint_id": current_id,
                            "error": "missing checkpoint row in canonical chain",
                        })
                        break
                    row_gen = _opt_nonneg_int(row.get("generation")) or 0
                    if row_gen != expected_gen:
                        chain_errors.append({
                            "thread_id": tid,
                            "checkpoint_id": current_id,
                            "error": f"generation mismatch expected={expected_gen} actual={row_gen}",
                        })
                        break
                    committed_ids.add(current_id)
                    chain_rows.append(row)
                    current_id = str(row.get("previous_checkpoint_id") or "").strip()
                    expected_gen -= 1
                chain_rows.reverse()
        else:
            # No thread JSON was provided (standalone checkpoint file): treat explicit committed rows only.
            for row in sorted(all_rows, key=lambda item: int(item.get("generation") or 0)):
                status = str(row.get("status") or "").strip().lower()
                if status in {"committed", "validated_candidate"} and row.get("checkpoint_id"):
                    cid = str(row["checkpoint_id"])
                    committed_ids.add(cid)
                    chain_rows.append(row)

        base_gen = max(0, int(baseline_gens.get(tid) or 0))
        for row in chain_rows:
            gen = max(0, int(row.get("generation") or 0))
            if gen <= base_gen:
                continue
            before_tokens = _opt_nonneg_int(row.get("projected_tokens_before")) or 0
            after_tokens = _opt_nonneg_int(row.get("projected_tokens_after")) or 0
            ratio = round(before_tokens / after_tokens, 4) if after_tokens > 0 else None
            reduction = (
                round(100.0 * (before_tokens - after_tokens) / before_tokens, 2)
                if before_tokens > 0
                else None
            )
            committed_cycles.append({
                "thread_id": tid,
                "generation": gen,
                "checkpoint_id": str(row.get("checkpoint_id") or ""),
                "previous_checkpoint_id": str(row.get("previous_checkpoint_id") or ""),
                "schema": str(row.get("schema") or ""),
                "before_tokens": before_tokens,
                "after_tokens": after_tokens,
                "delta_tokens": before_tokens - after_tokens,
                "ratio": ratio,
                "reduction_percent": reduction,
                "trigger_tokens": _opt_nonneg_int(row.get("trigger_tokens")) or 0,
                "context_window_tokens": _opt_nonneg_int(row.get("context_window_tokens")) or 0,
                "recovery_target_tokens": _opt_nonneg_int(row.get("recovery_target_tokens")) or 0,
                "forced": bool(row.get("forced")),
                "mode": _infer_checkpoint_mode(row),
                "backend": str(row.get("backend") or ""),
                "model": str(row.get("model") or ""),
                "summary_sha256": str(row.get("summary_sha256") or ""),
                "created_at": row.get("created_at"),
            })

        for row in all_rows:
            cid = str(row.get("checkpoint_id") or "").strip()
            if cid and cid in committed_ids:
                continue
            uncommitted_candidates.append({
                "thread_id": tid,
                "checkpoint_id": cid,
                "generation": _opt_nonneg_int(row.get("generation")) or 0,
                "status": str(row.get("status") or ""),
                "reason_code": str(row.get("reason_code") or row.get("error_code") or ""),
            })

    ratios = [cycle["ratio"] for cycle in committed_cycles if cycle["ratio"] is not None]
    return {
        "schema": "live_lab_compaction_checkpoints.v1",
        "successful_cycle_count": len(committed_cycles),
        "uncommitted_or_superseded_count": len(uncommitted_candidates),
        "cycles": committed_cycles,
        "uncommitted_candidates": uncommitted_candidates,
        "ratio_min": min(ratios) if ratios else None,
        "ratio_median": round(statistics.median(ratios), 4) if ratios else None,
        "errors": chain_errors,
    }


def _infer_checkpoint_mode(row: Mapping[str, Any]) -> str:
    explicit = _classify_compaction_mode_fields(row)
    if explicit != "unknown":
        return explicit
    if isinstance(row.get("provider_compaction"), Mapping) or "provider_compaction_ref" in row:
        return "provider_compaction"
    return "unknown"


def _classify_compaction_mode_fields(record: Mapping[str, Any]) -> str:
    for key in ("mode", "compact_mode", "compaction_mode", "summary_mode", "source_mode"):
        raw = str(record.get(key) or "").strip().lower()
        if raw in {"cache_fork", "fork", "active_turn_cache_fork"}:
            return "cache_fork"
        if raw in {"independent_summary", "independent", "no_prefix", "active_turn_summary_no_prefix"}:
            return "independent_summary"
        if raw in {"provider_compaction", "remote_compaction"}:
            return "provider_compaction"
        if raw in {"turn_local", "turn_local_recovery"}:
            return "turn_local"
    if record.get("cache_fork_used") is True or record.get("used_cache_fork") is True:
        return "cache_fork"
    if record.get("cache_fork_used") is False or record.get("used_cache_fork") is False:
        return "independent_summary"
    tool_choice = record.get("tool_choice")
    if isinstance(tool_choice, Mapping) and str(tool_choice.get("reason") or "") == "active_turn_summary_no_prefix":
        return "independent_summary"
    if record.get("tool_choice_reason") == "active_turn_summary_no_prefix":
        return "independent_summary"
    return "unknown"


def parse_compaction_telemetry(
    sources: Sequence[Mapping[str, Any] | str | Path],
    *,
    no_progress_loop_limit: int = _DEFAULT_NO_PROGRESS_LOOP_LIMIT,
) -> dict[str, Any]:
    """Parse structured compaction events/logs for mode, COMPACT_NO_PROGRESS, and RuntimeFactsTurn counts."""
    records: list[Mapping[str, Any]] = []
    text_lines: list[str] = []

    for item in sources:
        if isinstance(item, Mapping):
            records.append(item)
        elif isinstance(item, Path) or (isinstance(item, str) and "\n" not in item and len(item) < 512 and Path(item).is_file()):
            path = Path(item)
            if path.is_file():
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    for raw in handle:
                        _ingest_telemetry_line(raw, records, text_lines)
        elif isinstance(item, str):
            for raw in item.splitlines():
                _ingest_telemetry_line(raw, records, text_lines)

    mode_counts = {
        "cache_fork": 0,
        "independent_summary": 0,
        "provider_compaction": 0,
        "turn_local": 0,
        "unknown": 0,
    }
    no_progress_events: list[dict[str, Any]] = []
    turn_local_attempts = 0
    runtime_facts_counts: list[int] = []
    max_consecutive_no_progress = 0
    current_consecutive_no_progress = 0

    for rec in records:
        reason = str(rec.get("reason_code") or rec.get("error_code") or rec.get("code") or "").strip()
        event_name = str(rec.get("event") or rec.get("phase") or rec.get("stage") or "").strip().lower()
        mode = _classify_compaction_mode_fields(rec)

        is_no_progress = reason == "COMPACT_NO_PROGRESS" or "COMPACT_NO_PROGRESS" in str(rec.get("message") or "")
        if is_no_progress:
            current_consecutive_no_progress += 1
            max_consecutive_no_progress = max(max_consecutive_no_progress, current_consecutive_no_progress)
            no_progress_events.append({
                "reason_code": "COMPACT_NO_PROGRESS",
                "committed": bool(rec.get("committed", False)),
                "thread_id": str(rec.get("thread_id") or ""),
                "generation": _opt_nonneg_int(rec.get("generation")),
                "tokens_before": _first_opt_int(rec.get("tokens_before"), rec.get("projected_tokens_before"), rec.get("before_tokens")),
                "candidate_tokens": _first_opt_int(rec.get("candidate_tokens"), rec.get("tokens_after"), rec.get("after_tokens")),
                "operation_id": str(rec.get("operation_id") or ""),
            })
        elif mode in {"cache_fork", "independent_summary", "provider_compaction"} or (
            event_name in {"committed", "applied", "done", "complete"} and rec.get("committed") is True
        ):
            current_consecutive_no_progress = 0

        if mode == "turn_local" or reason == "COMPACT_LOCAL_RECOVERY_EXHAUSTED":
            turn_local_attempts += 1

        if mode != "unknown" or _looks_like_compaction_event(rec):
            mode_counts[mode if mode in mode_counts else "unknown"] += 1

        rt_count = _extract_runtime_facts_count(rec)
        if rt_count is not None:
            runtime_facts_counts.append(rt_count)

    for line in text_lines:
        if "COMPACT_NO_PROGRESS" in line:
            current_consecutive_no_progress += 1
            max_consecutive_no_progress = max(max_consecutive_no_progress, current_consecutive_no_progress)
            no_progress_events.append({
                "reason_code": "COMPACT_NO_PROGRESS",
                "committed": False,
                "raw_line": line[:240],
            })
        elif "COMPACT_LOCAL_RECOVERY_EXHAUSTED" in line:
            turn_local_attempts += 1

    loop_limit = max(1, int(no_progress_loop_limit))
    return {
        "schema": "live_lab_compaction_telemetry.v1",
        "mode_counts": mode_counts,
        "cache_fork_count": mode_counts["cache_fork"],
        "independent_summary_count": mode_counts["independent_summary"],
        "no_progress_count": len(no_progress_events),
        "max_consecutive_no_progress": max_consecutive_no_progress,
        "runaway_no_progress_detected": max_consecutive_no_progress >= loop_limit or len(no_progress_events) >= loop_limit * 2,
        "no_progress_events": no_progress_events,
        "turn_local_attempt_count": turn_local_attempts,
        "retained_runtime_facts_counts": runtime_facts_counts,
        "latest_retained_runtime_facts_count": runtime_facts_counts[-1] if runtime_facts_counts else None,
    }


def _ingest_telemetry_line(raw: str, records: list[Mapping[str, Any]], text_lines: list[str]) -> None:
    text = raw.strip()
    if not text:
        return
    if text.startswith("{") and text.endswith("}"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            text_lines.append(text)
            return
        if isinstance(obj, dict):
            records.append(obj)
            return
    text_lines.append(text)


def _looks_like_compaction_event(rec: Mapping[str, Any]) -> bool:
    schema = str(rec.get("schema") or "")
    if schema.startswith("conversation_compaction") or schema.startswith("conversation_compact_checkpoint"):
        return True
    event = str(rec.get("event") or "").lower()
    return "compact" in event


def _extract_runtime_facts_count(rec: Mapping[str, Any]) -> int | None:
    for key in ("retained_runtime_facts_count", "runtime_facts_turn_count", "runtime_facts_count"):
        if key in rec:
            val = _opt_nonneg_int(rec.get(key))
            if val is not None:
                return val
    for list_key in ("retained_runtime_facts", "runtime_facts_sources"):
        items = rec.get(list_key)
        if isinstance(items, (list, tuple)):
            return len(items)
    for ir_key in ("retained_ir_history", "ir_history"):
        ir_items = rec.get(ir_key)
        if isinstance(ir_items, (list, tuple)):
            return sum(1 for item in ir_items if _is_runtime_facts_item(item))
    return None


def _is_runtime_facts_item(item: Any) -> bool:
    if type(item).__name__ == "RuntimeFactsTurn":
        return True
    if isinstance(item, Mapping):
        kind = str(item.get("type") or item.get("kind") or item.get("role") or "").strip().lower()
        return kind in {"runtime_facts", "runtime_facts_turn"}
    return False


def _default_pid_alive(pid: int) -> bool | None:
    """Probe OS process liveness via signal 0 without killing or modifying state."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    except OSError:
        return None
    return True


def collect_bg_process_census(
    store_root: str | Path | None,
    *,
    pid_alive_fn: Callable[[int], bool | None] | None = None,
) -> dict[str, Any]:
    """Classify all managed bg-* process records into active, settled, and unknown states."""
    probe_pid = pid_alive_fn or _default_pid_alive
    if store_root is None:
        return {
            "schema": "live_lab_bg_census.v1",
            "store_root": "",
            "store_exists": False,
            "sampled_at": time.time(),
            "counts": {"active": 0, "settled": 0, "unknown": 0, "total": 0},
            "active": [],
            "settled": [],
            "unknown": [],
            "errors": [],
        }

    root_path = Path(store_root)
    if not root_path.exists():
        return {
            "schema": "live_lab_bg_census.v1",
            "store_root": str(root_path),
            "store_exists": False,
            "sampled_at": time.time(),
            "counts": {"active": 0, "settled": 0, "unknown": 0, "total": 0},
            "active": [],
            "settled": [],
            "unknown": [],
            "errors": [],
        }

    from agent_py_agent.agent.tooling.process_session_store import ProcessSessionStore

    store = ProcessSessionStore(root_path)
    records, read_errors = store.list_records()

    active: list[dict[str, Any]] = []
    settled: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []

    for err in read_errors:
        unknown.append({
            "session_id": _session_id_from_error(err),
            "state": "unknown",
            "status": "unreadable",
            "pid": 0,
            "host_pid": 0,
            "alive": None,
            "reason": str(err.get("message") or err.get("error") or "unreadable_process_record"),
            "path": str(err.get("path") or ""),
        })

    for record in records:
        classified = _classify_bg_record(record, probe_pid)
        state = classified["state"]
        if state == "active":
            active.append(classified)
        elif state == "settled":
            settled.append(classified)
        else:
            unknown.append(classified)

    return {
        "schema": "live_lab_bg_census.v1",
        "store_root": str(root_path),
        "store_exists": True,
        "sampled_at": time.time(),
        "counts": {
            "active": len(active),
            "settled": len(settled),
            "unknown": len(unknown),
            "total": len(active) + len(settled) + len(unknown),
        },
        "active": active,
        "settled": settled,
        "unknown": unknown,
        "errors": list(read_errors),
    }


def _session_id_from_error(err: Mapping[str, Any]) -> str:
    sid = str(err.get("session_id") or "").strip()
    if sid:
        return sid
    raw_path = str(err.get("path") or "").strip()
    return Path(raw_path).stem if raw_path else ""


def _classify_bg_record(
    record: Mapping[str, Any],
    probe_pid: Callable[[int], bool | None],
) -> dict[str, Any]:
    scope = record.get("execution_scope") if isinstance(record.get("execution_scope"), Mapping) else {}
    session_id = str(record.get("session_id") or "")
    status = str(record.get("status") or "unknown")
    child_pid = int(record.get("child_pid") or 0)
    host_pid = int(record.get("pid") or 0)
    launcher_pid = int(record.get("launcher_pid") or 0)
    target_pid = child_pid if child_pid > 0 else host_pid

    alive: bool | None = probe_pid(target_pid) if target_pid > 0 else False
    reason = ""
    if status in _TERMINAL_PROCESS_STATUSES:
        if target_pid <= 0:
            state = "settled"
            reason = f"terminal_status:{status}"
        elif alive is False:
            state = "settled"
            reason = f"terminal_status:{status}_pid_dead"
        elif alive is True:
            state = "unknown"
            reason = f"terminal_status:{status}_pid_still_alive"
        else:
            state = "unknown"
            reason = f"terminal_status:{status}_pid_unverified"
    elif status in {"starting", "running"}:
        liveness_pid = target_pid if target_pid > 0 else launcher_pid
        liveness = alive if target_pid > 0 else (probe_pid(liveness_pid) if liveness_pid > 0 else False)
        alive = liveness
        if liveness_pid <= 0:
            state = "unknown"
            reason = f"nonterminal_status:{status}_missing_pid"
        elif liveness is True:
            state = "active"
            reason = f"nonterminal_status:{status}_pid_alive"
        elif liveness is False:
            state = "unknown"
            reason = f"nonterminal_status:{status}_pid_missing_in_os"
        else:
            state = "unknown"
            reason = f"nonterminal_status:{status}_pid_unverified"
    else:
        state = "unknown"
        reason = f"status:{status}"

    return {
        "session_id": session_id,
        "state": state,
        "status": status,
        "pid": target_pid,
        "host_pid": host_pid,
        "alive": alive,
        "reason": reason,
        "thread_id": str(scope.get("thread_id") or ""),
        "root_task_id": str(scope.get("root_task_id") or ""),
        "run_id": str(scope.get("run_id") or ""),
    }


def diff_bg_process_census(pre: Mapping[str, Any], post: Mapping[str, Any]) -> dict[str, Any]:
    """Compare pre/post bg-* censuses to identify new active/unknown leaks."""
    pre_active_keys = {(item.get("session_id"), item.get("pid")) for item in (pre.get("active") or ())}
    pre_unknown_keys = {(item.get("session_id"), item.get("reason")) for item in (pre.get("unknown") or ())}
    pre_settled_ids = {item.get("session_id") for item in (pre.get("settled") or ())}

    new_active = [
        item for item in (post.get("active") or ())
        if (item.get("session_id"), item.get("pid")) not in pre_active_keys
    ]
    new_unknown = [
        item for item in (post.get("unknown") or ())
        if (item.get("session_id"), item.get("reason")) not in pre_unknown_keys
    ]
    newly_settled = [
        item for item in (post.get("settled") or ())
        if item.get("session_id") not in pre_settled_ids
    ]
    leaked_sessions = [*new_active, *new_unknown]

    return {
        "schema": "live_lab_bg_census_diff.v1",
        "pre_counts": dict(pre.get("counts") or {}),
        "post_counts": dict(post.get("counts") or {}),
        "new_active_count": len(new_active),
        "new_unknown_count": len(new_unknown),
        "newly_settled_count": len(newly_settled),
        "leaked_count": len(leaked_sessions),
        "new_active": new_active,
        "new_unknown": new_unknown,
        "newly_settled": newly_settled,
        "leaked_sessions": leaked_sessions,
    }


def collect_compaction_observations(
    *,
    config_path: str | Path,
    turn_usages: Sequence[Mapping[str, Any]] = (),
    telemetry_sources: Sequence[Mapping[str, Any] | str | Path] = (),
    baseline: Mapping[str, Any] | None = None,
    thread_id: str | None = None,
    pre_bg_census: Mapping[str, Any] | None = None,
    pid_alive_fn: Callable[[int], bool | None] | None = None,
) -> dict[str, Any]:
    """Assemble full compaction, cache, policy, and 3-state bg-* observation report."""
    home_ctx = resolve_live_lab_home_context(config_path)
    checkpoints = load_canonical_compaction_checkpoints(
        compact_dir=home_ctx["owner_compact_dir"],
        threads_dir=home_ctx["threads_dir"],
        thread_id=thread_id,
        baseline=baseline,
    )
    telemetry = parse_compaction_telemetry(telemetry_sources)
    cache_phases = compute_pre_post_compaction_cache_ratios(turn_usages)
    post_bg = collect_bg_process_census(home_ctx["process_store_root"], pid_alive_fn=pid_alive_fn)
    bg_diff = diff_bg_process_census(pre_bg_census or {"counts": {"active": 0, "settled": 0, "unknown": 0, "total": 0}}, post_bg)

    return {
        "schema": "live_lab_compact_observations.v1",
        "home_context": home_ctx,
        "compact_policy": home_ctx["compact_policy"],
        "checkpoints": checkpoints,
        "telemetry": telemetry,
        "turn_usage": list(turn_usages),
        "cache_phases": cache_phases,
        "bg_census_pre": pre_bg_census,
        "bg_census_post": post_bg,
        "bg_census_diff": bg_diff,
    }


__all__ = [
    "LiveLabIsolationError",
    "UsageFileCursor",
    "aggregate_usage_records",
    "assert_isolated_home_path",
    "collect_bg_process_census",
    "collect_compaction_observations",
    "compute_pre_post_compaction_cache_ratios",
    "default_host_agent_homes",
    "diff_bg_process_census",
    "discover_usage_ledger_files",
    "is_host_agent_home_path",
    "load_canonical_compaction_checkpoints",
    "normalize_usage_record",
    "parse_compaction_telemetry",
    "prepare_isolated_subprocess_env",
    "read_usage_delta_since",
    "real_user_home_dir",
    "resolve_live_lab_home_context",
    "slice_usage_jsonl_turns",
    "snapshot_compaction_baseline",
    "snapshot_usage_cursors",
]
