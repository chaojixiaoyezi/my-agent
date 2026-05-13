# LLM: Tool catalog rendering stays outside ToolRegistry so registry orchestration remains small.
# 模块用途: 根据工具目录配置渲染 Tool Catalog 的分页、过滤和紧凑/完整展示。

from __future__ import annotations

from dataclasses import dataclass

from .models import ToolSpec


# LLM: CatalogRenderConfig is the bundle for tool-catalog prompt shaping knobs.
# 类用途: 保存工具目录渲染时需要的分页、类别过滤和字符上限配置。
@dataclass(frozen=True)
class CatalogRenderConfig:
    mode: str
    offset: int
    limit: int
    categories: list[str]
    include_examples: bool
    entry_max_chars: int
    show_truncated_notice: bool
    detail_max_chars: int


# LLM: render_catalog_entries is the single entry for Tool Catalog prompt rendering.
# 函数用途: 按工具目录配置过滤、分页并渲染工具条目。
def render_catalog_entries(specs: list[ToolSpec], config: CatalogRenderConfig) -> list[str]:
    filtered = _filter_catalog_specs(specs, config.categories)
    if config.mode == "off":
        return ["- disabled：tool_catalog_mode=off，当前 prompt 不注入工具目录。"]
    if config.mode == "retrieval_only":
        return ["- retrieval_only：工具目录精简隐藏，请依赖 Recommended Tools 或显式工具名调用。"]
    page = filtered[config.offset : config.offset + max(0, config.limit)]
    entries = [_render_catalog_spec(spec, config) for spec in page]
    if config.show_truncated_notice:
        notice = _catalog_page_notice(config, total=len(filtered), returned=len(page))
        if notice:
            entries.append(notice)
    return entries


# LLM: _filter_catalog_specs makes catalog categories a prompt budget knob, not an auth rule.
# 函数用途: 只过滤工具目录展示类别，不改变真正可调用工具集合。
def _filter_catalog_specs(specs: list[ToolSpec], categories: list[str]) -> list[ToolSpec]:
    if not categories:
        return specs
    allowed_categories = set(categories)
    return [spec for spec in specs if spec.category in allowed_categories]


# LLM: _render_catalog_spec applies compact/full mode without leaking config handling to ToolSpec.
# 函数用途: 根据目录模式渲染单个工具条目。
def _render_catalog_spec(spec: ToolSpec, config: CatalogRenderConfig) -> str:
    if config.mode == "full":
        return spec.render_detail_entry(max_chars=config.detail_max_chars)
    return spec.render_catalog_entry(
        include_examples=config.include_examples,
        max_chars=config.entry_max_chars,
    )


# LLM: _catalog_page_notice gives models a copyable continuation offset.
# 函数用途: 在工具目录分页或过滤后追加 next_offset 提示。
def _catalog_page_notice(config: CatalogRenderConfig, *, total: int, returned: int) -> str:
    next_offset = config.offset + returned
    if total <= next_offset:
        return ""
    return (
        f"- more_tools：工具目录已分页，next_offset={next_offset} limit={config.limit} total={total}。"
        " 如需更多工具，请调大 tool_catalog_limit 或 tool_catalog_offset。"
    )
