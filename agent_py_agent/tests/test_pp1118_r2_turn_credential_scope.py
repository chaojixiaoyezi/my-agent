"""PP1118 Round-2 Slice 1: 回合级凭据脱敏作用域绑定、不可哈希宿主生命周期租约、索引淘汰与原子容量预检测试。"""
from __future__ import annotations

import contextvars
import copy
import gc
import logging
import threading
import weakref
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.agent_core.tool_runtime_ledger import write_boundary_with_runtime_ledger
from agent_py_agent.agent.common import log_redaction as log_red_mod
from agent_py_agent.agent.common.log_redaction import install_log_redaction
from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter
from agent_py_agent.agent.tooling import host_output_redaction as hor_mod
from agent_py_agent.agent.tooling.host_output_redaction import (
    _MAX_ACTIVE_SCOPES_COUNT,
    _MAX_ACTIVE_TURNS_PER_SCOPE_COUNT,
    _MAX_TURN_RAW_BYTES,
    _MAX_TURN_RAW_VALUES_COUNT,
    HostOutputRedaction,
    TurnCredentialRedaction,
    active_turn_redactions_for_scope,
    credential_echo_variants,
    turn_credential_redaction,
)
from agent_py_agent.agent.user_space.task_credentials import CredentialMaterial
from agent_py_agent.tests.test_ingestion_task_credential_scope import _agent


def _assert_zero_hits(container: object, secrets_list: tuple[str, ...]) -> None:
    rendered = repr(container)
    for variant in credential_echo_variants(secrets_list):
        assert variant not in rendered


def _make_tool_loop_params(
    *,
    effective_on_chunk: object,
    request_id: str = "loop-req-1",
    run_id: str = "loop-run-1",
    task_id: str = "loop-task-1",
    task_attributes: dict | None = None,
) -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt="查询报表",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=effective_on_chunk,
        allowed_tools=None,
        write_boundary=None,
        task_attributes=task_attributes if task_attributes is not None else {},
        request_id=request_id,
        run_id=run_id,
        task_id=task_id,
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
    )


def test_tool_loop_execute_params_effective_on_chunk_auto_binds_and_scrubs_chunks(tmp_path: Path) -> None:
    agent = _agent(tmp_path / "fg_loop", "user", full=False)
    secret_val = "SYNTHETIC_LOOP_EFFECTIVE_CHUNK_TOK_8842"
    live_events: list[dict[str, object]] = []

    writer = BufferedChunkStreamWriter(
        tmp_path / "loop_chunks.jsonl",
        delivery_channel="feishu",
        rich_transcript=True,
        transcript_sink=live_events.append,
    )
    writer.bind_conversation_identity("thr-effective-chunk-01")
    writer.set_verbose_level("stream")

    def _setup_and_register() -> tuple[TurnCredentialRedaction, weakref.ReferenceType[ToolLoopExecuteParams]]:
        loop_params = _make_tool_loop_params(
            effective_on_chunk=writer,
            request_id="req-eff-01",
            run_id="run-eff-01",
            task_id="task-eff-01",
            task_attributes={},
        )
        assert not hasattr(loop_params, "on_chunk")
        boundary = write_boundary_with_runtime_ledger(agent, loop_params)
        assert boundary is not None
        turn_red = turn_credential_redaction(agent, loop_params)
        assert writer._turn_credential_redaction is turn_red
        assert turn_red.thread_id == "thr-effective-chunk-01"

        material = CredentialMaterial(
            credential_ref="cred_" + "a" * 32,
            generation=1,
            origin="https://api.example.test",
            target_url="https://api.example.test/v1/data",
            values={"token": secret_val},
        )
        updated = turn_red.try_register(material)
        assert isinstance(updated, HostOutputRedaction)
        assert turn_red.active is True
        return turn_red, weakref.ref(loop_params)

    turn_red, params_ref = _setup_and_register()
    gc.collect()
    assert params_ref() is None
    # BufferedChunkStreamWriter 尚未 close 时继续持有回合租约，确保尾块与最终提交仍完成净化。
    assert turn_red.active is True

    stream_call = writer.bind_model_call("mc-eff-1")
    stream_call("状态回执前缀：" + secret_val[:14])
    stream_call(secret_val[14:] + " 已接收。")
    writer.write_thinking_delta("思考分片：" + secret_val[:11])
    writer.write_thinking_delta(secret_val[11:] + " 完成推理。\n")
    writer.write(f"过程说明：{secret_val}\n")
    writer.commit_model_response(
        {
            "schema_version": "committed_model_response.v1",
            "model_call_id": "mc-eff-1",
            "boundary": "final",
            "final_text": f"最终摘要：{secret_val} 正常完成。",
        }
    )
    writer.close()

    chunk_text = writer.chunk_path.read_text(encoding="utf-8")
    _assert_zero_hits(chunk_text, (secret_val,))
    _assert_zero_hits(live_events, (secret_val,))
    assert "[REDACTED]" in chunk_text
    # loop_params 已回收且 writer 已完成最终 flush/close，托管回合自动关闭并清理日志表。
    assert turn_red.active is False

    # 同时携带 effective_on_chunk 与 on_chunk 时按对象 id 去重，两个不同 sink 则全部绑定。
    bind_counts = {"a": 0, "b": 0}
    sink_a = SimpleNamespace(
        conversation_thread_id="thr-dedup",
        bind_credential_redaction=lambda _tr: bind_counts.__setitem__("a", bind_counts["a"] + 1),
    )
    sink_b = SimpleNamespace(
        bind_credential_redaction=lambda _tr: bind_counts.__setitem__("b", bind_counts["b"] + 1),
    )
    same_sink_params = SimpleNamespace(
        request_id="req-dedup-same",
        effective_on_chunk=sink_a,
        on_chunk=sink_a,
    )
    tr_same = turn_credential_redaction(agent, same_sink_params)
    assert bind_counts["a"] == 1
    assert tr_same.thread_id == "thr-dedup"

    dual_sink_params = SimpleNamespace(
        request_id="req-dedup-dual",
        effective_on_chunk=sink_a,
        on_chunk=sink_b,
    )
    tr_dual = turn_credential_redaction(agent, dual_sink_params)
    assert bind_counts["a"] == 2
    assert bind_counts["b"] == 1
    tr_same.close()
    tr_dual.close()


