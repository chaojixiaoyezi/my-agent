"""旧多进程设施的离线迁移入口；导入不读取或写入运行目录。"""


def run_single_process_migration(paths, *, dry_run):
    """在同一生命周期边界退役旧设施；不启动业务或授予执行权限。"""
    from .journal import run_migration

    return run_migration(paths, dry_run=dry_run)


def migration_module_names():
    """由实际安装的包成员得出离线模块集合，不维护第二份模块清单。"""
    import pkgutil

    return frozenset(module.name for module in pkgutil.iter_modules(__path__, __name__ + '.'))
