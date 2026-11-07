"""S2-W5B: the declared scale extra, not a CI-only workaround, selects psycopg."""
from __future__ import annotations

from pathlib import Path

import pytest
import tomllib
from packaging.markers import default_environment
from packaging.requirements import Requirement


@pytest.mark.parametrize("version,extras", [("3.12", {"binary"}), ("3.13", {"binary"}), ("3.14", set()), ("3.15", set())])
def test_scale_extra_selects_one_psycopg_driver_for_interpreter(version, extras):
    config = tomllib.loads((Path(__file__).resolve().parents[3] / "pyproject.toml").read_text())
    environment = {**default_environment(), "python_version": version}
    drivers = [Requirement(value) for value in config["project"]["optional-dependencies"]["scale"]]
    selected = [item for item in drivers if item.name == "psycopg" and (
        item.marker is None or item.marker.evaluate(environment)
    )]
    assert len(selected) == 1
    assert selected[0].extras == extras
    assert selected[0].specifier.contains("3.1")
