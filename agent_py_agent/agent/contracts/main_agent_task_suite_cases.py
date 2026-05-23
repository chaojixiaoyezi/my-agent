# LLM: Main-agent real task suite cases stay separate from runner plumbing for code-size safety.
# 模块用途: 定义默认真实任务样例，覆盖网页、表格、资料整理和格式化文档等主代理复杂场景。

from __future__ import annotations

from datetime import date

from .main_agent_task_suite import MainAgentTaskArtifact, MainAgentTaskCase
from .main_agent_task_suite_data_analysis import data_analysis_package_case


# LLM: default_main_agent_task_cases defines broad real E2E tasks as prompt refs plus contracts.
# 函数用途: 返回默认真实任务集合；自然语言只作为任务 prompt，机器事实在 artifact/check 字段里。
def default_main_agent_task_cases() -> list[MainAgentTaskCase]:
    return [
        _furniture_homepage_case(),
        _shopping_site_case(),
        _github_star_workbook_case(),
        _research_document_translation_case(),
        data_analysis_package_case(),
    ]


# LLM: _furniture_homepage_case covers short creative web generation with static validation.
# 函数用途: 定义家具品牌单文件 HTML 任务及对应的通用网页验收合同。
def _furniture_homepage_case() -> MainAgentTaskCase:
    artifact = MainAgentTaskArtifact(
        artifact_id="homepage_html",
        kind="html",
        preferred_path="outputs/furniture_homepage/index.html",
        validation_contract={
            "validator": "artifact_acceptance",
            "required_suffix": ".html",
            "quality_requirements": {
                "complete_html_document": True,
                "images_must_be_local_or_inline": True,
                "min_size_bytes": 4000,
                "single_file_no_external_assets": True,
            },
        },
    )
    return MainAgentTaskCase(
        case_id="furniture_homepage_html",
        title="高端家具品牌 HTML 首页",
        user_prompt=(
            "用单文件html做一个高端现代家具品牌的网站首页，风格高级、简洁、有设计感，"
            "适合真实商业品牌使用。只输出完整html，不要注释。"
        ),
        artifacts=(artifact,),
        acceptance_checks=(
            _artifact_check("homepage_html_exists", "artifact_exists", "homepage_html"),
            _artifact_check("homepage_html_validates", "artifact_acceptance", "homepage_html"),
        ),
    )


# LLM: _shopping_site_case covers multi-step static web app delivery and no-database constraints.
# 函数用途: 定义静态交易流程站点任务，验收重点是前端流程产物和静态站点机器检查。
def _shopping_site_case() -> MainAgentTaskCase:
    artifact = MainAgentTaskArtifact(
        artifact_id="shopping_site_root",
        kind="web_project",
        preferred_path="outputs/shopping_site",
        validation_contract={
            "validator": "static_site_check",
            "required_files": ["index.html", "app.js"],
            "require_complete_html": True,
        },
    )
    return MainAgentTaskCase(
        case_id="shopping_site_flow",
        title="无数据库静态交易流程站点",
        user_prompt=(
            "做一个静态交易流程站点，不要改变现有环境，不要用数据库。账号入口、条目浏览、"
            "选择条目、填写联系信息、到最终确认前这些流程都要能走通，按钮不要失灵。"
        ),
        artifacts=(artifact,),
        acceptance_checks=(
            _artifact_check("shopping_site_root_exists", "artifact_exists", "shopping_site_root"),
            _artifact_check(
                "shopping_site_static_check", "static_site_check", "shopping_site_root"
            ),
        ),
    )


# LLM: _github_star_workbook_case covers long repository-metrics research with spreadsheet deliverables.
# 函数用途: 定义仓库增长指标整理任务，验收为 xlsx 产物和结构化字段要求。
def _github_star_workbook_case() -> MainAgentTaskCase:
    artifact = _github_star_workbook_artifact()
    return MainAgentTaskCase(
        case_id="github_weekly_star_growth_xlsx",
        title="GitHub 周升星项目 XLSX",
        user_prompt=(
            "整理本年度从1月1日到今天为止，GitHub 上每周 star 增长最快的10个项目，"
            "分别记录到 xlsx，再汇总到同一个大 xlsx 的子表中。需要项目基础信息、地址、"
            "上升 star 数量、中文解释、推荐理由、技术栈、生态和应用方向，不能糊弄。"
        ),
        artifacts=(artifact,),
        acceptance_checks=(
            _artifact_check(
                "github_workbook_exists",
                "artifact_exists",
                "github_star_growth_workbook",
            ),
            _artifact_check(
                "github_workbook_columns",
                "spreadsheet_schema",
                "github_star_growth_workbook",
            ),
        ),
    )


# LLM: _github_star_workbook_artifact defines the spreadsheet artifact and validation contract.
# 函数用途: 将 GitHub workbook 产物合同从 case 构造中拆出，保持任务 case 薄而清楚。
def _github_star_workbook_artifact() -> MainAgentTaskArtifact:
    return MainAgentTaskArtifact(
        artifact_id="github_star_growth_workbook",
        kind="xlsx",
        preferred_path="outputs/github_star_growth/github_star_growth.xlsx",
        validation_contract=_github_star_workbook_validation_contract(),
    )


