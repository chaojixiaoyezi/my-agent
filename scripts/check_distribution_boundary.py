#!/usr/bin/env python3

from __future__ import annotations

"""Inspect a built wheel and reject forbidden or stale production members."""

import argparse
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from package_boundary_policy import forbidden_distribution_member

_RUNTIME_RESOURCE_ROOTS = (
    "agent_py_agent/config",
    "agent_py_agent/prompts",
    "agent_py_agent/skills/builtin",
    "agent_py_agent/vendor/bin",
)


def forbidden_members(wheel: Path) -> list[str]:
    with zipfile.ZipFile(wheel) as archive:
        return sorted(name for name in archive.namelist() if forbidden_distribution_member(name))


def source_missing_members(wheel: Path, source_root: Path = ROOT) -> list[str]:
    """Reject package payload resurrected from a stale local build directory."""
    with zipfile.ZipFile(wheel) as archive:
        members = [
            name
            for name in archive.namelist()
            if name.startswith("agent_py_agent/") and not name.endswith("/")
        ]
    return sorted(name for name in members if not (source_root / name).is_file())


def missing_runtime_resource_members(
    wheel: Path,
    source_root: Path = ROOT,
) -> list[str]:
    """Reject wheels that silently omit source-owned runtime resources.

    Python import checks cannot notice a missing prompt, config, bundled sandbox
    binary, or builtin SKILL.md.  Compare the built artifact with the canonical
    source resource roots so the release gate checks both sides of the boundary:
    no stale extras and no required omissions.
    """

    required = {
        path.relative_to(source_root).as_posix()
        for root_name in _RUNTIME_RESOURCE_ROOTS
        for path in (source_root / root_name).rglob("*")
        if path.is_file()
    }
    with zipfile.ZipFile(wheel) as archive:
        members = set(archive.namelist())
    return sorted(required - members)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a production wheel boundary.")
    parser.add_argument("wheel")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    wheel = Path(args.wheel).expanduser().resolve()
    findings = forbidden_members(wheel)
    source_missing = source_missing_members(wheel)
    resource_missing = missing_runtime_resource_members(wheel)
    payload = {
        "ok": not findings and not source_missing and not resource_missing,
        "wheel": str(wheel),
        "forbidden_members": findings,
        "source_missing_members": source_missing,
        "missing_runtime_resource_members": resource_missing,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            "DISTRIBUTION_BOUNDARY "
            f"ok={payload['ok']} forbidden={len(findings)} source_missing={len(source_missing)} "
            f"resource_missing={len(resource_missing)}"
        )
        for name in findings:
            print(f"- {name}")
        for name in source_missing:
            print(f"- stale-build-member: {name}")
        for name in resource_missing:
            print(f"- missing-runtime-resource: {name}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
