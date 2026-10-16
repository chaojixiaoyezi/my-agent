# LLM: 目录只保存产品领域声明；投影每次从原权威读，不缓存库存、不建状态库，身份和工具面由宿主传入。
# 模块用途: 提供自我目录登记、只读上下文和分页来源合同，新领域只需登记 reader。
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..runtime_context import current_subagent_run_id
from ..user_space.owner_access import is_complete_local_admin_owner
from .models import ToolInvocationContext, ToolRuntimeSnapshot
from .self_context import tool_self_context

# 固定未知观测说明；它不是环境分类或权限判断的输入。
SELF_MAP_UNKNOWN_OBSERVATION = "未观测，不能推断为任何一种环境。"


# LLM: DTO 附数量、显示标签及权威位置摘要；只携带原读侧事实，聚合器不能按编号猜领域业务。
# 类用途: 携带一次查询的公共条目、数量和位置；partial 不能冒充查全。
@dataclass(frozen=True)
class SectionProjection:
    items: tuple[dict[str, Any], ...]
    completeness: str = "complete"
    state: str = "known"
    errors: tuple[str, ...] = ()
    source: str = ""
    count_known: bool = True
    counts: dict[str, int | None] = field(default_factory=dict)
    count_reasons: dict[str, str] = field(default_factory=dict)
    locations: tuple[dict[str, Any], ...] = ()
    count_reason: str = ""
    summary: str = ""
    count_labels: dict[str, str] = field(default_factory=dict)
    title: str = ""
    list_mode: bool = True


# LLM: 仅注册 ID 不存在抛此类；参数类型、item 和读源损坏仍沿各自错误合同。
# 类用途: 让工具在不读取任何领域的情况下返回合法编号和总览入口。
class SelfMapSectionUnknown(ValueError):
    pass


# LLM: ID、静态用途与列表形态由原领域登记；schema从此表生成，不接受外部插件回调或第二份编号枚举。
# 类用途: 声明一个领域的标题、顺序和原权威读取器。
@dataclass(frozen=True)
class SectionDescriptor:
    id: str
    title: str
    order: int
    reader: Callable[[SelfMapReadContext], SectionProjection]
    purpose: str = ""
    list_mode: bool = True


# LLM: 冻结一次调用的原工具快照和实际展示投影；child 管理资格不得继承本机主 Agent 的身份。
# 类用途: 给各领域提供可信 owner、精确线程、工具和调用工作区，模型不能填写这些字段。
@dataclass(frozen=True)
class SelfMapReadContext:
    agent: Any
    invocation: ToolInvocationContext
    visible_names: frozenset[str]
    child: bool = False
    thread_id: str = ""
    context_scope: str = "default"
    public_context: dict[str, Any] = field(default_factory=dict)

    # LLM: 唯一完整本机身份判据复用原 owner_access；子代理即使共享 home 也没有管理库存资格。
    # 函数用途: 判断本次目录查询是否允许管理员库存。
    @property
    def admin(self) -> bool:
        return (not self.child and self.context_scope not in {"isolated", "task_local", "control_plane"}
                and is_complete_local_admin_owner(getattr(self.agent, "home_paths", None)))

    # LLM: 快照就是 execute_scoped 所用对象，禁止退回进程全集补齐权限。
    # 函数用途: 读取本次调用的授权工具面。
    @property
    def snapshot(self) -> ToolRuntimeSnapshot:
        return self.invocation.runtime_snapshot

    # LLM: 工具状态沿 main 本人公开读口，精确 child thread 不回退父线程；来源只读，不改变加载账或权限。
    # 函数用途: 从已绑定参数构造一次只读查询上下文，不刷新服务或创建线程。
    @classmethod
    def for_call(cls, agent: Any, invocation: ToolInvocationContext) -> SelfMapReadContext:
        params = getattr(agent, "_current_run_params", None)
        attrs = getattr(params, "task_attributes", None) or {}
        attrs = attrs if isinstance(attrs, dict) else {}
        public = tool_self_context(agent, invocation.runtime_snapshot, invocation)
        names = public["discovery"]["model_schema_tools"]
        child = bool(current_subagent_run_id(agent) or attrs.get("parent_run_id") or attrs.get("parent_id") or getattr(params, "parent_run_id", ""))
        child = child or bool(attrs.get("agent_thread_id") and attrs.get("agent_thread_id") != attrs.get("conversation_thread_id"))
        thread = attrs.get("agent_thread_id") if child else public["identity"]["thread_id"]
        return cls(agent, invocation, frozenset(names), child, str(thread or ""),
                   str(getattr(params, "context_scope", "default") or "default"), public)


