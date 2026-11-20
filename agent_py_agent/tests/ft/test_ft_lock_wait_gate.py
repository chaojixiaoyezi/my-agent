# LLM: Conveyor M00 / R3-G1（C2.1）：自由线程锁等待门禁。3.14t 在 GIL 关闭时，类型缓存未命中会去抢全局类型锁，
#   128 线程下吞吐塌到 16 线程的约 0.15（R3 §2.4）。本门禁在独立子进程里导入真实模块，按 CASES 逐个用例各跑
#   16 线程与 128 线程两档、每档共 3 秒计时：计时切成 3 个 16→128→128→16 块（每片 0.5 秒），取各块吞吐比的中位数，
#   要求它 ≥ 0.8。共享主机的负载漂移与短时尖峰在相邻片上近似相同，被块内对称排列与块间中位数抵消。
#   GIL 开启（含 3.11、3.12、3.14 默认构建）时跳过。
#   CASES 只追加不删除：每个 Cure-A 模块把它转换的类型以“零参可调用工厂”登记进来。
# 模块用途: 提供 G1 门禁测试与其子进程驱动（python -X gil=0 -m agent_py_agent.tests.ft.test_ft_lock_wait_gate <工厂>）。
from __future__ import annotations

import argparse
import importlib
import json
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

# G1 通过线：128 线程吞吐至少为 16 线程吞吐的 0.8 倍（R3 §8；类型锁护航下实测约 0.15，余量充足）。
G1_MIN_THROUGHPUT_RATIO = 0.8
# 每个线程数档位的计时总秒数（R3 §8：3 秒），均分到 G1_BLOCK_COUNT 个块里该档的各片。
G1_WINDOW_SECONDS = 3.0
# 基准档线程数。
G1_BASE_THREADS = 16
# 压力档线程数（线上 gateway_http_max_request_workers=128 的验收配置）。
G1_STRESS_THREADS = 128
# 一个计时块内各片的线程数顺序（ABBA）：块内两档的平均时刻相同，负载线性漂移对两档影响相同。
G1_SLICE_ORDER = (G1_BASE_THREADS, G1_STRESS_THREADS, G1_STRESS_THREADS, G1_BASE_THREADS)
# 计时块数：判定取各块吞吐比的中位数。共享主机（负载 40–100、18 核）上顺序测两个 3 秒窗时，同一用例比值在
#   0.38–1.74 间摆动；3 块中位数下同一用例稳定在 1.04–1.81，元组键字典的真实争用稳定在 0.21–0.23。
G1_BLOCK_COUNT = 3
# 每个线程进入计时前的热身调用次数，让特化字节码与对象分配进入稳态。
G1_WARMUP_CALLS = 200
# 子进程驱动的超时秒数：两档各 3 秒计时（12 片）加建类、建线程开销，仍在 CI 每测 60 秒预算内。
G1_SUBPROCESS_TIMEOUT_SECONDS = 50
# 驱动进程需要的最小打开文件数：128 对 socketpair 加解释器自身所需。
G1_MIN_OPEN_FILES = 1024

