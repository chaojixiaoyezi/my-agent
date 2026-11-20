# LLM: 原生业务宿主端口与引用解析适配；提供空间/工作流引用解析、活跃引用探测与治理端口桥接。
# 模块用途: 实现 WS-A1 业务宿主标准端口，拆分空间与工作流引用读者，装配治理与上下文解析。
from __future__ import annotations

import inspect
import json
import os
import stat
from collections import OrderedDict
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import Lock
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


def _fail_host_not_ready(reason: str, next_action: str, cause: BaseException | None = None) -> Any:
    from ..conversation.business_access import BusinessError

    error = BusinessError(
        'HOST_NOT_READY',
        details={
            'reason': reason,
            'effect_outcome': 'not_started',
            'next_action': next_action,
        },
    )
    if cause is not None:
        raise error from cause
    raise error


def _extract_thread_report(rep: Any, *, strict: bool) -> list[Any]:
    if isinstance(rep, tuple):
        rows = list(rep[0])
        errors = rep[1] if len(rep) > 1 else None
        if strict and errors:
            _fail_host_not_ready('THREAD_STORE_UNREADABLE', '线程记录存在损坏或不可读项，修复来源后重试空间归档。')
        return rows
    if isinstance(rep, dict):
        if strict and (rep.get('errors') or rep.get('partial') or rep.get('truncated')):
            _fail_host_not_ready('THREAD_SCAN_INCOMPLETE', '线程扫描未完整完成，确认宿主线程存储可用后重试空间归档。')
        return list(rep.get('rows', rep.get('items', [])))
    if hasattr(rep, 'rows'):
        if strict and (getattr(rep, 'errors', None) or getattr(rep, 'partial', False) or getattr(rep, 'truncated', False)):
            _fail_host_not_ready('THREAD_SCAN_INCOMPLETE', '线程扫描未完整完成，确认宿主线程存储可用后重试空间归档。')
        return list(getattr(rep, 'rows', []) or [])
    return list(rep)


def _reference_page(store, *, limit, cursor, source):
    from ..conversation.session_tasks import SessionTaskStore

    options = {} if cursor is None else {'cursor': cursor}
    if isinstance(store, SessionTaskStore):
        options['read_only'] = True
    page = store.list_page(limit=limit, **options)
    if not isinstance(page, dict) or not isinstance(page.get('items'), list) or len(page['items']) > limit:
        _fail_host_not_ready(f'{source}_SCAN_INVALID', '分页回执格式无效，修复来源后重试。')
    if (page.get('coverage', 'complete') != 'complete' or page.get('load_errors') or page.get('errors')
            or page.get('partial') or page.get('truncated') or page.get('incomplete')):
        _fail_host_not_ready(f'{source}_SCAN_INCOMPLETE', '来源尚未完整可读，核对索引后重试。')
    following = page.get('next_cursor')
    count = page.get('read_count', len(page['items']))
    if type(count) is not int or not len(page['items']) <= count <= limit:
        _fail_host_not_ready(f'{source}_SCAN_INVALID', '分页返回了无效计量，修复来源后重试。')
    if following is not None and (following == cursor or not isinstance(following, (str, dict))):
        _fail_host_not_ready(f'{source}_CURSOR_STALLED', '分页续点未推进，核对来源后重试。')
    return page['items'], following, count


def _thread_reference_page(store, *, limit, cursor):
    from ..memory_store.retention_scan_budget import RetentionScanBudget

    if callable(getattr(store, 'page_report', None)):
        budget = RetentionScanBudget.default()
        budget.max_entries = min(limit, budget.max_entries)
        names, errors, following, complete = store.page_report(cursor=cursor, limit=limit, budget=budget)
        if errors:
            _fail_host_not_ready('THREAD_SCAN_INCOMPLETE', '线程来源存在读取错误，修复后重试。')
        rows = [store.load(name) for name in names]
        if any(row is None for row in rows):
            _fail_host_not_ready('THREAD_SCAN_INCOMPLETE', '扫描期间线程来源变化，重试以读取当前版本。')
        return rows, None if complete else following, budget.entries
    if callable(getattr(store, 'list_page', None)):
        return _reference_page(store, limit=limit, cursor=cursor, source='THREAD')
    # 保留只有有界列表端口的旧宿主；达到上限时无法证明完整覆盖。
    legacy = getattr(store, 'list_report', None) or getattr(store, 'list_threads', None) or getattr(store, 'list_recent', None)
    if cursor is not None or not callable(legacy):
        _fail_host_not_ready('THREAD_SCAN_INCOMPLETE', '宿主需要提供可验证的线程分页端口。')
    response = legacy(limit)
    rows = _extract_thread_report(response, strict=True) if isinstance(response, (tuple, dict)) else list(response)
    if len(rows) >= limit:
        _fail_host_not_ready('THREAD_WINDOW_TRUNCATED', '线程数量超出旧端口窗口，请启用线程分页。')
    return rows, None, len(rows)


