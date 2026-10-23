# LLM: 工具只调已登记的只读投影，不刷新服务；游标用公共来源+范围指纹校验，不另造签名或分页状态。
# 模块用途: 以短总览、分块分页和精确条目回答自身问题；能力族沿原快照投影，不另建静态能力表。
from __future__ import annotations

import base64
import hashlib
import json
from collections import Counter
from dataclasses import replace
from typing import Any

from ..contracts.tool_input_schema import validate_tool_input
from ..settings.defaults import default_config_int
from .models import (
    BaseTool,
    ConcurrencyPolicy,
    EffectResolverPolicy,
    ToolHandlerOutcome,
    ToolInvocationContext,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
    tool_effect_for_runtime_policy,
)
from .self_map_display import (
    compact_columns,
    compact_item,
    compact_metadata,
    coverage_summary,
    status_summary,
)
from .self_map_registry import (
    SectionProjection,
    SelfMapReadContext,
    SelfMapRegistry,
    SelfMapSectionUnknown,
    next_read,
    unknown_observation_notice,
)

# 常见目录最多60条；只改变紧凑目录条数，不调大原输出归档阈值。
SELF_MAP_PAGE_DEFAULT_COUNT = 60
# 默认 JSON 页字符上限，同时服从原预览字符/外置字节阈值；不是提高归档上限。
SELF_MAP_PAGE_DEFAULT_MAX_CHARS = 3500


# LLM: 来源指纹变化不是参数坏，而是旧页不可续接；固定码避免泄露路径或错误正文。
# 类用途: 区分目录过期与普通参数错误。
class SelfMapCursorStale(ValueError):
    pass


# LLM: 预算小于最小结构时明确报错，不能截断 JSON、伪称空页或制造原地续读循环。
# 类用途: 区分输出预算不足与错误查询参数。
class SelfMapResponseTooLarge(ValueError):
    pass