# (工厂点分路径, 标签)：工厂每次调用返回一个零参可调用（一次调用 = 一个操作），可选带 close() 供驱动收尾。
CASES: list[tuple[str, str]] = [
    ('agent_py_agent.tests.test_conveyor_m00_hot_json.g1_case_hot_json', 'hot_json'),
    ('agent_py_agent.tests.test_conveyor_m00_fast_time.g1_case_fast_time', 'fast_time'),
    ('agent_py_agent.tests.test_conveyor_m00_fast_dataclass.g1_case_fast_frozen_dto', 'fast_frozen_dto'),
    ('agent_py_agent.tests.test_conveyor_m00_direct_socket_io.g1_case_direct_socket_io', 'direct_socket_io'),
    ('agent_py_agent.tests.test_conveyor_m05_cure_a_http.g1_case_conversation_control_command', 'm05_conversation_control_command'),
    ('agent_py_agent.tests.test_conveyor_m05_cure_a_http.g1_case_conversation_task_status', 'm05_conversation_task_status'),
    ('agent_py_agent.tests.test_conveyor_m05_cure_a_http.g1_case_named_conversation_work_status', 'm05_named_conversation_work_status'),
    ('agent_py_agent.tests.test_conveyor_m05_cure_a_http.g1_case_conversation_control_result', 'm05_conversation_control_result'),
    ('agent_py_agent.tests.test_conveyor_m05_cure_a_http.g1_case_gateway_client_credentials', 'm05_gateway_client_credentials'),
    ('agent_py_agent.tests.test_conveyor_m05_cure_a_http.g1_case_gateway_client_memory_result', 'm05_gateway_client_memory_result'),
    ('agent_py_agent.tests.test_conveyor_m05_cure_a_http.g1_case_gateway_client_history_result', 'm05_gateway_client_history_result'),
    ('agent_py_agent.tests.test_conveyor_m05_cure_a_http.g1_case_coordinator_input_request', 'm05_coordinator_input_request'),
    ('agent_py_agent.tests.test_conveyor_m05_cure_a_http.g1_case_request_event_gap', 'm05_request_event_gap'),
    ('agent_py_agent.tests.test_conveyor_m05_cure_a_http.g1_case_request_event', 'm05_request_event'),
    ('agent_py_agent.tests.test_conveyor_m05_cure_a_http.g1_case_durable_cursor', 'm05_durable_cursor'),
    ('agent_py_agent.tests.test_conveyor_m05_cure_a_http.g1_case_gateway_running_report', 'm05_gateway_running_report'),
    ('agent_py_agent.tests.test_conveyor_m05_cure_a_http.g1_case_gateway_heartbeat_facts', 'm05_gateway_heartbeat_facts'),
    ('agent_py_agent.tests.test_conveyor_m05_cure_a_http.g1_case_gateway_status_render_context', 'm05_gateway_status_render_context'),
    ('agent_py_agent.tests.test_conveyor_m05_cure_a_http.g1_case_gateway_readiness', 'm05_gateway_readiness'),
    ('agent_py_agent.tests.test_conveyor_m05_cure_a_http.g1_case_gateway_readiness_wait', 'm05_gateway_readiness_wait'),
    ('agent_py_agent.tests.test_conveyor_m05_cure_a_http.g1_case_gateway_record_facts', 'm05_gateway_record_facts'),
    ('agent_py_agent.tests.test_conveyor_m05_cure_a_http.g1_case_gateway_execution_target', 'm05_gateway_execution_target'),
    ('agent_py_agent.tests.test_conveyor_m06_direct_reader.g1_case_backend_options', 'm06_backend_options'),
    ('agent_py_agent.tests.test_conveyor_m06_direct_reader.g1_case_provider_request_options', 'm06_provider_request_options'),
    ('agent_py_agent.tests.test_conveyor_m06_direct_reader.g1_case_gateway_request', 'm06_gateway_request'),
    ('agent_py_agent.tests.test_conveyor_m06_direct_reader.g1_case_split_timeout_options', 'm06_split_timeout_options'),
    ('agent_py_agent.tests.test_conveyor_m06_direct_reader.g1_case_keepalive_route', 'm06_keepalive_route'),
    ('agent_py_agent.tests.test_conveyor_m06_direct_reader.g1_case_keepalive_target', 'm06_keepalive_target'),
    ('agent_py_agent.tests.test_conveyor_m06_direct_reader.g1_case_keepalive_lease', 'm06_keepalive_lease'),
    ('agent_py_agent.tests.test_conveyor_m06_direct_reader.g1_case_owner_change_batch', 'm06_owner_change_batch'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_tool_protocol_violation', 'm07_tool_protocol_violation'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_provider_tool_call_result', 'm07_provider_tool_call_result'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_stream_event', 'm07_stream_event'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_stream_completion', 'm07_stream_completion'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_scanned_text_block', 'm07_scanned_text_block'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_text_block_scan', 'm07_text_block_scan'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_assistant_turn', 'm07_assistant_turn'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_compaction_summary', 'm07_compaction_summary'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_runtime_facts_turn', 'm07_runtime_facts_turn'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_user_turn', 'm07_user_turn'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_decision_answer', 'm07_decision_answer'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_decision_response', 'm07_decision_response'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_prepared_decision_call', 'm07_prepared_decision_call'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_reasoning_level_rule', 'm07_reasoning_level_rule'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_reasoning_payload_limits', 'm07_reasoning_payload_limits'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_anthropic_budget_decision', 'm07_anthropic_budget_decision'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_vision_capability', 'm07_vision_capability'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_model_call_ledger_options', 'm07_model_call_ledger_options'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_model_call_ledger_context', 'm07_model_call_ledger_context'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_model_call_started_params', 'm07_model_call_started_params'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_model_call_first_token_params', 'm07_model_call_first_token_params'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_model_call_activity_params', 'm07_model_call_activity_params'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_model_call_finish_params', 'm07_model_call_finish_params'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_model_call_timeout_params', 'm07_model_call_timeout_params'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_model_call_failure_params', 'm07_model_call_failure_params'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_model_call_provider_attempt_params', 'm07_model_call_provider_attempt_params'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_model_call_record', 'm07_model_call_record'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_runner_model_stage_trace_request', 'm07_runner_model_stage_trace_request'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_runner_model_stream_activity_trace_request', 'm07_runner_model_stream_activity_trace_request'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_runner_provider_retry_trace_request', 'm07_runner_provider_retry_trace_request'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_runner_tool_stage_trace_request', 'm07_runner_tool_stage_trace_request'),
    ('agent_py_agent.tests.test_conveyor_m07_cure_a_model_dtos.g1_case_model_call_usage_summary', 'm07_model_call_usage_summary'),
    ('agent_py_agent.tests.test_conveyor_m08_cure_a_providers.g1_case_anthropic_generate_request', 'anthropic_generate_request'),
    ('agent_py_agent.tests.test_conveyor_m08_cure_a_providers.g1_case_anthropic_stream_callbacks', 'anthropic_stream_callbacks'),
    ('agent_py_agent.tests.test_conveyor_m08_cure_a_providers.g1_case_anthropic_cache_projection', 'anthropic_cache_projection'),
    ('agent_py_agent.tests.test_conveyor_m08_cure_a_providers.g1_case_openai_generate_request', 'openai_generate_request'),
    ('agent_py_agent.tests.test_conveyor_m08_cure_a_providers.g1_case_openai_stream_observers', 'openai_stream_observers'),
    ('agent_py_agent.tests.test_conveyor_m08_cure_a_providers.g1_case_provider_model_metadata', 'provider_model_metadata'),
    ('agent_py_agent.tests.test_conveyor_m08_cure_a_providers.g1_case_provider_metadata_options', 'provider_metadata_options'),
    ('agent_py_agent.tests.test_conveyor_m08_cure_a_providers.g1_case_request_token_budget', 'request_token_budget'),
    ('agent_py_agent.tests.test_conveyor_m08_cure_a_providers.g1_case_provider_send_attempt', 'provider_send_attempt'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_trusted_parameter_binding', 'm09_trusted_parameter_binding'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_tool_model_hints', 'm09_tool_model_hints'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_tool_model_spec', 'm09_tool_model_spec'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_effect_resolver_policy', 'm09_effect_resolver_policy'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_approval_policy', 'm09_approval_policy'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_sandbox_policy', 'm09_sandbox_policy'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_idempotency_policy', 'm09_idempotency_policy'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_timeout_policy', 'm09_timeout_policy'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_concurrency_policy', 'm09_concurrency_policy'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_resource_scope_policy', 'm09_resource_scope_policy'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_output_policy', 'm09_output_policy'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_availability_policy', 'm09_availability_policy'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_tool_runtime_policy', 'm09_tool_runtime_policy'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_tool_runtime_snapshot', 'm09_tool_runtime_snapshot'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_tool_choice', 'm09_tool_choice'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_provider_tool_capability', 'm09_provider_tool_capability'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_tool_protocol_snapshot', 'm09_tool_protocol_snapshot'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_tool_call', 'm09_tool_call'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_tool_content_block', 'm09_tool_content_block'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_tool_result_ref', 'm09_tool_result_ref'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_applied_tool_approval', 'm09_applied_tool_approval'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_tool_failure_facts', 'm09_tool_failure_facts'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_tool_result', 'm09_tool_result'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_tool_input_source', 'm09_tool_input_source'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_tool_input_completion_context', 'm09_tool_input_completion_context'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_tool_input_completion', 'm09_tool_input_completion'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_tool_operation_execution_request', 'm09_tool_operation_execution_request'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_tool_operation_claim_attempt', 'm09_tool_operation_claim_attempt'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_tool_approval_request', 'm09_tool_approval_request'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_tool_approval_decision', 'm09_tool_approval_decision'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_tool_call_derived_identity', 'm09_tool_call_derived_identity'),
    ('agent_py_agent.tests.test_conveyor_m09_cure_a_tool_dtos.g1_tool_result_production_metadata', 'm09_tool_result_production_metadata'),
    ('agent_py_agent.tests.test_conveyor_m10_cure_a_tool_loop.g1_case_tool_call_record_params', 'm10_tool_call_record_params'),
    ('agent_py_agent.tests.test_conveyor_m10_cure_a_tool_loop.g1_case_tool_call_execute_params', 'm10_tool_call_execute_params'),
    ('agent_py_agent.tests.test_conveyor_m10_cure_a_tool_loop.g1_case_tool_round_execution_request', 'm10_tool_round_execution_request'),
    ('agent_py_agent.tests.test_conveyor_m10_cure_a_tool_loop.g1_case_tool_progress_event', 'm10_tool_progress_event'),
    ('agent_py_agent.tests.test_conveyor_m10_cure_a_tool_loop.g1_case_parallel_thread_context', 'm10_parallel_thread_context'),
    ('agent_py_agent.tests.test_conveyor_m10_cure_a_tool_loop.g1_case_tool_loop_repair_counters', 'm10_tool_loop_repair_counters'),
    ('agent_py_agent.tests.test_conveyor_m10_cure_a_tool_loop.g1_case_tool_loop_response_decision', 'm10_tool_loop_response_decision'),
    ('agent_py_agent.tests.test_conveyor_m10_cure_a_tool_loop.g1_case_unresolved_runtime_issue_decision', 'm10_unresolved_runtime_issue_decision'),
    ('agent_py_agent.tests.test_conveyor_m10_cure_a_tool_loop.g1_case_unresolved_runtime_issue_decision_request', 'm10_unresolved_runtime_issue_decision_request'),
    ('agent_py_agent.tests.test_conveyor_m10_cure_a_tool_loop.g1_case_no_tool_calls_request', 'm10_no_tool_calls_request'),
    ('agent_py_agent.tests.test_conveyor_m10_cure_a_tool_loop.g1_case_model_turn_request', 'm10_model_turn_request'),
    ('agent_py_agent.tests.test_conveyor_m00_fast_dataclass.g1_case_fast_replace_open_class', 'fast_replace_open_class'),
    ('agent_py_agent.tests.test_conveyor_m00_fast_dataclass.g1_case_fast_replace_guarded_class', 'fast_replace_guarded_class'),
    ('agent_py_agent.tests.test_conveyor_m11_cure_a_tool_modules.g1_case_tool_call_runtime_request', 'm11_tool_call_runtime_request'),
    ('agent_py_agent.tests.test_conveyor_m11_cure_a_tool_modules.g1_case_native_compact_candidate', 'm11_native_compact_candidate'),
    ('agent_py_agent.tests.test_conveyor_m11_cure_a_tool_modules.g1_case_tool_loop_request_input', 'm11_tool_loop_request_input'),
    ('agent_py_agent.tests.test_conveyor_m11_cure_a_tool_modules.g1_case_tool_loop_request_projection', 'm11_tool_loop_request_projection'),
    ('agent_py_agent.tests.test_conveyor_m11_cure_a_tool_modules.g1_case_input_modality_decision', 'm11_input_modality_decision'),
    ('agent_py_agent.tests.test_conveyor_m11_cure_a_tool_modules.g1_case_input_modality_filter', 'm11_input_modality_filter'),
    ('agent_py_agent.tests.test_conveyor_m11_cure_a_tool_modules.g1_case_assistant_tool_round_context_request', 'm11_assistant_tool_round_context_request'),
    ('agent_py_agent.tests.test_conveyor_m11_cure_a_tool_modules.g1_case_identical_failure_streak', 'm11_identical_failure_streak'),
    ('agent_py_agent.tests.test_conveyor_m11_cure_a_tool_modules.g1_case_long_tool_content_abort_payload', 'm11_long_tool_content_abort_payload'),
]

# C0 规则 9 的 GIL 探针：3.11/3.12 没有 sys._is_gil_enabled，按“GIL 开启”处理，收集阶段不会 AttributeError。
_GIL_DISABLED = getattr(sys, "_is_gil_enabled", lambda: True)() is False


# 函数用途: 把 “模块.属性” 点分路径解析成工厂函数。
def _resolve(dotted: str):
    module_name, _, attribute = dotted.rpartition(".")
    return getattr(importlib.import_module(module_name), attribute)


# 函数用途: 驱动进程按需抬高打开文件软上限（macOS 终端默认 256，不够 128 对 socketpair）。
def _ensure_open_file_limit() -> None:
    try:
        import resource
    except ImportError:
        return
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft == resource.RLIM_INFINITY or soft >= G1_MIN_OPEN_FILES:
        return
    target = G1_MIN_OPEN_FILES if hard == resource.RLIM_INFINITY else min(hard, G1_MIN_OPEN_FILES)
    resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))


