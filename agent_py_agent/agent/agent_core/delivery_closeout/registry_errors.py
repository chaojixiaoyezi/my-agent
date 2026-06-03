"""Attach artifact registry diagnostics to delivery closeout reports."""

from __future__ import annotations

from typing import Any


def with_registry_read_errors(
    artifact_report: dict[str, Any],
    errors: list[dict[str, object]],
) -> dict[str, Any]:
    """Attach non-fatal registry read errors to one artifact report."""

    if not errors:
        return artifact_report
    updated = dict(artifact_report)
    updated["registry_read_errors"] = errors
    acceptance = updated.get("acceptance_report")
    if isinstance(acceptance, dict):
        updated["acceptance_report"] = {**acceptance, "registry_read_errors": errors}
    return updated


def registry_read_errors_from_artifacts(results: list[dict[str, Any]]) -> list[dict[str, object]]:
    """Collect registry read errors from artifact validation rows."""

    errors: list[dict[str, object]] = []
    for item in results:
        value = item.get("registry_read_errors")
        if isinstance(value, list):
            errors.extend(error for error in value if isinstance(error, dict))
    return errors
