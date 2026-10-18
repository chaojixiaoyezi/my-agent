"""P4 real SimpleAgent initialization; offline echo, not a live model/Gateway test."""
import os
from dataclasses import replace
from importlib import import_module
from pathlib import Path

import pytest

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import (
    WorkerBootstrapError,
    freeze_worker_bootstrap,
)
from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition, owner_worker
from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    ensure_owner_home,
    resolve_owner_home,
)


# LLM: 只快照共享树及管理员；新远端 owner 是本次唯一允许的物化范围。
# 函数用途: 比较目录、模式和文件字节，抓初始化的全局种子/DB写入。
def _shared_snapshot(home):
    return {
        str(path.relative_to(home)): (path.stat().st_mode, None if path.is_dir() else path.read_bytes())
        for path in home.rglob("*")
        if not path.is_relative_to(home / "owners" / "providers")
    }


# LLM: C 用默认真实入口准备共享资源，然后冻结原 W 启动事实；不替换 SimpleAgent。
# 函数用途: 提供离线模型及真实临时 home。
@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setattr(mode, "_WORKER_PARTITION", None)
    home = (tmp_path / "home").resolve()
    root = (tmp_path / "repo").resolve()
    config = AgentConfig(my_agent_home=str(home), model_backend="echo", prompt_files=[])
    SimpleAgent(replace(config), root)
    partition = WorkerPartition(0, 2, "p4-real-agent")
    launch = mode.WorkerLaunch(partition, home / "gateway", home, os.getppid(), workspace_roots=(root,))
    monkeypatch.setenv("MY_AGENT_GATEWAY_WORKER", mode.worker_environment(launch, {})["MY_AGENT_GATEWAY_WORKER"])
    assert mode.establish_worker_mode(launch) == {"ok": True}
    owners = [OwnerIdentity.provider_user("test", f"u{i}") for i in range(30)]
    good = next(owner for owner in owners if owner_worker(owner, 2) == 0)
    wrong = next(owner for owner in owners if owner_worker(owner, 2) == 1)
    return freeze_worker_bootstrap(launch), config, root, good, wrong


# LLM: 动态导入让未实现接口成为行为红测而非整文件收集失败。
# 函数用途: 调用父级约定的生产惰性池入口。
def _pool(frozen, config, root):
    module = import_module("agent_py_agent.agent.gateway_parts.multiprocess_worker_agent")
    return module.create_worker_pool(frozen, config, root)


# LLM: 正常链必须是真实Agent/真实库；共享字节比较覆盖构造而非仅资源清单。
# 函数用途: 核对惰性、原池复用、owner store读写、pin和分区冻结。
def test_real_owner_is_lazy_and_shared_initialization_is_read_only(worker):
    frozen, config, root, owner, _ = worker
    home = frozen.launch.owner_root
    before = _shared_snapshot(home)
    owner_home = resolve_owner_home(home, owner).home_dir
    pool = _pool(frozen, config, root)
    assert isinstance(pool, OwnerScopedAgentPool)
    assert pool.peek(owner) is None and pool.hard_agents() == []
    assert not owner_home.exists()
    # 本组件夹具显式准备C物化输出；不是native或实际C进程的证明。
    ensure_owner_home(home, owner)
    agent = pool.get(owner)
    assert isinstance(agent, SimpleAgent)
    assert agent.home_paths.owner_home_dir == owner_home
    assert agent._gateway_worker_partition is frozen.launch.partition
    assert pool.get(owner) is agent and pool.peek(owner) is agent
    with pool.pin(agent):
        assert pool.hard_agents() == [agent]
    assert Path(agent.config.local_store_path).is_relative_to(owner_home)
    assert agent.local_store.get_record("absent") is None
    row = agent.local_store.upsert_record(source_type="p4-test", source_id="one", title="owner data", content="private")
    assert agent.local_store.get_record(row.id).content == "private"
    assert _shared_snapshot(home) == before


# LLM: 拒绝必须发生在owner首次写入前，不能先构造main再改config。
# 函数用途: 核对管理员/错误分区和配置根漂移的硬阻断。
@pytest.mark.parametrize("bad", ["partition", "main", "home"])
def test_wrong_owner_or_root_rejected_before_writes(worker, bad):
    frozen, config, root, owner, wrong = worker
    pool = _pool(frozen, config, root)
    before = _shared_snapshot(frozen.launch.owner_root)
    if bad == "home":
        config = replace(config, my_agent_home=str(root / "wrong-home"))
        with pytest.raises(WorkerBootstrapError):
            _pool(frozen, config, root)
        assert not (root / "wrong-home").exists()
    else:
        rejected = wrong if bad == "partition" else OwnerIdentity.local_main()
        with pytest.raises(WorkerBootstrapError):
            pool.get(rejected)
    assert not resolve_owner_home(frozen.launch.owner_root, owner).home_dir.exists()
    assert not resolve_owner_home(frozen.launch.owner_root, wrong).home_dir.exists()
    assert _shared_snapshot(frozen.launch.owner_root) == before


