# LLM: 只由正常Count0最终writer持原锁调用；记录真实停止来源及原责任摘要，不自证进程死亡或物理quiet。
# 模块用途: 在唯一state内保存支持版Count0的版本化正常停止事实，缺旧身份不追补。
from __future__ import annotations

import json
import os
import sqlite3

from ..common.nofollow_fs import read_bytes_beneath
from ..common.strict_json import load_strict_json
from ..contracts.model_call_ledger import model_call_drain_snapshot
from ..user_space.home_layout import home_paths
from ..user_space.home_root import configured_home_root
from .io import update_json_file_atomic
from .standalone_inventory import prepare_standalone_read_facilities, standalone_inventory
from .standalone_migration_errors import StandaloneMigrationRejected
from .standalone_runtime_inventory import responsibility_digest

STANDALONE_STOP_SCHEMA = "gateway_standalone_stop.v1"
NORMAL_STOP_KINDS = frozenset({"planned_stop", "signal_shutdown", "planned_update_stop"})


# LLM: 旧宿主尚存，native launcher不在此处假判dead；同进程调用只读原ledger，完整物理死亡必须后继fresh核。
# 函数用途: 捕获正常停止时实际责任摘要和明确阻断，不把读取错误当空库存。
def _stop_sources(context, home):
    try:
        prepare_standalone_read_facilities(home)
        before = standalone_inventory(home, context.config, context.paths, check_launcher=False)
        open_calls = sum(record.status in {"started", "first_token"}
                         for _ledger, record, _retained in model_call_drain_snapshot())
        after = standalone_inventory(home, context.config, context.paths, check_launcher=False)
        if before != after:
            raise StandaloneMigrationRejected("stop_sources_changed", "保持0完成原责任收尾后正常停止。")
        blockers = [] if not open_calls else [{"reason": "model_calls_unsettled", "ref": "model_call_ledger"}]
        return {"digest": responsibility_digest(after), "refs": ["home/owners",
                "root/requests/processing", "root/control_operations"],
                "runtime_owner_count": len(after["databases"]), "source_file_count": len(after["files"]),
                "open_model_calls": open_calls}, blockers
    except StandaloneMigrationRejected as exc:
        return None, [{"reason": exc.reason, "ref": exc.ref}]
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return None, [{"reason": "stop_sources_unreadable", "ref": "canonical_sources"}]


# LLM: 仅NORMAL_STOP_KINDS采集迁移来源；planned_restart沿原持锁交接，不扫描库存或签MP/closure；原state CAS保持。
# 函数用途: 正常cleanup后保存停止事实，计划重启仅记原交接阻断，避免锁内无用的全home双扫描。
def record_standalone_stop(context, lifecycle, cleanup):
    lifecycle.require_held()
    if (context.control_plane is not None or type(context.coordinator_worker_count) is not int
            or context.coordinator_worker_count != 0 or context.paths.root != lifecycle.path.parent
            or context.process_identity.get("pid") != os.getpid()):
        raise ValueError("standalone_original_stop_context_required")
    raw = read_bytes_beneath(context.paths.root, ("gateway_state.json",), max_bytes=4 * 1024 * 1024, require_dir_fd=True)
    state = load_strict_json(raw)
    if (state.get("status") != "stopped" or state.get("exit_code") != 0
            or state.get("process_identity") != context.process_identity):
        raise ValueError("standalone_normal_stop_source_required")
    home = home_paths(configured_home_root(context.config)).root
    if state["termination_kind"] in NORMAL_STOP_KINDS:
        sources, blockers = _stop_sources(context, home)
    else:
        sources = None
        blockers = [{"reason": "planned_restart_requires_original_handover", "ref": "termination_kind"}]
    fact = {"schema_version": STANDALONE_STOP_SCHEMA, "root": str(context.paths.root), "home": str(home),
            "config_path": str(context.config_path.resolve()), "process_identity": dict(context.process_identity),
            "started_at": context.process_started_at, "status": "stopped", "termination_kind": state["termination_kind"],
            "cleanup": {key: cleanup[key] for key in ("drain_complete", "alive_threads", "interrupted_model_calls",
                "host_shutdown_executors", "surviving_background_sessions")},
            "sources": sources, "blockers": blockers}
    # LLM: 原state事务内重核完整raw与实际原锁，任何漂移不覆盖；只追加本次fact，不宣称physicalquiet。
    # 函数用途: 保存同一正常终止投影中的版本化来源。
    def update(current):
        lifecycle.require_held()
        if (read_bytes_beneath(context.paths.root, ("gateway_state.json",), max_bytes=4 * 1024 * 1024,
                               require_dir_fd=True) != raw
                or json.dumps(current, sort_keys=True) != json.dumps(state, sort_keys=True)):
            raise ValueError("standalone_stop_source_changed")
        return {**current, "standalone_stop": fact}

    return update_json_file_atomic(context.paths.state, update, require_existing=True)
