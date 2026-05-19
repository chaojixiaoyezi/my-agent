from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.tooling._filesystem_write import AppendFileTool
from agent_py_agent.agent.tooling.artifact_integrity import (
    ArtifactIntegrityCheckRequest,
    check_artifact_integrity,
    html_post_write_note,
)


# LLM: HTML artifacts should not accept append chunks after the document is closed.
# 函数用途: 防止子代理把内容追加到 </html> 后面，导致页面文件看似完成但实际结构脏掉。
def test_append_file_blocks_html_content_after_closing_tag(tmp_path: Path) -> None:
    artifact = tmp_path / "artifacts" / "index.html"
    artifact.parent.mkdir()
    artifact.write_text("<html><body><main>done</main></body></html>", encoding="utf-8")
    tool = AppendFileTool(tmp_path)

    result = tool.execute({
        "path": "artifacts/index.html",
        "content": "\n<section>late content</section>",
    })

    assert not result.ok
    assert "HTML 文件已经闭合" in result.output
    assert artifact.read_text(encoding="utf-8") == "<html><body><main>done</main></body></html>"


# LLM: artifact integrity should catch files that are incomplete before parent acceptance.
# 函数用途: 验证 HTML 产物少了结束标签时会被机器检查识别为不可验收。
def test_artifact_integrity_detects_incomplete_html(tmp_path: Path) -> None:
    artifact = tmp_path / "artifacts" / "index.html"
    artifact.parent.mkdir()
    artifact.write_text("<html><body><main>unfinished", encoding="utf-8")

    result = check_artifact_integrity(ArtifactIntegrityCheckRequest(path=artifact, require_complete=True))

    assert not result.ok
    assert "missing_body_close" in result.blocker_codes
    assert "missing_html_close" in result.blocker_codes


# LLM: artifact integrity should catch content appended after the closing html tag.
# 函数用途: 验证 HTML 已闭合后又追加正文会被标成脏产物，不能让 runner 直接进入验收。
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


# LLM: artifact integrity must catch CSS blocks left open before static site acceptance.
# 函数用途: 复现真实购物站产物 `<style>` 未闭合，防止坏 HTML 进入更晚的父级验收才暴露。
def test_artifact_integrity_detects_unbalanced_style_tag(tmp_path: Path) -> None:
    artifact = tmp_path / "artifacts" / "index.html"
    artifact.parent.mkdir()
    artifact.write_text(
        "<!doctype html><html><head><style>.hero{display:block}</head><body>done</body></html>",
        encoding="utf-8",
    )

    result = check_artifact_integrity(ArtifactIntegrityCheckRequest(path=artifact, require_complete=True))

    assert not result.ok
    assert "unbalanced_style" in result.blocker_codes


def test_artifact_integrity_detects_duplicate_html_skeleton(tmp_path: Path) -> None:
    artifact = tmp_path / "artifacts" / "index.html"
    artifact.parent.mkdir()
    artifact.write_text(
        (
            "<!doctype html><html><head><title>One</title></head><body><main>One</main>"
            "<head><title>Two</title></head><body><main>Two</main></body></html>"
        ),
        encoding="utf-8",
    )

    result = check_artifact_integrity(ArtifactIntegrityCheckRequest(path=artifact, require_complete=True))

    assert not result.ok
    assert "multiple_head_open" in result.blocker_codes
    assert "multiple_body_open" in result.blocker_codes


# LLM: post-write feedback should point models at fake hash links before parent acceptance.
# 函数用途: 写 HTML 后即时提示 href="#"，减少 runner 写完后继续空转或把假链接报成完成。
def test_html_post_write_note_warns_placeholder_hash_links(tmp_path: Path) -> None:
    artifact = tmp_path / "artifacts" / "index.html"
    artifact.parent.mkdir()
    text = "<html><body><main id='hero'>done</main><a href='#'>品牌故事</a></body></html>"

    note = html_post_write_note(artifact, text)

    assert "placeholder_hash_link" in note
    assert "品牌故事 href=#" in note


# LLM: HTML issue diagnostics should give runners concrete links to repair, not only abstract codes.
# 函数用途: 验证 href="#" 和缺失 #id 会汇总数量与示例，避免子代理在真实 E2E 里反复空修。
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
