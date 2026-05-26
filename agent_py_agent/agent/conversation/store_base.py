# LLM: Base paths for the long-running conversation control plane.
# 模块用途: 初始化长期会话账本目录，并集中维护 thread/message/task/policy/wake 路径。

from __future__ import annotations

from pathlib import Path

from .models import WakeSignal
from .store_common import safe_file_stem, wake_urgency


class ConversationBaseStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.threads_dir = self.root / "threads"
        self.messages_dir = self.root / "messages"
        self.tasks_dir = self.root / "tasks"
        self.policies_dir = self.root / "progress_policies"
        self.observations_dir = self.root / "observations"
        self.wake_queue_dir = self.root / "wake_queue"
        self.wake_handled_dir = self.wake_queue_dir / "handled"
        self.observation_handled_path = self.root / "observation_handled.json"
        self.bindings_path = self.root / "channel_bindings.json"
        self.user_latest_path = self.root / "user_latest_threads.json"
        self.background_claims_dir = self.root / "background_claims"
        self.wake_dedupe_dir = self.wake_queue_dir / "dedupe"
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        for path in self._managed_dirs():
            path.mkdir(parents=True, exist_ok=True)

    def _managed_dirs(self) -> tuple[Path, ...]:
        return (
            self.threads_dir,
            self.messages_dir,
            self.tasks_dir,
            self.policies_dir,
            self.observations_dir,
            self.background_claims_dir,
            self.wake_dedupe_dir,
            self.wake_queue_dir / "urgent",
            self.wake_queue_dir / "normal",
            self.wake_handled_dir,
        )

    def _thread_path(self, thread_id: str) -> Path:
        return self.threads_dir / f"{thread_id}.json"

    def _message_path(self, thread_id: str) -> Path:
        return self.messages_dir / f"{thread_id}.jsonl"

    def _task_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.json"

    def _policy_path(self, policy_id: str) -> Path:
        return self.policies_dir / f"{policy_id}.json"

    def _background_claim_path(self, thread_id: str) -> Path:
        return self.background_claims_dir / f"{safe_file_stem(thread_id)}.json"

    def _observation_path(self, thread_id: str) -> Path:
        return self.observations_dir / f"{thread_id}.jsonl"

    def _wake_signal_path(self, signal: WakeSignal) -> Path:
        return self.wake_queue_dir / wake_urgency(signal.urgency) / f"{signal.wake_signal_id}.json"

    def _wake_dedupe_path(self, thread_id: str, dedupe_key: str) -> Path:
        return self.wake_dedupe_dir / f"{safe_file_stem(thread_id)}.{safe_file_stem(dedupe_key)}.json"
