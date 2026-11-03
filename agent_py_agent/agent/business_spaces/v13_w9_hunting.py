"""数据湖、只读狩猎与 owner 隔离的保存查询和回溯策略。"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from uuid import uuid4

from ..plugin_host.errors import HostAPIError
from .v13_w9_datasources import (
    w9_check_query,
    w9_datasource,
    w9_datasource_store,
    w9_get,
    w9_list,
    w9_now,
    w9_page,
    w9_query,
    w9_require_space,
    w9_write,
)


# LLM: 显式数据源先按owner查找，缺失统一404，再解析空间；墓碑不得再次公开。
# 函数用途: 取得本次已经核验空间的数据源规格。
def _sources(call, datasource_ids=None):
    if datasource_ids is not None:
        sources = [w9_datasource(call, identifier) for identifier in dict.fromkeys(datasource_ids)]
        w9_require_space(call, call.params.get("space_id"))
        return sources
    w9_require_space(call, call.params.get("space_id"))
    if datasource_ids is None:
        workspace = getattr(call.context.host, "workspace", None)
        hidden = {row["datasource_id"] for row in w9_list(call, "datasource_tombstones")}
        datasource_ids = [row["datasource_id"] for row in
            w9_datasource_store(call).list_datasources(workspace_root=workspace)
            if row["datasource_id"] not in hidden]
    return [w9_datasource(call, identifier) for identifier in dict.fromkeys(datasource_ids)]


# LLM: 不猜测 OCSF 分类，不把 options、DSN 或底层异常返回给客户端。
# 函数用途: 对指定数据源进行真实表结构发现并报告不支持的驱动。
def _schema(call, datasource_ids=None):
    tables, unsupported = [], []
    workspace = getattr(call.context.host, "workspace", None)
    store = w9_datasource_store(call)
    for spec in _sources(call, datasource_ids):
        identifier, kind = spec["datasource_id"], spec["kind"]
        if kind != "sqlite":
            unsupported.append({"datasource_id": identifier, "kind": kind,
                "error_code": "DATASOURCE_DRIVER_UNSUPPORTED"})
            continue
        try:
            result = store.inspect_schema(identifier, workspace_root=workspace)
        except (OSError, ValueError, sqlite3.Error):
            unsupported.append({"datasource_id": identifier, "kind": kind,
                "error_code": "DATASOURCE_SCHEMA_FAILED"})
            continue
        if not result.get("ok"):
            unsupported.append({"datasource_id": identifier, "kind": kind,
                "error_code": result.get("error_code", "DATASOURCE_SCHEMA_FAILED")})
            continue
        mapping = spec.get("options", {}).get("ocsf_mapping", {})
        for table in result["tables"]:
            name = table["name"]
            classification = mapping.get(name) if isinstance(mapping, dict) else None
            if not isinstance(classification, str) or not 1 <= len(classification) <= 128:
                classification = None
            tables.append({"datasource_id": identifier, "table": name,
                "ocsf_class": classification, "columns": [
                    {"name": col["name"], "type": col["type"] or "UNKNOWN",
                     "notnull": bool(col["notnull"]), "pk": bool(col["pk"])}
                    for col in table["columns"]],
                "row_count_estimate": table.get("row_count_estimate")})
    if len(tables) > 2000 or len(unsupported) > 500 or any(
            len(table["columns"]) > 500 for table in tables):
        raise HostAPIError("LIMIT_EXCEEDED", 400)
    return {"tables": tables, "unsupported": unsupported,
        "coverage": "partial" if unsupported else "complete"}


# LLM: 使用原生 inspect_schema 的 allowed_tables 投影，显式 ID 仍须 owner 和墓碑检查。
# 函数用途: 返回可见数据源的表结构。
def lake_schema(call):
    identifier = call.params.get("datasource_id")
    return _schema(call, [identifier] if identifier else None)


# LLM: KQL 没有当前驱动实现，必须明确拒绝，不能当作 SQL 或空成功。
# 函数用途: 把数据湖 SQL 交给唯一共享只读查询引擎。
def lake_sql(call):
    identifier = call.params.get("datasource_id")
    if identifier:
        w9_datasource(call, identifier)
    w9_require_space(call, call.params.get("space_id"))
    if call.params.get("language", "sql") != "sql":
        raise HostAPIError("QUERY_REJECTED", 400)
    identifier = call.params.get("datasource_id")
    return w9_query(call, call.params["statement"],
        [identifier] if identifier else None, call.params.get("limit", 100))


# LLM: 任务沿 W0 assistant/job 生命周期提交，模型上下文只含受控 schema。
# 函数用途: 创建自然语言转查询异步任务。
def translate(call):
    from ..gateway_parts.business_api_v13 import v13_start_model_job
    space_id = w9_require_space(call, call.params.get("space_id"))
    facts = lake_schema(call)
    instruction = json.dumps({"prompt": call.params["prompt"], "schema": facts,
        "target": call.params["target"], "result_format": "JSON"}, ensure_ascii=False)
    return v13_start_model_job(call, kind="nl_query_translation", space_id=space_id,
        instruction=instruction)


# LLM: 普通查询无副作用，agent 模式只启动原生任务，不伪造同步查询结果。
# 函数用途: 执行只读狩猎或提交异步模型狩猎。
def search(call):
    space_id = w9_require_space(call, call.params.get("space_id"))
    if call.params.get("mode", "query") == "agent":
        from ..gateway_parts.business_api_v13 import v13_start_model_job
        facts = _schema(call, call.params.get("datasource_ids"))
        instruction = json.dumps({"prompt": call.params["query"], "schema": facts,
            "result_format": "JSON"}, ensure_ascii=False)
        return v13_start_model_job(call, kind="hunt_agent", space_id=space_id,
            instruction=instruction)
    return w9_query(call, call.params["query"], call.params.get("datasource_ids"),
        call.params.get("limit", 100))


# LLM: 公共投影只返回合同字段，内部 owner 与扩展字段不暴露。
# 函数用途: 构造保存狩猎的闭合视图。
def _hunt_view(record):
    return {key: record[key] for key in ("hunt_id", "space_id", "name", "description",
        "query", "language", "datasource_ids", "created_by", "last_run", "revision",
        "created_at", "updated_at")}


# LLM: 即使知道 ID，也不能绕过 owner 与当前空间访问核验。
# 函数用途: 检查并返回一条本 owner 可见的狩猎记录。
def _hunt(call, record):
    if record is None or record.get("owner_id") != call.owner_id:
        raise HostAPIError("RESOURCE_NOT_FOUND", 404)
    w9_require_space(call, record["space_id"])
    return record


# LLM: 已核验空间过滤后再进入 HMAC 分页，避免跨空间游标读取。
# 函数用途: 分页列出当前空间的保存狩猎。
def list_saved(call):
    space_id = w9_require_space(call, call.params.get("space_id"))
    rows = [_hunt_view(row) for row in w9_list(call, "saved_hunts")
        if row.get("space_id") == space_id and row.get("owner_id") == call.owner_id]
    rows.sort(key=lambda row: row["hunt_id"])
    scoped = replace(call, params={**call.params, "space_id": space_id})
    return w9_page(scoped, "saved_hunts", rows)


# LLM: 不把不存在或其它 owner 的记录降级为空成功。
# 函数用途: 读取一条保存狩猎。
def get_saved(call):
    return {"hunt": _hunt_view(_hunt(call, w9_get(call, "saved_hunts", call.params["hunt_id"])))}


# LLM: 保存前检查只读语法；带数据源时再经真实 authorizer 验证表权限。
# 函数用途: 在幂等事务中创建新的保存查询。
def create_saved(call):
    space_id = w9_require_space(call, call.params.get("space_id"))

    # LLM: 校验放在回调内，重放不因数据源随后失效而产生新的执行。
    # 函数用途: 校验并保存唯一的新狩猎记录。
    def mutate(records):
        w9_check_query(call.params["query"])
        identifiers = call.params.get("datasource_ids", [])
        if identifiers:
            w9_query(call, call.params["query"], identifiers, 1)
        identifier, now = "hunt-" + uuid4().hex, w9_now(call)
        record = {"hunt_id": identifier, "space_id": space_id, "owner_id": call.owner_id,
            "name": call.params["name"], "description": call.params.get("description", ""),
            "query": call.params["query"], "language": call.params.get("language", "sql"),
            "datasource_ids": list(identifiers), "created_by": call.subject_id,
            "last_run": None, "revision": 1, "created_at": now, "updated_at": now}
        records[identifier] = record
        return {"hunt": _hunt_view(record)}

    return w9_write(call, "saved_hunts", mutate)


# LLM: owner、空间与 CAS 都基于锁内最新记录，避免旧快照覆盖。
# 函数用途: 按版本删除保存狩猎并支持幂等重放。
def delete_saved(call):
    identifier = call.params["hunt_id"]

    # LLM: 收据重放在事务层处理，真实删除只发生一次。
    # 函数用途: 核验并移除最新记录。
    def mutate(records):
        record = _hunt(call, records.get(identifier))
        if call.expected_revision != record["revision"]:
            raise HostAPIError("REVISION_CONFLICT", 409)
        del records[identifier]
        return {"hunt_id": identifier, "deleted": True}

    return w9_write(call, "saved_hunts", mutate)


# LLM: 在事务内读取并复跑当前查询，避免覆盖并发删除或其它写入。
# 函数用途: 复跑保存查询并保存真实最近运行摘要。
def run_saved(call):
    identifier = call.params["hunt_id"]

    # LLM: 查询只读，出错不会部分写入 last_run。
    # 函数用途: 执行受控查询并递增狩猎记录版本。
    def mutate(records):
        record = _hunt(call, records.get(identifier))
        scoped = replace(call, params={**call.params, "space_id": record["space_id"]})
        result = w9_query(scoped, record["query"], record["datasource_ids"] or None,
            call.params.get("limit", 100))
        now = w9_now(call)
        updated = {**record, "revision": record["revision"] + 1, "updated_at": now,
            "last_run": {"ran_at": now,
                "row_counts": {row["datasource_id"]: len(row["rows"]) for row in result["results"]},
                "ok": all(row["ok"] for row in result["results"]),
                "error_codes": [row["error_code"] for row in result["results"] if row["error_code"]]}}
        records[identifier] = updated
        return {"hunt": _hunt_view(updated), **result}

    return w9_write(call, "saved_hunts", mutate)


# LLM: 默认策略是合同明示值，不把缺配置解释成启用。
# 函数用途: 返回本 owner 的回溯策略或未配置默认值。
def get_retro_settings(call):
    record = w9_get(call, "retro_hunt_settings", call.owner_id)
    if record is None:
        return {"settings": {"enabled": False, "lookback_days": 30,
            "indicator_types": ["ip", "domain", "sha256", "md5", "url"],
            "max_matches_per_sync": 100, "revision": 0, "updated_at": None}, "configured": False}
    return _settings_view(record)


# LLM: 内部 owner 和 created_at 不属于公开设置，不能直接回传整条记录。
# 函数用途: 生成回溯策略的闭合公开投影。
def _settings_view(record):
    return {"settings": {key: record[key] for key in ("enabled", "lookback_days",
        "indicator_types", "max_matches_per_sync", "revision", "updated_at")}, "configured": True}


# LLM: CAS 比对锁内当前记录版本，首次写入仅接受版本零。
# 函数用途: 原子更新本 owner 回溯策略。
def set_retro_settings(call):
    # LLM: 不使用锁外读取值覆盖，收据仍由共享事务管理。
    # 函数用途: 校验版本并提交新策略。
    def mutate(records):
        previous = records.get(call.owner_id)
        if previous is not None and previous.get("owner_id") != call.owner_id:
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)
        revision = previous["revision"] if previous else 0
        if call.expected_revision != revision:
            raise HostAPIError("REVISION_CONFLICT", 409)
        now = w9_now(call)
        record = {**call.params, "owner_id": call.owner_id, "revision": revision + 1,
            "created_at": previous["created_at"] if previous else now, "updated_at": now}
        records[call.owner_id] = record
        return _settings_view(record)

    return w9_write(call, "retro_hunt_settings", mutate)


# LLM: 只解析结构化 JSON，完整输出 schema 由 W0 job registry 最终校验。
# 函数用途: 解析模型作业对象，拒绝自由文本或非对象结果。
def _model_object(text):
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError("JOB_RESULT_INVALID")
    return result


# LLM: SQL 即使来自模型也须过共享只读检查，不能凭模型宣称安全。
# 函数用途: 完成翻译作业的领域结果检查。
def finalize_translation(text):
    result = _model_object(text)
    if result.get("target") == "sql":
        w9_check_query(result.get("query", ""))
    return result


# LLM: 狩猎结果保持结构化原文，W0 按 hunt_agent schema 核验后才发布。
# 函数用途: 解析异步狩猎的最终结果。
def finalize_hunt(text):
    return _model_object(text)