def _iter_reference_threads(store):
    cursor = None
    while True:
        rows, following, _ = _thread_reference_page(store, limit=100, cursor=cursor)
        yield from rows
        if following is None:
            return
        if following == cursor:
            _fail_host_not_ready('THREAD_CURSOR_STALLED', '线程分页续点未推进，请核对来源后重试。')
        cursor = following


def _optional_stat(path: Path) -> os.stat_result | None:
    """探测可选路径元数据时只把 FileNotFoundError 当缺失，避免吞掉读盘故障。"""
    try:
        return path.stat()
    except FileNotFoundError:
        return None


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
        if bc.get('task_id') != str(task_id):
            return None
        return BusinessExecutionContext.from_dict(bc)
    return task_business_context


def build_default_business_package_reference_resolver(host: Any) -> Callable[[str, str, Any], dict[str, Any] | None]:
    """按规范Owner安装表解析内容包，不装配执行端口，不以挂载授予工具或插件权限。"""
    owner_id = getattr(getattr(host, 'home_paths', None), 'owner_id', '')
    owner_home = getattr(getattr(host, 'home_paths', None), 'owner_home_dir', None)
    home = Path(owner_home).absolute() if owner_home else None

    def resolve(kind, ref_id, actor):
        from ..capability.package_provider import _owner_from_home
        from ..conversation.business_access import BusinessError, require_actor
        from ..plugin_install_store import PluginInstallStore
        from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home

        if kind != 'package' or not isinstance(ref_id, str) or not ref_id:
            return None
        if home is None or not owner_id:
            raise BusinessError('HOST_NOT_READY', details={'reason': 'PACKAGE_OWNER_SOURCE_UNAVAILABLE',
                'effect_outcome': 'not_started', 'next_action': '恢复宿主当前Owner的规范路径后重新读取包目录。'})
        if actor is not None:
            require_actor(actor)
            if actor.owner_id != owner_id or Path(actor.service.owner_root).absolute() != home:
                raise BusinessError('SPACE_SCOPE_MISMATCH')
        try:
            owner = _owner_from_home(home)
            if owner.owner_id != owner_id:
                raise BusinessError('SPACE_SCOPE_MISMATCH')
            packages = list(PluginInstallStore(owner).snapshot())
            if owner.identity != OwnerIdentity.local_main():
                public_owner = resolve_owner_home(owner.root, OwnerIdentity.local_main())
                packages.extend(item for item in PluginInstallStore(public_owner).snapshot()
                                if item.installation_scope == 'public')
        except BusinessError:
            raise
        except (OSError, ValueError, RuntimeError) as error:
            raise BusinessError('STORE_UNAVAILABLE', details={'reason': 'PACKAGE_INSTALL_SOURCE_UNREADABLE',
                'exception_type': type(error).__name__, 'effect_outcome': 'not_started',
                'next_action': '核对当前Owner原安装表及读取权限；不要把来源故障当作包不存在，修复后重读。'}) from error
        for package in packages:
            if not package.manifest.is_capability_pack:
                continue
            identifier = ('public/' if package.installation_scope == 'public' else '') + package.manifest.plugin_id
            if ref_id not in (identifier, f'capability:{identifier}'):
                continue
            value = {'owner_id': owner_id, 'ref': ref_id, 'package_ref': identifier,
                'state': 'available' if package.enabled else 'disabled', 'kind': 'package',
                'version_ref': package.package_sha256, 'name': identifier}
            if package.activation_id:
                value['activation_id'] = package.activation_id
            return value
        return None

    return resolve


