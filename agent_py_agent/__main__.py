from __future__ import annotations

"""CLI module entrypoint for `python -m agent_py_agent`."""

from .cli.parser import main

if __name__ == "__main__":
    raise SystemExit(main())
