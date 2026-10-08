# LLM: 主会话方法沿用的唯一负责模块；账本仅在线程隐藏字段中，原子更新不改摘要、pins 或权限。
#   只记成功 get，主会话资格来自运行结构字段；关掉现读开关时不写账、不改提示。联测 conversation_method_carry。
# 模块用途: 记下本会话读过的方法，供逐 run 目录和压缩后的参考资料恢复使用，不存第二份正文。
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import CancelledError
from dataclasses import dataclass, field, replace
from uuid import uuid4

from .. import runtime_context
from ..common.cancellation import ToolCancelled
from ..conversation.authority import current_conversation_task_attributes
from ..memory_archive import estimate_tokens
from .self_install_switches import read_fresh_capability_switch

# 单个主会话允许沿用的方法数（包与 skill 共用）。
CONVERSATION_METHOD_LIMIT_COUNT = 5
# 每个包保留的最近成功读取资料路径数，入口不占名额。
PACKAGE_METHOD_RESOURCE_LIMIT_COUNT = 8


# LLM: 持久化读写都走此解析器；仅接受宿主方法身份，剔除未知键和坏记录，不从摘要补造身份。
# 函数用途: 读取隐藏账本的结构化条目，旧线程缺字段时为空，最多保留最近五个方法。
def methods_from(value: object) -> tuple[dict, ...]:
    rows = value if isinstance(value, (tuple, list)) else ()
    valid = (_method_from(row) for row in rows if isinstance(row, dict))
    unique = {row["stable_id"]: row for row in valid if row is not None}
    return tuple(sorted(unique.values(), key=lambda row: row["last_used_at"])[-CONVERSATION_METHOD_LIMIT_COUNT:])


# LLM: 解析只保留身份、版本、代次与时间等宿主字段；坏值丢弃，不把正文或权限带入隐藏字段。
# 函数用途: 规范化一个方法条目，包才有版本和资料路径，skill 不复制正文。
def _method_from(row: dict) -> dict | None:
    kind, stable_id = row.get("kind"), row.get("stable_id")
    if kind not in {"capability_package", "skill"} or not isinstance(stable_id, str) or not stable_id:
        return None
    try:
        result = {"kind": kind, "stable_id": stable_id, "record_id": str(row.get("record_id") or ""),
                  "first_used_at": float(row.get("first_used_at") or 0), "last_used_at": float(row.get("last_used_at") or 0),
                  "read_generation": max(0, int(row.get("read_generation") or 0)),
                  "carried_generation": max(0, int(row.get("carried_generation") or 0))}
    except (TypeError, ValueError):
        return None
    if kind == "capability_package":
        result.update({key: str(row.get(key) or "") for key in ("package_id", "version", "content_sha256", "activation_id")})
        paths = row.get("resource_paths")
        result["resource_paths"] = list(dict.fromkeys(path for path in (paths if isinstance(paths, (list, tuple)) else ())
                                                     if isinstance(path, str) and path))[-PACKAGE_METHOD_RESOURCE_LIMIT_COUNT:]
    return result


# LLM: 主会话范围取当前工具执行参数；不能从自然语言或 task 名猜线程，也不把 parent 线程当成 child 的记录范围。
# 函数用途: 获取允许沿用的主会话线程编号；孩子、隔离、控制面和开关关闭时无范围。
def method_thread_id(agent: object, params: object = None) -> str:
    current = params if params is not None else getattr(agent, "_current_run_params", None)
    if runtime_context.current_subagent_run_id(agent) or getattr(current, "context_scope", "") in {"isolated", "control_plane"}:
        return ""
    if not read_fresh_capability_switch(agent, "conversation_method_carry_enabled"):
        return ""
    attributes = getattr(current, "task_attributes", None)
    if not isinstance(attributes, dict):
        attributes = current_conversation_task_attributes(agent)
    return str(attributes.get("conversation_thread_id") or "").strip()