# 函数用途: 用 threads 个线程各跑一个工厂产物，栅栏对齐后计时 seconds 秒，返回总操作数/秒。
def _measure(factory, threads: int, seconds: float) -> float:
    operations = [factory() for _ in range(threads)]
    counts = [0] * threads
    stop = [False]
    failures: list[str] = []
    barrier = threading.Barrier(threads + 1)

    def run(index: int) -> None:
        operation = operations[index]
        try:
            for _ in range(G1_WARMUP_CALLS):
                operation()
        except BaseException as exc:  # noqa: BLE001 - 失败经由 failures 交回主线程
            failures.append(repr(exc))
            barrier.abort()
            return
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            return
        done = 0
        try:
            while not stop[0]:
                operation()
                done += 1
        except BaseException as exc:  # noqa: BLE001 - 失败经由 failures 交回主线程
            failures.append(repr(exc))
        counts[index] = done

    workers = [threading.Thread(target=run, args=(index,), name=f"g1-{threads}-{index}", daemon=True)
               for index in range(threads)]
    for worker in workers:
        worker.start()
    try:
        barrier.wait()
    except threading.BrokenBarrierError:
        pass
    started = time.perf_counter()
    time.sleep(seconds)
    stop[0] = True
    stopped = time.perf_counter()
    for worker in workers:
        worker.join()
    for operation in operations:
        close = getattr(operation, "close", None)
        if callable(close):
            close()
    if failures:
        raise RuntimeError(f"G1 case failed: {failures[:3]}")
    return sum(counts) / (stopped - started)


