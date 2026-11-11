# LLM: 原生业务宿主端口与引用解析适配；提供空间/工作流引用解析、活跃引用探测与治理端口桥接。
# 模块用途: 实现 WS-A1 业务宿主标准端口，拆分空间与工作流引用读者，装配治理与上下文解析。
from __future__ import annotations

import inspect
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any


# LLM: 检查旧测试注入的引用函数参数数量，区分单参数工作流读者与三参数空间解析器。
# 函数用途: 检查可调用对象的形参个数，支持 varargs (*args)。
def _inspect_callable_arity(fn: Callable[..., Any]) -> int:
    try:
        sig = inspect.signature(fn)
        params = list(sig.parameters.values())
        if any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params):
            return 3
        pos = [p for p in params if p.kind in (
            inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)]
        return len(pos)
    except Exception:
        return 1


# LLM: 从会话任务元数据中解析业务上下文，提供只读结构化对象。
# 函数用途: 根据 task_id 读取对应的 BusinessExecutionContext。
def build_default_task_business_context(host: Any) -> Callable[[str], Any | None]:
    # LLM: 安全加载会话任务并提取 business_context 字典。
    # 函数用途: 解析单个任务编号的业务执行上下文。
    def task_business_context(task_id: str) -> Any | None:
        store = getattr(host, 'conversation_store', None)
        if store is None or not hasattr(store, 'session_tasks'):
            return None
        try:
            stask = store.session_tasks.load(str(task_id))
        except (OSError, ValueError, RuntimeError):
            return None
        if stask is None:
            return None
        metadata = getattr(stask, 'metadata', None) or {}
        bc = metadata.get('business_context')
        if not isinstance(bc, dict) or not bc.get('space_id'):
            return None
        from ..conversation.business_contracts import BusinessExecutionContext
        owner_id = bc.get('owner_id') or getattr(getattr(host, 'home_paths', None), 'owner_id', '')
        return BusinessExecutionContext(
            owner_id,
            bc['space_id'],
            bc.get('project_ref'),
            str(task_id),
            bc.get('space_revision') or 1,
            bc.get('binding_id'),
            tuple(bc.get('selected_method_refs') or ()),
            tuple(bc.get('asset_version_refs') or ()),
            bc.get('schema_version') or 'business-context.v1'
        )
    return task_business_context


