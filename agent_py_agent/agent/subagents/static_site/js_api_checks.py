
from __future__ import annotations

import re


def missing_window_app_method_hits(script_text: str) -> list[str]:
    refs = _referenced_window_app_methods(script_text)
    if not refs:
        return []
    exported = _exported_window_app_methods(script_text)
    return [f"app.{name}" for name in sorted(refs - exported)]


def _referenced_window_app_methods(script_text: str) -> set[str]:
    return {
        name
        for name in re.findall(r"\bapp\.([A-Za-z_$][\w$]*)\s*\(", script_text or "")
        if name not in {"addEventListener"}
    }


def _exported_window_app_methods(script_text: str) -> set[str]:
    text = script_text or ""
    names = {match.group(1) for match in re.finditer(r"\bwindow\.app\.([A-Za-z_$][\w$]*)\s*=", text)}
    for body in re.findall(r"\bwindow\.app\s*=\s*\{(?P<body>.*?)\}\s*;", text, flags=re.DOTALL):
        names.update(_object_property_names(body))
    for body in re.findall(
        r"\b(?:const|let|var)\s+app\s*=\s*\{(?P<body>.*?)\}\s*;",
        text,
        flags=re.DOTALL,
    ):
        names.update(_object_property_names(body))
    for body in re.findall(
        r"\b(?:const|let|var)\s+app\s*=\s*\{(?P<body>.*?)\}\s*;\s*window\.app\s*=\s*app\s*;",
        text,
        flags=re.DOTALL,
    ):
        names.update(_object_property_names(body))
    return names


def _object_property_names(body: str) -> set[str]:
    names: set[str] = set()
    cleaned = re.sub(r"//.*?$|/\*.*?\*/", "", body or "", flags=re.MULTILINE | re.DOTALL)
    for chunk in cleaned.split(","):
        item = chunk.strip()
        if not item:
            continue
        match = re.match(r"([A-Za-z_$][\w$]*)\s*:", item)
        if match:
            names.add(match.group(1))
            continue
        match = re.match(r"([A-Za-z_$][\w$]*)\s*(?:\(|$)", item)
        if match:
            names.add(match.group(1))
    return names


__all__ = ["missing_window_app_method_hits"]
