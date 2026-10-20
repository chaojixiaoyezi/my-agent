# LLM: 只有本次真实C/controller可持本variant；磁盘stop/preimage只是来源，不恢复授权，首publish前仍fresh死亡/全源/CAS。
# 模块用途: 在原接班与publisher中准确消费支持版Count0正常停止来源，不造旧MP角色或closure。
from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..common.nofollow_fs import read_bytes_beneath, write_bytes_atomic_beneath
from ..common.strict_json import load_strict_json
from ..settings import load_config
from ..user_space.home_layout import home_paths
from ..user_space.home_root import configured_home_root
from .daemon_metadata import process_identity_is_live
from .io import update_json_file_atomic
from .multiprocess_coordinator import current_coordinator
from .standalone_inventory import standalone_inventory
from .standalone_migration_errors import StandaloneMigrationRejected
from .standalone_runtime_inventory import responsibility_digest
from .standalone_sources import _source_snapshot as original_standalone_sources
from .standalone_stop import NORMAL_STOP_KINDS, STANDALONE_STOP_SCHEMA


# LLM: immutable原引用只来自本次capture，不接受JSON反序列化；actual controller/lease仍逐次核。
# 类用途: 冻结当次接班来源、配置和完整旧raw，避免晚改字段重捕为许可。
@dataclass(frozen=True)
class _MigrationOrigin:
    source: object
    controller: object
    lease: object
    root: Path
    home: Path
    count: int
    generation: str
    state_raw: bytes
    inventory_json: str
    config_path: Path
    config_raw: bytes
    config: object


# LLM: 错误只给固定原因与行动，不回显原状态/prompt/凭据。
# 函数用途: 要求用户沿原Count0或部署入口恢复，不建议删除marker。
def _reject(reason, action="保持数量0，用支持版正常运行并正常停止一次；处理所指原责任后再改为目标数量。", ref=""):
    raise StandaloneMigrationRejected(reason, action, ref)


# LLM: 编码只用于全源等值与原像留存，不把digest当执行许可。
# 函数用途: 冻结规范结构以防嵌套可变快照被改写。
def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


# LLM: 只收精确版本和原writer正常停止结果；缺birth、未知字段/cleanup/blocker都不能补猜。
# 函数用途: 校验支持版单进程来源，并绑定同一home/root/config与真实身份。
def _validate_stop(state, root, home, config_path):
    fact = state.get("standalone_stop")
    keys = {"schema_version", "root", "home", "config_path", "process_identity", "started_at", "status",
            "termination_kind", "cleanup", "sources", "blockers"}
    if (type(fact) is not dict or set(fact) != keys or fact["schema_version"] != STANDALONE_STOP_SCHEMA
            or fact["root"] != str(root) or fact["home"] != str(home) or fact["config_path"] != str(config_path)
            or fact["status"] != "stopped" or state.get("status") != "stopped" or state.get("exit_code") != 0
            or fact["termination_kind"] not in NORMAL_STOP_KINDS or state.get("termination_kind") != fact["termination_kind"]
            or type(state.get("worker_count")) is not int or state["worker_count"] != 0
            or fact["process_identity"] != state.get("process_identity") or fact["started_at"] != state.get("started_at")):
        _reject("standalone_normal_stop_required")
    identity = fact["process_identity"]
    if (type(identity) is not dict or not identity.get("host_id") or type(identity.get("pid")) is not int
            or identity["pid"] <= 0 or type(identity.get("start_time")) not in (str, int)
            or identity["start_time"] == "" or state.get("pid") != identity["pid"]):
        _reject("standalone_birth_identity_required")
    cleanup, sources = fact["cleanup"], fact["sources"]
    if (type(cleanup) is not dict or cleanup.get("drain_complete") is not True or cleanup.get("alive_threads") != []
            or fact["blockers"] != [] or type(sources) is not dict or sources.get("open_model_calls") != 0
            or type(sources.get("digest")) is not str or re.fullmatch(r"[0-9a-f]{64}", sources["digest"]) is None):
        _reject("standalone_stop_responsibility_unconfirmed")
    return fact


