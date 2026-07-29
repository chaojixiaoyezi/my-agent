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


def test_web_project_check_skips_server_side_templates(tmp_path: Path) -> None:
    """Flask/Django+Jinja2 服务端模板(templates/ 目录或含 {%/{{ 语法)不该被静态站点完整性校验误杀
    ——它们是片段、引用运行期由框架解析。真静态网站(无模板语法)仍照常校验(回归:dogfooding 真机大
    任务里 my-agent 写 Flask 模板被 write_file 反复 ACCEPTANCE_FAILED 卡死 6 轮、回退命令行又 too long)。"""
    from agent_py_agent.agent.tooling.web_project_integrity import check_web_project_post_write
    tdir = tmp_path / "web" / "templates"
    tdir.mkdir(parents=True)
    base = tdir / "base.html"
    base.write_text("<!DOCTYPE html><html>{% block body %}{% endblock %}</html>", encoding="utf-8")
    dash = tdir / "dashboard.html"
    dash.write_text('{% extends "base.html" %}{% block body %}{{ title }}{% endblock %}', encoding="utf-8")
    assert check_web_project_post_write(base, tmp_path).ok  # templates/ 模板片段:跳过,不报 incomplete
    assert check_web_project_post_write(dash, tmp_path).ok
    app = tmp_path / "app"
    app.mkdir()
    jt = app / "index.html"
    jt.write_text("<html><body>{% for x in items %}{{ x }}{% endfor %}</body></html>", encoding="utf-8")
    assert check_web_project_post_write(jt, tmp_path).ok  # 非 templates 目录但含模板语法:也跳过
    site = tmp_path / "site"
    site.mkdir()
    idx = site / "index.html"
    idx.write_text('<!DOCTYPE html><html lang="en"><head><title>x</title><link rel="stylesheet" href="missing.css"></head><body><h1>Hi</h1></body></html>', encoding="utf-8")
    static_decision = check_web_project_post_write(idx, tmp_path)
    assert static_decision.ok and static_decision.kind == "web_project"
    assert "STATIC_SITE_BROKEN_LOCAL_REFS" in static_decision.warning_codes


def test_incremental_site_write_succeeds_but_final_validator_stays_strict(
    tmp_path: Path,
) -> None:
    from agent_py_agent.agent.subagents.static_site import run_static_site_check
    from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool

    result = WriteFileTool(tmp_path).execute(
        {
            "path": "site/index.html",
            "content": (
                '<!doctype html><html><body><a href="details.html">'
                "详情</a></body></html>"
            ),
        }
    )

    assert result.ok is True
    integrity = result.result_envelope["artifact_integrity"]
    assert "STATIC_SITE_BROKEN_LOCAL_REFS" in integrity["warning_codes"]
    assert "写入已成功" in result.output

    incomplete = run_static_site_check(
        {
            "validation_method": "static_site_check",
            "site_root": "site",
            "check_local_refs": True,
        },
        tmp_path,
    )
    assert incomplete.passed is False

    second = WriteFileTool(tmp_path).execute(
        {
            "path": "site/details.html",
            "content": "<!doctype html><html><body>完成</body></html>",
        }
    )
    assert second.ok is True
    complete = run_static_site_check(
        {
            "validation_method": "static_site_check",
            "site_root": "site",
            "check_local_refs": True,
        },
        tmp_path,
    )
    assert complete.passed is True
