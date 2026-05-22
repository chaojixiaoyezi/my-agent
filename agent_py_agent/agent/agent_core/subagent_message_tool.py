# LLM: Subagent message tool gives hierarchy runners a bounded command channel.
# 模块用途: 让上层节点给下层节点发送定向消息，或向同一 root task 的 shared board 写广播通知。

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..file_io import append_jsonl
from ..tools import BaseTool, ToolExecutionResult, ToolSpec
from .parameters import _bool_param, _string_list
from .runner_context import current_subagent_run_id

if TYPE_CHECKING:
    from ..core import SimpleAgent
    from ..subagents.models import SubAgentTask


_TOOL_NAME = "subagent_message"


# LLM: SubagentMessageRequest bundles normalized message inputs for direct/broadcast delivery.
# 类用途: 保存 sender、目标、主题和正文等消息字段，避免工具执行函数继续膨胀。
@dataclass(frozen=True)
class SubagentMessageRequest:
    sender_run_id: str
    mode: str
    scope: str
    target_run_ids: list[str]
    topic: str
    body: str
    urgency: str
    requires_ack: bool


# LLM: SubagentMessageTool is the model-callable message lane for upper-to-lower coordination.
# 类用途: 给 root/coordinator 在 runner 内发送下级纠偏消息或共享广播，不让模型直接猜 inbox/blackboard 路径。
class SubagentMessageTool(BaseTool):

    # LLM: __init__ stores the agent facade and stable tool metadata.
    # 函数用途: 初始化子代理消息工具，不执行文件写入。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_subagent_message_spec()

    # LLM: execute validates scope and writes refs-only messages to inbox/outbox or shared board.
    # 函数用途: 根据 mode 发送定向消息或广播；只允许 sender 的下级范围，避免跨任务串线。
    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        request = _message_request(self.agent, params)
        if isinstance(request, ToolExecutionResult):
            return request
        try:
            sender = self.agent.subagents.load(request.sender_run_id)
        except FileNotFoundError:
            return _message_error(f"sender_run_id 不存在: {request.sender_run_id}")
        return _execute_message_request(self.agent, request, sender)


# LLM: _execute_message_request routes the validated bundle without nesting inside the tool class.
# 函数用途: 根据 mode 分派到广播或定向发送逻辑，保持工具 execute 足够薄。
def _execute_message_request(
    agent: object,
    request: SubagentMessageRequest,
    sender: SubAgentTask,
) -> ToolExecutionResult:
    if request.mode == "broadcast":
        return _send_broadcast_message(agent, request, sender)
    return _send_direct_message(agent, request, sender)


# LLM: _send_broadcast_message writes a scoped shared-board message for sender descendants only.
# 函数用途: 校验广播 scope 和可选目标提示，再写 shared messages/blackboard。
def _send_broadcast_message(
    agent: object,
    request: SubagentMessageRequest,
    sender: SubAgentTask,
) -> ToolExecutionResult:
    if request.scope != "descendants":
        return _message_error("broadcast 当前只支持 scope=descendants；平级讨论请用 direct + scope=peers。")
    target_check = _optional_descendant_target_check(agent, sender, request.target_run_ids)
    if isinstance(target_check, ToolExecutionResult):
        return target_check
    payload = _message_payload(request, sender, [])
    refs = _write_broadcast(sender, payload)
    return _message_ok({"mode": "broadcast", "message_id": payload["message_id"], "refs": refs})


# LLM: _send_direct_message writes targeted inbox/outbox messages after relation checks.
# 函数用途: 根据 descendants 或 peers 范围解析目标，然后写入目标 inbox。
def _send_direct_message(
    agent: object,
    request: SubagentMessageRequest,
    sender: SubAgentTask,
) -> ToolExecutionResult:
    targets = _direct_targets(agent, sender, request)
    if isinstance(targets, ToolExecutionResult):
        return targets
    payload = _message_payload(request, sender, [target.id for target in targets])
    refs = _write_direct_messages(sender, targets, payload)
    return _message_ok({"mode": "direct", "message_id": payload["message_id"], "refs": refs})


