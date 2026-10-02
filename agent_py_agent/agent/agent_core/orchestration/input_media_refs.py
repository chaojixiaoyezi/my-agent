# LLM: create_subagents 的 input_media_refs 解析与校验（第 14 条，子代理带图）。引用只认 sha256（父代理从
#   [INPUT_MEDIA_MANIFEST] 原样复制），宿主只在父级当前回合附件（task_attributes.input_media）和父级 transcript 的
#   canonical 用户媒体块里查找，再按 owner 附件根与配置上限重验；解析结果写进 child 的 host-owned 任务属性
#   input_media，与主会话同一键、同一管线（runtime/loop_support 的用户轮 typed media）。开关关闭时带引用整批拒绝；
#   未知/重复/格式错/超限整批拒绝；全部在创建任何 run 之前完成。不读正文、不按路径或文件名猜，attributes 里模型塞的
#   同名键一律丢弃。改动须同步 create_policy.create_task_attributes、orchestration_tools/hierarchy_tools 的错误映射、
#   error_taxonomy 两个码与 test_subagent_input_media.py。
# 模块用途: 把父代理点名的图片/视频安全地交给子代理：查、验、拒绝都在派工落账之前完成。
from __future__ import annotations

import json
import re
from typing import Any

from ...backends.request_content import is_local_media_block
from ...capability.runtime_config_reload import capability_config_for_agent
from ...conversation.authority import AGENT_THREAD_ID_ATTR, current_conversation_task_attributes
from ...conversation.input_media import InputMediaError, input_media_root, validate_input_media
from ...conversation.native_history import canonical_native_messages_from_metadata

INPUT_MEDIA_REFS_PARAM = "input_media_refs"
# child 任务属性里的宿主专有键；主会话的 Gateway 回合用同一个键放已验证附件引用。
INPUT_MEDIA_ATTR = "input_media"
INPUT_MEDIA_DISABLED_ERROR_CODE = "SUBAGENT_INPUT_MEDIA_DISABLED"
INPUT_MEDIA_INVALID_ERROR_CODE = "SUBAGENT_INPUT_MEDIA_INVALID"
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


# LLM: 创建前的结构化拒绝；message 已是给模型看的 JSON（ok=false + error_code + 逐项原因），调用方转成
#   not_started 工具回执，整批零创建。
# 类用途: 表示 input_media_refs 无法解析或开关关闭。
class SubagentInputMediaError(ValueError):
    def __init__(self, error_code: str, payload: dict[str, Any]) -> None:
        super().__init__(json.dumps({"ok": False, "error_code": error_code, **payload}, ensure_ascii=False))
        self.error_code = error_code
        self.payload = payload


# LLM: 唯一入口，根与递归创建共用（create_task_attributes）。attrs 里的 input_media 是宿主专有键，先清掉模型
#   可能塞进 attributes 的同名值；没传引用时不做任何事。开关读 capability 配置（capability_config_for_agent），
#   只认 True（MagicMock/字符串/读取失败都不算开）；主配置里没有这个键。
# 函数用途: 把 raw_params 的 input_media_refs 变成 child 任务属性里的已验证附件引用，或整批拒绝。
def bind_subagent_input_media(attrs: dict[str, Any], raw_params: dict[str, Any], agent: object) -> None:
    attrs.pop(INPUT_MEDIA_ATTR, None)
    requested = requested_media_refs(raw_params.get(INPUT_MEDIA_REFS_PARAM))
    if not requested:
        return
    if getattr(capability_config_for_agent(agent), "subagent_input_media_enabled", False) is not True:
        raise SubagentInputMediaError(INPUT_MEDIA_DISABLED_ERROR_CODE, {"requested_media_refs": requested})
    issues = _ref_issues(requested)
    wanted = [ref for ref in dict.fromkeys(requested) if _SHA256.match(ref)]
    available = _available_parent_media(agent, wanted)
    issues.extend({"media_ref": ref, "reason_code": "not_found"} for ref in wanted if ref not in available)
    if issues:
        raise SubagentInputMediaError(INPUT_MEDIA_INVALID_ERROR_CODE, {"invalid_media_refs": issues})
    attrs[INPUT_MEDIA_ATTR] = _validated_refs(agent, [available[ref] for ref in wanted])


