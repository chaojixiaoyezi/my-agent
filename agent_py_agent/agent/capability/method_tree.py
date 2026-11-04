"""只消费描述符的确定性方法树及固定目录；无正文读取、存储写入或资产执行。"""
from __future__ import annotations

import re
import unicodedata

from ..assets.topology import _components, _connect, _reference_index, _resolve_reference

# 完整公开方法块的估算token预算，包含两组固定用法，不含能力包区。
METHOD_CATALOG_BUDGET_TOKENS = 2_500

_METHOD_USAGE_INSTRUCTIONS = """### 固定方法用法
- 先用 skill_search(action=get, skill_id=...) 读技能；绑定方法随技能下钻，未读不执行。
- 用 scripts/workflows(action=list) 展开抽屉，再用 action=get 查看合同。⤷是直接绑定数，⏰是监控别名。
- 目录不授予权限；参数、凭据和执行沿原工具校验。"""


# LLM: 只清理展示字段，不解析其中指令，不把描述用于身份或授权判断。
# 函数用途: 把文字变成有长度上限的单行，剥除控制与格式字符。
def _one_line(value, cap=None):
    text = " ".join("".join(" " if char.isspace() else char
                    for char in str(value or "") if char.isspace() or not unicodedata.category(char).startswith("C")).split())
    return text[:cap] if cap is not None else text


# LLM: 排序仅消费固定库存字段，不接收提问、在用状态、时钟、版本或修订。
# 函数用途: 生成技能、工作流、脚本及同类来源的确定性总序。
def method_sort_key(node):
    metadata = node.get("metadata", {})
    kind_rank = {"skill": 0, "workflow": 1, "script": 2, "monitor": 3}
    source_rank = {"workspace": 0, "owner": 1, "shared": 2, "builtin": 3}
    source = metadata.get("source") or node.get("source_scope") or node.get("scope_layer")
    return (kind_rank.get(node["kind"], len(kind_rank)), source_rank.get(source, len(source_rank)),
            _one_line(node.get("name")).casefold(), str(metadata.get("stable_id") or node["id"]))


# LLM: W8的列表形状与既有AssetRefs字典均可读，错误项当作缺字段，不引入新模块依赖。
# 函数用途: 把描述符引用转换成四类去重列表。
def _refs(value):
    refs = {kind: set() for kind in ("skills", "workflows", "scripts", "monitors")}
    if isinstance(value, dict):
        for kind in refs:
            values = value.get(kind)
            if isinstance(values, (tuple, list)):
                refs[kind].update(item for item in values if isinstance(item, str) and item)
    elif isinstance(value, list):
        for item in value:
            if (isinstance(item, dict) and isinstance(item.get("kind"), str)
                    and item["kind"] in {"skill", "workflow", "script", "monitor"}):
                reference = item.get("id")
                if isinstance(reference, str) and reference:
                    refs[item["kind"] + "s"].add(reference)
    return {kind: sorted(values) for kind, values in refs.items()}


# LLM: 隐藏组合只作为监控重定向资料，不变成可见节点，且不沿任意路径递归读取。
# 函数用途: 从已给定库存抽取监控目标到基础工作流的映射。
def _composition_redirects(rows):
    result = {}
    for row in rows:
        metadata = row.get("metadata", {})
        composition = metadata.get("composition")
        if row.get("kind") == "workflow" and metadata.get("hidden") is True and isinstance(composition, dict):
            base = composition.get("base_workflow_id")
            if isinstance(base, str) and base:
                result[row["id"]] = base
    return result


# LLM: 监控不是方法图父节点；只给显式别名且目标可见的监控生成徽标，不猜别名或执行历史。
# 函数用途: 解析监控引用，并把隐藏组合的徽标移到其基础工作流。
def _monitor_badges(rows, nodes, redirects):
    index = _reference_index(nodes)
    badges = []
    for row in sorted(rows, key=method_sort_key):
        if row["kind"] != "monitor":
            continue
        raw_alias = row["metadata"].get("alias")
        alias = _one_line(raw_alias, 24) if isinstance(raw_alias, str) else ""
        if not alias:
            continue
        targets = set()
        for plural, references in row["refs"].items():
            kind = plural[:-1]
            for reference in references:
                if kind == "workflow":
                    reference = redirects.get(reference, reference)
                targets.update(_resolve_reference(kind, reference, index))
        for target in sorted(targets, key=lambda key: method_sort_key(nodes[key])):
            badges.append({"id": row["id"], "alias": alias, "target": target})
    return badges


