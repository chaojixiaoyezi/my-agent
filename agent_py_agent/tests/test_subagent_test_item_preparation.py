"""测试子代理验收测试项目录推断。"""

from agent_py_agent.agent.subagents.execution_test_items import (
    TestItemPreparationRequest,
    prepare_test_items,
)
from agent_py_agent.agent.subagents.static_required_files import static_required_files_from_texts


def test_prepare_test_items_infers_single_artifact_working_dir(tmp_path):
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

    assert prepared[0]["working_dir"] == "strings"


def test_prepare_test_items_keeps_explicit_working_dir(tmp_path):
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


def test_prepare_test_items_ignores_out_of_workspace_artifacts(tmp_path):
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


def test_prepare_test_items_keeps_workspace_cwd_when_command_names_relative_artifact(tmp_path):
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

    assert prepared[0]["working_dir"] == "."


def test_prepare_test_items_recovers_nested_relative_artifact_command_cwd(tmp_path):
    target_dir = tmp_path / "run-1" / "grandchild_sorting_edge"
    target_dir.mkdir(parents=True)
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

    assert prepared[0]["working_dir"] == "run-1"


def test_prepare_test_items_converts_safe_cd_chain_into_working_dir(tmp_path):
    """LLM: Verifies model-style cd && pytest commands become bounded cwd plus plain command."""
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

    assert prepared[0]["command"] == "python3 -m pytest test_solution.py -v"
    assert prepared[0]["working_dir"] == "deliverables/leaf"


def test_prepare_test_items_strips_safe_cd_chain_even_with_working_dir(tmp_path):
    """LLM: Verifies explicit working_dir does not leave a redundant shell cd chain in command."""
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
                "working_dir": str(target_dir),
            }],
            output={"artifacts": [{"path": str(artifact)}]},
            workspace_root=tmp_path,
        )
    )

    assert prepared[0]["command"] == "python3 -m pytest test_solution.py -v"
    assert prepared[0]["working_dir"] == str(target_dir)


def test_prepare_test_items_infers_pytest_when_runner_only_reports_test_artifact(tmp_path):
    """LLM: Verifies missing tests can still execute workspace-local test_*.py artifacts."""
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

    assert prepared == [{
        "name": "artifact pytest test_solution.py",
        "validation_method": "command",
        "command": "python3 -m pytest test_solution.py -q",
        "working_dir": "deliverables/leaf",
    }]


# LLM: test_prepare_test_items_infers_static_site_check covers auto validation for generated HTML sites.
# 函数用途: runner 只报告多个 HTML 产物时，父级验收会自动补 static_site_check。
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


# LLM: test_prepare_test_items_drops_malformed_runner_checklist_when_static_check_is_available covers R13.
# 函数用途: 模型把清单写成 content_check 但缺 file_path/pattern 时，自动站点验收接管，避免误报失败。
def test_prepare_test_items_drops_malformed_runner_checklist_when_static_check_is_available(tmp_path):
    site_dir = tmp_path / "deliverables" / "shop" / "build"
    site_dir.mkdir(parents=True)
    (site_dir / "index.html").write_text("<link rel='stylesheet' href='style.css'>", encoding="utf-8")
    (site_dir / "cart.html").write_text("<script src='app.js'></script>", encoding="utf-8")
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
                {"path": str(site_dir / "cart.html")},
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
        "required_files": ["cart.html", "index.html"],
        "require_complete_html": True,
    }]


# LLM: test_prepare_test_items_infers_static_site_check_for_single_html_artifact covers single-page leaf outputs.
# 函数用途: 只有一个 HTML 页面和 CSS/JS 资源时也要生成静态站点验收，避免模型空壳测试误报。
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
                "summary": "包含 login.html、register.html、products.html、cart.html 链接",
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