def test_128_sequential_unhashable_run_params_reclaim_on_gc_preserving_concurrent_long_turn(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path / "seq_gc", "user", full=False)
    owner_dir = str(Path(agent.home_paths.owner_home_dir).resolve(strict=False))

    sample_params = RunParams(request_id="check-unhashable")
    with pytest.raises(TypeError):
        hash(sample_params)

    long_secret = "SYNTHETIC_LONG_RUNNING_TURN_SECRET_9001"
    long_writer = BufferedChunkStreamWriter(
        tmp_path / "long_turn.jsonl",
        delivery_channel="feishu",
        rich_transcript=True,
    )
    long_params = RunParams(
        request_id="req-long-0",
        run_id="run-long-0",
        task_id="task-long-0",
        task_attributes={"conversation_thread_id": "thr-shared"},
        on_chunk=long_writer,
    )
    long_turn = turn_credential_redaction(agent, long_params)
    long_turn.register_values((long_secret,))
    assert long_turn.active is True

    seq_secrets: list[str] = []

    def _run_short_turn(idx: int) -> None:
        short_params = RunParams(
            request_id=f"req-seq-{idx:03d}",
            run_id=f"run-seq-{idx:03d}",
            task_attributes={"conversation_thread_id": "thr-shared"},
        )
        short_turn = turn_credential_redaction(agent, short_params)
        sec = f"SYNTHETIC_SHORT_SEQ_SECRET_{idx:04d}"
        seq_secrets.append(sec)
        short_turn.register_values((sec,))
        assert short_turn.active is True
        assert "[REDACTED]" in short_turn.public_text(f"val={sec}")

    for i in range(128):
        _run_short_turn(i)
        if i % 16 == 15:
            gc.collect()
    gc.collect()

    # 128 个顺序结束的不可哈希 RunParams 均已自动回收，未撑爆 64 槽桶，也未误清并发长回合。
    assert long_turn.active is True
    assert "[REDACTED]" in long_turn.public_text(f"still_protected={long_secret}")
    active_in_thread = active_turn_redactions_for_scope(owner_key=owner_dir, thread_id="thr-shared")
    assert active_in_thread == (long_turn,)
    assert f"{owner_dir}::req-long-0" in log_red_mod._ACTIVE_TURN_LOG_SECRETS
    assert long_secret in log_red_mod._ACTIVE_TURN_LOG_FLAT
    for idx in range(128):
        assert f"{owner_dir}::req-seq-{idx:03d}" not in log_red_mod._ACTIVE_TURN_LOG_SECRETS
    for sec in seq_secrets:
        assert sec not in log_red_mod._ACTIVE_TURN_LOG_FLAT

    long_writer.write(f"长回合尾段：{long_secret}\n")
    long_writer.close()
    _assert_zero_hits(long_writer.chunk_path.read_text(encoding="utf-8"), (long_secret,))
    # long_params 仍存活时，关闭 writer 不会提前关闭 long_turn。
    assert long_turn.active is True

    del long_params
    gc.collect()
    assert long_turn.active is False
    assert active_turn_redactions_for_scope(owner_key=owner_dir, thread_id="thr-shared") == ()
    assert f"{owner_dir}::req-long-0" not in log_red_mod._ACTIVE_TURN_LOG_SECRETS
    assert long_secret not in log_red_mod._ACTIVE_TURN_LOG_FLAT


