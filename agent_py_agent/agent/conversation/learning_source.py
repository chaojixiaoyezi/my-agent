# LLM: B 路学习来源的宿主构造与判定。只读结构化字段（线程来源投影、消息 metadata 里的
#   后台/调度/派活痕迹），绝不解析正文；宿主不能证明来源时一律按不可学（can_learn=false）
#   处理，不把未知来源猜成 user_dialogue。投影只保留判定与 lineage 所需字段，owner 与
#   thread/turn/request 身份在判定时由宿主上下文补全后合成冻结合同对象。
# 模块用途: 构造、解码学习来源投影，并从线程与消息推导当次有效来源事实。
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..capability.agents_learning_contract import (
    AgentsContractError,
    AgentsLearningSourceFacts,
)

# 消息 metadata 与线程记录共用的来源投影键。
LEARNING_SOURCE_KEY = "learning_source"
# v1 学习策略：只有用户对话触发、且直接来源是用户对话或带标签子代理时才有学习资格。
LEARNING_POLICY_VERSION = 1

SOURCE_USER_DIALOGUE = "user_dialogue"
SOURCE_SUBAGENT = "subagent"
SOURCE_DISPATCH = "dispatch"
SOURCE_SCHEDULE = "schedule"
SOURCE_BACKGROUND = "background"
SOURCE_UNKNOWN = "unknown"

TRIGGER_USER_DIALOGUE = "user_dialogue"

# 宿主写入的结构化痕迹键；只认这些键，不从正文、role 或时间推断来源。
_BACKGROUND_DELIVERY_KEYS = ("background_delivery_reason",)
_SCHEDULER_METADATA_KEYS = ("scheduler_run_id", "scheduler_job_id")
# conversation_session_task_id 由会话运行时按派活唤醒信封写入运行参数（attributes 级），
# 消息 metadata 不带该键；并入后派活回合的 spawn 判定与消息级判定保持同一语义。
_DISPATCH_METADATA_KEYS = (
    "session_task_id",
    "session_task_run_id",
    "session_message_run_id",
    "conversation_session_task_id",
)
_BACKGROUND_WAKE_METADATA_KEYS = ("background_wake_signal_id", "wake_signal_id")

# 投影不携带身份；合成完整事实时用占位身份通过合同的非空校验，真实身份由调用方提供。
_PLACEHOLDER_OWNER = "learning-source-projection"
_PLACEHOLDER_TEXT = "unknown"

# 投影固定字段集；与 AgentsLearningSourceFacts 的判定/lineage 字段一一对应。
_PROJECTION_FIELDS = (
    "schema_version",
    "source_kind",
    "trigger_kind",
    "can_learn",
    "subagent_tag",
    "source_run_ids",
    "source_task_ids",
    "policy_version",
)


# LLM: 投影校验复用冻结合同，不复制第二套规则；can_learn=true 与来源不匹配时合同直接拒绝。
# 函数用途: 用占位身份构造并校验一份来源投影，非法组合抛出 AgentsContractError。
def _projection(
    source_kind: str,
    trigger_kind: str,
    can_learn: bool,
    lineage: Mapping[str, object] | None,
) -> dict[str, object]:
    lineage = lineage if isinstance(lineage, Mapping) else {}
    facts = AgentsLearningSourceFacts(
        schema_version=1,
        owner=_PLACEHOLDER_OWNER,
        source_kind=str(source_kind or ""),
        trigger_kind=str(trigger_kind or ""),
        can_learn=bool(can_learn),
        subagent_tag=str(lineage.get("subagent_tag") or ""),
        source_run_ids=tuple(str(item) for item in lineage.get("source_run_ids") or ()),
        source_task_ids=tuple(str(item) for item in lineage.get("source_task_ids") or ()),
        thread_id=_PLACEHOLDER_TEXT,
        turn_id=_PLACEHOLDER_TEXT,
        request_id=_PLACEHOLDER_TEXT,
        policy_version=LEARNING_POLICY_VERSION,
    )
    AgentsLearningSourceFacts.from_dict(facts.to_dict(), _PLACEHOLDER_OWNER)
    return {field: facts.to_dict()[field] for field in _PROJECTION_FIELDS}