# LLM: 与其它列表参数同一解析口径（JSON 字符串/列表都可），只保留非空字符串并去掉首尾空白；不做任何路径解释。
# 函数用途: 读出模型传的 media_ref 列表。
def requested_media_refs(value: object) -> list[str]:
    from ...common.value_parsing import TOOL_TEXT_LIST_OPTIONS, string_list

    return [item.strip() for item in string_list(value, TOOL_TEXT_LIST_OPTIONS) if str(item or "").strip()]


# 函数用途: 逐项记录格式错（不是 64 位十六进制）与重复的引用。
def _ref_issues(requested: list[str]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    issues: list[dict[str, Any]] = []
    for ref in requested:
        reason = "malformed" if not _SHA256.match(ref) else "duplicate" if ref in seen else ""
        seen.add(ref)
        if reason:
            issues.append({"media_ref": ref, "reason_code": reason})
    return issues


# LLM: 先看父级当前回合的已验证附件（主会话：Gateway 写的 input_media；child：自己首轮的 input_media），
#   没找全再按父级 transcript（child 用 agent_thread_id，主会话用 conversation_thread_id）的 canonical 行查；
#   两处都没有就是 not_found。不查别的线程、不查 owner 附件根里的其它文件。
# 函数用途: 按 sha256 在父会话里找到已验证的附件引用。
def _available_parent_media(agent: object, wanted: list[str]) -> dict[str, dict[str, Any]]:
    attrs = current_conversation_task_attributes(agent)
    found = {ref["sha256"]: dict(ref) for ref in attrs.get(INPUT_MEDIA_ATTR) or ()
             if isinstance(ref, dict) and ref.get("sha256") in wanted}
    missing = [ref for ref in wanted if ref not in found]
    thread_id = str(attrs.get(AGENT_THREAD_ID_ATTR) or attrs.get("conversation_thread_id") or "").strip()
    if missing and thread_id:
        found.update(_transcript_media(agent, thread_id, missing))
    return found


# LLM: 顺序访问全部 canonical 行（visit_all_report 不驻留全量正文），只收顶层用户消息里的 local_file 媒体块；
#   行文件读坏时 visit_all_report 一条都不交给 visitor，这里按没找到处理（fail closed），不猜。
# 函数用途: 在父级 transcript 里按 sha256 找历史轮的附件引用。
def _transcript_media(agent: object, thread_id: str, wanted: list[str]) -> dict[str, dict[str, Any]]:
    store = getattr(getattr(agent, "conversation_store", None), "messages", None)
    if store is None:
        return {}
    found: dict[str, dict[str, Any]] = {}

    def visit(entry: object) -> None:
        found.update({source["sha256"]: source for source in _entry_media_sources(entry)
                      if source.get("sha256") in wanted and source["sha256"] not in found})

    store.visit_all_report(thread_id, visit)
    return found


# 函数用途: 取出一行 canonical 消息里顶层用户媒体块的引用字段（去掉 source.type）。
def _entry_media_sources(entry: object) -> list[dict[str, Any]]:
    messages = canonical_native_messages_from_metadata(getattr(entry, "metadata", None))
    return [
        {key: value for key, value in block["source"].items() if key != "type"}
        for message in messages
        if message.get("role") == "user" and isinstance(message.get("content"), list)
        for block in message["content"]
        if is_local_media_block(block)
    ]


# LLM: 与 Gateway 入口同一验证函数和同一配置上限（owner 附件根、总字节、数量）；文件被删或改写按确定失败整批拒绝。
# 函数用途: 把找到的引用按当前 owner 的附件根重验一遍，返回可直接写进任务属性的引用列表。
def _validated_refs(agent: object, refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    config = agent.config
    try:
        validated = validate_input_media(
            refs, root=input_media_root(agent),
            max_bytes=int(config.input_media_max_bytes), max_files=int(config.input_media_max_files),
        )
    except InputMediaError as exc:
        raise SubagentInputMediaError(
            INPUT_MEDIA_INVALID_ERROR_CODE,
            {"invalid_media_refs": [{"media_ref": ref.get("sha256", ""), "reason_code": "limit", "message": str(exc)}
                                    for ref in refs]},
        ) from exc
    return [dict(ref) for ref in validated]