# LLM: 读原线程权威，不读真实正文，不增加索引；失败仅留固定码，避免异常正文泄露或改变成功工具回执。
# 函数用途: 读取指定会话的在用账本，线程不存在时为空。
def conversation_methods(store: object, thread_id: str) -> tuple[dict, ...]:
    try:
        thread = store.threads.load(thread_id) if store is not None and thread_id else None
        return methods_from(getattr(thread, "conversation_methods", ()))
    except Exception:  # noqa: BLE001 - 附带方法提示不能拖垮主流程。
        logging.getLogger(__name__).warning("CONVERSATION_METHOD_READ_UNAVAILABLE")
        return ()


# LLM: 所有线程写入走同一锁内变换；异常只记结构码，不触碰主工具成功事实或其它线程状态。
# 函数用途: 原子更新一个线程的隐藏方法字段，写不进去时返回 False。
def _update(store: object, thread_id: str, update) -> bool:
    if store is None or not thread_id:
        return False
    try:
        store.threads.update_atomic(thread_id, update)
        return True
    except Exception:  # noqa: BLE001 - 不用附带记账故障翻转业务回执。
        logging.getLogger(__name__).warning("CONVERSATION_METHOD_WRITE_UNAVAILABLE")
        return False


# LLM: 原 skill_search 仅在有效正文返回前调用；同身份合并、入口不挤资料、LRU 淘汰，不改变原 pin 或读取权限。
# 函数用途: 为成功 get 记一笔，冻结首次使用时间供目录缓存，最近使用时间只用于淘汰和带回排序。
def record_method_read(agent: object, entry: object, resource_path: str = "") -> None:
    try:
        thread_id = method_thread_id(agent)
        if not thread_id:
            return
        now = time.time()

        # LLM: 在锁内合并，避免并行 skill_search 覆盖彼此条目；不更新摘要或线程 fingerprint。
        # 函数用途: 更新该方法的读取事实，再淘汰最近使用最早的一条。
        def update(thread):
            rows = list(methods_from(thread.conversation_methods))
            old = next((row for row in rows if row["stable_id"] == entry.stable_id), None)
            row = _read_record(entry, old, (now, thread.compact_generation), resource_path)
            kept = [item for item in rows if item["stable_id"] != entry.stable_id]
            return replace(thread, conversation_methods=methods_from([*kept, row]))

        _update(getattr(agent, "conversation_store", None), thread_id, update)
    except Exception:  # noqa: BLE001 - 成功 get 保持成功，记账失败只有固定码。
        logging.getLogger(__name__).warning("CONVERSATION_METHOD_RECORD_UNAVAILABLE")


# LLM: record_id 区分 remove 后同键重建；首次次序永不因重读刷新，carried_generation 只在带回成功后推进。
# 函数用途: 生成本次读取的条目；新方法以读时代次起步，不在尚未再压缩时重复注入。
def _read_record(entry, old: dict | None, observation: tuple, path: str) -> dict:
    now, generation = observation
    package_id = getattr(entry, "package_id", "")
    row = {**(old or {}), "kind": "capability_package" if package_id else "skill", "stable_id": entry.stable_id,
           "record_id": old["record_id"] if old else uuid4().hex,
           "first_used_at": old["first_used_at"] if old else now, "last_used_at": now,
           "read_generation": generation, "carried_generation": old["carried_generation"] if old else generation}
    if package_id:
        paths = list(row.get("resource_paths", ()))
        if path and path != entry.entry_document:
            paths = [item for item in paths if item != path] + [path]
        row.update(package_id=package_id, version=entry.version, content_sha256=entry.content_sha256,
                   activation_id=entry.activation_id, resource_paths=paths[-PACKAGE_METHOD_RESOURCE_LIMIT_COUNT:])
    return row


