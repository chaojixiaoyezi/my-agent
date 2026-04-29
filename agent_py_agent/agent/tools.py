from __future__ import annotations

"""智能体工具系统。

这个模块可以理解成“让模型不只会回答，还能真的动手”的那一层。

这次重构做了三件关键事：
1. 给每个工具补了更完整的元数据。
   不是只告诉模型“有这个工具”，还会告诉它适合什么场景、常用参数、什么时候别乱用。
2. 把工具说明拆成两层。
   常驻 prompt 里只放一份中等详细的工具目录，真正更细的说明只给当前任务最相关的几个工具。
3. 预留了混合检索框架。
   先用关键词命中保证稳，再预留向量检索接口，后面要接 embedding 时不用推翻现在的结构。

这样做的目的很直接：
- 少往 prompt 里塞无关内容
- 让模型更容易选对工具
- 后面继续加工具时不至于把提示词越堆越大
"""

import html
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ToolSpec:
    """单个工具的说明书。

    这份结构同时服务两类场景：
    - 生成给模型看的工具目录
    - 做工具检索和排序

    字段设计上尽量说人话，方便你后面继续扩展：
    - `description` 是一句话总述
    - `use_cases` 是“什么时候该用它”
    - `avoid_when` 是“什么时候别用它”
    - `keywords` 给检索器做召回
    - `parameters` / `parameter_details` 负责把参数说明拆成简版和详版
    """

    name: str
    category: str
    description: str
    use_cases: list[str]
    avoid_when: list[str]
    keywords: list[str]
    parameters: dict[str, str]
    parameter_details: dict[str, str] = field(default_factory=dict)
    examples: list[str] = field(default_factory=list)

    def render_catalog_entry(self) -> str:
        """渲染工具目录里的中等详细条目。

        这里故意不把所有细节都展开，只保留足够帮助模型做初步判断的信息。
        简单说，就是先给它看“工具菜单”，别一上来就把整本说明书塞过去。
        """

        params = "、".join(self.parameters.keys()) or "无"
        use_cases = "；".join(self.use_cases[:2]) or "无"
        avoid_when = "；".join(self.avoid_when[:1]) or "无"
        return (
            f"- {self.name} [{self.category}]：{self.description}\n"
            f"  适用场景：{use_cases}\n"
            f"  关键参数：{params}\n"
            f"  不适用时机：{avoid_when}"
        )

    def render_detail_entry(self) -> str:
        """渲染当前任务候选工具的详细说明。

        这里只给少数高相关工具展开，目的是减少误判，但不把所有工具都铺满 prompt。
        """

        params = "\n".join(
            f"  - {name}: {self.parameter_details.get(name, desc)}"
            for name, desc in self.parameters.items()
        ) or "  - 无"
        examples = "\n".join(f"  - {item}" for item in self.examples) or "  - 无"
        use_cases = "\n".join(f"  - {item}" for item in self.use_cases) or "  - 无"
        avoid_when = "\n".join(f"  - {item}" for item in self.avoid_when) or "  - 无"
        return (
            f"## {self.name}\n"
            f"类别：{self.category}\n"
            f"一句话说明：{self.description}\n"
            f"适合在这些时候用：\n{use_cases}\n"
            f"关键参数说明：\n{params}\n"
            f"示例：\n{examples}\n"
            f"这些场景别优先选它：\n{avoid_when}"
        )


@dataclass
class ToolExecutionResult:
    """工具执行结果。

    不管底层工具是读文件、写文件，还是发 HTTP 请求，最终都统一成这个结构。
    这样核心调度器只要认一种返回格式，后面加新工具也不用再改主循环。
    """

    tool: str
    ok: bool
    output: str

    def render_for_prompt(self) -> str:
        """把执行结果转成可直接塞回 prompt 的文本。"""

        status = "ok" if self.ok else "error"
        return f"[tool={self.tool}; status={status}]\n{self.output}"


@dataclass
class ToolSearchHit:
    """一次工具检索的命中结果。"""

    name: str
    score: float
    reasons: list[str]


class BaseToolSearchProvider:
    """工具检索提供者接口。

    先把接口定下来，后面无论你接本地 embedding 还是远端向量服务，都按这个协议接入。
    """

    name = "base"

    def search(self, query: str, specs: list[ToolSpec], limit: int) -> list[ToolSearchHit]:
        raise NotImplementedError


class KeywordToolSearchProvider(BaseToolSearchProvider):
    """关键词检索器。

    这是当前真正生效的第一层召回，优先保证稳定和可解释。
    说白了，它不够聪明，但胜在不容易胡来。
    """

    name = "keyword"

    def search(self, query: str, specs: list[ToolSpec], limit: int) -> list[ToolSearchHit]:
        tokens = _tokenize(query)
        hits: list[ToolSearchHit] = []
        for spec in specs:
            reasons: list[str] = []
            score = 0.0
            haystacks = {
                "name": spec.name.lower(),
                "description": spec.description.lower(),
                "category": spec.category.lower(),
                "keywords": " ".join(spec.keywords).lower(),
                "use_cases": " ".join(spec.use_cases).lower(),
            }
            for token in tokens:
                token_score = 0.0
                token_reasons: list[str] = []
                if token in haystacks["name"]:
                    token_score += 6.0
                    token_reasons.append(f"命中工具名“{token}”")
                if token in haystacks["keywords"]:
                    token_score += 4.0
                    token_reasons.append(f"命中关键词“{token}”")
                if token in haystacks["category"]:
                    token_score += 2.5
                    token_reasons.append(f"命中类别“{token}”")
                if token in haystacks["description"] or token in haystacks["use_cases"]:
                    token_score += 1.5
                    token_reasons.append(f"命中用途描述“{token}”")
                score += token_score
                reasons.extend(token_reasons[:1])
            if score > 0:
                hits.append(ToolSearchHit(name=spec.name, score=score, reasons=reasons[:3]))
        hits.sort(key=lambda item: (-item.score, item.name))
        return hits[:limit]