# LLM: _github_star_workbook_validation_contract keeps workbook requirements machine-readable.
# 函数用途: 定义 workbook 的 sheet/列/evidence/staging 合同，不从任务 prompt 推断验收事实。
def _github_star_workbook_validation_contract() -> dict[str, object]:
    return {
        "validator": "spreadsheet_acceptance",
        "required_sheets_min": 2,
        "required_columns": ["项目名", "地址", "上升 star 数", "中文解释", "推荐理由"],
        "evidence_contract": {
            "required_fields": ["项目名", "地址", "上升 star 数"],
            "require_verified": True,
        },
        "metric_contracts": [
            {
                "field": "上升 star 数",
                "expected_kind": "time_window_delta",
                "required_window": True,
                "allow_estimated": False,
            }
        ],
        "collection_contract": _github_star_collection_contract(),
        "staging_contract": _github_star_workbook_staging_contract(),
    }


# LLM: _github_star_workbook_staging_contract describes the source-data to workbook build path.
# 函数用途: 固化 source JSON、builder tool、workbook ref 和 checkpoint 结构提示。
def _github_star_workbook_staging_contract() -> dict[str, object]:
    return {
        "strategy": "data_then_tool_builder_then_workbook",
        "builder_tool": "data_to_workbook",
        "source_json_ref": "outputs/github_star_growth/source_data.json",
        "workbook_ref": "outputs/github_star_growth/github_star_growth.xlsx",
        "checkpoint_shape_hints": {
            "outputs/github_star_growth/source_data.json": (
                '{"completion_evidence":{"scope":"year_to_date","retrieved_at":"...","method":"..."},'
                '"sheets":[{"name":"YYYY-WW","columns":["项目名","地址","上升 star 数","中文解释","推荐理由"],'
                '"rows":[{"项目名":"...","地址":"...","上升 star 数":"...",'
                '"field_source_ids":{"项目名":["src-id"],"地址":["src-id"],"上升 star 数":["src-id"]}}]}],'
                '"source_refs":[{"source_id":"src-id","uri":"https://...","retrieved_at":"...",'
                '"reserved":{"metric_kind":"time_window_delta","window_start":"YYYY-MM-DD","window_end":"YYYY-MM-DD"}}],'
                '"claims":[{"field":"上升 star 数","source_ids":["src-id"],"verification_status":"VERIFIED",'
                '"reserved":{"metric_kind":"time_window_delta","window_start":"YYYY-MM-DD","window_end":"YYYY-MM-DD"}}]}'
            )
        },
        "checkpoint_refs": [
            "outputs/github_star_growth/source_data.json",
            "outputs/github_star_growth/github_star_growth.xlsx",
        ],
    }


# LLM: _github_star_collection_contract encodes year-to-date weekly coverage as machine fields.
# 函数用途: 计算本年度到今天的最小周分组数量，并要求每个分组至少 10 条结构化记录。
def _github_star_collection_contract() -> dict[str, object]:
    return {
        "source_json_ref": "outputs/github_star_growth/source_data.json",
        "groups_path": "sheets",
        "items_path": "rows",
        "min_groups": _year_to_date_week_count(),
        "min_items_per_group": 10,
        "required_item_fields": ["项目名", "地址", "上升 star 数", "中文解释", "推荐理由"],
        "require_completion_evidence": True,
        "completion_evidence_path": "completion_evidence",
        "api_request": _github_star_api_request(),
    }


# LLM: GitHub collection uses a structured API request plan instead of relying on prompt prose.
# 函数用途: 给 api_json_collection 提供可执行的周窗口、字段映射和来源证据字段。
def _github_star_api_request(today: date | None = None) -> dict[str, object]:
    return {
        "request_ranges": _github_star_request_ranges(today),
        "item_path": "items",
        "limit_per_request": 10,
        "fields": {
            "项目名": "full_name",
            "地址": "html_url",
            "上升 star 数": "stargazers_count",
            "中文解释": {
                "path": "description",
                "default_template": "{full_name}：主要语言 {language}，主题 {topics}",
            },
            "推荐理由": {
                "template": "窗口内 stars={stargazers_count}；forks={forks_count}；语言={language}；主题={topics}",
            },
        },
        "evidence_fields": ["项目名", "地址", "上升 star 数"],
        "completion_evidence": {"scope": "year_to_date_weekly_collection", "method": "github_search_api"},
    }


# LLM: _github_star_request_ranges returns one bounded weekly API range spec.
# 函数用途: 将年度到今天的周窗口变成结构化 request_ranges，api_json_collection 会展开成多个 requests。
def _github_star_request_ranges(today: date | None = None) -> list[dict[str, object]]:
    current = today or date.today()
    year_start = date(current.year, 1, 1)
    return [
        {
            "start_date": year_start.isoformat(),
            "end_date": current.isoformat(),
            "step_days": 7,
            "name_template": "{yyyy}-W{week}",
            "reserved": {
                "metric_kind": "time_window_delta",
                "time_window": {"end": "{end_date}", "start": "{start_date}"},
                "window_end": "{end_date}",
                "window_start": "{start_date}",
            },
            "source_id_template": "github-week-{yyyy}-{week}",
            "url_template": (
                "https://api.github.com/search/repositories"
                "?q=created:{start}..{end}&sort=stars&order=desc&per_page=10"
            ),
        }
    ]


