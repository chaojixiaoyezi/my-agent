# LLM: 拓扑只消费已给定描述符，反向引用是正向边的纯投影，不写Store、不读取授权或执行资产。
# 模块用途: 构建四类资产依赖图、反向引用、前向子图和稳定去重的全部简单有向环。
from __future__ import annotations

from .descriptor import ASSET_KINDS, AssetDescriptor, AssetRefs


# LLM: kind:id是唯一节点键；同键重复拒绝，不用输入used_by冒充实际反向边。
# 函数用途: 校验并建立独立的标准描述符节点表。
def _index_nodes(descriptors):
    nodes = {}
    for value in descriptors:
        descriptor = AssetDescriptor.from_dict(value.to_dict() if isinstance(value, AssetDescriptor) else value)
        key = f"{descriptor.kind}:{descriptor.id}"
        if key in nodes:
            raise ValueError("资产拓扑包含重复节点")
        nodes[key] = descriptor.to_dict() | {"key": key, "used_by": AssetRefs().to_dict()}
    return nodes


# LLM: 只枚举原输入节点的refs；缺失目标显式标记missing，边去重且反向引用只依据实际引用者kind。
# 函数用途: 生成确定性边表并把引用来源填入每个目标节点。
def _connect(nodes):
    edges = {}
    for key, node in tuple(nodes.items()):
        for kind in ASSET_KINDS:
            for asset_id in node["refs"][kind + "s"]:
                target = f"{kind}:{asset_id}"
                if target not in nodes:
                    nodes[target] = AssetDescriptor(asset_id, kind, asset_id).to_dict() | {"key": target, "missing": True}
                relation = "triggers" if node["kind"] == "monitor" and kind == "workflow" else "uses_" + kind
                edges[key, target, relation] = {"from": key, "to": target, "relation": relation}
                used_by = nodes[target]["used_by"][node["kind"] + "s"]
                if node["id"] not in used_by:
                    used_by.append(node["id"])
    for node in nodes.values():
        for ids in node["used_by"].values():
            ids.sort()
    return [edges[key] for key in sorted(edges)]


# LLM: 迭代式遍历对循环安全；root只能是图中确实存在的kind:id，不按文本猜测根。
# 函数用途: 提取起点的完整前向依赖集合。
def _reachable(nodes, edges, root_key):
    if root_key is None:
        return set(nodes)
    if not isinstance(root_key, str) or root_key not in nodes:
        raise ValueError("资产拓扑起点不存在")
    adjacency = {key: set() for key in nodes}
    for edge in edges:
        adjacency[edge["from"]].add(edge["to"])
    seen, pending = set(), [root_key]
    while pending:
        key = pending.pop()
        if key not in seen:
            seen.add(key)
            pending.extend(adjacency[key] - seen)
    return seen


# LLM: 迭代DFS只记录完成序，不依赖解释器递归深度；邻接表已限定展示子图。
# 函数用途: 线性遍历图，供强连通分量分解剔除无环支路。
def _finish_order(adjacency):
    seen, order = set(), []
    for root in adjacency:
        if root in seen:
            continue
        seen.add(root)
        pending = [(root, iter(adjacency[root]))]
        while pending:
            node, children = pending[-1]
            target = next(children, None)
            if target is None:
                order.append(node)
                pending.pop()
            elif target not in seen:
                seen.add(target)
                pending.append((target, iter(adjacency[target])))
    return order


# LLM: Kosaraju使用两次迭代DFS，复杂度O(V+E)；不把同一SCC误当作一条实际环。
# 函数用途: 拆分有向图，避免在无环图上枚举指数级无效路径。
def _components(adjacency):
    reverse = {key: set() for key in adjacency}
    for source, targets in adjacency.items():
        for target in targets:
            reverse[target].add(source)
    unseen = set(adjacency)
    for root in reversed(_finish_order(adjacency)):
        if root not in unseen:
            continue
        component, pending = set(), [root]
        unseen.remove(root)
        while pending:
            node = pending.pop()
            component.add(node)
            following = reverse[node] & unseen
            unseen.difference_update(following)
            pending.extend(following)
        yield component


# LLM: 每环仅从其字典序最小节点出发，内部节点不重复；闭合点重复首项，自环也保留。
# 函数用途: 从单一起点迭代枚举其规范简单环，不生成无限闭合游走。
def _cycles_from(adjacency, component, root):
    path, visited = [root], {root}
    pending = [iter(sorted(adjacency[root] & component))]
    while pending:
        target = next(pending[-1], None)
        if target is None:
            pending.pop()
            visited.remove(path.pop())
        elif target == root:
            yield [*path, root]
        elif target > root and target not in visited:
            path.append(target)
            visited.add(target)
            pending.append(iter(sorted(adjacency[target] & component)))


# LLM: 分量中每个节点作为一次规范起点，单起点函数负责过滤旋转重复；此层不改边或状态。
# 函数用途: 枚举一个强连通分量内所有简单有向环。
def _component_cycles(adjacency, component):
    for root in sorted(component):
        yield from _cycles_from(adjacency, component, root)


# LLM: 环只针对输出子图；结果全量排序、不设隐式截断，复杂度至少随真实环数量增长。
# 函数用途: 得到稳定环表，供图标记和has_cycles共用。
def _cycles(selected, edges):
    adjacency = {key: set() for key in sorted(selected)}
    for edge in edges:
        if edge['from'] in selected and edge['to'] in selected:
            adjacency[edge['from']].add(edge['to'])
    return sorted(cycle for component in _components(adjacency)
                  if len(component) > 1 or any(node in adjacency[node] for node in component)
                  for cycle in _component_cycles(adjacency, component))


# LLM: nodes/edges排序与used_by全图语义保持；新增环表只描述所选子图，不修改输入描述符。
# 函数用途: 为前端、Broker或Agent提供JSON可序列化的完整拓扑或可达子图。
def build_asset_topology(descriptors, root_key=None):
    nodes = _index_nodes(descriptors)
    edges = _connect(nodes)
    selected = _reachable(nodes, edges, root_key)
    cycles = _cycles(selected, edges)
    cyclic = {key for cycle in cycles for key in cycle}
    for key in selected:
        nodes[key]['in_cycle'] = key in cyclic
    return {"nodes": [nodes[key] for key in sorted(selected)],
            "edges": [edge for edge in edges if edge["from"] in selected and edge["to"] in selected],
            "cycles": cycles, "has_cycles": bool(cycles)}