# LLM: 普通用户线程的唯一合法投影；创建线程/请求上下文时由宿主写入，不从消息 role 推断。
# 函数用途: 构造用户对话来源投影（可学）。
def user_dialogue_projection() -> dict[str, object]:
    return _projection(SOURCE_USER_DIALOGUE, TRIGGER_USER_DIALOGUE, True, None)


# LLM: 子代理可学但必须打标签；trigger_kind 是当次有效触发，自动化祖先的禁止来源向后代继承。
# 函数用途: 构造子代理来源投影；仅当触发仍是用户对话时可学。
def subagent_projection(
    *,
    trigger_kind: str,
    run_ids: Sequence[str],
    task_ids: Sequence[str] = (),
) -> dict[str, object]:
    return _projection(
        SOURCE_SUBAGENT,
        trigger_kind,
        str(trigger_kind or "") == TRIGGER_USER_DIALOGUE,
        {
            "subagent_tag": "subagent",
            "source_run_ids": run_ids,
            "source_task_ids": task_ids,
        },
    )


# LLM: 派活、定时、后台与未知来源一律不可学；这些投影保留 false 事实用于追踪，不授予学习。
# 函数用途: 构造不可学的派活来源投影。
def dispatch_projection() -> dict[str, object]:
    return _projection(SOURCE_DISPATCH, SOURCE_DISPATCH, False, None)


# 函数用途: 构造不可学的定时来源投影。
# LLM: 定时任务的宿主来源始终不可学习；改变时同步scheduler唤醒与后代来源回归，不允许模型覆盖。
# 函数用途: 构造定时任务来源，保留明确的不可学标志。
def schedule_projection() -> dict[str, object]:
    return _projection(SOURCE_SCHEDULE, SOURCE_SCHEDULE, False, None)


# 函数用途: 构造不可学的后台来源投影。
# LLM: 后台通知的宿主来源始终不可学习；不得因消息role=user恢复用户来源资格。
# 函数用途: 构造后台执行的不可学来源投影。
def background_projection() -> dict[str, object]:
    return _projection(SOURCE_BACKGROUND, SOURCE_BACKGROUND, False, None)


# 函数用途: 构造不可学的未知来源投影（fail-closed 的默认值）。
# LLM: 未知或损坏来源一律保持不可学；不能依据正文、角色或历史缺字段推测授权。
# 函数用途: 返回来源不可确认时的保守默认投影。
def unknown_projection() -> dict[str, object]:
    return _projection(SOURCE_UNKNOWN, SOURCE_UNKNOWN, False, None)


# LLM: 引用 id（turn/request）由宿主上下文提供，模型候选不能提供或替换；refs 缺省为空占位。
# 类用途: projection_to_facts 的结构化引用 bundle，替代 **kwargs 服务接口。
@dataclass(frozen=True)
class LearningSourceRefs:
    turn_id: str = ""
    request_id: str = ""


# LLM: 身份只在合成时补全；owner 由调用方独立提供，模型候选不能提供或替换该事实。
# 函数用途: 把投影合成完整来源事实并做合同校验。
def projection_to_facts(
    projection: Mapping[str, object],
    *,
    owner: str,
    thread_id: str,
    refs: LearningSourceRefs | None = None,
) -> AgentsLearningSourceFacts:
    if not isinstance(projection, Mapping):
        raise AgentsContractError("AGENTS_CONTRACT_OBJECT_REQUIRED", LEARNING_SOURCE_KEY)
    turn_id = str(refs.turn_id or "") if refs is not None else ""
    request_id = str(refs.request_id or "") if refs is not None else ""
    payload = {
        "schema_version": projection.get("schema_version"),
        "owner": owner,
        "source_kind": projection.get("source_kind"),
        "trigger_kind": projection.get("trigger_kind"),
        "can_learn": projection.get("can_learn") is True,
        "subagent_tag": projection.get("subagent_tag", ""),
        "source_run_ids": list(projection.get("source_run_ids") or []),
        "source_task_ids": list(projection.get("source_task_ids") or []),
        "thread_id": str(thread_id or "") or _PLACEHOLDER_TEXT,
        "turn_id": turn_id or _PLACEHOLDER_TEXT,
        "request_id": request_id or _PLACEHOLDER_TEXT,
        "policy_version": projection.get("policy_version"),
    }
    return AgentsLearningSourceFacts.from_dict(payload, owner)


