from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.tooling._filesystem_write import AppendFileTool
from agent_py_agent.agent.tooling.artifact_integrity import (
    ArtifactIntegrityCheckRequest,
    check_artifact_integrity,
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
