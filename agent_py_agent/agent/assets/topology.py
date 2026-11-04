# LLM: 拓扑只消费已给定描述符，反向引用是正向边的纯投影，不写Store、不读取授权或执行资产。
# 模块用途: 构建四类资产依赖图、反向引用、前向子图和稳定去重的全部简单有向环。
"""资产依赖图的纯值投影；目录可单独复用连边与线性 SCC，不必枚举简单环。"""

from __future__ import annotations

from . import safe_asset_id
from .descriptor import ASSET_KINDS, AssetDescriptor, AssetRefs


# LLM: 无冲突时保留原 kind:id；同裸 ID 的不同来源 skill 用稳定身份区分，真正重复仍拒绝。
# 函数用途: 校验描述符并建立独立节点表，清空不可信的 stored used_by。
def _index_nodes(descriptors):
    groups, nodes = {}, {}
    for value in descriptors:
        descriptor = AssetDescriptor.from_dict(value.to_dict() if isinstance(value, AssetDescriptor) else value)
        key = f"{descriptor.kind}:{descriptor.id}"
        groups.setdefault(key, []).append(descriptor.to_dict())
    for original_key, group in groups.items():
        for node in group:
            key = original_key
            if len(group) > 1:
                stable_id = node['metadata'].get('stable_id')
                if node['kind'] != 'skill' or not isinstance(stable_id, str) or not stable_id.strip():
                    raise ValueError("资产拓扑包含重复节点")
                key = f"skill:{stable_id}"
            if key in nodes:
                raise ValueError("资产拓扑包含重复节点")
            nodes[key] = node | {"key": key, "used_by": AssetRefs().to_dict()}
    return nodes


# LLM: 规范节点通常已有 dict metadata；宽容读口不让缺失或错误扩展字段影响其它边。
# 函数用途: 取得只读元数据视图，非对象按空扩展处理。
def _metadata(node):
    metadata = node.get('metadata')
    return metadata if isinstance(metadata, dict) else {}


# LLM: member_of 只可折叠到真实可见父；placeholder、hidden=True 和 enabled=False 均不是父。
# 函数用途: 为反向绑定索引判断节点可见性，不改变完整依赖图的既有 refs 语义。
def _visible_parent(node):
    return not node.get('missing') and node.get('enabled', True) is not False and _metadata(node).get('hidden') is not True


# LLM: 来源权威是 metadata.source；仅空缺才回退 source_scope，不从 stable_id 的文本前缀推断。
# 函数用途: 提取裸 skill 别名排序使用的实际来源。
def _skill_source(node):
    source = _metadata(node).get('source')
    return source if isinstance(source, str) and source.strip() else node.get('source_scope', '')


# LLM: 别名与最高优先级候选只预建一次，重复引用不再扫描全表；字典保留无排序模式的输入顺序。
# 函数用途: 索引实际图键、准确 stable_id 及裸 id/name，支持可见父专用视图。
def _reference_index(nodes, *, visible_only=False):
    actual, stable, aliases, ranks = {}, {}, {}, {}
    priority = {source: rank for rank, source in enumerate(('workspace', 'owner', 'shared', 'builtin'))}
    for key, node in nodes.items():
        if node.get('missing') or visible_only and not _visible_parent(node):
            continue
        kind = node['kind']
        actual[kind, key] = (key,)
        stable_id = _metadata(node).get('stable_id')
        if kind == 'skill' and isinstance(stable_id, str) and stable_id:
            stable.setdefault((kind, stable_id), {})[key] = None
        source = _skill_source(node)
        rank = priority.get(source, len(priority)) if isinstance(source, str) else len(priority)
        names = {node['id'], node.get('name', '')}
        suffix = key.removeprefix(f'{kind}:')
        if ':' not in suffix:
            names.add(suffix)
        for name in names:
            if not isinstance(name, str) or not name:
                continue
            token = kind, name
            if token not in aliases or kind == 'skill' and rank < ranks[token]:
                aliases[token] = {}
                ranks[token] = rank
            if kind != 'skill' or rank == ranks[token]:
                aliases[token][key] = None
    return actual, stable, aliases


# LLM: 准确引用优先于裸别名；目标永远返回索引中的实际 key，不假设 key 等于 kind:id。
# 函数用途: 解析单个规范引用，裸 skill 名返回最高来源的全部并列候选。
def _resolve_reference(kind, reference, index):
    actual, stable, aliases = index
    token = kind, reference
    if token in actual:
        return actual[token]
    if token in stable:
        return stable[token]
    qualified = kind, f'{kind}:{reference}'
    if (kind != 'skill' or ':' in reference) and qualified in actual:
        return actual[qualified]
    return aliases.get(token, ())