# LLM: 声明仅在装配期变更；枚举不执行未登记代码，不维护固定领域枚举或第二份状态。
# 类用途: 接收各领域的目录贡献并按稳定顺序提供查询。
class SelfMapRegistry:
    # LLM: 构造不读盘或写盘，声明对象只活在原 Agent 生命周期中。
    # 函数用途: 建立空的领域登记表。
    def __init__(self) -> None:
        self._sections: dict[str, SectionDescriptor] = {}

    # LLM: 重复 ID 是产品装配错误；新领域必须有可调用读者，不允许覆盖已登记权威。
    # 函数用途: 在领域装配时登记一个只读贡献。
    def register(self, descriptor: SectionDescriptor) -> None:
        if not descriptor.id or descriptor.id in self._sections or not callable(descriptor.reader):
            raise ValueError("invalid or duplicate self_map section")
        self._sections[descriptor.id] = descriptor

    # LLM: 排序只依赖稳定声明，不依赖用户内容或加载时机。
    # 函数用途: 枚举本 Agent 的全部已登记领域。
    def descriptors(self) -> tuple[SectionDescriptor, ...]:
        return tuple(sorted(self._sections.values(), key=lambda value: (value.order, value.id)))

    # LLM: 未知 ID 用独立参数错交工具恢复；不猜同义词、不自动查询总览、不执行 reader。
    # 函数用途: 定位精确读取器，错编号由调用入口列出全部合法 ID。
    def get(self, section: str) -> SectionDescriptor:
        if section not in self._sections:
            raise SelfMapSectionUnknown("unknown self_map section")
        return self._sections[section]


# LLM: next_read 是原样可调用对象；是否当前展开由工具调度器最终复核，不支持路径 fallback。
# 函数用途: 构造一个固定工具及参数的读建议。
def next_read(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"tool": tool, "arguments": arguments}


# LLM: 字段先由领域白名单准入；只识别明确状态字段，数量未知另留原因，不把自由文本当状态。
# 函数用途: 创建公共条目并给顶层/嵌套未观测状态固定说明，保持原调用状态。
def map_item(identity: str, name: str, *, state: str = "known", fields: dict[str, Any] | None = None) -> dict[str, Any]:
    notice = unknown_observation_notice(state, fields)
    if fields and "count" in fields and fields["count"] is None:
        notice["count_reason"] = "未提供不读取正文的完整计数接口。"
    return {"id": identity, "name": name, "state": state, "summary": "", **notice, **(fields or {})}


# LLM: 只接收已经准入的公共状态；不重新分类、不读取环境或正文，投影和条目共用固定字节。
# 函数用途: 对明确未观测状态加说明，不覆盖已知可调用状态。
def unknown_observation_notice(state: str, fields: dict[str, Any] | None = None) -> dict[str, str]:
    unknown = state in {"unknown", "not_observed", "not_verified"} or _has_unknown_state(fields)
    return {"unknown_reason": SELF_MAP_UNKNOWN_OBSERVATION} if unknown else {}


# LLM: 仅遍历公开元信息的字典/列表；状态键与自由文本分开，unknown文字不产生环境判断。
# 函数用途: 发现嵌套面板/事件等明确未知状态，不更改其值。
def _has_unknown_state(value: Any) -> bool:
    if isinstance(value, (tuple, list)):
        return any(_has_unknown_state(item) for item in value)
    if not isinstance(value, dict):
        return False
    keys = {"state", "mounted", "executed", "provider_kind", "remote_call", "sandbox_observed", "external_network"}
    return any(_has_unknown_state(item) or (isinstance(item, str) and (key in keys or key.endswith("_state"))
               and item in {"unknown", "not_observed", "not_verified"}) for key, item in value.items())