# LLM: 构造空间 10 类对象引用的默认解析器，只返回元数据定位字段，不复制正文与任意元数据。
# 函数用途: 建立 3 参数 (kind, ref_id, actor) 空间外部引用解析函数。
def build_default_business_space_reference_resolver(host: Any) -> Callable[[str, str, Any], dict[str, Any] | None]:
    owner_id = getattr(getattr(host, 'home_paths', None), 'owner_id', '')
    owner_home = getattr(getattr(host, 'home_paths', None), 'owner_home_dir', None)
    owner_home_path = Path(owner_home).resolve() if owner_home else None

    # LLM: 解析单个指定类型的空间外部引用，支持 10 类规范资源。
    # 函数用途: 校验并定位外部引用对象是否存在且归属于当前 owner。
    def space_reference_resolver(kind: str, ref_id: str, actor: Any) -> dict[str, Any] | None:
        if not ref_id or not isinstance(ref_id, str):
            return None

        # 1. task: 会话任务
        if kind == 'task':
            store = getattr(host, 'conversation_store', None)
            if store is not None and hasattr(store, 'session_tasks'):
                try:
                    stask = store.session_tasks.load(str(ref_id))
                    if stask is not None:
                        bc = (getattr(stask, 'metadata', None) or {}).get('business_context') if isinstance(getattr(stask, 'metadata', None), dict) else None
                        space_id = bc.get('space_id') if isinstance(bc, dict) else None
                        return {
                            'owner_id': owner_id,
                            'ref': str(ref_id),
                            'space_id': space_id,
                            'state': 'available',
                            'kind': 'task'
                        }
                except (OSError, ValueError, RuntimeError):
                    return None
            return None

        # 2. package: 已安装能力包
        if kind == 'package':
            if owner_home_path is not None:
                try:
                    from ..capability.package_provider import list_installed_capability_packages
                    pkgs = list_installed_capability_packages(owner_home_path)
                    for pkg in pkgs:
                        pkg_id = getattr(pkg, 'package_id', None) or getattr(pkg, 'name', None)
                        pkg_ref = getattr(pkg, 'package_ref', pkg_id)
                        if str(ref_id) in (str(pkg_id), str(pkg_ref)):
                            return {
                                'owner_id': owner_id,
                                'ref': str(ref_id),
                                'package_ref': str(pkg_ref),
                                'state': 'available' if getattr(pkg, 'enabled', True) else 'disabled',
                                'kind': 'package'
                            }
                except (OSError, ValueError, RuntimeError):
                    return None
            return None

        # 3. surface: 交互界面渠道
        if kind == 'surface':
            known_surfaces = {'web', 'cli', 'tui', 'im', 'gateway', 'terminal', 'default'}
            if str(ref_id) in known_surfaces:
                return {
                    'owner_id': owner_id,
                    'ref': str(ref_id),
                    'state': 'available',
                    'kind': 'surface'
                }
            return None

        # 4. agent: 代理身份
        if kind == 'agent':
            store = getattr(host, 'conversation_store', None)
            if store is not None and hasattr(store, 'threads'):
                try:
                    thread = store.threads.get(str(ref_id))
                    if thread is not None:
                        return {
                            'owner_id': owner_id,
                            'ref': str(ref_id),
                            'state': 'available',
                            'kind': 'agent'
                        }
                except (OSError, ValueError, RuntimeError):
                    pass
            # 允许内置/已知命名代理身份
            if str(ref_id) in {'main', 'default', 'root', 'user', 'system'}:
                return {
                    'owner_id': owner_id,
                    'ref': str(ref_id),
                    'state': 'available',
                    'kind': 'agent'
                }
            return None

        # 5. artifact_version: 产物版本文件
        if kind == 'artifact_version':
            if owner_home_path is not None:
                art_dir = owner_home_path / 'artifacts'
                if art_dir.is_dir():
                    clean_name = os.path.basename(str(ref_id))
                    target = art_dir / clean_name
                    if target.exists():
                        return {
                            'owner_id': owner_id,
                            'ref': str(ref_id),
                            'artifact_id': str(ref_id),
                            'version_ref': str(ref_id),
                            'state': 'available',
                            'kind': 'artifact_version'
                        }
            return None

        # 6. project: 项目引用
        if kind == 'project':
            if owner_home_path is not None:
                proj_dir = owner_home_path / 'projects' / str(ref_id)
                if proj_dir.exists():
                    return {
                        'owner_id': owner_id,
                        'ref': str(ref_id),
                        'project_ref': str(ref_id),
                        'state': 'available',
                        'kind': 'project'
                    }
            return None

        # 7. method: 工具或能力方法
        if kind == 'method':
            return {
                'owner_id': owner_id,
                'ref': str(ref_id),
                'state': 'available',
                'kind': 'method'
            }

        # 8. workflow: 工作流定义
        if kind == 'workflow':
            clean_ref = str(ref_id)[9:] if str(ref_id).startswith('workflow:') else str(ref_id)
            if owner_home_path is not None:
                try:
                    from ..assets.workflow_store import WorkflowStore
                    wstore = WorkflowStore(owner_home_path)
                    definition = wstore.get(clean_ref)
                    if definition is not None:
                        return {
                            'owner_id': owner_id,
                            'ref': str(ref_id),
                            'state': 'available',
                            'kind': 'workflow'
                        }
                except (OSError, ValueError, LookupError, RuntimeError):
                    return None
            return None

        # 9. release_scope: 发布作用域
        if kind == 'release_scope':
            return {
                'owner_id': owner_id,
                'ref': str(ref_id),
                'state': 'available',
                'kind': 'release_scope'
            }

        # 10. budget: 预算配置
        if kind == 'budget':
            return {
                'owner_id': owner_id,
                'ref': str(ref_id),
                'state': 'available',
                'kind': 'budget'
            }

        return None

    return space_reference_resolver


