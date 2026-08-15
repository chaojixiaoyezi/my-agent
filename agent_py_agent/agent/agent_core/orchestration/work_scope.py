from __future__ import annotations

import hashlib
import json

from ..runner.ref_fields import params_input_refs, params_output_refs


def add_work_scope_key(attrs: dict[str, object]) -> None:
    if str(attrs.get("work_scope_key") or "").strip():
        return
    identity = _work_scope_identity(attrs)
    if not identity:
        return
    payload = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    attrs["work_scope_key"] = "scope:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _work_scope_identity(attrs: dict[str, object]) -> dict[str, object]:
    outputs = _scope_refs(attrs, ("output_files", "output_refs", "artifact_refs"), output=True)
    inputs = _scope_refs(attrs, ("input_files", "input_refs", "required_read_paths"), output=False)
    if not outputs or not inputs:
        return {}
    return {
        "outputs": outputs,
        "inputs": inputs,
    }


def _scope_refs(attrs: dict[str, object], keys: tuple[str, ...], *, output: bool) -> list[str]:
    refs: list[str] = []
    for key in keys:
        value = attrs.get(key)
        refs.extend(params_output_refs({key: value}) if output else params_input_refs({key: value}))
        refs.extend(_structured_refs(value))
    return sorted(dict.fromkeys(refs))


def _structured_refs(value: object) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple, set)):
        return [item for raw in value for item in _structured_refs(raw)]
    if isinstance(value, dict):
        refs: list[str] = []
        for raw in value.values():
            refs.extend(_structured_refs(raw))
        return refs
    return []


__all__ = ["add_work_scope_key"]