class VectorToolSearchProvider(BaseToolSearchProvider):
    """向量检索接口的占位实现。

    这版先不真的做 embedding 计算，只把扩展点留好。
    这样后面你要接向量库时，不需要再动核心调度器和 prompt 结构。
    """

    name = "vector"

    def __init__(self, enabled: bool = False):
        self.enabled = enabled

    def search(self, query: str, specs: list[ToolSpec], limit: int) -> list[ToolSearchHit]:
        if not self.enabled:
            return []
        return []


class HybridToolRetriever:
    """混合检索器。

    逻辑很简单：
    - 先把多个召回器的结果合并
    - 再按总分排
    - 最后给出“为什么推荐这个工具”

    现在真正起作用的是关键词层，向量层只是接口预留。
    """

    def __init__(self, providers: list[BaseToolSearchProvider]):
        self.providers = providers

    def search(self, query: str, specs: list[ToolSpec], limit: int) -> list[ToolSearchHit]:
        merged: dict[str, ToolSearchHit] = {}
        for provider in self.providers:
            for hit in provider.search(query, specs, limit):
                existing = merged.get(hit.name)
                provider_reason = f"{provider.name} 召回"
                if existing is None:
                    merged[hit.name] = ToolSearchHit(
                        name=hit.name,
                        score=hit.score,
                        reasons=[provider_reason, *hit.reasons][:4],
                    )
                    continue
                existing.score += hit.score
                for reason in [provider_reason, *hit.reasons]:
                    if reason not in existing.reasons:
                        existing.reasons.append(reason)
                existing.reasons = existing.reasons[:4]
        ranked = sorted(merged.values(), key=lambda item: (-item.score, item.name))
        return ranked[:limit]


class BaseTool:
    """所有具体工具的基类。"""

    spec: ToolSpec

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        raise NotImplementedError


class FileSystemTool(BaseTool):
    """文件系统类工具的安全边界。

    大白话解释：
    不管模型多聪明，都不能让它随便跳出工作区去乱读乱写。
    这里统一把路径钉死在工作区下面，减少误操作风险。
    """

    def __init__(self, workspace_root: Path):
        self.workspace_root = workspace_root.resolve()

    def resolve_path(self, raw_path: str) -> Path:
        """解析路径，并强制限制在工作区内部。"""

        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = (self.workspace_root / candidate).resolve()
        else:
            candidate = candidate.resolve()
        try:
            candidate.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ValueError(f"路径超出允许的工作区范围: {candidate}") from exc
        return candidate


