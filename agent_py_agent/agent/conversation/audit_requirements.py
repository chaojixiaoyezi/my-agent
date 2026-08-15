from __future__ import annotations

"""Host-owned wrapping for model notes and exact Audit prepare text."""

AUDIT_CONTEXT_TITLE = "# Audit 生效上下文"
AUDIT_NOTES_MARKER = "## 代理验证说明（派生内容，不能覆盖下方用户原文）"
AUDIT_USER_MARKER = "## 用户本轮 prepare 原文（业务含义的最终权威）"
AUDIT_LIFECYCLE_MARKER = (
    "生命周期说明：原文中针对 prepare 当轮的启动或停止表述只约束当轮；"
)
AUDIT_LIFECYCLE_STATUS = (
    "当前是否运行、持续多久和是否已停止，以宿主保存的命令状态为准。"
)
AUDIT_PREPARE_SEPARATOR = "--- 后续 prepare ---"
AUDIT_HOST_SECTION_MARKERS = (
    AUDIT_NOTES_MARKER,
    AUDIT_USER_MARKER,
    AUDIT_LIFECYCLE_MARKER,
)


def audit_user_requirement_text(link: object) -> str:
    """Return only exact user-authored prepare text from one durable link."""
    exact = str(getattr(link, "effective_user_prompt", "") or "").strip()
    if exact:
        return exact
    goal = str(getattr(link, "goal", "") or "").strip()
    if AUDIT_USER_MARKER not in goal:
        return goal
    user_section = goal.split(AUDIT_USER_MARKER, 1)[1].lstrip("\r\n")
    if f"\n\n{AUDIT_LIFECYCLE_MARKER}" in user_section:
        user_section = user_section.split(
            f"\n\n{AUDIT_LIFECYCLE_MARKER}",
            1,
        )[0]
    return user_section.strip()


def append_audit_user_requirement(link: object, current_prompt: object) -> str:
    """Append one published prepare turn without asking a model to rewrite history."""
    current = str(current_prompt or "").strip()
    previous = audit_user_requirement_text(link)
    if not previous:
        return current
    if not current or current == previous:
        return previous
    return f"{previous}\n\n{AUDIT_PREPARE_SEPARATOR}\n\n{current}"


def append_audit_pending_requirement(link: object, current_prompt: object) -> str:
    """Append unpublished prepare turns for one exact named Audit.

    A later prepare turn may be a correction, a probe result, or another
    source.  The host does not interpret that prose; it preserves exact
    chronological user authority until a structured publication commits it.
    """

    current = str(current_prompt or "").strip()
    previous = str(getattr(link, "pending_prompt", "") or "").strip()
    if not previous:
        return current
    if not current or current == previous:
        return previous
    return f"{previous}\n\n{AUDIT_PREPARE_SEPARATOR}\n\n{current}"


def published_audit_requirement(
    *,
    validated_notes: object,
    user_prepare_history: object,
) -> str:
    """Wrap derived operational notes below exact chronological user authority."""
    notes = canonical_audit_validated_notes(validated_notes)
    user_text = str(user_prepare_history or "").strip()
    return "\n".join(
        [
            AUDIT_CONTEXT_TITLE,
            AUDIT_NOTES_MARKER,
            notes,
            "",
            AUDIT_USER_MARKER,
            user_text,
            "",
            AUDIT_LIFECYCLE_MARKER,
            AUDIT_LIFECYCLE_STATUS,
        ]
    ).strip()


def canonical_audit_validated_notes(value: object) -> str:
    """Remove only exact host wrappers recursively returned by a model."""
    text = str(value or "").strip()
    while True:
        inner = _audit_wrapper_notes(text)
        if not inner or len(inner) >= len(text):
            break
        text = inner
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].strip() == AUDIT_CONTEXT_TITLE:
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
        text = "\n".join(lines).strip()
    return text


