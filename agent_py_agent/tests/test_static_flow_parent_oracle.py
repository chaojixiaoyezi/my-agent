"""Tests for the parent-owned generic static-flow oracle."""

from __future__ import annotations

import subprocess

from agent_py_agent.agent.subagents.static_flow_parent_oracle import (
    StaticFlowParentOracleRequest,
    write_static_flow_parent_oracle,
)


# LLM: The generic parent oracle should accept a complete static flow fixture.
# 函数用途: 验证父级验收 oracle 的 required sections/actions 都来自结构化请求，而不是内置电商流程。
def test_static_flow_parent_oracle_passes_on_complete_fixture(tmp_path):
    webapp = tmp_path / "static-flow-app"
    _write_flow_fixture(webapp, broken_image=False, missing_handler=False)

    pack_path = write_static_flow_parent_oracle(
        StaticFlowParentOracleRequest(
            reports_dir=tmp_path / "reports",
            webapp_dir=webapp,
            required_sections=("hero", "editor", "review"),
            required_actions=("open-editor", "save-draft", "publish"),
            require_local_images=True,
            require_responsive_layout=True,
        )
    )
    completed = subprocess.run(
        ["python3", "-m", "pytest", "tests/parent/test_static_flow_contract.py", "-q"],
        cwd=webapp,
        capture_output=True,
        text=True,
        check=False,
    )

    assert pack_path.is_file()
    assert completed.returncode == 0, completed.stdout + completed.stderr


# LLM: The generic parent oracle should catch broken assets and action handlers.
# 函数用途: 验证通用静态流程验收仍能抓资源丢失和按钮无处理器，不依赖示例流程领域词。
def test_static_flow_parent_oracle_catches_broken_asset_and_action_handler(tmp_path):
    webapp = tmp_path / "static-flow-app"
    _write_flow_fixture(webapp, broken_image=True, missing_handler=True)
    write_static_flow_parent_oracle(
        StaticFlowParentOracleRequest(
            reports_dir=tmp_path / "reports",
            webapp_dir=webapp,
            required_sections=("hero", "editor", "review"),
            required_actions=("open-editor", "save-draft", "publish"),
            require_local_images=True,
            require_responsive_layout=True,
        )
    )

    completed = subprocess.run(
        ["python3", "-m", "pytest", "tests/parent/test_static_flow_contract.py", "-q"],
        cwd=webapp,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    combined = completed.stdout + completed.stderr
    assert "missing image asset" in combined
    assert "button action has no JS handler" in combined


# LLM: Local asset validation should block path traversal, not only missing files.
# 函数用途: 确认通用静态流程 oracle 会拒绝逃出 webapp 根目录的本地资源引用。
def test_static_flow_parent_oracle_rejects_asset_path_escape(tmp_path):
    webapp = tmp_path / "static-flow-app"
    _write_flow_fixture(webapp, broken_image=False, missing_handler=False)
    (tmp_path / "outside.svg").write_text("<svg></svg>\n", encoding="utf-8")
    html = (webapp / "index.html").read_text(encoding="utf-8")
    (webapp / "index.html").write_text(
        html.replace("assets/preview.svg", "../outside.svg"),
        encoding="utf-8",
    )
    write_static_flow_parent_oracle(
        StaticFlowParentOracleRequest(
            reports_dir=tmp_path / "reports",
            webapp_dir=webapp,
            required_sections=("hero", "editor", "review"),
            required_actions=("open-editor", "save-draft", "publish"),
            require_local_images=True,
            require_responsive_layout=True,
        )
    )

    completed = subprocess.run(
        ["python3", "-m", "pytest", "tests/parent/test_static_flow_contract.py", "-q"],
        cwd=webapp,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "escapes webapp root" in completed.stdout + completed.stderr


def _write_flow_fixture(webapp, *, broken_image: bool, missing_handler: bool) -> None:
    (webapp / "assets").mkdir(parents=True)
    (webapp / "src").mkdir()
    image_src = "assets/missing.svg" if broken_image else "assets/preview.svg"
    if not broken_image:
        (webapp / "assets" / "preview.svg").write_text("<svg></svg>\n", encoding="utf-8")
    actions = ["open-editor", "save-draft", "publish"]
    buttons = "\n".join(f'<button data-action="{action}">{action}</button>' for action in actions)
    (webapp / "index.html").write_text(
        f"""
        <html>
          <head><link rel="stylesheet" href="src/styles.css"><script defer src="src/app.js"></script></head>
          <body>
            <main id="app">
              <section id="hero"><img src="{image_src}" alt="Preview"></section>
              <section id="editor"></section>
              <section id="review"></section>
              {buttons}
            </main>
          </body>
        </html>
        """,
        encoding="utf-8",
    )
    handled = actions[:-1] if missing_handler else actions
    (webapp / "src" / "app.js").write_text(
        "\n".join(f"handleAction('{action}')" for action in handled),
        encoding="utf-8",
    )
    (webapp / "src" / "styles.css").write_text(
        ".workflow { display: grid; grid-template-columns: repeat(3, 1fr); }\n"
        "@media (max-width: 720px) { .workflow { grid-template-columns: 1fr; } }\n",
        encoding="utf-8",
    )