# LLM: 检查发生在实际消费点；池建立后删除共享资源也不能补初始化。
# 函数用途: 缺共享DB或模板明确失败，字节/目录不变且owner未物化。
@pytest.mark.parametrize("relative", ["global_index/scheduler_due.sqlite3", "templates/memory.md"])
def test_missing_shared_resource_is_not_recreated(worker, relative):
    frozen, config, root, owner, _ = worker
    pool = _pool(frozen, config, root)
    database = frozen.launch.owner_root / relative
    database.unlink()
    before = _shared_snapshot(frozen.launch.owner_root)
    with pytest.raises(WorkerBootstrapError, match="resource_missing"):
        pool.get(owner)
    assert not database.exists()
    assert not resolve_owner_home(frozen.launch.owner_root, owner).home_dir.exists()
    assert _shared_snapshot(frozen.launch.owner_root) == before


# LLM: 真实池入口必须在首次ready之前核验共享种子及索引；读取拒绝不创建owner也不修复共享文件。
# 函数用途: 钉住缺资源的启动池不能被当作可接单状态。
@pytest.mark.parametrize("relative", ["templates/SOUL.md", "shared/indexes/skills.jsonl", "shared/indexes/role_templates.jsonl"])
def test_missing_shared_resource_blocks_pool_startup(worker, relative):
    frozen, config, root, owner, _ = worker
    path = frozen.launch.owner_root / relative
    assert path.is_file()
    path.unlink()
    before = _shared_snapshot(frozen.launch.owner_root)
    with pytest.raises(WorkerBootstrapError, match="resource_missing"):
        _pool(frozen, config, root)
    assert not path.exists()
    assert not resolve_owner_home(frozen.launch.owner_root, owner).home_dir.exists()
    assert _shared_snapshot(frozen.launch.owner_root) == before


# LLM: W不得从普通构造绕过冻结合同；默认单进程行为另有正测。
# 函数用途: 无bootstrap的真实构造在创建root前拒绝。
def test_plain_constructor_cannot_bypass_worker_contract(worker):
    _, config, root, _, _ = worker
    wrong_root = root / "must-not-create"
    with pytest.raises(WorkerBootstrapError):
        SimpleAgent(config, wrong_root)
    assert not wrong_root.exists()


# LLM: 默认count=0仍沿旧入口创建共享库和管理员，不因P4变成只读。
# 函数用途: 守住单进程默认初始化及本地store。
def test_default_constructor_still_initializes(tmp_path, monkeypatch):
    monkeypatch.setattr(mode, "_WORKER_PARTITION", None)
    agent = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / "home"), model_backend="echo"), tmp_path / "repo")
    assert agent.home_paths.owner_id == "local/main"
    assert (agent.home_paths.global_index_dir / "scheduler_due.sqlite3").is_file()
    assert agent.local_store.get_record("absent") is None


# LLM: 父级可热改config但不能从中覆盖原home；返回对象检查必须看实际路径。
# 函数用途: 核对配置快照和缓存对象的home守卫。
def test_hot_config_never_rebinds_home_and_cache_validates_actual_paths(worker):
    frozen, config, root, owner, _ = worker
    pool = _pool(frozen, config, root)
    config.my_agent_home = str(root / "hot-home")
    # 同进程组件备料，不声称真实native物化链通过。
    ensure_owner_home(frozen.launch.owner_root, owner)
    agent = pool.get(owner)
    agent.config.my_agent_home = str(root / "another-hot-home")
    assert pool.get(owner).home_paths.root == frozen.launch.owner_root
    agent.home_paths = replace(agent.home_paths, root=root / "wrong")
    with pytest.raises(WorkerBootstrapError, match="agent_home_root_mismatch"):
        pool.get(owner)
    with pytest.raises(WorkerBootstrapError, match="agent_home_root_mismatch"):
        pool.peek(owner)
    assert not (root / "hot-home").exists()


# LLM: 即使已缓存也不允许进程模式/代次更换后继续使用旧owner。
# 函数用途: 验证每次get都核原冻结分区。
def test_cached_agent_rejects_process_partition_drift(worker, monkeypatch):
    frozen, config, root, owner, _ = worker
    pool = _pool(frozen, config, root)
    ensure_owner_home(frozen.launch.owner_root, owner)  # 组件备料，不是C/native端到端证据。
    pool.get(owner)
    monkeypatch.setattr(mode, "_WORKER_PARTITION", replace(frozen.launch.partition, c_run_id="other"))
    with pytest.raises(WorkerBootstrapError, match="worker_mode_mismatch"):
        pool.get(owner)


