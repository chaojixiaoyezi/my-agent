"""owner 级按分词身份的校准比值缓存：新会话、换线程表面、压缩后的第一次调用也用上真实比值（2026-09-30）。

真机：新开的会话第一次调用显示 44852、实际 35806（偏高 25%），第二次才落回。线程观测只在同线程、同指纹、同压缩代次时可用；
缓存按分词身份（后端、模型、协议、不含凭据的连接）保存最近一次真实比值，线程与本轮观测优先，折算不低于 50%。
"""
import json
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.model import context_calibration_carry as carry
from agent_py_agent.agent.agent_core.model.context_pressure import (
    frozen_compact_request_calibration,
    model_visible_context_snapshot,
    model_visible_context_tokens,
    record_provider_context_observation,
)
from agent_py_agent.agent.conversation.compact_calibration import calibrated_compact_request_tokens
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot


class _World:
    def __init__(self, tmp_path, monkeypatch):
        self.raw = {"tokens": 100_000}
        monkeypatch.setattr("agent_py_agent.agent.agent_core.model.context_pressure.estimate_tokens",
                            lambda _payload: self.raw["tokens"])
        self.store = ConversationStore(tmp_path / "conversations")
        self.agent = SimpleNamespace(
            config=AgentConfig(auto_save_memory=True, model_context_window_tokens=1_000_000),
            backend=SimpleNamespace(context_window_tokens=1_000_000, name="openai_chat", model_name="deepseek-v4-flash",
                                    api_base="https://api.example.invalid/v1", api_key="carry-secret-value",
                                    custom_headers={}, auth_ref={"mode": "api_key"}),
            conversation_store=self.store, home_paths=ensure_my_agent_home(tmp_path / "home"),
        )

    def thread(self, name):
        # 线程按通道绑定区分：每个名字一个独立会话。
        thread = self.store.threads.get_or_create({"channel": "chat", "channel_conversation_id": name,
                                                   "channel_user_id": "carry-user", "canonical_user_id": "carry-user"})
        return thread.thread_id

    def params(self, thread_id):
        return SimpleNamespace(context_scope="conversation", save=True,
                               task_attributes={"conversation_thread_id": thread_id},
                               tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="text"),
                               live_archive_state={})

    def observe(self, params, raw, provider):
        self.raw["tokens"] = raw
        snapshot = model_visible_context_snapshot(self.agent, params, "prompt")
        assert record_provider_context_observation(
            self.agent, params, raw_estimated_tokens=snapshot.raw_estimated_tokens,
            context_surface_fingerprint=snapshot.context_surface_fingerprint,
            response=SimpleNamespace(usage={"prompt_tokens": provider}))

    def tokens(self, params, raw, prompt="prompt"):
        self.raw["tokens"] = raw
        return model_visible_context_tokens(self.agent, params, prompt)


def test_new_thread_first_call_uses_the_owner_ratio(tmp_path, monkeypatch):
    world = _World(tmp_path, monkeypatch)
    world.observe(world.params(world.thread("a")), 100_000, 78_000)
    # 改前：新会话第一次调用只有原始估算 50000；改后按 0.78 折算。
    assert world.tokens(world.params(world.thread("b")), 50_000) == 39_000


def test_other_model_or_endpoint_does_not_borrow_the_ratio(tmp_path, monkeypatch):
    world = _World(tmp_path, monkeypatch)
    world.observe(world.params(world.thread("a")), 100_000, 78_000)
    world.agent.backend.model_name = "deepseek-v4-pro"
    assert world.tokens(world.params(world.thread("b")), 50_000) == 50_000
    world.agent.backend.model_name = "deepseek-v4-flash"
    world.agent.backend.api_base = "https://other.example.invalid/v1"
    assert world.tokens(world.params(world.thread("c")), 50_000) == 50_000


def test_thread_observation_wins_over_the_owner_ratio(tmp_path, monkeypatch):
    world = _World(tmp_path, monkeypatch)
    thread_a = world.thread("a")
    world.observe(world.params(thread_a), 100_000, 60_000)
    world.observe(world.params(world.thread("b")), 100_000, 90_000)  # 更新的 owner 比值是 0.9
    # 线程 A 自己的耐久观测（0.6）优先：90000 × 0.6。
    assert world.tokens(world.params(thread_a), 90_000) == 54_000