# LLM: 目录只消费逐 run 冻结的结构化名单；无名单返回空，不在构建提示时重读线程。
# 函数用途: 给提示接缝提供本 run 在用方法的稳定显示身份。
def prompt_method_ids(agent: object) -> tuple[str, ...]:
    if not method_thread_id(agent):
        return ()
    rows = getattr(getattr(agent, "_current_run_params", None), "conversation_methods", ()) or ()
    return tuple(row["stable_id"] for row in sorted(rows, key=lambda row: row["first_used_at"]))


# LLM: current_prompt_scope 在 Skill 快照就绪后调用一次；只为有资格的主会话参数冻结，空线程/控制面不写通用 scope 占位对象。
#   缓存属于本 run 参数，不挂全局线程或共享 Router；联测 method_directory 与 prompt_scope_failure 的嵌套/并发清理。
# 函数用途: 冻结本 run 的在用名单；中途读取仅改线程账本，无会话时保持原 prompt scope 状态。
def freeze_conversation_methods(agent: object) -> None:
    params = getattr(agent, "_current_run_params", None)
    if params is None or getattr(params, "conversation_methods", None) is not None:
        return
    thread_id = method_thread_id(agent)
    if not thread_id:
        return
    rows = conversation_methods(getattr(agent, "conversation_store", None), thread_id)
    params.conversation_methods = rows


# LLM: 仅给当前授权卡片添加展示元数据，身份匹配不到不补造；同类在用按冻结顺序，时间不出现在提示中。
# 函数用途: 把在用名卡排在前面并标注，required 的最小显示仍复用 Router 原逻辑。
def using_method_cards(cards: list, method_ids: tuple[str, ...]) -> list:
    if not method_ids:
        return cards
    order = {stable_id: index for index, stable_id in enumerate(method_ids)}
    marked = [replace(card, metadata={**card.metadata, "conversation_in_use": True})
              if card.metadata.get("stable_id") in order else card for card in cards]
    return sorted(marked, key=lambda card: order.get(card.metadata.get("stable_id"), len(order)))


# LLM: 名称来自宿主账本的 package_id 或 Skill stable_id 的名称段，不扫描目录、不拼读取路径。
# 函数用途: 为命令显示和精确移除取得方法名称，停用后也能按原身份移除。
def _method_name(row: dict) -> str:
    return row["package_id"] if row["kind"] == "capability_package" else row["stable_id"].partition(":")[2] or row["stable_id"]


# LLM: Gateway 已裁决 owner 与线程；只投影隐藏登记，不加载正文或修改方法安装状态。
# 函数用途: 列出当前会话的包和 Skill，包附已读资料数。
def render_conversation_methods(store: object, thread_id: str) -> str:
    rows = sorted(conversation_methods(store, thread_id), key=lambda row: row["first_used_at"])
    if not rows:
        return "本会话没有在用的方法。"
    lines = ["本会话在用的方法："]
    for row in rows:
        label = f"能力包，资料 {len(row['resource_paths'])} 份" if row["kind"] == "capability_package" else "Skill"
        lines.append(f"- {_method_name(row)}（{label}；{row['stable_id']}）")
    return "\n".join(lines)


# LLM: 在原线程锁内精确匹配名称或 stable_id；重名拒绝，避免移除多个登记；不动 pins、授权和包启用。
# 函数用途: 去掉本会话一个方法登记，返回是否成功；以后成功 get 可重新登记。
def remove_conversation_method(store: object, thread_id: str, name: str) -> bool:
    removed = []

    # LLM: 读取与删除同一锁内完成，不把命令前快照覆盖并发成功 get。
    # 函数用途: 只删除唯一命中的登记，其它线程字段和在用项保持不变。
    def update(thread):
        rows = methods_from(thread.conversation_methods)
        matched = [row for row in rows if name in {_method_name(row), row["stable_id"]}]
        if len(matched) != 1:
            return thread
        removed.append(matched[0]["stable_id"])
        return replace(thread, conversation_methods=tuple(row for row in rows if row["stable_id"] != removed[0]))

    return _update(store, thread_id, update) and bool(removed)


