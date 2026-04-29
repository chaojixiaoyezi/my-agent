from __future__ import annotations

import argparse
from pathlib import Path

from agent_py_agent.__main__ import build_parser


def test_cli_reference_mentions_all_commands_and_long_options():
    project_root = Path(__file__).resolve().parents[2]
    reference = (project_root / "CLI_REFERENCE.md").read_text(encoding="utf-8")
    parser = build_parser()

    missing: list[str] = []
    for action in parser._actions:
        for option in action.option_strings:
            if option.startswith("--") and option not in reference:
                missing.append(option)
        if isinstance(action, argparse._SubParsersAction):
            for command, subparser in action.choices.items():
                if f"`{command}`" not in reference:
                    missing.append(command)
                for sub_action in subparser._actions:
                    for option in sub_action.option_strings:
                        if option.startswith("--") and option not in reference:
                            missing.append(f"{command} {option}")

    assert missing == []