# LLM: test_prepare_test_items_merges_task_required_static_files covers root whole-site required files.
# 函数用途: 分支目录已有 HTML 时，也要把任务明确要求的顶层购物站文件放进 static_site_check。
def test_prepare_test_items_merges_task_required_static_files(tmp_path):
    site_dir = tmp_path / "deliverables" / "shop" / "build"
    auth = site_dir / "auth"
    catalog = site_dir / "catalog"
    auth.mkdir(parents=True)
    catalog.mkdir(parents=True)
    (auth / "login.html").write_text("<a href='../catalog/products.html'>products</a>", encoding="utf-8")
    (catalog / "products.html").write_text("<a href='../auth/login.html'>login</a>", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[],
            output={"artifacts": [
                {"path": str(auth / "login.html")},
                {"path": str(catalog / "products.html")},
            ]},
            workspace_root=tmp_path,
            required_files=["index.html", "login.html", "products.html", "cart.html", "style.css", "app.js"],
        )
    )

    assert prepared == [{
        "name": "inferred static site check",
        "validation_method": "static_site_check",
        "site_root": "deliverables/shop/build",
        "required_files": [
            "app.js",
            "auth/login.html",
            "cart.html",
            "catalog/products.html",
            "index.html",
            "login.html",
            "products.html",
            "style.css",
        ],
        "require_complete_html": True,
    }]


# LLM: test_static_required_files_from_texts_extracts_static_web_targets validates task-text oracle input.
# 函数用途: 从用户目标和验收条件中抽取静态文件名，供父级验收补全 whole-site 必需文件。
def test_static_required_files_from_texts_extracts_static_web_targets():
    files = static_required_files_from_texts([
        "必须生成 index.html, products.html, product-detail.html, cart.html, checkout.html。",
        "还要有 style.css 和 app.js；不要把 /Users/xiaoyezi/project/docs.md 当静态站文件。",
        "不允许把 product-detail.html 改名成 product.html 或 old-product.html，也不要创建 legacy.html、old-detail.html。",
        "不得改名为 old-detail.html。",
    ])

    assert files == [
        "index.html",
        "products.html",
        "product-detail.html",
        "cart.html",
        "checkout.html",
        "style.css",
        "app.js",
    ]


# LLM: test_prepare_test_items_keeps_malformed_check_without_machine_fallback preserves conservative failure signals.
# 函数用途: 没有自动机器验收兜底时，格式不完整的测试项仍保留，让父级看到 runner 输出不合格。
def test_prepare_test_items_keeps_malformed_check_without_machine_fallback(tmp_path):
    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[{"name": "文件结构验证", "validation_method": "content_check"}],
            output={"artifacts": []},
            workspace_root=tmp_path,
        )
    )

    assert prepared == [{"name": "文件结构验证", "validation_method": "content_check"}]


# LLM: test_prepare_test_items_does_not_duplicate_static_site_check preserves runner-declared tests.
# 函数用途: 模型已显式给 static_site_check 时，预处理只保留原测试并补安全字段，不重复追加。
def test_prepare_test_items_does_not_duplicate_static_site_check(tmp_path):
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    (site_dir / "index.html").write_text("<a href='cart.html'>cart</a>", encoding="utf-8")
    (site_dir / "cart.html").write_text("<a href='index.html'>home</a>", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[{
                "name": "declared static check",
                "validation_method": "static_site_check",
                "site_root": "site",
                "required_files": ["index.html", "cart.html"],
            }],
            output={"artifacts": [
                {"path": str(site_dir / "index.html")},
                {"path": str(site_dir / "cart.html")},
            ]},
            workspace_root=tmp_path,
        )
    )

    assert [item["validation_method"] for item in prepared] == ["static_site_check"]


def test_prepare_test_items_normalizes_pytest_method_with_command(tmp_path):
    """LLM: Verifies runner `validation_method=pytest` remains executable by TestExecutor."""
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

    assert prepared[0]["validation_method"] == "command"
    assert prepared[0]["working_dir"] == "deliverables/leaf"


def test_prepare_test_items_converts_cat_content_assertion_to_content_check(tmp_path):
    """LLM: Verifies model-style cat checks become bounded exact content checks."""
    target_dir = tmp_path / "deliverables" / "leaf"
    target_dir.mkdir(parents=True)
    artifact = target_dir / "proof.txt"
    artifact.write_text("context-lineage-ok", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[{
                "name": "leaf_worker_proof_content",
                "validation_method": "command",
                "command": f"cat {artifact}",
                "summary": "需父代理独立验证文件内容是否为context-lineage-ok",
            }],
            output={"artifacts": [{
                "path": str(artifact),
                "summary": "leaf_worker 写入的目标产物，内容应为context-lineage-ok",
            }]},
            workspace_root=tmp_path,
        )
    )

    assert prepared[0]["validation_method"] == "content_check"
    assert prepared[0]["file_path"] == "deliverables/leaf/proof.txt"
    assert prepared[0]["content_equals"] == "context-lineage-ok"
    assert prepared[0]["match_mode"] == "exact"
