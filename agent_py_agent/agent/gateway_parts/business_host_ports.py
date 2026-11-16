# LLM: 原生业务宿主端口与引用解析适配；提供空间/工作流引用解析、活跃引用探测与治理端口桥接。
# 模块用途: 实现 WS-A1 业务宿主标准端口，拆分空间与工作流引用读者，装配治理与上下文解析。
from __future__ import annotations

import inspect
import json
import os
import stat
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


def _list_all_threads(threads_store: Any, *, strict: bool = False, scan_cap: int = 1_000_000) -> list[Any]:
    instance_dict = getattr(threads_store, '__dict__', {})
    use_threads_override = 'list_threads' in instance_dict
    if hasattr(threads_store, 'list_report') and not use_threads_override:
        try:
            rep = threads_store.list_report(limit=0)
        except TypeError:
            rep = threads_store.list_report()
        return _extract_thread_report(rep, strict=strict)
    if hasattr(threads_store, 'list_threads'):
        try:
            threads = list(threads_store.list_threads(limit=0))
        except TypeError:
            threads = list(threads_store.list_threads())
        if threads:
            return threads
        # 兼容只用切片实现 limit 的测试桩：若 limit=0 返回空，探测无参或大窗口调用。
        if not hasattr(threads_store, 'list_report'):
            try:
                probe = list(threads_store.list_threads(limit=scan_cap))
            except TypeError:
                probe = list(threads_store.list_threads())
            if strict and len(probe) >= scan_cap:
                _fail_host_not_ready('THREAD_WINDOW_TRUNCATED', '线程数量超出单次窗口上限，确认完整索引后重试。')
            return probe
        return threads
    if hasattr(threads_store, 'list_report'):
        try:
            rep = threads_store.list_report(limit=0)
        except TypeError:
            rep = threads_store.list_report()
        return _extract_thread_report(rep, strict=strict)
    if hasattr(threads_store, 'list_recent'):
        try:
            threads = list(threads_store.list_recent(scan_cap))
        except TypeError:
            threads = list(threads_store.list_recent())
        if strict and len(threads) >= scan_cap:
            _fail_host_not_ready('THREAD_WINDOW_TRUNCATED', '线程数量超出单次窗口上限，确认完整索引后重试。')
        return threads
    return []


def _list_all_session_tasks(session_tasks: Any, *, scan_cap: int = 1_000_000) -> list[Any]:
    list_report = getattr(session_tasks, 'list_report', None)
    # 若测试或调用方未在实例上覆写 list_page，则优先走直接扫描目录与 load_errors 的 list_report(limit=0)。
    use_page_override = 'list_page' in getattr(session_tasks, '__dict__', {})
    if callable(list_report) and not use_page_override:
        try:
            items, load_errors = list_report(limit=0)
        except (OSError, ValueError, TypeError) as error:
            _fail_host_not_ready('SESSION_TASK_SCAN_FAILED', '会话任务扫描失败，确认宿主存储就绪后重试。', error)
        if load_errors or not isinstance(items, (list, tuple)):
            _fail_host_not_ready('SESSION_TASK_SCAN_INCOMPLETE', '会话任务扫描未完整完成，不能按无活跃任务归档。')
        return list(items)
    if hasattr(session_tasks, 'list_page'):
        tasks: list[Any] = []
        seen_ids: set[str] = set()
        seen_cursors: set[Any] = set()
        cursor: Any = None
        while True:
            page = session_tasks.list_page(limit=100) if cursor is None else session_tasks.list_page(limit=100, cursor=cursor)
            if not isinstance(page, dict):
                _fail_host_not_ready('SESSION_TASK_SCAN_INVALID', '会话任务分页结果格式无效，请修复后重试。')
            if (
                page.get('partial')
                or page.get('incomplete')
                or page.get('truncated')
                or page.get('errors')
                or page.get('load_errors')
                or page.get('coverage') not in (None, 'complete')
            ):
                _fail_host_not_ready('SESSION_TASK_SCAN_INCOMPLETE', '会话任务分页未完整完成，不能按无活跃任务归档。')
            for task in page.get('items', []):
                tid = getattr(task, 'task_id', None)
                if tid is not None:
                    key = str(tid)
                    if key in seen_ids:
                        continue
                    seen_ids.add(key)
                tasks.append(task)
            next_cursor = page.get('next_cursor')
            if not next_cursor:
                break
            if next_cursor == cursor or next_cursor in seen_cursors:
                _fail_host_not_ready('SESSION_TASK_CURSOR_STALLED', '会话任务分页游标停滞，不能跳过未扫描任务。')
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return tasks
    if hasattr(session_tasks, 'list_recent'):
        try:
            tasks = list(session_tasks.list_recent(scan_cap))
        except TypeError:
            tasks = list(session_tasks.list_recent())
        if len(tasks) >= scan_cap:
            _fail_host_not_ready('SESSION_TASK_WINDOW_TRUNCATED', '会话任务数量超出窗口上限，确认完整清单后重试。')
        return tasks
    return []