# LLM: 业务首请求的压缩恢复接缝；正文仍是参考，不执行资源、不增加调用或扩权。
# 函数用途: 压缩后按当前授权和缓存预算恢复主会话方法。
def prepare_conversation_method_carry(agent: object, params: object) -> None:
    thread_id = method_thread_id(agent, params)
    if not thread_id:
        return
    try:
        _prepare_method_carry(agent, params, thread_id)
    except (InterruptedError, ToolCancelled, CancelledError):
        raise
    except Exception:  # noqa: BLE001 - 增强失败只有固定码，不泄漏路径或异常正文。
        logging.getLogger(__name__).warning("CONVERSATION_METHOD_CARRY_UNAVAILABLE")


# LLM: 引用原取消、运行 scope 与 ManagedOperationStore；不创建任务，不把会话号冒充执行号。
# 类用途: 为压缩带回的读前、读后和投递前提供轻量当前执行权检查。
@dataclass(frozen=True)
class MethodCarryAuthority:
    agent: object
    params: object
    thread_id: str
    execution: object = field(init=False)

    # LLM: 冻结原执行范围而不是持久会话工作区；后续 params 变更不能冒充旧 attempt。
    # 函数用途: 保存准备开始时的运行身份，不新增任何持久状态。
    def __post_init__(self) -> None:
        from ..agent_core.tool_loop.recovery import runtime_run_scope
        object.__setattr__(self, "execution", runtime_run_scope(self.agent, self.params))

    # LLM: 取消传播；当前参数、原执行链与线程都要匹配，缺真实 operation authority 不能自动读取。
    # 函数用途: 在读取和发布边界拒绝已停止、换轮或串会话的旧准备结果。
    def check(self) -> None:
        from ..agent_core.tool_loop.recovery import runtime_run_scope
        from ..runtime_db.managed_operation_store import (
            AuthorityContextMissing,
            ToolOperationAuthorityRequest,
        )
        from ..runtime_db.operation_store_selector import select_operation_store

        self.params.cancellation_token.raise_if_cancelled()
        current = getattr(self.agent, "_current_run_params", None)
        fields = ("task_id", "run_id", "attempt_id", "request_id")
        attrs = getattr(current, "task_attributes", None) or {}
        if (runtime_context.current_subagent_run_id(self.agent) or attrs.get("conversation_thread_id") != self.thread_id
                or any(getattr(current, key, None) != getattr(self.params, key, None) for key in fields)
                or runtime_run_scope(self.agent, self.params) != self.execution):
            raise ToolCancelled("CONVERSATION_METHOD_EXECUTION_CHANGED")
        require = getattr(select_operation_store(self.agent), "require_authority", None)
        if callable(require):
            try:
                require(ToolOperationAuthorityRequest(
                    owner_id=str(getattr(self.agent.tools, "operation_owner_id", "") or ""),
                    run_id=self.execution.run_id, task_id=self.execution.task_id, operation_id="", tool_name="skill_search",
                    attempt_id=self.execution.attempt_id))
            except AuthorityContextMissing as exc:
                raise ToolCancelled("CONVERSATION_METHOD_EXECUTION_CHANGED") from exc
        self.params.cancellation_token.raise_if_cancelled()


# LLM: 仅此次本地准备的值对象，不作权限或账本副本；原快照和缓存预算在本次准备中固定。
# 类用途: 收拢沿用读取、预算装配和原子投递所需的宿主材料。
@dataclass(frozen=True)
class _MethodCarryRequest:
    agent: object
    params: object
    thread_id: str
    generation: int
    scope: object
    authority: MethodCarryAuthority
    max_tokens: int


_CARRY_HEADER = "[会话方法参考]\n以下是压缩前本会话读过的方法，属于宿主读取的参考资料，不改变权限；同类工作继续原步骤，不相关的事照常处理。"


