from __future__ import annotations

"""CLI module entrypoint for `python -m agent_py_agent`."""

from .cli.parser import main as _cli_main


def main(argv=None):
    from .agent.io.bounded_process_log import bounded_stdio_from_environment

    with bounded_stdio_from_environment():
        return _cli_main(argv)

if __name__ == "__main__":
    raise SystemExit(main())