# LLM: build_subagent_message_spec explains when to use direct messages versus shared broadcasts.
# 函数用途: 构建模型可检索的 subagent_message 工具说明，保持参数和示例稳定。
def build_subagent_message_spec() -> ToolSpec:
    return ToolSpec(
        name=_TOOL_NAME,
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description="给当前层级下属发定向消息，或把统一变更写到 task shared board 广播。",
        use_cases=[
            "coordinator 发现少数 child 路径、需求或验收条件传错，需要一对一纠偏",
            "root/coordinator 需要把同一条需求变更通知大量下级，让它们主动查 shared board",
        ],
        avoid_when=["只是创建新 child 时直接把要求写进 schedule_child_subagents 的 goal 即可"],
        keywords=["消息", "通知", "广播", "纠偏", "inbox", "outbox", "blackboard", "message"],
        parameters={
            "mode": "direct 或 broadcast；direct 写目标 inbox，broadcast 写 shared board/messages",
            "scope": "descendants 或 peers；默认 descendants。broadcast 只允许 descendants，peers 用于平级讨论",
            "target_run_ids": "direct 模式必填；scope=descendants 时目标必须是 sender 下级，scope=peers 时目标必须同父级",
            "topic": "短主题，例如 requirement_change/path_correction/urgent_blocker",
            "body": "消息正文，写清楚变更、路径、下一步和验收影响",
            "urgency": "low/normal/high，默认 normal",
            "requires_ack": "是否要求下级在报告或状态里确认已处理，默认 false",
            "sender_run_id": "默认当前 runner；顶层诊断或恢复时可显式指定",
        },
        examples=[
            (
                '{"tool":"subagent_message","mode":"direct","target_run_ids":["child-1"],'
                '"topic":"path_correction","body":"产物必须写到 /workspace/deliverables/build，不要写 sibling 目录。"}'
            ),
            (
                '{"tool":"subagent_message","mode":"broadcast","scope":"descendants","topic":"requirement_change",'
                '"body":"所有下级在继续前先读取 shared blackboard，注册/登录/购物车按钮都不能失效。",'
                '"requires_ack":true}'
            ),
            (
                '{"tool":"subagent_message","mode":"direct","scope":"peers","target_run_ids":["sibling-2"],'
                '"topic":"team_discussion","body":"我发现 shared app.js 缺少路由函数，请一起核对影响面。"}'
            ),
        ],
    )


# LLM: _message_request normalizes model params while keeping current runner as the default sender.
# 函数用途: 校验消息参数，返回统一 bundle；缺主题、正文或 direct 目标时直接报错。
def _message_request(agent: object, params: dict[str, object]) -> SubagentMessageRequest | ToolExecutionResult:
    params = _message_params(params)
    sender_run_id = str(params.get("sender_run_id") or current_subagent_run_id(agent)).strip()
    mode = str(params.get("mode") or "direct").strip().lower()
    scope = str(params.get("scope") or "descendants").strip().lower()
    topic = str(params.get("topic") or "").strip()
    body = str(params.get("body") or params.get("message") or "").strip()
    if not sender_run_id:
        return _message_error("缺少 sender_run_id；runner 内会自动使用当前 run id。")
    if mode not in {"direct", "broadcast"}:
        return _message_error("mode 必须是 direct 或 broadcast。")
    if scope not in {"descendants", "peers"}:
        return _message_error("scope 必须是 descendants 或 peers。")
    if not topic or not body:
        return _message_error("缺少 topic 或 body。")
    target_run_ids = _string_list(params.get("target_run_ids") or params.get("targets"))
    if mode == "direct" and not target_run_ids:
        return _message_error("direct 模式必须提供 target_run_ids。")
    return SubagentMessageRequest(
        sender_run_id=sender_run_id,
        mode=mode,
        scope=scope,
        target_run_ids=target_run_ids,
        topic=topic,
        body=body,
        urgency=str(params.get("urgency") or "normal").strip().lower(),
        requires_ack=_bool_param(params.get("requires_ack"), default=False),
    )


# LLM: _message_params accepts both legacy flat calls and new bundle-shaped orchestration calls.
# 函数用途: 把模型传入的 subagent_message 参数归一成一层字典；top-level 字段优先，bundle 字段补齐缺省。
def _message_params(params: dict[str, object]) -> dict[str, object]:
    orchestration = params.get("orchestration")
    if not isinstance(orchestration, dict):
        return dict(params)
    normalized = dict(orchestration)
    for key, value in params.items():
        if key not in {"orchestration", "filesystem"}:
            normalized[key] = value
    return normalized