# LLM: actual lease/controller绑定只在真实接班组合根创建；JSON或复制对象不能通过controller原origin引用门。
# 类用途: 在首个C发表前持有并复核准确standalone来源，提交后只保原像绑定。
class StandaloneMigration:
    # LLM: origin由唯一capture构造并同时绑定原controller；此类自身不产生C/W或P3权。
    # 函数用途: 保存当前迁移原像与提交状态。
    def __init__(self, origin):
        self._origin = origin
        self._committed = False
        self._archive_raw = None
        self.reference = ""

    # LLM: 每次核同一真实lease/controller/generation/count/root，原origin引用替换或退出即失效。
    # 函数用途: 阻止旧对象、复制来源或换代后继续消费迁移许可。
    def require_current(self):
        origin = self._origin
        controller = origin.controller
        lease = current_coordinator(origin.root, origin.count)
        if (origin.source is not self or lease is not origin.lease or not controller.active() or lease.controller is not controller
                or controller._standalone_migration is not self or controller._standalone_origin is not origin
                or controller.paths.root != origin.root or controller.count != origin.count
                or controller.c_run_id != origin.generation):
            _reject("standalone_original_controller_required")

    # LLM: 原角色reader必须同root/count/新代，不把别处活variant或duck-typed对象当此处来源。
    # 函数用途: 绑定原角色覆盖调用的准确范围。
    def require_role_scope(self, paths, generation, count):
        self.require_current()
        origin = self._origin
        if paths.root != origin.root or generation != origin.generation or count != origin.count:
            _reject("standalone_role_scope_conflict")

    # LLM: signature在首次正常发表前后不变；实际提交状态只决定读哪些原源，不借缓存解除未知。
    # 函数用途: 把本次variant纳入原bootstrap/末检快照。
    def signature(self):
        self.require_current()
        origin = self._origin
        return ("standalone.v1", hashlib.sha256(origin.state_raw).hexdigest(), origin.inventory_json,
                hashlib.sha256(origin.config_raw).hexdigest(), origin.count, origin.generation)

    # LLM: 待提交只剔除准确旧state原件，所有其它role仍走原MP门；已提交后不豁免任何role。
    # 函数用途: 让原角色覆盖识别这一明确来源类型，避免把Count0当无ID旧MP代。
    def filter_roles(self, roles):
        self.require_current()
        if self._committed:
            return roles
        if roles != {"gateway_state.json": self._origin.state_raw}:
            _reject("standalone_role_source_changed")
        return {}

    # LLM: 首次发表前完整重读原源与fresh身份，已发表后只核本次不可变原像；不要求业务之后永远冻结。
    # 函数用途: 在原接班末门拒绝来源漂移或旧宿主仍活/未知。
    def require_unchanged(self):
        self.require_current()
        origin = self._origin
        if self._committed:
            if read_bytes_beneath(origin.root, tuple(self.reference.split("/")), max_bytes=8 * 1024 * 1024,
                                  require_dir_fd=True) != self._archive_raw:
                _reject("standalone_migration_preimage_changed")
            return
        raw = read_bytes_beneath(origin.root, ("gateway_state.json",), max_bytes=4 * 1024 * 1024, require_dir_fd=True)
        config_raw = read_bytes_beneath(origin.config_path.parent, (origin.config_path.name,), max_bytes=4 * 1024 * 1024,
                                        require_dir_fd=True)
        if raw != origin.state_raw or config_raw != origin.config_raw:
            _reject("standalone_source_or_configuration_changed", "先停止会修改该来源的原入口，确认目标YAML后重试；不要改写旧停止记录。")
        original_standalone_sources(origin.root)
        identity = load_strict_json(origin.state_raw)["process_identity"]
        if process_identity_is_live(identity) is not False:
            _reject("standalone_previous_instance_alive_or_unknown", "通过原部署入口确认并正常停止准确旧实例；身份未知不能迁移。")
        current = standalone_inventory(origin.home, origin.config, origin.controller.paths)
        if _canonical(current) != origin.inventory_json:
            _reject("standalone_responsibility_source_changed", "先收口或暂停会修改准确责任来源的原入口，再重试迁移。")

    # LLM: 仅原publisher在controller→lease短门调用；先留不可变prepared原像，再原state锁内精确raw CAS，新C仍由原publisher实际发表。
    # 函数用途: 第一次覆盖旧Count0状态，保版本化原像；中断留下的文件自身不代表迁移完成。
    def publish_first(self, context, payload):
        self.require_unchanged()
        origin = self._origin
        if self._committed or context.control_plane.controller() is not origin.controller:
            _reject("standalone_first_publish_context_required")
        state_sha = hashlib.sha256(origin.state_raw).hexdigest()
        name = hashlib.sha256(origin.generation.encode()).hexdigest() + "-" + state_sha + ".json"
        parts = ("standalone_migrations", name)
        archive = {"schema_version": "gateway_standalone_migration_preimage.v1", "state": "prepared",
                   "source_root": str(origin.root), "source_home": str(origin.home), "source_sha256": state_sha,
                   "source_raw_base64": base64.b64encode(origin.state_raw).decode(),
                   "target_worker_count": origin.count, "new_c_run_id": origin.generation}
        archive_raw = (_canonical(archive) + "\n").encode()
        existing = read_bytes_beneath(origin.root, parts, max_bytes=8 * 1024 * 1024, require_dir_fd=True)
        if existing is not None and existing != archive_raw:
            _reject("standalone_migration_preimage_conflict")
        if existing is None:
            write_bytes_atomic_beneath(origin.root, parts, archive_raw)
        self.require_unchanged()

        # LLM: 实际原state锁内核最后raw和原lease；归档存在不放宽任何一步，也不改旧role/P4或closure。
        # 函数用途: 原子发表真实新C投影；被替换的旧state一字不覆盖。
        def update(current):
            self.require_unchanged()
            if _canonical(current) != _canonical(load_strict_json(origin.state_raw)):
                _reject("standalone_state_compare_conflict")
            return {**payload, "standalone_migration_ref": "/".join(parts)}

        result = update_json_file_atomic(context.paths.state, update, require_existing=True)
        self._archive_raw, self.reference, self._committed = archive_raw, "/".join(parts), True
        return result


