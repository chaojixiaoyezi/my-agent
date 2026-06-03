from importlib import import_module
from typing import Any

_EXPORTS = {
    "artifact_pytest_items": "pytest_items",
    "CatContentCheckRequest": "content_checks",
    "ContentCheckInferenceRequest": "inferred_content_items",
    "drop_non_executable_model_checklist_items": "test_checklists",
    "expand_artifact_integrity_items": "artifact_integrity_items",
    "expand_file_exists_items": "file_exists_items",
    "inferred_content_check_items": "inferred_content_items",
    "inferred_static_site_items": "static_site_items",
    "load_test_execution_report": "report",
    "normalize_cat_content_check": "content_checks",
    "prepare_test_items": "test_items",
    "render_test_execution_markdown": "report",
    "StaticSiteTestItemsRequest": "static_site_items",
    "TestExecutionRecord": "records",
    "TestExecutionReport": "report",
    "TestExecutionReportOptions": "report",
    "TestExecutor": "executor",
    "TestItemPreparationContext": "test_items",
    "TestItemPreparationRequest": "test_items",
    "write_test_execution_report": "report",
}

__all__ = [
    "CatContentCheckRequest",
    "ContentCheckInferenceRequest",
    "StaticSiteTestItemsRequest",
    "TestExecutionRecord",
    "TestExecutionReport",
    "TestExecutionReportOptions",
    "TestExecutor",
    "TestItemPreparationContext",
    "TestItemPreparationRequest",
    "artifact_pytest_items",
    "drop_non_executable_model_checklist_items",
    "expand_artifact_integrity_items",
    "expand_file_exists_items",
    "inferred_content_check_items",
    "inferred_static_site_items",
    "load_test_execution_report",
    "normalize_cat_content_check",
    "prepare_test_items",
    "render_test_execution_markdown",
    "write_test_execution_report",
]


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name:
        return getattr(import_module(f"{__name__}.{module_name}"), name)
    raise AttributeError(name)
