from __future__ import annotations

import json
from types import SimpleNamespace

import openpyxl

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core._tool_loop_service import ToolLoopService
from agent_py_agent.agent.contracts.delivery_contract import (
    delivery_repair_prompt,
    repair_delivery_contract,
    run_delivery_contract,
)


# LLM: Delivery contract checks are explicit JSON contracts, not prompt-derived expectations.
# 函数用途: 验证主代理产物验收只看结构化 static_site_check 字段。
def test_delivery_contract_static_site_check_rejects_missing_script_ref(tmp_path):
    site = tmp_path / "lab_outputs" / "site"
    site.mkdir(parents=True)
    (site / "index.html").write_text(
        "<!doctype html><html><head><link rel=\"stylesheet\" href=\"styles.css\"></head>"
        "<body><section id=\"hero\">家具</section></body></html>",
        encoding="utf-8",
    )
    (site / "styles.css").write_text("body { color: #111; }\n", encoding="utf-8")
    (site / "app.js").write_text("document.body.dataset.ready = '1';\n", encoding="utf-8")
    contract = tmp_path / "delivery_contract.json"
    contract.write_text(
        json.dumps(
            {
                "checks": [
                    {
                        "name": "site",
                        "method": "static_site_check",
                        "site_root": "lab_outputs/site",
                        "required_files": ["index.html", "styles.css", "app.js"],
                        "require_script": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = run_delivery_contract(contract, workspace_root=tmp_path)

    assert report.ok is False
    payload = report.to_dict()
    assert payload["checks"][0]["method"] == "static_site_check"
    assert "missing_script" in json.dumps(payload, ensure_ascii=False)
    assert "DELIVERY_CONTRACT_REPORT" in delivery_repair_prompt(report)


def test_delivery_contract_static_site_check_accepts_complete_site(tmp_path):
    site = tmp_path / "lab_outputs" / "site"
    site.mkdir(parents=True)
    (site / "index.html").write_text(
        "<!doctype html><html><head><link rel=\"stylesheet\" href=\"styles.css\"></head>"
        "<body><section id=\"hero\">家具</section><script src=\"app.js\"></script></body></html>",
        encoding="utf-8",
    )
    (site / "styles.css").write_text("body { color: #111; }\n", encoding="utf-8")
    (site / "app.js").write_text("document.body.dataset.ready = '1';\n", encoding="utf-8")
    contract = tmp_path / "delivery_contract.json"
    contract.write_text(
        json.dumps(
            {
                "checks": [
                    {
                        "name": "site",
                        "method": "static_site_check",
                        "site_root": "lab_outputs/site",
                        "required_files": ["index.html", "styles.css", "app.js"],
                        "require_script": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = run_delivery_contract(contract, workspace_root=tmp_path)

    assert report.ok is True


def test_delivery_contract_accepts_relative_workspace_root(tmp_path, monkeypatch):
    site = tmp_path / "lab_outputs" / "site"
    site.mkdir(parents=True)
    (site / "index.html").write_text(
        "<!doctype html><html><head><link rel=\"stylesheet\" href=\"styles.css\"></head>"
        "<body><section id=\"hero\">家具</section><script src=\"app.js\"></script></body></html>",
        encoding="utf-8",
    )
    (site / "styles.css").write_text("body { color: #111; }\n", encoding="utf-8")
    (site / "app.js").write_text("document.body.dataset.ready = '1';\n", encoding="utf-8")
    contract = tmp_path / "delivery_contract.json"
    contract.write_text(
        json.dumps(
            {
                "checks": [
                    {
                        "name": "site",
                        "method": "static_site_check",
                        "site_root": "lab_outputs/site",
                        "required_files": ["index.html", "styles.css", "app.js"],
                        "require_script": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path.parent)

    report = run_delivery_contract(contract, workspace_root=tmp_path.name)

    assert report.ok is True


# LLM: xlsx_table contracts catch spreadsheet rows that model self-checks miss.
# 函数用途: 验证 xlsx 表格合同能检查列、行数、数字列和 GitHub URL 格式。
def test_delivery_contract_xlsx_table_rejects_bad_rows(tmp_path):
    report_dir = tmp_path / "lab_outputs" / "github-stars"
    report_dir.mkdir(parents=True)
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["rank", "repo_url", "stars_added"])
    sheet.append([1, "https://github.com/openai/openai-python", 100])
    sheet.append([2, "https://example.com/not-github", "many"])
    workbook.save(report_dir / "weekly_top20.xlsx")
    contract = tmp_path / "delivery_contract.json"
    contract.write_text(
        json.dumps(
            {
                "checks": [
                    {
                        "name": "github-stars",
                        "method": "xlsx_table_check",
                        "path": "lab_outputs/github-stars/weekly_top20.xlsx",
                        "required_columns": ["rank", "repo_url", "stars_added"],
                        "min_rows": 3,
                        "numeric_columns": ["stars_added"],
                        "url_columns": ["repo_url"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = run_delivery_contract(contract, workspace_root=tmp_path)

    assert report.ok is False
    payload = json.dumps(report.to_dict(), ensure_ascii=False)
    assert "XLSX_TOO_FEW_ROWS" in payload
    assert "XLSX_NON_NUMERIC" in payload
    assert "XLSX_BAD_GITHUB_URL" in payload


def test_delivery_contract_xlsx_table_rejects_empty_evidence_and_old_dates(tmp_path):
    report_dir = tmp_path / "lab_outputs" / "github-stars"
    report_dir.mkdir(parents=True)
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["repo_url", "created_at", "evidence", "stars_added"])
    sheet.append(["https://github.com/openai/openai-python", "2024-01-01T00:00:00Z", "", 100])
    workbook.save(report_dir / "weekly_top20.xlsx")
    contract = tmp_path / "delivery_contract.json"
    contract.write_text(
        json.dumps(
            {
                "checks": [
                    {
                        "name": "github-stars",
                        "method": "xlsx_table_check",
                        "path": "lab_outputs/github-stars/weekly_top20.xlsx",
                        "required_columns": ["repo_url", "created_at", "evidence", "stars_added"],
                        "min_rows": 1,
                        "numeric_columns": ["stars_added"],
                        "nonempty_columns": ["evidence"],
                        "min_date_columns": {"created_at": "2026-01-01"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = run_delivery_contract(contract, workspace_root=tmp_path)

    assert report.ok is False
    payload = json.dumps(report.to_dict(), ensure_ascii=False)
    assert "XLSX_EMPTY_CELL" in payload
    assert "XLSX_DATE_BEFORE_MIN" in payload


def test_delivery_contract_auto_repairs_static_site_missing_assets(tmp_path):
    site = tmp_path / "lab_outputs" / "site"
    site.mkdir(parents=True)
    (site / "index.html").write_text(
        "<!doctype html><html><head><title>Site</title></head>"
        "<body><main id=\"hero\"><button>菜单</button></main><script src=\"app.js\"></script></body></html>",
        encoding="utf-8",
    )
    contract = tmp_path / "delivery_contract.json"
    contract.write_text(
        json.dumps(
            {
                "checks": [
                    {
                        "name": "site",
                        "method": "static_site_check",
                        "site_root": "lab_outputs/site",
                        "required_files": ["index.html", "styles.css", "app.js", "README.md"],
                        "require_complete_html": True,
                        "strict_dom_bindings": True,
                        "auto_repair": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = repair_delivery_contract(contract, workspace_root=tmp_path)

    assert report.ok is True
    assert (site / "styles.css").exists()
    assert (site / "app.js").exists()
    assert (site / "README.md").exists()


def test_delivery_contract_auto_repair_does_not_create_primary_html(tmp_path):
    site = tmp_path / "lab_outputs" / "site"
    site.mkdir(parents=True)
    contract = tmp_path / "delivery_contract.json"
    contract.write_text(
        json.dumps(
            {
                "checks": [
                    {
                        "name": "site",
                        "method": "static_site_check",
                        "site_root": "lab_outputs/site",
                        "required_files": ["index.html", "styles.css", "app.js", "README.md"],
                        "auto_repair": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = repair_delivery_contract(contract, workspace_root=tmp_path)

    assert report.ok is False
    assert not (site / "index.html").exists()


def test_delivery_contract_can_regenerate_primary_static_html_when_explicit(tmp_path):
    site = tmp_path / "lab_outputs" / "site"
    site.mkdir(parents=True)
    (site / "index.html").write_text(
        "<!doctype html><html><head><title>Broken</title></head><body>"
        "<section id=\"register\"></section><script>function showPage(){",
        encoding="utf-8",
    )
    (site / "README.md").write_text("# Site\n", encoding="utf-8")
    contract = tmp_path / "delivery_contract.json"
    contract.write_text(
        json.dumps(
            {
                "checks": [
                    {
                        "name": "site",
                        "method": "static_site_check",
                        "site_root": "lab_outputs/site",
                        "required_files": ["index.html", "README.md"],
                        "required_dom_ids": ["register", "login", "catalog", "cart", "checkout", "order-confirmation"],
                        "require_script": True,
                        "require_complete_html": True,
                        "strict_dom_bindings": True,
                        "auto_repair": True,
                        "allow_primary_regenerate": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = repair_delivery_contract(contract, workspace_root=tmp_path)

    assert report.ok is True
    html = (site / "index.html").read_text(encoding="utf-8")
    assert "function showSection" in html
    assert 'id="order-confirmation"' in html


def test_delivery_contract_auto_repair_removes_stale_readme_anchor_refs(tmp_path):
    site = tmp_path / "lab_outputs" / "site"
    site.mkdir(parents=True)
    (site / "index.html").write_text(
        "<!doctype html><html><head><title>Site</title><link rel=\"stylesheet\" href=\"styles.css\"></head>"
        "<body><main id=\"hero\"></main><section id=\"products\"></section><script src=\"app.js\"></script></body></html>",
        encoding="utf-8",
    )
    (site / "styles.css").write_text("body { color: #111; }\n", encoding="utf-8")
    (site / "app.js").write_text("document.addEventListener('DOMContentLoaded', function(){});\n", encoding="utf-8")
    (site / "README.md").write_text(
        "| `#hero` | ok |\n| `#collection` | stale |\n1. links point to `#id`\n",
        encoding="utf-8",
    )
    contract = tmp_path / "delivery_contract.json"
    contract.write_text(
        json.dumps(
            {
                "checks": [
                    {
                        "name": "site",
                        "method": "static_site_check",
                        "site_root": "lab_outputs/site",
                        "required_files": ["index.html", "styles.css", "app.js", "README.md"],
                        "auto_repair": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = repair_delivery_contract(contract, workspace_root=tmp_path)

    assert report.ok is True
    readme = (site / "README.md").read_text(encoding="utf-8")
    assert "#collection" not in readme
    assert "#hero" in readme
    assert "#id" in readme


# LLM: Tool loop delivery contracts should close out by machine report before another model turn.
# 函数用途: 验证合同通过后工具循环能确定性停止，避免主代理继续长输出。
def test_tool_loop_delivery_contract_returns_deterministic_closeout(tmp_path):
    site = tmp_path / "lab_outputs" / "site"
    site.mkdir(parents=True)
    (site / "index.html").write_text(
        "<!doctype html><html><head><link rel=\"stylesheet\" href=\"styles.css\"></head>"
        "<body><section id=\"hero\">家具</section><script src=\"app.js\"></script></body></html>",
        encoding="utf-8",
    )
    (site / "styles.css").write_text("body { color: #111; }\n", encoding="utf-8")
    (site / "app.js").write_text("document.body.dataset.ready = '1';\n", encoding="utf-8")
    contract = tmp_path / "delivery_contract.json"
    contract.write_text(
        json.dumps({
            "checks": [{
                "name": "site",
                "method": "static_site_check",
                "site_root": "lab_outputs/site",
                "required_files": ["index.html", "styles.css", "app.js"],
                "require_script": True,
            }]
        }),
        encoding="utf-8",
    )
    attrs = {"delivery_contract_file": str(contract)}
    params = _tool_loop_params(attrs)
    agent = SimpleNamespace(workspace_root=tmp_path, backend=SimpleNamespace(name="test"))

    response = ToolLoopService(agent)._delivery_contract_response(params)

    assert response is not None
    assert response.text.startswith("delivery_contract=pass")
    assert params.tool_context


def test_tool_loop_delivery_contract_blocks_final_without_artifact(tmp_path):
    contract = tmp_path / "delivery_contract.json"
    contract.write_text(
        json.dumps({
            "checks": [{
                "name": "report",
                "method": "artifact",
                "path": "lab_outputs/report.md",
            }]
        }),
        encoding="utf-8",
    )
    attrs = {"delivery_contract_file": str(contract)}
    params = _tool_loop_params(attrs)
    agent = SimpleNamespace(workspace_root=tmp_path, backend=SimpleNamespace(name="test"))
    service = ToolLoopService(agent)

    first = service._delivery_contract_break_response(params, SimpleNamespace(text="done", backend="test"))
    second = service._delivery_contract_break_response(params, SimpleNamespace(text="done", backend="test"))

    assert first is None
    assert params.tool_context
    assert second.text.startswith("delivery_contract=fail")


def _tool_loop_params(attrs: dict[str, object]) -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt="",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        granted_capabilities=None,
        write_boundary=None,
        task_attributes=attrs,
        request_id="req",
        run_id="run",
        task_id="task",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
    )