class ListFilesTool(FileSystemTool):
    """列目录内容。"""

    def __init__(self, workspace_root: Path, max_entries: int):
        super().__init__(workspace_root)
        self.max_entries = max_entries
        self.spec = ToolSpec(
            name="list_files",
            category="filesystem",
            description="列出目录中的文件和子目录，适合先摸清项目结构。",
            use_cases=[
                "刚接手一个项目，先看看目录树大概长什么样",
                "不知道文件放在哪，先按目录层级摸排",
            ],
            avoid_when=[
                "已经知道目标文件路径时，别用它兜圈子，直接 read_file 更快",
            ],
            keywords=["目录", "文件树", "结构", "项目结构", "列文件", "list", "tree"],
            parameters={
                "path": "要查看的目录，默认是工作区根目录",
                "recursive": "是否递归展开子目录，默认 false",
            },
            parameter_details={
                "path": "相对工作区的目录路径；不传时默认从项目根目录开始列。",
                "recursive": "传 true 时会继续往下展开子目录；目录很大时要谨慎用，避免结果太长。",
            },
            examples=[
                '{"tool": "list_files", "path": "."}',
                '{"tool": "list_files", "path": "agent_py_agent/agent", "recursive": true}',
            ],
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        raw_path = str(params.get("path", "."))
        recursive = bool(params.get("recursive", False))
        target = self.resolve_path(raw_path)
        if not target.exists():
            return ToolExecutionResult("list_files", False, f"路径不存在: {target}")
        if target.is_file():
            return ToolExecutionResult("list_files", True, str(target.relative_to(self.workspace_root)))

        iterator = target.rglob("*") if recursive else target.iterdir()
        entries: list[str] = []
        for item in iterator:
            suffix = "/" if item.is_dir() else ""
            entries.append(str(item.relative_to(self.workspace_root)) + suffix)
            if len(entries) >= self.max_entries:
                entries.append(f"... 已截断，最多显示 {self.max_entries} 条")
                break
        return ToolExecutionResult("list_files", True, "\n".join(entries) or "目录为空")


class ReadFileTool(FileSystemTool):
    """读取文本文件。"""

    def __init__(self, workspace_root: Path, max_chars: int):
        super().__init__(workspace_root)
        self.max_chars = max_chars
        self.spec = ToolSpec(
            name="read_file",
            category="filesystem",
            description="读取文本文件内容，适合看代码、配置和文档。",
            use_cases=[
                "查看某个 Python 文件、配置文件或 Markdown 文档",
                "定位报错后，按行阅读相关代码",
            ],
            avoid_when=[
                "只想知道关键字在哪些文件出现过时，先用 search_text 更省",
            ],
            keywords=["读文件", "查看文件", "代码", "配置", "文档", "cat", "open file"],
            parameters={
                "path": "要读取的文件路径",
                "start_line": "起始行号，可选",
                "end_line": "结束行号，可选",
            },
            parameter_details={
                "path": "相对工作区的文本文件路径，必须是文件而不是目录。",
                "start_line": "从第几行开始读，默认从第 1 行开始。",
                "end_line": "读到第几行结束，包含该行；不传时默认读到文件结尾。",
            },
            examples=[
                '{"tool": "read_file", "path": "agent_py_agent/agent/core.py"}',
                '{"tool": "read_file", "path": "agent_py_agent/agent/core.py", "start_line": 1, "end_line": 120}',
            ],
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        raw_path = params.get("path")
        if not raw_path:
            return ToolExecutionResult("read_file", False, "缺少必填参数 path")

        target = self.resolve_path(str(raw_path))
        if not target.exists():
            return ToolExecutionResult("read_file", False, f"文件不存在: {target}")
        if not target.is_file():
            return ToolExecutionResult("read_file", False, f"目标不是文件: {target}")

        content = target.read_text(encoding="utf-8")
        lines = content.splitlines()
        start_line = max(int(params.get("start_line", 1)), 1)
        end_line = int(params.get("end_line", len(lines)))
        selected = lines[start_line - 1 : end_line]
        numbered = [f"{idx}: {line}" for idx, line in enumerate(selected, start=start_line)]
        result = "\n".join(numbered)
        if len(result) > self.max_chars:
            result = result[: self.max_chars] + "\n... 已截断"
        return ToolExecutionResult("read_file", True, result or "(空文件)")


class SearchTextTool(FileSystemTool):
    """在工作区里做纯文本搜索。"""

    def __init__(self, workspace_root: Path, max_matches: int):
        super().__init__(workspace_root)
        self.max_matches = max_matches
        self.spec = ToolSpec(
            name="search_text",
            category="filesystem",
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
                "path": "从哪个目录开始搜，默认是工作区根目录",
            },
            parameter_details={
                "query": "必填，直接按文本包含关系匹配，不做正则解析。",
                "path": "可选，把搜索范围缩小到某个子目录时更高效。",
            },
            examples=[
                '{"tool": "search_text", "query": "PromptBuilder"}',
                '{"tool": "search_text", "query": "max_tool_rounds", "path": "agent_py_agent"}',
            ],
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        query = str(params.get("query", "")).strip()
        if not query:
            return ToolExecutionResult("search_text", False, "缺少必填参数 query")

        target = self.resolve_path(str(params.get("path", ".")))
        if not target.exists():
            return ToolExecutionResult("search_text", False, f"路径不存在: {target}")

        search_root = target if target.is_dir() else target.parent
        candidates = [target] if target.is_file() else list(search_root.rglob("*"))
        matches: list[str] = []
        for item in candidates:
            if not item.is_file():
                continue
            try:
                for idx, line in enumerate(item.read_text(encoding="utf-8").splitlines(), start=1):
                    if query in line:
                        rel = item.relative_to(self.workspace_root)
                        matches.append(f"{rel}:{idx}: {line.strip()}")
                        if len(matches) >= self.max_matches:
                            matches.append(f"... 已截断，最多显示 {self.max_matches} 条")
                            return ToolExecutionResult("search_text", True, "\n".join(matches))
            except UnicodeDecodeError:
                continue
        return ToolExecutionResult("search_text", True, "\n".join(matches) or "没有找到匹配项")


class WriteFileTool(FileSystemTool):
    """写文件或覆盖文件。"""

    def __init__(self, workspace_root: Path):
        super().__init__(workspace_root)
        self.spec = ToolSpec(
            name="write_file",
            category="filesystem",
            description="写入或覆盖一个文本文件，适合生成新代码、脚本和配置。",
            use_cases=[
                "新建代码文件、配置文件或文档",
                "已经明确要重写某个文件的完整内容",
            ],
            avoid_when=[
                "只想补几行内容时别整文件重写，优先 append_file 或后续更细粒度编辑工具",
            ],
            keywords=["写文件", "生成代码", "创建文件", "覆盖", "save file", "write"],
            parameters={
                "path": "要写入的文件路径",
                "content": "完整文本内容",
            },
            parameter_details={
                "path": "相对工作区的目标文件路径；父目录不存在时会自动创建。",
                "content": "会直接成为文件的新内容；原文件存在时会被整体覆盖。",
            },
            examples=[
                '{"tool": "write_file", "path": "src/demo.py", "content": "print(\\"hello\\")\\n"}',
            ],
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        raw_path = params.get("path")
        content = params.get("content")
        if not raw_path:
            return ToolExecutionResult("write_file", False, "缺少必填参数 path")
        if content is None:
            return ToolExecutionResult("write_file", False, "缺少必填参数 content")

        target = self.resolve_path(str(raw_path))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
        return ToolExecutionResult(
            "write_file",
            True,
            f"已写入文件: {target.relative_to(self.workspace_root)}",
        )


class AppendFileTool(FileSystemTool):
    """向文件末尾追加内容。"""

    def __init__(self, workspace_root: Path):
        super().__init__(workspace_root)
        self.spec = ToolSpec(
            name="append_file",
            category="filesystem",
            description="向文本文件末尾追加内容，适合补日志、补文档和补配置片段。",
            use_cases=[
                "往日志、Markdown、结果汇总文件后面追加一段内容",
                "在不覆盖原文件的前提下补充说明",
            ],
            avoid_when=[
                "需要精确修改文件中间某一段时，不要拿它硬凑",
            ],
            keywords=["追加", "append", "补文档", "补日志", "末尾添加"],
            parameters={
                "path": "要追加的文件路径",
                "content": "要追加的文本内容",
            },
            parameter_details={
                "path": "相对工作区的目标文件路径；父目录不存在时会自动创建。",
                "content": "会直接拼接到文件尾部，不会替换已有内容。",
            },
            examples=[
                '{"tool": "append_file", "path": "RUNLOG.md", "content": "\\n- 新增一条记录"}',
            ],
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        raw_path = params.get("path")
        content = params.get("content")
        if not raw_path:
            return ToolExecutionResult("append_file", False, "缺少必填参数 path")
        if content is None:
            return ToolExecutionResult("append_file", False, "缺少必填参数 content")

        target = self.resolve_path(str(raw_path))
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as file:
            file.write(str(content))
        return ToolExecutionResult(
            "append_file",
            True,
            f"已追加文件: {target.relative_to(self.workspace_root)}",
        )


class ReplaceInFileTool(FileSystemTool):
    """精确替换文件中的一段文本。

    这个工具是给“差异化编辑”准备的。
    大白话说：如果只需要改一个函数、一行配置或一小段说明，就不要整文件覆盖。
    """

    def __init__(self, workspace_root: Path):
        super().__init__(workspace_root)
        self.spec = ToolSpec(
            name="replace_in_file",
            category="filesystem",
            description="在文本文件中精确替换一段已有内容，适合小范围改代码和改配置。",
            use_cases=[
                "只改一个函数、一段注释、一行配置或一小段文档",
                "已经通过 read_file 看过上下文，知道要替换的原文",
                "希望保留文件其他部分不动，避免 write_file 整文件覆盖",
            ],
            avoid_when=[
                "要创建新文件时用 write_file",
                "只是往文件末尾补内容时用 append_file",
                "不知道原文是否唯一时，先 read_file 或 search_text 确认上下文",
            ],
            keywords=[
                "替换",
                "修改代码",
                "局部编辑",
                "差异化编辑",
                "replace",
                "patch",
                "refactor",
                "edit",
            ],
            parameters={
                "path": "要修改的文件路径",
                "old": "文件中已经存在的原文",
                "new": "替换后的新内容",
                "count": "最多替换几处，默认 1",
            },
            parameter_details={
                "path": "相对工作区的文本文件路径，必须是已有文件。",
                "old": "必填，必须和文件里的原文完全一致；建议先用 read_file 获取准确片段。",
                "new": "必填，用来替换 old 的新文本。",
                "count": "可选，默认只替换第一处；传 0 或负数表示替换全部匹配。",
            },
            examples=[
                '{"tool": "replace_in_file", "path": "agent_py_agent/agent/core.py", "old": "max_tool_rounds: int = 5", "new": "max_tool_rounds: int = 8"}',
                '{"tool": "replace_in_file", "path": "README.md", "old": "old text", "new": "new text", "count": 1}',
            ],
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        raw_path = params.get("path")
        old = params.get("old")
        new = params.get("new")
        if not raw_path:
            return ToolExecutionResult("replace_in_file", False, "缺少必填参数 path")
        if old is None:
            return ToolExecutionResult("replace_in_file", False, "缺少必填参数 old")
        if new is None:
            return ToolExecutionResult("replace_in_file", False, "缺少必填参数 new")

        target = self.resolve_path(str(raw_path))
        if not target.exists():
            return ToolExecutionResult("replace_in_file", False, f"文件不存在: {target}")
        if not target.is_file():
            return ToolExecutionResult("replace_in_file", False, f"目标不是文件: {target}")

        content = target.read_text(encoding="utf-8")
        old_text = str(old)
        if old_text == "":
            return ToolExecutionResult("replace_in_file", False, "old 不能为空字符串")

        matches = content.count(old_text)
        if matches == 0:
            return ToolExecutionResult("replace_in_file", False, "没有找到要替换的原文，请先 read_file 确认上下文")

        raw_count = int(params.get("count", 1))
        replace_count = matches if raw_count <= 0 else raw_count
        updated = content.replace(old_text, str(new), replace_count)
        changed = min(matches, replace_count)
        target.write_text(updated, encoding="utf-8")
        return ToolExecutionResult(
            "replace_in_file",
            True,
            f"已修改文件: {target.relative_to(self.workspace_root)}；替换 {changed} 处；原文共命中 {matches} 处",
        )


class FetchUrlTool(BaseTool):
    """抓取网页或纯文本接口内容。"""

    def __init__(self, *, max_chars: int, timeout: int):
        self.max_chars = max_chars
        self.timeout = timeout
        self.spec = ToolSpec(
            name="fetch_url",
            category="web",
            description="抓取网页或文本接口内容，适合查在线文档、网页说明和纯文本页面。",
            use_cases=[
                "查看在线文档、普通网页正文或文本接口响应",
                "快速确认某个 URL 是否能访问、返回了什么文本",
            ],
            avoid_when=[
                "需要带复杂请求头、请求体或切换 HTTP 方法时，优先用 http_request",
            ],
            keywords=["网页", "抓网页", "文档", "URL", "fetch", "GET", "在线说明"],
            parameters={
                "url": "完整 URL",
            },
            parameter_details={
                "url": "必填，传入完整的 http 或 https 地址；工具内部固定按 GET 请求处理。",
            },
            examples=[
                '{"tool": "fetch_url", "url": "https://example.com/docs"}',
            ],
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        url = str(params.get("url", "")).strip()
        if not url:
            return ToolExecutionResult("fetch_url", False, "缺少必填参数 url")

        req = urllib.request.Request(
            url,
            method="GET",
            headers={"User-Agent": "SimplePythonAgent/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                result = (
                    f"status={resp.status}\n"
                    f"content_type={resp.headers.get('Content-Type', '')}\n\n"
                    f"{body[: self.max_chars]}"
                )
                if len(body) > self.max_chars:
                    result += "\n... 已截断"
                return ToolExecutionResult("fetch_url", True, result)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            return ToolExecutionResult("fetch_url", False, f"HTTP {exc.code}: {detail}")
        except Exception as exc:
            return ToolExecutionResult("fetch_url", False, f"请求失败: {exc}")


class HttpRequestTool(BaseTool):
    """通用 HTTP / API 调试工具。"""

    def __init__(self, *, max_chars: int, timeout: int):
        self.max_chars = max_chars
        self.timeout = timeout
        self.spec = ToolSpec(
            name="http_request",
            category="api",
            description="发送通用 HTTP 请求，适合调 REST API、Webhook 和普通接口。",
            use_cases=[
                "测试 GET/POST/PUT/DELETE 等接口返回是否正常",
                "带请求头、请求体去联调 API",
            ],
            avoid_when=[
                "只是想看一个普通网页正文时，fetch_url 更简单",
            ],
            keywords=["API", "接口", "HTTP", "POST", "GET", "Webhook", "请求头", "请求体"],
            parameters={
                "url": "完整 URL",
                "method": "HTTP 方法，默认 GET",
                "headers": "可选请求头",
                "body": "可选请求体",
            },
            parameter_details={
                "url": "必填，接口完整地址。",
                "method": "可选，支持 GET/POST/PUT/DELETE 等；默认是 GET。",
                "headers": "可传 JSON 对象或 JSON 字符串，常用于 Content-Type、Authorization 等。",
                "body": "可选，请求体会按 utf-8 文本发送；适合传 JSON 字符串或普通文本。",
            },
            examples=[
                '{"tool": "http_request", "url": "https://example.com/health"}',
                '{"tool": "http_request", "url": "https://example.com/api", "method": "POST", "headers": {"Content-Type": "application/json"}, "body": "{\\"name\\": \\"demo\\"}"}',
            ],
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        url = str(params.get("url", "")).strip()
        if not url:
            return ToolExecutionResult("http_request", False, "缺少必填参数 url")

        method = str(params.get("method", "GET")).upper()
        headers = self._normalize_headers(params.get("headers"))
        body = params.get("body")
        data = None if body is None else str(body).encode("utf-8")

        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body_text = resp.read().decode("utf-8", "replace")
                result = (
                    f"status={resp.status}\n"
                    f"content_type={resp.headers.get('Content-Type', '')}\n\n"
                    f"{body_text[: self.max_chars]}"
                )
                if len(body_text) > self.max_chars:
                    result += "\n... 已截断"
                return ToolExecutionResult("http_request", True, result)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            return ToolExecutionResult("http_request", False, f"HTTP {exc.code}: {detail}")
        except Exception as exc:
            return ToolExecutionResult("http_request", False, f"请求失败: {exc}")

    def _normalize_headers(self, headers: Any) -> dict[str, str]:
        """把请求头统一整理成 `dict[str, str]`。"""

        if headers is None:
            return {"User-Agent": "SimplePythonAgent/1.0"}
        if isinstance(headers, dict):
            normalized = {str(k): str(v) for k, v in headers.items()}
            normalized.setdefault("User-Agent", "SimplePythonAgent/1.0")
            return normalized
        if isinstance(headers, str):
            parsed = json.loads(headers)
            if not isinstance(parsed, dict):
                raise ValueError("headers 字符串解析后必须是 JSON 对象")
            normalized = {str(k): str(v) for k, v in parsed.items()}
            normalized.setdefault("User-Agent", "SimplePythonAgent/1.0")
            return normalized
        raise ValueError("headers 必须为空、对象或 JSON 字符串")


class ToolRegistry:
    """工具注册表。

    它负责四件事：
    - 统一登记有哪些工具
    - 生成“常驻目录”和“候选详情”两层工具说明
    - 解析模型发出的工具调用
    - 执行实际工具
    """

    def __init__(
        self,
        workspace_root: Path,
        *,
        max_chars: int,
        max_entries: int,
        max_matches: int,
        web_max_chars: int,
        http_timeout: int,
        catalog_limit: int,
        retrieval_limit: int,
        vector_search_enabled: bool,
    ):
        self.workspace_root = workspace_root.resolve()
        self.tools: dict[str, BaseTool] = {}
        self.catalog_limit = catalog_limit
        self.retrieval_limit = retrieval_limit
        self.retriever = HybridToolRetriever(
            [
                KeywordToolSearchProvider(),
                VectorToolSearchProvider(enabled=vector_search_enabled),
            ]
        )

        self.register(ListFilesTool(self.workspace_root, max_entries))
        self.register(ReadFileTool(self.workspace_root, max_chars))
        self.register(SearchTextTool(self.workspace_root, max_matches))
        self.register(WriteFileTool(self.workspace_root))
        self.register(AppendFileTool(self.workspace_root))
        self.register(ReplaceInFileTool(self.workspace_root))
        self.register(FetchUrlTool(max_chars=web_max_chars, timeout=http_timeout))
        self.register(HttpRequestTool(max_chars=web_max_chars, timeout=http_timeout))

    def register(self, tool: BaseTool) -> None:
        """注册一个工具。"""

        self.tools[tool.spec.name] = tool

    def specs(
        self,
        *,
        allowed_tools: list[str] | None = None,
        include_orchestration: bool = False,
    ) -> list[ToolSpec]:
        """按注册顺序返回所有工具说明。"""

        allowed = _allowed_tool_set(allowed_tools)
        specs = [tool.spec for tool in self.tools.values()]
        if not include_orchestration:
            specs = [spec for spec in specs if spec.category != "orchestration"]
        if allowed is None:
            return specs
        return [spec for spec in specs if spec.name in allowed]

    def render_catalog_section(self, *, allowed_tools: list[str] | None = None) -> str:
        """生成常驻 prompt 的工具目录。

        这里给的是中等详细度版本：
        模型能知道每个工具大概做什么、什么时候用、要传哪些关键参数，
        但不会把每个参数的长篇说明全塞进去。
        """

        specs = self.specs(allowed_tools=allowed_tools, include_orchestration=True)
        entries = [spec.render_catalog_entry() for spec in specs[: self.catalog_limit]]
        if not entries:
            entries = ["- none：当前执行上下文没有授权任何工具；缺能力时请上抛 capability_request。"]
        return (
            "# Tools\n"
            "当你需要看文件、改代码、查网页或测接口时，可以调用工具。\n"
            "工具调用格式必须严格写成：\n"
            "[TOOL_CALL]\n"
            '{"tool": "tool_name", "path": "example"}\n'
            "[/TOOL_CALL]\n"
            "可以连续写多个 [TOOL_CALL] 块。拿到工具结果后，再输出最终答案，不要把工具调用块留在最后回复里。\n\n"
            "# Tool Catalog\n"
            + "\n".join(entries)
        )

    def find_relevant_specs(
        self,
        query: str,
        *,
        allowed_tools: list[str] | None = None,
    ) -> list[ToolSpec]:
        """根据当前任务挑出最相关的少量工具。"""

        specs = self.specs(allowed_tools=allowed_tools, include_orchestration=True)
        hits = self.retriever.search(query, specs, self.retrieval_limit)
        if not hits:
            return []
        by_name = {spec.name: spec for spec in specs}
        return [by_name[hit.name] for hit in hits if hit.name in by_name]

    def render_recommended_tools_section(
        self,
        query: str,
        *,
        allowed_tools: list[str] | None = None,
    ) -> str:
        """生成当前任务的候选工具详情区块。"""

        specs = self.specs(allowed_tools=allowed_tools, include_orchestration=True)
        if not specs:
            return (
                "# Recommended Tools\n"
                "当前执行上下文没有授权工具。若缺少能力，请提交 capability_request。"
            )

        hits = self.retriever.search(query, specs, self.retrieval_limit)
        if not hits:
            return (
                "# Recommended Tools\n"
                "当前没有明显高相关的工具命中。若要动手操作，请先根据 Tool Catalog 选最接近的工具。"
            )

        by_name = {spec.name: spec for spec in specs}
        blocks: list[str] = []
        for hit in hits:
            spec = by_name[hit.name]
            reason_text = "；".join(hit.reasons) or "与当前任务相关"
            blocks.append(f"{spec.render_detail_entry()}\n推荐理由：{reason_text}")
        return "# Recommended Tools\n" + "\n\n".join(blocks)

    def parse_tool_calls(self, text: str) -> list[dict[str, Any]]:
        """从模型输出里提取工具调用块。"""

        calls: list[tuple[int, dict[str, Any]]] = []
        start_markers = ["[TOOL_CALL]", "[SUBAGENT_CALL]"]
        end_markers = ["[/TOOL_CALL]", "[/SUBAGENT_CALL]"]
        cursor = 0
        while True:
            starts = [
                (pos, marker)
                for marker in start_markers
                for pos in [text.find(marker, cursor)]
                if pos != -1
            ]
            if not starts:
                break
            start, marker_start = min(starts, key=lambda item: item[0])
            ends = [
                (pos, marker)
                for marker in end_markers
                for pos in [text.find(marker, start + len(marker_start))]
                if pos != -1
            ]
            if not ends:
                break
            end, marker_end = min(ends, key=lambda item: item[0])
            raw = text[start + len(marker_start) : end].strip().strip("`")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                calls.append(
                    (
                        start,
                        {
                            "tool": "__parse_error__",
                            "error": f"工具调用 JSON 解析失败: {exc}",
                            "raw": raw,
                        },
                    )
                )
            else:
                calls.append((start, payload))
            cursor = end + len(marker_end)
        calls.extend(_parse_xmlish_tool_calls(text))
        calls.sort(key=lambda item: item[0])
        return [payload for _, payload in calls]

    def execute_call(
        self,
        payload: dict[str, Any],
        *,
        allowed_tools: list[str] | None = None,
        write_boundary: dict[str, object] | None = None,
    ) -> ToolExecutionResult:
        """执行单个工具调用。"""

        if payload.get("tool") == "__parse_error__":
            return ToolExecutionResult("__parse_error__", False, payload["error"])

        tool_name = payload.get("tool")
        if not tool_name:
            return ToolExecutionResult("unknown", False, "工具调用缺少 tool 字段")

        allowed = _allowed_tool_set(allowed_tools)
        if allowed is not None and str(tool_name) not in allowed:
            return ToolExecutionResult(str(tool_name), False, f"工具未授权: {tool_name}")

        tool = self.tools.get(str(tool_name))
        if tool is None:
            return ToolExecutionResult(str(tool_name), False, f"未知工具: {tool_name}")

        params = {key: value for key, value in payload.items() if key != "tool"}
        boundary_error = _validate_write_boundary(
            str(tool_name),
            params,
            workspace_root=self.workspace_root,
            write_boundary=write_boundary,
        )
        if boundary_error:
            return ToolExecutionResult(str(tool_name), False, boundary_error)

        try:
            return tool.execute(params)
        except Exception as exc:
            return ToolExecutionResult(str(tool_name), False, f"工具执行失败: {exc}")


def _tokenize(text: str) -> list[str]:
    """把自然语言查询切成适合粗检索的小片段。

    这里不追求花哨，只做够用的切分：
    - 英文、数字、下划线按连续片段切
    - 中文按连续中文片段保留，再拆出 2 到 4 字的小片段补召回
    """

    lowered = text.lower()
    tokens = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", lowered)
    expanded: list[str] = []
    for token in tokens:
        expanded.append(token)
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            for size in range(2, min(4, len(token)) + 1):
                for idx in range(0, len(token) - size + 1):
                    expanded.append(token[idx : idx + size])
    seen: set[str] = set()
    unique: list[str] = []
    for token in expanded:
        if token not in seen:
            seen.add(token)
            unique.append(token)
    return unique


def _allowed_tool_set(allowed_tools: list[str] | None) -> set[str] | None:
    """把工具 allowlist 规范成集合；None 表示不限制。"""

    if allowed_tools is None:
        return None
    return {str(item) for item in allowed_tools if str(item).strip()}


WRITE_TOOL_NAMES = {"write_file", "append_file", "replace_in_file"}


def _validate_write_boundary(
    tool_name: str,
    params: dict[str, Any],
    *,
    workspace_root: Path,
    write_boundary: dict[str, object] | None,
) -> str:
    """Enforce subagent write boundaries before filesystem write tools run.

    In plain terms: prompts can tell a subagent "only write here", but prompts
    are not a lock. This check is the real lock at the tool layer: a write must
    stay inside allowed roots and avoid forbidden or locked paths.
    """

    if tool_name not in WRITE_TOOL_NAMES or write_boundary is None:
        return ""

    raw_path = params.get("path")
    if not raw_path:
        return ""

    try:
        target = _resolve_boundary_path(str(raw_path), workspace_root)
    except ValueError as exc:
        return str(exc)

    allowed_roots = _boundary_paths(write_boundary.get("allowed_write_roots"), workspace_root)
    if not allowed_roots:
        return "写入被阻止: 当前 subagent 没有配置 allowed_write_roots，不能执行写文件工具。"
    if not any(_is_relative_to(target, root) for root in allowed_roots):
        roots = ", ".join(_display_path(root, workspace_root) for root in allowed_roots)
        return (
            "写入被阻止: 目标路径不在 allowed_write_roots 内。"
            f" target={_display_path(target, workspace_root)} allowed={roots}"
        )

    forbidden_roots = _boundary_paths(write_boundary.get("forbidden_write_roots"), workspace_root)
    for root in forbidden_roots:
        if _is_relative_to(target, root):
            return (
                "写入被阻止: 目标路径落在 forbidden_write_roots 内。"
                f" target={_display_path(target, workspace_root)} forbidden={_display_path(root, workspace_root)}"
            )

    locked_paths = _boundary_paths(write_boundary.get("locked_files"), workspace_root)
    for locked in locked_paths:
        if target == locked or _is_relative_to(target, locked):
            return (
                "写入被阻止: 目标路径已被 locked_files 锁定。"
                f" target={_display_path(target, workspace_root)} locked={_display_path(locked, workspace_root)}"
            )

    return ""


def _boundary_paths(raw_paths: object, workspace_root: Path) -> list[Path]:
    if not isinstance(raw_paths, list):
        return []
    paths: list[Path] = []
    for raw in raw_paths:
        text = str(raw).strip()
        if not text:
            continue
        try:
            paths.append(_resolve_boundary_path(text, workspace_root))
        except ValueError:
            continue
    return paths


def _resolve_boundary_path(raw_path: str, workspace_root: Path) -> Path:
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError(f"路径超出允许的工作区范围: {resolved}") from exc
    return resolved


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _display_path(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root))
    except ValueError:
        return str(path)


_XMLISH_TOOL_BLOCK_RE = re.compile(
    r"<tool_call\b[^>]*>(?P<body>.*?)</tool_call\s*>",
    re.IGNORECASE | re.DOTALL,
)
_XMLISH_FUNCTION_EQ_RE = re.compile(
    r"<function\s*=\s*['\"]?(?P<name>[^'\">\s]+)['\"]?\s*>",
    re.IGNORECASE,
)
_XMLISH_FUNCTION_NAME_RE = re.compile(
    r"<function\b[^>]*\bname\s*=\s*['\"](?P<name>[^'\"]+)['\"][^>]*>",
    re.IGNORECASE,
)
_XMLISH_PARAMETER_EQ_RE = re.compile(
    r"<parameter\s*=\s*['\"]?(?P<name>[^'\">\s]+)['\"]?\s*>(?P<value>.*?)</parameter\s*>",
    re.IGNORECASE | re.DOTALL,
)
_XMLISH_PARAMETER_NAME_RE = re.compile(
    r"<parameter\b[^>]*\bname\s*=\s*['\"](?P<name>[^'\"]+)['\"][^>]*>(?P<value>.*?)</parameter\s*>",
    re.IGNORECASE | re.DOTALL,
)

_XMLISH_TOOL_ALIASES = {
    "append": "append_file",
    "append_file": "append_file",
    "cat": "read_file",
    "fetch": "fetch_url",
    "fetch_url": "fetch_url",
    "grep": "search_text",
    "http": "http_request",
    "http_request": "http_request",
    "list": "list_files",
    "list_files": "list_files",
    "ls": "list_files",
    "open": "read_file",
    "read": "read_file",
    "read_file": "read_file",
    "replace": "replace_in_file",
    "replace_in_file": "replace_in_file",
    "request": "http_request",
    "search": "search_text",
    "search_text": "search_text",
    "write": "write_file",
    "write_file": "write_file",
}

_XMLISH_PARAMETER_ALIASES = {
    "file": "path",
    "file_path": "path",
    "filepath": "path",
    "filename": "path",
}


def _parse_xmlish_tool_calls(text: str) -> list[tuple[int, dict[str, Any]]]:
    """Parse Qwen/OpenClaw-style XML-ish tool calls.

    Some runtimes emit blocks like:
    <tool_call><function=read><parameter=file_path>README.md</parameter>...

    They are not real XML, so we parse this small dialect explicitly. Broken
    blocks become __parse_error__ payloads instead of crashing the agent loop.
    """

    calls: list[tuple[int, dict[str, Any]]] = []
    cursor = 0
    for match in _XMLISH_TOOL_BLOCK_RE.finditer(text):
        calls.append(
            (
                match.start(),
                _parse_xmlish_tool_call_body(match.group("body"), match.group(0)),
            )
        )
        cursor = match.end()

    tail_start = text.lower().find("<tool_call", cursor)
    if tail_start != -1:
        calls.append(
            (
                tail_start,
                {
                    "tool": "__parse_error__",
                    "error": "XML-ish tool call is missing a closing </tool_call> tag",
                    "raw": text[tail_start:].strip(),
                },
            )
        )
    return calls


def _parse_xmlish_tool_call_body(body: str, raw: str) -> dict[str, Any]:
    function_match = _XMLISH_FUNCTION_EQ_RE.search(body)
    if function_match is None:
        function_match = _XMLISH_FUNCTION_NAME_RE.search(body)
    if function_match is None:
        return {
            "tool": "__parse_error__",
            "error": "XML-ish tool call is missing a function name",
            "raw": raw.strip(),
        }

    payload: dict[str, Any] = {
        "tool": _normalize_xmlish_tool_name(function_match.group("name"))
    }
    parameter_matches = list(_XMLISH_PARAMETER_EQ_RE.finditer(body))
    parameter_matches.extend(_XMLISH_PARAMETER_NAME_RE.finditer(body))
    parameter_matches.sort(key=lambda item: item.start())
    for parameter_match in parameter_matches:
        name = _normalize_xmlish_parameter_name(parameter_match.group("name"))
        payload[name] = _decode_xmlish_parameter_value(parameter_match.group("value"))
    return payload


def _normalize_xmlish_tool_name(name: str) -> str:
    cleaned = name.strip().lower().replace("-", "_")
    return _XMLISH_TOOL_ALIASES.get(cleaned, cleaned)


def _normalize_xmlish_parameter_name(name: str) -> str:
    cleaned = name.strip().lower().replace("-", "_")
    return _XMLISH_PARAMETER_ALIASES.get(cleaned, cleaned)


def _decode_xmlish_parameter_value(value: str) -> Any:
    text = html.unescape(value.strip())
    if not text:
        return ""
    lower = text.lower()
    looks_like_json = (
        text[0] in '{"['
        or lower in {"true", "false", "null"}
        or re.fullmatch(r"-?\d+(?:\.\d+)?", text) is not None
    )
    if looks_like_json:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    return text