# LLM: 构造空间 10 类对象引用的默认解析器，只返回元数据定位字段，不复制正文与任意元数据。
# 函数用途: 建立 3 参数 (kind, ref_id, actor) 空间外部引用解析函数。
def build_default_business_space_reference_resolver(host: Any) -> Callable[[str, str, Any], dict[str, Any] | None]:
    owner_id = getattr(getattr(host, 'home_paths', None), 'owner_id', '')
    owner_home = getattr(getattr(host, 'home_paths', None), 'owner_home_dir', None)
    owner_home_path = Path(owner_home).resolve() if owner_home else None
    package_resolver = build_default_business_package_reference_resolver(host)

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

        # 2. package: 与默认空间挂载共用原Owner安装表，只投影内容元数据。
        if kind == 'package':
            return package_resolver(kind, ref_id, actor)

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
                    for th in _iter_reference_threads(store.threads):
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

        # 原生登记只核元数据与原执行归属，不读取内容或扩展到任务声明产物。
        if kind == 'artifact':
            from ..conversation.business_access import BusinessActor, BusinessError, require_actor
            from ..conversation.registered_artifact_observation import registered_artifact
            from ..conversation.workflow_artifact_observation import workflow_artifact
            from .business_api_v13_readers import _OwnerReaders

            require_actor(actor)
            if actor.owner_id != owner_id or owner_home_path is None:
                return None
            from .business_api import _local_business_services

            observation = _local_business_services(host).observation
            store, runtime = observation.store, observation.runtime
            if store is None or runtime is None:
                raise BusinessError('HOST_NOT_READY', details={
                    'reason': 'NATIVE_ARTIFACT_AUTHORITY_UNAVAILABLE',
                    'side_effect_state': 'none',
                    'next_action': '恢复当前Owner的原会话存储与运行库后重试登记；不要把来源缺失当作产物不存在。'})
            observer = BusinessActor(owner_id, actor.subject_id, observation, actor.authorize)
            source = _OwnerReaders(owner_home_path, owner_id, store, runtime,
                workspace_task_path_template=getattr(host.config, 'workspace_task_path_template', None))
            reader = workflow_artifact if ref_id.startswith('wfart-') else registered_artifact
            found = reader(source, ref_id, with_content=False, authority=(observation, observer, 'artifacts.get'))
            if found is None:
                return None
            facts, record, _ = found
            if not facts.value['available']:
                return None
            return {'owner_id': owner_id, 'ref': ref_id, 'artifact_id': ref_id,
                'version_ref': facts.value['version_ref'], 'content_sha256': record.sha256,
                'space_id': facts.space_id, 'project_ref': facts.project_ref, 'state': 'available'}

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


def _read_optional_reference_json(path: Path) -> Any | None:
    """直接打开并解析可选引用 JSON，只把 FileNotFoundError 视作缺失，保留其它存储错误。"""
    try:
        with path.open('r', encoding='utf-8') as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None


