# LLM: 会话持久路径在本上下文唯一组装，所有领域必须共用；不持有线程、任务或唤醒业务状态。
# 模块用途: 管理原会话目录与读取侧索引，支持无副作用的只读打开，不迁移任何数据格式。
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .models import WakeSignal
from .store_index import ScanIndexes
from .store_io import safe_file_stem


# LLM: 明确 urgency 字段保持原有归一规则；路径与唤醒创建共用，不读取用户自然语言。
# 函数用途: 将结构化唤醒级别归到现有紧急或普通队列。
def wake_urgency(value: str) -> str:
    return "urgent" if str(value or "").strip().lower() == "urgent" else "normal"


# LLM: 目录初始化和路径计算不拥有生命周期；各领域显式接收此上下文，不能另建 canonical 根。
# 类用途: 为同一会话 Store 提供唯一持久位置和只读扫描投影。
class ConversationStorage:
    # LLM: Passive Gateway replay may open an already-existing owner store without creating any
    # directories. Runtime writers keep the default initialize=True; read-only callers must never
    # pass initialize=False for a path that has not already been resolved inside an authenticated
    # owner home.
    # 函数用途: 建立 canonical 路径与扫描上下文；被动重放只读打开已有目录，不读取配置或密钥。
    def __init__(self, root: str | Path, *, initialize: bool = True):
        self.root = Path(root)
        self.threads_dir = self.root / "threads"
        self.messages_dir = self.root / "messages"
        self.model_usage_dir = self.root / "model_usage"
        self.tasks_dir = self.root / "tasks"
        self.policies_dir = self.root / "progress_policies"
        self.observations_dir = self.root / "observations"
        self.guidance_dir = self.root / "guidance"
        self.guidance_dedupe_dir = self.root / "guidance_dedupe"
        self.guidance_turn_index_dir = self.root / "guidance_turn_index"
        self.guidance_input_index_dir = self.root / "guidance_input_index"
        self.guidance_submission_batches_dir = self.root / "guidance_submission_batches"
        self.guidance_ack_batches_dir = self.root / "guidance_ack_batches"
        self.goals_dir = self.root / "goals"
        self.wake_queue_dir = self.root / "wake_queue"
        self.wake_handled_dir = self.wake_queue_dir / "handled"
        self.observation_handled_path = self.root / "observation_handled.json"
        self.guidance_delivered_path = self.root / "guidance_delivered.json"
        self.bindings_path = self.root / "channel_bindings.json"
        self.user_latest_path = self.root / "user_latest_threads.json"
        self.background_claims_dir = self.root / "background_claims"
        self.wake_dedupe_dir = self.wake_queue_dir / "dedupe"
        self.indexes = ScanIndexes()
        if initialize:
            self.ensure_dirs()

    # LLM: 只创建既有目录集合；被动读取不能调用本入口，不写业务文件。
    # 函数用途: 在可写 Store 初始化时准备原有目录，失败直接暴露给调用方。
    def ensure_dirs(self) -> None:
        for path in self.managed_dirs():
            path.mkdir(parents=True, exist_ok=True)

    # LLM: 目录集合是初始化唯一来源，不引入按任务命名的新数据目录。
    # 函数用途: 列出本会话存储管理的固定目录，供初始化和维护核对。
    def managed_dirs(self) -> tuple[Path, ...]:
        return (
            self.threads_dir,
            self.messages_dir,
            self.model_usage_dir,
            self.tasks_dir,
            self.policies_dir,
            self.observations_dir,
            self.guidance_dir,
            self.guidance_dedupe_dir,
            self.guidance_turn_index_dir,
            self.guidance_input_index_dir,
            self.guidance_submission_batches_dir,
            self.guidance_ack_batches_dir,
            self.goals_dir,
            self.background_claims_dir,
            self.wake_dedupe_dir,
            self.wake_queue_dir / "urgent",
            self.wake_queue_dir / "normal",
            self.wake_handled_dir,
        )

    # LLM: 线程 ID 的原路径映射保持，权限与存在性校验仍在调用领域。
    # 函数用途: 定位 canonical 线程 JSON，供身份校验和原子更新共用。
    def thread_path(self, thread_id: str) -> Path:
        return self.threads_dir / f"{thread_id}.json"

    # LLM: 消息文件与原线程一一对应；历史读取和追加必须使用同一路径。
    # 函数用途: 定位完整 transcript 的 JSONL 文件，不读取或裁剪内容。
    def message_path(self, thread_id: str) -> Path:
        return self.messages_dir / f"{thread_id}.jsonl"

    # LLM: 任务链接仍以显式任务 ID 寻址，不按标题或提示词推断归属。
    # 函数用途: 定位线程关联的任务链接文件，事务锁沿用这个路径。
    def task_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.json"

    # LLM: 策略 ID 原样映射到现有文件；路径计算不授权调度或修改到期时间。
    # 函数用途: 定位进度策略 JSON，供原子策略更新和扫描核对。
    def policy_path(self, policy_id: str) -> Path:
        return self.policies_dir / f"{policy_id}.json"

    # LLM: 执行 claim 按原安全文件名映射，所有租约操作必须共用这一文件锁。
    # 函数用途: 定位精确执行范围的租约文件，不决定是否允许接管。
    def background_claim_path(self, thread_id: str) -> Path:
        return self.background_claims_dir / f"{safe_file_stem(thread_id)}.json"

    # LLM: 观察事件沿原 thread ID 存储；路径定位不消费事件。
    # 函数用途: 定位本线程的观察 JSONL，供追加与未处理扫描共用。
    def observation_path(self, thread_id: str) -> Path:
        return self.observations_dir / f"{thread_id}.jsonl"

    # LLM: 补充消息按显式目标类型和 ID 沿用原安全文件名，投递状态另读回执。
    # 函数用途: 定位一个接收目标的 guidance 队列，不改变消息状态。
    def guidance_path(self, target_type: str, target_id: str) -> Path:
        return (
            self.guidance_dir / f"{safe_file_stem(target_type)}.{safe_file_stem(target_id)}.jsonl"
        )

    # LLM: Dedupe keys may contain channel and thread identifiers, so filenames use a full
    # cryptographic digest while the original key remains inside the validated receipt.
    # 函数用途: 返回一条 guidance 幂等回执的唯一安全文件路径。
    def guidance_dedupe_path(self, dedupe_key: str) -> Path:
        digest = hashlib.sha256(str(dedupe_key).encode("utf-8")).hexdigest()
        return self.guidance_dedupe_dir / f"{digest}.json"

    # LLM: The per-turn folder is a bounded lookup projection, never delivery authority. Receipt
    # state remains canonical and every ref stores the original dedupe key for validation.
    # 函数用途: 返回精确回合的补充消息索引目录，避免结束时扫描所有历史回执。
    def guidance_turn_index_path(self, expected_turn_id: str, dedupe_key: str) -> Path:
        turn_digest = hashlib.sha256(str(expected_turn_id).encode("utf-8")).hexdigest()
        receipt_digest = hashlib.sha256(str(dedupe_key).encode("utf-8")).hexdigest()
        return self.guidance_turn_index_dir / turn_digest / f"{receipt_digest}.json"

    # LLM: Gateway ingress ids are opaque and owner-scoped; only their digest appears in a path.
    # 函数用途: 返回普通消息回执到 guidance 回执的反向索引路径，供崩溃恢复找回半完成接入。
    def guidance_input_index_path(self, gateway_input_request_id: str) -> Path:
        digest = hashlib.sha256(gateway_input_request_id.encode("utf-8")).hexdigest()
        return self.guidance_input_index_dir / f"{digest}.json"

    # LLM: One provider-ack batch is immutable and addressed by exact turn plus its guidance ids.
    # 函数用途: 返回模型一次确认接收整批补充消息的原子提交文件位置。
    def guidance_ack_batch_path(
        self,
        expected_turn_id: str,
        guidance_ids: tuple[str, ...],
    ) -> Path:
        turn_digest = hashlib.sha256(expected_turn_id.encode("utf-8")).hexdigest()
        batch_source = json.dumps(sorted(guidance_ids), ensure_ascii=False, separators=(",", ":"))
        batch_digest = hashlib.sha256(batch_source.encode("utf-8")).hexdigest()
        return self.guidance_ack_batches_dir / turn_digest / f"{batch_digest}.json"

    # LLM: A provider call id is host-generated and immutable. Hashing it under the exact turn
    # yields one atomic provider-boundary authority without exposing identifiers in filenames.
    # 函数用途: 返回一次模型物理调用所对应的补充消息提交批次文件。
    def guidance_submission_batch_path(
        self,
        expected_turn_id: str,
        provider_call_id: str,
    ) -> Path:
        turn_digest = hashlib.sha256(expected_turn_id.encode("utf-8")).hexdigest()
        batch_digest = hashlib.sha256(provider_call_id.encode("utf-8")).hexdigest()
        return self.guidance_submission_batches_dir / turn_digest / f"{batch_digest}.json"

    # LLM: One sanitized path per thread is the sole durable goal record authority.
    # 函数用途: 返回当前 conversation thread 唯一的持续目标文件路径。
    def goal_path(self, thread_id: str) -> Path:
        return self.goals_dir / f"{safe_file_stem(thread_id)}.json"

    # LLM: 只按结构化 urgency 与唤醒 ID 沿用队列路径，不能从正文判断紧急程度。
    # 函数用途: 定位待处理唤醒文件，不生成或消费唤醒。
    def wake_signal_path(self, signal: WakeSignal) -> Path:
        return self.wake_queue_dir / wake_urgency(signal.urgency) / f"{signal.wake_signal_id}.json"

    # LLM: 去重键与线程共同决定原回执路径，不增加第二份投递状态。
    # 函数用途: 定位精确去重键的唤醒回执文件。
    def wake_dedupe_path(self, thread_id: str, dedupe_key: str) -> Path:
        return (
            self.wake_dedupe_dir / f"{safe_file_stem(thread_id)}.{safe_file_stem(dedupe_key)}.json"
        )
