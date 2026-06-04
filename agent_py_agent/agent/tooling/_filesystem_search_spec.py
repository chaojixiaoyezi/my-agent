
from __future__ import annotations

from .models import ToolSpec


def build_search_text_spec() -> ToolSpec:
    return ToolSpec(
        name="search_text",
        category="filesystem",
        effect="read_only",
        description="在工作区里搜索纯文本，适合找函数名、配置项和关键字。",
        use_cases=[
            "想找某个函数、类、配置项出现在哪些文件里",
            "先全局搜索，再决定读哪几个文件",
        ],
        avoid_when=[
            "已经知道具体文件并且要看上下文时，直接 read_file 更合适",
        ],
        keywords=["搜索", "查找", "关键字", "grep", "rg", "全文检索", "文本匹配"],
        parameters={
            "query": "要搜索的文本",
            "pattern": "query 的别名，便于按 grep/rg 习惯调用",
            "path": "从哪个目录开始搜，默认是工作区根目录",
            "limit": "本次最多返回多少条匹配，默认使用工具配置上限",
            "offset": "跳过前多少条匹配，用于分页，默认 0",
            "file_glob": "只搜索匹配 glob 的文件，例如 *.py",
            "context": "每条命中前后额外展示多少行上下文，默认 0",
            "literal": "是否按普通文本匹配，默认 true；false 时按正则匹配",
            "ignore_case": "是否忽略大小写，默认 false",
            "output_mode": "输出模式：content、files_with_matches 或 count，默认 content",
            "include_ignored": "是否搜索 .git/node_modules 等常见噪声目录，默认 false",
        },
        parameter_details={
            "query": "必填，默认按文本包含关系匹配；需要正则时传 literal=false。",
            "pattern": "可选，和 query 等价；同时传时 query 优先。",
            "path": "可选，把搜索范围缩小到某个子目录时更高效。",
            "limit": "分页大小；命中很多时先看小批量，再用 next_offset 继续。",
            "offset": "上一页返回 next_offset 后，下一次传入这里继续看。",
            "file_glob": "按文件名或工作区相对路径过滤，例如 *.py、docs/*.md。",
            "context": "需要看命中附近内容时传 1 或 2；越大越占 prompt。",
            "literal": "默认 true，避免把用户普通文字误当正则；传 false 才启用正则。",
            "ignore_case": "大小写不确定时传 true。",
            "output_mode": "content 返回行内容；files_with_matches 只返回文件；count 返回每个文件命中数。",
            "include_ignored": "默认跳过 .git、node_modules 和常见缓存目录；确实要查时传 true。",
        },
        examples=[
            '{"tool": "search_text", "query": "PromptBuilder"}',
            '{"tool": "search_text", "query": "max_tool_rounds", "path": "agent_py_agent", "limit": 20, "offset": 0}',
            '{"tool": "search_text", "query": "class .*Tool", "literal": false, "file_glob": "*.py"}',
            '{"tool": "search_text", "query": "TODO", "output_mode": "files_with_matches"}',
        ],
    )
