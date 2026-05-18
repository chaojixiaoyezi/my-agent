# LLM: Main-agent real task suite cases stay separate from runner plumbing for code-size safety.
# 模块用途: 定义默认真实任务样例，覆盖网页、表格、资料整理和文档翻译等主代理复杂场景。

from __future__ import annotations

from .main_agent_real_task_suite import MainAgentRealTaskArtifact, MainAgentRealTaskCase


# LLM: default_main_agent_real_task_cases defines broad real E2E tasks as prompt refs plus contracts.
# 函数用途: 返回默认真实任务集合；自然语言只作为任务 prompt，机器事实在 artifact/check 字段里。
def default_main_agent_real_task_cases() -> list[MainAgentRealTaskCase]:
    return [
        _furniture_homepage_case(),
        _shopping_site_case(),
        _github_star_workbook_case(),
        _deepseek_papers_translation_case(),
    ]


# LLM: _furniture_homepage_case covers short creative web generation with static validation.
# 函数用途: 定义家具品牌单文件 HTML 任务及对应的通用网页验收合同。
def _furniture_homepage_case() -> MainAgentRealTaskCase:
    artifact = MainAgentRealTaskArtifact(
        artifact_id="homepage_html",
        kind="html",
        preferred_path="outputs/furniture_homepage/index.html",
        validation_contract={
            "validator": "artifact_acceptance",
            "required_suffix": ".html",
            "forbidden_hrefs": ["", "#", "javascript:void(0)", "javascript:void(0);"],
            "failure_codes": ["HTML_PLACEHOLDER_LINK", "HTML_EXTERNAL_IMAGE_REF"],
            "quality_requirements": {
                "clickable_links_must_resolve": True,
                "complete_html_document": True,
                "images_must_be_local_or_inline": True,
                "min_size_bytes": 4000,
                "single_file_no_external_assets": True,
            },
        },
    )
    return MainAgentRealTaskCase(
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


# LLM: _shopping_site_case covers multi-step web app delivery and no-database constraints.
# 函数用途: 定义购物站点完整流程任务，验收重点是前端流程产物和静态站点机器检查。
def _shopping_site_case() -> MainAgentRealTaskCase:
    artifact = MainAgentRealTaskArtifact(
        artifact_id="shopping_site_root",
        kind="web_project",
        preferred_path="outputs/shopping_site",
        validation_contract={
            "validator": "static_site_check",
            "required_files": ["index.html", "app.js"],
        },
    )
    return MainAgentRealTaskCase(
        case_id="shopping_site_flow",
        title="无数据库购物网站流程",
        user_prompt=(
            "做一个购物网站，不要改变现有环境，不要用数据库。登录、浏览商品、加入购物车、"
            "填写收货信息、到付款前确认订单这些流程都要能走通，按钮不要失灵。"
        ),
        artifacts=(artifact,),
        acceptance_checks=(
            _artifact_check("shopping_site_root_exists", "artifact_exists", "shopping_site_root"),
            _artifact_check(
                "shopping_site_static_check", "static_site_check", "shopping_site_root"
            ),
        ),
    )


# LLM: _github_star_workbook_case covers long research with spreadsheet deliverables.
# 函数用途: 定义 GitHub 升星项目整理任务，验收为 xlsx 产物和结构化字段要求。
def _github_star_workbook_case() -> MainAgentRealTaskCase:
    artifact = MainAgentRealTaskArtifact(
        artifact_id="github_star_growth_workbook",
        kind="xlsx",
        preferred_path="outputs/github_star_growth/github_star_growth.xlsx",
        validation_contract={
            "validator": "spreadsheet_acceptance",
            "required_sheets_min": 2,
            "required_columns": ["项目名", "地址", "上升 star 数", "中文解释", "推荐理由"],
        },
    )
    return MainAgentRealTaskCase(
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


# LLM: _deepseek_papers_translation_case covers research, translation, and formatted document output.
# 函数用途: 定义论文翻译 PDF 任务，验收为 PDF 产物、来源清单和格式检查合同。
def _deepseek_papers_translation_case() -> MainAgentRealTaskCase:
    artifact = MainAgentRealTaskArtifact(
        artifact_id="deepseek_translation_pdf",
        kind="pdf",
        preferred_path="outputs/deepseek_papers/deepseek_papers_zh.pdf",
        validation_contract={
            "validator": "document_acceptance",
            "required_suffix": ".pdf",
            "requires_source_index": True,
        },
    )
    return MainAgentRealTaskCase(
        case_id="deepseek_papers_translation_pdf",
        title="DeepSeek 论文中文翻译 PDF",
        user_prompt=(
            "找到2025年之后 DeepSeek 发布的所有论文并翻译成中文，正文翻译准确，专业术语可以保留英文。"
            "最终成品需要是 PDF，排版要正确、清楚、好看，并附来源清单。"
        ),
        artifacts=(artifact,),
        acceptance_checks=(
            _artifact_check("deepseek_pdf_exists", "artifact_exists", "deepseek_translation_pdf"),
            _artifact_check(
                "deepseek_pdf_format", "document_acceptance", "deepseek_translation_pdf"
            ),
        ),
    )


# LLM: _artifact_check keeps acceptance contracts structured and reusable across task types.
# 函数用途: 生成通用 artifact 验收项，避免在代码里解析任务 prompt 的自然语言。
def _artifact_check(check_id: str, kind: str, artifact_id: str) -> dict[str, object]:
    return {"check_id": check_id, "kind": kind, "artifact_id": artifact_id}


__all__ = ["default_main_agent_real_task_cases"]
