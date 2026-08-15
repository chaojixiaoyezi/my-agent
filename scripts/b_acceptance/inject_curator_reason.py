"""B项验收辅助:向指定 owner 的 curator state.json 注入 pending reason(紧急车道)。

复用生产 MemoryCuratorStateStore.request(带文件锁+generation 语义),不手改 JSON。
用法: python3 inject_curator_reason.py <state.json路径> [reason=admin]
"""
import sys
from pathlib import Path

from agent_py_agent.agent.memory_store.curator_state import MemoryCuratorStateStore

path = sys.argv[1]
reason = sys.argv[2] if len(sys.argv) > 2 else "admin"
store = MemoryCuratorStateStore(path)
state = store.request(reason)
print(f"injected reason={reason}")
print(f"pending_reasons={list(state.pending_reasons)}")
print(f"generation={state.pending_reason_generations.get(reason)}")
print(f"requested_at={state.pending_requested_at}")