def test_65_simultaneous_turns_across_threads_preserve_leased_turn_zero_and_log_after_lru_eviction(
    tmp_path: Path,
) -> None:
    install_log_redaction()
    agent = _agent(tmp_path / "concurrent_65", "user", full=False)
    owner_dir = str(Path(agent.home_paths.owner_home_dir).resolve(strict=False))

    live_params: list[RunParams] = []
    live_turns: list[TurnCredentialRedaction] = []
    secrets_by_idx: list[str] = []

    total_concurrent = _MAX_ACTIVE_TURNS_PER_SCOPE_COUNT + 1  # 65
    for idx in range(total_concurrent):
        p = RunParams(
            request_id=f"turn-{idx}",
            run_id=f"run-{idx}",
            task_attributes={"conversation_thread_id": f"thr-{idx}"},
        )
        tr = turn_credential_redaction(agent, p)
        sec = f"SYNTHETIC_CONCURRENT_65_SECRET_{idx:04d}"
        tr.register_values((sec,))
        live_params.append(p)
        live_turns.append(tr)
        secrets_by_idx.append(sec)

    turn_0 = live_turns[0]
    secret_0 = secrets_by_idx[0]
    try:
        # 所有 65 个并发活跃回合均保持 active == True。
        assert all(t.active for t in live_turns)
        assert turn_0.active is True

        # 在独立工作线程验证早期回合投影、增量补充注册与按线程精确检索。
        worker_check: dict[str, object] = {}

        def _verify_on_worker() -> None:
            worker_check["public"] = turn_0.public_text(f"body={secret_0}")
            worker_check["lookup"] = active_turn_redactions_for_scope(
                owner_key=owner_dir,
                thread_id="thr-0",
            )
            extra_sec = "SYNTHETIC_TURN0_EXTRA_SECRET_7700"
            turn_0.register_values((extra_sec,))
            worker_check["extra_public"] = turn_0.public_text(f"extra={extra_sec}")

        worker = threading.Thread(target=_verify_on_worker)
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert worker_check["public"] == "body=[REDACTED]"
        assert worker_check["extra_public"] == "extra=[REDACTED]"
        assert worker_check["lookup"] == (turn_0,)

        # turn-0 虽从 owner 通配桶溢出，但仍驻留在 (owner, 'thr-0') 分桶中，非真正孤儿，日志保护必须保留。
        assert f"{owner_dir}::turn-0" in log_red_mod._ACTIVE_TURN_LOG_SECRETS
        assert secret_0 in log_red_mod._ACTIVE_TURN_LOG_FLAT
        rec = logging.getLogger("test.r2.turn0").makeRecord(
            "test.r2.turn0", logging.INFO, __file__, 1, "diag=%s", (secret_0,), None
        )
        assert secret_0 not in rec.getMessage()
        assert "[REDACTED]" in rec.getMessage()

        # 再向同一线程 thr-0 与同一 conv_root 填入 64 个新回合，使 turn-0 从其全部 _scope_keys() LRU 分桶溢出。
        thr0_extra_params: list[RunParams] = []
        thr0_extra_turns: list[TurnCredentialRedaction] = []
        for extra_idx in range(_MAX_ACTIVE_TURNS_PER_SCOPE_COUNT):
            ep = RunParams(
                request_id=f"thr0-overflow-{extra_idx}",
                task_attributes={"conversation_thread_id": "thr-0"},
            )
            etr = turn_credential_redaction(agent, ep)
            etr.register_values((f"SYNTHETIC_THR0_OVERFLOW_SEC_{extra_idx:04d}",))
            thr0_extra_params.append(ep)
            thr0_extra_turns.append(etr)

        try:
            # turn_0 的 RunParams 仍在 live_params 中持有租约：LRU 溢出只挤出缓存，租约索引保留精确检索与日志变体。
            with hor_mod._ACTIVE_TURN_SCOPES_LOCK:
                assert not any(turn_0 in bucket.values() for bucket in hor_mod._ACTIVE_TURN_SCOPES.values())
            assert f"{owner_dir}::turn-0" in log_red_mod._ACTIVE_TURN_LOG_SECRETS
            assert secret_0 in log_red_mod._ACTIVE_TURN_LOG_FLAT
            assert turn_0.active is True
            assert turn_0.public_text(f"live_handle={secret_0}") == "live_handle=[REDACTED]"
            assert active_turn_redactions_for_scope(
                owner_key=owner_dir, thread_id="thr-0", turn_keys=("turn-0",), exact_only=True
            ) == (turn_0,)
        finally:
            thr0_extra_params.clear()
            for etr in thr0_extra_turns:
                etr.close()
    finally:
        live_params.clear()
        for tr in live_turns:
            tr.close()
        gc.collect()

    assert not any(k.startswith(f"{owner_dir}::") for k in log_red_mod._ACTIVE_TURN_LOG_SECRETS)
    with hor_mod._ACTIVE_TURN_SCOPES_LOCK:
        assert not any(scope_k[0] == f"owner:{owner_dir}" for scope_k in hor_mod._ACTIVE_TURN_SCOPES)
        assert not any(scope_k[0] == f"owner:{owner_dir}" for scope_k in hor_mod._LEASED_ACTIVE_TURNS)