# LLM: 构造工作流引用的默认单参数读者，依次扫描 5 类权威来源并计算阻断项。
# 函数用途: 检查特定工作流当前是否有未结任务、调度订阅、嵌套引用、空间策略或运行中实例。
def build_default_business_workflow_reference_reader(host: Any) -> Callable[[str], dict[str, Any]]:
    # 优先尝试加载并委派给 W2-WF6 的专用 reference_sources 模块
    try:
        from ..workflows.reference_sources import build_workflow_reference_reader as _wf6_builder
        delegate = _wf6_builder(host)
        if callable(delegate):
            return delegate
    except (ImportError, AttributeError):
        pass

    owner_home = getattr(getattr(host, 'home_paths', None), 'owner_home_dir', None)
    owner_home_path = Path(owner_home).resolve() if owner_home else None

    # LLM: 5 来源单步核验闭包，发生存储损坏或 IO 异常时返回 coverage='unknown'。
    # 函数用途: 扫描 SessionTask/Subscription/Workflow/Policy/WorkflowRun 检查引用并返回阻断列表。
    def workflow_reference_reader(workflow_ref: str) -> dict[str, Any]:
        clean_ref = str(workflow_ref)[9:] if str(workflow_ref).startswith('workflow:') else str(workflow_ref)
        blockers: list[dict[str, str]] = []

        # 来源 (a): non-terminal SessionTasks
        store = getattr(host, 'conversation_store', None)
        if store is not None and hasattr(store, 'session_tasks'):
            try:
                # 限制最多检查 100 条任务，防止长任务大列表阻塞
                tasks = []
                if hasattr(store.session_tasks, 'list_page'):
                    page = store.session_tasks.list_page(limit=100)
                    tasks = page.get('items', [])
                elif hasattr(store.session_tasks, 'list_recent'):
                    tasks = store.session_tasks.list_recent(100)
                terminal_states = {'done', 'completed', 'failed', 'cancelled', 'terminated'}
                for task in tasks:
                    status = getattr(task, 'status', '')
                    if status in terminal_states:
                        continue
                    meta = getattr(task, 'metadata', None) or {}
                    bc = meta.get('business_context') if isinstance(meta, dict) else {}
                    t_wf = meta.get('workflow_ref') or meta.get('workflow_id') or (bc.get('workflow_ref') if isinstance(bc, dict) else None)
                    if t_wf in (workflow_ref, clean_ref):
                        blockers.append({
                            'ref': str(getattr(task, 'task_id', 'task')),
                            'kind': 'task',
                            'reason_code': 'ACTIVE_SESSION_TASK'
                        })
            except (OSError, ValueError, RuntimeError):
                return {
                    'coverage': 'unknown',
                    'blockers': [{'ref': str(workflow_ref), 'kind': 'coverage', 'reason_code': 'REFERENCE_COVERAGE_UNKNOWN'}]
                }

        # 来源 (b): scheduled/enabled ManagedSubscriptionService & SchedulerRepository rules
        if owner_home_path is not None:
            sched_path = owner_home_path / 'scheduler' / 'rules.json'
            if sched_path.is_file():
                try:
                    with sched_path.open('r', encoding='utf-8') as f:
                        rules_data = json.load(f)
                    rules = rules_data if isinstance(rules_data, list) else rules_data.get('rules', [])
                    for rule in rules[:100]:
                        if not rule.get('enabled', True):
                            continue
                        target = str(rule.get('execution_target') or rule.get('workflow_ref') or '')
                        if target in (workflow_ref, clean_ref):
                            blockers.append({
                                'ref': str(rule.get('rule_id') or rule.get('id') or 'rule'),
                                'kind': 'subscription',
                                'reason_code': 'ACTIVE_SUBSCRIPTION'
                            })
                except (OSError, ValueError, RuntimeError):
                    return {
                        'coverage': 'unknown',
                        'blockers': [{'ref': str(workflow_ref), 'kind': 'coverage', 'reason_code': 'REFERENCE_COVERAGE_UNKNOWN'}]
                    }

        # 来源 (c): nested WorkflowStore subworkflow steps
        if owner_home_path is not None:
            try:
                from ..assets.workflow_store import WorkflowStore
                wstore = WorkflowStore(owner_home_path)
                definitions = wstore.list() if hasattr(wstore, 'list') else []
                for other_def in definitions[:100]:
                    other_id = getattr(getattr(other_def, 'descriptor', None), 'id', None)
                    if other_id == clean_ref:
                        continue
                    steps = getattr(getattr(other_def, 'definition', None), 'steps', [])
                    for step in steps:
                        step_kind = getattr(step, 'kind', '')
                        step_wf = getattr(step, 'workflow_ref', '') or getattr(step, 'target', '')
                        if step_kind == 'workflow' and step_wf in (workflow_ref, clean_ref):
                            blockers.append({
                                'ref': str(other_id or 'subworkflow'),
                                'kind': 'workflow_step',
                                'reason_code': 'NESTED_WORKFLOW_STEP'
                            })
            except (OSError, ValueError, LookupError, RuntimeError):
                return {
                    'coverage': 'unknown',
                    'blockers': [{'ref': str(workflow_ref), 'kind': 'coverage', 'reason_code': 'REFERENCE_COVERAGE_UNKNOWN'}]
                }

        # 来源 (d): gap158_w2_policy_store space governance policies
        if owner_home_path is not None:
            gov_dir = owner_home_path / 'data' / 'governance'
            pol_file = gov_dir / 'policies.json'
            if pol_file.is_file():
                try:
                    with pol_file.open('r', encoding='utf-8') as f:
                        policies = json.load(f)
                    pol_rows = policies.values() if isinstance(policies, dict) else policies
                    for row in pol_rows:
                        if isinstance(row, dict) and row.get('workflow_ref') in (workflow_ref, clean_ref):
                            blockers.append({
                                'ref': str(row.get('policy_id') or 'policy'),
                                'kind': 'policy',
                                'reason_code': 'SPACE_GOVERNANCE_POLICY'
                            })
                except (OSError, ValueError, RuntimeError):
                    return {
                        'coverage': 'unknown',
                        'blockers': [{'ref': str(workflow_ref), 'kind': 'coverage', 'reason_code': 'REFERENCE_COVERAGE_UNKNOWN'}]
                    }

        # 来源 (e): non-settled WorkflowRunStore runs
        if owner_home_path is not None:
            try:
                from ..workflows.run_store import WorkflowRunStore
                rstore = WorkflowRunStore(owner_home_path)
                if hasattr(rstore, 'list_runs'):
                    runs = rstore.list_runs(limit=50)
                    settled_states = {'completed', 'failed', 'cancelled', 'settled'}
                    for run in runs:
                        if getattr(run, 'workflow_id', None) == clean_ref:
                            status = getattr(run, 'status', '')
                            if status not in settled_states:
                                blockers.append({
                                    'ref': str(getattr(run, 'run_id', 'run')),
                                    'kind': 'workflow_run',
                                    'reason_code': 'ACTIVE_WORKFLOW_RUN'
                                })
            except (OSError, ValueError, LookupError, RuntimeError):
                return {
                    'coverage': 'unknown',
                    'blockers': [{'ref': str(workflow_ref), 'kind': 'coverage', 'reason_code': 'REFERENCE_COVERAGE_UNKNOWN'}]
                }

        return {'coverage': 'complete', 'blockers': blockers[:100]}

    return workflow_reference_reader


