# LLM: USER.md 周期性做梦整合只允许处理 target="user"；AGENTS.md 与 SOUL.md 一律拒绝。
# 模块用途: 当用户空闲且阈值越限时，按时间顺序整理、去重并迁移临时/情节性条目到 long_term，经 CAS 原子提交并失效线程 user 前缀。
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..backends.decision_protocol import decision_input_tokens
from ..backends.request_scope import foreground_model_active
from ..capability.persona_repository import (
    PersonaConflictError,
    PersonaRepository,
    PersonaSecurityError,
    _is_empty_persona_template_content,
    _persona_birth_commit,
    persona_entry_id,
    versions_ledger_tail,
)
from ..common.json_io import (
    append_private_jsonl_records,
    locked_json_path,
    write_private_text_file_atomic,
    write_private_text_file_atomic_unlocked,
)
from ..common.nofollow_fs import ensure_private_dir
from ..prompting_parts.thread_persona import expire_thread_user_persona_prefixes
from .security import scan_memory_content

_TAG_PREFIX_RE = re.compile(r"\[(?:personal|user|global)(?::[^\]\n]+)?\]\s*", re.IGNORECASE)
_KV_RE = re.compile(r"^([^:\n：。！？；]{1,32})\s*[:：]\s*(.+)$")
_SIMPLE_IS_RE = re.compile(
    r"^(language|timezone|theme|editor|format|style|preferred\s+language|回答语言|语言|时区|主题|编辑器|回复风格)\s+(?:is|=|为)\s+(.+)$",
    re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(r"\b20\d{2}[-/]\d{1,2}(?:[-/]\d{1,2})?\b")
_CN_DATE_RE = re.compile(r"20\d{2}\s*年\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?")
_WORK_REF_RE = re.compile(r"\b(?:pr|issue|ticket|bug|hotfix|commit|incident)\s*#?\d+\b", re.IGNORECASE)
_TMP_PATH_RE = re.compile(r"(?:^|\s)/(?:tmp|var|private/tmp)/")

_EPISODIC_MARKERS = (
    "临时",
    "本次",
    "今天",
    "昨天",
    "前天",
    "上周",
    "这周",
    "本周",
    "暂定",
    "调试",
    "报错",
    "异常",
    "修复了",
    "已修复",
    "上线",
    "发布日志",
    "会议记录",
    "工单",
    "部署",
    "进度",
    "today",
    "yesterday",
    "last week",
    "this week",
    "temporary",
    "temporarily",
    "for now",
    "debugging",
    "stack trace",
    "traceback",
    "incident",
    "meeting note",
    "worklog",
    "migrated",
    "deployed",
    "exit code",
    "sprint",
)

_CORE_MARKERS = (
    "偏好",
    "喜欢",
    "习惯",
    "常用",
    "默认",
    "姓名",
    "称呼",
    "名字",
    "时区",
    "语言",
    "职业",
    "角色",
    "身份",
    "风格",
    "简洁",
    "详细",
    "中文",
    "英文",
    "prefer",
    "prefers",
    "likes",
    "dislike",
    "timezone",
    "language",
    "role",
    "style",
    "concise",
    "detailed",
    "always ",
    "never ",
    "name is",
    "call me",
)

_PREFERENCE_KEY_ALIASES = {
    "language": "language",
    "preferred language": "language",
    "回答语言": "language",
    "语言": "language",
    "语言偏好": "language",
    "timezone": "timezone",
    "时区": "timezone",
    "theme": "theme",
    "主题": "theme",
    "editor": "editor",
    "编辑器": "editor",
    "style": "style",
    "回复风格": "style",
    "输出风格": "style",
    "format": "format",
    "格式": "format",
}


@dataclass(frozen=True)
class ParsedUserProfileEntry:
    """Single parsed entry from USER.md with ledger-derived chronology."""

    entry_id: str
    raw_body: str
    clean_text: str
    file_index: int
    version: int | None
    created_at: float | None
    chronology_rank: int = 0


@dataclass(frozen=True)
class UserProfileConsolidationPlan:
    """Pure in-memory consolidation plan guaranteeing 100% entry accounting."""

    heading: str
    input_entry_ids: tuple[str, ...]
    kept_entries: tuple[ParsedUserProfileEntry, ...]
    relocated_entries: tuple[ParsedUserProfileEntry, ...]
    superseded_ids: tuple[str, ...]
    kept_source_ids: tuple[str, ...]
    relocated_ids: tuple[str, ...]
    consolidated_content: str
    entry_count_before: int
    entry_count_after: int
    tokens_before: int
    tokens_after: int
    # 保留条目里同一非别名键出现不同取值的分组：(键, 条目 ID...)；只供计划报告审阅，不参与取舍。
    same_key_conflicts: tuple[tuple[str, tuple[str, ...]], ...] = ()


def _require_user_dream_target(target: object) -> str:
    normalized = str(target if target is not None else "user").strip().lower()
    if normalized != "user":
        raise PersonaSecurityError(
            f"PERSONA_DREAM_TARGET_FORBIDDEN: Periodic dreaming only supports target='user' (USER.md); {normalized!r} is forbidden."
        )
    return "user"


def _ensure_no_symlinks(paths: Sequence[Path]) -> None:
    for path in paths:
        if path.is_symlink():
            raise PersonaSecurityError(f"PERSONA_DREAM_SYMLINK: symlink path is forbidden ({path.name})")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _strip_machine_tags(text: str) -> str:
    cleaned = _TAG_PREFIX_RE.sub("", str(text or "")).strip()
    cleaned = _normalize_whitespace(cleaned)
    cleaned = cleaned.lstrip(":：").strip()
    return cleaned


def _normalized_dedupe_key(clean_text: str) -> str:
    return _normalize_whitespace(clean_text)


def _preference_key(clean_text: str) -> str | None:
    if re.match(r"\[[A-Za-z_]+:[^\]\n]*\]", clean_text):
        return None
    match = _KV_RE.match(clean_text) or _SIMPLE_IS_RE.match(clean_text)
    if match:
        raw_key = _normalize_whitespace(match.group(1)).lower()
        value = _normalize_whitespace(match.group(2))
        if raw_key and value and len(raw_key) <= 32:
            if raw_key in _PREFERENCE_KEY_ALIASES:
                return f"alias:{_PREFERENCE_KEY_ALIASES[raw_key]}"
            return f"kv:{raw_key}"
    return None


# LLM: 别名键（语言、时区等）已按新者为准合并；非别名键同键多值可能都成立（如多个常用工具），做梦不替用户取舍，
#   只把保留下来的同键不同值条目分组列进计划报告。
# 函数用途: 返回保留条目中同一非别名键下取值不同的条目分组。
def _same_key_conflicts(entries: Sequence[ParsedUserProfileEntry]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    groups: dict[str, list[tuple[str, str]]] = {}
    for entry in entries:
        pref_key = _preference_key(entry.clean_text)
        match = _KV_RE.match(entry.clean_text)
        if pref_key is None or not pref_key.startswith("kv:") or match is None:
            continue
        groups.setdefault(pref_key[3:], []).append((entry.entry_id, _normalize_whitespace(match.group(2))))
    return tuple(
        (key, tuple(entry_id for entry_id, _value in rows))
        for key, rows in groups.items()
        if len({value for _entry_id, value in rows}) > 1
    )


def _classify_user_entry(clean_text: str, *, verbose_char_limit: int = 280) -> int:
    """Return tier: 0=episodic/relocate, 1=neutral fact, 2=core profile/preference."""
    if len(clean_text) > verbose_char_limit:
        return 0
    lowered = clean_text.lower()
    if (
        _ISO_DATE_RE.search(clean_text)
        or _CN_DATE_RE.search(clean_text)
        or _WORK_REF_RE.search(clean_text)
        or _TMP_PATH_RE.search(clean_text)
        or any(marker in lowered for marker in _EPISODIC_MARKERS)
    ):
        return 0
    if _preference_key(clean_text) is not None or any(marker in lowered for marker in _CORE_MARKERS):
        return 2
    return 1


def _history_index_by_entry(
    persona_history: Sequence[Mapping[str, object]],
) -> dict[str, tuple[int, float]]:
    """Map entry_id -> latest (version, created_at) recorded for USER.md."""
    index: dict[str, tuple[int, float]] = {}
    for row in persona_history:
        if str(row.get("target") or "user").strip().lower() != "user":
            continue
        action = str(row.get("action") or "").strip().lower()
        if action not in {"add", "replace", "batch"}:
            continue
        raw_ver = row.get("version")
        try:
            version = int(raw_ver) if raw_ver is not None and not isinstance(raw_ver, bool) else 0
        except (TypeError, ValueError):
            version = 0
        raw_ts = row.get("created_at")
        try:
            created_at = float(raw_ts) if raw_ts is not None and not isinstance(raw_ts, bool) else 0.0
        except (TypeError, ValueError):
            created_at = 0.0
        if not math.isfinite(created_at):
            created_at = 0.0
        eid = str(row.get("entry_id") or "").strip()
        if eid:
            prev = index.get(eid)
            if prev is None or (version, created_at) >= prev:
                index[eid] = (version, created_at)
        ops = row.get("operations")
        if isinstance(ops, list):
            for op in ops:
                if not isinstance(op, Mapping):
                    continue
                op_eid = str(op.get("entry_id") or "").strip()
                if op_eid:
                    prev = index.get(op_eid)
                    if prev is None or (version, created_at) >= prev:
                        index[op_eid] = (version, created_at)
    return index


def _extract_top_heading(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and len(stripped) > 2:
            return stripped
    return "# USER.md"


def parse_user_profile_entries(
    content: str,
    persona_history: Sequence[Mapping[str, object]] = (),
) -> list[ParsedUserProfileEntry]:
    """Parse USER.md entries and enrich each entry with ledger chronology."""
    history_map = _history_index_by_entry(persona_history)
    entries: list[ParsedUserProfileEntry] = []
    for line_idx, raw_line in enumerate(str(content or "").splitlines()):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or stripped == "---" or stripped.startswith("<!--"):
            continue
        if stripped.startswith(("- ", "* ", "+ ")):
            raw_body = stripped[2:].strip()
        else:
            numbered = re.match(r"^\d+\.\s+(.+)$", stripped)
            raw_body = numbered.group(1).strip() if numbered else stripped
        if not raw_body or _is_empty_persona_template_content(raw_body):
            continue
        entry_id = persona_entry_id("user", raw_body)
        clean_text = _strip_machine_tags(raw_body) or _normalize_whitespace(raw_body)
        hist = history_map.get(entry_id)
        entries.append(
            ParsedUserProfileEntry(
                entry_id=entry_id,
                raw_body=raw_body,
                clean_text=clean_text,
                file_index=line_idx,
                version=hist[0] if hist is not None else None,
                created_at=hist[1] if hist is not None else None,
            )
        )
    ordered = sorted(
        entries,
        key=lambda item: (
            0 if item.version is None else 1,
            item.version or 0,
            item.created_at or 0.0,
            item.file_index,
        ),
    )
    return [
        ParsedUserProfileEntry(
            entry_id=item.entry_id,
            raw_body=item.raw_body,
            clean_text=item.clean_text,
            file_index=item.file_index,
            version=item.version,
            created_at=item.created_at,
            chronology_rank=rank,
        )
        for rank, item in enumerate(ordered)
    ]


def _render_user_profile_document(heading: str, kept_entries: Sequence[ParsedUserProfileEntry]) -> str:
    clean_heading = str(heading or "# USER.md").strip() or "# USER.md"
    if not kept_entries:
        return f"{clean_heading}\n"
    bullets = "\n".join(f"- {entry.clean_text}" for entry in kept_entries)
    return f"{clean_heading}\n\n{bullets}\n"


def plan_user_profile_consolidation(
    current_content: str,
    *,
    persona_history: Sequence[Mapping[str, object]] = (),
    entry_threshold: int = 70,
    token_threshold: int = 5000,
    target_fill_ratio: float = 0.7,
    verbose_char_limit: int = 280,
) -> UserProfileConsolidationPlan:
    """Plan deterministic consolidation with newer-wins resolution and full entry accounting."""
    chronological = parse_user_profile_entries(current_content, persona_history)
    input_entry_ids = tuple(dict.fromkeys(entry.entry_id for entry in chronological))
    heading = _extract_top_heading(current_content)
    tokens_before = decision_input_tokens(current_content)

    # Walk newest -> oldest so newer explicit preferences always win.
    winners_by_id: dict[str, ParsedUserProfileEntry] = {}
    seen_norm: set[str] = set()
    seen_pref_keys: set[str] = set()
    superseded_ids_set: set[str] = set()

    for entry in reversed(chronological):
        norm_key = _normalized_dedupe_key(entry.clean_text)
        pref_key = _preference_key(entry.clean_text)
        if entry.entry_id in winners_by_id:
            # Literal duplicate line with identical entry_id; keep newest instance.
            continue
        alias_pref_key = pref_key if (pref_key is not None and pref_key.startswith("alias:")) else None
        is_conflict = (norm_key in seen_norm) or (alias_pref_key is not None and alias_pref_key in seen_pref_keys)
        if is_conflict:
            superseded_ids_set.add(entry.entry_id)
            continue
        winners_by_id[entry.entry_id] = entry
        seen_norm.add(norm_key)
        if alias_pref_key is not None:
            seen_pref_keys.add(alias_pref_key)

    unique_winners = sorted(winners_by_id.values(), key=lambda item: item.chronology_rank)

    target_entry_cap: int | None = None
    if entry_threshold > 0:
        scaled_entries = int(entry_threshold * target_fill_ratio)
        target_entry_cap = max(1, min(entry_threshold, scaled_entries or 1))

    target_token_cap: int | None = None
    if token_threshold > 0:
        scaled_tokens = int(token_threshold * target_fill_ratio)
        target_token_cap = min(token_threshold, max(1, scaled_tokens))

    relocated: list[ParsedUserProfileEntry] = []
    eligible: list[tuple[int, ParsedUserProfileEntry]] = []
    for entry in unique_winners:
        tier = _classify_user_entry(entry.clean_text, verbose_char_limit=verbose_char_limit)
        if tier == 0:
            relocated.append(entry)
        else:
            eligible.append((tier, entry))

    # Highest tier first, newest first within equal tier.
    prioritized = sorted(
        eligible,
        key=lambda pair: (pair[0], pair[1].chronology_rank),
        reverse=True,
    )

    kept: list[ParsedUserProfileEntry] = []
    heading_overhead_tokens = decision_input_tokens(f"{heading}\n\n")
    running_tokens = heading_overhead_tokens
    for _tier, entry in prioritized:
        if target_entry_cap is not None and len(kept) >= target_entry_cap:
            relocated.append(entry)
            continue
        line_tokens = max(1, decision_input_tokens(f"- {entry.clean_text}\n"))
        if target_token_cap is not None and (running_tokens + line_tokens) > target_token_cap:
            relocated.append(entry)
            continue
        kept.append(entry)
        running_tokens += line_tokens

    kept_chronological = tuple(sorted(kept, key=lambda item: item.chronology_rank))
    relocated_chronological = tuple(sorted(relocated, key=lambda item: item.chronology_rank))
    kept_source_ids = tuple(entry.entry_id for entry in kept_chronological)
    relocated_ids = tuple(entry.entry_id for entry in relocated_chronological)
    superseded_ids = tuple(
        eid for eid in input_entry_ids
        if eid in superseded_ids_set and eid not in winners_by_id
    )

    # Assert 100% distinct entry accounting invariant.
    kept_set = set(kept_source_ids)
    relocated_set = set(relocated_ids)
    superseded_set = set(superseded_ids)
    if (kept_set & relocated_set) or (kept_set & superseded_set) or (relocated_set & superseded_set):
        raise AssertionError("user_profile_dream accounting partitions must be pairwise disjoint")
    if (kept_set | relocated_set | superseded_set) != set(input_entry_ids):
        raise AssertionError("user_profile_dream accounting must cover 100% of input entry_ids")

    consolidated_content = _render_user_profile_document(heading, kept_chronological)
    tokens_after = decision_input_tokens(consolidated_content)
    return UserProfileConsolidationPlan(
        heading=heading,
        input_entry_ids=input_entry_ids,
        kept_entries=kept_chronological,
        relocated_entries=relocated_chronological,
        superseded_ids=superseded_ids,
        kept_source_ids=kept_source_ids,
        relocated_ids=relocated_ids,
        consolidated_content=consolidated_content,
        entry_count_before=len(chronological),
        entry_count_after=len(kept_chronological),
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        same_key_conflicts=_same_key_conflicts(kept_chronological),
    )


def _dream_state_path(owner_home: Path) -> Path:
    return owner_home / "persona" / "user_dream_state.json"


def _dream_ledger_path(owner_home: Path) -> Path:
    return owner_home / "persona" / "user_dream_ledger.jsonl"


def _read_dream_state(path: Path) -> dict[str, object]:
    _ensure_no_symlinks((path,))
    if not path.exists():
        return {}
    if not path.is_file():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _latest_owner_activity_ts(
    conversation_store: object | None,
    *,
    explicit_activity_at: float | None = None,
) -> tuple[float | None, bool]:
    """Return (latest_activity_ts, corrupt_unknown) without unbounded directory scans."""
    if explicit_activity_at is not None:
        val = float(explicit_activity_at)
        if not math.isfinite(val):
            return None, True
        return val, False
    if conversation_store is None:
        return None, False
    threads_obj = getattr(conversation_store, "threads", conversation_store)
    list_fn = getattr(threads_obj, "list_threads", None) or getattr(threads_obj, "list", None)
    if not callable(list_fn):
        return None, False
    threads = list_fn(limit=20)
    latest: float | None = None
    saw_corrupt = False
    for thread in threads or ():
        metadata = getattr(thread, "metadata", None) or {}
        if not isinstance(metadata, Mapping):
            saw_corrupt = True
            continue
        raw_ts = metadata.get("last_user_activity_at", getattr(thread, "updated_at", None))
        if isinstance(raw_ts, bool) or not isinstance(raw_ts, (int, float)) or not math.isfinite(float(raw_ts)):
            saw_corrupt = True
            continue
        val = float(raw_ts)
        if latest is None or val > latest:
            latest = val
    if latest is None and saw_corrupt:
        return None, True
    return latest, False


def _resolve_now_timestamp(now: datetime | float | int | None) -> float:
    if isinstance(now, datetime):
        return now.timestamp()
    if isinstance(now, (int, float)) and not isinstance(now, bool):
        return float(now)
    return datetime.now(timezone.utc).timestamp()


def _config_value(
    explicit: object | None,
    sources: Sequence[object | None],
    attr: str,
    default: object,
) -> object:
    if explicit is not None:
        return explicit
    for src in sources:
        if src is not None and hasattr(src, attr):
            val = getattr(src, attr)
            if val is not None:
                return val
    return default


def _resolve_cooldown_seconds(
    explicit: float | None,
    sources: Sequence[object | None],
) -> float | int:
    if explicit is not None:
        return explicit
    from .curator_models import MemoryCuratorConfig

    for src in sources:
        if src is None:
            continue
        if isinstance(src, MemoryCuratorConfig):
            return src.interval_seconds
        val = getattr(src, "interval_seconds", None)
        if val is not None:
            return val
        return MemoryCuratorConfig.from_agent_config(src).interval_seconds
    return 10_800


def _scan_consolidated_content(content: str) -> None:
    full_scan = scan_memory_content(content)
    if not full_scan.safe:
        raise PersonaSecurityError(f"PERSONA_DREAM_SECURITY_BLOCKED: {full_scan.reason()}")
    for raw_line in content.splitlines():
        if raw_line.strip():
            line_scan = scan_memory_content(raw_line)
            if not line_scan.safe:
                raise PersonaSecurityError(f"PERSONA_DREAM_SECURITY_BLOCKED: {line_scan.reason()}")


def _skip_result(
    reason: str,
    *,
    entry_threshold: int,
    token_threshold: int,
    entry_count_before: int = 0,
    tokens_before: int = 0,
) -> dict[str, object]:
    return {
        "status": "skipped",
        "reason": reason,
        "target": "user",
        "entry_threshold": entry_threshold,
        "token_threshold": token_threshold,
        "entry_count_before": entry_count_before,
        "tokens_before": tokens_before,
    }


def _resolve_config_bundle(
    config: object | None,
    config_provider: Callable[[], object] | None,
    *,
    entry_threshold: int | None,
    token_threshold: int | None,
    idle_seconds: float | None,
    cooldown_seconds: float | None,
) -> tuple[bool, str, int, int, int, int]:
    """Read live thresholds & idle/learning controls cleanly without hardcoded cutoffs."""
    live_obj: object | None = config
    if callable(config_provider):
        try:
            provided = config_provider()
        except Exception:
            provided = None
        if provided is not None:
            live_obj = provided

    sources: tuple[object | None, ...] = (live_obj, config)
    enable_self_learning = bool(_config_value(None, sources, "enable_self_learning", True))
    maintenance_mode = (
        str(_config_value(None, sources, "self_learning_maintenance_mode", "autonomous") or "autonomous")
        .strip()
        .lower()
    )

    raw_entry_thr = _config_value(
        entry_threshold,
        sources,
        "user_profile_dream_entry_threshold",
        70,
    )
    raw_token_thr = _config_value(
        token_threshold,
        sources,
        "user_profile_dream_token_threshold",
        5000,
    )
    raw_idle = _config_value(
        idle_seconds,
        sources,
        "memory_compact_idle_seconds",
        0,
    )
    raw_cooldown = _resolve_cooldown_seconds(cooldown_seconds, sources)

    eff_entry_thr = max(0, int(raw_entry_thr))
    eff_token_thr = max(0, int(raw_token_thr))
    eff_idle_sec = max(0, int(float(raw_idle)))
    eff_cooldown_sec = max(0, int(float(raw_cooldown)))
    return (
        enable_self_learning,
        maintenance_mode,
        eff_entry_thr,
        eff_token_thr,
        eff_idle_sec,
        eff_cooldown_sec,
    )


def run_user_profile_dream_if_due(
    *,
    owner_home: Path,
    persona_repository: PersonaRepository | None = None,
    long_term_store: object | None = None,
    conversation_store: object | None = None,
    backend: object | None = None,
    config: object | None = None,
    config_provider: Callable[[], object] | None = None,
    target: str = "user",
    now: datetime | float | int | None = None,
    last_user_activity_at: float | None = None,
    entry_threshold: int | None = None,
    token_threshold: int | None = None,
    idle_seconds: float | None = None,
    cooldown_seconds: float | None = None,
) -> dict[str, object]:
    """Consolidate USER.md during owner idle when thresholds exceed limits.

    Strictly restricted to `target="user"`; never modifies AGENTS.md or SOUL.md.
    Fail-closed order:
      1. Enforce target/symlink/policy/foreground/idle/cooldown/watermark/threshold gates.
      2. Plan consolidation + validate 100% entry accounting + run threat scan in memory.
      3. Relocate episodic/overflow entries to `long_term.apply_batch` BEFORE touching USER.md.
      4. Save pre-dream backup + commit USER.md via PersonaRepository CAS.
      5. Append provenance ledger + update durable watermark + expire thread `user` prefix only.
    """
    _require_user_dream_target(target)
    home = Path(owner_home)
    _ensure_no_symlinks(
        (
            home,
            home / "persona",
            home / "USER.md",
            _dream_state_path(home),
            _dream_ledger_path(home),
            home / "persona" / "backups",
            home / "persona" / "backups" / "user",
        )
    )

    (
        enable_self_learning,
        maintenance_mode,
        eff_entry_thr,
        eff_token_thr,
        eff_idle_sec,
        eff_cooldown_sec,
    ) = _resolve_config_bundle(
        config,
        config_provider,
        entry_threshold=entry_threshold,
        token_threshold=token_threshold,
        idle_seconds=idle_seconds,
        cooldown_seconds=cooldown_seconds,
    )

    if not enable_self_learning:
        return _skip_result(
            "self_learning_disabled",
            entry_threshold=eff_entry_thr,
            token_threshold=eff_token_thr,
        )
    if maintenance_mode != "autonomous":
        return _skip_result(
            "maintenance_mode_not_autonomous",
            entry_threshold=eff_entry_thr,
            token_threshold=eff_token_thr,
        )
    if eff_entry_thr <= 0 and eff_token_thr <= 0:
        return _skip_result(
            "thresholds_disabled",
            entry_threshold=eff_entry_thr,
            token_threshold=eff_token_thr,
        )
    if backend is not None and foreground_model_active(backend):
        return _skip_result(
            "foreground_model_active",
            entry_threshold=eff_entry_thr,
            token_threshold=eff_token_thr,
        )

    now_ts = _resolve_now_timestamp(now)
    if eff_idle_sec > 0:
        latest_activity, activity_unknown = _latest_owner_activity_ts(
            conversation_store,
            explicit_activity_at=last_user_activity_at,
        )
        if activity_unknown:
            return _skip_result(
                "activity_unknown",
                entry_threshold=eff_entry_thr,
                token_threshold=eff_token_thr,
            )
        if latest_activity is not None and (now_ts - latest_activity) < eff_idle_sec:
            return _skip_result(
                "owner_not_idle",
                entry_threshold=eff_entry_thr,
                token_threshold=eff_token_thr,
            )

    repo = persona_repository or PersonaRepository(
        owner_home=home,
        soul_path=home / "SOUL.md",
        user_path=home / "USER.md",
        agents_path=home / "AGENTS.md",
    )
    snapshot = repo.load("user")
    if snapshot.diagnostic.state in {"security", "io", "blocked"} or snapshot.diagnostic.blocked_lines > 0:
        raise PersonaSecurityError(snapshot.diagnostic.detail or snapshot.diagnostic.state)
    if snapshot.diagnostic.state == "missing":
        return _skip_result(
            "user_profile_empty",
            entry_threshold=eff_entry_thr,
            token_threshold=eff_token_thr,
        )
    user_path = repo.path_for("user")
    read_lock_opts = {} if not repo.coordinator_io else {"birth_commit": _persona_birth_commit}
    with locked_json_path(user_path, **read_lock_opts):
        if user_path.is_symlink() or (user_path.exists() and not user_path.is_file()):
            raise PersonaSecurityError("persona target must be a regular file")
        current_content = user_path.read_text(encoding="utf-8") if user_path.exists() else ""
    if not current_content.strip():
        return _skip_result(
            "user_profile_empty",
            entry_threshold=eff_entry_thr,
            token_threshold=eff_token_thr,
        )

    current_sha256 = _sha256_text(current_content)
    state_path = _dream_state_path(home)
    state = _read_dream_state(state_path)
    last_sha = str(state.get("last_consolidated_sha256") or "")
    if last_sha and last_sha == current_sha256:
        return _skip_result(
            "watermark_unchanged",
            entry_threshold=eff_entry_thr,
            token_threshold=eff_token_thr,
        )
    raw_last_at = state.get("last_consolidated_at")
    if (
        eff_cooldown_sec > 0
        and isinstance(raw_last_at, (int, float))
        and not isinstance(raw_last_at, bool)
        and math.isfinite(float(raw_last_at))
        and 0.0 <= (now_ts - float(raw_last_at)) < eff_cooldown_sec
    ):
        return _skip_result(
            "cooldown_active",
            entry_threshold=eff_entry_thr,
            token_threshold=eff_token_thr,
        )

    history_rows = repo.history("user")
    parsed_entries = parse_user_profile_entries(current_content, history_rows)
    entry_count = len(parsed_entries)
    token_count = decision_input_tokens(current_content)

    entry_triggered = eff_entry_thr > 0 and entry_count > eff_entry_thr
    token_triggered = eff_token_thr > 0 and token_count > eff_token_thr
    if not (entry_triggered or token_triggered):
        return _skip_result(
            "below_thresholds",
            entry_threshold=eff_entry_thr,
            token_threshold=eff_token_thr,
            entry_count_before=entry_count,
            tokens_before=token_count,
        )

    plan = plan_user_profile_consolidation(
        current_content,
        persona_history=history_rows,
        entry_threshold=eff_entry_thr,
        token_threshold=eff_token_thr,
    )
    _scan_consolidated_content(plan.consolidated_content)

    if plan.consolidated_content == current_content and not plan.relocated_entries:
        return _skip_result(
            "no_changes_needed",
            entry_threshold=eff_entry_thr,
            token_threshold=eff_token_thr,
            entry_count_before=entry_count,
            tokens_before=token_count,
        )

    dt_utc = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    ts_tag = dt_utc.strftime("%Y%m%dT%H%M%SZ")
    dream_run_id = f"dream-{ts_tag}-{uuid.uuid4().hex[:8]}"
    versions_tail = versions_ledger_tail(
        repo.versions_path,
        **({"coordinator_io": True} if repo.coordinator_io else {}),
    )

    # Fail-closed Stage A: persist relocated items into long_term BEFORE touching USER.md or backup/ledger.
    relocated_memory_ids: list[str] = []
    if plan.relocated_entries:
        apply_fn = getattr(long_term_store, "apply_batch", None)
        if not callable(apply_fn):
            raise RuntimeError(
                "PERSONA_DREAM_LONG_TERM_UNAVAILABLE: long_term_store.apply_batch is required for fail-closed relocation"
            )
        operations = [
            {
                "action": "add",
                "role": "user",
                "kind": "fact",
                "content": entry.clean_text,
                "tags": ["user-profile-dream"],
                "source": "user-profile-dream",
                "attributes": {
                    # 迁移沿用结构化身份，精确区分大小写并保持重试幂等。
                    "subject_key": (
                        "user-profile-dream:"
                        f"{persona_entry_id('user', entry.clean_text)}"
                    ),
                    "relocated_from": f"USER.md#{entry.entry_id}",
                    "dream_run_id": dream_run_id,
                },
            }
            for entry in plan.relocated_entries
        ]
        records = apply_fn(operations)
        if not isinstance(records, Sequence):
            raise RuntimeError("PERSONA_DREAM_RELOCATE_FAILED: long_term.apply_batch did not return committed records")
        relocated_memory_ids = [
            str(getattr(rec, "entry_id", "") or "") for rec in records if getattr(rec, "entry_id", "")
        ]
        if len(relocated_memory_ids) < len(operations):
            # apply_batch returns [] (or fewer records) when identical facts are already durably committed
            # (e.g. retry after a prior CAS conflict); verify durability before continuing or fail closed.
            read_all_fn = getattr(long_term_store, "all", None)
            if not callable(read_all_fn):
                if len(relocated_memory_ids) == 0:
                    raise RuntimeError(
                        "PERSONA_DREAM_RELOCATE_FAILED: long_term.apply_batch returned no committed records"
                    )
            else:
                existing_records = list(read_all_fn())
                by_content: dict[str, str] = {}
                for rec in existing_records:
                    key = str(getattr(rec, "content", "") or "").strip()
                    eid = str(getattr(rec, "entry_id", "") or "").strip()
                    if key and eid and key not in by_content:
                        by_content[key] = eid
                missing = [entry.clean_text for entry in plan.relocated_entries if entry.clean_text.strip() not in by_content]
                if missing:
                    raise RuntimeError(
                        "PERSONA_DREAM_RELOCATE_FAILED: relocated entries missing from long_term store"
                    )
                for entry in plan.relocated_entries:
                    eid = by_content.get(entry.clean_text.strip())
                    if eid and eid not in relocated_memory_ids:
                        relocated_memory_ids.append(eid)

    # Fail-closed Stage B: create pre-dream backup and commit USER.md atomically via CAS.
    pre_ver = repo._current_version("user", current_sha256)
    backup_dir = home / "persona" / "backups" / "user"
    ensure_private_dir(backup_dir)
    backup_path = backup_dir / f"{ts_tag}-v{pre_ver}-{uuid.uuid4().hex[:6]}.md"
    _ensure_no_symlinks((backup_path,))
    write_private_text_file_atomic(backup_path, current_content)
    pre_dream_backup_ref = str(backup_path.relative_to(home))

    try:
        commit_result = repo.consolidate_user_profile(
            target="user",
            consolidated_content=plan.consolidated_content,
            expected_sha256=current_sha256,
            expected_versions_tail=versions_tail,
            source="user_profile_dream",
            pre_dream_backup_ref=pre_dream_backup_ref,
            dream_run_id=dream_run_id,
        )
    except Exception as exc:
        from ..plugin_host.errors import HostAPIError

        if isinstance(exc, PersonaConflictError) or (
            isinstance(exc, HostAPIError) and getattr(exc, "outcome", None) == "not_started"
            and getattr(exc, "code", None) in ("AUTO_GOVERNANCE_SELF_LEARNING_DISABLED", "AUTO_GOVERNANCE_ADMIN_REVIEW_REQUIRED", "AUTO_GOVERNANCE_ADMIN_ONLY")
        ):
            try:
                if backup_path.exists() and not backup_path.is_symlink():
                    backup_path.unlink()
            except OSError:
                pass
        raise

    if not commit_result.get("changed"):
        try:
            if backup_path.exists() and not backup_path.is_symlink():
                backup_path.unlink()
        except OSError:
            pass
        return _skip_result(
            "no_changes_needed",
            entry_threshold=eff_entry_thr,
            token_threshold=eff_token_thr,
            entry_count_before=entry_count,
            tokens_before=token_count,
        )

    consolidated_sha256 = str(commit_result.get("sha256") or _sha256_text(plan.consolidated_content))
    pre_dream_version = commit_result.get("pre_dream_version")
    post_dream_version = commit_result.get("version")

    # Stage C: durable watermark, provenance ledger, and selective thread user-prefix expiry.
    ensure_private_dir(home / "persona")
    ledger_record = {
        "dream_run_id": dream_run_id,
        "target": "user",
        "created_at": now_ts,
        "created_at_iso": dt_utc.isoformat(),
        "entry_threshold": eff_entry_thr,
        "token_threshold": eff_token_thr,
        "entry_count_before": plan.entry_count_before,
        "entry_count_after": plan.entry_count_after,
        "tokens_before": plan.tokens_before,
        "tokens_after": plan.tokens_after,
        "input_entry_ids": list(plan.input_entry_ids),
        "kept_source_ids": list(plan.kept_source_ids),
        "relocated_ids": list(plan.relocated_ids),
        "superseded_ids": list(plan.superseded_ids),
        "relocated_memory_ids": relocated_memory_ids,
        "previous_sha256": current_sha256,
        "consolidated_sha256": consolidated_sha256,
        "pre_dream_version": pre_dream_version,
        "post_dream_version": post_dream_version,
        "pre_dream_backup_ref": pre_dream_backup_ref,
    }
    append_private_jsonl_records(_dream_ledger_path(home), [ledger_record])

    next_state = {
        "schema_version": 1,
        "last_dream_run_id": dream_run_id,
        "last_consolidated_sha256": consolidated_sha256,
        "last_consolidated_at": now_ts,
        "pre_dream_version": pre_dream_version,
        "post_dream_version": post_dream_version,
        "pre_dream_backup_ref": pre_dream_backup_ref,
    }
    with locked_json_path(state_path):
        _ensure_no_symlinks((state_path,))
        write_private_text_file_atomic_unlocked(
            state_path,
            json.dumps(next_state, ensure_ascii=False, indent=2),
        )

    expired_thread_prefixes = expire_thread_user_persona_prefixes(home)

    return {
        "status": "consolidated",
        "reason": "threshold_exceeded",
        "target": "user",
        "dream_run_id": dream_run_id,
        "entry_threshold": eff_entry_thr,
        "token_threshold": eff_token_thr,
        "entry_count_before": plan.entry_count_before,
        "entry_count_after": plan.entry_count_after,
        "tokens_before": plan.tokens_before,
        "tokens_after": plan.tokens_after,
        "input_entry_ids": list(plan.input_entry_ids),
        "kept_source_ids": list(plan.kept_source_ids),
        "relocated_ids": list(plan.relocated_ids),
        "superseded_ids": list(plan.superseded_ids),
        "relocated_memory_ids": relocated_memory_ids,
        "previous_sha256": current_sha256,
        "consolidated_sha256": consolidated_sha256,
        "pre_dream_version": pre_dream_version,
        "post_dream_version": post_dream_version,
        "pre_dream_backup_ref": pre_dream_backup_ref,
        "expired_thread_prefixes": expired_thread_prefixes,
    }


def plan_report_dict(plan: UserProfileConsolidationPlan) -> dict[str, object]:
    """Serialize consolidation plan summary for audit/testing inspection."""
    return asdict(plan)
