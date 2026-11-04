"""W11：额度沿canonical身份及跨进程门，重放不重新占槽。"""
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.background_agents import BackgroundAgentService
from agent_py_agent.agent.conversation.host_actor import HostChannelActor
from agent_py_agent.tests.test_business_v12_f3_agents import business_host as business_host


# LLM: 子进程只替换模型选择，不运行模型；真实Store、锁和null空间准入保持。
# 函数用途: 给并发进程提交一个后台身份并返回结构化失败码。
def _process_create(root, home, actor, key, start, output):
    class Service(BackgroundAgentService):
        # LLM: 测试不依赖模型凭据，身份创建没有模型执行。
        # 函数用途: 提供固定测试模型引用。
        def _profile(self, requested=None):
            return "model-A"

    host = SimpleNamespace(conversation_store=ConversationStore(root),
        home_paths=SimpleNamespace(owner_id="owner", owner_home_dir=home),
        config=SimpleNamespace(max_background_agents=2))
    start.wait(20)
    try:
        result = Service(host).create({"space_id": None, "name": key}, actor=actor, idempotency_key=key)
        output.put(("created", result["agent"]["agent_id"]))
    except Exception as error:
        output.put((getattr(error, "error_code", type(error).__name__), str(error)))


# LLM: 读取公开只读summary，不从列表页长推算额度。
# 函数用途: 取得当前Owner跨空间的身份额度。
def _summary(host, actor):
    from agent_py_agent.agent.conversation.background_agent_quota import (
        background_agent_quota_summary,
    )
    return background_agent_quota_summary(host, actor=actor)


# LLM: pytest仅构造配置并检查参数注册表，验证后台额度默认值、零值及非法类型/负数边界；不写部署配置或运行模型。
# 函数用途: 检查后台助手额度属于不可由普通用户改写的部署安全参数。
def test_config_default_validation_and_admin_boundary():
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.settings.parameter_registry import parameter_registry
    assert AgentConfig().max_background_agents == AgentConfig().max_subagents
    assert AgentConfig(max_background_agents=0).max_background_agents == 0
    for invalid in (-1, True, "8", 1.5):
        with pytest.raises((ValueError, TypeError)):
            AgentConfig(max_background_agents=invalid)
    spec = parameter_registry()["max_background_agents"]
    assert spec.tier == "deployment" and not spec.writable
    assert spec.safety == "boundary"


# LLM: pytest在临时宿主用线程池跨空间竞争同一Owner配额，并创建总控及重放成功请求；只写测试Store，断言超额拒绝、重放不重复占槽且总控不计数。
# 函数用途: 验证并发创建仍遵守跨空间统一额度，幂等重放不多建助手。
def test_atomic_thread_limit_replays_and_cross_space_usage(business_host):
    host, actor = business_host
    host.config.max_background_agents = 2
    service = BackgroundAgentService(host)

    # LLM: 供本测试线程池与重放循环共用；向临时服务提交稳定幂等键，只将准确额度拒绝及其详情转为None，其它失败不能冒充预期拒绝。
    # 函数用途: 尝试创建一个助手，并核对配额已满时的结构化错误。
    def create(index):
        try:
            return service.create({"space_id": "space-" + str(index), "name": "worker"},
                actor=actor, idempotency_key=str(index))
        except Exception as error:
            assert getattr(error, "error_code", None) == "BACKGROUND_AGENT_LIMIT_REACHED"
            assert error.details["used"] == error.details["limit"] == 2
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        replies = list(pool.map(create, range(8)))
    assert sum(reply is not None for reply in replies) == 2
    for index, result in enumerate(replies):
        if result:
            replay = create(index)
            assert replay["agent"]["agent_id"] == result["agent"]["agent_id"]
    service.ensure_controller(actor=actor)
    summary = _summary(host, actor)
    assert (summary["limit"], summary["used"], summary["available"], summary["coverage"]) == (2, 2, 0, "complete")


# LLM: pytest在临时Store创建、退休、恢复及挂起身份并调整内存额度；验证恢复需空槽、降限不杀既有身份、挂起仍计数及零限额与重放语义，不运行模型。
# 函数用途: 检查额度变化与身份生命周期之间的占槽和幂等规则。
def test_restore_checks_limit_and_lowering_never_kills_existing(business_host):
    host, actor = business_host
    host.config.max_background_agents = 2
    service = BackgroundAgentService(host)
    first = service.create({"space_id": "space-A", "name": "first"}, actor=actor, idempotency_key="a")["agent"]
    retired = service.transition(first["agent_id"], "retire", actor=actor,
        expected_revision=first["revision"], idempotency_key="retire")["agent"]
    assert retired["identity_state"] == "retired"
    second = service.create({"space_id": "space-B", "name": "second"}, actor=actor, idempotency_key="b")["agent"]
    third = service.create({"space_id": "space-C", "name": "third"}, actor=actor, idempotency_key="c")["agent"]
    host.config.max_background_agents = 1
    with pytest.raises(ValueError, match="BACKGROUND_AGENT_LIMIT_REACHED"):
        service.transition(first["agent_id"], "restore", actor=actor,
            expected_revision=retired["revision"], idempotency_key="restore")
    summary = _summary(host, actor)
    assert summary["used"] == 2 and summary["available"] == 0 and summary["over_limit"] is True
    assert service.get(second["agent_id"], actor=actor)["agent"]["identity_state"] == "available"
    suspended = service.transition(third["agent_id"], "suspend", actor=actor,
        expected_revision=third["revision"], idempotency_key="suspend")["agent"]
    assert _summary(host, actor)["used"] == 2
    service.transition(third["agent_id"], "resume", actor=actor,
        expected_revision=suspended["revision"], idempotency_key="resume")
    host.config.max_background_agents = 0
    restored = service.transition(first["agent_id"], "restore", actor=actor,
        expected_revision=retired["revision"], idempotency_key="restore")
    host.config.max_background_agents = 1
    replay = service.transition(first["agent_id"], "restore", actor=actor,
        expected_revision=retired["revision"], idempotency_key="restore")
    assert replay["agent"]["revision"] == restored["agent"]["revision"]