# LLM: provider 由 composition root 注入；handler 不保存库存、原始对象或 loaded，每调用只做临时投影。
# 类用途: 为主会话提供常驻的统一自我目录入口。
class SelfMapTool(BaseTool):
    _base_spec = ToolModelSpec(
        name="self_map", description="查询自己的目录、工具、能力、模型、权限和工作状态；无参数总览，section 分块，cursor 翻页。",
        input_schema={"type": "object", "properties": {
            "section": {"type": "string"}, "item": {"type": "string"}, "cursor": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        }, "additionalProperties": False},
        hints=ToolModelHints(category="introspection", keywords=("自己", "装了什么", "权限", "目录")),
    )
    runtime_policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy("read_only"), concurrency_policy=ConcurrencyPolicy("parallel_safe"))

    # LLM: 构造只保存依赖，领域读者在各自装配点登记，不在工具维护固定领域列表。
    # 函数用途: 接入原 Agent 与声明注册表。
    def __init__(self, agent: Any, sections: SelfMapRegistry):
        self.agent = agent
        self.sections = sections

    # LLM: 只读取装配期声明；每次返回独立schema，不缓存库存、身份、时间或路径，新增登记也同源更新枚举。
    # 函数用途: 让模型第一次调用前看见全部合法编号与静态用途，不执行reader。
    @property
    def model_spec(self):
        return _registered_spec(self._base_spec, self.sections)

    # LLM: 兼容本地调用也复用原 registry 快照；生产执行必须优先 execute_scoped。
    # 函数用途: 在当前默认范围读取一次目录。
    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        return self.execute_scoped(params, ToolInvocationContext(self.agent.tools.runtime_snapshot()))

    # LLM: 先校验再读权威；错编号只列声明、不执行读源，预算不足不伪称空库；不回显异常正文。
    # 函数用途: 在授权面内返回目录，给错编号和过期游标合法恢复入口。
    def execute_scoped(self, params: dict[str, Any], context: ToolInvocationContext) -> ToolHandlerOutcome:
        try:
            _validate_query(params, self.sections)
            read_context = SelfMapReadContext.for_call(self.agent, context)
            payload = self._query(params, read_context)
        except SelfMapSectionUnknown:
            return _outcome(False, {"error": "section 编号无效，请先不带参数看总览。",
                            "valid_sections": [row.id for row in self.sections.descriptors()],
                            "next_read": next_read("self_map", {})}, "TOOL_INVALID_ARGUMENTS")
        except SelfMapResponseTooLarge:
            return _outcome(False, {"error": "当前输出预算无法容纳最小目录回执，请调整预览预算或查询精确条目。"}, "SELF_MAP_RESPONSE_TOO_LARGE")
        except SelfMapCursorStale:
            return _outcome(False, {"error": "来源快照已变化，请重读本分块。", "next_read": next_read("self_map", {"section": params["section"]})}, "SELF_MAP_CURSOR_STALE")
        except ValueError:
            return _outcome(False, {"error": "section/item/cursor/limit 参数无效。"}, "TOOL_INVALID_ARGUMENTS")
        return _outcome(True, payload)

    # LLM: 总览和默认页约束真实归档预算，详情保留完整公开投影；只读同一次来源，不写领域枚举。
    # 函数用途: 分派总览、分页和详情；能力族从同次原领域投影聚合，不额外读取或刷新快照。
    def _query(self, params: dict[str, Any], context: SelfMapReadContext) -> dict[str, Any]:
        section = params.get("section")
        if not section:
            projections = [(descriptor, _read_projection(descriptor, context)) for descriptor in self.sections.descriptors()]
            payload = {"sections": [_overview(descriptor, projection) for descriptor, projection in projections],
                       "capability_families": {descriptor.id: _capability_families(projection)
                           for descriptor, projection in projections if descriptor.id in {"tools", "skills"}}}
            if not _fits_default(payload, context):
                raise SelfMapResponseTooLarge()
            return payload
        descriptor = self.sections.get(section)
        projection = _read_projection(descriptor, context)
        if params.get("item"):
            item = next((row for row in projection.items if row["id"] == params["item"]), None)
            if item is None:
                raise ValueError("unknown item")
            return {"section": section, "item": _item_entry(item, section, context, detail=True), "detail_complete": True,
                    "completeness": projection.completeness, "source": projection.source, "errors": projection.errors,
                    **_summary(projection, "total"), "locations": projection.locations,
                    **_family_fields(section, projection)}
        return _page(params, context, projection)


# LLM: 不允许查询时偷带身份或服务回调；bool 不是整数，只有分块列表支持分页。
# 函数用途: 在来源读取前拒绝错误参数组合。
def _validate_query(params: dict[str, Any], registry: SelfMapRegistry) -> None:
    if set(params) - {"section", "item", "cursor", "limit"}:
        raise ValueError("unknown argument")
    for field in ("section", "item", "cursor"):
        if field in params and (type(params[field]) is not str or not params[field].strip()):
            raise ValueError("invalid selector")
    if any(key in params for key in ("item", "cursor", "limit")) and "section" not in params:
        raise ValueError("section required")
    if "item" in params and any(key in params for key in ("cursor", "limit")):
        raise ValueError("item cannot paginate")
    if "limit" in params and (type(params["limit"]) is not int or not 1 <= params["limit"] <= 100):
        raise ValueError("invalid limit")
    if "section" in params:
        registry.get(params["section"])
    if "cursor" in params:
        _decode_cursor(params["cursor"])


# LLM: 读源失败只报安全码；显示标题和列表形态来自声明，不从领域ID或自由文本决定行为。
# 函数用途: 取得一个领域的公共快照，区分不可用与完整空集。
def _read_projection(descriptor: Any, context: SelfMapReadContext) -> SectionProjection:
    try:
        return replace(descriptor.reader(context), title=descriptor.title, list_mode=descriptor.list_mode)
    except (OSError, RuntimeError, TypeError, AttributeError, ValueError):
        return SectionProjection((), "partial", "unavailable", ("SELF_MAP_SOURCE_UNAVAILABLE",), count_known=False,
                                 title=descriptor.title, list_mode=descriptor.list_mode)


