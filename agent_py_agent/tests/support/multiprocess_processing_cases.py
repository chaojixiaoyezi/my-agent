# LLM: 仅用于隔离临时目录的processing恢复合同，旧域进程表由测试显式合成，不代表真实完整主机P2观察。
# 模块用途: 复用固定来源的真实P1/P3准备步骤及原队列、RuntimeDB；不启动服务或模型，不伪造执行许可。
import os
from dataclasses import asdict
from types import SimpleNamespace

from agent_py_agent.agent.gateway_parts import multiprocess_admission as ma
from agent_py_agent.agent.gateway_parts import request_worker as rw
from agent_py_agent.agent.gateway_parts.daemon_metadata import process_host_id
from agent_py_agent.agent.gateway_parts.io import (
    GATEWAY_REQUEST_FINGERPRINT_SCHEMA,
    gateway_request_fingerprint,
    write_json_file_atomic,
)
from agent_py_agent.agent.gateway_parts.multiprocess_executor_identity import process_birth
from agent_py_agent.agent.gateway_parts.multiprocess_executor_ledger import ExecutorLedger
from agent_py_agent.agent.gateway_parts.multiprocess_records import validate_record
from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition, owner_digest
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.request_owner_binding import REQUEST_OWNER_KEY
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


# LLM: 原实际PID/birth只供旧generation归档，不能替代停止证据或P2许可；没有跨主机查询。
# 函数用途: 查询本测试进程的准确身份供原registry producer保存。
def worker_process_identity(pid):
    return validate_record({'schema_version': 'multiprocess_process_identity.v1', 'host_id': process_host_id(), 'pid': pid, 'start_time': process_birth(pid)})

# LLM: 仅保存临时隔离的原P1/P3事实，真实许可仍从产品门取得；修改须保留恢复负例。
# 函数用途: 准备processing恢复的受控测试输入，不连接外部服务。
def ledger_case(root):
    owner_sha = owner_digest(OwnerIdentity.provider_user("synthetic", "p2-user"))
    ledger = ExecutorLedger(root, WorkerPartition(0, 1, "old-c"))
    scope = {"state_scope": "agent_state", "role": "W", "c_run_id": "old-c", "worker_index": 0,
             "worker_count": 1, "owner_shas": [owner_sha], "executor_reservation_ids": ["exec-one"]}
    record = {"schema_version": "multiprocess_executor_reservation.v1", "reservation_id": "exec-one", "kind": "shell",
              "c_run_id": "old-c", "owner_sha": owner_sha, "request_id": "request-one",
              "execution_attempt_id": "attempt-one", "lease_epoch": 0, "tool_call_id": "tool-one",
              "state": "reserved", "process_identity": None, "pgid": None, "sid": None}
    return ledger, scope, record


# LLM: 仅保存临时隔离的原P1/P3事实，真实许可仍从产品门取得；修改须保留恢复负例。
# 函数用途: 准备processing恢复的受控测试输入，不连接外部服务。
def register(ledger, source, pid=101, birth=10):
    record = ledger.reserve(source)
    return ledger.advance(record, "registered", {"process_identity": {"schema_version": "multiprocess_process_identity.v1",
                         "host_id": "host-one", "pid": pid, "start_time": birth}, "pgid": pid, "sid": pid})


# LLM: 仅P1旧count准备沿原v1 publisher，不出生P4活角色或手写closure；后续恢复/P3与真实权限断言原样保留。
# 函数用途: 在临时目录准备旧规范元信息和原请求账，不能当作真实生命周期关闭证明。
def setup_case(tmp_path, local=False, previous_generations=()):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.inbox.mkdir(parents=True)
    ledger, scope, source = ledger_case(paths.root)
    from agent_py_agent.agent.gateway_parts.multiprocess_generation_metadata import (
        _publish_generation_once,
        read_generation_metadata,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_records import serialize_record

    # 此fixture只需要P1旧count，不曾出生C/W角色；不再用带活PID的P4冒充纯count元信息。
    for generation, count in (*previous_generations, (ledger.partition.c_run_id, ledger.partition.count)):
        metadata = {'schema_version': 'multiprocess_c_generation.v1',
                    'c_run_id': generation, 'worker_count': count}
        before = serialize_record(metadata)
        _publish_generation_once(paths.root, metadata)
        actual, raw = read_generation_metadata(paths.root, generation)
        assert raw == before and actual == metadata and actual['worker_count'] == count
    owner = OwnerIdentity.local_main() if local else OwnerIdentity.provider_user("synthetic", "p2-user")
    scope["owner_shas"] = [ma.owner_digest(owner)]
    if local:
        scope.update(role="C", worker_index=None)
    source["owner_sha"] = ma.owner_digest(owner)
    agent = SimpleNamespace(config=SimpleNamespace(gateway_per_user_owner_scoping=False))
    gate = ma.PersistentAdmission(paths, agent, ma.CoordinatorAdmissionScope(1, "old-c"))
    payload = {"id": "request-one", "kind": "ask", "prompt": "isolated", "created_at": 1.0,
               "metadata": {"channel": "synthetic", "user_id": "p2-user"},
               "conversation": {"channel": "synthetic", "channel_conversation_id": "test-chat"}}
    # 合成旧入口已准备的结构化事实；本夹具不验认证边界，后续真实gate仍自行验证完整指纹。
    payload[REQUEST_OWNER_KEY] = asdict(owner)
    payload["metadata"][REQUEST_OWNER_KEY] = asdict(owner)
    payload["metadata"]["gateway_admin_binding_version"] = "binding-v1"
    payload["request_fingerprint_schema"] = GATEWAY_REQUEST_FINGERPRINT_SCHEMA
    payload["request_fingerprint"] = gateway_request_fingerprint(payload, payload["id"])
    path = paths.inbox / "request-one.json"
    write_json_file_atomic(path, payload)
    ownership = (owner, ma.session_scope_for_request(payload))
    assert gate.admit(path, ownership, rw.AdmissionLimits(8, 50))["status"] == "admitted"
    return paths, ledger, scope, source, path, ownership, agent


# LLM: 仅保存临时隔离的原P1/P3事实，真实许可仍从产品门取得；修改须保留恢复负例。
# 函数用途: 准备processing恢复的受控测试输入，不连接外部服务。
def current_gate(case, run="new-c"):
    paths, _ledger, _scope, _source, _path, _ownership, agent = case
    return ma.PersistentAdmission(paths, agent, ma.CoordinatorAdmissionScope(1, run))