# LLM: pytest替换临时服务的索引读页，分别注入partial覆盖及加载错误；只检查配额summary保留None与真实覆盖，不把空页当Owner总量为零。
# 函数用途: 验证资料不完整或损坏时配额数量明确显示未知。
def test_unknown_or_partial_counts_never_claim_zero(business_host, monkeypatch):
    host, actor = business_host
    service = BackgroundAgentService(host)
    monkeypatch.setattr(service, "_index", lambda: SimpleNamespace(page=lambda **k: {
        "items": [], "next_cursor": None, "coverage": "partial", "load_errors": []}))
    result = service.quota_summary(actor=actor)
    assert result["coverage"] == "partial" and result["used"] is None and result["available"] is None
    monkeypatch.setattr(service, "_index", lambda: SimpleNamespace(page=lambda **k: {
        "items": [], "next_cursor": None, "coverage": "complete", "load_errors": [{"error": "unreadable"}]}))
    result = service.quota_summary(actor=actor)
    assert result["coverage"] == "unknown" and result["used"] is None


# LLM: pytest先在临时Store建立身份和额度索引，再让任何历史扫描直接失败；验证后续摘要沿活跃引用计数并拒绝外Owner，不启动任务。
# 函数用途: 检查额度摘要不反复扫历史名册，也不能跨用户读取。
def test_summary_uses_active_references_not_historical_listing(business_host, monkeypatch):
    host, actor = business_host
    service = BackgroundAgentService(host)
    service.create({"space_id": "space-A", "name": "worker"}, actor=actor, idempotency_key="a")
    assert _summary(host, actor)["used"] == 1
    monkeypatch.setattr(BackgroundAgentService, "_index", lambda self: pytest.fail("history scan after bootstrap"))
    assert _summary(host, actor)["used"] == 1
    with pytest.raises(PermissionError):
        _summary(host, replace(actor, owner_id="other"))


# LLM: pytest在同一临时Store启动spawn子进程同步竞争创建额度；子进程使用固定模型引用而不执行模型，汇总创建/拒绝结果并核退出码，覆盖跨进程锁边界。
# 函数用途: 验证多个进程同时创建助手时也不会突破统一额度。
def test_limit_is_cross_process_not_only_thread_lock(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    source = store.threads.create(owner_id="owner", canonical_user_id="local-agent")
    actor = HostChannelActor("owner", source.thread_id, "entry", "local", "local", "user")
    context = multiprocessing.get_context("spawn")
    start, output = context.Event(), context.Queue()
    processes = [context.Process(target=_process_create, args=(store.storage.root, tmp_path, actor, str(i), start, output))
                 for i in range(6)]
    for process in processes:
        process.start()
    start.set()
    results = [output.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    assert sum(code == "created" for code, _ in results) == 2, results
    assert sum(code == "BACKGROUND_AGENT_LIMIT_REACHED" for code, _ in results) == 4, results


# LLM: pytest在临时无归属身份上写入未完成任务使退休停在retiring，再故意损坏该canonical线程；断言责任未清仍占槽、损坏后摘要未知而非零。
# 函数用途: 检查退役中的助手继续计入额度，坏记录不能被当成空额度。
def test_retiring_is_counted_and_corrupt_canonical_never_becomes_zero(business_host):
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft

    host, actor = business_host
    host.config.max_background_agents = 1
    service = BackgroundAgentService(host)
    agent = service.create({"space_id": None, "name": "worker"}, actor=actor, idempotency_key="a")["agent"]
    host.conversation_store.session_tasks.create(SessionTaskDraft(actor.source_thread_id, agent["agent_id"], "pending"))
    retired = service.transition(agent["agent_id"], "retire", actor=actor,
        expected_revision=agent["revision"], idempotency_key="retire")["agent"]
    assert retired["identity_state"] == "retiring"
    assert _summary(host, actor)["unassigned"] == _summary(host, actor)["used"] == 1
    host.conversation_store.storage.thread_path(agent["agent_id"]).write_text("{bad", encoding="utf-8")
    summary = _summary(host, actor)
    assert summary["coverage"] == "unknown" and summary["used"] is None and summary["available"] is None


# LLM: pytest向临时Store写入一批普通历史线程并多次读取配额；断言首轮为partial/未知、后续有界回填推进到complete，不把中途未覆盖的记录判为零。
# 函数用途: 验证旧历史的额度索引分批补齐，而不是一次扫描全部记录。
def test_legacy_backfill_is_bounded_and_progresses_instead_of_scanning_all(business_host):
    host, actor = business_host
    for _ in range(140):
        host.conversation_store.threads.create(owner_id=actor.owner_id, canonical_user_id="local-agent")
    summary = _summary(host, actor)
    assert summary["coverage"] == "partial" and summary["used"] is None
    for _ in range(20):
        summary = _summary(host, actor)
        if summary["coverage"] == "complete":
            break
    assert summary["coverage"] == "complete" and summary["used"] == 0
