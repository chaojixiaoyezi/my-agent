"""合同、策略与上下文准备可独立加载，不能隐式引入调度执行和网络后端。"""
from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.parametrize("statement", [
    "from agent_py_agent.agent.conversation.store_guidance import GuidanceStore; import sys; assert callable(GuidanceStore); assert 'agent_py_agent.agent.conversation.store' not in sys.modules",
    "from agent_py_agent.agent.conversation.store_guidance_ledger import GuidanceLedger; import sys; assert callable(GuidanceLedger); assert 'agent_py_agent.agent.conversation.store' not in sys.modules",
    "from agent_py_agent.agent.conversation.store_guidance_submission import GuidanceSubmissions; import sys; assert callable(GuidanceSubmissions); assert 'agent_py_agent.agent.conversation.store' not in sys.modules",
    "from agent_py_agent.agent.conversation.store_guidance_acknowledgements import GuidanceAcknowledgements; import sys; assert callable(GuidanceAcknowledgements); assert 'agent_py_agent.agent.conversation.store' not in sys.modules",
    "from agent_py_agent.agent.conversation.store_guidance_recovery import GuidanceRecovery; import sys; assert callable(GuidanceRecovery); assert 'agent_py_agent.agent.conversation.store' not in sys.modules",
    "from agent_py_agent.agent.conversation.store_guidance_records import GuidanceOnceReceipt; import sys; assert callable(GuidanceOnceReceipt); assert 'agent_py_agent.agent.conversation.store' not in sys.modules",
    "from agent_py_agent.agent.conversation.store_threads import ThreadStore; import sys; assert callable(ThreadStore); assert 'agent_py_agent.agent.conversation.store' not in sys.modules",
    "from agent_py_agent.agent.conversation.store_messages import MessageStore; import sys; assert callable(MessageStore); assert 'agent_py_agent.agent.conversation.store' not in sys.modules",
    "from agent_py_agent.agent.conversation.store_tasks import TaskStore; import sys; assert callable(TaskStore); assert 'agent_py_agent.agent.conversation.store' not in sys.modules",
    "from agent_py_agent.agent.conversation.store_audits import AuditStore; import sys; assert callable(AuditStore); assert 'agent_py_agent.agent.conversation.store' not in sys.modules",
    "from agent_py_agent.agent.conversation.store_goals import GoalStore; import sys; assert callable(GoalStore); assert 'agent_py_agent.agent.conversation.store' not in sys.modules",
    "from agent_py_agent.agent.conversation.store_observations import ObservationStore; import sys; assert callable(ObservationStore); assert 'agent_py_agent.agent.conversation.store' not in sys.modules",
    "from agent_py_agent.agent.conversation.store_wakes import WakeStore; import sys; assert callable(WakeStore); assert 'agent_py_agent.agent.conversation.store' not in sys.modules",
    "from agent_py_agent.agent.conversation.store_progress import ProgressStore; import sys; assert callable(ProgressStore); assert 'agent_py_agent.agent.conversation.store' not in sys.modules",
    "from agent_py_agent.agent.conversation.store_claims import ClaimStore; import sys; assert callable(ClaimStore); assert 'agent_py_agent.agent.conversation.store' not in sys.modules",
    "from agent_py_agent.agent.conversation.store_index import ScanIndexes; import sys; ScanIndexes().get('observations'); assert 'agent_py_agent.agent.conversation.store' not in sys.modules",
    "from agent_py_agent.agent.conversation.store_layout import ConversationStorage; import sys; assert callable(ConversationStorage); assert 'agent_py_agent.agent.conversation.store' not in sys.modules",
    "from agent_py_agent.agent.conversation.store_usage import ModelUsageStore; import sys; assert callable(ModelUsageStore); assert 'agent_py_agent.agent.conversation.store' not in sys.modules",
    "from agent_py_agent.agent.conversation.goal_clock import GoalClockGroup; import sys; GoalClockGroup(); assert 'agent_py_agent.agent.conversation.store' not in sys.modules",
    "from agent_py_agent.agent.conversation.store_io import safe_file_stem; import sys; assert safe_file_stem('thread/scope') == 'thread_scope'; assert 'agent_py_agent.agent.conversation.store' not in sys.modules",
    "from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter; assert callable(BufferedChunkStreamWriter)",
    "from agent_py_agent.agent.gateway_parts.request_context import GatewayConversationContext; GatewayConversationContext()",
    "from agent_py_agent.agent.gateway_parts.request_history import GatewayAssistantTurn; assert GatewayAssistantTurn().metadata() == {}",
    "from agent_py_agent.agent.gateway_parts.request_binding import gateway_runtime_authority; assert gateway_runtime_authority({}, 'request') == {}",
    "from agent_py_agent.agent.gateway_parts.request_prompt import gateway_conversation_history_seed; from types import SimpleNamespace; assert gateway_conversation_history_seed(SimpleNamespace(thread_id='')) is None",
    """
from types import SimpleNamespace
from agent_py_agent.agent.conversation.history_projection import conversation_history_rows
rows = [
    SimpleNamespace(message_id='kept', role='user', content='保留完整历史', metadata={'typed': {'value': 3}}),
    SimpleNamespace(message_id='current', role='user', content='本轮不重复', metadata={'gateway_request_id': 'request'}),
]
errors = []
selected = conversation_history_rows(SimpleNamespace(), 'thread', 'request', errors, rows=rows)
assert len(selected) == 1 and selected[0].message_id == 'kept'
assert selected[0].metadata == {'typed': {'value': 3}} and errors == []
import sys
assert not any(name.startswith('agent_py_agent.agent.gateway_parts.') for name in sys.modules)
""",
    "from agent_py_agent.agent.conversation.compact_carry import compact_overflow_carry; assert callable(compact_overflow_carry)",
    "from agent_py_agent.agent.backends import ModelResponse; ModelResponse(text='', backend='test')",
    "from agent_py_agent.agent.conversation.background_tool_policy import background_tool_policy_decision; background_tool_policy_decision()",
    "from agent_py_agent.agent.conversation.background_context import context_markdown; assert callable(context_markdown)",
    "from agent_py_agent.agent.conversation.background_history_seed import prepare_background_history_or_raise; assert callable(prepare_background_history_or_raise)",
    "from agent_py_agent.agent.gateway_parts.workspace_scope import gateway_request_workspace_scope; assert callable(gateway_request_workspace_scope)",
    "from agent_py_agent.agent.conversation.background_execution import invoke_background_turn; assert callable(invoke_background_turn)",
    "from agent_py_agent.agent.conversation.background_claim import run_claimed; assert callable(run_claimed)",
    "from agent_py_agent.agent.conversation.background_recovery import BackgroundRecoveryGuard; assert callable(BackgroundRecoveryGuard)",
    "from agent_py_agent.agent.turn_end import should_continue_task; assert should_continue_task(object()) == (False, 'not_continuable')",
    "from agent_py_agent.agent.conversation.background_delivery import record_background_response; assert callable(record_background_response)",
])
def test_contract_and_policy_imports_do_not_load_execution_modules(statement):
    code = statement + "\n" + """
import sys
heavy = {
    'agent_py_agent.agent.backends.http',
    'agent_py_agent.agent.backends.factory',
    'agent_py_agent.agent.backends.openai_chat',
    'agent_py_agent.agent.backends.anthropic',
    'agent_py_agent.agent.conversation.runtime',
    'agent_py_agent.agent.gateway_parts.request_execution',
}
assert not (heavy & sys.modules.keys()), sorted(heavy & sys.modules.keys())
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
