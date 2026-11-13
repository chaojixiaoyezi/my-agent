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
                            name = pkg.get('name') if isinstance(pkg, dict) else getattr(pkg, 'name', None)
                            if isinstance(name, str) and 0 < len(name) <= 160:
                                res['name'] = name
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


def build_default_business_workflow_reference_reader(host: Any) -> Callable[[str], dict[str, Any]]:
    """沿唯一引用读口检查清理阻断项，保留公开的 task 类型名称。"""
    from ..workflows.reference_sources import build_workflow_reference_reader

    reader = build_workflow_reference_reader(host)

    def workflow_reference_reader(workflow_ref: str) -> dict[str, Any]:
        # ConversationStore 可在宿主端口之后装配；每次读取准确的当前实例。
        tasks = getattr(getattr(host, 'conversation_store', None), 'session_tasks', None)
        if tasks is not None:
            reader.session_task_store = tasks
        result = reader(workflow_ref)
        return {**result, 'blockers': [
            {**blocker, 'kind': 'task'} if blocker.get('kind') == 'session_task' else blocker
            for blocker in result['blockers']
        ]}

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
