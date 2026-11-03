# LLM: 读取workspace优先再Owner和全局；写入只看显式target_scope，版本CAS在原根锁/集合锁内，不执行代码。
# 模块用途: 分层管理固化脚本、项目覆盖、动态查询范围、候选评估和运行摘要。
from __future__ import annotations

import json
import uuid
from dataclasses import fields, replace

from ..common.nofollow_fs import _supports_dir_fd, read_bytes_beneath
from . import (
    MAX_ASSET_BYTES,
    TRANSACTION_FILE,
    AssetPostCommitReadbackError,
    _AssetStore,
    next_asset_revision,
    positive_timeout,
    safe_asset_id,
)
from .descriptor import (
    AssetDescriptor,
    EvaluatedCandidate,
    RuntimeSummary,
    clean_json,
    execution_definition_ref,
)


# LLM: 参数只是脚本执行声明，不授予解释器或网络权限；安全单叶也不能以大小写别名覆写权威元数据。
# 函数用途: 统一校验保存和恢复时的代码入口、解释器和超时。
def _script_options(entrypoint, interpreter, timeout_seconds):
    safe_asset_id(entrypoint)
    if entrypoint.casefold() in {"script.json", TRANSACTION_FILE} or not isinstance(interpreter, str) or not interpreter.strip():
        raise ValueError("无效脚本入口或解释器")
    return {"entrypoint": entrypoint, "interpreter": interpreter, "timeout_seconds": positive_timeout(timeout_seconds)}


# LLM: 平铺JSON中的描述符与执行声明分别验证，未知普通字段不吞掉；不消费代码路径字段。
# 函数用途: 恢复存储的脚本契约，拒绝错误kind和损坏参数。
def _decode_script(payload):
    values = dict(payload)
    options = _script_options(*(values.pop(name) for name in ("entrypoint", "interpreter", "timeout_seconds")))
    descriptor = AssetDescriptor.from_dict(values)
    if descriptor.kind != "script":
        raise ValueError("脚本存储只接受script描述符")
    return descriptor, options


# LLM: 历史按候选五个值字段去重保序；同ID的不同时间、决定、原因或样本数均为不同观察。
# 函数用途: 校验并追加候选评估，不覆盖已保存的评估历史。
def _candidate_history(previous, incoming):
    if not isinstance(incoming, (list, tuple)):
        raise ValueError('evaluated_candidates必须是序列')
    observations = tuple(EvaluatedCandidate.from_dict(item.to_dict() if isinstance(item, EvaluatedCandidate) else item)
                         for item in incoming)
    return tuple(dict.fromkeys((*previous, *observations)))


# LLM: 只恢复标准字段，代码/执行声明和实时源投影不能混入严格描述符；不执行额外IO。
# 函数用途: 给锁内历史合并与源更新共用严格描述符解码。
def _record_descriptor(record):
    names = {item.name for item in fields(AssetDescriptor)}
    return AssetDescriptor.from_dict({key: value for key, value in record.items() if key in names})


# LLM: 仅继承仍存在的source_id历史；幂等凭据属于存储运行事实，普通定义保存不能抹除。
# 函数用途: 普通脚本保存时保护候选历史和未提供的运行摘要。
def _preserve_history(descriptor, old):
    previous = {source.source_id: source for source in old.data_sources}
    sources = tuple(replace(source, evaluated_candidates=_candidate_history(
        previous[source.source_id].evaluated_candidates, source.evaluated_candidates))
        if source.source_id in previous else source for source in descriptor.data_sources)
    summary = old.runtime_summary if descriptor.runtime_summary == RuntimeSummary() else descriptor.runtime_summary
    metadata = dict(descriptor.metadata)
    if '_run_summary_spool' in old.metadata:
        metadata['_run_summary_spool'] = old.metadata['_run_summary_spool']
    if '_run_summary_generation' in old.metadata:
        metadata['_run_summary_generation'] = old.metadata['_run_summary_generation']
    else:
        metadata.pop('_run_summary_generation', None)
    return replace(descriptor, data_sources=sources, runtime_summary=summary, metadata=metadata)