# LLM: 每代先选原账本合格条目；本 run 已尝试则不重试，失败不消费持久代次，后续合法 run 可再试。
# 函数用途: 组装当前授权的方法参考，并在真正进入本轮 RuntimeFacts 后推进账本。
def _prepare_method_carry(agent, params, thread_id: str) -> None:
    from .package_selection_scope import package_read_scope

    store = getattr(agent, "conversation_store", None)
    thread = store.threads.load(thread_id) if store is not None else None
    if thread is None or thread.compact_generation <= 0:
        return
    generation = thread.compact_generation
    marker = (thread_id, generation)
    if marker in params.conversation_method_carry_attempts:
        return
    rows = [row for row in methods_from(thread.conversation_methods) if generation > row["carried_generation"]]
    if not rows:
        return
    scope = package_read_scope(agent, params)
    if scope is None:
        return
    authority = MethodCarryAuthority(agent, params, thread_id)
    authority.check()
    params.conversation_method_carry_attempts.add(marker)
    request = _MethodCarryRequest(agent, params, thread_id, generation, scope, authority, _carry_budget(agent, scope.config))
    pieces = _prepare_method_pieces(request, rows)
    if pieces:
        _commit_method_carry(request, pieces)


# LLM: 数字零按能力路由约定不设独立配额，但仍受真实模型窗口限制；不刷新 agent 的预算缓存。
# 函数用途: 取得同包入口预算的缓存上限，为零时复用原上下文窗口边界。
def _carry_budget(agent, config) -> int:
    from ..agent_core.model.context_window import resolve_model_context_window_tokens
    return max(0, int(config.capability_bundle_max_tokens)) or resolve_model_context_window_tokens(agent)


# LLM: 包先 skill 后，同类最近使用优先；计费覆盖头、分隔符和完整资料参数，不截断 JSON 或 next_read。
# 函数用途: 在总预算内依次准备可投递的方法片段，失败条目不阻挡其它条目。
def _prepare_method_pieces(request: _MethodCarryRequest, rows: list) -> list:
    ordered = sorted(rows, key=lambda row: (row["kind"] != "capability_package", -row["last_used_at"]))
    pieces = []
    for row in ordered:
        used = _carry_text(pieces)
        remaining = max(0, request.max_tokens - estimate_tokens(used + "\n\n"))
        loader = _prepare_package_method if row["kind"] == "capability_package" else _prepare_skill_method
        text = loader(request, row, remaining)
        if text and estimate_tokens(used + "\n\n" + text) <= request.max_tokens:
            pieces.append((row, text))
    return pieces


# LLM: 包按当前授权快照 ref 读；原 reader/policy/pins 保持，停用/删除不退回全局安装，升级不绕旧 task pin。
# 函数用途: 带回包入口和当前版本仍存在的已读资料清单，不加载资料正文或执行脚本。
def _prepare_package_method(request: _MethodCarryRequest, row: dict, remaining: int) -> str:
    from .package_selection_context import prepare_package_entry_context

    package = request.scope.skills.resolve_package(row["package_id"])
    if package is None:
        return ""
    resource_text = _package_method_resources(package, row)
    reserved = estimate_tokens(resource_text + "\n\n")
    if remaining <= reserved:
        return ""
    entry = prepare_package_entry_context(
        request.agent, request.params, request.scope, [package.to_ref()], authority=request.authority,
        claim_id=f"carry:{request.thread_id}:{request.generation}", max_tokens=remaining - reserved)
    if not entry.text:
        logging.getLogger(__name__).warning("CONVERSATION_METHOD_ENTRY_UNAVAILABLE")
        return ""
    return entry.text + "\n\n" + resource_text


# LLM: 仅当前清单中存在的路径提供完整重读参数，入口不算资料；不替旧建议去掉 expected pin。
# 函数用途: 把压缩前读过的包内资料投影成路径与 next_read 清单。
def _package_method_resources(package, row: dict) -> str:
    from .package_snapshot import package_read_parameters
    paths = [path for path in row.get("resource_paths", ()) if path != package.entry_document and package.resolve(path) is not None]
    references = [{"resource_path": path, "next_read": {**package_read_parameters(package.to_ref()), "resource_path": path}}
                  for path in paths]
    return "压缩前读过的包内资料：\n" + json.dumps(references, ensure_ascii=False, separators=(",", ":"))


