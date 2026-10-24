# LLM: 旧算法作为独立差分oracle保留；仅临时数据，比较真实UTF8输出与序列化工作量。
# 模块用途: 回归manifest裁剪字段、顺序、省略计数和线性编码成本。
import copy
import json
import random

import pytest

from agent_py_agent.agent.tooling import registry


# LLM: 与生产最终输出参数完全相同，不用字符数估字节。
# 函数用途: 编码差分比较对象。
def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# LLM: 此参考流程来自基线e1ab9441f的两次用途降级规则。
# 函数用途: 完成旧算法的一轮字符截断。
def legacy_shorten(items, length):
    for item in items:
        for key in ("purpose", "description"):
            if key in item:
                item[key] = item[key][:length]


# LLM: 原core固定字段与插入顺序逐字保留，不能与新实现共用以免同错通过。
# 函数用途: 构造旧算法兜底信封。
def legacy_core(live, omitted):
    return {
        "schema_name": "tool_runtime_manifest_live", "schema_version": 4,
        "counts": live["counts"], "visible_tool_count": live["visible_tool_count"],
        "self_context": {key: value for key, value in (live.get("self_context") or {}).items()
                         if key in {"tool_states", "usage", "discovery"}},
        "tools": [], "unavailable_tools": [], "omitted_items": omitted, "details_omitted": True,
        "detail_ref": "tool_search / self_map(section='tools'); 权限与完整目录读取本次归档",
        "detail_policy": {"read_artifact": {"artifact_ref_source": "本次完整manifest的宿主归档回执"}},
    }


# LLM: 旧算法每次删除都重新编码全体；本函数只作小规模差分，不进入产品路径。
# 函数用途: 保留原usage/discovery删除优先级。
def legacy_context_field(result, key, limit, omitted):
    context = result["self_context"]
    value = context.get(key)
    if len(encoded(result)) <= limit or len(encoded(value)) <= limit // 4:
        return
    if not isinstance(value, dict):
        context[key], omitted[key] = None, 1
        return
    context[key] = dict(value)
    for field in sorted(value, key=lambda name: len(encoded({name: value[name]})), reverse=True):
        if len(encoded(result)) <= limit:
            break
        del context[key][field]
        omitted[key] += 1


# LLM: 此reference保留core初始短路和state字段/行省略的原始观察点。
# 函数用途: 以旧算法处理极大核心事实。
def legacy_bound_core(result, limit):
    if len(encoded(result)) <= limit:
        return result
    context = dict(result["self_context"])
    states = [dict(state) for state in context.get("tool_states") or []]
    context["tool_states"] = states
    result["self_context"] = context
    omitted = {"tool_state_details": 0, "tool_states": 0, "usage": 0, "discovery": 0}
    result["self_context_omitted"] = omitted
    for state in states:
        for key in ("reason", "error_code"):
            value = state.get(key)
            if isinstance(value, str) and len(value) > 160:
                state[key] = value[:160] + "…"
                omitted["tool_state_details"] += 1
    for key in ("usage", "discovery"):
        legacy_context_field(result, key, limit, omitted)
    while states and len(encoded(result)) > limit:
        states.pop()
        omitted["tool_states"] += 1
    return result


# LLM: 除拆出短截断辅助函数外保持基线流程，不以新字节公式构造oracle。
# 函数用途: 计算优化前的准确输出。
def legacy_bound(live, limit):
    if len(encoded(live)) <= limit:
        return live
    result = dict(live)
    result["tools"] = [dict(item) for item in live.get("tools") or []]
    result["unavailable_tools"] = list(live.get("unavailable_tools") or [])
    for length in (18, 8):
        legacy_shorten(result["tools"], length)
        if len(encoded(result)) <= limit:
            return result
    omitted = {"tools": 0, "unavailable_tools": 0}
    result["omitted_items"] = omitted
    for key in ("tools", "unavailable_tools"):
        while result[key] and len(encoded(result)) > limit:
            result[key].pop()
            omitted[key] += 1
    if len(encoded(result)) <= limit:
        return result
    return legacy_bound_core(legacy_core(live, omitted), limit)


