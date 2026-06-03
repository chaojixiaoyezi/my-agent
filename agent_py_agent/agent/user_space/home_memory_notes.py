
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class HomeMemoryWriteResult:
    changed: bool
    hot_path: str = ""
    lesson_path: str = ""
    route_index_path: str = ""


@dataclass(frozen=True)
class LessonNoteRequest:
    lesson_id: str
    topic: str
    content: str
    trigger_keywords: tuple[str, ...] = ()


def append_hot_note(home_paths: object, text: str, *, created_at: str | None = None) -> HomeMemoryWriteResult:
    """Append one short HOT reminder if it is not already present."""

    note = str(text or "").strip()
    path = _owner_scoped_path(home_paths, "owner_memory_hot_md", "memory_hot_md")
    if not note or not path:
        return HomeMemoryWriteResult(changed=False, hot_path=str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_text(path)
    if note in existing:
        return HomeMemoryWriteResult(changed=False, hot_path=str(path))
    stamp = _date_part(created_at)
    addition = "\n\n## Added Notes\n" if "## Added Notes" not in existing else "\n"
    updated = (existing.rstrip() or "# Memory HOT") + addition + f"- {stamp}: {note}\n"
    path.write_text(updated, encoding="utf-8")
    return HomeMemoryWriteResult(changed=True, hot_path=str(path))


def upsert_lesson_note(
    home_paths: object,
    request: LessonNoteRequest,
) -> HomeMemoryWriteResult:
    """Write or replace one owner lesson and ensure the route index points at it."""

    slug = _slug(request.lesson_id)
    if not slug:
        return HomeMemoryWriteResult(changed=False)
    lesson_dir = _owner_scoped_path(home_paths, "owner_memory_lessons_dir", "memory_lessons_dir")
    index_path = _owner_scoped_path(home_paths, "owner_memory_routing_index_md", "memory_routing_index_md")
    lesson_path = lesson_dir / f"{slug}.md"
    lesson_dir.mkdir(parents=True, exist_ok=True)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    lesson_body = _lesson_body(topic=request.topic, content=request.content)
    old_lesson = _read_text(lesson_path)
    lesson_changed = old_lesson != lesson_body
    if lesson_changed:
        lesson_path.write_text(lesson_body, encoding="utf-8")
    route_changed = _ensure_lesson_route(index_path, slug, request.topic, request.trigger_keywords)
    return HomeMemoryWriteResult(
        changed=lesson_changed or route_changed,
        lesson_path=str(lesson_path),
        route_index_path=str(index_path),
    )


def _ensure_lesson_route(index_path: Path, slug: str, topic: str, trigger_keywords: Iterable[str]) -> bool:
    text = _read_text(index_path)
    route_id = f"lessons.{slug}"
    if f"## {route_id}" in text:
        return False
    keywords = ", ".join(str(item).strip() for item in trigger_keywords if str(item).strip())
    if not keywords:
        keywords = slug.replace("-", " ")
    section = (
        f"\n\n## {route_id}\n"
        f"topic: {topic or slug}\n"
        f"trigger_keywords: {keywords}\n"
        f"aliases: {slug}\n"
        f"when_to_read: Read when the current task mentions this lesson topic.\n"
        f"authority_path: memory/lessons/{slug}.md\n"
        f"inject_mode: summary\n"
        f"scope: owner\n"
        f"priority: 60\n"
        f"stale_check: review when related behavior changes\n"
    )
    (text.rstrip() + section).lstrip().rstrip()
    index_path.write_text((text.rstrip() + section).lstrip().rstrip() + "\n", encoding="utf-8")
    return True


def _owner_scoped_path(home_paths: object, owner_attr: str, legacy_attr: str) -> Path:
    if _is_local_main_owner(home_paths):
        return Path(getattr(home_paths, legacy_attr, "") or getattr(home_paths, owner_attr, ""))
    return Path(getattr(home_paths, owner_attr, "") or getattr(home_paths, legacy_attr, ""))


def _is_local_main_owner(home_paths: object) -> bool:
    return (
        str(getattr(home_paths, "owner_provider", "") or "local") == "local"
        and str(getattr(home_paths, "owner_kind", "") or "main") == "main"
        and str(getattr(home_paths, "owner_id", "") or "local/main") == "local/main"
    )


def _lesson_body(*, topic: str, content: str) -> str:
    title = str(topic or "Lesson").strip() or "Lesson"
    body = str(content or "").strip()
    return f"# {title}\n\n{body}\n"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _date_part(created_at: str | None) -> str:
    raw = str(created_at or "").strip()
    if raw:
        return raw[:10]
    return datetime.now(timezone.utc).date().isoformat()


def _slug(value: str) -> str:
    text = str(value or "").strip().lower()
    chars = [ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in text]
    return "-".join("".join(chars).split("-")).strip("-_")


__all__ = ["HomeMemoryWriteResult", "LessonNoteRequest", "append_hot_note", "upsert_lesson_note"]