# LLM: _research_document_translation_case covers research, translation, and formatted document output.
# 函数用途: 定义通用研究文档翻译任务，验收为 PDF 产物、来源清单和格式检查合同。
def _research_document_translation_case() -> MainAgentTaskCase:
    artifact = MainAgentTaskArtifact(
        artifact_id="research_translation_pdf",
        kind="pdf",
        preferred_path="outputs/research_documents/research_documents_zh.pdf",
        validation_contract=_research_document_validation_contract(),
    )
    return MainAgentTaskCase(
        case_id="research_documents_translation_pdf",
        title="研究文档中文翻译 PDF",
        user_prompt=(
            "找到指定开源大模型项目在 2025 年之后公开发布的所有论文或研究文档，逐篇翻译成中文，"
            "正文翻译准确，专业术语可以保留英文。最终成品需要是 PDF，排版要正确、清楚、好看，并附来源清单。"
        ),
        artifacts=(artifact,),
        acceptance_checks=(
            _artifact_check("research_pdf_exists", "artifact_exists", "research_translation_pdf"),
            _artifact_check(
                "research_pdf_format", "document_acceptance", "research_translation_pdf"
            ),
        ),
    )


# LLM: _artifact_check keeps acceptance contracts structured and reusable across task types.
# 函数用途: 生成通用 artifact 验收项，避免在代码里解析任务 prompt 的自然语言。
def _artifact_check(check_id: str, kind: str, artifact_id: str) -> dict[str, object]:
    return {"check_id": check_id, "kind": kind, "artifact_id": artifact_id}


# LLM: _research_document_validation_contract groups PDF, source-index, and evidence requirements.
# 函数用途: 将研究 PDF 产物的验收合同从 case 构造拆出，避免任务函数膨胀。
def _research_document_validation_contract() -> dict[str, object]:
    return {
        "validator": "document_acceptance",
        "required_suffix": ".pdf",
        "requires_source_index": True,
        "evidence_contract": {"required_fields": ["title", "url", "date"], "require_verified": True},
        "collection_contract": _research_document_collection_contract(),
        "staging_contract": _research_document_staging_contract(),
    }


def _research_document_staging_contract() -> dict[str, object]:
    return {
        "strategy": "source_index_then_translation_draft_then_pdf",
        "builder_tool": "markdown_to_pdf",
        "source_markdown_ref": "outputs/research_documents/research_documents_zh.md",
        "pdf_ref": "outputs/research_documents/research_documents_zh.pdf",
        "checkpoint_shape_hints": {"outputs/research_documents/source_index.json": _research_source_index_shape_hint()},
        "checkpoint_refs": [
            "outputs/research_documents/source_index.json",
            "outputs/research_documents/research_documents_zh.md",
            "outputs/research_documents/research_documents_zh.pdf",
        ],
    }


def _research_source_index_shape_hint() -> str:
    return (
        '{"completion_evidence":{"scope":"all_public_documents_after_2025","method":"...","retrieved_at":"..."},'
        '"rows":[{"title":"...","authors":["..."],"date":"...","url":"...","abstract":"...","translated":true,'
        '"field_source_ids":{"title":["src-id"],"url":["src-id"],"date":["src-id"]}}],'
        '"source_refs":[{"source_id":"src-id","uri":"https://...","reserved":{"tool_call_id":"..."}}],'
        '"claims":[{"field":"title","source_ids":["src-id"],"value":"...","verification_status":"VERIFIED",'
        '"reserved":{"item_index":0}}]}'
    )


# LLM: _research_document_collection_contract keeps source-index completeness generic.
# 函数用途: 要求 source index 有多条记录、完整性证据，并能映射到 Markdown 翻译稿。
def _research_document_collection_contract() -> dict[str, object]:
    return {
        "source_json_ref": "outputs/research_documents/source_index.json",
        "items_path": "rows",
        "min_items_total": 3,
        "required_item_fields": ["title", "url", "date", "translated"],
        "required_item_values": {"translated": True},
        "item_date_bounds": {"field": "date", "min": "2025-01-01"},
        "require_completion_evidence": True,
        "completion_evidence_path": "completion_evidence",
        "require_item_evidence": True,
        "required_item_evidence_fields": ["title", "url", "date"],
        "mapping": {
            "artifact_ref": "outputs/research_documents/research_documents_zh.md",
            "key_fields": ["title"],
            "min_mapped_items": 3,
        },
    }


# LLM: _year_to_date_week_count 是 agent_py_agent/agent/contracts/main_agent_task_suite_cases.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 year to date week count 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _year_to_date_week_count(today: date | None = None) -> int:
    current = today or date.today()
    year_start = date(current.year, 1, 1)
    return ((current - year_start).days // 7) + 1


__all__ = ["default_main_agent_task_cases"]