# LLM: 包含中文、emoji、JSON转义与大量字典；种子固定，预期由旧实现逐字节计算。
# 函数用途: 生成可重复的manifest极端输入。
def sample(count, seed=0):
    rng = random.Random(seed)
    text = '中文🐳\\\n\x00"' * rng.randint(3, 30)
    return {"counts": {"registered": count}, "visible_tool_count": count,
            "snapshot_hash": "x" * rng.randint(0, 300), "permission_scope": text * 10,
            "tools": [{"name": f"tool-{n}-" + text, "purpose": text, "description": text}
                      for n in range(count)],
            "unavailable_tools": [{"name": f"bad-{n}", "reason": text * 3} for n in range(count // 2)],
            "self_context": {"tool_states": [{"name": f"state-{n}", "reason": text * 10, "error_code": text}
                                              for n in range(count)],
                             "usage": {f"u{n}": text * rng.randint(1, 20) for n in range(10)},
                             "discovery": {f"d{n}": text * rng.randint(1, 20) for n in range(10)}}}


# LLM: 统计编码字节总量而非耗时阈值；旧反复全字典编码会明显超出线性预算。
# 函数用途: 复现O(N²)整体序列化。
def test_manifest_serialization_work_is_linear(monkeypatch):
    value = sample(300)
    original, sizes = json.dumps, []

    # LLM: 仍执行标准编码；记录编码器实际处理的字节总量。
    # 函数用途: 观察序列化工作量。
    def measured(*args, **kwargs):
        text = original(*args, **kwargs)
        sizes.append(len(text.encode("utf-8")))
        return text

    original_size = len(encoded(value))
    monkeypatch.setattr(json, "dumps", measured)
    result = registry._bounded_live_manifest(value)
    assert len(encoded(result)) <= registry._LIVE_LIST_TOOLS_MAX_BYTES
    assert sum(sizes) <= original_size * 16


# LLM: 比较JSON字节而非仅dict相等，覆盖插入顺序与omitted数字位数变化。
# 函数用途: 验证新旧裁剪完全等价且不改原始输入。
@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("limit", [1200, 3999, 12000, 25000])
def test_manifest_matches_legacy_bytes(monkeypatch, seed, limit):
    live = sample(seed * 10, seed)
    before = copy.deepcopy(live)
    expected = legacy_bound(copy.deepcopy(live), limit)
    monkeypatch.setattr(registry, "_LIVE_LIST_TOOLS_MAX_BYTES", limit)
    actual = registry._bounded_live_manifest(live)
    assert encoded(actual) == encoded(expected)
    assert live == before


# LLM: 恰好满足原始/18字符/8字符预算时应在同一观察点返回，不额外省略名卡。
# 函数用途: 核对整轮短截断的字节边界。
@pytest.mark.parametrize("length", [None, 18, 8])
def test_exact_shortening_budget(monkeypatch, length):
    live = {"counts": {"registered": 2}, "visible_tool_count": 2,
            "tools": [{"name": "中文🐳", "purpose": '中文🐳\\\n"' * 30} for _ in range(2)],
            "unavailable_tools": []}
    expected = copy.deepcopy(live)
    if length is not None:
        legacy_shorten(expected["tools"], length)
    limit = len(encoded(expected))
    monkeypatch.setattr(registry, "_LIVE_LIST_TOOLS_MAX_BYTES", limit)
    actual = registry._bounded_live_manifest(live)
    assert encoded(actual) == encoded(expected) and "omitted_items" not in actual
    if length is None:
        assert actual is live


# LLM: 9/99附近精确预算必须计入新增数字位数，不能少删或多删一个尾项。
# 函数用途: 验证省略计数进位和最后真实JSON预算。
@pytest.mark.parametrize("removed", [9, 99])
@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_omitted_decimal_carry(monkeypatch, removed, delta):
    count = removed + 6
    live = {"counts": {"registered": count}, "visible_tool_count": count,
            "tools": [{"name": "n" * 180, "purpose": "p" * 30} for _ in range(count)],
            "unavailable_tools": []}
    desired = copy.deepcopy(live)
    legacy_shorten(desired["tools"], 8)
    desired["tools"] = desired["tools"][:6]
    desired["omitted_items"] = {"tools": removed, "unavailable_tools": 0}
    limit = len(encoded(desired)) + delta
    monkeypatch.setattr(registry, "_LIVE_LIST_TOOLS_MAX_BYTES", limit)
    actual = registry._bounded_live_manifest(live)
    assert encoded(actual) == encoded(legacy_bound(copy.deepcopy(live), limit))
    assert len(encoded(actual)) <= limit
    if not delta:
        assert actual["omitted_items"]["tools"] == removed


# LLM: core字段仅按旧usage优先和稳定同分顺序处理，非字典为单次省略；state裁剪可增字节。
# 函数用途: 核对核心异常输入裁剪的逐字节等价。
@pytest.mark.parametrize("usage", [{"b": "x" * 500, "a": "x" * 500}, "x" * 1000, ["x" * 500] * 2, None])
def test_core_non_dictionary_and_state_edges(monkeypatch, usage):
    core = legacy_core({"counts": {}, "visible_tool_count": 0, "self_context": {
        "usage": usage, "discovery": {"x": "中" * 600}, "tool_states": [
            {"reason": "a" * 161, "error_code": "a" * 160 + "…"},
            {"reason": "中" * 160, "error_code": 5}]}}, {"tools": 0, "unavailable_tools": 0})
    monkeypatch.setattr(registry, "_LIVE_LIST_TOOLS_MAX_BYTES", 1200)
    actual = registry._bounded_manifest_core(copy.deepcopy(core), lambda _: pytest.fail("旧全量循环仍在调用"))
    assert encoded(actual) == encoded(legacy_bound_core(copy.deepcopy(core), 1200))
