# LLM: 仅实际_LocalControlWriteSource工厂/固定端口调用；组合真实actor资源并使用原receipt-owner P2，不签target权或缓存HTTP来源。
# 模块用途: 保留操作者会话初始化与群身份使用的原线程，把actor和target两个写范围分开。
from .multiprocess_local_control import LocalControlCoordinatorUnavailable, _LocalControlWriteSource
from .multiprocess_route import owner_digest


# LLM: 只能由同原HTTP factory调用一次；显式home默认解析器来自C1唯一纯叶，Store初始化False零目录/模型IO。
# 函数用途: 保存真正的操作者会话资源，不接调用方Store、callback、路径或目标owner。
def _bind_actor_control_resources(source):
    if type(source) is not _LocalControlWriteSource:
        raise LocalControlCoordinatorUnavailable('control original actor resources are unavailable')
    from functools import partial
    from types import SimpleNamespace

    from ..settings.thread_model_selection import default_model_profile_id_for_home
    from .owner_conversation_store import owner_conversation_store_from_config
    from .owner_resources import resolve_owner_resource_configuration
    if source.http_origin is None or source._actor_control_resources is not None:
        raise LocalControlCoordinatorUnavailable('control original actor resources are unavailable')
    home, config, paths = resolve_owner_resource_configuration(source.context, source.receipt_owner)
    default = partial(default_model_profile_id_for_home, home)
    store = owner_conversation_store_from_config(config, home, initialize=False, model_default=default)
    host = SimpleNamespace(home_paths=home, config=config, conversation_store=store)
    source._actor_control_resources = (host, home, config, store, store.threads, store.storage, default,
                                     paths['conversation_workspace'])
    source._actor_control_resources_origin = source._actor_control_resources


# LLM: 原资源引用和canonical路径每次核同对象，不因target切换改变actor；None仅factory首次捕获前的真实未绑定状态。
# 函数用途: 拒绝替换会话Store、默认callback或目录后借原C门写入。
def _check_actor_control_resources(source):
    resources = source._actor_control_resources
    if resources is None and source._actor_control_resources_origin is None:
        return
    if resources is not source._actor_control_resources_origin:
        raise LocalControlCoordinatorUnavailable('control original actor resources are unavailable')
    host, home, config, store, threads, storage, default, workspace = resources
    if (host.home_paths is not home or host.config is not config or host.conversation_store is not store
            or store.threads is not threads or store.storage is not storage or threads.storage is not storage
            or threads.model_default is not default or storage.root != workspace
            or home.root != source.context.home_root):
        raise LocalControlCoordinatorUnavailable('control original actor resources are unavailable')


# LLM: 固定操作者端口只有原receipt owner P2，无callback/path/owner输入；原线程/索引同步事务只try锁，target资源另行绑定。
# 函数用途: 在选择目标前初始化原操作者目录和会话，保留原模型默认与群身份使用的真实线程。
def _prepare_actor_control_thread(source):
    if type(source) is not _LocalControlWriteSource:
        raise LocalControlCoordinatorUnavailable('control original actor resources are unavailable')
    source._require_current()
    if source._actor_control_resources is None or not source.http_origin[7].used:
        raise LocalControlCoordinatorUnavailable('control original actor resources are unavailable')
    results = []

    # LLM: Root内只有固定原目录/线程本地写；runtime反向门只try，完整当前Permission/executing/资源末核均在写前。
    # 函数用途: 让操作者自己的线程与通道索引只写进本人P2，不能借目标P2。
    def prepare():
        gate = source.http_origin[9]._gate
        if not gate.acquire(blocking=False):
            raise BlockingIOError('control HTTP resource gate is busy')
        try:
            source._require_current()
            host, home, _config, _store, threads, storage, _default, _workspace = source._actor_control_resources
            from .owner_management_control import _initialize_missing_owner
            _initialize_missing_owner(home)
            storage.ensure_dirs()
            scope = source.http_origin[5]
            thread = threads.get_or_create({'canonical_user_id': scope.user_id, 'owner_id': home.owner_id,
                'owner_home': str(home.owner_home_dir), 'channel': scope.channel,
                'channel_conversation_id': scope.conversation_id, 'channel_user_id': scope.user_id,
                'title': '会话设置'}, blocking=False)
            results.append((host, thread))
        finally:
            gate.release()

    decision = source.controller.launch_owner_writer(owner_digest(source.receipt_owner), prepare)
    if decision.get('status') != 'started' or len(results) != 1:
        raise LocalControlCoordinatorUnavailable('control coordinator generation is unavailable')
    return results[0]
