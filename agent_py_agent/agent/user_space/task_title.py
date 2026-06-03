from __future__ import annotations

import hashlib
import re
from pathlib import Path


def prompt_fingerprint(prompt: str) -> str:
    text = str(prompt or "").strip()
    if not text:
        return ""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def concise_task_title(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return "task"
    if looks_like_machine_id(raw):
        return "task"
    path_title = _title_from_paths(raw)
    if path_title:
        return path_title
    first = _first_meaningful_line(raw)
    first = re.sub(r"^(你现在|现在)?只做一件事[:：]?", "", first).strip()
    first = re.sub(r"^(请|帮我|麻烦)?(你)?", "", first).strip()
    first = first.replace("/Users/xiaoyezi/", "")
    first = re.sub(r"\s+", "-", first)
    first = collapse_dashes("".join(workspace_slug_char(char) for char in first.lower())).strip("-_")
    return (first[:32].strip("-_") or "task")


def looks_like_machine_id(value: str) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    machine_prefixes = (
        "run-",
        "gw-",
        "req-",
        "session-",
        "thread-",
        "subagent-",
        "capreq-",
        "capreq_",
        "auto-compact",
    )
    if text.startswith(machine_prefixes):
        return True
    return bool(re.fullmatch(r"(run|gw|req|task|session|thread)[_-]?[0-9a-f]{6,}", text))


def workspace_slug_char(char: str) -> str:
    return char if char.isalnum() or char in {"_", "-"} else "-"


def collapse_dashes(text: str) -> str:
    while "--" in text:
        text = text.replace("--", "-")
    return text


def _first_meaningful_line(text: str) -> str:
    for line in text.splitlines():
        cleaned = line.strip(" -#\t")
        if cleaned and not cleaned.startswith(("要求", "1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.")):
            return cleaned
    return text.strip()


def _title_from_paths(text: str) -> str:
    paths = re.findall(r"(/[\w.\- \u4e00-\u9fff/]+)", text)
    names: list[str] = []
    for item in paths:
        path = re.split(r"(?:下面|下|目录|里|中|，|。|；|:|：)", item.strip(), maxsplit=1)[0].rstrip("/")
        name = Path(path).name.strip()
        if name and name not in names:
            names.append(name)
        if len(names) >= 2:
            break
    if not names:
        return ""
    action = _action_label(text)
    return "-".join([*names, action]) if action else "-".join(names)


def _action_label(text: str) -> str:
    lowered = text.lower()
    if "架构" in text and ("分析" in text or "报告" in text):
        return "架构分析"
    if "源码" in text and ("分析" in text or "阅读" in text):
        return "源码分析"
    if "报告" in text:
        return "报告"
    if "测试" in text or "验收" in text:
        return "测试验收"
    if "readme" in lowered:
        return "readme"
    return ""


__all__ = [
    "collapse_dashes",
    "concise_task_title",
    "looks_like_machine_id",
    "prompt_fingerprint",
    "workspace_slug_char",
]
