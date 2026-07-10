#!/usr/bin/env python3

from __future__ import annotations

"""Inspect a built wheel and reject test/dev-only modules in production."""

import argparse
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from package_boundary_policy import forbidden_distribution_member


def forbidden_members(wheel: Path) -> list[str]:
    with zipfile.ZipFile(wheel) as archive:
        return sorted(name for name in archive.namelist() if forbidden_distribution_member(name))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a production wheel boundary.")
    parser.add_argument("wheel")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    wheel = Path(args.wheel).expanduser().resolve()
    findings = forbidden_members(wheel)
    payload = {"ok": not findings, "wheel": str(wheel), "forbidden_members": findings}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"DISTRIBUTION_BOUNDARY ok={payload['ok']} forbidden={len(findings)}")
        for name in findings:
            print(f"- {name}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