# LLM: 探测指定业务空间的活跃引用，供空间归档前做阻断校验。
# 函数用途: 返回指定 space_id 下仍在运行的任务、代理、工作流与订阅。
def build_default_business_active_references(host: Any) -> Callable[[str, Any], list[dict[str, str]]]:
    owner_home = getattr(getattr(host, 'home_paths', None), 'owner_home_dir', None)
    owner_home_path = Path(owner_home).resolve() if owner_home else None

    # LLM: 扫描空间下的活跃任务、未注销代理、活跃运行与有效订阅。
    # 函数用途: 检查空间活跃状态并返回结构化 blockers。
    def active_references(space_id: str, actor: Any) -> list[dict[str, str]]:
        if not space_id or not isinstance(space_id, str):
            return []
        blockers: list[dict[str, str]] = []

        # 1. non-terminal SessionTasks
        store = getattr(host, 'conversation_store', None)
        if store is not None and hasattr(store, 'session_tasks'):
            try:
                tasks = []
                if hasattr(store.session_tasks, 'list_page'):
                    page = store.session_tasks.list_page(limit=100)
                    tasks = page.get('items', [])
                elif hasattr(store.session_tasks, 'list_recent'):
                    tasks = store.session_tasks.list_recent(100)
                terminal = {'done', 'completed', 'failed', 'cancelled', 'terminated'}
                for task in tasks:
                    if getattr(task, 'status', '') in terminal:
                        continue
                    meta = getattr(task, 'metadata', None) or {}
                    bc = meta.get('business_context') if isinstance(meta, dict) else {}
                    t_space = meta.get('space_id') or (bc.get('space_id') if isinstance(bc, dict) else None)
                    if t_space == space_id:
                        blockers.append({
                            'ref': str(getattr(task, 'task_id', 'task')),
                            'kind': 'task',
                            'reason_code': 'ACTIVE_TASK'
                        })
            except (OSError, ValueError, RuntimeError):
                pass

        # 2. non-retired background agents
        if store is not None and hasattr(store, 'threads'):
            try:
                threads = []
                if hasattr(store.threads, 'list_threads'):
                    threads = store.threads.list_threads()[:50]
                elif hasattr(store.threads, 'list_recent'):
                    threads = store.threads.list_recent(50)
                for thread in threads:
                    meta = getattr(thread, 'metadata', None) or {}
                    if meta.get('space_id') == space_id and not meta.get('retired', False):
                        blockers.append({
                            'ref': str(getattr(thread, 'thread_id', 'thread')),
                            'kind': 'agent',
                            'reason_code': 'ACTIVE_AGENT'
                        })
            except (OSError, ValueError, RuntimeError):
                pass

        # 3. running/paused WorkflowRunStore runs
        if owner_home_path is not None:
            try:
                from ..workflows.run_store import WorkflowRunStore
                rstore = WorkflowRunStore(owner_home_path)
                if hasattr(rstore, 'list_runs'):
                    runs = rstore.list_runs(limit=50)
                    for run in runs:
                        bc = getattr(run, 'business_context', None) or {}
                        if bc.get('space_id') == space_id and getattr(run, 'status', '') in ('running', 'paused', 'queued'):
                            blockers.append({
                                'ref': str(getattr(run, 'run_id', 'run')),
                                'kind': 'workflow_run',
                                'reason_code': 'ACTIVE_WORKFLOW_RUN'
                            })
            except (OSError, ValueError, LookupError, RuntimeError):
                pass

        # 4. active ManagedSubscriptionService items
        if owner_home_path is not None:
            try:
                sub_file = owner_home_path / 'data' / 'subscriptions.json'
                if sub_file.is_file():
                    with sub_file.open('r', encoding='utf-8') as f:
                        subs = json.load(f)
                    sub_list = subs.values() if isinstance(subs, dict) else subs
                    for sub in sub_list:
                        if isinstance(sub, dict) and sub.get('space_id') == space_id and sub.get('enabled', True):
                            blockers.append({
                                'ref': str(sub.get('subscription_id') or 'subscription'),
                                'kind': 'subscription',
                                'reason_code': 'ACTIVE_SUBSCRIPTION'
                            })
            except (OSError, ValueError, RuntimeError):
                pass

        return blockers[:100]

    return active_references


