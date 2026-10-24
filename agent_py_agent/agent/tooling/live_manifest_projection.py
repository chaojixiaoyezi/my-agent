# LLM: 只处理当前完整manifest的live副本，不缓存权限/归档；JSON参数必须与最终运输一致。
# 模块用途: 沿原字段优先级线性扣减UTF8字节，避免每删一项重编码全表。
import json


# LLM: 保留原编码异常，Unicode切片与JSON转义分别计量。
# 函数用途: 精确测量最终格式的JSON字节数。
def json_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


# LLM: omitted从9到10等进位会增加字节；不假设每次加1编码宽度不变。
# 函数用途: 增加省略计数并返回字节增量。
def _increment(omitted, key):
    previous = omitted[key]
    omitted[key] += 1
    return len(str(omitted[key])) - len(str(previous))


# LLM: 每个被删除元素仅编码一次；最后一个元素没有逗号，保留旧尾删停止观察点。
# 函数用途: 在给定字节预算下从数组尾部省略条目。
def _drop_tail(items, omitted, key, total, limit):
    while items and total > limit:
        total -= json_size(items.pop()) + bool(items)
        total += _increment(omitted, key)
    return total


# LLM: 截断是原Python字符18/8规则，缺字段不补；每轮全部改完才测完整信封。
# 函数用途: 缩短全部工具名卡的用途文本。
def _shorten_tools(tools, length):
    for item in tools:
        for key in ("purpose", "description"):
            if key in item:
                item[key] = item[key][:length]


# LLM: 固定字段、插入顺序及文字沿原core，不以裁剪后的数组重算原始数量。
# 函数用途: 构造极端大manifest的核心事实副本。
def _core(live, omitted):
    return {
        "schema_name": "tool_runtime_manifest_live", "schema_version": 4,
        "counts": live["counts"], "visible_tool_count": live["visible_tool_count"],
        "self_context": {key: value for key, value in (live.get("self_context") or {}).items()
                         if key in {"tool_states", "usage", "discovery"}},
        "tools": [], "unavailable_tools": [], "omitted_items": omitted, "details_omitted": True,
        "detail_ref": "tool_search / self_map(section='tools'); 权限与完整目录读取本次归档",
        "detail_policy": {"read_artifact": {"artifact_ref_source": "本次完整manifest的宿主归档回执"}},
    }


# LLM: 完整真实编码校验累计值，漂移直接失败而不返回超限假事实；原不可删core的极端输入保持旧语义。
# 函数用途: 在返回前核对精确扣减结果。
def _checked(result, total):
    if json_size(result) != total:
        raise ValueError("live manifest字节计量不一致")
    return result


# LLM: 每个分支只固定次数整体编码；不新增权限memo，不改变完整manifest或每次归档。
# 函数用途: 按旧两轮截断、工具尾删、不可用尾删和核心降级顺序生成live视图。
def bounded_live_manifest(live, limit):
    if json_size(live) <= limit:
        return live
    result = dict(live)
    result["tools"] = [dict(item) for item in live.get("tools") or []]
    result["unavailable_tools"] = list(live.get("unavailable_tools") or [])
    for length in (18, 8):
        _shorten_tools(result["tools"], length)
        total = json_size(result)
        if total <= limit:
            return _checked(result, total)
    omitted = {"tools": 0, "unavailable_tools": 0}
    result["omitted_items"] = omitted
    total = json_size(result)
    for key in ("tools", "unavailable_tools"):
        total = _drop_tail(result[key], omitted, key, total, limit)
    if total <= limit:
        return _checked(result, total)
    return bounded_manifest_core(_core(live, omitted), limit)


# LLM: state详情全部先缩短再判预算，即使ASCII换省略号增字节也沿旧规则计数。
# 函数用途: 缩短状态中异常长的原因字段。
def _shorten_states(states, omitted):
    for state in states:
        for key in ("reason", "error_code"):
            value = state.get(key)
            if isinstance(value, str) and len(value) > 160:
                state[key] = value[:160] + "…"
                omitted["tool_state_details"] += 1


# LLM: 排序只编码每字段一次，稳定降序保留同分插入顺序；不在删除中途重查quarter门槛。
# 函数用途: 按原优先级削减usage或discovery字典。
def _drop_context_field(context, key, omitted, total, limit):
    value = context.get(key)
    size = json_size(value)
    if total <= limit or size <= limit // 4:
        return total
    if not isinstance(value, dict):
        context[key] = None
        omitted[key] = 1
        return total + 4 - size
    context[key] = remaining = dict(value)
    sizes = {name: json_size({name: item}) for name, item in value.items()}
    for name in sorted(value, key=sizes.__getitem__, reverse=True):
        if total <= limit:
            break
        del remaining[name]
        total -= sizes[name] - 2 + bool(remaining)
        total += _increment(omitted, key)
    return total


# LLM: core初始已合预算时不增加省略字段；状态与usage复制后只改副本，每次删除仅做局部字节扣减。
# 函数用途: 在保留原观察事实优先级的前提下裁剪极大核心目录。
def bounded_manifest_core(result, limit):
    if json_size(result) <= limit:
        return result
    context = dict(result["self_context"])
    states = [dict(state) for state in context.get("tool_states") or []]
    context["tool_states"] = states
    result["self_context"] = context
    omitted = {"tool_state_details": 0, "tool_states": 0, "usage": 0, "discovery": 0}
    result["self_context_omitted"] = omitted
    _shorten_states(states, omitted)
    total = json_size(result)
    for key in ("usage", "discovery"):
        total = _drop_context_field(context, key, omitted, total, limit)
    total = _drop_tail(states, omitted, "tool_states", total, limit)
    return _checked(result, total)