def test_outer_turn_lifetime_owner_survives_inner_run_params_replace_copy_gc_and_closes_on_owner_gc(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path / "bg_owner", "user", full=False)
    owner_dir = str(Path(agent.home_paths.owner_home_dir).resolve(strict=False))
    bg_secret = "SYNTHETIC_BG_LIFETIME_OWNER_SECRET_3141"

    outer_request = BackgroundRunRequest(
        thread_id="thr-bg-lifetime",
        task_id="task-bg-lifetime",
        wake_signal={"kind": "credential_ready", "generation": 1},
    )
    # 含 dict 字段的 frozen dataclass 同样不可哈希，验证 _TurnHolderBox 兼容它。
    with pytest.raises(TypeError):
        hash(outer_request)

    class _MockBackgroundSink:
        def __init__(self, lifetime_owner: object, recorder: list[object]) -> None:
            self._turn_lifetime_owner = lifetime_owner
            self._recorder = recorder

        def bind_credential_redaction(self, turn_redaction: object) -> None:
            self._recorder.append(turn_redaction)

    def _run_inner_background_slice(req: BackgroundRunRequest):
        bound_refs: list[object] = []
        sink = _MockBackgroundSink(req, bound_refs)
        outer_params = RunParams(
            request_id="runreq-bg-attempt-1",
            run_id="bg-run-1",
            task_id=req.task_id,
            conversation_turn_id=req.conversation_turn_id,
            task_attributes={"conversation_thread_id": req.thread_id},
            on_chunk=sink,
        )
        tr_main = turn_credential_redaction(agent, outer_params)
        tr_main.register_values((bg_secret,))

        inner_replaced = replace(outer_params, compact_auto_continue_depth=1)
        inner_copied = copy.copy(outer_params)
        tr_replaced = turn_credential_redaction(agent, inner_replaced)
        tr_copied = turn_credential_redaction(agent, inner_copied)
        assert tr_replaced is tr_main
        assert tr_copied is tr_main
        assert bound_refs and bound_refs[0] is tr_main

        replaced_ref = weakref.ref(inner_replaced)
        copied_ref = weakref.ref(inner_copied)
        del inner_replaced, inner_copied
        gc.collect()
        assert replaced_ref() is None
        assert copied_ref() is None
        assert tr_main.active is True

        return tr_main, weakref.ref(outer_params), weakref.ref(sink)

    turn_red, outer_params_ref, sink_ref = _run_inner_background_slice(outer_request)
    gc.collect()
    assert outer_params_ref() is None
    assert sink_ref() is None

    # 内层 RunParams 和 sink 均已 GC，但外层 BackgroundRunRequest 仍存活，投递阶段必须继续命中脱敏对象。
    assert turn_red.active is True
    assert turn_red.public_text(f"deliver={bg_secret}") == "deliver=[REDACTED]"
    matched = active_turn_redactions_for_scope(
        owner_key=owner_dir,
        thread_id=outer_request.thread_id,
        turn_keys=(outer_request.conversation_turn_id, outer_request.task_id),
    )
    assert matched == (turn_red,)
    assert bg_secret in log_red_mod._ACTIVE_TURN_LOG_FLAT

    # 外层 BackgroundRunRequest 投递完毕释放后，自动关闭并清理全部索引与日志密钥。
    del outer_request
    gc.collect()
    assert turn_red.active is False
    assert (
        active_turn_redactions_for_scope(
            owner_key=owner_dir,
            thread_id="thr-bg-lifetime",
            turn_keys=("task-bg-lifetime",),
        )
        == ()
    )
    assert bg_secret not in log_red_mod._ACTIVE_TURN_LOG_FLAT


