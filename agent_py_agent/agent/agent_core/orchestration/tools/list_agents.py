"""当前代理树的只读模型工具，返回状态与真实产物引用，不暴露界面恢复细节。"""

# LLM: This module is the only model-visible read adapter for the canonical
# agent-tree projection. Keep lifecycle mutations in create/guidance/cancel and
# do not add a second status store or polling loop here.
# 模块用途: 让主代理或子代理只读查看自己的代理树状态，不承担推进、唤醒或取消。

from __future__ import annotations

import json
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
    ToolRuntimePolicy,
)
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
    # 函数用途: 读取状态与产物引用，并排除仅耗时变化的假进展；不改等待、权限或生命周期状态。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        run_id = str(params.get("run_id") or "").strip()
        query = {"run_id": run_id, "scope": "own_subtree"} if run_id else {}
        snapshot = agent_tree_status_payload(self.agent, query)
        payload = agent_tree_model_payload(snapshot, progress_context=progress_read_context(self.agent, snapshot))
        return ToolHandlerOutcome(
            self.model_spec.name,
            True,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            result_envelope={
                "tool_output_policy": {"live_prompt_output": agent_tree_model_preview(payload)},
                "progress_observation": agent_tree_progress_observation(snapshot, payload),
            },
        )


# LLM: 进展观察缓存上限（条）；键含 owner+run+仓库+配置代次，只作比较样本，不是生命周期或授权事实。
# 常量用途: 限制挂在 agent 上的上一次 git 观察缓存条数。
_MAX_GIT_PREVIOUS_ENTRIES_COUNT = 128
# 常量用途: 一次 list_agents 读工具审批账的总时长（所有根共用一个截止）。
_APPROVAL_READ_BUDGET_SECONDS = 1.0
# 常量用途: 每个根一次审批目录扫描最多看的条目数；没扫完就记为读不出，不当作没有审批。
_APPROVAL_SCAN_LIMIT_COUNT = 128


# LLM: ORCH03-R1/R2/R3（PRO-MSG-024）：只从已授权快照与宿主事实构造冻结上下文，不接受模型参数。配置读不出或子代理
#   上限非法时整次不分类（全部 unknown），不退化成不限；只对前 child_limit 个节点读必需来源。git 观察默认关：开关
#   不是 True 时不校验仓库、不启动任何 git 进程。读取不改生命周期、Goal/Todo、审批或唤醒。
# 函数用途: 为一次 list_agents 构造进展读取上下文。
def progress_read_context(agent: object, snapshot: dict[str, object]) -> ProgressReadContext:
    from ....capability import runtime_config_reload
    from ....capability.config import CapabilityConfig

    now = time.time()
    try:
        config, refusal = runtime_config_reload.capability_config_with_trusted_source(agent)
    except Exception:  # noqa: BLE001 - 配置读不出按整次不分类处理
        config, refusal = None, "CONFIG_UNREADABLE"
    if not isinstance(config, CapabilityConfig):
        # 坏行、读不到来源都和读不出一样：整次不分类，不拿默认值冒充用户配置。
        return ProgressReadContext(now=now, child_limit=0, config_error=f"PROGRESS_{refusal or 'CONFIG_UNREADABLE'}")
    limit = config.orchestration_progress_check_child_limit
    if type(limit) is not int or limit < 0:
        return ProgressReadContext(now=now, child_limit=0, config_error="PROGRESS_CHILD_LIMIT_INVALID")
    limit = limit or DEFAULT_PROGRESS_CHECK_CHILD_LIMIT_COUNT
    nodes = [node for node in snapshot.get("nodes") or [] if isinstance(node, dict)][:limit]
    sources = _node_sources(agent, nodes)
    if config.orchestration_git_progress_enabled is True:
        git = _git_facts(agent, config, nodes)
    else:
        # 关掉即作废上一次样本：重新打开后第一次观察没有比较对象，不会拿关之前的旧样本说“有变化”。
        git = {}
        _previous_cache(agent).clear()
    return ProgressReadContext(now=now, child_limit=limit,
                               sources=MappingProxyType({key: MappingProxyType(value) for key, value in sources.items()}),
                               git=MappingProxyType(git))


def _node_sources(agent: object, nodes: list[dict[str, object]]) -> dict[str, dict[str, dict[str, object]]]:
    from ...agent_tree.node_rendering import progress_source_facts

    approvals = _approval_counts(agent, {str(node.get("root_id") or "") for node in nodes})
    result: dict[str, dict[str, dict[str, object]]] = {}
    for node in nodes:
        run_id = str(node.get("run_id") or "")
        if not run_id:
            continue
        facts = progress_source_facts(agent, run_id)
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


# LLM: 只在开关为 True 时调用。仓库先过 validate_repo；关联只用子代理任务记录的 allowed_write_roots；同一仓库一次调用
#   只观察一次，整次调用共用一个绝对截止。上一次样本按 owner+run+仓库+配置代次取自 agent 上的有界缓存，没有就不比较。
# 函数用途: 读取本次 list_agents 的 git 关联与观察。
def _git_facts(agent: object, config: object, nodes: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    from .. import git_progress_adapter as git

    registered = tuple(item for item in config.orchestration_git_progress_repos or [] if isinstance(item, str))
    allowed_roots = [Path(item) for item in getattr(agent, "workspace_roots", None) or []]
    repos = [checked for checked in (git.validate_repo(Path(item), allowed_roots) for item in registered)
             if isinstance(checked, Path)]
    write_roots: dict[str, list[str]] = {}
    for node in nodes:
        run_id = str(node.get("run_id") or "")
        if not run_id:
            continue
        try:
            task = agent.subagents.load(run_id)
            write_roots[run_id] = [item for item in getattr(task, "allowed_write_roots", None) or [] if isinstance(item, str)]
        except Exception:  # noqa: BLE001 - 读不到任务记录就没有权威关联
            write_roots[run_id] = []
    associations = git.associate_repos(write_roots, repos)
    owner = str(getattr(getattr(agent, "home_paths", None), "owner_id", "") or "")
    cache = _previous_cache(agent)
    deadline = time.monotonic() + git.GIT_OBSERVATION_BUDGET_SECONDS
    observed: dict[str, dict[str, object]] = {}
    facts: dict[str, dict[str, object]] = {}
    for run_id, association in associations.items():
        entry: dict[str, object] = dict(association)
        if association["association"] == "exact":
            repo = association["repo"]
            if repo not in observed:
                observed[repo] = git.observe(Path(repo), deadline=deadline)
            key = (owner, run_id, repo, registered)
            entry["previous"] = cache.get(key)
            entry["observation"] = observed[repo]
            if not observed[repo].get("error_code"):
                cache[key] = observed[repo]
                cache.move_to_end(key)
                while len(cache) > _MAX_GIT_PREVIOUS_ENTRIES_COUNT:
                    cache.popitem(last=False)
        facts[run_id] = MappingProxyType(entry)
    return facts


def _previous_cache(agent: object) -> OrderedDict:
    cache = getattr(agent, "_progress_git_previous", None)
    if not isinstance(cache, OrderedDict):
        cache = OrderedDict()
        agent._progress_git_previous = cache
    return cache


__all__ = ["ListAgentsTool", "progress_read_context"]