def test_after_compaction_the_first_call_uses_the_owner_ratio(tmp_path, monkeypatch):
    world = _World(tmp_path, monkeypatch)
    thread_a = world.thread("a")
    world.observe(world.params(thread_a), 100_000, 78_000)
    thread = world.store.threads.load(thread_a)
    # 压缩提交的 CAS 会清掉线程观测；这里直接清掉，模拟提交后的第一次调用。
    world.store.threads.update_provider_context_observation(thread_a, {}, expected_compact_generation=thread.compact_generation)
    assert world.tokens(world.params(thread_a), 40_000) == 31_200


def test_changed_request_surface_in_the_same_run_uses_the_owner_ratio(tmp_path, monkeypatch):
    world = _World(tmp_path, monkeypatch)
    params = world.params(world.thread("a"))
    world.observe(params, 100_000, 78_000)
    assert world.tokens(params, 110_000, prompt="stable prompt changed") == 85_800


def test_ratio_never_goes_below_half_and_small_calls_do_not_update(tmp_path, monkeypatch):
    world = _World(tmp_path, monkeypatch)
    world.observe(world.params(world.thread("a")), 100_000, 30_000)
    assert world.tokens(world.params(world.thread("b")), 10_000) == 5_000
    before = world.agent.home_paths.owner_context_calibration_json.read_text(encoding="utf-8")
    world.observe(world.params(world.thread("c")), carry.MIN_CARRY_RAW_TOKENS - 1, 10)
    assert world.agent.home_paths.owner_context_calibration_json.read_text(encoding="utf-8") == before


def test_display_and_compaction_gate_agree_on_a_new_thread(tmp_path, monkeypatch):
    world = _World(tmp_path, monkeypatch)
    world.observe(world.params(world.thread("a")), 100_000, 78_000)
    # 请求比观测小和比观测大两种情况，状态条与压缩候选门都得出同一个数（只按比例，不加跨会话增量）。
    for name, raw, expected in (("b", 50_000, 39_000), ("c", 150_000, 117_000)):
        params = world.params(world.thread(name))
        world.raw["tokens"] = raw
        snapshot = model_visible_context_snapshot(world.agent, params, "prompt")
        frozen = frozen_compact_request_calibration(world.agent, params,
                                                    context_surface_fingerprint=snapshot.context_surface_fingerprint)
        assert frozen is not None and frozen.calibration_scope == "owner_ratio"
        assert calibrated_compact_request_tokens(raw, frozen) == snapshot.current_tokens == expected


def test_cache_file_is_numbers_only_bounded_and_corruption_falls_back(tmp_path, monkeypatch):
    world = _World(tmp_path, monkeypatch)
    for index in range(40):
        world.agent.backend.model_name = f"model-{index}"
        world.observe(world.params(world.thread(f"t{index}")), 100_000, 50_000 + index)
    path = world.agent.home_paths.owner_context_calibration_json
    text = path.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert payload["schema"] == "owner_context_calibration.v1" and len(payload["entries"]) == 32
    assert "carry-secret-value" not in text and "api.example.invalid" not in text
    assert all(set(entry) == {"raw_estimated_tokens", "provider_input_tokens", "observed_at"} for entry in payload["entries"].values())
    path.write_text("{broken", encoding="utf-8")
    assert world.tokens(world.params(world.thread("fresh")), 50_000) == 50_000
    # 缓存写不进去（位置被目录占住）也不能让成功的调用失败。
    path.unlink()
    path.mkdir()
    world.observe(world.params(world.thread("still-ok")), 100_000, 78_000)


def test_switch_off_neither_reads_nor_writes_the_cache(tmp_path, monkeypatch):
    world = _World(tmp_path, monkeypatch)
    world.observe(world.params(world.thread("a")), 100_000, 78_000)
    path = world.agent.home_paths.owner_context_calibration_json
    before = path.read_text(encoding="utf-8")
    world.agent.config.memory_context_calibration_carry_enabled = False
    # 关掉后：新会话不读缓存（回到原始估算），成功调用也不写缓存。
    assert world.tokens(world.params(world.thread("b")), 50_000) == 50_000
    world.observe(world.params(world.thread("c")), 100_000, 90_000)
    assert path.read_text(encoding="utf-8") == before
    world.agent.config.memory_context_calibration_carry_enabled = True
    assert world.tokens(world.params(world.thread("d")), 50_000) == 39_000


def test_switch_off_from_the_start_creates_no_file(tmp_path, monkeypatch):
    world = _World(tmp_path, monkeypatch)
    world.agent.config.memory_context_calibration_carry_enabled = False
    world.observe(world.params(world.thread("a")), 100_000, 78_000)
    assert not world.agent.home_paths.owner_context_calibration_json.exists()