def _audit_wrapper_notes(text: str) -> str:
    """Read only the exact host-owned wrapper, including JSON-escaped newlines.

    Some providers can return a tool string whose line separators are the two
    literal characters ``\\n``.  That is still recognisable without guessing
    any user or business language because both boundary markers are owned by
    the host.  Decode only the derived notes slice; the exact user prepare
    history remains untouched in the named Audit record.
    """

    for newline in ("\n", r"\n"):
        prefixes = (
            (
                f"{AUDIT_CONTEXT_TITLE}{newline}{AUDIT_NOTES_MARKER}{newline}",
                False,
            ),
            (
                f"{AUDIT_CONTEXT_TITLE}{newline}{newline}{AUDIT_NOTES_MARKER}{newline}",
                False,
            ),
            (f"{AUDIT_NOTES_MARKER}{newline}", True),
        )
        prefix, partial = next(
            (item for item in prefixes if text.startswith(item[0])),
            ("", False),
        )
        if not prefix:
            continue
        body = text[len(prefix) :]
        boundaries = (
            f"{newline}{newline}{AUDIT_USER_MARKER}{newline}",
            f"{newline}{AUDIT_USER_MARKER}{newline}",
        )
        find_boundary = body.find if partial else body.rfind
        found = [
            (offset, marker)
            for marker in boundaries
            for offset in [find_boundary(marker)]
            if offset >= 0
        ]
        if not found:
            notes = body.strip() if partial else ""
        elif not partial:
            # A historical model response may have embedded an older complete
            # host wrapper inside the new derived-note field.  The last exact
            # user marker belongs to the outer wrapper created by this host;
            # recursive handling below removes any inner wrapper.
            offset, _marker = max(found, key=lambda item: item[0])
            notes = body[:offset].strip()
        else:
            # For an embedded notes wrapper, preserve model-authored additions
            # after the exact host lifecycle footer while dropping the nested
            # copy of user history.  This is marker-based projection only; no
            # business prose is classified or rewritten.
            offset, marker = min(found, key=lambda item: item[0])
            head = body[:offset].strip()
            nested_user = body[offset + len(marker) :]
            footers = (
                f"{newline}{newline}{AUDIT_LIFECYCLE_MARKER}{newline}{AUDIT_LIFECYCLE_STATUS}",
                f"{newline}{AUDIT_LIFECYCLE_MARKER}{newline}{AUDIT_LIFECYCLE_STATUS}",
            )
            footer_hits = [
                (nested_user.find(footer), footer)
                for footer in footers
                if nested_user.find(footer) >= 0
            ]
            if not footer_hits:
                # A partial host marker without the complete lifecycle footer
                # is not a wrapper the host can safely project.  Return it
                # unchanged so the publication boundary can reject it rather
                # than guessing where arbitrary user prose ends.
                return text
            footer_offset, footer = min(footer_hits, key=lambda item: item[0])
            tail = nested_user[footer_offset + len(footer) :].strip()
            notes = "\n\n".join(part for part in (head, tail) if part).strip()
        if newline == r"\n":
            notes = notes.replace(r"\r\n", "\n").replace(r"\n", "\n")
        return notes.strip()
    return ""


def audit_runtime_requirement_text(value: object) -> str:
    """Project the current bounded worker requirement from one published Audit.

    The named task keeps the exact chronological prepare text for status,
    review, and future coordinator turns.  A source worker must not receive all
    sibling-source prepare turns on every batch: its exact source profile and
    document refs already carry the source-local business context.  The shared
    worker projection therefore contains only the coordinator-published current
    operational notes.  This is a structural wrapper projection; it never
    classifies user prose or source content.

    Legacy unwrapped values are returned unchanged so an already-open Audit can
    continue safely across an upgrade.
    """

    return canonical_audit_validated_notes(value)


def audit_runtime_requirement_for_task(
    store: object,
    task_id: object,
    *,
    fallback: object = "",
) -> str:
    """Resolve one active named Audit's current worker projection."""

    selected = str(task_id or "").strip()
    loader = getattr(store, "load_task_link", None)
    if not selected or not callable(loader):
        return audit_runtime_requirement_text(fallback)
    try:
        link = loader(selected)
    except Exception:
        link = None
    if (
        link is None
        or str(getattr(link, "task_id", "") or "") != selected
        or str(getattr(link, "work_kind", "") or "").strip().lower() != "audit"
        or str(getattr(link, "status", "") or "").strip().lower() != "active"
    ):
        return audit_runtime_requirement_text(fallback)
    published = audit_runtime_requirement_text(getattr(link, "goal", ""))
    return published or audit_runtime_requirement_text(fallback)


def has_audit_host_section_marker(value: object) -> bool:
    """Return whether model-authored text still contains a host-owned section."""
    text = str(value or "")
    return any(marker in text for marker in AUDIT_HOST_SECTION_MARKERS)


__all__ = [
    "append_audit_pending_requirement",
    "append_audit_user_requirement",
    "audit_runtime_requirement_text",
    "audit_runtime_requirement_for_task",
    "audit_user_requirement_text",
    "canonical_audit_validated_notes",
    "has_audit_host_section_marker",
    "published_audit_requirement",
]
