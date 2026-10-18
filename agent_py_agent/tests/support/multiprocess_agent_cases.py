"""只在临时目录构造原启动事实和轻量 base；不初始化真实 Agent。"""
from types import SimpleNamespace

from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import freeze_worker_bootstrap
from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition, owner_worker
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


def constructor_case(tmp_path, monkeypatch, owner=None):
    owner = owner or OwnerIdentity.provider_user("synthetic", "alice")
    launch = mode.WorkerLaunch(WorkerPartition(owner_worker(owner, 2), 2, "c-agent"),
                              tmp_path / "gateway", tmp_path / "frozen-home", 101)
    monkeypatch.setattr(mode, "_WORKER_PARTITION", launch.partition)
    frozen = freeze_worker_bootstrap(launch)
    config = AgentConfig(my_agent_home=str(launch.owner_root), model_backend="echo")
    base = SimpleNamespace(config=config, root=tmp_path / "project",
                           runtime_path_resolution=SimpleNamespace(paths={}))
    return frozen, owner, base


# LLM: 真实文件认领必须携带P3预留与P5快照；只初始化临时目录，不触碰用户资料。
# 函数用途: 为执行归属测试提供可复核的原claim。
def execution_case(tmp_path, monkeypatch):
    import json

    from agent_py_agent.agent.gateway_parts.multiprocess_dispatch_binding import (
        bind_claimed_request,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_dispatch_claim import (
        ClaimCandidate,
        freeze_claim_snapshot,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.queue_service import (
        claim_request,
        ensure_gateway_folders,
    )
    from agent_py_agent.tests.support.multiprocess_dispatch_cases import (
        reservation_for,
        stamped_request,
    )

    request = stamped_request()
    launch = mode.WorkerLaunch(WorkerPartition(request["route"]["worker"], 2, "c-p5"),
                              tmp_path / "gateway", tmp_path / "home", 101)
    monkeypatch.setattr(mode, "_WORKER_PARTITION", launch.partition)
    paths = gateway_paths_from_root(launch.gateway_root)
    ensure_gateway_folders(paths)
    path = paths.inbox / f"{request['id']}.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    snapshot = freeze_claim_snapshot(freeze_worker_bootstrap(launch), ClaimCandidate(path, request, reservation_for(request)))
    # 使用真实 P3 预留和新的原 claim 入口，保留下面全部执行身份断言。
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
    reservation = reservation_file(paths, request["id"])
    reservation.parent.mkdir(parents=True, exist_ok=True)
    reservation.write_bytes(snapshot.reservation_bytes)
    claim = claim_request(paths, path, worker_snapshot=snapshot)
    assert claim is not None
    current = json.loads(claim.path.read_text(encoding="utf-8"))
    return bind_claimed_request(snapshot, claim, current), current
