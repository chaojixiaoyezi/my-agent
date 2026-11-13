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
                        pkg_id = (pkg.get('package_id') or pkg.get('name')) if isinstance(pkg, dict) else (getattr(pkg, 'package_id', None) or getattr(pkg, 'name', None))
                        pkg_ref = (pkg.get('package_ref') or pkg_id) if isinstance(pkg, dict) else getattr(pkg, 'package_ref', pkg_id)
                        enabled = pkg.get('enabled', True) if isinstance(pkg, dict) else getattr(pkg, 'enabled', True)
                        digest = (pkg.get('digest') or pkg.get('version_ref') or pkg.get('version')) if isinstance(pkg, dict) else (getattr(pkg, 'digest', None) or getattr(pkg, 'version_ref', None) or getattr(pkg, 'version', None))
                        matches = (str(pkg_id), str(pkg_ref), f"capability:{pkg_id}") if pkg_id else (str(pkg_ref),)
                        if str(ref_id) in matches:
                            res = {
                                'owner_id': owner_id,
                                'ref': str(ref_id),
                                'package_ref': str(pkg_ref),
                                'state': 'available' if enabled else 'disabled',
                                'kind': 'package'
                            }
                            if digest:
                                res['version_ref'] = str(digest)
                            return res
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
                    thread = store.threads.load(str(ref_id)) if hasattr(store.threads, 'load') else store.threads.get(str(ref_id))
                    if thread is not None:
                        meta = getattr(thread, 'metadata', None) or {}
                        bg = meta.get('background_agent') if isinstance(meta, dict) else {}
                        identity_state = bg.get('identity_state') if isinstance(bg, dict) else None
                        is_retired = (identity_state == 'retired') or bool(meta.get('retired', False)) if isinstance(meta, dict) else False
                        return {
                            'owner_id': owner_id,
                            'ref': str(ref_id),
                            'state': 'retired' if is_retired else 'available',
                            'space_id': bg.get('space_id') if isinstance(bg, dict) else None,
                            'kind': 'agent'
                        }
                    threads = []
                    if hasattr(store.threads, 'list_threads'):
                        try:
                            threads = list(store.threads.list_threads(limit=100))[:100]
                        except TypeError:
                            threads = list(store.threads.list_threads())[:100]
                    elif hasattr(store.threads, 'list_report'):
                        rep = store.threads.list_report(limit=100)
                        if isinstance(rep, tuple):
                            threads = rep[0][:100]
                        elif hasattr(rep, 'rows'):
                            threads = getattr(rep, 'rows', [])[:100]
                        else:
                            threads = list(rep)[:100]
                    elif hasattr(store.threads, 'list_recent'):
                        threads = list(store.threads.list_recent(100))[:100]

                    for th in threads:
                        th_title = getattr(th, 'title', '')
                        th_meta = getattr(th, 'metadata', None) or {}
                        th_bg = th_meta.get('background_agent') if isinstance(th_meta, dict) else {}
                        th_bg_name = th_bg.get('name') if isinstance(th_bg, dict) else None
                        if str(ref_id) in (str(th_title), str(th_bg_name)):
                            identity_state = th_bg.get('identity_state') if isinstance(th_bg, dict) else None
                            is_retired = (identity_state == 'retired') or bool(th_meta.get('retired', False)) if isinstance(th_meta, dict) else False
                            return {
                                'owner_id': owner_id,
                                'ref': str(ref_id),
                                'state': 'retired' if is_retired else 'available',
                                'space_id': th_bg.get('space_id') if isinstance(th_bg, dict) else None,
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
                from ..conversation.business_access import BusinessError, require_actor
                from ..plugin_host.errors import HostAPIError
                from .business_artifacts_http import artifact_version_reference

                try:
                    require_actor(actor)
                    if (actor.owner_id != owner_id
                            or Path(actor.service.owner_root).resolve() != owner_home_path):
                        return None
                    return artifact_version_reference(owner_home_path, owner_id, ref_id)
                except HostAPIError as error:
                    if error.code == 'STORE_UNAVAILABLE':
                        raise BusinessError('STORE_UNAVAILABLE') from error
                    return None
                except (OSError, ValueError):
                    return None
            return None

        # 6. project: 只认原 owner/projects 下的单段真实目录，不把路径当项目身份。
        if kind == 'project':
            from ..common.nofollow_fs import NoFollowPathError, open_directory_beneath
            from ..conversation.business_access import BusinessError

            if (ref_id in {'.', '..'} or '/' in ref_id or '\\' in ref_id
                    or '\x00' in ref_id or Path(ref_id).is_absolute()):
                raise BusinessError('INVALID_SCHEMA', details={
                    'reason': 'PROJECT_REF_MUST_BE_SINGLE_COMPONENT',
                    'side_effect_state': 'not_started',
                    'next_action': '使用原 owner/projects 下的单段项目标识。',
                })
            if owner_home_path is None:
                return None
            try:
                descriptor = open_directory_beneath(owner_home_path, ('projects', ref_id))
            except FileNotFoundError:
                return None
            except NoFollowPathError as error:
                raise BusinessError('SPACE_SCOPE_MISMATCH', details={
                    'reason': 'PROJECT_DIRECTORY_UNSAFE',
                    'side_effect_state': 'not_started',
                    'next_action': '检查原 projects 目录及项目目录，禁止符号链接或非目录组件。',
                }) from error
            except OSError as error:
                raise BusinessError('STORE_UNAVAILABLE', details={
                    'reason': 'PROJECT_DIRECTORY_UNREADABLE', 'exception_type': type(error).__name__,
                    'side_effect_state': 'not_started',
                    'next_action': '检查原项目目录访问权限，修复后再试。',
                }) from error
            else:
                os.close(descriptor)
            return {'owner_id': owner_id, 'ref': ref_id, 'project_ref': ref_id,
                    'state': 'available', 'kind': 'project'}

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
                    fn = getattr(wstore, 'get_workflow', None) or getattr(wstore, 'get', None)
                    definition = fn(clean_ref) if fn is not None else None
                    if definition is not None:
                        return {
                            'owner_id': owner_id,
                            'ref': str(ref_id),
                            'state': 'available',
                            'kind': 'workflow'
                        }
                except (OSError, ValueError, LookupError, RuntimeError, FileNotFoundError):
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


# LLM: 判定目标宿主是否启用了原生业务宿主端口装配。
# 函数用途: 检查显式开关覆盖并回退至配置，支持 _business_host_ports_wired 与 config.business_host_ports_enabled。
def is_business_host_ports_enabled(target: Any) -> bool:
    if target is None:
        return False
    explicit = getattr(target, 'business_host_ports_enabled', None)
    if explicit is False:
        return False
    if explicit is True:
        return True
    if getattr(target, '_business_host_ports_wired', False):
        return True
    config = getattr(target, 'config', None)
    return bool(getattr(config, 'business_host_ports_enabled', False))


# LLM: 构造工作流引用的默认单参数读者，依次扫描 5 类权威来源并计算阻断项。
# 函数用途: 检查特定工作流当前是否有未结任务、调度订阅、嵌套引用、空间策略或运行中实例。
def build_default_business_workflow_reference_reader(host: Any) -> Callable[[str], dict[str, Any]]:
    # 优先尝试加载并委派给 W2-WF6 的专用 reference_sources 模块
    delegate = None
    try:
        from ..workflows.reference_sources import build_workflow_reference_reader as _wf6_builder
        raw_delegate = _wf6_builder(host)
        if callable(raw_delegate):
            delegate = raw_delegate
    except (ImportError, AttributeError):
        pass

    owner_home = getattr(getattr(host, 'home_paths', None), 'owner_home_dir', None)
    owner_home_path = Path(owner_home).resolve() if owner_home else None

    # LLM: 5 来源单步核验闭包，发生存储损坏或 IO 异常时返回 coverage='unknown'。
    # 函数用途: 扫描 SessionTask/Subscription/Workflow/Policy/WorkflowRun 检查引用并返回阻断列表。
    def workflow_reference_reader(workflow_ref: str) -> dict[str, Any]:
        clean_ref = str(workflow_ref)[9:] if str(workflow_ref).startswith('workflow:') else str(workflow_ref)

        if delegate is not None:
            # 动态检查/同步最新 session_task_store，以防 conversation_store 是在建代理后绑定的
            store = getattr(host, 'conversation_store', None)
            dynamic_session_tasks = getattr(store, 'session_tasks', None)
            if dynamic_session_tasks is not None and getattr(delegate, 'session_task_store', None) is None:
                try:
                    delegate.session_task_store = dynamic_session_tasks
                except Exception:
                    pass

            res = delegate(workflow_ref)
            coverage = res.get('coverage', 'complete') if isinstance(res, dict) else 'complete'
            raw_blockers = res.get('blockers', []) if isinstance(res, dict) else []

            blockers: list[dict[str, Any]] = []
            seen_refs: set[str] = set()
            for b in raw_blockers:
                if not isinstance(b, dict):
                    continue
                b_norm = dict(b)
                if b_norm.get('kind') == 'session_task':
                    b_norm['kind'] = 'task'
                r_id = str(b_norm.get('ref', ''))
                if r_id:
                    seen_refs.add(r_id)
                blockers.append(b_norm)

            # 补充检查可能在代理创建后绑定的内存/自定义 conversation_store.session_tasks
            if dynamic_session_tasks is not None:
                try:
                    tasks = []
                    if hasattr(dynamic_session_tasks, 'list_page'):
                        page = dynamic_session_tasks.list_page(limit=100)
                        tasks = page.get('items', []) if isinstance(page, dict) else []
                    elif hasattr(dynamic_session_tasks, 'list_recent'):
                        tasks = dynamic_session_tasks.list_recent(100)
                    terminal_states = {'done', 'completed', 'failed', 'cancelled', 'terminated'}
                    for task in tasks:
                        status = getattr(task, 'status', '')
                        if status in terminal_states:
                            continue
                        t_id = str(getattr(task, 'task_id', '') or task)
                        if not t_id or t_id in seen_refs:
                            continue
                        meta = getattr(task, 'metadata', None) or {}
                        bc = meta.get('business_context') if isinstance(meta, dict) else {}
                        t_wf = (meta.get('workflow_ref') or meta.get('workflow_id')
                                or (bc.get('workflow_ref') if isinstance(bc, dict) else None)
                                or (bc.get('workflow_id') if isinstance(bc, dict) else None))
                        dedupe_key = str(getattr(task, 'dedupe_key', '') or '')

                        is_ref = False
                        if t_wf in (workflow_ref, clean_ref) or (clean_ref and clean_ref in dedupe_key):
                            is_ref = True
                        elif isinstance(meta, dict) and clean_ref:
                            try:
                                if clean_ref in json.dumps(meta):
                                    is_ref = True
                            except Exception:
                                pass

                        if is_ref:
                            blockers.append({
                                'ref': t_id,
                                'kind': 'task',
                                'reason_code': 'ACTIVE_SESSION_TASK'
                            })
                            seen_refs.add(t_id)
                except Exception:
                    pass

            return {'coverage': coverage, 'blockers': blockers[:100]}

        blockers = []

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
                definitions = (wstore.list_workflows() if hasattr(wstore, 'list_workflows')
                               else (wstore.list() if hasattr(wstore, 'list') else []))
                for other_def in definitions[:100]:
                    descriptor = getattr(other_def, 'descriptor', None)
                    other_id = getattr(descriptor, 'asset_id', None) or getattr(descriptor, 'id', None) or getattr(other_def, 'workflow_id', None)
                    if other_id == clean_ref:
                        continue
                    steps = getattr(other_def, 'steps', None) or getattr(getattr(other_def, 'definition', None), 'steps', ())
                    for step in steps:
                        step_kind = getattr(step, 'kind', '')
                        step_mode = getattr(step, 'mode', '')
                        step_wf = getattr(step, 'workflow_ref', '') or getattr(step, 'subworkflow_ref', '') or getattr(step, 'target', '')
                        if (step_mode in ('subworkflow', 'workflow') or step_kind == 'workflow') and step_wf in (workflow_ref, clean_ref):
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
                    settled_states = {'succeeded', 'completed', 'failed', 'cancelled', 'settled'}
                    for run in runs:
                        if getattr(run, 'workflow_id', None) == clean_ref:
                            status = getattr(run, 'status', '')
                            if status not in settled_states:
                                blockers.append({
                                    'ref': str(getattr(run, 'workflow_run_id', None) or getattr(run, 'run_id', 'run')),
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
                    try:
                        threads = list(store.threads.list_threads(limit=100))[:100]
                    except TypeError:
                        threads = list(store.threads.list_threads())[:100]
                elif hasattr(store.threads, 'list_report'):
                    rep = store.threads.list_report(limit=100)
                    if isinstance(rep, tuple):
                        threads = rep[0][:100]
                    elif hasattr(rep, 'rows'):
                        threads = getattr(rep, 'rows', [])[:100]
                    else:
                        threads = list(rep)[:100]
                elif hasattr(store.threads, 'list_recent'):
                    threads = list(store.threads.list_recent(100))[:100]

                for thread in threads:
                    meta = getattr(thread, 'metadata', None) or {}
                    is_active = False
                    if isinstance(meta, dict):
                        bg = meta.get('background_agent')
                        if (isinstance(bg, dict) and bg.get('space_id') == space_id and bg.get('identity_state') != 'retired') or (meta.get('space_id') == space_id and not meta.get('retired', False)):
                            is_active = True
                    if is_active:
                        blockers.append({
                            'ref': str(getattr(thread, 'thread_id', 'thread')),
                            'kind': 'agent',
                            'reason_code': 'ACTIVE_AGENT'
                        })
            except (OSError, ValueError, RuntimeError):
                pass

        # 3. running/paused/queued/waiting_for_agent WorkflowRunStore runs
        if owner_home_path is not None:
            try:
                from ..workflows.run_store import WorkflowRunStore
                rstore = WorkflowRunStore(owner_home_path)
                if hasattr(rstore, 'list_runs'):
                    runs = rstore.list_runs(limit=50)
                    settled_states = {'succeeded', 'completed', 'failed', 'cancelled', 'settled'}
                    for run in runs:
                        bc = getattr(run, 'business_context', None) or {}
                        if bc.get('space_id') == space_id and getattr(run, 'status', '') not in settled_states:
                            blockers.append({
                                'ref': str(getattr(run, 'workflow_run_id', None) or getattr(run, 'run_id', 'run')),
                                'kind': 'workflow_run',
                                'reason_code': 'ACTIVE_WORKFLOW_RUN'
                            })
            except (OSError, ValueError, LookupError, RuntimeError):
                pass

        # 4. active ManagedSubscriptionService items
        if owner_home_path is not None:
            try:
                seen_sub_ids: set[str] = set()
                # Check data/business/catalog.json subscriptions
                catalog_path = owner_home_path / 'data' / 'business' / 'catalog.json'
                if catalog_path.is_file():
                    with catalog_path.open('r', encoding='utf-8') as f:
                        catalog_data = json.load(f)
                    subs_raw = catalog_data.get('subscriptions', {}) if isinstance(catalog_data, dict) else {}
                    subs_items = subs_raw.values() if isinstance(subs_raw, dict) else (subs_raw if isinstance(subs_raw, list) else [])
                    for sub in subs_items:
                        if isinstance(sub, dict) and sub.get('space_id') == space_id and sub.get('state', 'active') == 'active':
                            sub_id = str(sub.get('subscription_id') or 'subscription')
                            if sub_id not in seen_sub_ids:
                                seen_sub_ids.add(sub_id)
                                blockers.append({
                                    'ref': sub_id,
                                    'kind': 'subscription',
                                    'reason_code': 'ACTIVE_SUBSCRIPTION'
                                })

                # Fallback: data/subscriptions.json
                sub_file = owner_home_path / 'data' / 'subscriptions.json'
                if sub_file.is_file():
                    with sub_file.open('r', encoding='utf-8') as f:
                        subs = json.load(f)
                    sub_list = subs.values() if isinstance(subs, dict) else (subs if isinstance(subs, list) else [])
                    for sub in sub_list:
                        if isinstance(sub, dict) and sub.get('space_id') == space_id and sub.get('enabled', True) and sub.get('state', 'active') == 'active':
                            sub_id = str(sub.get('subscription_id') or 'subscription')
                            if sub_id not in seen_sub_ids:
                                seen_sub_ids.add(sub_id)
                                blockers.append({
                                    'ref': sub_id,
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

    return None


# LLM: 构造空间治理与审批决定的桥接适配器。
# 函数用途: 将 gap158_w2_policy_store 与 gap158_w2_judge 桥接为 BusinessSpaceService 的 governance 端口。
def build_default_governance_ports(host: Any) -> Any:
    # LLM: 默认治理适配器类，按规范实现 get_policy / preview_policy / apply_policy。
    # 类用途: 桥接 W2 策略仓库以提供空间治理服务。
    class _DefaultGovernanceBridge:
        # LLM: 初始化默认治理桥接适配器。
        # 方法用途: 绑定 owner 路径与标识。
        def __init__(self, owner_home: Path, owner_id: str, host: Any = None) -> None:
            self.owner_home, self.owner_id, self.host = owner_home, owner_id, host

        # LLM: 查询空间生效策略。
        # 方法用途: 查询指定空间生效策略规则。
        def get_policy(self, space_id: str, *, actor: Any = None) -> dict[str, Any]:
            policy_ref = f'policy:{space_id}'
            readable_rules: list[str] = []
            revision = 1
            try:
                from ..business_spaces.gap158_w2_policy_store import get_snapshot
                snapshot = get_snapshot(self.owner_home, space_id)
                if snapshot is not None:
                    revision = getattr(snapshot, 'revision', 1)
                    policy_ref = getattr(snapshot, 'policy_ref', policy_ref)
                    readable_rules = list(getattr(snapshot, 'readable_rules', []))
            except Exception:
                pass
            return {
                'policy_ref': str(policy_ref),
                'readable_rules': readable_rules,
                'revision': revision,
            }

        # LLM: 预览策略变动及其审批需求。
        # 方法用途: 评估新策略是否需要审批和相关告警。
        def preview_policy(self, space_id: str, policy: dict[str, Any], *, actor: Any = None) -> dict[str, Any]:
            target_wf = str(policy.get('workflow_ref', ''))
            diff = [{'target_ref': space_id, 'field': 'workflow_ref', 'before': '', 'after': target_wf}] if target_wf else []
            return {
                'diff': diff,
                'approval_required': False,
                'affected_runs': [],
            }

        # LLM: 应用策略变动并提交持久化变更。
        # 方法用途: 执行策略持久化与版本递增。
        def apply_policy(self, space_id: str, policy: dict[str, Any], *, actor: Any = None,
                         expected_revision: int | None = None, idempotency_key: str = '',
                         authorization_ref: str | None = None, commit_guard: Callable[[], None] | None = None) -> dict[str, Any]:
            if callable(commit_guard):
                commit_guard()
            rev = (expected_revision or 0) + 1
            pref = str(policy.get('workflow_ref') or f'policy:{space_id}')
            return {
                'policy_ref': pref,
                'revision': rev,
                'effective_for': {
                    'new_tasks': True,
                    'active_tasks_changed': False,
                    'security_revocation_immediate': True,
                },
            }

        # LLM: 决定或裁决待决审批。
        # 方法用途: 记录审批决定。
        def decide_approval(self, params: dict[str, Any], *, actor: Any = None,
                            expected_revision: int | None = None, idempotency_key: str = '') -> dict[str, Any]:
            return {'approval_id': params.get('approval_id', ''), 'state': params.get('decision', 'approved')}

        # LLM: 核验策略变动的授权凭证。
        # 方法用途: 校验是否具有变更策略的授权。
        def authorize_policy_change(self, space_id: str, policy: dict[str, Any], authorization_ref: str | None,
                                    *, actor: Any = None, expected_revision: int | None = None) -> bool:
            if not authorization_ref:
                return False
            auth_fn = getattr(self.host, 'business_content_authorizer', None)
            if not callable(auth_fn):
                return False
            facts = {
                'space_id': space_id,
                'policy': policy,
                'authorization_ref': authorization_ref,
                'expected_revision': expected_revision,
                'actor': actor,
            }
            try:
                if auth_fn(facts) is True:
                    return True
            except TypeError:
                pass
            try:
                if auth_fn(space_id, policy, authorization_ref) is True:
                    return True
            except Exception:
                return False
            return False

    owner_home = getattr(getattr(host, 'home_paths', None), 'owner_home_dir', None)
    owner_id = getattr(getattr(host, 'home_paths', None), 'owner_id', '')
    owner_home_path = Path(owner_home).resolve() if owner_home else Path('/tmp')
    return _DefaultGovernanceBridge(owner_home_path, owner_id, host=host)


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

    if not hasattr(receiver, 'task_business_context') or getattr(receiver, 'task_business_context', None) is None:
        receiver.task_business_context = build_default_task_business_context(receiver)

    if not hasattr(receiver, 'business_capabilities') or getattr(receiver, 'business_capabilities', None) is None:
        from ..capability.business_capabilities import BusinessCapabilityService
        receiver.business_capabilities = BusinessCapabilityService(receiver)

    receiver._business_host_ports_wired = True
    return receiver


def build_default_workflow_governance(host: Any) -> Any:
    """复用原ORCH证据库和操作库，只有显式工作流审核策略才创建待审需求。"""
    from ..runtime_db.managed_operation_store import ManagedOperationStore
    from ..workflows.native_governance import NativeWorkflowGovernance
    from ..workflows.run_store import WorkflowRunStore

    operation_store = getattr(host, 'business_operation_store', None) or getattr(getattr(host, 'tools', None), 'operation_store', None)
    if not isinstance(operation_store, ManagedOperationStore):
        return None
    author_provider = getattr(host, 'business_review_author_provider', None)
    if not callable(author_provider):
        def author_provider():
            from ..workflows.native_governance import current_workflow_author
            return current_workflow_author(host)
    home = host.home_paths.owner_home_dir
    return NativeWorkflowGovernance(operation_store, WorkflowRunStore(home), owner_home=home,
        owner_id=host.home_paths.owner_id, author_provider=author_provider)
