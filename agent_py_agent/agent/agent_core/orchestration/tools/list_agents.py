"""当前代理树的只读模型工具，返回状态与真实产物引用，不暴露界面恢复细节。"""

# LLM: This module is the only model-visible read adapter for the canonical
# agent-tree projection. Keep lifecycle mutations in create/guidance/cancel and
# do not add a second status store or polling loop here.
# 模块用途: 让主代理或子代理只读查看自己的代理树状态，不承担推进、唤醒或取消。

from __future__ import annotations

import json
import os
import time
from collections import OrderedDict
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from ....tooling.models import (
    BaseTool,
    ConcurrencyPolicy,
    EffectResolverPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolInvocationContext,
    ToolRuntimePolicy,
)
from ....workspace_read_context import WorkspaceReadContext
from ...agent_tree.model_view import (
    agent_tree_model_payload,
    agent_tree_model_preview,
    agent_tree_progress_observation,
)
from ...agent_tree.status import agent_tree_status_payload
from ..progress_watch import DEFAULT_PROGRESS_CHECK_CHILD_LIMIT_COUNT, ProgressReadContext
from ..tool_specs import build_list_agents_model_spec

if TYPE_CHECKING:
    from ....core import SimpleAgent


# LLM: list_agents delegates every identity/scope decision to
# agent_tree_status_payload. The optional run_id narrows an owner-scoped query;
# descendants still cannot escape their own subtree because the canonical
# projection resolves current_subagent_run_id before model parameters.
# 类用途: 返回当前任务代理树或一个已知 run 的子树快照，供模型回答“谁还在跑”。
class ListAgentsTool(BaseTool):
    model_spec = build_list_agents_model_spec()
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("read_only"),
        concurrency_policy=ConcurrencyPolicy("parallel_safe"),
        resource_scopes=ResourceScopePolicy(
            mode="declared",
            static_scopes=("current_agent_tree",),
        ),
        promotes_task=False,
        mutates_workspace=False,
    )

    # LLM: Keep only the owner-bound agent reference; callers cannot inject a
    # manager, owner root, or filesystem path through model parameters.
    # 函数用途: 绑定当前代理的 owner 范围和规范子代理管理器。
    def __init__(self, agent: SimpleAgent) -> None:
        self.agent = agent

    # LLM: 身份裁决仍由 canonical tree 完成；原始结果保留，稳定进展元数据只接既有软观察，不判死或取消。
    #   快照构造前的单调时钟起点一并传给进展上下文，使快照/仓库校验/关联读取与 git 观察共用同一总预算。
    # 函数用途: 读取状态与产物引用，并排除仅耗时变化的假进展；不改等待、权限或生命周期状态。
    def execute_scoped(self, params: dict[str, object], context: ToolInvocationContext) -> ToolHandlerOutcome:
        return self._execute(params, context.workspace_read_context)

    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        return self._execute(params, None)

    def _execute(self, params: dict[str, object], read_context: WorkspaceReadContext | None) -> ToolHandlerOutcome:
        started_monotonic = time.monotonic()
        run_id = str(params.get("run_id") or "").strip()
        query = {"run_id": run_id, "scope": "own_subtree"} if run_id else {}
        snapshot = agent_tree_status_payload(self.agent, query)
        context = progress_read_context(self.agent, snapshot, started_monotonic=started_monotonic, read_context=read_context)
        payload = agent_tree_model_payload(snapshot, progress_context=context)
        return ToolHandlerOutcome(
            self.model_spec.name,
            True,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            result_envelope={
                "tool_output_policy": {"live_prompt_output": agent_tree_model_preview(payload)},
                "progress_observation": agent_tree_progress_observation(snapshot, payload),
            },
        )


# LLM: 进展观察缓存上限（条）；只保存上一次调用里仍是 exact 的 (run, 仓库) 样本，配置代次一变整表作废；只作比较
#   样本，不是生命周期或授权事实。
# 常量用途: 限制挂在 agent 上的上一次 git 观察缓存条数。
_MAX_GIT_PREVIOUS_ENTRIES_COUNT = 128
# LLM: 完整授权视图做仓库独占关联时的节点数上限（条数上限，不是硬墙钟/CPU 上界；耗时由共享 git 观察截止约束）。
#   一旦视图不完整（非根子树、带 warnings/scope_warnings 或节点数超过本上限），立即短路、0 次读取任务记录，全部记为 unproven。
# 常量用途: 一次完整视图做仓库独占关联允许的最多节点数。
_MAX_ASSOCIATION_NODES_COUNT = 256
# 常量用途: 一次 list_agents 读工具审批账的总时长（所有根共用一个截止）。
_APPROVAL_READ_BUDGET_SECONDS = 1.0
# 常量用途: 每个根一次审批目录扫描最多看的条目数；没扫完就记为读不出，不当作没有审批。
_APPROVAL_SCAN_LIMIT_COUNT = 128