# LLM: 读侧遇到坏投影或旧记录缺字段时按不可学处理（fail-closed），不猜测、不升级来源。
# 函数用途: 判断一份投影是否合法且可学。
def projection_is_learnable(projection: object) -> bool:
    if not isinstance(projection, Mapping):
        return False
    try:
        facts = projection_to_facts(
            projection,
            owner=_PLACEHOLDER_OWNER,
            thread_id=_PLACEHOLDER_TEXT,
        )
    except AgentsContractError:
        return False
    return facts.can_learn


# LLM: 候选要证明来源，必须它引用的消息全部可学；没有可验证引用时不猜 user（保守关闭）。
# 函数用途: 判断一组消息来源事实是否全部具备学习资格。
def all_sources_learnable(facts_list: Sequence[AgentsLearningSourceFacts]) -> bool:
    return bool(facts_list) and all(facts.can_learn for facts in facts_list)


# LLM: Bot线程禁学先于任何消息投影；自动化痕迹仍收紧显式可学来源，不能由page_context或消息false覆盖。
# 函数用途: 从宿主线程与消息推导来源，只读Bot即使携带显式user来源也不可学。
def facts_for_message(
    metadata: Mapping[str, object],
    *,
    owner: str,
    thread_id: str,
    thread_source: Mapping[str, object] | None = None,
) -> AgentsLearningSourceFacts:
    meta = metadata if isinstance(metadata, Mapping) else {}
    turn_id = str(meta.get("conversation_request_id") or "")
    if bot_learning_excluded(thread_source) or bot_learning_excluded(meta):
        return projection_to_facts(bot_excluded_projection(), owner=owner, thread_id=thread_id,
                                   refs=LearningSourceRefs(turn_id=turn_id, request_id=turn_id))
    derived = _derived_projection(meta)
    explicit = meta.get(LEARNING_SOURCE_KEY)
    if explicit is not None:
        explicit_facts = _facts_from_explicit(explicit, owner=owner, thread_id=thread_id, turn_id=turn_id)
        if derived is None or not explicit_facts.can_learn:
            # 明确不可学的类别和子代理谱系原样保留；自动化痕迹只收紧可学投影。
            return explicit_facts
    if derived is None and isinstance(thread_source, Mapping):
        derived = thread_source
    if derived is None:
        derived = unknown_projection()
    refs = LearningSourceRefs(turn_id=turn_id, request_id=turn_id)
    try:
        return projection_to_facts(derived, owner=owner, thread_id=thread_id, refs=refs)
    except AgentsContractError:
        return projection_to_facts(
            unknown_projection(), owner=owner, thread_id=thread_id, refs=refs
        )


# 函数用途: 显式投影分支——合法投影直接解码；损坏投影 fail-closed 为未知，不回退线程默认。
# LLM: 显式来源存在即占优；坏投影必须返回unknown，禁止退回线程旧来源洗白当前自动化消息。
# 函数用途: 把宿主显式投影解码成消息来源事实，损坏时保守关闭。
def _facts_from_explicit(
    explicit: object, *, owner: str, thread_id: str, turn_id: str
) -> AgentsLearningSourceFacts:
    refs = LearningSourceRefs(turn_id=turn_id, request_id=turn_id)
    if isinstance(explicit, Mapping):
        try:
            return projection_to_facts(explicit, owner=owner, thread_id=thread_id, refs=refs)
        except AgentsContractError:
            pass
    return projection_to_facts(
        unknown_projection(), owner=owner, thread_id=thread_id, refs=refs
    )


# LLM: 只按宿主写入的结构化键判定；键存在即表示该回合来自对应自动化来源，不读正文或 role。
# 函数用途: 从消息 metadata 的后台/调度/派活痕迹推导来源投影。
def _derived_projection(meta: Mapping[str, object]) -> dict[str, object] | None:
    if any(meta.get(key) for key in _BACKGROUND_DELIVERY_KEYS):
        return background_projection()
    if any(meta.get(key) for key in _SCHEDULER_METADATA_KEYS):
        return schedule_projection()
    if any(meta.get(key) for key in _DISPATCH_METADATA_KEYS):
        return dispatch_projection()
    if any(meta.get(key) for key in _BACKGROUND_WAKE_METADATA_KEYS):
        return background_projection()
    return None