# LLM: 普通 skill 仍经原工具 ActionPolicy，只读当前快照正文并按完整投递 token 截开头；失败不泄漏异常正文。
# 函数用途: 带回普通 skill 正文开头，保留完整稳定身份；预算放不下身份时跳过。
def _prepare_skill_method(request: _MethodCarryRequest, row: dict, remaining: int) -> str:
    from .package_selection_authority import package_entry_policy

    entry = request.scope.skills.resolve(row["stable_id"])
    if entry is None or remaining <= 0:
        return ""
    request.authority.check()
    decision = package_entry_policy(request.agent, request.params, request.scope.tools,
                                    {"action": "get", "skill_id": entry.stable_id},
                                    claim_id=f"carry:{request.thread_id}:{request.generation}")
    if not decision.allowed or decision.resolved_effect != "read_only":
        return ""
    try:
        body = request.scope.skills.read_body(entry.stable_id)
    except Exception:  # noqa: BLE001 - 恢复失败只留结构化提示码。
        logging.getLogger(__name__).warning("CONVERSATION_METHOD_SKILL_UNAVAILABLE")
        return ""
    request.authority.check()
    prefix = f"Skill 参考（skill_id: {entry.stable_id}）\n"
    return _fit_skill_prefix(prefix, body, remaining)


# LLM: estimate_tokens 的同源估算包含头和正文；二分截取 Unicode 字符，不截身份或结构化参数。
# 函数用途: 在剩余预算里取 skill 的非空正文开头。
def _fit_skill_prefix(prefix: str, body: str, allowance: int) -> str:
    low, high, result = 1, len(body), ""
    while low <= high:
        size = (low + high) // 2
        text = prefix + body[:size]
        if estimate_tokens(text) <= allowance:
            result, low = text, size + 1
        else:
            high = size - 1
    return result


# LLM: 文本只来自宿主准备的片段；排序已在读取前确定，不显示时间或解析正文决定状态。
# 函数用途: 为预算和 RuntimeFacts 共用同一完整方法参考文本。
def _carry_text(pieces: list) -> str:
    return _CARRY_HEADER + "".join("\n\n" + text for _row, text in pieces)


# LLM: 线程锁内复核同代、同登记实例，再投递 IR 和推进代次；并发 remove 不复活、晚到提交不降代。
#   多个准备可读到相同候选，但同方法同代的实际 IR 投递在原线程锁内去重，不另建持久领取表。
# 函数用途: 只为真正进入本轮 RuntimeFacts 的条目更新已带回代次，投递失败不消费资格。
def _commit_method_carry(request: _MethodCarryRequest, pieces: list) -> None:
    from ..agent_core.tool_ir_history import record_runtime_facts_turn_ir

    # LLM: 仅本地内存投递在锁内；不在此锁内读取包/pin，避免和原 TaskStore 争锁。
    # 函数用途: 排除已经带回或删除重建的条目后，提交参考资料和对应代次。
    def update(thread):
        request.authority.check()
        if thread.compact_generation != request.generation:
            return thread
        latest = methods_from(thread.conversation_methods)
        eligible = {(row["stable_id"], row["record_id"]) for row in latest if request.generation > row["carried_generation"]}
        selected = [(row, text) for row, text in pieces if (row["stable_id"], row["record_id"]) in eligible]
        if not selected or not record_runtime_facts_turn_ir(request.params, _carry_text(selected), source="conversation_method_carry"):
            return thread
        delivered = {row["stable_id"] for row, _text in selected}
        rows = tuple(dict(row, carried_generation=max(row["carried_generation"], request.generation))
                     if row["stable_id"] in delivered else row for row in latest)
        return replace(thread, conversation_methods=rows)

    request.agent.conversation_store.threads.update_atomic(request.thread_id, update)