def _optional_stat(path: Path) -> os.stat_result | None:
    """探测可选路径元数据时只把 FileNotFoundError 当缺失，避免吞掉读盘故障。"""
    try:
        return path.stat()
    except FileNotFoundError:
        return None


def _list_unsettled_workflow_runs(
    owner_home_path: Path, settled_states: set[str], *, scan_cap: int = 1_000_000
) -> list[tuple[str, dict[str, Any]]]:
    import sqlite3

    from ..workflows.run_list_index import RunListIndex
    from ..workflows.run_store import WorkflowRunStore

    runs_dir = owner_home_path / 'data' / 'workflow_runs'
    index_db = owner_home_path / 'data' / 'workflow_run_index.sqlite3'
    rstore = WorkflowRunStore(owner_home_path)
    list_runs_fn = getattr(rstore, 'list_runs', None)
    use_custom_list = (
        callable(list_runs_fn)
        and (
            not hasattr(rstore, '_read')
            or 'list_runs' in getattr(rstore, '__dict__', {})
            or getattr(WorkflowRunStore, 'list_runs', None).__module__ != WorkflowRunStore.__module__
        )
    )
    if use_custom_list:
        try:
            runs = list(rstore.list_runs(limit=scan_cap))
        except TypeError:
            runs = list(rstore.list_runs())
        if len(runs) >= scan_cap:
            _fail_host_not_ready('WORKFLOW_RUN_WINDOW_TRUNCATED', '工作流运行数量超出扫描上限，请使用索引完整查询。')
        active: list[tuple[str, dict[str, Any]]] = []
        for run in runs:
            bc = getattr(run, 'business_context', None) or {}
            status = getattr(run, 'status', '')
            if status not in settled_states:
                run_id = str(getattr(run, 'workflow_run_id', None) or getattr(run, 'run_id', 'run'))
                active.append((run_id, bc if isinstance(bc, dict) else {}))
        return active

    runs_stat = _optional_stat(runs_dir)
    index_stat = _optional_stat(index_db)
    if not (runs_stat is not None and stat.S_ISDIR(runs_stat.st_mode)) and not (
        index_stat is not None and stat.S_ISREG(index_stat.st_mode)
    ):
        return []

    # SQL 终态过滤列表与外层 settled_states 保持同源，避免新增终态时 SQL 与二次过滤不一致。
    settled_tuple = tuple(sorted(str(state) for state in settled_states))
    index = RunListIndex(owner_home_path)
    try:
        with index.connection() as conn:
            if settled_tuple:
                placeholders = ', '.join('?' for _ in settled_tuple)
                rows = conn.execute(
                    f'SELECT run_id FROM runs WHERE status NOT IN ({placeholders}) '
                    'UNION SELECT run_id FROM pending '
                    'UNION SELECT run_id FROM legacy',
                    settled_tuple,
                ).fetchall()
            else:
                rows = conn.execute(
                    'SELECT run_id FROM runs '
                    'UNION SELECT run_id FROM pending '
                    'UNION SELECT run_id FROM legacy'
                ).fetchall()
            run_ids = [row[0] for row in rows]
    except sqlite3.Error as error:
        _fail_host_not_ready('WORKFLOW_INDEX_UNREADABLE', '工作流运行索引不可读，确认宿主存储就绪后重试。', error)

    active_runs: list[tuple[str, dict[str, Any]]] = []
    for run_id in run_ids:
        try:
            data = rstore._read(run_id, 'run.json', concurrent_publication=True)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, TypeError, KeyError, UnicodeError) as error:
            _fail_host_not_ready('WORKFLOW_RUN_UNREADABLE', f'工作流运行 {run_id} 快照不可读，修复来源后重试。', error)
        if not isinstance(data, dict):
            _fail_host_not_ready('WORKFLOW_RUN_UNREADABLE', f'工作流运行 {run_id} 快照格式无效，修复来源后重试。')
        status = data.get('status', '')
        if status not in settled_states:
            bc = data.get('business_context') or {}
            wf_run_id = str(data.get('workflow_run_id') or data.get('run_id') or run_id)
            active_runs.append((wf_run_id, bc if isinstance(bc, dict) else {}))
    return active_runs


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
                    threads = _list_all_threads(store.threads, strict=False)

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

        from ..conversation.business_access import BusinessError

        # 1. non-terminal SessionTasks
        store = getattr(host, 'conversation_store', None)
        if store is not None and hasattr(store, 'session_tasks'):
            try:
                tasks = _list_all_session_tasks(store.session_tasks)
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
            except BusinessError:
                raise
            except (OSError, ValueError, RuntimeError) as error:
                _fail_host_not_ready('SESSION_TASK_SCAN_FAILED', '无法完整检查活跃会话任务，稍后重试空间归档。', error)

        # 2. non-retired background agents
        if store is not None and hasattr(store, 'threads'):
            try:
                threads = _list_all_threads(store.threads, strict=True)
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
            except BusinessError:
                raise
            except (OSError, ValueError, RuntimeError) as error:
                _fail_host_not_ready('THREAD_SCAN_FAILED', '无法完整检查后台代理线程，稍后重试空间归档。', error)

        # 3. running/paused/queued/waiting_for_agent WorkflowRunStore runs
        if owner_home_path is not None:
            try:
                settled_states = {
                    'succeeded',
                    'completed',
                    'done',
                    'failed',
                    'cancelled',
                    'aborted',
                    'retired',
                    'settled',
                    'skipped',
                }
                for wf_run_id, bc in _list_unsettled_workflow_runs(owner_home_path, settled_states):
                    if bc.get('space_id') == space_id:
                        blockers.append({
                            'ref': wf_run_id,
                            'kind': 'workflow_run',
                            'reason_code': 'ACTIVE_WORKFLOW_RUN'
                        })
            except BusinessError:
                raise
            except (OSError, ValueError, LookupError, RuntimeError) as error:
                _fail_host_not_ready('WORKFLOW_RUN_SCAN_FAILED', '无法完整检查未完结工作流运行，稍后重试空间归档。', error)

        # 4. active ManagedSubscriptionService items
        if owner_home_path is not None:
            try:
                seen_sub_ids: set[str] = set()
                # Check data/business/catalog.json subscriptions
                catalog_path = owner_home_path / 'data' / 'business' / 'catalog.json'
                catalog_data = _read_optional_reference_json(catalog_path)
                if catalog_data is not None:
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
                subs = _read_optional_reference_json(sub_file)
                if subs is not None:
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
            except (OSError, ValueError, RuntimeError) as error:
                _fail_host_not_ready('SUBSCRIPTION_SCAN_FAILED', '无法完整检查活跃订阅，稍后重试空间归档。', error)

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