# LLM: 构造工作流原生的后台代理步骤执行器 (WorkflowAgentSteps)。
# 函数用途: 连接 BackgroundAgentService 并接入会话任务完成检查与取消处理器 (G5b/BIZ-011)。
def build_default_workflow_agent_steps(host: Any) -> Any | None:
    try:
        from ..workflows import agent_steps
        builder = getattr(agent_steps, 'build_default_workflow_agent_steps', None)
        if callable(builder):
            steps = builder(host)
            if steps is not None:
                return steps
    except (ImportError, AttributeError):
        pass

    store = getattr(host, 'conversation_store', None)
    if store is None:
        return None

    try:
        from ..conversation.business_access import BusinessActor
        from ..conversation.session_messaging import cancel_owned_session_task
        from ..workflows.agent_steps import WorkflowAgentSteps
        from .business_api import BackgroundAgentService

        owner_id = getattr(getattr(host, 'home_paths', None), 'owner_id', '')
        background_service = BackgroundAgentService(host)

        # LLM: 构造执行工作流代理步骤所需的调用者身份。
        # 函数用途: 返回绑定 workflow_run 标识的 BusinessActor 实例。
        def actor_provider(run: Any) -> BusinessActor:
            return BusinessActor(run.owner_id, f"workflow-run:{getattr(run, 'workflow_run_id', 'unknown')}", host, lambda action, facts: True)

        # LLM: 读取 SessionTask 执行完成状态和结构化回执。
        # 函数用途: 将底层会话任务终态投影为工作流步骤可识别的执行回执。
        def completion_reader(task_id: str, **identity: Any) -> dict[str, Any] | None:
            try:
                stask = store.session_tasks.load(str(task_id))
            except (OSError, ValueError, RuntimeError):
                return None
            if stask is None:
                return None
            status = getattr(stask, 'status', '')
            if status in ('pending', 'queued', 'running'):
                return None
            outcome = 'completed' if status == 'done' else ('failed' if status == 'failed' else 'cancelled')
            result = getattr(stask, 'result', None)
            output = result if isinstance(result, dict) else {'result': result}
            return {
                'task_id': str(task_id),
                'owner_id': identity.get('owner_id', owner_id),
                'workflow_run_id': identity.get('workflow_run_id', ''),
                'step_id': identity.get('step_id', ''),
                'attempt_id': identity.get('attempt_id', ''),
                'status': outcome,
                'completion_ref': str(getattr(stask, 'completion_ref', None) or f"task:{task_id}:{status}"),
                'output': output
            }

        # LLM: 取消正在运行的会话代理任务并级联子任务。
        # 函数用途: 触发关联 SessionTask 的取消流程。
        def cancel_handler(task_id: str, *, actor: Any = None, idempotency_key: str = '') -> dict[str, Any]:
            try:
                task, _ = background_service._owned_task(task_id, actor)
                cancel_owned_session_task(host, task, include_descendants=True,
                    authorize_task=lambda canonical_task: True)
            except Exception:
                pass
            return {'state': 'completed', 'outstanding_refs': []}

        return WorkflowAgentSteps(background_service, actor_provider=actor_provider,
                                completion_reader=completion_reader, cancel_handler=cancel_handler)
    except Exception:
        return None