# LLM: 总览不带路径或库存清单；标题已在状态句里，不重复字段挤占原预算；未知数量仍保留原因及null。
# 函数用途: 给每域一行数量、空状态和查询入口，不重复同一标题。
def _overview(descriptor: Any, projection: SectionProjection) -> dict[str, Any]:
    return {"section": descriptor.id, **_summary(projection, "count"),
            "summary": status_summary(projection),
            "state": _projection_state(projection),
            "completeness": projection.completeness, "next_read": next_read("self_map", {"section": descriptor.id})}


# LLM: 完整性与 count_known 必须同时成立；没有计数接口的空投影也不能声称已知 empty。
# 函数用途: 总览与列表同源区分完整空集和未统计。
def _projection_state(projection: SectionProjection) -> str:
    empty = not projection.items and projection.count_known and projection.completeness == "complete" and projection.state == "known"
    return "empty" if empty else projection.state


# LLM: 已展开目标直接读；授权但收起的目标先发现并保留精确后续参数，无资格或可能写入的目标才回地图。
# 函数用途: 补合法读建议，避免收起的合法查询在地图详情之间空转。
def _item_entry(item: dict[str, Any], section: str, context: SelfMapReadContext, *, detail: bool = False) -> dict[str, Any]:
    row = dict(item)
    candidate = row.get("next_read") or {}
    if _readable_entry(candidate, context):
        return row
    if _readable_entry(candidate, context, expanded=False) and "tool_search" in context.visible_names:
        row["next_read"] = next_read("tool_search", {"query": candidate["tool"], "limit": 1})
        row["after_expansion"] = candidate
        return row
    row["next_read"] = next_read("self_map", {"section": section} if detail else {"section": section, "item": row["id"]})
    return row


# LLM: 先拒绝坏结构，再用冻结定义和原effect判定；发现后读口仍不能放宽工具授权或schema。
# 函数用途: 受控拒绝结构错误、不存在、未展开或可能写入的读建议。
def _readable_entry(candidate: Any, context: SelfMapReadContext, *, expanded: bool = True) -> bool:
    if not isinstance(candidate, dict):
        return False
    tool = candidate.get("tool")
    if type(tool) is not str or not tool.strip() or not isinstance(candidate.get("arguments"), dict):
        return False
    if expanded and tool not in context.visible_names:
        return False
    runtime = context.snapshot.runtime(tool)
    if runtime is None or not validate_tool_input(candidate["arguments"], runtime.model_spec.input_schema).ok:
        return False
    return tool_effect_for_runtime_policy(runtime.runtime_policy, candidate["arguments"]) == "read_only"


