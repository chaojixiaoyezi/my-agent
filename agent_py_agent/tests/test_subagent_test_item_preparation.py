"""测试子代理验收测试项目录推断。

审计 R0:command/cwd/working_dir 的 loader 路径已删除——不再合成 pytest 项、
不再归一化 cd 前缀、不再推断 working_dir、不再把 cat 命令转成 content_check。
模型声明的 command 只保留为 inert evidence 原样不动。
"""

from agent_py_agent.agent.subagents.execution.test_items import (
    TestItemPreparationRequest,
    prepare_test_items,
)


def test_prepare_test_items_does_not_infer_working_dir_for_command_items(tmp_path):
    """command 项原样保留,不再推断 working_dir。"""
    target_dir = tmp_path / "strings"
    target_dir.mkdir()
    artifact = target_dir / "test_string_tools.py"
    artifact.write_text("pass\n", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[{
                "name": "test_string_tools.py",
                "validation_method": "command",
                "command": "python -m unittest discover -s . -p 'test_*.py'",
            }],
            output={"artifacts": [{"path": str(artifact)}]},
            workspace_root=tmp_path,
        )
    )

    assert "working_dir" not in prepared[0]
    assert prepared[0]["command"] == "python -m unittest discover -s . -p 'test_*.py'"


def test_prepare_test_items_keeps_explicit_working_dir(tmp_path):
    """显式 working_dir 原样保留(仅作 inert evidence,不参与任何路径处理)。"""
    target_dir = tmp_path / "strings"
    target_dir.mkdir()
    artifact = target_dir / "test_string_tools.py"
    artifact.write_text("pass\n", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[{
                "name": "test_string_tools.py",
                "validation_method": "command",
                "command": "python -m unittest discover -s . -p 'test_*.py'",
                "working_dir": ".",
            }],
            output={"artifacts": [{"path": str(artifact)}]},
            workspace_root=tmp_path,
        )
    )

    assert prepared[0]["working_dir"] == "."


def test_prepare_test_items_does_not_touch_out_of_workspace_command(tmp_path):
    """工作区外产物不影响 command 项(不推断、不拒绝)。"""
    outside = tmp_path.parent / "test_outside.py"

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[{
                "name": "test_outside.py",
                "validation_method": "command",
                "command": "python -m unittest discover -s . -p 'test_*.py'",
            }],
            output={"artifacts": [{"path": str(outside)}]},
            workspace_root=tmp_path,
        )
    )

    assert "working_dir" not in prepared[0]


def test_prepare_test_items_keeps_relative_artifact_command_untouched(tmp_path):
    """command 里出现相对产物路径也不再触发 cwd 推断,原样保留。"""
    target_dir = tmp_path / "grandchild_sorting_edge"
    target_dir.mkdir()
    artifact = target_dir / "test_sorting_edges.py"
    artifact.write_text("pass\n", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[{
                "name": "test_sorting_edges.py",
                "validation_method": "command",
                "command": "python -m pytest grandchild_sorting_edge/test_sorting_edges.py -v",
            }],
            output={"artifacts": [{"path": "grandchild_sorting_edge/test_sorting_edges.py"}]},
            workspace_root=tmp_path,
        )
    )

    assert "working_dir" not in prepared[0]
    assert prepared[0]["command"] == "python -m pytest grandchild_sorting_edge/test_sorting_edges.py -v"


def test_prepare_test_items_keeps_cd_chain_command_untouched(tmp_path):
    """cd && 链不再剥离为 working_dir——command 原样保留为 inert evidence。"""
    target_dir = tmp_path / "deliverables" / "leaf"
    target_dir.mkdir(parents=True)
    artifact = target_dir / "test_solution.py"
    artifact.write_text("pass\n", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[{
                "name": "pytest test_solution.py",
                "validation_method": "command",
                "command": f"cd {target_dir} && python3 -m pytest test_solution.py -v",
            }],
            output={"artifacts": [{"path": str(artifact)}]},
            workspace_root=tmp_path,
        )
    )

    assert prepared[0]["command"] == f"cd {target_dir} && python3 -m pytest test_solution.py -v"
    assert "working_dir" not in prepared[0]


def test_prepare_test_items_does_not_synthesize_pytest_items(tmp_path):
    """没有 tests 时不再从 test_*.py 产物合成 pytest command 项。"""
    target_dir = tmp_path / "deliverables" / "leaf"
    target_dir.mkdir(parents=True)
    artifact = target_dir / "test_solution.py"
    artifact.write_text("pass\n", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[],
            output={"artifacts": [{"path": str(artifact), "kind": "test"}]},
            workspace_root=tmp_path,
        )
    )

    assert prepared == []


