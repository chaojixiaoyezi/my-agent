# LLM: Item dependency enrichment turns natural sibling pipelines into machine-readable refs.
# 模块用途: 在 create_subagents items[] 批量派工时，自动把上游产物接到下游 required_read_paths。

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from .orchestration_create_items import CreateSubagentItem
from .parameters import _string_list
from .runner_input_dependencies import goal_input_refs, goal_output_refs


# LLM: enrich_item_dependencies preserves item order while adding refs-first sibling dependencies.
# 函数用途: 根据 items[] 里的 agent_name/goal 推断上游输出和下游输入，避免下游子代理抢跑。
def enrich_item_dependencies(items: list[CreateSubagentItem]) -> list[CreateSubagentItem]:
    enriched: list[CreateSubagentItem] = []
    producers: list[_Producer] = []
    for index, item in enumerate(items):
        refs = _inferred_required_refs(item, producers)
        next_item = _with_required_refs(item, refs) if refs else item
        enriched.append(next_item)
        producers.append(_Producer.from_item(index, item, goal_output_refs(item.goal)))
    return enriched


# LLM: item_dependency_edges maps natural sibling references to prior item indexes.
# 函数用途: 即使下游没写文件路径，只要提到上游代理结果，也能生成 run 级等待关系。
def item_dependency_edges(items: list[CreateSubagentItem]) -> list[list[int]]:
    edges: list[list[int]] = []
    producers: list[_Producer] = []
    for index, item in enumerate(items):
        deps = [producer.index for producer in producers if _depends_on_producer(item, producer)]
        edges.append(deps)
        producers.append(_Producer.from_item(index, item, goal_output_refs(item.goal)))
    return edges


# LLM: _Producer keeps only the lightweight facts needed to connect later item goals.
# 类用途: 记录一个已声明产物的上游子代理名称、目标和输出路径，供后续 item 依赖推断。
@dataclass(frozen=True)
class _Producer:
    index: int
    agent_name: str
    role: str
    goal: str
    outputs: list[str]

    # LLM: from_item normalizes producer identity without depending on persisted SubAgentTask.
    # 函数用途: 从 CreateSubagentItem 提取可匹配的上游身份，保持 create 阶段纯参数处理。
    @classmethod
    def from_item(cls, index: int, item: CreateSubagentItem, outputs: list[str]) -> _Producer:
        return cls(
            index=index,
            agent_name=str(item.params.get("agent_name") or "").strip(),
            role=str(item.params.get("role") or "").strip(),
            goal=item.goal,
            outputs=outputs,
        )


# LLM: _inferred_required_refs combines explicit input paths with prior sibling output refs.
# 函数用途: 既识别 goal 中直接写出的输入路径，也补齐“接收某代理输出”但没写路径的场景。
def _inferred_required_refs(item: CreateSubagentItem, producers: list[_Producer]) -> list[str]:
    refs = goal_input_refs(item.goal)
    for producer in producers:
        if producer.outputs and _depends_on_producer(item, producer):
            refs.extend(producer.outputs)
    return _merge_refs([_existing_required_paths(item.params), refs])


# LLM: _depends_on_producer joins explicit dependencies, path refs, and natural-language producer mentions.
# 函数用途: 判断当前 item 是否应该等待某个上游 item，避免下游读取上游产物时抢跑。
def _depends_on_producer(item: CreateSubagentItem, producer: _Producer) -> bool:
    return (
        _mentions_producer_output(item.goal, producer)
        or _refs_overlap(goal_input_refs(item.goal), producer.outputs)
        or _explicit_dependency_matches(item.params, producer)
    )


# LLM: _mentions_producer_output recognizes natural references to a previous sibling's deliverable.
# 函数用途: 判断当前 item 是否在说“接收/读取/基于某个上游代理的输出”。
def _mentions_producer_output(goal: str, producer: _Producer) -> bool:
    text = str(goal or "")
    if producer.agent_name and producer.agent_name in text:
        return _has_dependency_word(text)
    short_name = _short_agent_name(producer.agent_name)
    if short_name and short_name in text:
        return _has_dependency_word(text)
    return False


# LLM: _with_required_refs returns a copied item so the original parser output remains immutable.
# 函数用途: 将推断出来的 required_read_paths 写回单个 item 参数，供 create_context_manifest 固化。
def _with_required_refs(item: CreateSubagentItem, refs: list[str]) -> CreateSubagentItem:
    params = dict(item.params)
    params["required_read_paths"] = refs
    return replace(item, params=params)