# LLM: 指纹绑定公共条目、数量、位置及显示声明；摘要/列形态变化也拒旧游标，不含秘密或秘密散列。
# 函数用途: 计算公共来源与范围指纹，业务数量、访问状态或显示合同改变也拒旧游标。
def _fingerprint(context: SelfMapReadContext, params: dict[str, Any], projection: SectionProjection) -> str:
    scope = (str(getattr(context.agent.home_paths, "owner_home_dir", "")), context.snapshot.snapshot_hash, context.thread_id,
             context.child, context.context_scope, sorted(context.visible_names), params["section"])
    fields = [scope, projection.items, projection.completeness, projection.errors,
              projection.source, projection.state, projection.count_known,
              projection.counts, projection.count_reasons, projection.locations, projection.count_reason,
              projection.summary, projection.count_labels, projection.title, projection.list_mode]
    payload = json.dumps(fields, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


# LLM: 游标结构严格校验，不用 eval 或签名；来源不匹配是 stale，任意偏移不能扩大授权。
# 函数用途: 解码偏移及公共来源指纹。
def _decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        value = json.loads(base64.b64decode(cursor.encode(), altchars=b"-_", validate=True))
    except (ValueError, UnicodeError):
        raise ValueError("bad cursor") from None
    if not isinstance(value, dict) or set(value) != {"offset", "fingerprint"}:
        raise ValueError("bad cursor fields")
    if type(value["offset"]) is not int or value["offset"] < 0 or type(value["fingerprint"]) is not str:
        raise ValueError("bad cursor values")
    return value


# LLM: 默认列表先压成列式短行再按真实预算收页；状态卡/显式limit保留公开详情，ID和名称不截。
# 函数用途: 常见目录一次列全并给逐条详情参数，预算不足仍有精确续页，不改变归档规则。
def _page(params: dict[str, Any], context: SelfMapReadContext, projection: SectionProjection) -> dict[str, Any]:
    items = sorted(projection.items, key=lambda row: row["id"])
    fingerprint = _fingerprint(context, params, projection)
    cursor = _decode_cursor(params["cursor"]) if params.get("cursor") else {"offset": 0, "fingerprint": fingerprint}
    if cursor["fingerprint"] != fingerprint:
        raise SelfMapCursorStale()
    offset, limit = cursor["offset"], params.get("limit", SELF_MAP_PAGE_DEFAULT_COUNT)
    if offset > len(items):
        raise ValueError("offset out of range")
    compact = "limit" not in params and projection.list_mode
    columns = compact_columns(items)
    selection = {"items": [compact_item(row, columns) if compact else _item_entry(row, params["section"], context)
                            for row in items[offset:offset + limit]],
                 "offset": offset, "fingerprint": fingerprint, "compact": compact,
                 "display": compact_metadata(params["section"], columns, items) if compact else {}}
    payload = _page_result(params, projection, selection)
    while "limit" not in params and not _fits_default(payload, context):
        _shrink_default_selection(selection, params["section"])
        payload = _page_result(params, projection, selection)
    return payload


# LLM: 只改临时页片；列式行已经是短摘要，不能再截身份或名称；旧状态卡仍最多缩一次。
# 函数用途: 单层收紧默认页，保留原身份、详情恢复入口和失败条件。
def _shrink_default_selection(selection, section):
    rows = selection["items"]
    if len(rows) > 1:
        rows.pop()
        return
    if selection.get("compact"):
        raise SelfMapResponseTooLarge()
    if rows and rows[0].get("detail_complete") is not False:
        rows[0] = _brief_item(rows[0], section)
        return
    raise SelfMapResponseTooLarge()


# LLM: 总览/分块数量与未观测说明同源；null不被0覆盖，领域计数原因不被通用状态说明替换。
# 函数用途: 生成公共数量摘要、逐项未知原因和投影级未观测说明。
def _summary(projection: SectionProjection, count_key: str) -> dict[str, Any]:
    known = projection.completeness == "complete" and projection.count_known
    reason = projection.count_reason or ("来源不完整或没有无副作用的完整计数接口。" if not known else "")
    return {count_key: len(projection.items) if known else None, "count_reason": reason,
            **unknown_observation_notice(projection.state),
            **({"counts": projection.counts} if projection.counts else {}),
            **({"count_reasons": projection.count_reasons} if projection.count_reasons else {})}


# LLM: 摘要与coverage同源给已全或实际剩余；共享详情模板绑定每行item参数，不复制60份调用壳或扩大阈值。
# 函数用途: 返回目录页及全投影类别计数，不把分页条数当能力总数，不保存分页状态。
def _page_result(params, projection, selection):
    tail = selection["offset"] + len(selection["items"])
    following = base64.urlsafe_b64encode(json.dumps({"offset": tail, "fingerprint": selection["fingerprint"]}, separators=(",", ":")).encode()).decode() if tail < len(projection.items) else None
    arguments = {"section": params["section"], "cursor": following, **({"limit": params["limit"]} if "limit" in params else {})}
    return {"section": params["section"], "items": selection["items"], **_summary(projection, "total"),
            "summary": (projection.summary or status_summary(projection)) + coverage_summary(projection, tail),
            "coverage": coverage_summary(projection, tail), "remaining": len(projection.items) - tail,
            **selection["display"],
            "returned": len(selection["items"]), "completeness": projection.completeness, "state": _projection_state(projection),
            "next_cursor": following, "next_read": next_read("self_map", arguments) if following else None,
            "source": projection.source, "errors": projection.errors, "locations": projection.locations,
            **_family_fields(params["section"], projection)}


# LLM: tool_projection来自ToolRuntimeSnapshot的category/group，skill_projection来自已核owner的SkillSnapshot；不回查全集。
# 函数用途: 对完整公开投影按开放类别计数，未绑定或降级来源返回None而不是假空集。
def _capability_families(projection: SectionProjection) -> dict[str, int] | None:
    if projection.completeness != "complete" or not projection.count_known:
        return None
    categories = (str(row.get("group") or row.get("category") or "uncategorized") for row in projection.items)
    return dict(sorted(Counter(categories).items()))


# LLM: 仅现有工具/方法读域附加类别摘要；不能给其它领域猜类别或授予可调用性。
# 函数用途: 为列表和详情共用能力族字段，类别计数与原完整性说明配套。
def _family_fields(section: str, projection: SectionProjection) -> dict[str, Any]:
    return {"capability_families": _capability_families(projection)} if section in {"tools", "skills"} else {}


# LLM: 只裁过长非身份字段，明确列出遗漏并给地图详情；path/id/name不截，详情仍返回原完整公开字段。
# 函数用途: 最小单条太大时形成短卡，不以摘要冒充完整详情。
def _brief_item(item, section):
    protected = {"id", "name", "path", "state", "next_read"}
    omitted = [key for key, value in item.items() if key not in protected and len(json.dumps(value, ensure_ascii=False)) > 256]
    return {**{key: value for key, value in item.items() if key not in omitted}, "detail_fields": omitted,
            "detail_complete": False, "next_read": next_read("self_map", {"section": section, "item": item["id"]})}


# LLM: 归档真正用字符/UTF8阈值和预览字符双门；不改配置、不请求保留正文豁免，0外置仍遵从原政策。
# 函数用途: 按当前有效预览和归档上限判断默认 JSON 是否能直接进入模型。
def _fits_default(payload, context):
    config = context.agent.config
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    threshold = getattr(config, "tool_output_externalize_min_chars", default_config_int("tool_output_externalize_min_chars"))
    return len(encoded) <= _default_budget(config) and len(encoded.encode()) < threshold


# LLM: 默认页只收紧原预览预算；单独入口便于真实阈值变异，不持有运行时状态。
# 函数用途: 给默认输出一个固定安全上限并服从更小的有效预览配置。
def _default_budget(config):
    return min(SELF_MAP_PAGE_DEFAULT_MAX_CHARS, getattr(config, "tool_output_preview_chars", default_config_int("tool_output_preview_chars")))


# LLM: 只序列化原公共投影与固定错误，不回显异常/输入值。
# 函数用途: 返回工具框架的标准结果。
def _outcome(ok: bool, payload: dict[str, Any], code: str = "") -> ToolHandlerOutcome:
    return ToolHandlerOutcome("self_map", ok, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), error_code=code)


# LLM: schema和说明仅由登记声明生成；空装配不生成非法空enum，不执行reader、不拼运行态，返回独立字典避免跨Agent污染。
# 函数用途: 从唯一登记表生成合法编号枚举和逐编号用途；空目录仍可合法查询总览。
def _registered_spec(base, registry):
    descriptors = registry.descriptors()
    selector = {"type": "string", **({"enum": [row.id for row in descriptors]} if descriptors else {})}
    properties = base.input_schema["properties"] | {"section": selector}
    description = base.description + "\n" + "\n".join(row.id + "：" + (row.purpose or row.title) + "。" for row in descriptors)
    description += "\n默认列表用item_columns声明短行；省略列沿item_defaults，item_name_from明确同ID名称来源。首列next_read是完整item参数，合并detail_next_read.arguments后调详情；显式limit给详细页。"
    return replace(base, description=description, input_schema=base.input_schema | {"properties": properties}, schema_hash="")
