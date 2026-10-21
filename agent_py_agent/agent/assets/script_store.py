# LLM: 固化脚本权威仅在构造时Owner/scripts/id；元数据平铺标准Key，代码通过nofollow读取，不执行脚本。
# 模块用途: 保存与管理固化脚本、动态查询范围、候选评估和运行摘要。
from __future__ import annotations

from dataclasses import replace

from . import TRANSACTION_FILE, _AssetStore, next_run_summary, positive_timeout, safe_asset_id
from .descriptor import AssetDescriptor, EvaluatedCandidate, RuntimeSummary, clean_json


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


# LLM: 只恢复真实记录的描述符部分，代码/执行声明不混入公共契约；不执行额外IO。
# 函数用途: 给锁内历史合并与源更新共用严格描述符解码。
def _record_descriptor(record):
    return AssetDescriptor.from_dict({key: value for key, value in record.items()
                                     if key not in {'entrypoint', 'interpreter', 'timeout_seconds', 'code', 'entrypoint_path'}})


# LLM: 仅继承新定义仍存在的source_id历史；非空新摘要保持显式值，不恢复已删除数据源。
# 函数用途: 普通脚本保存时保护候选历史和未提供的运行摘要。
def _preserve_history(descriptor, old):
    previous = {source.source_id: source for source in old.data_sources}
    sources = tuple(replace(source, evaluated_candidates=_candidate_history(
        previous[source.source_id].evaluated_candidates, source.evaluated_candidates))
        if source.source_id in previous else source for source in descriptor.data_sources)
    summary = old.runtime_summary if descriptor.runtime_summary == RuntimeSummary() else descriptor.runtime_summary
    return replace(descriptor, data_sources=sources, runtime_summary=summary)


# LLM: 每个public操作共用原集合锁；调用方只传安全ID，根路径不从输入记录恢复。
# 类用途: 在Owner空间管理固化脚本，供工具、工作流和Broker共用。
class ScriptStore(_AssetStore):
    # LLM: 只构造scripts集合，不读取或执行任何脚本。
    # 函数用途: 固定Owner脚本权威目录。
    def __init__(self, owner_home):
        super().__init__(owner_home, "scripts", "script.json")

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
        return descriptor.to_dict() | options | {"code": code, "entrypoint_path": str(
            self.owner_home / "scripts" / script_id / options["entrypoint"],
        )}

    # LLM: 缺记录是正常查询结果，但更新必须显式失败；不隐式新建空脚本。
    # 函数用途: 锁内为写操作取得已存在脚本。
    def _required(self, script_id):
        value = self._get(script_id)
        if value is None:
            raise KeyError(script_id)
        return value

    # LLM: 同锁继承历史后走可恢复事务；入口大小写别名和旧叶清理保持原合同。
    # 函数用途: 保存新脚本或替换已有脚本，返回安全读回的记录。
    def save_script(self, descriptor, code, *, entrypoint="main.py", interpreter="python3", timeout_seconds=120):
        descriptor = AssetDescriptor.from_dict(descriptor.to_dict())
        options = _script_options(entrypoint, interpreter, timeout_seconds)
        if descriptor.kind != "script" or not isinstance(code, str):
            raise ValueError("必须提供script描述符与文本代码")
        with self._locked():
            old = self._get(descriptor.id)
            if old:
                descriptor = _preserve_history(descriptor, _record_descriptor(old))
            if old and old["entrypoint"].casefold() == entrypoint.casefold():
                entrypoint = options["entrypoint"] = old["entrypoint"]
            files = {entrypoint: code}
            if old and old["entrypoint"] != entrypoint:
                files[old["entrypoint"]] = None
            self._write(descriptor.to_dict() | options, files)
            return self._get(descriptor.id)

    # LLM: 返回的是副本，不缓存其它Owner；缺失为None，损坏或链接拒绝上抛。
    # 函数用途: 读取指定脚本及其代码。
    def get_script(self, script_id):
        safe_asset_id(script_id)
        with self._locked():
            return self._get(script_id)

    # LLM: 列表不读取代码；每条元数据仍须严格校验，不以坏记录冒充空列表。
    # 函数用途: 按ID稳定列出本Owner所有脚本标准Key和执行声明。
    def list_scripts(self):
        with self._locked():
            records = [self._read(asset_id) for asset_id in self._ids()]
            return [descriptor.to_dict() | options for descriptor, options in
                    (_decode_script(row) for row in records if row is not None)]

    # LLM: 删除前验证代码及元数据，基座拒绝所有链接和未支持目录；不执行文件。
    # 函数用途: 删除单个脚本目录，缺失返回False。
    def delete_script(self, script_id):
        safe_asset_id(script_id)
        with self._locked():
            if self._get(script_id) is None:
                return False
            return self._delete(script_id)

    # LLM: 查询范围按键合并，None删除键；单条及批量候选完整去重追加，评估不是查询授权。
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
            return self._get(script_id)

    # LLM: 同一集合锁覆盖读改写，多个Store实例不会覆盖计数；只写摘要，不创建运行或调用模型。
    # 函数用途: 保存脚本最近运行和连续失败摘要。
    def record_run_summary(self, script_id, status, duration_ms, run_at):
        with self._locked():
            record = self._required(script_id)
            descriptor, options = _decode_script({key: value for key, value in record.items() if key not in {"code", "entrypoint_path"}})
            summary = next_run_summary(descriptor.runtime_summary, status, duration_ms, run_at)
            self._write(replace(descriptor, runtime_summary=summary).to_dict() | options)
            return self._get(script_id)
