from __future__ import annotations

import argparse
from pathlib import Path

from agent_py_agent.__main__ import build_parser


def _collect_subparser_missing(subparser, command, reference):
    """Collect missing commands and options from a subparser."""
    missing = []
    for sub_action in subparser._actions:
        for sub_option in sub_action.option_strings:
            if sub_option.startswith("--") and sub_option not in reference:
                missing.append(f"{command} {sub_option}")
    return missing


def _check_action_options(action, reference, missing):
    """Check a single parser action for missing --options."""
    for option in action.option_strings:
        if option.startswith("--") and option not in reference:
            missing.append(option)


def test_cli_reference_mentions_all_commands_and_long_options():
    """Every --long option and subcommand in the parser appears in CLI_REFERENCE.md."""
    project_root = Path(__file__).resolve().parents[2]
    reference = (project_root / "CLI_REFERENCE.md").read_text(encoding="utf-8")
    parser = build_parser()

    missing: list[str] = []
    for action in parser._actions:
        _check_action_options(action, reference, missing)
        if isinstance(action, argparse._SubParsersAction):
            for command, subparser in action.choices.items():
                if f"`{command}`" not in reference:
                    missing.append(command)
                missing.extend(_collect_subparser_missing(subparser, command, reference))

    assert missing == []