# LLM: 保留宿主管理的Bot禁学投影供现有thread.learning_source读者消费；坏普通投影仍返回None。
# 函数用途: 解码线程来源，不在规范化过程中丢掉Bot硬禁学标记。
def thread_source_projection(thread_source: object) -> dict[str, object] | None:
    if not isinstance(thread_source, Mapping):
        return None
    if bot_learning_excluded(thread_source):
        return bot_excluded_projection()
    try:
        facts = projection_to_facts(
            thread_source, owner=_PLACEHOLDER_OWNER, thread_id=_PLACEHOLDER_TEXT
        )
    except AgentsContractError:
        return None
    return {field: facts.to_dict()[field] for field in _PROJECTION_FIELDS}


# LLM: 唤醒信封（scheduler/派活/后台）由宿主写入投影；runtime 只做透传，不从提示词猜来源。
# 函数用途: 从唤醒信封 metadata 里读取宿主写入的来源投影。
def projection_from_wake_metadata(wake_metadata: Mapping[str, object]) -> dict[str, object] | None:
    if not isinstance(wake_metadata, Mapping):
        return None
    return thread_source_projection(wake_metadata.get(LEARNING_SOURCE_KEY))


# LLM: 候选来源=其全部引用消息；任一引用不可学即整体不可学；全部可学时确定性合并所有子代理
#   标签与 run/task 关联（顺序无关、去重），无子代理事实时保留首条投影；不升级、不洗白。
# 函数用途: 把一组消息来源事实合并成候选级来源投影。
def merge_message_sources(facts_list: Sequence[AgentsLearningSourceFacts]) -> dict[str, object]:
    if not facts_list:
        return unknown_projection()
    for facts in facts_list:
        if not facts.can_learn:
            return _projection_fields(facts)
    return _merged_learnable_projection(facts_list)


# 函数用途: 合并全部可学来源的子代理 lineage；无子代理事实时保留首条投影。
# LLM: 只接收已校验全部可学的来源；lineage合并去重，不提升来源资格或制造用户批准。
# 函数用途: 合并可学消息的子代理运行与任务关联。
def _merged_learnable_projection(
    facts_list: Sequence[AgentsLearningSourceFacts],
) -> dict[str, object]:
    run_ids: list[str] = []
    task_ids: list[str] = []
    has_subagent = False
    for facts in facts_list:
        if facts.source_kind == SOURCE_SUBAGENT or facts.source_run_ids or facts.source_task_ids:
            has_subagent = True
        run_ids.extend(str(item).strip() for item in facts.source_run_ids or ())
        task_ids.extend(str(item).strip() for item in facts.source_task_ids or ())
    if not has_subagent:
        return _projection_fields(facts_list[0])
    return subagent_projection(
        trigger_kind=str(facts_list[0].trigger_kind or ""),
        run_ids=list(dict.fromkeys(text for text in run_ids if text)),
        task_ids=list(dict.fromkeys(text for text in task_ids if text)),
    )


# 函数用途: 只保留投影合同的稳定字段，供合并与持久化复用。
# LLM: 仅复制共享合同稳定字段；不得把模型内容或额外权限字段传播进候选来源。
# 函数用途: 提取用于候选持久化和合并的来源投影。
def _projection_fields(facts: AgentsLearningSourceFacts) -> dict[str, object]:
    return {field: facts.to_dict()[field] for field in _PROJECTION_FIELDS}


# LLM: 候选级合并不丢弃坏类型（包括显式 None）；坏类型与坏投影均降为未知，
#   不能让另一份可学来源替它授权。空字典仅保留人工入口的无投影语义；引用覆盖由候选仓库核对。
#   全部有效时合并子代理 lineage，显式不可学优先；参数使用显式序列而非可变服务接口。
# 函数用途: 合并来源投影并保守关闭非法输入，不替未知来源补猜权限。
def merge_source_projections(projections: Sequence[object]) -> dict[str, object]:
    if any(not isinstance(item, Mapping) for item in projections):
        return unknown_projection()
    present = [item for item in projections if item]
    if not present:
        return {}
    facts_list: list[AgentsLearningSourceFacts] = []
    for item in present:
        try:
            facts_list.append(
                projection_to_facts(item, owner=_PLACEHOLDER_OWNER, thread_id=_PLACEHOLDER_TEXT)
            )
        except AgentsContractError:
            return unknown_projection()
    return merge_message_sources(facts_list)