# LLM: 树只含可见启用方法；SCC线性分解，不枚举环；副本上的反向边不修改输入描述符。
# 函数用途: 计算绑定、独立入口、循环代表和监控徽标。
def build_method_tree(descriptors):
    rows = []
    for raw in descriptors:
        raw = raw.to_dict() if hasattr(raw, "to_dict") else raw
        if not isinstance(raw, dict) or raw.get("kind") not in {"skill", "workflow", "script", "monitor"}:
            continue
        if not isinstance(raw.get("id"), str) or not raw["id"] or raw.get("enabled", True) is not True:
            continue
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        if raw.get("status") == "draft" or metadata.get("draft") is True:
            continue
        rows.append({**raw, "name": raw.get("name") or raw["id"], "metadata": dict(metadata), "refs": _refs(raw.get("refs"))})
    redirects = _composition_redirects(rows)
    visible = [row for row in rows if row["kind"] != "monitor" and row["metadata"].get("hidden") is not True]
    nodes = {}
    for row in sorted(visible, key=method_sort_key):
        identifier = row["metadata"].get("stable_id", row["id"]) if row["kind"] == "skill" else row["id"]
        key = f"{row['kind']}:{identifier}"
        nodes.setdefault(key, {**row, "key": key})
    index = _reference_index(nodes)
    # 不可见引用既不创建占位节点，也不能把可见子项折叠到隐藏父项下。
    for node in nodes.values():
        node["refs"] = {plural: [reference for reference in references
                        if _resolve_reference(plural[:-1], reference, index)]
                        for plural, references in node["refs"].items()}
    children, parents = {key: set() for key in nodes}, {key: set() for key in nodes}
    for edge in _connect(nodes, stable_order=False):
        source, target = edge["from"], edge["to"]
        if nodes[source]["kind"] in {"skill", "workflow"} and nodes[target]["kind"] in {"script", "workflow"}:
            children[source].add(target)
            parents[target].add(source)
    roots = {key for key, node in nodes.items() if node["kind"] == "skill"}
    for component in _components(children):
        if not any(parents[key] - component for key in component):
            roots.add(min(component, key=lambda key: method_sort_key(nodes[key])))
    # LLM: 排序只查当前图的固定节点，不读取外部状态或改变输入。
    # 函数用途: 为根和邻接表共用同一个确定性顺序。
    def order(key):
        return method_sort_key(nodes[key])

    return {"nodes": nodes, "children": {key: sorted(values, key=order) for key, values in children.items()},
            "parents": {key: sorted(values, key=order) for key, values in parents.items()},
            "roots": sorted(roots, key=order), "monitors": _monitor_badges(rows, nodes, redirects)}


# LLM: 类目只来自固定metadata或首个tag，非法slug回退other，不根据提问推断。
# 函数用途: 取得至多24字符的小写类目名。
def _category(node):
    tags = node.get("tags")
    values = [node["metadata"].get("category"), tags[0] if isinstance(tags, (tuple, list)) and tags else None]
    for value in values:
        if isinstance(value, str):
            slug = value.strip().lower()
            if re.fullmatch(r"[a-z0-9_-]{1,24}", slug):
                return slug
    return "other"


# LLM: 同一目标最多展示两个显式别名，余量用计数保留，不消费运行状态。
# 函数用途: 生成有界监控徽标文字。
def _alias_text(aliases):
    return ",".join(aliases[:2]) + (f"(+{len(aliases) - 2})" if len(aliases) > 2 else "")


# LLM: 抽屉仅列独立节点，不扫描存储或推断绑定，类目与类目内节点顺序固定。
# 函数用途: 生成脚本或工作流的ID抽屉或类目计数。
def _drawers(nodes, keys, kind, *, counts=False):
    categories = {}
    for key in keys:
        categories.setdefault(_category(nodes[key]), []).append(key)
    lines = []
    for category in sorted(categories):
        members = categories[category]
        line = f"- [{kind}·{category}] {len(members)}"
        if not counts:
            line += ": " + ", ".join(_one_line(nodes[key]["id"]) for key in members)
        lines.append(line)
    return lines