def test_prepare_test_items_infers_static_site_check_for_html_artifacts(tmp_path):
    site_dir = tmp_path / "deliverables" / "shop" / "build"
    site_dir.mkdir(parents=True)
    (site_dir / "register.html").write_text("<a href='login.html'>login</a>", encoding="utf-8")
    (site_dir / "login.html").write_text("<a href='register.html'>register</a>", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[],
            output={"artifacts": [
                {"path": str(site_dir / "register.html")},
                {"path": str(site_dir / "login.html")},
            ]},
            workspace_root=tmp_path,
        )
    )

    assert prepared == [{
        "name": "inferred static site check",
        "validation_method": "static_site_check",
        "site_root": "deliverables/shop/build",
        "required_files": ["login.html", "register.html"],
        "require_complete_html": True,
    }]


def test_prepare_test_items_drops_malformed_runner_checklist_when_static_check_is_available(tmp_path):
    site_dir = tmp_path / "deliverables" / "shop" / "build"
    site_dir.mkdir(parents=True)
    (site_dir / "index.html").write_text("<link rel='stylesheet' href='style.css'>", encoding="utf-8")
    (site_dir / "flow-a.html").write_text("<script src='app.js'></script>", encoding="utf-8")
    (site_dir / "style.css").write_text("body { color: #111; }\n", encoding="utf-8")
    (site_dir / "app.js").write_text("console.log('ok');\n", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[{
                "name": "文件结构验证",
                "validation_method": "content_check",
                "command": "",
                "working_dir": str(site_dir),
                "ok": True,
                "summary": "8个HTML文件 + CSS + JS + images目录",
            }],
            output={"artifacts": [
                {"path": str(site_dir / "index.html")},
                {"path": str(site_dir / "flow-a.html")},
                {"path": str(site_dir / "style.css")},
                {"path": str(site_dir / "app.js")},
            ]},
            workspace_root=tmp_path,
        )
    )

    assert prepared == [{
        "name": "inferred static site check",
        "validation_method": "static_site_check",
        "site_root": "deliverables/shop/build",
        "required_files": ["flow-a.html", "index.html"],
        "require_complete_html": True,
    }]


def test_prepare_test_items_infers_static_site_check_for_single_html_artifact(tmp_path):
    site_dir = tmp_path / "deliverables" / "shop" / "build"
    site_dir.mkdir(parents=True)
    (site_dir / "index.html").write_text(
        "<link rel='stylesheet' href='shared/style.css'><script src='shared/main.js'></script>",
        encoding="utf-8",
    )
    shared = site_dir / "shared"
    shared.mkdir()
    (shared / "style.css").write_text("body { color: #111; }\n", encoding="utf-8")
    (shared / "main.js").write_text("console.log('ok');\n", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[{
                "name": "index.html 导航链接验证",
                "validation_method": "content_check",
                "ok": True,
                "summary": "包含 login.html、register.html、items.html、flow-a.html 链接",
            }],
            output={"artifacts": [
                {"path": str(site_dir / "index.html")},
                {"path": str(shared / "style.css")},
                {"path": str(shared / "main.js")},
            ]},
            workspace_root=tmp_path,
        )
    )

    assert prepared == [{
        "name": "inferred static site check",
        "validation_method": "static_site_check",
        "site_root": "deliverables/shop/build",
        "required_files": ["index.html"],
        "require_complete_html": True,
        "html_files": ["index.html"],
    }]


def test_prepare_test_items_fills_artifact_integrity_file_path(tmp_path):
    output_path = tmp_path / "deliverables" / "site" / "index.html"
    output_path.parent.mkdir(parents=True)
    output_path.write_text("<html><body></body></html>", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[{"name": "artifact integrity", "validation_method": "artifact_integrity"}],
            output={"artifacts": [{"path": str(output_path)}]},
            workspace_root=tmp_path,
        )
    )

    assert prepared[0]["validation_method"] == "artifact_integrity"
    assert prepared[0]["file_path"] == "deliverables/site/index.html"


def test_prepare_test_items_infers_static_site_check_with_required_dom_ids(tmp_path):
    site_dir = tmp_path / "deliverables" / "shop" / "build"
    site_dir.mkdir(parents=True)
    (site_dir / "index.html").write_text("<main id='catalog'></main>", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[],
            output={"artifacts": [{"path": str(site_dir / "index.html")}]},
            workspace_root=tmp_path,
            required_dom_ids=["register", "login", "catalog"],
        )
    )

    assert prepared == [{
        "name": "inferred static site check",
        "validation_method": "static_site_check",
        "site_root": "deliverables/shop/build",
        "required_files": ["index.html"],
        "require_complete_html": True,
        "required_dom_ids": ["register", "login", "catalog"],
        "html_files": ["index.html"],
    }]