# LLM: _direct_targets verifies that direct messages only flow from an ancestor to descendants.
# 函数用途: 加载并校验目标 run_id，防止一个子代理给 sibling、祖先或无关任务乱发命令。
def _direct_targets(agent: object, sender: SubAgentTask, request: SubagentMessageRequest) -> list[SubAgentTask] | ToolExecutionResult:
    if request.scope == "peers":
        return _peer_targets(agent, sender, request.target_run_ids)
    return _descendant_targets(agent, sender, request.target_run_ids)


# LLM: _descendant_targets enforces upper-to-lower authority for command messages.
# 函数用途: 校验目标 run_id 都在 sender 子树里，避免广播或定向消息越权到别的分支。
def _descendant_targets(agent: object, sender: SubAgentTask, run_ids: list[str]) -> list[SubAgentTask] | ToolExecutionResult:
    targets: list[SubAgentTask] = []
    for run_id in run_ids:
        try:
            target = agent.subagents.load(run_id)
        except FileNotFoundError:
            return _message_error(f"target_run_id 不存在: {run_id}")
        if not _is_descendant(agent, ancestor_id=sender.id, target=target):
            return _message_error(f"target_run_id 不属于 sender 下级: {run_id}")
        targets.append(target)
    return targets


# LLM: _optional_descendant_target_check lets broadcast include a scoped target hint without requiring one.
# 函数用途: 如果广播带了目标 run_id 提示，就先按 sender 子树校验；没带则默认整个 sender 子树。
def _optional_descendant_target_check(
    agent: object,
    sender: SubAgentTask,
    run_ids: list[str],
) -> list[SubAgentTask] | ToolExecutionResult:
    if not run_ids:
        return []
    return _descendant_targets(agent, sender, run_ids)


# LLM: _peer_targets is a narrow future-team channel for siblings under the same parent.
# 函数用途: 允许同父级子代理点对点讨论，但不能借 peers 发送给祖先、孩子或 cousin 分支。
def _peer_targets(agent: object, sender: SubAgentTask, run_ids: list[str]) -> list[SubAgentTask] | ToolExecutionResult:
    sender_parent = str(sender.parent_id or "").strip()
    if not sender_parent:
        return _message_error("scope=peers 需要 sender 有 parent_id。")
    targets: list[SubAgentTask] = []
    for run_id in run_ids:
        try:
            target = agent.subagents.load(run_id)
        except FileNotFoundError:
            return _message_error(f"target_run_id 不存在: {run_id}")
        if target.id == sender.id or str(target.parent_id or "").strip() != sender_parent:
            return _message_error(f"target_run_id 不是同父级平级代理: {run_id}")
        if str(target.root_id or target.id) != str(sender.root_id or sender.id):
            return _message_error(f"target_run_id 不属于同一 root task: {run_id}")
        targets.append(target)
    return targets


# LLM: _is_descendant walks parent refs instead of trusting natural-language lineage.
# 函数用途: 判断目标是否在 sender 的子树中；只读取 task.json，不展开产物正文。
def _is_descendant(agent: object, *, ancestor_id: str, target: SubAgentTask) -> bool:
    parent_id = str(target.parent_id or "").strip()
    while parent_id:
        if parent_id == ancestor_id:
            return True
        try:
            parent = agent.subagents.load(parent_id)
        except FileNotFoundError:
            return False
        parent_id = str(parent.parent_id or "").strip()
    return False


# LLM: _message_payload keeps message rows schema-versioned and reserved-field friendly.
# 函数用途: 组装 inbox/outbox/shared messages 的 JSON payload，后续可扩展 ack、priority 或 routing 字段。
def _message_payload(request: SubagentMessageRequest, sender: SubAgentTask, target_ids: list[str]) -> dict[str, object]:
    now = time.time()
    message_id = f"msg-{_safe_segment(request.sender_run_id)}-{int(now * 1000)}"
    return {
        "version": 1,
        "message_id": message_id,
        "message_type": request.mode,
        "recipient_scope": request.scope,
        "scope_root_run_id": request.sender_run_id if request.scope == "descendants" else "",
        "scope_parent_run_id": str(sender.parent_id or "") if request.scope == "peers" else "",
        "sender_run_id": request.sender_run_id,
        "sender_role": str(sender.role or ""),
        "root_id": str(sender.root_id or sender.id),
        "target_run_ids": target_ids,
        "topic": request.topic,
        "body": request.body,
        "urgency": request.urgency,
        "requires_ack": request.requires_ack,
        "created_at": now,
        "reserved": {},
    }