def test_atomic_register_values_and_try_register_exact_and_plus_one_count_and_byte_boundaries() -> None:
    # 1. 数量边界（256 项）：try_register / try_register_values 超限返回 None、零部分写入、不污染既有回合。
    tr_count = TurnCredentialRedaction(owner_key="cap-count-owner", thread_id="thr-cap-1", turn_key="turn-cap-count")
    try:
        initial_254 = tuple(f"SYNTH_CNT_VAL_{i:03d}" for i in range(_MAX_TURN_RAW_VALUES_COUNT - 2))
        res_254 = tr_count.try_register_values(initial_254)
        assert isinstance(res_254, HostOutputRedaction)
        assert len(tr_count._raw_values) == 254
        assert tr_count.withheld is False

        # 254 + 3 = 257 > 256：整批原子拒绝，不追加前 2 项，也不置 withheld=True。
        over_batch = ("SYNTH_CNT_OVER_A", "SYNTH_CNT_OVER_B", "SYNTH_CNT_OVER_C")
        assert tr_count.try_register_values(over_batch) is None
        assert len(tr_count._raw_values) == 254
        assert "SYNTH_CNT_OVER_A" not in tr_count._raw_values
        assert tr_count.withheld is False
        assert tr_count.public_text("check SYNTH_CNT_VAL_000") == "check [REDACTED]"

        # 精确补齐至 256 项：成功。
        exact_pair = ("SYNTH_CNT_254", "SYNTH_CNT_255")
        res_256 = tr_count.try_register_values(exact_pair)
        assert isinstance(res_256, HostOutputRedaction)
        assert len(tr_count._raw_values) == _MAX_TURN_RAW_VALUES_COUNT
        assert tr_count.withheld is False

        # 已登记重复值在满额 256/256 下幂等重验仍成功（新增数量与字节均为 0）。
        dup_mat = CredentialMaterial(
            credential_ref="cred_" + "b" * 32,
            generation=1,
            origin="https://cap.example.test",
            target_url="https://cap.example.test",
            values={"token": "SYNTH_CNT_255"},
        )
        assert isinstance(tr_count.try_register(dup_mat), HostOutputRedaction)

        # +1 项（第 257 个新值）通过 try_register 预检：返回 None 且不置 withheld。
        plus_one_mat = CredentialMaterial(
            credential_ref="cred_" + "c" * 32,
            generation=1,
            origin="https://cap.example.test",
            target_url="https://cap.example.test",
            values={"token": "SYNTH_CNT_256_PLUS_ONE"},
        )
        assert tr_count.try_register(plus_one_mat) is None
        assert len(tr_count._raw_values) == _MAX_TURN_RAW_VALUES_COUNT
        assert tr_count.withheld is False

        # register_values 跨上限批次（先退到新实例 254 项 + 3 项）：不部分写入，且置 withheld=True。
        tr_reg_count = TurnCredentialRedaction(
            owner_key="cap-count-owner", thread_id="thr-cap-2", turn_key="turn-reg-count"
        )
        try:
            tr_reg_count.register_values(initial_254)
            ver_before = tr_reg_count._version
            tr_reg_count.register_values(over_batch)
            assert tr_reg_count.withheld is True
            assert tr_reg_count._version > ver_before
            assert len(tr_reg_count._raw_values) == 254
            assert "SYNTH_CNT_OVER_A" not in tr_reg_count._raw_values
            assert tr_reg_count.public_text("normal text") == "[内容因凭据脱敏额度超限已扣留]"
            assert tr_reg_count.try_register_values(("ANY_AFTER_WITHHELD",)) is None
        finally:
            tr_reg_count.close()
    finally:
        tr_count.close()
    assert tr_count.try_register_values(("AFTER_CLOSE",)) is None

    # 2. UTF-8 字节边界（512 KiB = 524,288 字节，含多字节 UTF-8 字符）：精确上限与 +1 字节原子检查。
    tr_bytes = TurnCredentialRedaction(owner_key="cap-byte-owner", thread_id="thr-byte-1", turn_key="turn-cap-bytes")
    try:
        # 63 个 8192 字节块 + 1 个多字节 UTF-8 块（2730 * 3 + 2 = 8192 字节）= 524,288 字节。
        chunks_63 = [f"{i:04d}_" + ("A" * (8192 - 5)) for i in range(63)]
        multibyte_last = ("密" * 2730) + "Z9"
        assert len(multibyte_last.encode("utf-8")) == 8192
        total_exact = (*chunks_63, multibyte_last)
        assert sum(len(item.encode("utf-8")) for item in total_exact) == _MAX_TURN_RAW_BYTES

        res_exact_bytes = tr_bytes.try_register_values(total_exact)
        assert isinstance(res_exact_bytes, HostOutputRedaction)
        assert tr_bytes._raw_bytes == _MAX_TURN_RAW_BYTES
        assert tr_bytes.withheld is False

        # +1 字节：try_register_values 返回 None，不修改 _raw_bytes 且不置 withheld。
        assert tr_bytes.try_register_values(("X",)) is None
        assert tr_bytes._raw_bytes == _MAX_TURN_RAW_BYTES
        assert tr_bytes.withheld is False
    finally:
        tr_bytes.close()

    # 3. register_values 在距离 512 KiB 差 8 字节处接收 ("1234", "56789")（共 9 字节）：零部分追加，置 withheld=True。
    tr_reg_bytes = TurnCredentialRedaction(
        owner_key="cap-byte-owner", thread_id="thr-byte-2", turn_key="turn-reg-bytes"
    )
    try:
        chunks_near = [f"{i:04d}_" + ("B" * (8192 - 5)) for i in range(63)]
        tail_8184 = "T" * (8192 - 8)
        tr_reg_bytes.register_values((*chunks_near, tail_8184))
        assert tr_reg_bytes._raw_bytes == _MAX_TURN_RAW_BYTES - 8
        assert tr_reg_bytes.withheld is False

        tr_reg_bytes.register_values(("1234", "56789"))
        assert tr_reg_bytes.withheld is True
        assert tr_reg_bytes._raw_bytes == _MAX_TURN_RAW_BYTES - 8
        assert "1234" not in tr_reg_bytes._raw_values
    finally:
        tr_reg_bytes.close()