# LLM: owner路径已有链接也不能跳到另一owner执行种子/初始化；比较受害者完整字节。
# 函数用途: 抓owner根及实际初始化子目录的跨owner破坏。
@pytest.mark.parametrize("location", ["home", "memory"])
def test_owner_symlink_is_rejected_before_victim_write(worker, location):
    frozen, config, root, owner, _ = worker
    home = frozen.launch.owner_root
    target = resolve_owner_home(home, owner).home_dir
    victim = home / "owners" / "local" / "main"
    if location == "memory":
        target.mkdir(parents=True)
        target = target / "memory"
        victim = victim / "memory"
    else:
        target.parent.mkdir(parents=True)
    target.symlink_to(victim, target_is_directory=True)
    before = _shared_snapshot(home)
    with pytest.raises(WorkerBootstrapError, match="resource_unsafe_path"):
        _pool(frozen, config, root).get(owner)
    assert _shared_snapshot(home) == before


# LLM: 同分区的第二owner同样使用独立真实store，不能因池复用泄露前一个用户记录。
# 函数用途: 用实际写入/读回验证canonical owner隔离和池分区容量。
def test_same_partition_owners_have_separate_stores(worker):
    frozen, config, root, owner, _ = worker
    other = next(
        candidate for i in range(30)
        if (candidate := OwnerIdentity.provider_user("test", f"second{i}")) != owner
        and owner_worker(candidate, frozen.launch.partition.count) == frozen.launch.partition.index
    )
    pool = _pool(frozen, config, root)
    # 分别准备原C输出；保留两owner真实store/读回断言，非native通信证明。
    ensure_owner_home(frozen.launch.owner_root, owner)
    ensure_owner_home(frozen.launch.owner_root, other)
    first, second = pool.get(owner), pool.get(other)
    row = first.local_store.upsert_record(source_type="p4-test", source_id="one", title="private", content="only first")
    assert first.local_store.get_record(row.id).content == "only first"
    assert second.local_store.get_record(row.id) is None
    assert first.home_paths.owner_home_dir != second.home_paths.owner_home_dir


# LLM: W的真实构造不能负责owner目录/种子首写；缺C物化明确失败，不因原P4就绪自行补建。
# 函数用途: 钉住默认W冷构造没有当次物化结果时零首写。
def test_worker_home_requires_c_materialization_before_construct(worker):
    frozen, config, root, owner, _ = worker
    home = resolve_owner_home(frozen.launch.owner_root, owner).home_dir
    with pytest.raises(WorkerBootstrapError, match='resource_missing'):
        _pool(frozen, config, root).get(owner)
    assert not home.exists()


# LLM: 这里只证明真实W构造消费已存在种子；C物化撤权/native另有真实Popen测试，不把此fixture叫E2E。
# 函数用途: 禁止W复用ensure_owner_home做种子更新，即使全部文件已存在。
def test_worker_home_consumes_seed_read_only(worker, monkeypatch):
    from agent_py_agent.agent.user_space import worker_home
    from agent_py_agent.agent.user_space.owner_resolver import ensure_owner_home

    frozen, config, root, owner, _ = worker
    ensure_owner_home(frozen.launch.owner_root, owner)
    monkeypatch.setattr(worker_home, 'ensure_owner_home', lambda *_: pytest.fail('W must not seed owner home'), raising=False)
    actual = _pool(frozen, config, root).get(owner)
    assert actual.home_paths.owner_home_dir == resolve_owner_home(frozen.launch.owner_root, owner).home_dir


# LLM: 此处仅查生产组合根保留原对象，绝不把桩当原生sender证明；真实通信由native套件覆盖。
# 函数用途: 防止握手对象在factory边界被丢弃，池消费请求时仍可调用同一client。
def test_request_factory_transports_original_native_client(worker, monkeypatch):
    from types import SimpleNamespace  # noqa: I001

    from agent_py_agent.agent import settings
    from agent_py_agent.agent.gateway_parts.multiprocess_worker import create_request_pump

    frozen, config, _, _, _ = worker
    original = SimpleNamespace()
    monkeypatch.setattr(settings, 'load_config', lambda _: config)
    pump = create_request_pump(frozen.launch, Path('unused-config'), native_client=original)
    try:
        assert getattr(pump.pool, 'native_client', None) is original
        assert pump.inflight == 0
    finally:
        pump.close()


@pytest.mark.parametrize('configured', ['relative', 'tilde'])
def test_request_factory_compares_original_canonical_home_before_freezing_snapshot(worker, monkeypatch, configured):
    from agent_py_agent.agent import settings
    from agent_py_agent.agent.gateway_parts.multiprocess_worker import create_request_pump
    from agent_py_agent.agent.user_space.home_root import configured_home_root

    frozen, config, _, _, _ = worker
    home = frozen.launch.owner_root
    monkeypatch.chdir(home.parent)
    monkeypatch.setenv('HOME', str(home.parent))
    config.my_agent_home = home.name if configured == 'relative' else '~/' + home.name
    before = _shared_snapshot(home)
    monkeypatch.setattr(settings, 'load_config', lambda _: config)
    pump = create_request_pump(frozen.launch, Path('unused-config'))
    try:
        assert config.my_agent_home == str(home)
        assert configured_home_root(config) == str(home)
        assert config.host_home_snapshot.root == home
        assert pump.inflight == 0 and _shared_snapshot(home) == before
    finally:
        pump.close()