def test_prepare_test_items_merges_task_required_static_files(tmp_path):
    site_dir = tmp_path / "deliverables" / "shop" / "build"
    auth = site_dir / "auth"
    catalog = site_dir / "catalog"
    auth.mkdir(parents=True)
    catalog.mkdir(parents=True)
    (auth / "login.html").write_text("<a href='../catalog/items.html'>products</a>", encoding="utf-8")
    (catalog / "items.html").write_text("<a href='../auth/login.html'>login</a>", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[],
            output={"artifacts": [
                {"path": str(auth / "login.html")},
                {"path": str(catalog / "items.html")},
            ]},
            workspace_root=tmp_path,
            required_files=["index.html", "login.html", "items.html", "flow-a.html", "style.css", "app.js"],
        )
    )

    assert prepared == [{
        "name": "inferred static site check",
        "validation_method": "static_site_check",
        "site_root": "deliverables/shop/build",
        "required_files": [
            "app.js",
            "auth/login.html",
            "catalog/items.html",
            "flow-a.html",
            "index.html",
            "items.html",
            "login.html",
            "style.css",
        ],
        "require_complete_html": True,
    }]


def test_prepare_test_items_keeps_malformed_check_without_machine_fallback(tmp_path):
    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[{"name": "文件结构验证", "validation_method": "content_check"}],
            output={"artifacts": []},
            workspace_root=tmp_path,
        )
    )

    assert prepared == [{"name": "文件结构验证", "validation_method": "content_check"}]


def test_prepare_test_items_does_not_duplicate_static_site_check(tmp_path):
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    (site_dir / "index.html").write_text("<a href='flow-a.html'>cart</a>", encoding="utf-8")
    (site_dir / "flow-a.html").write_text("<a href='index.html'>home</a>", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[{
                "name": "declared static check",
                "validation_method": "static_site_check",
                "site_root": "site",
                "required_files": ["index.html", "flow-a.html"],
            }],
            output={"artifacts": [
                {"path": str(site_dir / "index.html")},
                {"path": str(site_dir / "flow-a.html")},
            ]},
            workspace_root=tmp_path,
        )
    )

    assert [item["validation_method"] for item in prepared] == ["static_site_check"]


def test_prepare_test_items_does_not_convert_static_site_command_alias(tmp_path):
    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[{"name": "static-site-smoke", "command": "static_site_check"}],
            output={"artifacts": []},
            workspace_root=tmp_path,
        )
    )

    assert prepared == [
        {
            "name": "static-site-smoke",
            "command": "static_site_check",
        }
    ]


def test_prepare_test_items_keeps_pytest_method_untouched(tmp_path):
    """pytest/unittest 标签不再归一化为 command——原样保留为 inert evidence。"""
    target_dir = tmp_path / "deliverables" / "leaf"
    target_dir.mkdir(parents=True)
    artifact = target_dir / "test_solution.py"
    artifact.write_text("pass\n", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[{
                "name": "pytest alias",
                "validation_method": "pytest",
                "command": "python3 -m pytest test_solution.py -q",
            }],
            output={"artifacts": [{"path": str(artifact)}]},
            workspace_root=tmp_path,
        )
    )

    assert prepared[0]["validation_method"] == "pytest"
    assert "working_dir" not in prepared[0]


def test_prepare_test_items_keeps_cat_command_as_inert(tmp_path):
    """cat 命令不再转成 content_check——command 原样保留为 inert evidence。"""
    target_dir = tmp_path / "deliverables" / "leaf"
    target_dir.mkdir(parents=True)
    artifact = target_dir / "proof.txt"
    artifact.write_text("context-lineage-ok", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[{
                "name": "worker_proof_content",
                "validation_method": "command",
                "command": f"cat {artifact}",
                "expected_content": "context-lineage-ok",
            }],
            output={"artifacts": [{
                "path": str(artifact),
            }]},
            workspace_root=tmp_path,
        )
    )

    assert prepared[0]["validation_method"] == "command"
    assert prepared[0]["command"] == f"cat {artifact}"
    assert "file_path" not in prepared[0]
    assert "content_equals" not in prepared[0]


def test_prepare_test_items_does_not_parse_summary_for_cat_content(tmp_path):
    """Human summaries must not become exact machine acceptance facts."""
    target_dir = tmp_path / "deliverables" / "leaf"
    target_dir.mkdir(parents=True)
    artifact = target_dir / "proof.txt"
    artifact.write_text("context-lineage-ok", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[{
                "name": "worker_proof_content",
                "validation_method": "command",
                "command": f"cat {artifact}",
                "summary": "需父代理独立验证文件内容是否为context-lineage-ok",
            }],
            output={"artifacts": [{
                "path": str(artifact),
                "summary": "worker 写入的目标产物，内容应为context-lineage-ok",
            }]},
            workspace_root=tmp_path,
        )
    )

    assert prepared[0]["validation_method"] == "command"
    assert "content_equals" not in prepared[0]