# LLM: ORCH03-R1/R2/R3/S3-budget（PRO-MSG-024/038）：只从已授权快照与宿主事实构造冻结上下文，不接受模型参数。配置读不出或子代理
#   上限非法时整次不分类（全部 unknown），不退化成不限；只对前 child_limit 个节点读必需来源。git 观察默认关：开关
#   不是 True 时不校验仓库、不启动任何 git 进程；开启时全视图关联与已分类目标观察分离，共用 started_monotonic 起的单调截止。
# 函数用途: 为一次 list_agents 构造进展读取上下文。
def progress_read_context(
    agent: object,
    snapshot: dict[str, object],
    *,
    started_monotonic: float | None = None,
    read_context: WorkspaceReadContext | None = None,
) -> ProgressReadContext:
    from ....capability import runtime_config_reload
    from ....capability.config import CapabilityConfig
    from ..git_progress_adapter import GIT_OBSERVATION_BUDGET_SECONDS

    started = time.monotonic() if started_monotonic is None else started_monotonic
    now = time.time()
    try:
        config, refusal = runtime_config_reload.capability_config_with_trusted_source(agent)
    except Exception:  # noqa: BLE001 - 配置读不出按整次不分类处理
        config, refusal = None, "CONFIG_UNREADABLE"
    if not isinstance(config, CapabilityConfig):
        # 坏行、读不到来源都和读不出一样：整次不分类，不拿默认值冒充用户配置；上一次样本同时作废。
        _reset_previous(agent)
        return ProgressReadContext(now=now, child_limit=0, config_error=f"PROGRESS_{refusal or 'CONFIG_UNREADABLE'}")
    limit = config.orchestration_progress_check_child_limit
    if type(limit) is not int or limit < 0:
        _reset_previous(agent)
        return ProgressReadContext(now=now, child_limit=0, config_error="PROGRESS_CHILD_LIMIT_INVALID")
    limit = limit or DEFAULT_PROGRESS_CHECK_CHILD_LIMIT_COUNT
    visible = [node for node in snapshot.get("nodes") or [] if isinstance(node, dict)]
    classified = visible[:limit]
    sources = _node_sources(agent, classified, deadline=started + GIT_OBSERVATION_BUDGET_SECONDS)
    if config.orchestration_git_progress_enabled is True:
        # ORCH03-R2-a / S3-budget：独占关联看完整已授权视图，但只对进入本次分类窗口的 exact 目标执行 git 观察。
        classified_ids = {run_id for node in classified if (run_id := str(node.get("run_id") or ""))}
        git = _git_facts(
            agent,
            config,
            snapshot,
            visible,
            classified_ids=classified_ids,
            started_monotonic=started,
            read_context=read_context,
        )
    else:
        # 关掉即作废上一次样本：重新打开后第一次观察没有比较对象，不会拿关之前的旧样本说“有变化”。
        git = {}
        _reset_previous(agent)
    return ProgressReadContext(now=now, child_limit=limit,
                               sources=MappingProxyType({key: MappingProxyType(value) for key, value in sources.items()}),
                               git=MappingProxyType(git))


# LLM: 只给前 child_limit 个节点读必需来源；审批账每个根一次、共用一个截止。runtime_read 取“节点已在授权快照里”
#   这一事实，其它记录的加载错误仍在快照 warnings 里。不写任何状态。
# 函数用途: 汇总受限节点集合的必需来源事实。
def _node_sources(agent: object, nodes: list[dict[str, object]], *, deadline: float | None = None) -> dict[str, dict[str, dict[str, object]]]:
    from ...agent_tree.node_rendering import progress_source_facts
    from ..coordination_progress import coordination_source_facts

    coordination = coordination_source_facts(agent, nodes, deadline=deadline)
    approvals = _approval_counts(agent, {str(node.get("root_id") or "") for node in nodes})
    result: dict[str, dict[str, dict[str, object]]] = {}
    for node in nodes:
        run_id = str(node.get("run_id") or "")
        if not run_id:
            continue
        facts = progress_source_facts(agent, run_id)
        facts.update(coordination.get(run_id, {}))
        root = str(node.get("root_id") or "")
        state = approvals.get(root)
        facts["tool_approvals"] = ({"state": "not_applicable"} if not root else
                                   {"state": "read", "pending": state.get(run_id, 0)}
                                   if isinstance(state, dict) else {"state": "unreadable"})
        # 节点出现在已授权快照里，说明它自己的运行记录已读到；其它记录的加载错误在快照 warnings 里如实保留。
        facts["runtime_read"] = {"state": "read"}
        result[run_id] = facts
    return result