# LLM: _existing_required_paths reads both top-level and manifest-level read path hints.
# 函数用途: 保留用户或模型已经显式给出的 required_read_paths，推断值只做追加。
def _existing_required_paths(params: dict[str, object]) -> list[str]:
    manifest = params.get("context_manifest")
    manifest_refs = manifest.get("required_read_paths") if isinstance(manifest, dict) else None
    return _merge_refs([params.get("required_read_paths"), manifest_refs])


# LLM: _short_agent_name lets "核验翻译" match "小傻妞-核验翻译" without hard-coded roles.
# 函数用途: 提取 agent_name 的后缀辨识词，兼容模型省略小傻妞前缀的自然表达。
def _short_agent_name(agent_name: str) -> str:
    text = str(agent_name or "").strip()
    if "-" in text:
        return text.rsplit("-", 1)[-1].strip()
    return text


# LLM: _has_dependency_word keeps sibling inference tied to read/receive semantics.
# 函数用途: 避免只是提到另一个代理名称就误把它的产物塞进输入依赖。
def _has_dependency_word(text: str) -> bool:
    markers = (
        "接收",
        "读取",
        "读",
        "基于",
        "根据",
        "依赖",
        "引用",
        "参考",
        "汇总",
        "整理",
        "整合",
        "分析",
        "提供",
        "结果",
        "输出",
    )
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in markers)


# LLM: _refs_overlap treats matching input/output paths as a workflow edge.
# 函数用途: `读取 data_collection.md` 应等待写出 `data_collection.md` 的上游，而不是只靠代理名字匹配。
def _refs_overlap(inputs: list[str], outputs: list[str]) -> bool:
    return any(_path_ref_matches(input_ref, output_ref) for input_ref in inputs for output_ref in outputs)


# LLM: _explicit_dependency_matches accepts common LLM dependency labels without making users name run ids.
# 函数用途: 支持 `dependencies: ["data_collection"]` 匹配上游产物 stem、代理短名或完整代理名。
def _explicit_dependency_matches(params: dict[str, object], producer: _Producer) -> bool:
    tokens = _dependency_tokens(params)
    if not tokens:
        return False
    producer_tokens = _producer_dependency_tokens(producer)
    return bool(tokens & producer_tokens)


# LLM: _dependency_tokens normalizes explicit item dependency fields into loose labels.
# 函数用途: 读取 dependencies/depends_on/workflow_depends_on，供 create 阶段转成真实 run 级边。
def _dependency_tokens(params: dict[str, object]) -> set[str]:
    raw = []
    for key in ("dependencies", "depends_on", "workflow_depends_on"):
        raw.extend(_string_list(params.get(key)))
    return {_token(value) for value in raw if _token(value)}


# LLM: _producer_dependency_tokens derives labels a downstream item may use for an upstream item.
# 函数用途: 从上游代理名和产物路径生成可匹配 label，比如 data_collection.md -> data_collection。
def _producer_dependency_tokens(producer: _Producer) -> set[str]:
    values = [producer.agent_name, _short_agent_name(producer.agent_name), *producer.outputs]
    tokens: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        path = Path(text)
        candidates = [text, path.name, path.stem]
        tokens.update(_token(candidate) for candidate in candidates if _token(candidate))
    return tokens


# LLM: _path_ref_matches keeps path matching conservative but works for bare filenames.
# 函数用途: 支持 `data/foo.md`、`foo.md` 和同名上游绝对产物互相匹配。
def _path_ref_matches(input_ref: str, output_ref: str) -> bool:
    input_text = str(input_ref or "").strip()
    output_text = str(output_ref or "").strip()
    if not input_text or not output_text:
        return False
    if input_text == output_text or output_text.endswith("/" + input_text):
        return True
    return Path(input_text).name == Path(output_text).name


# LLM: _token normalizes loose LLM dependency labels for matching.
# 函数用途: 把路径、短名和依赖标签转成小写 token；保留中文，统一空格/连字符/下划线差异。
def _token(value: str) -> str:
    text = str(value or "").strip().casefold()
    return text.replace(" ", "").replace("-", "_")


# LLM: _merge_refs preserves first-seen order across existing and inferred refs.
# 函数用途: 合并路径列表并去重，让测试和恢复包输出保持稳定。
def _merge_refs(values: list[object]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for item in _iter_refs(values):
        if item in seen:
            continue
        seen.add(item)
        merged.append(item)
    return merged


# LLM: _iter_refs flattens shallow ref lists for low-nesting merge code.
# 函数用途: 将多个可能的字符串/列表参数展开成单个路径流。
def _iter_refs(values: list[object]):
    for value in values:
        yield from _string_list(value)
