"""宿主业务事实的只读载体；只校验数据形状，不认证、不读写存储或授予权限。

完整任务上下文归 canonical SessionTask；运行/工作流中的值只是同次已核验快照。
构造 Python 对象或从 JSON 读回成功，不证明 owner、claim、父执行关系或当前授权。
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Literal, Self


class _BusinessDTO:
    """六个固定载体共用独立 JSON 投影，不增添持久状态或动态属性。"""

    __slots__ = ()

    def to_dict(self) -> dict[str, object]:
        return {item.name: list(value) if type(value) is tuple else value
                for item in fields(self) for value in (getattr(self, item.name),)}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        if type(value) is not dict:
            raise TypeError('业务 DTO 必须从 JSON 对象恢复')
        # 精确构造签名拒绝未知键和缺失必填键；不读取 trusted 等聊天声明。
        return cls(**value)


def _string(value: object, name: str, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if type(value) is not str:
        raise TypeError(f'{name} 必须是字符串' + ('或 None' if optional else ''))
    if not value:
        raise ValueError(f'{name} 不能为空')


def _strings(value: _BusinessDTO, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> None:
    for name in required:
        _string(getattr(value, name), name)
    for name in optional:
        _string(getattr(value, name), name, optional=True)


def _counter(value: object, name: str, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    # bool 是 int 的子类，不能把它当空间/身份修订或工具轮数。
    if type(value) is not int:
        raise TypeError(f'{name} 必须是整数' + ('或 None' if optional else ''))
    if value < 0:
        raise ValueError(f'{name} 不能为负数')


def _freeze_refs(value: _BusinessDTO, name: str) -> None:
    refs = getattr(value, name)
    if type(refs) not in (list, tuple):
        raise TypeError(f'{name} 必须是字符串数组或元组')
    for ref in refs:
        _string(ref, name)
    # JSON 数组/调用方列表只在这里复制成规范元组；元素只能是不可变 str，拒绝嵌套可变对象。
    object.__setattr__(value, name, tuple(refs))


def _enum(value: object, name: str, choices: tuple[str, ...]) -> None:
    _string(value, name)
    if value not in choices:
        raise ValueError(f'{name} 不是当前合同枚举值')


@dataclass(frozen=True, slots=True)
class HostParentExecutionRef(_BusinessDTO):
    """准确 run→AgentRun→TaskRun 父引用，不用 legacy parent_id 或 latest 推导。

    observed_attempt_id 只是可选观察；pending 父节点可无运行中 attempt。
    消费者仍须在原 DB 事务核 owner/父链，并独立经过当前调用或原调度 claim 准入。
    managed_by 组织边不属于本引用。
    """

    owner_id: str
    run_id: str
    agent_run_id: str
    task_run_id: str
    observed_attempt_id: str | None = None

    def __post_init__(self) -> None:
        _strings(self, ('owner_id', 'run_id', 'agent_run_id', 'task_run_id'), ('observed_attempt_id',))


@dataclass(frozen=True, slots=True)
class BusinessContextIntent(_BusinessDTO):
    """只保存幂等业务意图；None 默认空间与显式空间不合并。

    不含任务 ID、空间修订或当前观察。创建门先比较原意图；重放不调用 factory，
    首次才由宿主 factory 按准确预留的 stask ID 解析，意图与 context 同次 canonical 写。
    factory 只在宿主进程内使用，不进入模型 schema、Workflow JSON 或第二份任务账。
    """

    owner_id: str
    requested_space_id: str | None
    project_ref: str | None
    binding_id: str | None
    requested_method_refs: tuple[str, ...] = ()
    requested_asset_version_refs: tuple[str, ...] = ()
    schema_version: str = 'business-context-intent.v1'

    def __post_init__(self) -> None:
        _strings(self, ('owner_id', 'schema_version'), ('requested_space_id', 'project_ref', 'binding_id'))
        _freeze_refs(self, 'requested_method_refs')
        _freeze_refs(self, 'requested_asset_version_refs')


@dataclass(frozen=True, slots=True)
class BusinessExecutionContext(_BusinessDTO):
    """已冻结业务选择；task_id 是 canonical 业务任务，不是 runtime task ID。

    SessionTask 创建时必须使用其准确 stask ID；恢复不得为匹配 SQLite ID 改写。
    新业务任务空间由宿主必填，旧无空间入口保留 None；space_revision 仅是选择证据。
    TaskRun metadata/session_task_ref 与 Workflow 快照沿准确绑定保存；跨存储写回失败
    仍由原 operation 修复，不能假称跨存储原子性，也不能缺绑定即启动模型。
    同键不同意图须报冲突；同意图重放不重绑当前默认空间，副作用门仍实时核授权。
    """

    owner_id: str
    space_id: str | None
    project_ref: str | None
    task_id: str
    space_revision: int | None
    binding_id: str | None
    selected_method_refs: tuple[str, ...]
    asset_version_refs: tuple[str, ...]
    schema_version: str

    def __post_init__(self) -> None:
        _strings(self, ('owner_id', 'task_id', 'schema_version'), ('space_id', 'project_ref', 'binding_id'))
        _counter(self.space_revision, 'space_revision', optional=True)
        _freeze_refs(self, 'selected_method_refs')
        _freeze_refs(self, 'asset_version_refs')


@dataclass(frozen=True, slots=True)
class BackgroundAgentIdentity(_BusinessDTO):
    """agent_id 就是 canonical thread_id；历史与模型设置仍归原线程。

    managed_by_thread_id 只表达组织边，不写父执行身份、不计子代理深度。
    持久身份不产生空闲模型调用；退役须由原生命周期处理在途/子树/交付义务，
    unknown 副作用不得提前越过 retiring。
    """

    agent_id: str
    managed_by_thread_id: str | None
    space_id: str | None
    project_ref: str | None
    identity_state: Literal['available', 'suspended', 'retiring', 'retired']
    after_task: Literal['reuse', 'suspend', 'retire']
    identity_revision: int

    # LLM: dataclass构造及DTO反序列化自动调用；只校验必填/可空字符串、身份与收尾枚举和非负整数修订，无存储或授权副作用。
    # 函数用途: 拒绝格式错误的后台助手身份，同时允许空间、项目和管理线程为空。
    def __post_init__(self) -> None:
        _strings(self, ('agent_id',), ('managed_by_thread_id', 'space_id', 'project_ref'))
        _enum(self.identity_state, 'identity_state', ('available', 'suspended', 'retiring', 'retired'))
        _enum(self.after_task, 'after_task', ('reuse', 'suspend', 'retire'))
        _counter(self.identity_revision, 'identity_revision')


@dataclass(frozen=True, slots=True)
class TrustedTaskCompletion(_BusinessDTO):
    """宿主完成证据的运输值，不因构造成功或聊天 trusted=true 获得信任。

    W5 仅形成候选，W2 持久确认 canonical 完成、claim/completion_ref 后交 W6 复核入队。
    收尾不调用学习模型；沿原 owner/task/run/attempt/completion_ref 队列幂等身份，
    save/do_save 回复保存和学习资格独立；正式 Skill 确认/CAS 与 persona 保护不变。
    """

    owner_id: str
    thread_id: str
    task_id: str
    run_id: str
    attempt_id: str
    terminal_status: Literal['completed']
    claim_ref: str
    completion_ref: str
    space_id: str | None
    project_ref: str | None
    source_kind: str
    tool_rounds: int
    result_refs: tuple[str, ...]
    trajectory_digest: str

    def __post_init__(self) -> None:
        _strings(self, ('owner_id', 'thread_id', 'task_id', 'run_id', 'attempt_id', 'claim_ref',
                        'completion_ref', 'source_kind', 'trajectory_digest'), ('space_id', 'project_ref'))
        _enum(self.terminal_status, 'terminal_status', ('completed',))
        _counter(self.tool_rounds, 'tool_rounds')
        _freeze_refs(self, 'result_refs')


@dataclass(frozen=True, slots=True)
class WorkflowScope(_BusinessDTO):
    """范围修订与 execution_definition_ref 分离；在途绑定冻结，撤权仍实时核验。

    旧无 scope 的记录由原读取入口解释为 owner_global；新建未提交范围不得暴露成 global。
    资产回收/恢复仍归原生命周期，恢复默认 unassigned 或同操作显式范围，不新增状态库。
    """

    scope_mode: Literal['owner_global', 'spaces', 'unassigned']
    space_ids: tuple[str, ...]
    scope_revision: int

    def __post_init__(self) -> None:
        _enum(self.scope_mode, 'scope_mode', ('owner_global', 'spaces', 'unassigned'))
        _counter(self.scope_revision, 'scope_revision')
        _freeze_refs(self, 'space_ids')
        if self.scope_mode == 'spaces':
            if not self.space_ids or len(set(self.space_ids)) != len(self.space_ids):
                raise ValueError('spaces 范围必须非空且空间编号唯一')
        elif self.space_ids:
            raise ValueError('owner_global/unassigned 范围的空间集合必须为空')