# LLM: 每一级只改变固定展示粒度，所有计数源自同一可见图，不允许引入提问或在用参数。
# 函数用途: 准备一档目录的技能行与其它方法行，供整块预算选择。
def _level_lines(tree, level):
    nodes = tree["nodes"]
    skills = [key for key in tree["roots"] if nodes[key]["kind"] == "skill"]
    workflows = [key for key in tree["roots"] if nodes[key]["kind"] == "workflow"]
    scripts = [key for key in tree["roots"] if nodes[key]["kind"] == "script"]
    badges = {}
    for badge in tree["monitors"]:
        badges.setdefault(badge["target"], []).append(badge["alias"])

    # LLM: 子项计数只统计直接边，同一共享子项在每个父项各计一次。
    # 函数用途: 拼接一个确实有单独行的父项徽标。
    def suffix(key):
        count = len(tree["children"][key])
        return (f" ⤷{count}" if count else "") + (" ⏰" + _alias_text(badges[key]) if key in badges and level < 5 else "")

    skill_lines = []
    for key in skills:
        node = nodes[key]
        cap = 40 if level == 0 else 20 if level < 3 else 0
        summary = _one_line(node.get("summary"), cap)
        stable_id = node["metadata"].get("stable_id") or node["id"]
        skill_lines.append(f"- {_one_line(node['name'], 64)}" + (f": {summary}" if summary else "")
                           + f" (skill_id: {_one_line(stable_id)})" + suffix(key))
    lines = []
    if level == 5:
        flow_count = sum(node["kind"] == "workflow" for node in nodes.values())
        script_count = sum(node["kind"] == "script" for node in nodes.values())
        monitor_count = len({badge["id"] for badge in tree["monitors"]})
        lines.append(f"- workflow:{flow_count}; script:{script_count}; monitor:{monitor_count}; scripts/workflows(action=list)")
    elif level < 3:
        for key in workflows:
            node = nodes[key]
            cap = 24 if level == 0 else 12 if level == 1 else 0
            summary = _one_line(node.get("summary"), cap)
            lines.append(f"- [workflow] {_one_line(node['id'])}" + (f": {summary}" if summary else "") + suffix(key))
        lines.extend(_drawers(nodes, scripts, "script"))
    else:
        lines.extend(_drawers(nodes, workflows, "workflow", counts=level == 4))
        lines.extend(_drawers(nodes, scripts, "script", counts=level == 4))
    if level < 5:
        visible_lines = set(skills) | (set(workflows) if level < 3 else set())
        for target in sorted(badges, key=lambda key: method_sort_key(nodes[key])):
            if target not in visible_lines:
                lines.append(f"- [⏰] {_alias_text(badges[target])}→{_one_line(target)}")
    return skill_lines, lines


# LLM: 原技能用法逐字保留；方法用法是固定常量，两者都属于预算，不含能力包区。
# 函数用途: 装配可以直接进入系统提示的完整Available Skills块。
def _assemble(header, skill_lines, method_lines, usage):
    return "\n".join([*header, *skill_lines, "### 固定方法（工作流 / 脚本）", *method_lines]) + "\n\n" + usage + "\n\n" + _METHOD_USAGE_INSTRUCTIONS


# LLM: 超大技能库存只在末级按整行省略，保留固定用法和真实省略数，不按内容挑条目。
# 函数用途: 在固定信封内取最大有序技能前缀，避免二次方拼接。
def _omit_skills(header, skill_lines, method_lines, usage, max_bytes):
    notice = f"- 另有 {len(skill_lines)} 个 Skill 未显示；用 skill_search 检索。"
    used = len(_assemble(header, [], method_lines, usage).encode("utf-8")) + len(notice.encode("utf-8")) + 1
    kept = []
    for line in skill_lines:
        size = len(line.encode("utf-8")) + 1
        if used + size > max_bytes:
            break
        kept.append(line)
        used += size
    omitted = len(skill_lines) - len(kept)
    notice = f"- 另有 {omitted} 个 Skill 未显示；用 skill_search 检索。"
    return _assemble(header, [*kept, notice], method_lines, usage), omitted


# LLM: 首个完整块费用不超预算的级别获选；纯函数无IO，无运行时间或修订参与，供路由与测试共用。
# 函数用途: 渲染固定方法块并返回各级实际字节估算费用。
def render_method_catalog(descriptors, *, skill_usage_instructions, skill_header=(), bytes_per_token=4):
    tree = build_method_tree(descriptors)
    header = tuple(skill_header) or ("# Available Skills",)
    texts, costs = [], []
    for level in range(6):
        skill_lines, method_lines = _level_lines(tree, level)
        text = _assemble(header, skill_lines, method_lines, skill_usage_instructions)
        texts.append(text)
        costs.append((len(text.encode("utf-8")) + bytes_per_token - 1) // bytes_per_token)
    chosen = next((level for level, cost in enumerate(costs) if cost <= METHOD_CATALOG_BUDGET_TOKENS), 5)
    omitted = 0
    text = texts[chosen]
    if costs[chosen] > METHOD_CATALOG_BUDGET_TOKENS:
        skills, methods = _level_lines(tree, chosen)
        text, omitted = _omit_skills(header, skills, methods, skill_usage_instructions,
                                    METHOD_CATALOG_BUDGET_TOKENS * bytes_per_token)
    return {"text": text, "level": f"L{chosen}", "tokens": (len(text.encode("utf-8")) + bytes_per_token - 1) // bytes_per_token,
            "level_tokens": costs, "omitted_skills": omitted}