# LLM: fresh/旧MP沿原门不变；真实当前C须核当前完整责任摘要仍等正常stop来源，再绑定实际YAML，不信任调用JSON或磁盘preimage。
# 函数用途: 在业务/恢复之前验证支持版Count0来源，冻结本次目标N与严格当前责任快照。
def _capture_standalone_migration(controller, config, config_path):
    raw = read_bytes_beneath(controller.paths.root, ("gateway_state.json",), max_bytes=4 * 1024 * 1024, require_dir_fd=True)
    if raw is None:
        return None
    state = load_strict_json(raw)
    if type(state) is dict and "c_run_id" in state:
        return None
    if type(state) is not dict:
        _reject("standalone_state_invalid")
    path = Path(config_path).resolve()
    home = home_paths(configured_home_root(config)).root
    stop = _validate_stop(state, controller.paths.root, home, path)
    if (config.gateway_worker_process_count != controller.count
            or load_config(path).gateway_worker_process_count != controller.count):
        _reject("standalone_target_configuration_conflict")
    original_standalone_sources(controller.paths.root)
    config_raw = read_bytes_beneath(path.parent, (path.name,), max_bytes=4 * 1024 * 1024, require_dir_fd=True)
    if config_raw is None:
        _reject("standalone_configuration_missing")
    if process_identity_is_live(state["process_identity"]) is not False:
        _reject("standalone_previous_instance_alive_or_unknown", "用原部署入口正常停止准确旧实例；unknown或foreign host不得迁移。")
    first = standalone_inventory(home, config, controller.paths)
    second = standalone_inventory(home, config, controller.paths)
    if first != second:
        _reject("standalone_responsibility_source_changed")
    if responsibility_digest(second) != stop["sources"]["digest"]:
        _reject("standalone_stop_responsibility_changed",
                "停止后原责任已变化；保持数量0沿原入口核对并正常运行/停止，不能重写旧停止摘要。")
    source = StandaloneMigration(None)
    origin = _MigrationOrigin(source, controller, controller._lease, controller.paths.root, home, controller.count,
                              controller.c_run_id, raw, _canonical(second), path, config_raw, copy.deepcopy(config))
    source._origin = origin
    controller._standalone_migration, controller._standalone_origin = source, origin
    source.require_unchanged()
    return source


# LLM: 原读取错误不降为空库存，只转换固定行动错误；不输出exception里的用户值，不恢复或重试来源。
# 函数用途: 给正式启动链提供可行动拒绝，原MP和fresh入口仍沿原算法。
def capture_standalone_migration(controller, config, config_path):
    try:
        return _capture_standalone_migration(controller, config, config_path)
    except StandaloneMigrationRejected:
        raise
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
        raise StandaloneMigrationRejected("standalone_sources_unverifiable",
            "保持数量0，通过原入口恢复准确来源并正常停止；危险或未知资料不支持自动迁移。") from error


# LLM: 组合根取得准确原source类型与原origin；替换、复制或清空单个引用不能跳过末核，None只代表真正未捕获。
# 函数用途: 给原controller与publisher取得同一个迁移来源，不从磁盘恢复授权。
def current_standalone_migration(controller):
    source, origin = controller._standalone_migration, controller._standalone_origin
    if source is None and origin is None:
        return None
    if type(source) is not StandaloneMigration:
        _reject("standalone_original_source_required")
    source.require_current()
    return source
