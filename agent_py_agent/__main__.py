
from __future__ import annotations

"""compatibility entrypoint re-exporting the split CLI package.

给人看的解释：
CLI 真实实现已经拆到 `agent_py_agent.cli` 目录。
这个文件只保留 Python `-m agent_py_agent` 入口和老测试/老调用方依赖的导入名字。
"""

from .agent.gateway import (
    AdapterPaths,
    GatewayAskParams,
    GatewayPaths,
    _handle_gateway_request,
    _process_gateway_requests,
    adapter_paths,
    gateway_paths,
    gateway_request_counts,
    gateway_response_path,
    gateway_running,
    gateway_stale_processing,
    is_pid_alive,
    log_gateway_event,
    new_gateway_request_id,
    print_gateway_response,
    process_file_adapter_once,
    read_json_file,
    read_pid,
    rebuild_gateway_index,
    recover_gateway_processing_requests,
    render_gateway_status,
    requeue_gateway_processing_requests,
    submit_gateway_ask,
    tail_lines,
    terminate_pid,
    wait_for_gateway_response,
    wait_for_gateway_running,
    wait_for_pid_exit,
    write_json_file,
)
from .cli.adapter import cmd_adapter, cmd_adapter_file
from .cli.bench_model import cmd_bench_model
from .cli.chat import cmd_chat
from .cli.common import (
    CHAT_PROMPT,
    DEFAULT_CAPABILITY_CONFIG,
    DEFAULT_CONFIG,
    FALLBACK_CHAT_PROMPT,
    ROOT,
    _memory_record_count,
    configure_stdio,
    format_local_time,
    make_agent,
    make_capability_router,
    resolve_workspace_root,
    resolve_workspace_roots,
)
from .cli.daemon import (
    _resolve_daemon_max_runners,
    _resolve_daemon_options,
    _validate_daemon_numbers,
    cmd_daemon,
)
from .cli.gateway_client import (
    cmd_default,
    cmd_gateway,
    cmd_gateway_ask,
    cmd_gateway_result,
    ensure_gateway_started,
)
from .cli.gateway_process import (
    _gateway_heartbeat_loop,
    _gateway_request_loop,
    _gateway_request_worker_loop,
    _write_gateway_heartbeat,
    cmd_gateway_logs,
    cmd_gateway_restart,
    cmd_gateway_run,
    cmd_gateway_start,
    cmd_gateway_status,
    cmd_gateway_stop,
)
from .cli.learning import cmd_learn_accept, cmd_learn_list, cmd_learn_reject, cmd_learn_stats
from .cli.local_commands import (
    cmd_local_doctor,
    cmd_local_index_memory,
    cmd_local_rebuild,
    cmd_local_search,
    cmd_local_store_status,
    cmd_memory_list,
    cmd_memory_search,
    cmd_remember,
    cmd_run,
    cmd_status,
    cmd_timeline,
)
from .cli.local_doctor import (
    _add_doctor_check,
    build_local_doctor_report,
    build_status_suggestions,
    rebuild_local_store,
    rebuild_subagent_index,
)
from .cli.memory_archive_commands import (
    cmd_memory_archive_list,
    cmd_memory_archive_search,
    cmd_memory_resume,
)
from .cli.memory_artifact_commands import cmd_memory_artifact_read
from .cli.memory_commands import cmd_memory_doctor, cmd_memory_route
from .cli.models import ChatJob, DaemonOptions
from .cli.parser import build_parser, main
from .cli.scenario import cmd_scenario_test, print_dispatch_report, run_scenario_suite
from .cli.scenario_cases import (
    ScenarioRetryBackend,
    ScenarioStructuredRepairBackend,
    run_scenario_gateway_restart_case,
    run_scenario_real_model_recovery_case,
    run_scenario_runner_retry_case,
    run_scenario_structured_repair_case,
)
from .cli.scenario_utils import (
    ScenarioPaths,
    build_scenario_prompt,
    build_scenario_runner_instruction,
    create_scenario_workspace,
    load_scenario_agent,
    print_scenario_board,
    print_scenario_step,
    run_scenario_gateway_ask,
    run_scenario_subprocess,
    scenario_tasks_verified,
    write_scenario_config,
    write_scenario_fixture,
    write_scenario_summary,
)
from .cli.subagents import (
    cmd_spawn,
    cmd_subagent_context,
    cmd_subagent_detail,
    cmd_subagent_run,
    cmd_subagents,
    cmd_subagents_apply_actions,
    cmd_subagents_dispatch,
    cmd_subagents_due_check,
    cmd_subagents_memory_gate,
    cmd_subagents_patches,
    cmd_subagents_plan_actions,
    cmd_subagents_probe,
    cmd_subagents_route_capabilities,
    cmd_subagents_workflow_plan,
)

if __name__ == "__main__":
    raise SystemExit(main())