# LLM: 每个根只读一次规范审批账；没读完（锁忙、超时、容量未知）或报错都记为读不出（None），不当作没有审批。
# 函数用途: 统计各根下每个 run 的待处理工具审批数。
def _approval_counts(agent: object, roots: set[str]) -> dict[str, dict[str, int] | None]:
    from ....conversation.agent_tool_approval import list_pending_agent_tool_approvals_report

    deadline = time.monotonic() + _APPROVAL_READ_BUDGET_SECONDS
    counts: dict[str, dict[str, int] | None] = {}
    for root in sorted(item for item in roots if item):
        try:
            rows, complete = list_pending_agent_tool_approvals_report(
                agent, root_task_id=root, deadline=deadline, scan_limit=_APPROVAL_SCAN_LIMIT_COUNT)
        except Exception:  # noqa: BLE001 - 审批账读不出时只能说不知道
            counts[root] = None
            continue
        if not complete:
            counts[root] = None
            continue
        per_run: dict[str, int] = {}
        for row in rows:
            run_id = str(row.get("run_id") or "") if isinstance(row, dict) else ""
            if run_id:
                per_run[run_id] = per_run.get(run_id, 0) + 1
        counts[root] = per_run
    return counts


# LLM: 只在开关为 True 时调用。ORCH03-R2-a / S3-budget（PRO-MSG-038）：先判视图头部是否具备完整独占证明条件（整棵根树或会话线程
#   视图、无 warnings/scope_warnings、节点数不超 _MAX_ASSOCIATION_NODES_COUNT）以及共享截止是否未超；任一不满足立即短路返回
#   unproven，不做无收益的仓库校验或任务记录加载。仓库校验、.git 身份 stat、任务记录加载与 associate_repos 均在同一起始单调
#   预算内逐步检查截止（不声称能打断已进入内核的同步系统调用），预算耗尽即保持 unproven 并清空基线缓存。全视图关联完成后，仅对
#   classified_ids 内的 exact 节点调用 git.observe。已知边界：视图之外（别的会话/线程）写者不可见，exact 仅代表本完整视图内唯一写者。
# 函数用途: 在共享预算内计算完整视图的仓库关联，并仅对进入分类窗口的独占目标做 git 观察。
def _git_facts(
    agent: object,
    config: object,
    snapshot: dict[str, object],
    nodes: list[dict[str, object]],
    *,
    classified_ids: set[str] | None = None,
    started_monotonic: float | None = None,
    read_context: WorkspaceReadContext | None = None,
) -> dict[str, dict[str, object]]:
    from .. import git_progress_adapter as git

    started = time.monotonic() if started_monotonic is None else started_monotonic
    deadline = started + git.GIT_OBSERVATION_BUDGET_SECONDS
    if not _is_complete_association_view(snapshot, nodes) or time.monotonic() >= deadline:
        return _unproven_git_facts(agent, nodes)
    registered = tuple(item for item in config.orchestration_git_progress_repos or [] if isinstance(item, str))
    allowed_roots = [Path(item) for item in getattr(agent, "workspace_roots", None) or []]
    repos: list[Path] = []
    identities: list[tuple[str, int, int]] = []
    for item in registered:
        if time.monotonic() >= deadline:
            return _unproven_git_facts(agent, nodes)
        checked = git.validate_repo(Path(item), allowed_roots)
        if isinstance(checked, Path):
            if time.monotonic() >= deadline:
                return _unproven_git_facts(agent, nodes)
            identity = _repo_identity(checked)
            if identity is None:
                return _unproven_git_facts(agent, nodes)
            repos.append(checked)
            identities.append(identity)
    write_roots, complete = _view_write_roots(agent, snapshot, nodes, deadline=deadline)
    if not complete or time.monotonic() >= deadline:
        return _unproven_git_facts(agent, nodes)
    associations = git.associate_repos(write_roots, repos)
    if time.monotonic() >= deadline:
        return _unproven_git_facts(agent, nodes)
    targets = (
        {run_id for node in nodes if (run_id := str(node.get("run_id") or ""))}
        if classified_ids is None
        else classified_ids
    )
    owner = str(getattr(getattr(agent, "home_paths", None), "owner_id", "") or "")
    previous = _previous_cache(agent, (owner, registered, tuple(identities)))
    observed: dict[str, dict[str, object]] = {}
    kept: dict[tuple[str, str], dict[str, object]] = {}
    facts: dict[str, dict[str, object]] = {}
    for run_id, association in associations.items():
        # 每个条目都带 previous；非 exact 或未纳入本次分类窗口的目标不启动 git 观察，previous 保持 None。
        entry: dict[str, object] = {**association, "previous": None}
        if association["association"] == "exact" and run_id in targets:
            repo = association["repo"]
            if repo not in observed:
                observed[repo] = git.observe_registered(agent, repo, deadline=deadline, read_context=read_context)
            entry["previous"] = previous.get((run_id, repo))
            entry["observation"] = observed[repo]
            if not observed[repo].get("error_code"):
                kept[(run_id, repo)] = observed[repo]
        facts[run_id] = MappingProxyType(entry)
    previous.clear()
    for key, value in list(kept.items())[-_MAX_GIT_PREVIOUS_ENTRIES_COUNT:]:
        previous[key] = value
    return facts


