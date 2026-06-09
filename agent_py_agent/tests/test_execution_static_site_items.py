"""LLM: regression tests for inferred static-site acceptance items."""

from agent_py_agent.agent.subagents.execution.test_items import (
    TestItemPreparationRequest,
    prepare_test_items,
)


def test_prepare_items_infers_static_check_from_required_files_without_artifacts(tmp_path):
    site = tmp_path / "artifacts"
    site.mkdir()
    (site / "index1.html").write_text("<html></html>", encoding="utf-8")
    (site / "index2.html").write_text("<html></html>", encoding="utf-8")

    items = prepare_test_items(
        TestItemPreparationRequest(
            tests=[],
            output={"artifacts": []},
            workspace_root=tmp_path,
            required_files=["index1.html", "index2.html"],
        )
    )

    assert items == [
        {
            "name": "inferred static site check",
            "validation_method": "static_site_check",
            "site_root": "artifacts",
            "required_files": ["index1.html", "index2.html"],
            "require_complete_html": True,
        }
    ]


def test_prepare_items_scopes_static_check_to_observed_leaf_artifacts(tmp_path):
    site = tmp_path / "artifacts"
    site.mkdir()
    (site / "index1.html").write_text("<html></html>", encoding="utf-8")

    items = prepare_test_items(
        TestItemPreparationRequest(
            tests=[],
            output={"artifacts": [{"path": str(site / "index1.html"), "kind": "file"}]},
            workspace_root=tmp_path,
            required_files=["index1.html", "index2.html"],
        )
    )

    assert items == [
        {
            "name": "inferred static site check",
            "validation_method": "static_site_check",
            "site_root": "artifacts",
            "required_files": ["index1.html"],
            "require_complete_html": True,
            "html_files": ["index1.html"],
        }
    ]


def test_prepare_items_uses_site_root_hints_when_no_artifacts(tmp_path):
    deliverables = tmp_path / "nested" / "deliverables"
    deliverables.mkdir(parents=True)
    (deliverables / "index2.html").write_text("<!doctype html><html><body>ok</body></html>", encoding="utf-8")

    items = prepare_test_items(
        TestItemPreparationRequest(
            tests=[],
            output={},
            workspace_root=tmp_path,
            required_files=["index2.html"],
            site_root_hints=[deliverables],
        )
    )

    assert items[0]["site_root"] == "nested/deliverables"
    assert items[0]["required_files"] == ["index2.html"]