# LLM: 无效 member_of 逐项忽略；仅消费既有安全 ID 规则，不跨片导入尚未合并的 normalizer。
# 函数用途: 读取 list 中的 skill/workflow 字典或 kind:id 声明，不创建父节点。
def _member_of(node):
    declarations = _metadata(node).get('member_of')
    if not isinstance(declarations, list):
        return
    for declaration in declarations:
        if isinstance(declaration, str):
            kind, _, asset_id = declaration.partition(':')
        elif isinstance(declaration, dict):
            kind, asset_id = declaration.get('kind'), declaration.get('id')
        else:
            continue
        if kind not in ('skill', 'workflow'):
            continue
        try:
            safe_asset_id(asset_id)
        except ValueError:
            continue
        yield kind, asset_id


# LLM: 缺依赖仍显式保留，逻辑 stable_id 不作为磁盘路径；公共描述符的安全 ID 校验不放宽。
# 函数用途: 为规范图中的未解析 refs 创建同形 missing 叶节点。
def _missing_node(nodes, kind, reference):
    prefix = f'{kind}:'
    target = reference if reference.startswith(prefix) else prefix + reference
    if target not in nodes:
        asset_id = reference.removeprefix(prefix)
        nodes[target] = AssetDescriptor('missing', kind, 'missing').to_dict() | {
            'id': asset_id, 'name': asset_id, 'key': target, 'missing': True}
    return target


# LLM: refs 和 member_of 共用边去重与反向投影，used_by 只含真实连边来源的原描述符 ID。
# 函数用途: 记录一条父到子边，保持 monitor→workflow 的 triggers 和既有四类 used_by shape。
def _add_edge(nodes, edges, incoming, source, target):
    node, child = nodes[source], nodes[target]
    relation = 'triggers' if node['kind'] == 'monitor' and child['kind'] == 'workflow' else 'uses_' + child['kind']
    edges[source, target, relation] = {'from': source, 'to': target, 'relation': relation}
    reference = target, node['kind'], node['id']
    if reference not in incoming:
        child['used_by'][node['kind'] + 's'].append(node['id'])
        incoming.add(reference)


# LLM: 只按 refs/member_of 连边，清空 stored used_by；无排序模式的预索引和连边均为 O(V+E)。
# 函数用途: 连接规范节点表及 monitor 引用，允许调用方使用 skill:<stable_id> 等实际节点键。
def _connect(nodes, *, stable_order=True):
    edges, incoming = {}, set()
    for node in nodes.values():
        node['used_by'] = AssetRefs().to_dict()
    index = _reference_index(nodes)
    parents = _reference_index(nodes, visible_only=True)
    for key, node in tuple(nodes.items()):
        if node.get('missing'):
            continue
        for kind in ASSET_KINDS:
            for asset_id in node.get('refs', {}).get(kind + 's', ()):
                targets = _resolve_reference(kind, asset_id, index)
                if not targets:
                    targets = (_missing_node(nodes, kind, asset_id),)
                for target in targets:
                    _add_edge(nodes, edges, incoming, key, target)
        for kind, asset_id in _member_of(node):
            for parent in _resolve_reference(kind, asset_id, parents):
                _add_edge(nodes, edges, incoming, parent, key)
    if stable_order:
        for node in nodes.values():
            for ids in node["used_by"].values():
                ids.sort()
    return [edges[key] for key in sorted(edges)] if stable_order else list(edges.values())


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


# LLM: 仅携带缺失kind:id及机器码，调用者不得按错误文案决定是否漏装依赖。
# 类用途: 为投递闭包提供结构化缺依赖错误。
class AssetClosureError(ValueError):
    # LLM: 引用来自校验过的图键，不带资产正文或凭据；不进行IO。
    # 函数用途: 保存明确的缺失依赖清单。
    def __init__(self, missing_refs):
        self.error_code = 'ASSET_DEPENDENCY_MISSING'
        self.missing_refs = list(missing_refs)
        super().__init__('资产依赖缺失')


# LLM: 仅用索引、无排序连边及迭代可达/完成序，O(V+E)，绝不调用简单环枚举；循环仅遍历一次，原子安装保障同包引用。
# 函数用途: 取得根资产的完整依赖先行闭包，根最后，缺失可达依赖明确失败，不含授权或数据源实体。
def extract_asset_closure(descriptors, root_key: str) -> tuple[AssetDescriptor, ...]:
    nodes = _index_nodes(descriptors)
    if root_key not in nodes:
        raise AssetClosureError([root_key])
    edges = _connect(nodes, stable_order=False)
    selected = _reachable(nodes, edges, root_key)
    missing = [key for key, node in nodes.items() if key in selected and node.get('missing')]
    if missing:
        raise AssetClosureError(missing)
    adjacency = {key: [] for key in (root_key, *nodes) if key in selected}
    for edge in edges:
        if edge['from'] in selected:
            adjacency[edge['from']].append(edge['to'])
    return tuple(AssetDescriptor.from_dict({key: value for key, value in nodes[name].items() if key != 'key'})
        for name in _finish_order(adjacency))