# LLM: 只做纯内存头部完整性判定，不触发任何磁盘或子进程读取；只有整棵根树或当前会话线程视图、无任何快照警告且节点数不超
#   _MAX_ASSOCIATION_NODES_COUNT 时才返回 True。
# 函数用途: 在读取任何仓库或任务记录前判断当前快照是否具备证明仓库独占的前提条件。
def _is_complete_association_view(snapshot: dict[str, object], nodes: list[dict[str, object]]) -> bool:
    effective = dict((snapshot.get("scope_resolution") or {}).get("effective") or {})
    whole_tree = (
        effective.get("scope") == "own_subtree"
        and bool(effective.get("root_id"))
        and effective.get("run_id") == effective.get("root_id")
    )
    return bool(
        (whole_tree or effective.get("scope") == "conversation_thread")
        and not snapshot.get("warnings")
        and not snapshot.get("scope_warnings")
        and len(nodes) <= _MAX_ASSOCIATION_NODES_COUNT
    )


# LLM: 视图不完整、仓库身份缺失或预算耗尽时统一调用：先清空上一次比较样本缓存，再为视图内所有带 run_id 的节点返回只读
#   unproven 条目，绝不保留半截关联。
# 函数用途: 清空 git 基线缓存并构造全视图 unproven 关联映射。
def _unproven_git_facts(agent: object, nodes: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    _reset_previous(agent)
    unproven = MappingProxyType({"association": "unproven", "repo": "", "previous": None})
    return {run_id: unproven for node in nodes if (run_id := str(node.get("run_id") or ""))}


# LLM: 返回 (run_id -> 写入根列表, 视图是否完整)。先用 _is_complete_association_view 短路已知不完整的视图（0 次 load）；
#   逐条加载前及加载全部结束后均检查 deadline，遇到缺 run_id、加载异常或超时立即短路返回 ({}, False)，不再继续读取剩余前缀。
# 函数用途: 在预算内读取完整已授权视图的全部子代理写入范围。
def _view_write_roots(
    agent: object,
    snapshot: dict[str, object],
    nodes: list[dict[str, object]],
    *,
    deadline: float | None = None,
) -> tuple[dict[str, list[str]], bool]:
    if not _is_complete_association_view(snapshot, nodes):
        return {}, False
    write_roots: dict[str, list[str]] = {}
    for node in nodes:
        if deadline is not None and time.monotonic() >= deadline:
            return {}, False
        run_id = str(node.get("run_id") or "")
        if not run_id:
            return {}, False
        try:
            task = agent.subagents.load(run_id)
            write_roots[run_id] = [item for item in getattr(task, "allowed_write_roots", None) or [] if isinstance(item, str)]
        except Exception:  # noqa: BLE001 - 读不到任务记录：它可能写任何仓库，整个视图证明不了独占，立即短路
            return {}, False
    if deadline is not None and time.monotonic() >= deadline:
        return {}, False
    return write_roots, True


# LLM: 仓库实例身份取 .git 目录的 (设备, inode)；同一路径被换成另一个仓库时代次随之改变。读不到返回 None（不确定）。
# 函数用途: 读取已校验仓库的实例身份。
def _repo_identity(repo: Path) -> tuple[str, int, int] | None:
    try:
        info = os.stat(repo / ".git", follow_symlinks=False)
    except OSError:
        return None
    return str(repo), info.st_dev, info.st_ino


# LLM: 样本挂在 agent 上，只是观察缓存，不是权威或持久存储；代次与上次不同就整表作废后换成新代次。
# 函数用途: 取当前配置代次下的上一次 git 样本表。
def _previous_cache(agent: object, generation: tuple) -> OrderedDict:
    cache = getattr(agent, "_progress_git_previous", None)
    if not isinstance(cache, OrderedDict) or getattr(agent, "_progress_git_generation", None) != generation:
        cache = OrderedDict()
        agent._progress_git_previous = cache
        agent._progress_git_generation = generation
    return cache


# LLM: 配置读不出、上限非法、开关关闭都走这里；之后第一次观察没有比较对象。
# 函数用途: 作废上一次 git 样本。
def _reset_previous(agent: object) -> None:
    agent._progress_git_previous = OrderedDict()
    agent._progress_git_generation = None


__all__ = ["ListAgentsTool", "progress_read_context"]