# 函数用途: 子进程入口：确认 GIL 关闭后按块交替测 16 与 128 线程两档，输出一行 JSON（含各块比值与中位数）。
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="G1 free-threading lock-wait driver")
    parser.add_argument("factory", help="dotted path of a zero-argument callable factory")
    args = parser.parse_args(argv)
    if getattr(sys, "_is_gil_enabled", lambda: True)() is not False:
        print(json.dumps({"error": "gil_enabled"}))
        return 2
    _ensure_open_file_limit()
    factory = _resolve(args.factory)
    all_rates: dict[int, list[float]] = {G1_BASE_THREADS: [], G1_STRESS_THREADS: []}
    block_ratios = []
    for _block in range(G1_BLOCK_COUNT):
        rates: dict[int, list[float]] = {G1_BASE_THREADS: [], G1_STRESS_THREADS: []}
        for threads in G1_SLICE_ORDER:
            seconds = G1_WINDOW_SECONDS / (G1_BLOCK_COUNT * G1_SLICE_ORDER.count(threads))
            rates[threads].append(_measure(factory, threads, seconds))
        block_ratios.append(sum(rates[G1_STRESS_THREADS]) / sum(rates[G1_BASE_THREADS]))
        for threads, values in rates.items():
            all_rates[threads] += values
    print(json.dumps({
        "base_ops_per_second": statistics.fmean(all_rates[G1_BASE_THREADS]),
        "stress_ops_per_second": statistics.fmean(all_rates[G1_STRESS_THREADS]),
        "block_ratios": block_ratios,
        "ratio": statistics.median(block_ratios),
        "gil_disabled": getattr(sys, "_is_gil_enabled", lambda: True)() is False,
    }))
    return 0


def test_cases_are_unique_and_resolve_to_factories():
    labels = [label for _dotted, label in CASES]
    assert len(set(labels)) == len(labels)
    assert len({dotted for dotted, _label in CASES}) == len(CASES)
    for dotted, _label in CASES:
        assert callable(_resolve(dotted)), dotted


@pytest.mark.skipif(not _GIL_DISABLED, reason="G1 只在 GIL 关闭的自由线程解释器上判定")
@pytest.mark.parametrize(("factory", "label"), CASES, ids=[label for _dotted, label in CASES])
def test_throughput_at_128_threads_holds_against_16_threads(factory, label):
    completed = subprocess.run(
        [sys.executable, "-X", "gil=0", "-m", "agent_py_agent.tests.ft.test_ft_lock_wait_gate", factory],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=G1_SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-4000:]
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["gil_disabled"] is True, result
    assert result["ratio"] >= G1_MIN_THROUGHPUT_RATIO, (label, round(result["ratio"], 3), result)


if __name__ == "__main__":
    raise SystemExit(main())