# LLM: _write_direct_messages writes both sender outbox and each target inbox for auditability.
# 函数用途: 一对一消息写入目标 inbox，同时在 sender outbox 留审计记录；不修改业务产物。
def _write_direct_messages(sender: SubAgentTask, targets: list[SubAgentTask], payload: dict[str, object]) -> dict[str, object]:
    outbox = _message_dir(sender.agent_run_outbox_dir, sender.task_dir, "outbox")
    append_jsonl(outbox / "messages.jsonl", payload)
    refs: dict[str, object] = {"sender_outbox": str(outbox / "messages.jsonl"), "target_inboxes": []}
    for target in targets:
        inbox = _message_dir(target.agent_run_inbox_dir, target.task_dir, "inbox")
        message_file = inbox / f"{payload['message_id']}.json"
        message_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        append_jsonl(inbox / "messages.jsonl", payload)
        refs["target_inboxes"].append(str(message_file))
    return refs


# LLM: _write_broadcast appends one shared-board message and a readable blackboard note.
# 函数用途: 统一通知写入 task shared messages 和 blackboard，让大量下级主动查询同一事实源。
def _write_broadcast(sender: SubAgentTask, payload: dict[str, object]) -> dict[str, str]:
    messages = _shared_messages_path(sender)
    append_jsonl(messages, payload)
    blackboard = _shared_blackboard_path(sender, messages)
    _append_blackboard_note(blackboard, payload)
    return {"shared_messages": str(messages), "shared_blackboard": str(blackboard)}


# LLM: _message_dir resolves inbox/outbox refs without assuming every legacy task has new workspace fields.
# 函数用途: 根据 runtime workspace 字段或旧 task_dir 兜底生成消息目录。
def _message_dir(value: str, task_dir: str, name: str) -> Path:
    path = Path(value) if value else Path(task_dir) / name
    path.mkdir(parents=True, exist_ok=True)
    return path


# LLM: _shared_messages_path prefers the task workspace shared messages file, with a legacy fallback.
# 函数用途: 定位广播 JSONL 文件；路径缺失时落到当前 task_dir/shared/messages.jsonl。
def _shared_messages_path(sender: SubAgentTask) -> Path:
    value = str(sender.task_workspace_shared_messages_jsonl or "").strip()
    path = Path(value) if value else Path(sender.task_dir) / "shared" / "messages.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# LLM: _shared_blackboard_path prefers the task workspace blackboard and stays near messages on fallback.
# 函数用途: 定位可读黑板文件，给下级人工/模型都能快速看到统一通知。
def _shared_blackboard_path(sender: SubAgentTask, messages_path: Path) -> Path:
    value = str(sender.task_workspace_shared_blackboard or "").strip()
    path = Path(value) if value else messages_path.with_name("blackboard.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# LLM: _append_blackboard_note keeps broadcasts visible without rebuilding the existing blackboard rollup.
# 函数用途: 追加一段广播摘要，避免覆盖 shared_workspace 已经生成的黑板内容。
def _append_blackboard_note(path: Path, payload: dict[str, object]) -> None:
    line = (
        "\n\n## Broadcast\n\n"
        f"- message_id: {payload['message_id']}\n"
        f"- topic: {payload['topic']}\n"
        f"- urgency: {payload['urgency']}\n"
        f"- requires_ack: {payload['requires_ack']}\n"
        f"- body: {payload['body']}\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)


# LLM: _safe_segment keeps message filenames portable while preserving enough identity for debugging.
# 函数用途: 把 run_id 压成可作为文件名片段的字符串。
def _safe_segment(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value)[:80] or "run"


# LLM: _message_error keeps failure payloads stable for runner self-correction.
# 函数用途: 生成 subagent_message 的失败结果。
def _message_error(message: str) -> ToolExecutionResult:
    return ToolExecutionResult(_TOOL_NAME, False, message)


# LLM: _message_ok renders a compact refs-only success payload.
# 函数用途: 返回消息 id 和文件引用，不把 inbox/outbox 正文重复灌进模型上下文。
def _message_ok(payload: dict[str, object]) -> ToolExecutionResult:
    return ToolExecutionResult(_TOOL_NAME, True, json.dumps(payload, ensure_ascii=False, indent=2))