# LLM: 每个写操作共用根锁及原集合锁；workspace仅用于读取覆盖，写入必须显式指定，路径不从投影恢复。
# 类用途: 在Owner空间管理固化脚本，供工具、工作流和Broker共用。
class ScriptStore(_AssetStore):
    # LLM: 项目根只来自显式宿主参数或同Owner运行栈，冷读不创建.agent，不从资产正文取权限。
    # 函数用途: 绑定脚本Owner写根、项目读取覆盖及全局只读来源。
    def __init__(self, owner_home, *, extra_roots=(), is_admin: bool = False, workspace_root=None):
        from ..workflows.datasource_runtime import script_workspace_root
        if workspace_root is None:
            workspace_root = script_workspace_root(owner_home)
        super().__init__(owner_home, "scripts", "script.json", extra_roots=extra_roots, is_admin=is_admin,
                         workspace_root=workspace_root)

    # LLM: 返回副本以原Owner和授权workspace计算，不写回实时源状态或改变描述符。
    # 函数用途: 给脚本统一投影四个展示字段和源差异。
    def _project_record(self, record):
        from ..workflows.datasource_runtime import project_script_asset_fields
        return {**record, **project_script_asset_fields(self.owner_home, record, workspace_root=self.workspace_root)} if record is not None else None

    # LLM: 只投影本次已提交的定义快照，不能用并发新聚合覆盖显式摘要或fold快照；错误阶段保持。
    # 函数用途: 给保存/更新读回隐藏内部摘要元数据，不改变本次写入返回的摘要时点。
    def _readback_after_commit(self, asset_id):
        record = super()._readback_after_commit(asset_id)
        try:
            record = self._project_record(record)
            metadata = dict(record['metadata'])
            metadata.pop('_run_summary_spool', None)
            metadata.pop('_run_summary_generation', None)
            return {**record, 'metadata': metadata}
        except Exception as exc:
            raise AssetPostCommitReadbackError('脚本已提交但展示投影失败') from exc

    # LLM: 标记只存在于返回副本，不写共享元数据，不从元数据取得路径或写入权。
    # 函数用途: 给全局脚本的详情或列表补充真实来源标记。
    def _mark_global(self, record):
        return {**record, 'scope_layer': 'global', 'source_scope': 'builtin', 'workspace_root': '',
                'metadata': {**record['metadata'], 'asset_scope': 'global'}}

    # LLM: 全局和Owner列表均只读元数据；继承基座事务校验，不加载代码或返回执行路径。
    # 函数用途: 严格解码一条脚本列表投影。
    def _list_entry(self, script_id):
        payload = self._read(script_id)
        if payload is None:
            return None
        descriptor, options = _decode_script(payload)
        return descriptor.to_dict() | options

    # LLM: 不重入锁；先校验元数据再nofollow读代码，路径只是展示，后续执行仍须冻结代码或重新核验。
    # 函数用途: 锁内取得包含安全代码副本的完整脚本记录。
    def _get(self, script_id):
        payload = self._read(script_id)
        if payload is None:
            return None
        descriptor, options = _decode_script(payload)
        code = self._text(script_id, options["entrypoint"])
        if code is None:
            raise ValueError("脚本代码文件缺失")
        record = descriptor.to_dict() | options | {"code": code, "entrypoint_path": str(
            self.owner_home / "scripts" / script_id / options["entrypoint"],
        )}
        return {**record, 'execution_definition_ref': execution_definition_ref(record)}

    # LLM: 缺记录是正常查询结果，但更新必须显式失败；不隐式新建空脚本。
    # 函数用途: 锁内为写操作取得已存在脚本。
    def _required(self, script_id):
        value = self._get(script_id)
        if value is None:
            raise KeyError(script_id)
        return value

    # LLM: scope只由显式参数决定，目标CAS在锁内检查；保留旧历史、入口别名及提交后读回阶段，不执行代码。
    # 函数用途: 保存Owner或项目脚本，返回包含递增revision的完整记录。
    def save_script(self, descriptor, code, *, entrypoint="main.py", interpreter="python3", timeout_seconds=120,
                    target_scope='owner', expected_revision=None):
        descriptor = AssetDescriptor.from_dict(descriptor.to_dict())
        options = _script_options(entrypoint, interpreter, timeout_seconds)
        if descriptor.kind != "script" or not isinstance(code, str):
            raise ValueError("必须提供script描述符与文本代码")
        target = self._target_store(target_scope)
        with target._locked():
            old = target._get(descriptor.id)
            next_asset_revision(old, expected_revision)
            if old:
                descriptor = _preserve_history(descriptor, _record_descriptor(old))
            else:
                # 生命周期只在持根锁的新建时生成；同ID删除后重建不能复用旧摘要或调用方身份。
                descriptor = replace(descriptor, metadata={**descriptor.metadata,
                    '_run_summary_generation': uuid.uuid4().hex})
            if old and old["entrypoint"].casefold() == entrypoint.casefold():
                entrypoint = options["entrypoint"] = old["entrypoint"]
            files = {entrypoint: code}
            if old and old["entrypoint"] != entrypoint:
                files[old["entrypoint"]] = None
            target._write(descriptor.to_dict() | options, files)
            return target._readback_after_commit(descriptor.id)

    # LLM: 同作用域可信执行栈只读已验快照；其它冷读沿readonly视图，坏记录不回退，不跨Owner枚举。
    # 函数用途: 优先读取项目、再Owner、最后全局脚本的安全代码副本。
    def get_script(self, script_id):
        safe_asset_id(script_id)
        from ..workflows.datasource_runtime import script_execution_record
        snapshot = script_execution_record(self.owner_home, script_id, self.workspace_root)
        if snapshot is not None:
            return snapshot
        if self.workspace_root is not None:
            from .workspace_assets import WorkspaceAssetOverlay

            record = WorkspaceAssetOverlay(self.workspace_root, owner_home=self.owner_home,
                                         builtin_roots=self._global_roots).get_asset('script', script_id)
            return self._summary_projection(self._project_record(record))
        with self._locked():
            record = self._get(script_id)
        return self._summary_projection(self._project_record(record if record is not None else self._global_get(script_id)))

    # LLM: 有workspace走同一只读覆盖入口，不创建.agent；只投影元数据，实时状态不持久化。
    # 函数用途: 按ID稳定列出项目、Owner和全局脚本的去重元数据。
    def list_scripts(self):
        if self.workspace_root is not None:
            from .workspace_assets import WorkspaceAssetOverlay

            records = WorkspaceAssetOverlay(self.workspace_root, owner_home=self.owner_home,
                                         builtin_roots=self._global_roots).list_assets('script')
            return [self._project_record(row) for row in records]
        with self._locked():
            records = self._local_records()
        return [self._project_record(row) for row in self._merge_globals(records)]

    # LLM: 删除前验证代码及元数据，基座拒绝所有链接和未支持目录；不执行文件。
    # 函数用途: 删除单个脚本目录，缺失返回False。
    def delete_script(self, script_id):
        safe_asset_id(script_id)
        with self._locked():
            if self._get(script_id) is None:
                return False
            return self._delete(script_id)

    # LLM: 沿本Owner原锁和元数据事务更新运行闸，不读取其他Owner或执行代码，保留代码、声明及历史。
    # 函数用途: 开启或停用已有固化脚本并返回持久化描述符。
    def set_enabled(self, script_id: str, enabled: bool) -> AssetDescriptor:
        if type(enabled) is not bool:
            raise ValueError('enabled必须是布尔值')
        with self._locked():
            record = self._required(script_id)
            descriptor = replace(_record_descriptor(record), enabled=enabled)
            self._write(descriptor.to_dict() | {key: record[key]
                for key in ('entrypoint', 'interpreter', 'timeout_seconds')})
            return _record_descriptor(self._readback_after_commit(script_id))

    # LLM: 查询范围按键合并且None删键；候选去重不授查询权，提交后读回错误不能降成参数错。
    # 函数用途: 锁内修改活跃数据源范围及已评估新源备忘。
    def update_data_sources(self, script_id, source_id, *, query_scope=None, candidate=None, evaluated_candidates=()):
        safe_asset_id(source_id)
        with self._locked():
            record = self._required(script_id)
            descriptor = _record_descriptor(record)
            sources = list(descriptor.data_sources)
            index = next((i for i, source in enumerate(sources) if source.source_id == source_id), None)
            if index is None:
                raise KeyError(source_id)
            source = sources[index]
            if query_scope is not None:
                if not isinstance(query_scope, dict):
                    raise ValueError("query_scope补丁必须是对象")
                scope = source.query_scope | clean_json(query_scope)
                source = replace(source, query_scope={key: value for key, value in scope.items() if value is not None})
            history = _candidate_history(source.evaluated_candidates, evaluated_candidates)
            if candidate is not None:
                history = _candidate_history(history, (candidate,))
            source = replace(source, evaluated_candidates=history)
            sources[index] = source
            options = {key: record[key] for key in ("entrypoint", "interpreter", "timeout_seconds")}
            self._write(replace(descriptor, data_sources=tuple(sources)).to_dict() | options)
            return self._readback_after_commit(script_id)

    # LLM: 展示投影直读聚合；存储私有迁移元数据不进入公开定义，保留旧调度固定引用。
    # 函数用途: 为公开脚本读口返回最新事务摘要。
    def _summary_projection(self, record):
        if record is None or record.get('scope_layer', 'owner') != 'owner':
            return record
        from ..workflows.run_summary_spool import project
        metadata = dict(record.get('metadata', {}))
        summary = project(self.owner_home, 'script', record['id'], RuntimeSummary.from_dict(record['runtime_summary']), metadata)
        metadata.pop('_run_summary_spool', None)
        metadata.pop('_run_summary_generation', None)
        return {**record, 'runtime_summary': summary.to_dict(), 'metadata': metadata}

    # LLM: 仅用于摘要来源选择，不供执行；并发事务中原子元数据版本可认领来源，但不恢复事务或写其目录。
    # 函数用途: 无锁读取完整安全元数据快照，保留原返回字段并有限重读原子替换。
    def _summary_snapshot(self, script_id):
        try:
            raw = read_bytes_beneath(self.owner_home, (self.collection, script_id, self.filename),
                                     max_bytes=MAX_ASSET_BYTES, snapshot_retries=2 if _supports_dir_fd() else 0)
        except FileNotFoundError:
            return None
        if raw is None:
            return None
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get('id') != script_id:
            raise ValueError('资产元数据ID与目录不匹配')
        descriptor, options = _decode_script(payload)
        code = self._text(script_id, options['entrypoint'])
        if code is None:
            raise ValueError('脚本代码文件缺失')
        record = descriptor.to_dict() | options | {'code': code, 'entrypoint_path': str(
            self.owner_home / self.collection / script_id / options['entrypoint'])}
        return {**record, 'execution_definition_ref': execution_definition_ref(record)}

    # LLM: 不走get_script的Owner根锁；只认实际只读来源，项目覆盖不把摘要写到同名Owner。
    # 函数用途: 为摘要选择实际来源，返回记录及是否允许写本Owner。
    def _summary_source(self, script_id):
        safe_asset_id(script_id)
        if self.workspace_root is not None:
            from . import workspace_asset_home

            view = self._global_view(workspace_asset_home(self.workspace_root))
            record = view._summary_snapshot(script_id)
            if record is not None:
                return {**record, 'scope_layer': 'workspace', 'source_scope': 'workspace'}, False
        record = self._summary_snapshot(script_id)
        if record is not None:
            return record, not self._read_only
        for root in self._global_roots:
            view = self._global_view(root)
            record = view._summary_snapshot(script_id)
            if record is not None:
                return view._mark_global(record), False
        raise KeyError(script_id)

    # LLM: 非阻塞调用直接提交SQLite聚合，不取Owner根锁；返回值仍保持旧定义快照契约。
    # 函数用途: 记录脚本摘要，可显式同步保存定义快照，run_id用于跨调用与重启幂等。
    def record_run_summary(self, script_id, status, duration_ms, run_at=None, *, last_error="", blocking=True, run_id=""):
        from ..workflows import run_summary_spool

        if type(blocking) is not bool:
            raise ValueError('blocking必须是布尔值')
        record, writable = self._summary_source(script_id)
        if not writable:
            return record
        run_summary_spool.record(self.owner_home, 'script', script_id,
                                {'status': status, 'duration_ms': duration_ms, 'run_at': run_at,
                                 'last_error': last_error, 'run_id': run_id},
                                 RuntimeSummary.from_dict(record['runtime_summary']), record['metadata'])
        return self.fold_run_summaries(script_id) if blocking else record

    # LLM: 原根锁下重新读定义，SQLite负责去重；写回固定大小摘要，ACK失败可重放。
    # 函数用途: 显式迁移本脚本旧spool并保存摘要快照，项目及全局来源仍保持只读。
    def fold_run_summaries(self, script_id):
        from ..workflows import run_summary_spool

        selected, writable = self._summary_source(script_id)
        if not writable:
            return selected
        with self._locked():
            record = self._required(script_id)
            descriptor = _record_descriptor(record)
            options = {key: record[key] for key in ('entrypoint', 'interpreter', 'timeout_seconds')}

            # LLM: 回调只提交同一锁内取得的定义；不能先ACK或独立保存幂等清单。
            # 函数用途: 以原资产事务原子提交合并的摘要及幂等元数据。
            def commit(summary, metadata):
                self._write(replace(descriptor, runtime_summary=summary, metadata=metadata).to_dict() | options)

            run_summary_spool.fold(self.owner_home, 'script', script_id, descriptor.runtime_summary,
                                   descriptor.metadata, commit)
            return self._readback_after_commit(script_id)