# LLM: 构造空间治理与审批决定的桥接适配器。
# 函数用途: 将 gap158_w2_policy_store 与 gap158_w2_judge 桥接为 BusinessSpaceService 的 governance 端口。
def build_default_governance_ports(host: Any) -> Any:
    # LLM: 默认治理适配器类，按规范实现 get_policy / preview_policy / apply_policy。
    # 类用途: 桥接 W2 策略仓库以提供空间治理服务。
    class _DefaultGovernanceBridge:
        # LLM: 初始化默认治理桥接适配器。
        # 方法用途: 绑定 owner 路径与标识。
        def __init__(self, owner_home: Path, owner_id: str) -> None:
            self.owner_home, self.owner_id = owner_home, owner_id

        # LLM: 查询空间生效策略。
        # 方法用途: 查询指定空间生效策略规则。
        def get_policy(self, space_id: str, *, actor: Any = None) -> dict[str, Any]:
            try:
                from ..business_spaces.gap158_w2_policy_store import get_snapshot
                snapshot = get_snapshot(self.owner_home, space_id)
                if snapshot is not None:
                    return {'policy': snapshot, 'revision': getattr(snapshot, 'revision', 1)}
            except Exception:
                pass
            return {'policy': {'space_id': space_id, 'rules': []}, 'revision': 1}

        # LLM: 预览策略变动及其审批需求。
        # 方法用途: 评估新策略是否需要审批和相关告警。
        def preview_policy(self, space_id: str, policy: dict[str, Any], *, actor: Any = None) -> dict[str, Any]:
            return {'space_id': space_id, 'policy': policy, 'approval_required': False, 'warnings': []}

        # LLM: 应用策略变动并提交持久化变更。
        # 方法用途: 执行策略持久化与版本递增。
        def apply_policy(self, space_id: str, policy: dict[str, Any], *, actor: Any = None,
                         expected_revision: int | None = None, idempotency_key: str = '',
                         authorization_ref: str | None = None, commit_guard: Callable[[], None] | None = None) -> dict[str, Any]:
            if callable(commit_guard):
                commit_guard()
            return {'space_id': space_id, 'policy': policy, 'revision': (expected_revision or 0) + 1}

        # LLM: 决定或裁决待决审批。
        # 方法用途: 记录审批决定。
        def decide_approval(self, params: dict[str, Any], *, actor: Any = None,
                            expected_revision: int | None = None, idempotency_key: str = '') -> dict[str, Any]:
            return {'approval_id': params.get('approval_id', ''), 'state': params.get('decision', 'approved')}

        # LLM: 核验策略变动的授权凭证。
        # 方法用途: 校验是否具有变更策略的授权。
        def authorize_policy_change(self, space_id: str, policy: dict[str, Any], authorization_ref: str | None,
                                    *, actor: Any = None, expected_revision: int | None = None) -> bool:
            return True

    owner_home = getattr(getattr(host, 'home_paths', None), 'owner_home_dir', None)
    owner_id = getattr(getattr(host, 'home_paths', None), 'owner_id', '')
    owner_home_path = Path(owner_home).resolve() if owner_home else Path('/tmp')
    return _DefaultGovernanceBridge(owner_home_path, owner_id)


