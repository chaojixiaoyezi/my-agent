# LLM: DBrow是原background_source的一种载体，不hash数据库文件；C只读existing runtime.db，owner来自原C/P5，W认领后不得再用旧摘要否定自己的claim。
# 模块用途: 将原运行库到期字条冻结为同一P3/P5运输引用，不构造业务Agent。
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from ..runtime_db.repository import RuntimeRepository
from ..runtime_db.schema import runtime_db_path
from ..user_space.owner_resolver import _owner_home_dir


# LLM: 原source末核可在C短门内，read_only+existing_only保持ro并复用零等待独立连接；缺库不创建，符号链接不改指向其它库。
# 函数用途: 取得DB来源的只读仓储。
def runtime_wake_reader(home):
    path = runtime_db_path(home)
    if path.is_symlink():
        raise ValueError('background_runtime_db_path_invalid')
    return RuntimeRepository(path, read_only=True, existing_only=True)


# LLM: 仅原canonical db/Store相对路径；source_id与revision分别绑定原wake_id和完整行摘要，不将查询引用变成执行权。
# 函数用途: 检查DB字条引用不冒用文件Wake路径。
def validate_runtime_wake_ref(ref):
    if ref['source_ref'] != 'runtime.db' or not ref['source_exists'] or ref['input_revision'] != ref['source_sha256']:
        raise ValueError('background_runtime_wake_ref_invalid')
    store = Path(ref['store_ref'])
    if not ref['source_id'] or not store.parts or store.is_absolute() or any(p in {'..','.'} for p in store.parts):
        raise ValueError('background_runtime_wake_ref_invalid')


# LLM: 每次重读原行，来源变更只退旧运输，不领取或改写新行；整个SQLite/WAL变化不影响别的字条身份。
# 函数用途: 返回与冻结摘要一致的准确原字条。
def read_runtime_wake_source(home_root, ref):
    from .multiprocess_background_request import (
        BackgroundSourceChanged,
        source_digest,
        source_owner,
    )

    home = _owner_home_dir(Path(home_root), source_owner(ref['owner']))
    row = runtime_wake_reader(home).read_runtime_wake(ref['source_id'])
    if row is None or source_digest(row) != ref['source_sha256']:
        raise BackgroundSourceChanged()
    return row


# LLM: 仅原C已读owner/Store/完整DB行可冻结，线程从准确TaskLink取得；不信模型字段或另算owner路径。
# 函数用途: 为一条真实到期字条构造现有通用运输引用。
def freeze_runtime_wake(home, owner, store, row):
    from .multiprocess_background_request import source_digest

    link, error = store.tasks.load_report(row['root_task_id'])
    if error:
        raise ValueError('background_runtime_wake_task_unavailable')
    if link is not None and row['root_thread_id'] and row['root_thread_id'] != link.thread_id:
        raise ValueError('background_runtime_wake_thread_mismatch')
    digest = source_digest(row)
    return {'schema_version':'gateway_background_source.v1','kind':'runtime_wake','owner':asdict(owner),
        'source_ref':'runtime.db','source_exists':True,'source_sha256':digest,'input_revision':digest,
        'store_ref':Path(store.storage.root).relative_to(home).as_posix(),
        'source_id':row['wake_id'],'thread_id':link.thread_id if link is not None else row['root_thread_id']}