def test_scope_count_1024_eviction_and_zero_holder_direct_writer_close_invariants(tmp_path: Path) -> None:
    # 1. 直接构造的 0-holder TurnCredentialRedaction 绑定到 BufferedChunkStreamWriter 后，writer.close() 不自动关闭它。
    direct_turn = TurnCredentialRedaction(owner_key="direct-owner", thread_id="thr-direct", turn_key="turn-direct-0")
    direct_sec = "SYNTHETIC_DIRECT_ZERO_HOLDER_SEC_4040"
    direct_turn.register_values((direct_sec,))
    direct_writer = BufferedChunkStreamWriter(tmp_path / "direct.jsonl", delivery_channel="feishu")
    try:
        direct_writer.bind_credential_redaction(direct_turn)
        direct_writer.write(f"msg={direct_sec}\n")
        direct_writer.close()
        _assert_zero_hits(direct_writer.chunk_path.read_text(encoding="utf-8"), (direct_sec,))
        assert direct_turn.active is True
        assert direct_turn.public_text(f"after_writer_close={direct_sec}") == "after_writer_close=[REDACTED]"
    finally:
        direct_turn.close()
    assert direct_turn.active is False

    # 2. 绑定方法 effective_on_chunk = writer.write_model 也能通过 __self__ 自动绑定底层 BufferedChunkStreamWriter。
    agent = _agent(tmp_path / "bound_method", "user", full=False)
    method_writer = BufferedChunkStreamWriter(tmp_path / "method.jsonl", delivery_channel="feishu")
    method_writer.bind_conversation_identity("thr-bound-method")
    method_params = _make_tool_loop_params(
        effective_on_chunk=method_writer.write_model,
        request_id="req-method-1",
    )
    method_turn = turn_credential_redaction(agent, method_params)
    assert method_writer._turn_credential_redaction is method_turn
    assert method_turn.thread_id == "thr-bound-method"
    method_writer.close()
    method_turn.close()

    # 3. 进程级作用域上限（1024 个独立作用域）溢出淘汰只卸索引与孤儿日志，绝不清空仍在运行的活跃回合句柄。
    scope_turns: list[TurnCredentialRedaction] = []
    first_scope_sec = "SYNTHETIC_SCOPE_CAP_FIRST_SEC_0000"
    try:
        for idx in range(_MAX_ACTIVE_SCOPES_COUNT + 4):
            tr = TurnCredentialRedaction(owner_key=f"scope-owner-{idx}", thread_id="", turn_key=f"sc-turn-{idx}")
            tr.register_values((first_scope_sec if idx == 0 else f"SYNTHETIC_SCOPE_SEC_{idx:04d}",))
            scope_turns.append(tr)
        assert scope_turns[0].active is True
        assert scope_turns[0].public_text(f"echo={first_scope_sec}") == "echo=[REDACTED]"
        assert "scope-owner-0::sc-turn-0" not in log_red_mod._ACTIVE_TURN_LOG_SECRETS
        assert first_scope_sec not in log_red_mod._ACTIVE_TURN_LOG_FLAT
    finally:
        for tr in scope_turns:
            tr.close()


def test_257_concurrent_leased_owners_keep_owner_zero_message_store_and_worker_log_redaction(
    tmp_path: Path,
) -> None:
    install_log_redaction()
    # 每个 owner 的回合占 4 个作用域键；257 × 4 = 1028 > 1024，owner #0 的全部 LRU 分桶都会被淘汰。
    owner_count = _MAX_ACTIVE_SCOPES_COUNT // 4 + 1
    store_0 = ConversationStore(tmp_path / "conv-0")
    thread_0 = store_0.threads.get_or_create({"canonical_user_id": "owner-leased-0"})
    secret_0 = "SYNTHETIC_LEASED_OWNER_ZERO_SECRET_2570"

    def _open_leased_turns() -> tuple[list[RunParams], list[TurnCredentialRedaction]]:
        params_list: list[RunParams] = []
        turns: list[TurnCredentialRedaction] = []
        for idx in range(owner_count):
            store = store_0 if idx == 0 else SimpleNamespace(storage=SimpleNamespace(root=tmp_path / f"conv-{idx}"))
            agent = SimpleNamespace(
                home_paths=SimpleNamespace(owner_home_dir=str(tmp_path / f"owner-{idx}"), owner_id=f"owner-{idx}"),
                conversation_store=store,
            )
            params = RunParams(
                request_id=f"req-leased-{idx}",
                run_id=f"run-leased-{idx}",
                task_attributes={"conversation_thread_id": thread_0.thread_id if idx == 0 else f"thr-leased-{idx}"},
            )
            turn = turn_credential_redaction(agent, params)
            turn.register_values((secret_0 if idx == 0 else f"SYNTHETIC_LEASED_OWNER_SECRET_{idx:04d}",))
            params_list.append(params)
            turns.append(turn)
        return params_list, turns

    # 在独立上下文中登记，避免把 257 组变体绑定进主线程 ContextVar 而影响后续用例。
    live_params, turns = contextvars.copy_context().run(_open_leased_turns)
    turn_0 = turns[0]
    all_scope_keys = {key for turn in turns for key in turn._scope_keys()}
    assert len(turn_0._scope_keys()) == 4
    assert len(all_scope_keys) == owner_count * 4 > _MAX_ACTIVE_SCOPES_COUNT
    worker_check: dict[str, object] = {}

    def _owner_zero_worker() -> None:
        entry = store_0.messages.append({
            "thread_id": thread_0.thread_id,
            "role": "assistant",
            "content": f"回执：{secret_0}",
            "metadata": {"request_id": "req-leased-0"},
        })
        worker_check["content"] = entry.content
        record = logging.getLogger("test.r3.leased_owner_zero").makeRecord(
            "test.r3.leased_owner_zero", logging.INFO, __file__, 1, "diag=%s", (secret_0,), None
        )
        worker_check["log"] = record.getMessage()
        worker_check["lookup"] = active_turn_redactions_for_scope(
            conversation_root=store_0.storage.root,
            thread_id=thread_0.thread_id,
            turn_keys=("req-leased-0",),
            exact_only=True,
        )

    try:
        assert all(turn.active for turn in turns)
        with hor_mod._ACTIVE_TURN_SCOPES_LOCK:
            assert not any(turn_0 in bucket.values() for bucket in hor_mod._ACTIVE_TURN_SCOPES.values())
        worker = threading.Thread(target=_owner_zero_worker)
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive()
        # owner #0 已被挤出全部 LRU 分桶但仍持租约：MessageStore.append 与工作线程 LogRecord 均保持脱敏。
        assert secret_0 not in str(worker_check["content"])
        assert "[REDACTED]" in str(worker_check["content"])
        assert secret_0 not in str(worker_check["log"])
        assert "[REDACTED]" in str(worker_check["log"])
        assert f"{turn_0.owner_key}::req-leased-0" in log_red_mod._ACTIVE_TURN_LOG_SECRETS
        assert worker_check["lookup"] == (turn_0,)
        with hor_mod._ACTIVE_TURN_SCOPES_LOCK:
            for key in turn_0._scope_keys():
                assert hor_mod._LEASED_ACTIVE_TURNS[key][turn_0.turn_key] is turn_0
    finally:
        live_params.clear()
        gc.collect()

    # 宿主租约全部释放并 GC 后：回合自动关闭，租约索引、LRU 与日志表中这些 owner 的登记归零。
    assert not any(turn.active for turn in turns)
    with hor_mod._ACTIVE_TURN_SCOPES_LOCK:
        assert all_scope_keys.isdisjoint(hor_mod._LEASED_ACTIVE_TURNS)
        assert all_scope_keys.isdisjoint(hor_mod._ACTIVE_TURN_SCOPES)
    owner_keys = {turn.owner_key for turn in turns}
    assert not any(key.split("::", 1)[0] in owner_keys for key in log_red_mod._ACTIVE_TURN_LOG_SECRETS)
    assert secret_0 not in log_red_mod._ACTIVE_TURN_LOG_FLAT