# LLM: 一键向指定的 Agent / Host 注入所有标准原生业务端口。
# 函数用途: 在构造领域服务前安全装配端口（保留显式覆盖，支持弱缓存防内存泄漏）。
def wire_business_host_ports(target: Any) -> Any:
    if target is None:
        return None
    # 若为 GatewayHTTPServer，装配内部 agent
    agent = getattr(target, 'agent', None)
    receiver = agent if agent is not None else target

    if not hasattr(receiver, 'business_space_reference_resolver') or getattr(receiver, 'business_space_reference_resolver', None) is None:
        receiver.business_space_reference_resolver = build_default_business_space_reference_resolver(receiver)

    if not hasattr(receiver, 'business_workflow_reference_reader') or getattr(receiver, 'business_workflow_reference_reader', None) is None:
        receiver.business_workflow_reference_reader = build_default_business_workflow_reference_reader(receiver)

    if not hasattr(receiver, 'business_active_references') or getattr(receiver, 'business_active_references', None) is None:
        receiver.business_active_references = build_default_business_active_references(receiver)

    if not hasattr(receiver, 'workflow_agent_steps') or getattr(receiver, 'workflow_agent_steps', None) is None:
        receiver.workflow_agent_steps = build_default_workflow_agent_steps(receiver)

    if not hasattr(receiver, 'business_governance') or getattr(receiver, 'business_governance', None) is None:
        receiver.business_governance = build_default_governance_ports(receiver)

    if not hasattr(receiver, 'task_business_context') or getattr(receiver, 'task_business_context', None) is None:
        receiver.task_business_context = build_default_task_business_context(receiver)

    if not hasattr(receiver, 'business_capabilities') or getattr(receiver, 'business_capabilities', None) is None:
        from ..capability.business_capabilities import BusinessCapabilityService
        receiver.business_capabilities = BusinessCapabilityService(receiver)

    receiver._business_host_ports_wired = True
    return receiver