# LLM: 子代理标签与 lineage 从父级事实继承：trigger 取父级有效触发（user_dialogue→可学，
#   定时/派活/后台→不可学且沿后代传播）；父级已是子代理时累积其 run/task 关联。
#   父级缺失或坏投影返回 None（调用方按不可学处理），绝不合成 user_dialogue。
# 函数用途: 由父级来源事实合成子代理自己的来源投影（subagent 标签）。
def child_projection_from_parent(
    parent: object,
    *,
    run_id: str,
    task_id: str = "",
) -> dict[str, object] | None:
    facts = thread_source_projection(parent)
    child_run_id = str(run_id or "").strip()
    trigger = str(facts.get("trigger_kind") or "").strip() if facts else ""
    if facts is None or not child_run_id or not trigger:
        return None
    run_ids: list[str] = []
    task_ids: list[str] = []
    if facts.get("source_kind") == SOURCE_SUBAGENT:
        run_ids.extend(str(item).strip() for item in facts.get("source_run_ids") or ())
        task_ids.extend(str(item).strip() for item in facts.get("source_task_ids") or ())
    run_ids.append(child_run_id)
    if child_task_id := str(task_id or "").strip():
        task_ids.append(child_task_id)
    return subagent_projection(
        trigger_kind=trigger,
        run_ids=list(dict.fromkeys(text for text in run_ids if text)),
        task_ids=list(dict.fromkeys(text for text in task_ids if text)),
    )


# LLM: Bot禁学优先于自动化痕迹和显式投影，后代沿既有来源继承；普通未知来源仍失败关闭。
# 函数用途: 从唤醒信封或运行参数推导来源，禁止只读Bot恢复学习资格。
def derive_source_projection(fields: object) -> dict[str, object] | None:
    attrs = fields if isinstance(fields, Mapping) else {}
    if bot_learning_excluded(attrs):
        return bot_excluded_projection()
    derived = _derived_projection(attrs)
    if derived is not None:
        return derived
    explicit = attrs.get(LEARNING_SOURCE_KEY)
    if explicit is None:
        return None
    return thread_source_projection(explicit)


# LLM: 只读结构化宿主字段；嵌套仅限线程metadata/learning_source，不解析正文或page_context。
# 函数用途: 判断线程或来源投影是否具有不可覆盖的Bot禁学标记。
def bot_learning_excluded(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    sources = [value, *(item for key in ("metadata", "learning_source")
                        if isinstance(item := value.get(key), Mapping))]
    return any(item.get("bot_mode") == "query_readonly" or item.get("learning_excluded") is True
               or item.get("reason") == "bot_query_readonly_excluded" for item in sources)


# LLM: 原冻结facts没有reason字段，使用明确不可学source_kind并在来源投影附reason，不修改共享合同。
# 函数用途: 构造只读Bot的固定禁学来源，保留可机器核对的原因。
def bot_excluded_projection() -> dict[str, object]:
    return {**_projection("bot_query_readonly_excluded", "bot_query_readonly_excluded", False, None),
            "reason": "bot_query_readonly_excluded", "bot_mode": "query_readonly", "learning_excluded": True}


# LLM: spawn 边界复用同一推导（痕迹优先>显式投影>None），保证父级来源判定与消息级一致。
# 函数用途: 从当次运行参数判定子代理 spawn 的父级来源事实。
def spawn_source_projection(run_attributes: object) -> dict[str, object] | None:
    return derive_source_projection(run_attributes)


__all__ = [
    "LEARNING_SOURCE_KEY",
    "LEARNING_POLICY_VERSION",
    "SOURCE_USER_DIALOGUE",
    "SOURCE_SUBAGENT",
    "SOURCE_DISPATCH",
    "SOURCE_SCHEDULE",
    "SOURCE_BACKGROUND",
    "SOURCE_UNKNOWN",
    "TRIGGER_USER_DIALOGUE",
    "all_sources_learnable",
    "background_projection",
    "child_projection_from_parent",
    "derive_source_projection",
    "dispatch_projection",
    "facts_for_message",
    "merge_message_sources",
    "merge_source_projections",
    "projection_from_wake_metadata",
    "projection_is_learnable",
    "projection_to_facts",
    "schedule_projection",
    "spawn_source_projection",
    "subagent_projection",
    "thread_source_projection",
    "unknown_projection",
    "user_dialogue_projection",
]