def test_exact_only_lookup_returns_empty_for_mismatched_turn_keys_despite_active_neighbors(
    tmp_path: Path,
) -> None:
    conv_root = tmp_path / "conv-exact"
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(tmp_path / "owner-exact"), owner_id="owner-exact"),
        conversation_store=SimpleNamespace(storage=SimpleNamespace(root=conv_root)),
    )
    leased_params = RunParams(
        request_id="req-exact-leased",
        run_id="run-exact-leased",
        task_attributes={"conversation_thread_id": "thr-exact"},
    )
    leased_turn = turn_credential_redaction(agent, leased_params)
    neighbor_turn = TurnCredentialRedaction(
        owner_key=leased_turn.owner_key,
        conversation_root=str(conv_root),
        thread_id="thr-exact",
        turn_key="turn-exact-neighbor",
    )
    try:
        leased_turn.register_values(("SYNTHETIC_EXACT_LEASED_SECRET_6160",))
        neighbor_turn.register_values(("SYNTHETIC_EXACT_NEIGHBOR_SECRET_5150",))
        by_owner = {"owner_key": leased_turn.owner_key, "thread_id": "thr-exact"}
        by_conv = {"conversation_root": conv_root, "thread_id": "thr-exact"}
        # 默认模式保持既有合同：精确 turn_key 未命中时回退到同线程全部活动回合。
        fallback = active_turn_redactions_for_scope(**by_owner, turn_keys=("req-other",))
        assert set(fallback) == {leased_turn, neighbor_turn}
        for scope in (by_owner, by_conv):
            # exact_only：未命中或 turn_keys 为空都返回 ()，绝不回退到同线程仍活动的邻近回合。
            assert active_turn_redactions_for_scope(
                **scope, turn_keys=("req-other", "run-other"), exact_only=True
            ) == ()
            assert active_turn_redactions_for_scope(**scope, exact_only=True) == ()
            assert active_turn_redactions_for_scope(
                **scope, turn_keys=("turn-exact-neighbor",), exact_only=True
            ) == (neighbor_turn,)
            # 租约回合的 run_id 别名同样算精确命中。
            assert active_turn_redactions_for_scope(
                **scope, turn_keys=("run-exact-leased",), exact_only=True
            ) == (leased_turn,)
    finally:
        neighbor_turn.close()
        leased_turn.close()


def test_reverse_order_concurrent_try_register_publishes_latest_union_to_process_log(monkeypatch) -> None:
    turn = TurnCredentialRedaction(owner_key="publish-owner", thread_id="thr-publish", turn_key="turn-publish")
    log_key = "publish-owner::turn-publish"
    older_secret = "SYNTHETIC_PUBLISH_OLDER_SNAPSHOT_A1"
    newer_secret = "SYNTHETIC_PUBLISH_NEWER_SNAPSHOT_B2"
    older_mutated = threading.Event()
    newer_published = threading.Event()
    real_bind = hor_mod.bind_active_credential_log_values
    results: dict[str, object] = {}

    def _hold_older_publisher(values: tuple[str, ...]) -> None:
        # 旧快照已写入回合状态、尚未发布到进程日志表时停住，让新快照先完成发布（逆序发布）。
        if threading.current_thread().name == "older-publisher":
            older_mutated.set()
            assert newer_published.wait(timeout=10)
        real_bind(values)

    def _register(name: str, secret: str) -> None:
        results[name] = turn.try_register_values((secret,))

    monkeypatch.setattr(hor_mod, "bind_active_credential_log_values", _hold_older_publisher)
    older = threading.Thread(target=_register, args=("older", older_secret), name="older-publisher")
    newer = threading.Thread(target=_register, args=("newer", newer_secret), name="newer-publisher")
    try:
        older.start()
        assert older_mutated.wait(timeout=10)
        newer.start()
        newer.join(timeout=10)
        assert not newer.is_alive()
        newer_published.set()
        older.join(timeout=10)
        assert not older.is_alive()
        assert isinstance(results.get("older"), HostOutputRedaction)
        assert isinstance(results.get("newer"), HostOutputRedaction)
        # 迟到的旧快照发布者必须发布最新单调并集，不能把新快照覆盖回旧值。
        published = log_red_mod._ACTIVE_TURN_LOG_SECRETS[log_key]
        assert older_secret in published
        assert newer_secret in published
        assert {older_secret, newer_secret} <= set(log_red_mod._ACTIVE_TURN_LOG_FLAT)
    finally:
        newer_published.set()
        turn.close()
    assert log_key not in log_red_mod._ACTIVE_TURN_LOG_SECRETS


