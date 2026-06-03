from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.tooling.artifact_integrity import (
    ArtifactIntegrityCheckRequest,
    check_artifact_integrity,
    html_post_write_note,
)


def test_artifact_integrity_detects_incomplete_html(tmp_path: Path) -> None:
    artifact = tmp_path / "artifacts" / "index.html"
    artifact.parent.mkdir()
    artifact.write_text("<html><body><main>unfinished", encoding="utf-8")

    result = check_artifact_integrity(ArtifactIntegrityCheckRequest(path=artifact, require_complete=True))

    assert not result.ok
    assert "missing_body_close" in result.blocker_codes
    assert "missing_html_close" in result.blocker_codes


def test_artifact_integrity_detects_content_after_html_close(tmp_path: Path) -> None:
    artifact = tmp_path / "artifacts" / "index.html"
    artifact.parent.mkdir()
    artifact.write_text(
        "<html><body><main>done</main></body></html>\n<section>late</section>",
        encoding="utf-8",
    )

    result = check_artifact_integrity(ArtifactIntegrityCheckRequest(path=artifact, require_complete=True))

    assert not result.ok
    assert "content_after_html_close" in result.blocker_codes


def test_html_post_write_note_warns_placeholder_hash_links(tmp_path: Path) -> None:
    artifact = tmp_path / "artifacts" / "index.html"
    artifact.parent.mkdir()
    text = "<html><body><main id='hero'>done</main><a href='#'>品牌故事</a></body></html>"

    note = html_post_write_note(artifact, text)

    assert "placeholder_hash_link" in note
    assert "品牌故事 href=#" in note


def test_artifact_integrity_link_issues_include_counts_and_examples(tmp_path: Path) -> None:
    artifact = tmp_path / "artifacts" / "index.html"
    artifact.parent.mkdir()
    artifact.write_text(
        (
            "<html><body><main id='hero'>done</main>"
            "<a href='#'>品牌故事</a>"
            "<a href='#'>立即预约</a>"
            "<a href='#missing'>空间系列</a>"
            "</body></html>"
        ),
        encoding="utf-8",
    )

    result = check_artifact_integrity(ArtifactIntegrityCheckRequest(path=artifact, require_complete=True))
    issues = {issue.code: issue for issue in result.issues}

    assert issues["placeholder_hash_link"].count == 2
    assert "品牌故事 href=#" in issues["placeholder_hash_link"].examples
    assert "立即预约 href=#" in issues["placeholder_hash_link"].examples
    assert issues["missing_hash_target"].count == 1
    assert "空间系列 href=#missing" in issues["missing_hash_target"].examples