def build_default_business_active_references(host: Any) -> Callable[[str, Any], list[dict[str, str]]]:
    from ..conversation.background_team_lifecycle import has_open_obligation as identity_is_open
    from ..conversation.business_access import BusinessError
    from ..conversation.managed_subscriptions import has_open_obligation as subscription_is_open
    from ..conversation.session_tasks import has_open_obligation as task_is_open
    from ..memory_store.retention_scan_budget import RetentionScanBudget
    from ..workflows.run_store import WorkflowRunStore
    from ..workflows.run_store import has_open_obligation as run_is_open

    owner_home = getattr(getattr(host, 'home_paths', None), 'owner_home_dir', None)
    owner = Path(owner_home).resolve() if owner_home else None
    receipts = OrderedDict()
    guard = Lock()

    def source_versions():
        store = getattr(host, 'conversation_store', None)
        storage = getattr(store, 'storage', None)
        paths = [getattr(storage, name, None) for name in ('threads_dir', 'session_tasks_dir')]
        if owner is not None:
            paths.extend(owner / name for name in (
                'data/workflow_runs', 'data/workflow_run_index.sqlite3',
                'data/workflow_run_index.sqlite3-wal', 'data/business/catalog.json', 'data/subscriptions.json',
            ))
        versions = []
        for path in paths:
            if path is None:
                continue
            info = _optional_stat(Path(path))
            versions.append(None if info is None else (
                info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            ))
        return tuple(versions)

    def read_source_versions():
        try:
            return source_versions()
        except OSError as error:
            _fail_host_not_ready('REFERENCE_VERSION_UNREADABLE', '来源版本不可读，核对存储后重试。', error)

    def task_blocker(task, space_id):
        row = task if isinstance(task, Mapping) else {
            'task_id': getattr(task, 'task_id', 'task'),
            'status': getattr(task, 'status', None), 'metadata': getattr(task, 'metadata', {}),
        }
        metadata = row.get('metadata') or {}
        context = metadata.get('business_context') or {}
        if space_id == (metadata.get('space_id') or context.get('space_id')) and task_is_open(row):
            return {'ref': str(row.get('task_id') or 'task'), 'kind': 'task', 'reason_code': 'ACTIVE_TASK'}
        return None

    def thread_blocker(thread, space_id):
        metadata = getattr(thread, 'metadata', None) or {}
        identity = metadata.get('background_agent')
        if not isinstance(identity, Mapping) and metadata.get('space_id') == space_id:
            identity = {**metadata, 'identity_state': 'retired' if metadata.get('retired') else 'available'}
        if isinstance(identity, Mapping) and identity.get('space_id') == space_id and identity_is_open(identity):
            return {'ref': str(getattr(thread, 'thread_id', 'thread')), 'kind': 'agent', 'reason_code': 'ACTIVE_AGENT'}
        return None

    def run_blocker(run, space_id):
        context = run.get('business_context') or {}
        if context.get('space_id') == space_id and run_is_open(run):
            return {'ref': run['workflow_run_id'], 'kind': 'workflow_run', 'reason_code': 'ACTIVE_WORKFLOW_RUN'}
        return None

    def subscription_blocker(space_id):
        if owner is None:
            return None
        try:
            catalog = _read_optional_reference_json(owner / 'data/business/catalog.json')
            if catalog is not None and not isinstance(catalog, dict):
                raise ValueError('invalid subscription catalog')
            for old_layout in (False, True):
                rows = (_read_optional_reference_json(owner / 'data/subscriptions.json') if old_layout
                        else (catalog or {}).get('subscriptions', {}))
                if rows is None:
                    continue
                if isinstance(rows, Mapping):
                    rows = rows.values()
                elif not isinstance(rows, list):
                    raise ValueError('invalid subscription records')
                for row in rows:
                    if not isinstance(row, dict):
                        raise ValueError('invalid subscription record')
                    if row.get('space_id') != space_id:
                        continue
                    state = row.get('state', 'active')
                    if old_layout and row.get('enabled') is False and state == 'active':
                        state = 'disabled'
                    if subscription_is_open({**row, 'state': state}):
                        return {'ref': str(row.get('subscription_id') or 'subscription'),
                                'kind': 'subscription', 'reason_code': 'ACTIVE_SUBSCRIPTION'}
        except (OSError, ValueError, RuntimeError) as error:
            _fail_host_not_ready('SUBSCRIPTION_SCAN_FAILED', '无法核对订阅责任，修复来源后重试。', error)
        return None

    def active_references(space_id: str, actor: Any) -> list[dict[str, str]]:
        if not isinstance(space_id, str) or not space_id:
            return []
        versions = read_source_versions()
        with guard:
            previous = receipts.pop(space_id, None)
        source, cursor, checked = previous[1:] if previous is not None and previous[0] == versions else (0, None, 0)
        budget = RetentionScanBudget.default(getattr(host, 'config', None))
        store = getattr(host, 'conversation_store', None)
        while source < 3 and not budget.exhausted():
            limit = min(100, budget.max_entries - budget.entries)
            if source == 0:
                task_store = getattr(store, 'session_tasks', None)
                if task_store is None:
                    source, cursor = source + 1, None
                    continue
                reader, blocker, label = task_store, task_blocker, 'SESSION_TASK'
            elif source == 1:
                reader, blocker, label = getattr(store, 'threads', None), thread_blocker, 'THREAD'
                if reader is None:
                    source, cursor = source + 1, None
                    continue
            else:
                if owner is None:
                    source, cursor = source + 1, None
                    continue
                reader, blocker, label = WorkflowRunStore(owner), run_blocker, 'WORKFLOW_RUN'
            try:
                if source == 1:
                    rows, following, observed = _thread_reference_page(reader, limit=limit, cursor=cursor)
                else:
                    rows, following, observed = _reference_page(reader, limit=limit, cursor=cursor, source=label)
                for row in rows:
                    opened = blocker(row, space_id)
                    if opened is not None:
                        return [opened]
            except BusinessError:
                raise
            except (OSError, ValueError, LookupError, TypeError, AttributeError, RuntimeError) as error:
                reason = 'WORKFLOW_RUN_UNREADABLE' if source == 2 else f'{label}_SCAN_FAILED'
                _fail_host_not_ready(reason, '责任来源不可读，修复来源后重试空间归档。', error)
            budget.charge(entries=observed)
            checked += observed
            if following is None:
                source, cursor = source + 1, None
            else:
                cursor = following
                if observed == 0:
                    break
        if source < 3:
            with guard:
                receipts[space_id] = (versions, source, cursor, checked)
                while len(receipts) > 100:
                    receipts.popitem(last=False)
            raise BusinessError('HOST_NOT_READY', details={
                'reason': 'OPEN_OBLIGATION_SCAN_PENDING', 'effect_outcome': 'not_started',
                'checked_entries': checked, 'source': ('session_tasks', 'threads', 'workflow_runs')[source],
                'next_action': '使用原空间和幂等键重试归档，以继续检查余下的责任条目。',
            })
        opened = subscription_blocker(space_id)
        if opened is not None:
            return [opened]
        if read_source_versions() != versions:
            _fail_host_not_ready('OPEN_OBLIGATION_SOURCE_CHANGED', '归档来源版本已变化，请使用原空间和幂等键重试。')
        return []

    active_references.source_versions = source_versions
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