def test_delayed_publisher_after_close_cannot_resurrect_log_or_scope_registration(monkeypatch) -> None:
    turn = TurnCredentialRedaction(owner_key="resurrect-owner", thread_id="thr-resurrect", turn_key="turn-resurrect")
    log_key = "resurrect-owner::turn-resurrect"
    late_secret = "SYNTHETIC_LATE_PUBLISHER_SECRET_C3"
    late_mutated = threading.Event()
    turn_closed = threading.Event()
    real_bind = hor_mod.bind_active_credential_log_values
    results: dict[str, object] = {}

    def _hold_late_publisher(values: tuple[str, ...]) -> None:
        if threading.current_thread().name == "late-publisher":
            late_mutated.set()
            assert turn_closed.wait(timeout=10)
        real_bind(values)

    def _late_register() -> None:
        results["late"] = turn.try_register_values((late_secret,))

    monkeypatch.setattr(hor_mod, "bind_active_credential_log_values", _hold_late_publisher)
    late = threading.Thread(target=_late_register, name="late-publisher")
    try:
        late.start()
        assert late_mutated.wait(timeout=10)
        turn.close()
    finally:
        turn_closed.set()
    late.join(timeout=10)
    assert not late.is_alive()
    assert isinstance(results.get("late"), HostOutputRedaction)
    # close() 之后才轮到的发布者读到 closed 快照，既不复活日志变体，也不复活作用域索引。
    assert turn.active is False
    assert log_key not in log_red_mod._ACTIVE_TURN_LOG_SECRETS
    assert late_secret not in log_red_mod._ACTIVE_TURN_LOG_FLAT
    with hor_mod._ACTIVE_TURN_SCOPES_LOCK:
        for index in (hor_mod._ACTIVE_TURN_SCOPES, hor_mod._LEASED_ACTIVE_TURNS):
            assert not any(turn in bucket.values() for bucket in index.values())


def test_first_lease_on_active_direct_turn_promotes_into_leased_index_and_republishes_log() -> None:
    owner = "promote-owner"
    promoted = TurnCredentialRedaction(owner_key=owner, thread_id="thr-promote", turn_key="turn-promote-0")
    promoted_secret = "SYNTHETIC_PROMOTED_TURN_SECRET_8080"
    fillers: list[TurnCredentialRedaction] = []
    try:
        promoted.register_values((promoted_secret,))
        # 0 持有者的直接实例照旧受 LRU 约束：同作用域再塞 64 个回合后被淘汰，日志变体一并卸载。
        for idx in range(_MAX_ACTIVE_TURNS_PER_SCOPE_COUNT):
            filler = TurnCredentialRedaction(owner_key=owner, thread_id="thr-promote", turn_key=f"turn-fill-{idx}")
            filler.register_values((f"SYNTHETIC_PROMOTE_FILLER_SECRET_{idx:04d}",))
            fillers.append(filler)
        assert f"{owner}::turn-promote-0" not in log_red_mod._ACTIVE_TURN_LOG_SECRETS

        holder = RunParams(request_id="turn-promote-0")
        assert promoted._attach_lifetime_holder(holder) is not None
        # 首个租约挂到已活动回合：晋升进租约索引并按最新快照重新发布日志变体。
        assert f"{owner}::turn-promote-0" in log_red_mod._ACTIVE_TURN_LOG_SECRETS
        assert promoted_secret in log_red_mod._ACTIVE_TURN_LOG_FLAT
        with hor_mod._ACTIVE_TURN_SCOPES_LOCK:
            for key in promoted._scope_keys():
                assert hor_mod._LEASED_ACTIVE_TURNS[key]["turn-promote-0"] is promoted

        del holder
        gc.collect()
        assert promoted.active is False
        assert f"{owner}::turn-promote-0" not in log_red_mod._ACTIVE_TURN_LOG_SECRETS
        with hor_mod._ACTIVE_TURN_SCOPES_LOCK:
            assert not any(promoted in bucket.values() for bucket in hor_mod._LEASED_ACTIVE_TURNS.values())
    finally:
        promoted.close()
        for filler in fillers:
            filler.close()
